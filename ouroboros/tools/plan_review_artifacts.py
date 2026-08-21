"""Exact plan-review waves and reviewer-continuation inputs.

The hot task-result state remains bounded. This companion uses the existing
task artifact store for immutable authority bytes and reconstructs either an
API transcript or a Claudexor thread id for the next evidence turn. It adds no
store, transport route, or review policy of its own.
"""

from __future__ import annotations

import dataclasses
from hashlib import sha256
import json
import pathlib
from typing import Any, Dict, List, Optional


def persist_wave(drive_root: Any, task_id: str, wave: Dict[str, Any]) -> Dict[str, Any]:
    from ouroboros.artifacts import store_task_artifact_bytes
    from ouroboros.observability import redact_projection
    from ouroboros.utils import utc_now_iso

    fingerprint = str(wave.get("request_fingerprint") or "")
    if len(fingerprint) != 64:
        raise ValueError("plan-review wave artifact needs a full fingerprint")
    payload = {
        **wave,
        "artifact_meta": {
            "kind": "plan_review_wave",
            "schema_version": 1,
            "producer_task_id": task_id,
            "producer_root": "artifact_store",
            "source_generation": int(wave.get("cycle_index") or 0),
            "created_at": str(
                wave.get("disposition_recorded_at") or wave.get("reviewed_at") or utc_now_iso()
            ),
            "read_operation": "read_file",
            "retention_owner": "task_artifact_store",
        },
    }
    payload = redact_projection(payload).value
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    digest = sha256(raw).hexdigest()
    cycle = int(wave.get("cycle_index") or 0)
    return store_task_artifact_bytes(
        drive_root, task_id,
        f"plan-review-wave-{cycle:04d}-{fingerprint}-{digest[:12]}.json",
        raw, kind="plan_review_wave",
    )


def read_wave(drive_root: Any, task_id: str, ref: Dict[str, Any]) -> Dict[str, Any]:
    from ouroboros.artifacts import task_artifact_dir_path

    if not isinstance(ref, dict) or ref.get("root") != "artifact_store":
        raise ValueError("invalid plan-review wave artifact ref")
    name = pathlib.Path(str(ref.get("path") or "")).name
    if not name or name != str(ref.get("path") or ""):
        raise ValueError("invalid plan-review wave artifact path")
    raw = (task_artifact_dir_path(drive_root, task_id, create=False) / name).read_bytes()
    if len(raw) != int(ref.get("bytes") or -1) or sha256(raw).hexdigest() != str(ref.get("sha256") or ""):
        raise ValueError("plan-review wave artifact digest mismatch")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("plan-review wave artifact is not an object")
    return value


def authority_wave(drive_root: Any, task_id: str, hot_wave: Optional[dict]) -> Optional[dict]:
    """Materialize exact authority while retaining newer hot lifecycle stamps."""
    if not isinstance(hot_wave, dict):
        return None
    ref = hot_wave.get("wave_artifact") if isinstance(hot_wave.get("wave_artifact"), dict) else {}
    if not ref:
        return hot_wave
    exact = read_wave(drive_root, task_id, ref)
    return {**exact, **hot_wave, "findings": list(exact.get("findings") or [])}


def hot_index_wave(wave: dict, *, page_size: int) -> dict:
    """Keep a bounded per-slot page; exact authority stays in ``wave_artifact``."""
    findings = [dict(row) for row in wave.get("findings") or [] if isinstance(row, dict)]
    counts: Dict[str, int] = {}
    page = []
    for finding in findings:
        slot = str(finding.get("slot") or "")
        count = counts.get(slot, 0)
        counts[slot] = count + 1
        if count < max(1, int(page_size)):
            page.append(finding)
    if len(page) == len(findings):
        return wave
    return {
        **wave, "findings": page, "findings_total": len(findings),
        "findings_paged": True,
    }


def continuation_state(
    state_root: pathlib.Path, task_id: str, previous: Optional[dict], slots: List[Any],
    manifest: dict, *, user_content: str,
) -> tuple[List[Any], Dict[str, List[Dict[str, Any]]], Dict[str, str], str]:
    """Resolve one evidence continuation and its fail-closed reason."""
    if not manifest.get("reviewer_requested"):
        return slots, {}, {}, ""
    rebound, messages, threads, error = continuation_inputs(
        state_root, task_id, previous, slots, user_content=user_content,
    )
    requested = {str(item) for item in manifest.get("reviewer_requested") or []}
    missing = [
        dict(item) for item in manifest.get("omissions") or []
        if str(item.get("locator") or "") in requested
    ]
    reason = error or (
        "requested_evidence_unavailable:" + ",".join(
            f"{item.get('locator')}={item.get('reason')}" for item in missing
        ) if missing else ""
    )
    return rebound, messages, threads, str(reason or "")


