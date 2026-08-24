"""The owner-facing progress ticker: what a quiet task reports while it works.

The gap this covers: every existing progress line is emitted at a DECISION point,
so the span between two decisions was silent in chat. On a slow route that span is
minutes. These tests pin the three properties that make the ticker useful rather
than noisy — it reports FACTS the loop stamped, it stays silent when the task is
already talking, and it says nothing at all when nothing was stamped.
"""

from __future__ import annotations

import time
from typing import List

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


# --- the tick gate --------------------------------------------------------------
#
# No stub Agent any more: the tick moved to `task_activity` precisely because
# nothing it does needs one. It takes how to emit and when the task last spoke,
# and the child-count clause is stubbed out so these stay hermetic.


class _Recorder:
    """The two things the tick actually needs from its caller."""

    def __init__(self, last_progress_ts: float):
        self.last_progress_ts = last_progress_ts
        self.emitted: List[str] = []

    def emit(self, text: str) -> None:
        self.emitted.append(text)
        self.last_progress_ts = time.time()

    def tick(self, task_id: str) -> None:
        task_activity.emit_tick(
            task_id, emit=self.emit, quiet_since=self.last_progress_ts,
            metadata={}, drive_root="/nonexistent")


@pytest.fixture(autouse=True)
def _no_children(monkeypatch):
    monkeypatch.setattr(task_activity, "live_children_clause", lambda *a, **k: "")


def test_ticker_stays_silent_while_the_task_is_already_talking():
    task_activity.mark("task-1", task_activity.PHASE_MODEL, model="m")
    agent = _Recorder(last_progress_ts=time.time())
    agent.tick("task-1")
    assert agent.emitted == []


def test_ticker_speaks_after_the_silence_window():
    task_activity.mark("task-1", task_activity.PHASE_MODEL, round_idx=2, max_rounds=200, model="m")
    agent = _Recorder(last_progress_ts=time.time() - 600.0)
    agent.tick("task-1")
    assert len(agent.emitted) == 1
    assert "waiting on m" in agent.emitted[0]


def test_a_silent_but_unstamped_task_still_emits_nothing():
    agent = _Recorder(last_progress_ts=time.time() - 600.0)
    agent.tick("task-1")
    assert agent.emitted == []


def test_one_tick_per_window_not_one_per_heartbeat():
    task_activity.mark("task-1", task_activity.PHASE_MODEL, model="m")
    agent = _Recorder(last_progress_ts=time.time() - 600.0)
    agent.tick("task-1")
    agent.tick("task-1")
    assert len(agent.emitted) == 1


def test_ticker_disabled_by_setting(monkeypatch):
    monkeypatch.setenv("OUROBOROS_PROGRESS_TICKER_SEC", "0")
    task_activity.mark("task-1", task_activity.PHASE_MODEL, model="m")
    agent = _Recorder(last_progress_ts=time.time() - 600.0)
    agent.tick("task-1")
    assert agent.emitted == []
