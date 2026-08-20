"""The owner-facing progress ticker: what a quiet task reports while it works.

The gap this covers: every existing progress line is emitted at a DECISION point,
so the span between two decisions was silent in chat. On a slow route that span is
minutes. These tests pin the three properties that make the ticker useful rather
than noisy — it reports FACTS the loop stamped, it stays silent when the task is
already talking, and it says nothing at all when nothing was stamped.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest

from ouroboros import task_activity


@pytest.fixture(autouse=True)
def _clean_registry():
    task_activity.clear("task-1")
    task_activity.clear("task-2")
    yield
    task_activity.clear("task-1")
    task_activity.clear("task-2")


def test_unstamped_task_renders_nothing_rather_than_a_reassuring_guess():
    assert task_activity.render("task-1") == ""


def test_model_phase_names_the_route_and_the_round():
    task_activity.mark(
        "task-1", task_activity.PHASE_MODEL,
        round_idx=4, max_rounds=200, model="local-model (local)",
    )
    line = task_activity.render("task-1")
    assert "waiting on local-model (local)" in line
    assert "round 4/200" in line


def test_tool_phase_names_the_tools_and_inherits_the_round():
    task_activity.mark("task-1", task_activity.PHASE_MODEL, round_idx=7, max_rounds=200, model="m")
    task_activity.mark("task-1", task_activity.PHASE_TOOL, detail="read_file, search_code")
    line = task_activity.render("task-1")
    assert "running read_file, search_code" in line
    # The tool stamp omitted the round facts; blanking them would make the line
    # LESS informative than the one before it.
    assert "round 7/200" in line


def test_restamping_the_same_phase_keeps_the_original_start_time():
    task_activity.mark("task-1", task_activity.PHASE_MODEL, model="m")
    snapshot = task_activity.snapshot("task-1")
    assert snapshot is not None
    snapshot.started_ts -= 120.0
    task_activity.mark("task-1", task_activity.PHASE_MODEL, model="m")
    # A wait that has run two minutes must not report itself as fresh.
    assert task_activity.snapshot("task-1").elapsed_sec() >= 100.0
    assert "2m" in task_activity.render("task-1")


def test_a_changed_phase_resets_the_clock():
    task_activity.mark("task-1", task_activity.PHASE_MODEL, model="m")
    task_activity.snapshot("task-1").started_ts -= 120.0
    task_activity.mark("task-1", task_activity.PHASE_TOOL, detail="write_file")
    assert task_activity.snapshot("task-1").elapsed_sec() < 5.0


def test_tasks_do_not_overwrite_each_other():
    task_activity.mark("task-1", task_activity.PHASE_MODEL, model="parent-model")
    task_activity.mark("task-2", task_activity.PHASE_TOOL, detail="run_command")
    assert "parent-model" in task_activity.render("task-1")
    assert "run_command" in task_activity.render("task-2")


def test_clear_drops_the_task():
    task_activity.mark("task-1", task_activity.PHASE_MODEL, model="m")
    task_activity.clear("task-1")
    assert task_activity.render("task-1") == ""


def test_blank_task_id_is_ignored():
    task_activity.mark("", task_activity.PHASE_MODEL, model="m")
    assert task_activity.snapshot("") is None


def test_extra_clause_is_appended():
    task_activity.mark("task-1", task_activity.PHASE_MODEL, model="m")
    line = task_activity.render("task-1", extra="2 of 5 subagents still running")
    assert line.endswith("2 of 5 subagents still running")


@pytest.mark.parametrize("seconds,expected", [(0, "0s"), (9, "9s"), (59, "59s"), (60, "1m 00s"), (130, "2m 10s"), (3700, "1h 01m")])
def test_duration_formatting(seconds, expected):
    assert task_activity.format_duration(seconds) == expected


# --- the agent-side gate -------------------------------------------------------


class _StubAgent:
    """Only the state ``_maybe_emit_progress_tick`` actually touches."""

    def __init__(self, task_id: str, last_progress_ts: float):
        from ouroboros.agent import OuroborosAgent

        self._maybe_emit_progress_tick = OuroborosAgent._maybe_emit_progress_tick.__get__(self)
        self._live_children_clause = lambda _task_id: ""
        self._last_progress_ts = last_progress_ts
        self.emitted: List[str] = []

    def _emit_progress(self, text: str) -> None:
        self.emitted.append(text)
        self._last_progress_ts = time.time()


def test_ticker_stays_silent_while_the_task_is_already_talking():
    task_activity.mark("task-1", task_activity.PHASE_MODEL, model="m")
    agent = _StubAgent("task-1", last_progress_ts=time.time())
    agent._maybe_emit_progress_tick("task-1")
    assert agent.emitted == []


def test_ticker_speaks_after_the_silence_window():
    task_activity.mark("task-1", task_activity.PHASE_MODEL, round_idx=2, max_rounds=200, model="m")
    agent = _StubAgent("task-1", last_progress_ts=time.time() - 600.0)
    agent._maybe_emit_progress_tick("task-1")
    assert len(agent.emitted) == 1
    assert "waiting on m" in agent.emitted[0]


def test_a_silent_but_unstamped_task_still_emits_nothing():
    agent = _StubAgent("task-1", last_progress_ts=time.time() - 600.0)
    agent._maybe_emit_progress_tick("task-1")
    assert agent.emitted == []


def test_one_tick_per_window_not_one_per_heartbeat():
    task_activity.mark("task-1", task_activity.PHASE_MODEL, model="m")
    agent = _StubAgent("task-1", last_progress_ts=time.time() - 600.0)
    agent._maybe_emit_progress_tick("task-1")
    agent._maybe_emit_progress_tick("task-1")
    assert len(agent.emitted) == 1


def test_ticker_disabled_by_setting(monkeypatch):
    monkeypatch.setenv("OUROBOROS_PROGRESS_TICKER_SEC", "0")
    task_activity.mark("task-1", task_activity.PHASE_MODEL, model="m")
    agent = _StubAgent("task-1", last_progress_ts=time.time() - 600.0)
    agent._maybe_emit_progress_tick("task-1")
    assert agent.emitted == []


def test_tool_batch_stamp_names_up_to_three_and_counts_the_rest():
    from ouroboros.loop_tool_execution import _stamp_tool_activity

    calls: List[Dict[str, Any]] = [
        {"function": {"name": name}} for name in
        ["read_file", "search_code", "list_files", "query_code", "vcs_diff"]
    ]
    _stamp_tool_activity("task-1", calls)
    line = task_activity.render("task-1")
    assert "read_file, search_code, list_files" in line
    assert "+2 more" in line


def test_tool_batch_stamp_deduplicates_repeated_tools():
    from ouroboros.loop_tool_execution import _stamp_tool_activity

    _stamp_tool_activity("task-1", [{"function": {"name": "read_file"}}] * 4)
    assert "read_file" in task_activity.render("task-1")
    assert "+" not in task_activity.render("task-1")


def test_tool_batch_stamp_ignores_a_nameless_batch():
    from ouroboros.loop_tool_execution import _stamp_tool_activity

    _stamp_tool_activity("task-1", [{"function": {}}])
    assert task_activity.render("task-1") == ""


# --- the swarm-visible phases --------------------------------------------------


def test_review_phase_names_the_pass():
    task_activity.mark(
        "task-1", task_activity.PHASE_REVIEW, detail="acceptance panel, pass 1",
    )
    assert "review: acceptance panel, pass 1" in task_activity.render("task-1")


def test_children_phase_names_the_wait_set():
    task_activity.mark(
        "task-1", task_activity.PHASE_CHILDREN, detail="waiting on 3 subagent(s) (all terminal)",
    )
    assert "waiting on 3 subagent(s) (all terminal)" in task_activity.render("task-1")


def test_wait_tasks_stamps_the_wait_set_before_it_blocks(tmp_path, monkeypatch):
    """The swarm parent's longest silence is inside wait_tasks, so the stamp has
    to land BEFORE the blocking call, not after it returns."""
    from ouroboros.tools import control

    stamped: List[str] = []

    def _fake_wait(*_args, **_kwargs):
        # Read the ticker from inside the block: a stamp written afterwards would
        # be useless to an owner watching a 7200s wait.
        stamped.append(task_activity.render("task-1"))
        return {"tasks": {}, "elapsed_sec": 0.0, "early_return": None}

    monkeypatch.setattr(control, "wait_for_effective_tasks", _fake_wait)
    monkeypatch.setattr(control, "_unminted_wait_ids", lambda *_a, **_k: [])
    monkeypatch.setattr(control, "_wait_attention_poll", lambda *_a, **_k: (lambda *a, **k: None))

    ctx = type("Ctx", (), {})()
    ctx.task_id = "task-1"
    ctx.task_metadata = {}
    ctx.drive_root = tmp_path

    control._wait_for_tasks(ctx, ["11111111", "22222222"], timeout_sec=5)

    assert stamped, "wait_for_effective_tasks was never reached"
    assert "waiting on 2 subagent(s) (all terminal)" in stamped[0]