def record_exact_wave(
    state_root: pathlib.Path, task_id: str, wave: dict, exact: dict,
    *, need_evidence_seen: List[str], page_size: int,
) -> dict:
    """Persist exact bytes first, then publish their bounded hot index."""
    from ouroboros.task_results import record_plan_review_wave

    wave["wave_artifact"] = persist_wave(state_root, task_id, exact)
    return record_plan_review_wave(
        state_root, task_id, hot_index_wave(wave, page_size=page_size),
        need_evidence_seen=need_evidence_seen,
    )


def record_cannot_verify_attempt(
    state_root: pathlib.Path, task_id: str, *, cycle_index: int, fingerprint: str,
    previous: Optional[dict], spec: dict, plan_prose: str, manifest: dict,
    manifest_hash: str, constitutional: bool, constitutional_note: str,
    slots: List[Any], enforcement: str, cap: Optional[int], reason: str,
    reviewer_config_fingerprint: str, reviewed_at: str, system_prompt: str,
    user_content: str, session_task: str, need_evidence_seen: List[str], page_size: int,
) -> dict:
    """Persist an unpaid non-clean continuation refusal before hot projection."""
    wave = cannot_verify_wave(
        cycle_index=cycle_index, fingerprint=fingerprint, previous=previous,
        spec=spec, plan_prose=plan_prose, manifest=manifest,
        manifest_hash=manifest_hash, constitutional=constitutional,
        constitutional_note=constitutional_note, slots=slots,
        enforcement=enforcement, cap=cap, reason=reason,
        reviewer_config_fingerprint=reviewer_config_fingerprint, reviewed_at=reviewed_at,
    )
    exact = exact_wave(
        wave, plan_prose=plan_prose, manifest=manifest, slots=slots, rows=[],
        system_prompt=system_prompt, user_content=user_content,
        session_task=session_task, slot_messages={},
    )
    return record_exact_wave(
        state_root, task_id, wave, exact,
        need_evidence_seen=need_evidence_seen, page_size=page_size,
    )


def slot_row(slot: Any) -> dict:
    route = getattr(slot, "route", "api_chat")
    return {
        "slot_id": str(getattr(slot, "slot_id", "") or ""),
        "model": str(getattr(slot, "model", "") or ""),
        "effort": str(getattr(slot, "effort", "") or ""),
        "route": str(getattr(route, "value", route) or "api_chat"),
        "session_target": str(getattr(slot, "session_target", "") or ""),
        "session_profile": str(getattr(slot, "session_profile", "") or ""),
    }


def continuation_inputs(
    state_root: pathlib.Path, task_id: str, previous: Optional[dict], slots: List[Any],
    *, user_content: str,
) -> tuple[List[Any], Dict[str, List[Dict[str, Any]]], Dict[str, str], Optional[str]]:
    if not previous:
        return slots, {}, {}, "prior_exact_wave_missing"
    ref = previous.get("wave_artifact") if isinstance(previous.get("wave_artifact"), dict) else {}
    if not ref:
        return slots, {}, {}, "prior_exact_wave_ref_missing"
    try:
        exact = read_wave(state_root, task_id, ref)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return slots, {}, {}, f"prior_exact_wave_unreadable:{type(exc).__name__}"
    configs = {str(r.get("slot_id") or ""): r for r in exact.get("slots") or [] if isinstance(r, dict)}
    outputs = {str(r.get("slot_id") or ""): r for r in exact.get("reviewer_outputs") or [] if isinstance(r, dict)}
    rebound, slot_messages, session_threads = [], {}, {}
    from ouroboros.review_execution import ReviewRouteKind

    for current in slots:
        sid = str(getattr(current, "slot_id", "") or "")
        config, output = configs.get(sid), outputs.get(sid)
        if not config or not output:
            return slots, {}, {}, f"prior_slot_receipt_missing:{sid}"
        route = ReviewRouteKind(str(config.get("route") or "api_chat"))
        rebound.append(dataclasses.replace(
            current, model=str(config.get("model") or ""), effort=str(config.get("effort") or ""),
            route=route, session_target=str(config.get("session_target") or ""),
            session_profile=str(config.get("session_profile") or ""),
        ))
        if route is ReviewRouteKind.AGENT_SESSION:
            thread_id = str(output.get("review_thread_id") or "")
            if not thread_id:
                return slots, {}, {}, f"prior_review_thread_missing:{sid}"
            session_threads[sid] = thread_id
        else:
            prior_messages = output.get("request_messages")
            if not isinstance(prior_messages, list):
                return slots, {}, {}, f"prior_api_transcript_missing:{sid}"
            slot_messages[sid] = [
                *[dict(row) for row in prior_messages if isinstance(row, dict)],
                {"role": "assistant", "content": str(output.get("text") or "")},
                {"role": "user", "content": user_content},
            ]
    return rebound, slot_messages, session_threads, None


