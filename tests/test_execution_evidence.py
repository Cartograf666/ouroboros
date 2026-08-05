"""Completion-seam execution evidence (route labels = DECISIONS, ledger = EVIDENCE).

A live test (a1d69c6c) showed two mutating children whose durable records and UI
chips said `executor_route=claude` — a dispatch-time decision — while the custody
rows showed ZERO delegated runs: 100% of their cognition ran metered, and the card
read the label as a receipt. The fix reconciles the route against the task's own
delegate custody rows exactly once, at ``subagents.envelope_from_task``, into ONE
additive ``execution_evidence`` field. The dispatch decision is never overwritten.
"""
from __future__ import annotations

import json

from ouroboros import delegate_custody as custody
from ouroboros.subagents import envelope_from_task


def _drive(tmp_path):
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _subagent_task(tmp_path, **extra):
    return {
        "id": "child-1",
        "parent_task_id": "root-1",
        "root_task_id": "root-1",
        "delegation_role": "subagent",
        "requested_executor": "harness",
        "effective_executor": "harness",
        "executor_route": "claude",
        "model": "openai/gpt-5.6-terra",
        "budget_drive_root": str(tmp_path),
        **extra,
    }


def _emit_started(drive, run_id="run-1", task_id="child-1", model=""):
    assert custody.emit(drive, custody.STARTED, {
        "run_id": run_id, "task_id": task_id, "route": "claude", "model": model,
        "max_seconds": 300,
    })


def _emit_settled(drive, run_id="run-1", task_id="child-1", *,
                  cost_usd=0.0, spend_disclosed=True, model="claude-sonnet",
                  spend_estimated=False):
    assert custody.emit(drive, custody.SETTLED, {
        "run_id": run_id, "task_id": task_id, "route": "claude", "model": model,
        "state": "succeeded", "cost_usd": cost_usd,
        "cost_final": spend_disclosed and not spend_estimated,
        "spend_disclosed": spend_disclosed, "spend_estimated": spend_estimated,
    })


class TestCustodyAggregation:
    def test_no_rows_is_zero_runs_not_an_error(self, tmp_path):
        drive = _drive(tmp_path)
        evidence = custody.task_execution_evidence(drive, "child-1")
        assert evidence == {
            "delegated_runs_started": 0,
            "delegated_runs_settled": 0,
            "subscription_cost_usd": None,
            "subscription_cost_estimated": False,
            "harness_models": [],
        }

    def test_started_and_settled_runs_aggregate_with_disclosed_spend(self, tmp_path):
        drive = _drive(tmp_path)
        _emit_started(drive, "run-1")
        _emit_settled(drive, "run-1", cost_usd=0.0)
        _emit_started(drive, "run-2")
        evidence = custody.task_execution_evidence(drive, "child-1")
        assert evidence["delegated_runs_started"] == 2
        assert evidence["delegated_runs_settled"] == 1
        assert evidence["subscription_cost_usd"] == 0.0
        assert evidence["harness_models"] == ["claude-sonnet"]

    def test_harness_models_lists_engine_reported_models_only(self, tmp_path):
        # A STARTED row carries the REQUESTED pin — with an owner default model
        # it is routinely non-empty, and listing it would name a model that
        # never executed. Only SETTLED rows are engine-reported.
        drive = _drive(tmp_path)
        _emit_started(drive, "run-1", model="sonnet")
        _emit_settled(drive, "run-1", model="claude-opus-5")
        _emit_started(drive, "run-2", model="sonnet")   # started, never settled
        evidence = custody.task_execution_evidence(drive, "child-1")
        assert evidence["harness_models"] == ["claude-opus-5"]

    def test_estimated_spend_is_flagged_not_dressed_as_exact(self, tmp_path):
        # The settlement row's estimated/final distinction rides into the
        # aggregate: an estimated sum must never render as an exact receipt.
        drive = _drive(tmp_path)
        _emit_started(drive, "run-1")
        _emit_settled(drive, "run-1", cost_usd=0.42, spend_estimated=True)
        evidence = custody.task_execution_evidence(drive, "child-1")
        assert evidence["subscription_cost_usd"] == 0.42
        assert evidence["subscription_cost_estimated"] is True

    def test_undisclosed_spend_never_renders_as_zero(self, tmp_path):
        drive = _drive(tmp_path)
        _emit_started(drive, "run-1")
        _emit_settled(drive, "run-1", cost_usd=None, spend_disclosed=False)
        evidence = custody.task_execution_evidence(drive, "child-1")
        assert evidence["delegated_runs_settled"] == 1
        assert evidence["subscription_cost_usd"] is None

    def test_another_tasks_runs_do_not_leak_in(self, tmp_path):
        drive = _drive(tmp_path)
        _emit_started(drive, "run-9", task_id="other-task")
        _emit_settled(drive, "run-9", task_id="other-task")
        evidence = custody.task_execution_evidence(drive, "child-1")
        assert evidence["delegated_runs_started"] == 0
        assert evidence["delegated_runs_settled"] == 0


