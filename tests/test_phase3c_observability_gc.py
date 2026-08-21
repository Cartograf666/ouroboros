"""Continuity Phase 3C: child forensic refs survive headless-drive GC."""

from __future__ import annotations

import gzip
import hashlib
import json
import pathlib
from concurrent.futures import ThreadPoolExecutor

from ouroboros.headless import (
    copy_child_task_result,
    prepare_task_drive,
    prune_headless_task_drives,
    remove_subagent_task_drive,
)
from ouroboros.observability import persist_call, read_blob_ref, write_blob
from ouroboros.task_results import STATUS_COMPLETED, write_task_result


def _child(tmp_path: pathlib.Path, task_id: str) -> tuple[pathlib.Path, pathlib.Path]:
    parent = tmp_path / "data"
    parent.mkdir()
    child = prepare_task_drive(parent, task_id, "empty")
    assert child is not None
    return parent, child


def _future_now() -> float:
    return 4_000_000_000.0


def _manifest(ref: dict) -> dict:
    return json.loads(pathlib.Path(ref["path"]).read_text(encoding="utf-8"))


def test_copyback_promotes_trace_manifest_and_blobs_before_headless_gc(tmp_path):
    task_id = "phase3c-trace"
    parent, child = _child(tmp_path, task_id)
    request = persist_call(
        child,
        task_id=task_id,
        call_id="llm_request",
        call_type="llm_request",
        payload={"prompt": "exact prompt", "reasoning": "exact reasoning"},
    )
    response = persist_call(
        child,
        task_id=task_id,
        call_id="llm_response",
        call_type="llm_response",
        payload={"response": "exact response"},
    )
    tool = persist_call(
        child,
        task_id=task_id,
        call_id="tool_call",
        call_type="tool_call",
        payload={"tool": "read_file", "result": "exact tool result"},
    )
    write_task_result(
        child,
        task_id,
        STATUS_COMPLETED,
        result="done",
        artifact_status="ready",
        trace_refs={
            "llm_call_refs": [{
                "request_ref": request["manifest_ref"],
                "response_ref": response["manifest_ref"],
            }],
            "tool_call_refs": [{
                "manifest_ref": tool["manifest_ref"],
                "redacted_projection_ref": tool["redacted_projection_ref"],
            }],
        },
    )

    copied = copy_child_task_result(parent, {"id": task_id, "drive_root": str(child)})

    assert copied is not None
    assert copied["child_ref_promotion"]["status"] == "complete"
    promoted_manifest_ref = copied["trace_refs"]["llm_call_refs"][0]["request_ref"]
    assert pathlib.Path(promoted_manifest_ref["path"]).is_relative_to(parent / "observability")
    promoted_manifest = _manifest(promoted_manifest_ref)
    assert read_blob_ref(parent, promoted_manifest["full_payload_ref"]) == {
        "prompt": "exact prompt",
        "reasoning": "exact reasoning",
    }
    promoted_response_ref = copied["trace_refs"]["llm_call_refs"][0]["response_ref"]
    assert read_blob_ref(
        parent, _manifest(promoted_response_ref)["full_payload_ref"]
    ) == {"response": "exact response"}
    promoted_tool_ref = copied["trace_refs"]["tool_call_refs"][0]["manifest_ref"]
    assert read_blob_ref(parent, _manifest(promoted_tool_ref)["full_payload_ref"])[
        "result"
    ] == "exact tool result"

    report = prune_headless_task_drives(parent, retention_days=7, now=_future_now())
    assert report["pruned"][0]["task_id"] == task_id
    assert not child.exists()
    assert read_blob_ref(parent, promoted_manifest["full_payload_ref"])["prompt"] == "exact prompt"
    assert read_blob_ref(parent, _manifest(promoted_response_ref)["full_payload_ref"])[
        "response"
    ] == "exact response"
    assert read_blob_ref(parent, _manifest(promoted_tool_ref)["full_payload_ref"])[
        "tool"
    ] == "read_file"


