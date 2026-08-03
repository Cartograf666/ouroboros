from __future__ import annotations

import json
import queue


def test_no_lane_fans_out_and_depth_does_not_downgrade(monkeypatch):
    """A lane names STRENGTH, and strength is one model, so every lane resolves to exactly
    one slot at every depth.

    Two behaviors died in v6.87.7 and this pins both. The `review`/`scope` lanes fanned out
    across the configured reviewer slots — a TOPOLOGY smuggled in through a strength
    parameter, which no review surface ever used (they read their slots from config and run
    on the review substrate). And the capability-depth cap collapsed a nested child to Light
    regardless of what its parent asked for. Depth bounds how deep delegation NESTS, never
    how strong a descendant is.
    """
    from ouroboros.subagents import (
        SUBAGENT_MODEL_LANES,
        expand_subagent_lane_slots,
        normalize_subagent_model_lane,
    )
    import pytest

    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "light-model")
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "heavy-model")

    for lane in ("review", "scope"):
        assert lane not in SUBAGENT_MODEL_LANES
        with pytest.raises(ValueError, match="model_lane must be one of"):
            normalize_subagent_model_lane(lane)

    for depth in (1, 2, 4):
        for lane in sorted(SUBAGENT_MODEL_LANES):
            slots = expand_subagent_lane_slots(lane, depth=depth)
            assert len(slots) == 1, (lane, depth)
            assert slots[0].slot_count == 1, (lane, depth)
        assert expand_subagent_lane_slots("heavy", depth=depth)[0].model == "heavy-model", depth
        assert expand_subagent_lane_slots("auto", depth=depth)[0].effective_lane == "light", depth


def test_schedule_subagent_emits_lane_metadata_without_a_task_group(monkeypatch, tmp_path):
    """One request schedules one child. The task-group plumbing stays in the scheduler for a
    future multi-slot source, but nothing populates it today, so no group id is minted."""
    from ouroboros.task_results import STATUS_REQUESTED
    from ouroboros.tools.control import _schedule_task
    from ouroboros.tools.registry import ToolContext

    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "heavy-model")
    event_queue: queue.Queue = queue.Queue()
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "parent1"
    ctx.task_depth = 0
    ctx.current_chat_id = 1
    ctx.event_queue = event_queue
    ctx.task_metadata = {"root_task_id": "root1", "session_id": "sess1"}

    result = _schedule_task(
        ctx,
        objective="Review the design",
        expected_output="One findings list",
        role="reviewer",
        model_lane="heavy",
    )

    assert "TOOL_ARG_ERROR" not in result
    event = event_queue.get_nowait()
    assert event_queue.empty()
    assert event["requested_model_lane"] == "heavy"
    assert event["effective_model_lane"] == "heavy"
    assert event["model"] == "heavy-model"
    assert event["task_group_id"] == ""
    assert event["subagent_envelope"]["status"] == STATUS_REQUESTED
    assert event["subagent_envelope"]["lineage"]["root_task_id"] == "root1"

    path = tmp_path / "task_results" / f"{event['task_id']}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["model"] == "heavy-model"
    assert data["effective_model_lane"] == "heavy"
    assert data["task_group_id"] == ""


def test_schedule_subagent_drive_failure_is_fail_closed(monkeypatch, tmp_path):
    """A drive that cannot be prepared leaves NOTHING behind: no event, no durable record,
    no half-provisioned state directory."""
    import ouroboros.tools.control as control
    from ouroboros.headless import HEADLESS_TASKS_DIR
    from ouroboros.tools.registry import ToolContext

    def fake_prepare(_root, _tid, _mode):
        raise RuntimeError("boom")

    monkeypatch.setattr(control, "prepare_task_drive", fake_prepare)
    event_queue: queue.Queue = queue.Queue()
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "parent1"
    ctx.task_depth = 0
    ctx.current_chat_id = 1
    ctx.event_queue = event_queue
    ctx.task_metadata = {"root_task_id": "root1", "session_id": "sess1"}

    result = control._schedule_task(
        ctx,
        objective="Review the design",
        expected_output="One findings list",
        role="reviewer",
    )

    assert "SUBTASK_DRIVE_ERROR" in result
    assert event_queue.empty()
    assert not any((tmp_path / "task_results").glob("*.json"))
    assert not any((tmp_path / HEADLESS_TASKS_DIR).glob("*"))