def exact_wave(
    wave: dict, *, plan_prose: str, manifest: dict, slots: List[Any], rows: List[dict],
    system_prompt: str, user_content: str, session_task: str,
    slot_messages: Dict[str, List[Dict[str, Any]]],
) -> dict:
    from ouroboros.tools.plan_packet import plan_user_stable_len
    from ouroboros.tools.review_synthesis import build_plan_review_messages

    common = build_plan_review_messages(system_prompt, user_content, plan_user_stable_len(user_content))
    outputs = []
    for row in rows:
        sid, route = str(row.get("slot_id") or ""), str(row.get("route") or "")
        outputs.append({
            "slot_id": sid, "model": str(row.get("model") or ""),
            "request_model": str(row.get("request_model") or ""), "route": route,
            "text": str(row.get("text") or ""), "error": str(row.get("error") or ""),
            "request_messages": list(slot_messages.get(sid) or common) if route == "api_chat" else [],
            "session_task": session_task if route == "agent_session" else "",
            "review_thread_id": str(row.get("review_thread_id") or ""),
            "review_turn_id": str(row.get("review_turn_id") or ""),
            "review_thread_receipt": row.get("review_thread_receipt") or {},
            "auth_route_receipt": row.get("auth_route_receipt") or {},
            "applied_profile": str(row.get("applied_profile") or ""),
            "prompt_ref": row.get("prompt_ref") or {}, "response_ref": row.get("response_ref") or {},
        })
    return {
        **wave, "plan_prose": plan_prose, "evidence_manifest_full": manifest,
        "slots": [slot_row(slot) for slot in slots], "reviewer_outputs": outputs,
    }


def cannot_verify_wave(
    *, cycle_index: int, fingerprint: str, previous: Optional[dict], spec: dict,
    plan_prose: str, manifest: dict, manifest_hash: str, constitutional: bool,
    constitutional_note: str, slots: List[Any], enforcement: str, cap: Optional[int],
    reason: str, reviewer_config_fingerprint: str, reviewed_at: str,
) -> dict:
    from ouroboros.config import adaptive_quorum
    from ouroboros.tools import plan_spec

    return {
        "schema_version": 2, "cycle_index": cycle_index,
        "request_fingerprint": fingerprint,
        "previous_fingerprint": str((previous or {}).get("request_fingerprint") or ""),
        "goal": spec["goal"], "plan_prose_hash": sha256(plan_prose.encode("utf-8")).hexdigest(),
        "spec": spec, "spec_hash": plan_spec.spec_hash(spec), "evidence_manifest": manifest,
        "evidence_manifest_hash": manifest_hash, "constitutional": constitutional,
        "constitutional_note": constitutional_note, "findings": [], "aggregate": "DEGRADED",
        "reasons": [f"cannot_verify:{reason}"],
        "counts": {"configured": len(slots), "parseable": 0, "quorum": adaptive_quorum(len(slots)),
                   "blocking_slots": 0, "blocking": 0, "note": 0, "need_evidence": 0},
        "closed": False, "dispositions": [], "actors": [],
        "actors_degraded": [str(getattr(slot, "slot_id", "")) for slot in slots],
        "enforcement": enforcement, "cycle_cap": cap, "paid": False, "health_epoch": [],
        "reviewer_config_fingerprint": reviewer_config_fingerprint, "reviewed_at": reviewed_at,
    }