def test_copyback_promotes_service_full_log_refs_in_durable_evidence_and_tool_payload(tmp_path):
    task_id = "phase3c-service"
    parent, child = _child(tmp_path, task_id)
    log_ref = write_blob(child, "READY\nfull service log\n", kind="txt")
    tool_trace = persist_call(
        child,
        task_id=task_id,
        call_id="tool_service_logs",
        call_type="tool_call",
        payload={
            "tool": "service_logs",
            "result": json.dumps({"tail": "READY", "full_log_ref": log_ref}),
        },
    )
    write_task_result(
        child,
        task_id,
        STATUS_COMPLETED,
        result="done",
        artifact_status="ready",
        trace_refs={"tool_call_refs": [{"manifest_ref": tool_trace["manifest_ref"]}]},
        verification_ledger={
            "entries": [{
                "kind": "runtime_event",
                "services": [{"log_finalization": {"full_log_ref": log_ref}}],
            }],
        },
    )

    copied = copy_child_task_result(parent, {"id": task_id, "drive_root": str(child)})

    assert copied is not None
    evidence_ref = copied["verification_ledger"]["entries"][0]["services"][0][
        "log_finalization"
    ]["full_log_ref"]
    assert read_blob_ref(parent, evidence_ref, expected_kind="txt") == "READY\nfull service log\n"
    tool_manifest = _manifest(copied["trace_refs"]["tool_call_refs"][0]["manifest_ref"])
    tool_payload = read_blob_ref(parent, tool_manifest["full_payload_ref"])
    nested_ref = json.loads(tool_payload["result"])["full_log_ref"]
    assert read_blob_ref(parent, nested_ref, expected_kind="txt") == "READY\nfull service log\n"

    prune_headless_task_drives(parent, retention_days=0, now=_future_now())
    assert not child.exists()
    assert read_blob_ref(parent, evidence_ref, expected_kind="txt").endswith("service log\n")
    assert read_blob_ref(parent, nested_ref, expected_kind="txt").startswith("READY")


def test_interrupted_live_ref_promotion_blocks_gc_until_idempotent_retry(
    tmp_path, monkeypatch,
):
    import ouroboros.observability as observability

    task_id = "phase3c-interrupted"
    parent, child = _child(tmp_path, task_id)
    trace = persist_call(
        child,
        task_id=task_id,
        call_id="tool_call",
        call_type="tool_call",
        payload={"result": "must survive"},
    )
    write_task_result(
        child,
        task_id,
        STATUS_COMPLETED,
        result="done",
        artifact_status="ready",
        trace_refs={"tool_call_refs": [{"manifest_ref": trace["manifest_ref"]}]},
    )
    real = observability.promote_call_manifest_ref
    monkeypatch.setattr(
        observability,
        "promote_call_manifest_ref",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("interrupted copy")),
    )

    copied = copy_child_task_result(parent, {"id": task_id, "drive_root": str(child)})

    assert copied is not None
    assert copied["child_ref_promotion"]["status"] == "incomplete"
    assert copied["child_ref_promotion"]["pending_refs"]
    assert remove_subagent_task_drive(parent, task_id) is False
    report = prune_headless_task_drives(parent, retention_days=0, now=_future_now())
    assert report["pruned"] == []
    assert report["skipped"][0]["reason"] == "child_refs_unpromoted"
    assert child.exists()

    monkeypatch.setattr(observability, "promote_call_manifest_ref", real)
    retried = copy_child_task_result(parent, {"id": task_id, "drive_root": str(child)})
    assert retried is not None
    assert retried["child_ref_promotion"]["status"] == "complete"
    assert retried["child_ref_promotion"]["pending_refs"] == []
    assert prune_headless_task_drives(parent, retention_days=0, now=_future_now())["pruned"]


def test_digest_mismatch_becomes_typed_unavailable_and_does_not_pin_drive(tmp_path):
    task_id = "phase3c-digest"
    parent, child = _child(tmp_path, task_id)
    ref = write_blob(child, {"result": "original"})
    with gzip.open(ref["path"], "wb") as handle:
        handle.write(b'{"result":"tampered"}')
    write_task_result(
        child,
        task_id,
        STATUS_COMPLETED,
        result="done",
        artifact_status="ready",
        trace_refs={"tool_call_refs": [{"redacted_projection_ref": ref}]},
    )

    copied = copy_child_task_result(parent, {"id": task_id, "drive_root": str(child)})

    assert copied is not None
    unavailable = copied["trace_refs"]["tool_call_refs"][0]["redacted_projection_ref"]
    assert unavailable["availability"] == "unavailable"
    assert unavailable["reason"] == "digest_mismatch"
    assert "path" not in unavailable
    assert copied["child_ref_promotion"]["unavailable_refs"]
    assert copied["child_ref_promotion"]["pending_refs"] == []
    assert prune_headless_task_drives(parent, retention_days=0, now=_future_now())["pruned"]