class TestEnvelopeReconciliation:
    def test_dispatched_route_with_no_runs_reads_zero_evidence(self, tmp_path):
        # Direction 1 (the live defect): route decided, nothing delegated —
        # the envelope now SAYS so, beside the untouched dispatch decision.
        drive = _drive(tmp_path)
        task = _subagent_task(drive)
        envelope = envelope_from_task(task, status="completed")
        assert envelope["executor_route"] == "claude"          # decision, untouched
        assert envelope["effective_executor"] == "harness"     # decision, untouched
        assert envelope["execution_evidence"] == {
            "delegated_runs_started": 0,
            "delegated_runs_settled": 0,
            "subscription_cost_usd": None,
            "subscription_cost_estimated": False,
            "harness_models": [],
        }

    def test_settled_delegate_rows_read_as_harness_evidence(self, tmp_path):
        # Direction 2: real delegated runs settle -> counts, spend and the
        # engine-reported model land on the envelope.
        drive = _drive(tmp_path)
        _emit_started(drive, "run-1")
        _emit_settled(drive, "run-1", cost_usd=0.0)
        task = _subagent_task(drive)
        envelope = envelope_from_task(task, status="completed")
        evidence = envelope["execution_evidence"]
        assert evidence["delegated_runs_started"] == 1
        assert evidence["delegated_runs_settled"] == 1
        assert evidence["subscription_cost_usd"] == 0.0
        assert evidence["harness_models"] == ["claude-sonnet"]

    def test_running_envelope_carries_no_evidence(self, tmp_path):
        # Pre-completion there is no evidence to state: the chip stays the
        # neutral "dispatched" decision, so the field must be ABSENT, not zeroed.
        drive = _drive(tmp_path)
        task = _subagent_task(drive)
        envelope = envelope_from_task(task, status="running")
        assert "execution_evidence" not in envelope

    def test_native_child_has_no_delegation_claim_to_reconcile(self, tmp_path):
        drive = _drive(tmp_path)
        task = _subagent_task(drive, executor_route="", effective_executor="native")
        envelope = envelope_from_task(task, status="completed")
        assert "execution_evidence" not in envelope

    def test_evidence_read_failure_never_breaks_completion(self, tmp_path, monkeypatch):
        drive = _drive(tmp_path)
        task = _subagent_task(drive)

        def _boom(*a, **k):
            raise OSError("event log unreadable")

        monkeypatch.setattr(custody, "task_execution_evidence", _boom)
        envelope = envelope_from_task(task, status="completed")
        assert "execution_evidence" not in envelope
        assert envelope["executor_route"] == "claude"


def test_terminal_frame_field_rides_the_history_replay_allowlist():
    # The chip's layered truth must survive a reload: the terminal frame carries
    # execution_evidence, and history replay filters progress meta by this list.
    from ouroboros.gateway.history import _PROGRESS_META_FIELDS

    assert "execution_evidence" in _PROGRESS_META_FIELDS
    assert "executor_route" in _PROGRESS_META_FIELDS


def test_run_timing_reads_the_started_row(tmp_path):
    drive = _drive(tmp_path)
    _emit_started(drive, "run-1")
    started_ts, max_seconds = custody.run_timing(drive, "run-1")
    assert started_ts  # the emit stamped ts
    assert max_seconds == 300
    assert custody.run_timing(drive, "run-unknown") == ("", 0)


def test_evidence_is_json_serializable(tmp_path):
    drive = _drive(tmp_path)
    _emit_started(drive, "run-1")
    _emit_settled(drive, "run-1")
    envelope = envelope_from_task(_subagent_task(drive), status="failed")
    json.dumps(envelope)
