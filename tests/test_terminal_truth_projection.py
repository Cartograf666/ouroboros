"""Canonical/replica custody regressions for terminal task-result truth."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ouroboros.post_task_checkpoint import project_replica_task_result_fields
from ouroboros.task_results import STATUS_COMPLETED, load_task_result, write_task_result
from ouroboros.task_status import load_effective_task_result


def _seed_split_result(
    tmp_path,
    *,
    canonical_post_task="completed",
    child_post_task="running",
    canonical_cost_final=True,
):
    data = tmp_path / "data"
    child = tmp_path / "child"
    data.mkdir()
    child.mkdir()
    task_id = "terminal-root"
    write_task_result(
        child,
        task_id,
        STATUS_COMPLETED,
        result="child answer",
        review_status={"status": "fail", "source": "acceptance"},
        trace_summary="child trace",
        root_phase_checkpoint={
            "phase": "task_acceptance",
            "status": "degraded",
            "pass_index": 3,
            "post_task_synthesis": child_post_task,
            "post_task_stop_reason": "stale_child_reason",
        },
        cost_usd=7.0,
        accounted_upper_bound_usd=7.0,
        cost_usd_with_children=8.0,
        accounted_upper_bound_usd_with_children=8.0,
        cost_final=not canonical_cost_final,
        cost_with_children_partial=canonical_cost_final,
        non_final_rows=4,
        total_rounds=17,
        prompt_tokens=700,
        completion_tokens=70,
        ts="2026-08-19T00:00:02+00:00",
    )
    write_task_result(
        data,
        task_id,
        STATUS_COMPLETED,
        result="canonical placeholder",
        child_drive_root=str(child),
        root_phase_checkpoint={
            "phase": "task_acceptance",
            "status": "not_required",
            "pass_index": 0,
            "post_task_synthesis": canonical_post_task,
            "post_task_stop_reason": f"canonical_{canonical_post_task}_reason",
        },
        cost_usd=91.0,
        accounted_upper_bound_usd=91.0,
        cost_usd_with_children=99.0,
        accounted_upper_bound_usd_with_children=99.0,
        cost_final=canonical_cost_final,
        cost_with_children_partial=not canonical_cost_final,
        non_final_rows=0 if canonical_cost_final else 2,
        total_rounds=41,
        prompt_tokens=4100,
        completion_tokens=410,
        ts="2026-08-19T00:00:01+00:00",
    )
    return data, child, task_id


def _assert_terminal_projection(
    result,
    *,
    canonical_post_task,
    canonical_cost_final=True,
):
    checkpoint = result["root_phase_checkpoint"]
    assert checkpoint == {
        "phase": "task_acceptance",
        "status": "degraded",
        "pass_index": 3,
        "post_task_synthesis": canonical_post_task,
        "post_task_stop_reason": f"canonical_{canonical_post_task}_reason",
    }
    assert result["result"] == "child answer"
    assert result["review_status"] == {"status": "fail", "source": "acceptance"}
    assert result["trace_summary"] == "child trace"
    assert result["cost_usd"] == 91.0
    assert result["accounted_upper_bound_usd"] == 91.0
    assert result["cost_usd_with_children"] == 99.0
    assert result["accounted_upper_bound_usd_with_children"] == 99.0
    assert result["cost_final"] is canonical_cost_final
    assert result["cost_with_children_partial"] is (not canonical_cost_final)
    assert result["non_final_rows"] == (0 if canonical_cost_final else 2)
    assert result["total_rounds"] == 41
    assert result["prompt_tokens"] == 4100
    assert result["completion_tokens"] == 410


def test_replica_field_projector_is_pure_and_updated_at_is_metadata_only():
    canonical = {
        "updated_at": "2026-08-19T00:00:03+00:00",
        "ts": "canonical-ts",
    }
    replica = {
        "updated_at": "2026-08-19T00:00:02+00:00",
        "ts": "replica-ts",
        "result": "replica answer",
    }
    canonical_before = deepcopy(canonical)
    replica_before = deepcopy(replica)

    projected = project_replica_task_result_fields(canonical, replica)

    assert projected["updated_at"] == canonical["updated_at"]
    assert projected["ts"] == "replica-ts"
    assert projected["result"] == "replica answer"
    assert canonical == canonical_before
    assert replica == replica_before

    projected = project_replica_task_result_fields(
        {"updated_at": "2026-08-19T00:00:01+00:00"},
        {"updated_at": "2026-08-19T00:00:04+00:00"},
    )
    assert projected["updated_at"] == "2026-08-19T00:00:04+00:00"


@pytest.mark.parametrize("canonical_post_task", ["completed", "degraded"])
@pytest.mark.parametrize("child_post_task", ["pending_once", "running"])
@pytest.mark.parametrize("materialize_artifacts", [False, True])
def test_effective_terminal_truth_wins_open_replica_cartesian(
    tmp_path,
    canonical_post_task,
    child_post_task,
    materialize_artifacts,
):
    data, _child, task_id = _seed_split_result(
        tmp_path,
        canonical_post_task=canonical_post_task,
        child_post_task=child_post_task,
    )
    canonical = load_task_result(data, task_id)

    effective = load_effective_task_result(
        data,
        task_id,
        materialize_artifacts=materialize_artifacts,
    )

    _assert_terminal_projection(
        effective,
        canonical_post_task=canonical_post_task,
    )
    assert effective["updated_at"] == canonical["updated_at"]
    # Creation/sort metadata keeps the pre-existing child-overlay behavior.
    assert effective["ts"] == "2026-08-19T00:00:02+00:00"


@pytest.mark.parametrize(
    ("child_has_checkpoint", "child_checkpoint"),
    [(False, None), (True, "legacy-non-dict-checkpoint")],
)
def test_terminal_canonical_acceptance_survives_missing_child_checkpoint(
    tmp_path,
    child_has_checkpoint,
    child_checkpoint,
):
    data = tmp_path / "data"
    child = tmp_path / "child"
    data.mkdir()
    child.mkdir()
    task_id = "checkpoint-fallback"
    child_fields = {"result": "child answer"}
    if child_has_checkpoint:
        child_fields["root_phase_checkpoint"] = child_checkpoint
    write_task_result(child, task_id, STATUS_COMPLETED, **child_fields)
    canonical_checkpoint = {
        "phase": "task_acceptance",
        "status": "not_required",
        "pass_index": 0,
        "post_task_synthesis": "completed",
        "post_task_stop_reason": "canonical_reason",
    }
    write_task_result(
        data,
        task_id,
        STATUS_COMPLETED,
        child_drive_root=str(child),
        root_phase_checkpoint=canonical_checkpoint,
    )

    effective = load_effective_task_result(data, task_id, materialize_artifacts=False)

    assert effective["result"] == "child answer"
    assert effective["root_phase_checkpoint"] == canonical_checkpoint


def test_absent_canonical_stop_reason_preserves_child_reason(tmp_path):
    data = tmp_path / "data"
    child = tmp_path / "child"
    data.mkdir()
    child.mkdir()
    task_id = "child-stop-reason"
    write_task_result(
        child,
        task_id,
        STATUS_COMPLETED,
        root_phase_checkpoint={
            "phase": "task_acceptance",
            "status": "pass",
            "pass_index": 1,
            "post_task_synthesis": "running",
            "post_task_stop_reason": "child_reason",
        },
    )
    write_task_result(
        data,
        task_id,
        STATUS_COMPLETED,
        child_drive_root=str(child),
        root_phase_checkpoint={
            "phase": "task_acceptance",
            "status": "not_required",
            "pass_index": 0,
            "post_task_synthesis": "completed",
        },
    )

    effective = load_effective_task_result(data, task_id, materialize_artifacts=False)

    checkpoint = effective["root_phase_checkpoint"]
    assert checkpoint["phase"] == "task_acceptance"
    assert checkpoint["status"] == "pass"
    assert checkpoint["pass_index"] == 1
    assert checkpoint["post_task_synthesis"] == "completed"
    assert checkpoint["post_task_stop_reason"] == "child_reason"


@pytest.mark.parametrize("materialize_artifacts", [False, True])
def test_effective_terminal_partial_accounting_stays_canonical(
    tmp_path,
    materialize_artifacts,
):
    data, _child, task_id = _seed_split_result(
        tmp_path,
        canonical_cost_final=False,
    )

    effective = load_effective_task_result(
        data,
        task_id,
        materialize_artifacts=materialize_artifacts,
    )

    _assert_terminal_projection(
        effective,
        canonical_post_task="completed",
        canonical_cost_final=False,
    )


@pytest.mark.parametrize(
    "canonical_checkpoint",
    [None, {"post_task_synthesis": "pending_once", "status": "not_required"}],
)
def test_open_and_legacy_canonical_results_keep_provisional_child_overlay(
    tmp_path,
    canonical_checkpoint,
):
    data = tmp_path / "data"
    child = tmp_path / "child"
    data.mkdir()
    child.mkdir()
    task_id = "open-root"
    write_task_result(
        child,
        task_id,
        STATUS_COMPLETED,
        result="provisional child answer",
        root_phase_checkpoint={
            "phase": "task_acceptance",
            "status": "pass",
            "pass_index": 1,
            "post_task_synthesis": "running",
        },
        cost_usd=12.5,
        cost_final=False,
        total_rounds=12,
        ts="2026-08-19T00:00:02+00:00",
    )
    canonical_fields = {
        "result": "canonical placeholder",
        "child_drive_root": str(child),
        "cost_usd": 1.0,
        "cost_final": True,
        "total_rounds": 1,
        "ts": "2026-08-19T00:00:01+00:00",
    }
    if canonical_checkpoint is not None:
        canonical_fields["root_phase_checkpoint"] = canonical_checkpoint
    write_task_result(data, task_id, STATUS_COMPLETED, **canonical_fields)

    effective = load_effective_task_result(data, task_id, materialize_artifacts=False)

    assert effective["result"] == "provisional child answer"
    assert effective["root_phase_checkpoint"]["post_task_synthesis"] == "running"
    assert effective["root_phase_checkpoint"]["status"] == "pass"
    assert effective["cost_usd"] == 12.5
    assert effective["cost_final"] is False
    assert effective["total_rounds"] == 12
    assert effective["ts"] == "2026-08-19T00:00:02+00:00"


def test_terminal_truth_is_stable_before_and_after_physical_copyback(
    tmp_path,
    monkeypatch,
):
    from ouroboros.headless import copy_child_task_result
    import ouroboros.task_results as task_results

    data, child, task_id = _seed_split_result(
        tmp_path,
        canonical_cost_final=False,
    )

    before = load_effective_task_result(data, task_id, materialize_artifacts=False)
    _assert_terminal_projection(
        before,
        canonical_post_task="completed",
        canonical_cost_final=False,
    )

    copyback_updated_at = "2099-08-19T00:01:00+00:00"
    monkeypatch.setattr(task_results, "utc_now_iso", lambda: copyback_updated_at)
    copied = copy_child_task_result(data, {"id": task_id, "drive_root": str(child)})

    assert copied is not None
    assert (child / "task_results" / f"{task_id}.json").is_file()
    retained_child = load_task_result(child, task_id)
    assert retained_child["root_phase_checkpoint"]["post_task_synthesis"] == "running"
    _assert_terminal_projection(
        copied,
        canonical_post_task="completed",
        canonical_cost_final=False,
    )
    assert copied["ts"] == "2026-08-19T00:00:02+00:00"
    assert copied["updated_at"] == copyback_updated_at
    after = load_effective_task_result(data, task_id, materialize_artifacts=False)
    _assert_terminal_projection(
        after,
        canonical_post_task="completed",
        canonical_cost_final=False,
    )
    assert after["root_phase_checkpoint"] == before["root_phase_checkpoint"]
    assert after["updated_at"] == copyback_updated_at


def _write_history_rows(data, task_id):
    logs = data / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "progress.jsonl").write_text(
        json.dumps({
            "ts": "2026-08-19T00:00:03+00:00",
            "type": "send_message",
            "task_id": task_id,
            "is_progress": True,
            "direction": "out",
            "chat_id": 1,
            "user_id": 1,
            "text": "delivered answer",
            "content": "delivered answer",
        }) + "\n",
        encoding="utf-8",
    )
    (logs / "chat.jsonl").write_text("", encoding="utf-8")


def test_history_and_task_detail_project_terminal_canonical_accounting(tmp_path):
    from ouroboros.gateway.history import make_chat_history_endpoint
    from ouroboros.gateway.tasks import api_task_get

    data, _child, task_id = _seed_split_result(tmp_path)
    _write_history_rows(data, task_id)

    detail_request = SimpleNamespace(
        path_params={"task_id": task_id},
        app=SimpleNamespace(state=SimpleNamespace(drive_root=data)),
    )
    detail = json.loads(asyncio.run(api_task_get(detail_request)).body.decode("utf-8"))

    # ProgramBench polls this exact task-detail surface before deciding whether
    # cost is still partial; the stale child must not restart its bounded wait.
    assert detail["status"] == STATUS_COMPLETED
    assert detail["root_phase_checkpoint"]["post_task_synthesis"] == "completed"
    assert detail["root_phase_checkpoint"]["post_task_stop_reason"] == (
        "canonical_completed_reason"
    )
    assert detail["cost_usd_with_children"] == 99.0
    assert detail["accounted_upper_bound_usd_with_children"] == 99.0
    assert detail["cost_final"] is True
    assert detail["cost_with_children_partial"] is False

    endpoint = make_chat_history_endpoint(data)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"limit": "20"})))
    messages = json.loads(response.body.decode("utf-8"))["messages"]
    progress = next(
        row
        for row in messages
        if row.get("is_progress") and row.get("task_id") == task_id
    )
    assert progress["task_terminal_status"] == STATUS_COMPLETED
    assert progress.get("task_phase") != "finalizing"
    assert progress["cost_usd_with_children"] == 99.0
    assert progress["accounted_upper_bound_usd_with_children"] == 99.0
    assert progress["cost_final"] is True
    assert progress["cost_with_children_partial"] is False