def test_concurrent_copyback_is_idempotent_and_copies_only_referenced_source_handle(
    tmp_path,
):
    task_id = "phase3c-source-handles"
    parent, child = _child(tmp_path, task_id)
    source_dir = child / "task_results" / "artifacts" / task_id / "source_handles" / "tool_results"
    source_dir.mkdir(parents=True)
    source_bytes = b"actor promised source"
    source_digest = hashlib.sha256(source_bytes).hexdigest()
    source = source_dir / ("tool-" + source_digest + ".txt")
    source.write_bytes(source_bytes)
    unreferenced_source_bytes = b"unreferenced source handle"
    unreferenced_source = source_dir / (
        "unused-" + hashlib.sha256(unreferenced_source_bytes).hexdigest() + ".txt"
    )
    unreferenced_source.write_bytes(unreferenced_source_bytes)
    unrelated = child / "task_results" / "artifacts" / task_id / "unrelated.txt"
    unrelated.write_text("must not copy", encoding="utf-8")
    unreferenced_blob = write_blob(child, {"unreferenced": True})
    trace = persist_call(
        child,
        task_id=task_id,
        call_id="duplicate-tool-call",
        call_type="tool_call",
        payload={"result": "copy once by content identity"},
    )
    source_ref = {
        "kind": "task_source",
        "root": "artifact_store",
        "path": f"source_handles/tool_results/{source.name}",
        "size": len(source_bytes),
        "sha256": source_digest,
        "read": {
            "tool": "read_file",
            "root": "artifact_store",
            "path": f"source_handles/tool_results/{source.name}",
        },
    }
    write_task_result(
        child,
        task_id,
        STATUS_COMPLETED,
        result="done",
        artifact_status="ready",
        trace_refs={"tool_call_refs": [{"manifest_ref": trace["manifest_ref"]}]},
        review_evidence={"exact_source_ref": source_ref},
    )

    task = {"id": task_id, "drive_root": str(child)}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(
            pool.map(
                lambda _ordinal: copy_child_task_result(parent, task),
                range(2),
            )
        )

    assert first is not None and second is not None
    assert first["child_ref_promotion"] == second["child_ref_promotion"]
    assert first["trace_refs"] == second["trace_refs"]
    assert first["review_evidence"]["exact_source_ref"] == source_ref
    copied_source = parent / "task_results" / "artifacts" / task_id / source.relative_to(
        child / "task_results" / "artifacts" / task_id
    )
    assert copied_source.read_text(encoding="utf-8") == "actor promised source"
    copied_unreferenced_source = (
        parent
        / "task_results"
        / "artifacts"
        / task_id
        / unreferenced_source.relative_to(
            child / "task_results" / "artifacts" / task_id
        )
    )
    assert not copied_unreferenced_source.exists()
    assert not (parent / "task_results" / "artifacts" / task_id / "unrelated.txt").exists()
    assert not (parent / "observability" / "blobs" / pathlib.Path(unreferenced_blob["path"]).name).exists()


def test_legacy_missing_child_ref_is_typed_gap_without_permanent_retention(tmp_path):
    task_id = "phase3c-legacy-gap"
    parent, child = _child(tmp_path, task_id)
    missing = {
        "sha256": "b" * 64,
        "path": str(child / "observability" / "blobs" / (("b" * 64) + ".json.gz")),
        "kind": "json",
        "encoding": "gzip",
        "size": 12,
        "compressed_size": 20,
    }
    write_task_result(
        child,
        task_id,
        STATUS_COMPLETED,
        result="legacy",
        artifact_status="ready",
        trace_refs={"tool_call_refs": [{"redacted_projection_ref": missing}]},
    )

    copied = copy_child_task_result(parent, {"id": task_id, "drive_root": str(child)})

    assert copied is not None
    gap = copied["trace_refs"]["tool_call_refs"][0]["redacted_projection_ref"]
    assert gap["availability"] == "unavailable"
    assert gap["reason"] == "source_missing"
    assert copied["child_ref_promotion"]["status"] == "complete"
    assert prune_headless_task_drives(parent, retention_days=0, now=_future_now())["pruned"]
