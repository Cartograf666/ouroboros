"""S3 graceful owner stop («Подвести итог») test matrix — Q1/Q2/Q3=A/Q4=A/Q6=A.

Covers the policy axis end to end against production seams: the typed
``stop_policy`` vocabulary and its monotonic hardening in
``ouroboros/cancel_intents.py``; the graceful HTTP ingress (immediate 202
pending acknowledgement, no synchronous teardown); the policy-aware episode
predicates and orchestration in ``supervisor/owner_stop.py`` (deterministic
``ownerstop:<request_id>`` control identity, idempotent arming, custody feed on
settle/expiry/pending); the Q4=A summary suppression; the Q6=A bounded child
projection; and the reload-visible ``stop_policy`` projection in
``cancel_state_fields``. Absence of the policy stays byte-identical immediate
hard cancellation (§13.1) — proven by the explicit-immediate legacy envelope.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import ouroboros.cancel_intents as ci
import supervisor.owner_stop as ostop
from ouroboros.gateway.tasks import api_task_cancel
from ouroboros.outcomes import REASON_OWNER_REQUESTED_FINALIZATION
from ouroboros.owner_mailbox import _mailbox_path
from ouroboros.task_results import load_task_result, write_task_result
from ouroboros.utils import utc_now_iso


def _isolate_queue(monkeypatch, tmp_path, *, pending=(), running=None):
    from supervisor import queue as q
    from supervisor import workers

    monkeypatch.setattr(q, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(q, "PENDING", [dict(t) for t in pending])
    monkeypatch.setattr(q, "RUNNING", dict(running or {}))
    monkeypatch.setattr(workers, "WORKERS", {}, raising=False)
    monkeypatch.setattr(q, "persist_queue_snapshot", lambda reason="": None)
    return q


def _client(tmp_path):
    app = Starlette(routes=[
        Route("/api/tasks/{task_id}/cancel", api_task_cancel, methods=["POST"]),
    ])
    app.state.drive_root = tmp_path
    return TestClient(app)


def _finalize_rows(drive_root, task_id):
    path = _mailbox_path(pathlib.Path(drive_root), task_id)
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("kind") == "finalize_now"
    ]


# ---------------------------------------------------------------------------
# Vocabulary, monotonic hardening, reload projection
# ---------------------------------------------------------------------------


def test_stop_policy_reads_immediate_for_absent_or_unknown():
    assert ci.stop_policy(None) == ci.STOP_POLICY_IMMEDIATE
    assert ci.stop_policy({}) == ci.STOP_POLICY_IMMEDIATE
    assert ci.stop_policy({"stop_policy": "nonsense"}) == ci.STOP_POLICY_IMMEDIATE
    assert ci.stop_policy({"stop_policy": "finalize_then_cancel"}) == ci.STOP_POLICY_FINALIZE


def test_immediate_hardens_pending_graceful_and_never_softens(tmp_path):
    graceful = ci.request_cancel(
        tmp_path, "t-h", requested_stop_policy=ci.STOP_POLICY_FINALIZE,
    )
    rid = graceful["request_id"]
    assert ci.stop_policy(graceful) == ci.STOP_POLICY_FINALIZE
    # Stop-now during the wait: the SAME durable request hardens in place.
    hardened = ci.request_cancel(
        tmp_path, "t-h", requested_stop_policy=ci.STOP_POLICY_IMMEDIATE,
    )
    assert hardened["request_id"] == rid                # single kill-owner
    assert ci.stop_policy(hardened) == ci.STOP_POLICY_IMMEDIATE
    assert hardened["hardened_at"]
    # Graceful over an accepted immediate is the forbidden softening direction.
    softened = ci.request_cancel(
        tmp_path, "t-h", requested_stop_policy=ci.STOP_POLICY_FINALIZE,
    )
    assert softened["request_id"] == rid
    assert ci.stop_policy(ci.active_intent(tmp_path, "t-h")) == ci.STOP_POLICY_IMMEDIATE
    # The hardening left a typed forensic row.
    ledger = (tmp_path / "logs" / "supervisor.jsonl").read_text(encoding="utf-8")
    assert "stop_policy_hardened" in ledger


def test_cancel_state_fields_projects_the_graceful_policy_for_reload(tmp_path):
    ci.request_cancel(tmp_path, "t-proj", requested_stop_policy=ci.STOP_POLICY_FINALIZE)
    fields = ci.cancel_state_fields(tmp_path, "t-proj")
    assert fields["cancel_state"] == "pending"
    assert fields["stop_policy"] == ci.STOP_POLICY_FINALIZE
    # Immediate intents keep the pre-S3 shape: NO stop_policy key at all.
    ci.request_cancel(tmp_path, "t-imm")
    assert "stop_policy" not in ci.cancel_state_fields(tmp_path, "t-imm")


# ---------------------------------------------------------------------------
# HTTP ingress: 202 pending acknowledgement vs synchronous legacy teardown
# ---------------------------------------------------------------------------


def test_graceful_post_returns_202_pending_without_synchronous_teardown(tmp_path, monkeypatch):
    q = _isolate_queue(
        monkeypatch, tmp_path,
        pending=[{"id": "root-g", "chat_id": 0, "root_task_id": "root-g"}],
    )
    kicks = []
    monkeypatch.setattr(ostop, "begin_graceful_stop", lambda tid: kicks.append(tid))
    with _client(tmp_path) as client:
        resp = client.post(
            "/api/tasks/root-g/cancel", json={"stop_policy": "finalize_then_cancel"},
        )
    assert resp.status_code == 202
    assert resp.json() == {
        "ok": True, "task_id": "root-g",
        "cancel_state": "pending", "stop_policy": "finalize_then_cancel",
    }
    # The durable intent IS the whole owner will; nothing was torn down yet.
    intent = ci.active_intent(tmp_path, "root-g")
    assert ci.stop_policy(intent) == ci.STOP_POLICY_FINALIZE
    assert [t["id"] for t in q.PENDING] == ["root-g"]
    assert (load_task_result(tmp_path, "root-g") or {}).get("status") != "cancelled"
    # One orchestration pass was kicked off the HTTP thread.
    deadline = time.time() + 5
    while not kicks and time.time() < deadline:
        time.sleep(0.01)
    assert kicks == ["root-g"]


def test_bad_policy_is_400_and_explicit_immediate_keeps_the_legacy_contract(tmp_path, monkeypatch):
    _isolate_queue(
        monkeypatch, tmp_path,
        pending=[{"id": "root-i", "chat_id": 0, "root_task_id": "root-i"}],
    )
    with _client(tmp_path) as client:
        bad = client.post("/api/tasks/root-i/cancel", json={"stop_policy": "graceful"})
        typed = client.post("/api/tasks/root-i/cancel", json={"stop_policy": "immediate"})
    assert bad.status_code == 400
    # Explicit immediate stays the synchronous legacy teardown + envelope.
    assert typed.status_code == 200
    assert typed.json() == {"ok": True, "task_id": "root-i"}
    assert load_task_result(tmp_path, "root-i")["status"] == "cancelled"
    intent = ci.active_intent(tmp_path, "root-i") or {}
    assert ci.stop_policy(intent) == ci.STOP_POLICY_IMMEDIATE


def test_graceful_escalates_to_stop_now_through_the_same_intent(tmp_path, monkeypatch):
    """The owner presses «Остановить немедленно» during the graceful wait: the
    same durable request hardens and the synchronous teardown runs."""
    q = _isolate_queue(
        monkeypatch, tmp_path,
        pending=[{"id": "root-e", "chat_id": 0, "root_task_id": "root-e"}],
    )
    monkeypatch.setattr(ostop, "begin_graceful_stop", lambda tid: None)
    with _client(tmp_path) as client:
        graceful = client.post(
            "/api/tasks/root-e/cancel", json={"stop_policy": "finalize_then_cancel"},
        )
        rid = ci.active_intent(tmp_path, "root-e")["request_id"]
        hard = client.post("/api/tasks/root-e/cancel", json={})
    assert graceful.status_code == 202
    assert hard.status_code == 200
    assert q.PENDING == []
    assert load_task_result(tmp_path, "root-e")["status"] == "cancelled"
    # Same request id end to end: one stop episode, monotonically hardened.
    ledger = (tmp_path / "logs" / "supervisor.jsonl").read_text(encoding="utf-8")
    hardened_rows = [
        json.loads(line) for line in ledger.splitlines()
        if line.strip() and "stop_policy_hardened" in line
    ]
    assert [row["request_id"] for row in hardened_rows] == [rid]


# ---------------------------------------------------------------------------
# Episode predicates and sweep orchestration
# ---------------------------------------------------------------------------


def _graceful_intent(tmp_path, task_id, **kw):
    return ci.request_cancel(
        tmp_path, task_id, requested_stop_policy=ci.STOP_POLICY_FINALIZE, **kw,
    )


def test_owner_stop_active_matrix(tmp_path):
    intent = _graceful_intent(tmp_path, "t-act")
    now = time.time()
    assert ostop.owner_stop_active(intent, now=now, grace_sec=120.0) is True
    # A custody claim means the kill already started.
    assert ostop.owner_stop_active(
        {**intent, "state": ci.INTENT_CLAIMED}, now=now, grace_sec=120.0,
    ) is False
    # The immutable deadline passed: the episode no longer owns the intent.
    assert ostop.owner_stop_active(intent, now=now + 121.0, grace_sec=120.0) is False
    # Immediate intents never form an episode.
    immediate = ci.request_cancel(tmp_path, "t-act2")
    assert ostop.owner_stop_active(immediate, now=now, grace_sec=120.0) is False


def test_running_owner_stop_tasks_reads_the_durable_projection(tmp_path):
    _graceful_intent(tmp_path, "t-held")
    ci.request_cancel(tmp_path, "t-hard")            # immediate: not held
    held = ostop.running_owner_stop_tasks(tmp_path, grace_sec=120.0)
    assert held == {"t-held"}
    assert ostop.running_owner_stop_tasks(tmp_path, grace_sec=0.0) == set()


def test_timeout_enforcement_bypasses_a_held_task_whole(tmp_path, monkeypatch):
    """§12.2 item 8: a hard-over-timeout RUNNING task inside an active owner-stop
    episode is skipped by the generic enforcement loop — not withdrawn, killed,
    reaped, or retried this tick."""
    from supervisor import queue as q

    _graceful_intent(tmp_path, "t-timeout")
    meta = {
        "task": {"id": "t-timeout", "chat_id": 0, "type": "task"},
        "started_at": time.time() - 10_000_000,       # absurdly over every rail
        "last_heartbeat_at": time.time() - 10_000_000,
        "attempt": 1,
    }
    q_isolated = _isolate_queue(monkeypatch, tmp_path, running={"t-timeout": meta})
    monkeypatch.setattr(q, "FINALIZATION_GRACE_SEC", 120.0, raising=False)
    q_isolated._enforce_task_timeouts_locked(None, time.time(), 0, {})
    assert "t-timeout" in q_isolated.RUNNING          # untouched, no kill path ran
    assert q_isolated.RUNNING["t-timeout"] is meta


def _fake_queue(tmp_path, running):
    return SimpleNamespace(
        _queue_lock=threading.Lock(),
        RUNNING=running,
        PENDING=[],
        DRIVE_ROOT=tmp_path,
        FINALIZATION_GRACE_SEC=120.0,
        _task_drive_for_task=lambda task, tid: tmp_path,
    )


def test_sweep_hold_arms_the_episode_idempotently(tmp_path, monkeypatch):
    from supervisor import workers

    toasts = []
    monkeypatch.setattr(
        workers, "get_event_q", lambda: SimpleNamespace(put=toasts.append),
    )
    intent = _graceful_intent(tmp_path, "t-arm")
    running = {"t-arm": {"task": {"id": "t-arm", "chat_id": 5}, "started_at": time.time()}}
    q = _fake_queue(tmp_path, running)
    assert ostop.sweep_owner_stop_hold(q, "t-arm", intent, now=time.time()) is True
    control_id = ostop.owner_stop_control_id(intent)
    assert control_id == f"ownerstop:{intent['request_id']}"
    # The coupled control + RUNNING latch both carry the deterministic identity.
    assert running["t-arm"]["finalization_control_msg_id"] == control_id
    assert running["t-arm"]["finalization_reason"] == REASON_OWNER_REQUESTED_FINALIZATION
    rows = _finalize_rows(tmp_path, "t-arm")
    assert len(rows) == 1
    assert rows[0]["msg_id"] == control_id
    assert rows[0]["text"].startswith(REASON_OWNER_REQUESTED_FINALIZATION)
    # The owner-facing toast replaced the generic reached-terminal wording.
    assert len(toasts) == 1
    assert toasts[0]["chat_id"] == 5 and toasts[0]["is_progress"] is True
    assert "summarize and stop" in toasts[0]["text"]
    assert "Stop now remains available" in toasts[0]["text"]
    # A watchdog/restart replay re-arms the SAME id: no duplicate control/toast.
    assert ostop.sweep_owner_stop_hold(q, "t-arm", intent, now=time.time()) is True
    assert len(_finalize_rows(tmp_path, "t-arm")) == 1
    assert len(toasts) == 1


def test_sweep_feeds_custody_for_settled_pending_or_expired_roots(tmp_path, monkeypatch):
    from supervisor import workers

    monkeypatch.setattr(
        workers, "get_event_q", lambda: SimpleNamespace(put=lambda _e: None),
    )
    now = time.time()
    # Settled root: natural completion won — custody settles the intent honestly.
    intent = _graceful_intent(tmp_path, "t-done", allow_settled_target=True)
    write_task_result(tmp_path, "t-done", "completed", result="finished naturally")
    q = _fake_queue(tmp_path, {"t-done": {"task": {"id": "t-done", "chat_id": 0}}})
    assert ostop.sweep_owner_stop_hold(q, "t-done", intent, now=now) is False
    # Pending root (never started): zero model turns, custody feed.
    pending_intent = _graceful_intent(tmp_path, "t-pend")
    assert ostop.sweep_owner_stop_hold(
        _fake_queue(tmp_path, {}), "t-pend", pending_intent, now=now,
    ) is False
    # Expired deadline: the episode is over; the generic path proceeds.
    expired = dict(_graceful_intent(tmp_path, "t-exp"))
    assert ostop.sweep_owner_stop_hold(
        _fake_queue(tmp_path, {"t-exp": {"task": {"id": "t-exp", "chat_id": 0}}}),
        "t-exp", expired, now=now + 400.0,
    ) is False
    # And no episode was armed anywhere along the way.
    for tid in ("t-done", "t-pend", "t-exp"):
        assert _finalize_rows(tmp_path, tid) == []


# ---------------------------------------------------------------------------
# Q4=A summary suppression + Q6=A child projection
# ---------------------------------------------------------------------------


def test_graceful_summary_suppressed_only_for_completed_finalize_roots(tmp_path):
    q = _fake_queue(tmp_path, {})
    # SUCCESS: finalize intent + COMPLETED durable result -> suppressed + forensic.
    _graceful_intent(tmp_path, "t-ok", allow_settled_target=True)
    write_task_result(tmp_path, "t-ok", "completed", result="final summary answer")
    assert ostop.graceful_summary_suppressed(q, "t-ok") is True
    forensics = (tmp_path / "logs" / "supervisor.jsonl").read_text(encoding="utf-8")
    assert "owner_stop_summary_suppressed" in forensics
    # Expiry -> cancelled keeps the tree's ONE receipt.
    _graceful_intent(tmp_path, "t-exp2", allow_settled_target=True)
    write_task_result(tmp_path, "t-exp2", "cancelled", result="expired")
    assert ostop.graceful_summary_suppressed(q, "t-exp2") is False
    # An immediate stop never suppresses.
    ci.request_cancel(tmp_path, "t-imm2", allow_settled_target=True)
    write_task_result(tmp_path, "t-imm2", "completed", result="done")
    assert ostop.graceful_summary_suppressed(q, "t-imm2") is False


def test_child_result_projection_is_bounded_and_includes_cancelled_children(tmp_path):
    q = _fake_queue(tmp_path, {})
    for i in range(3):
        write_task_result(
            tmp_path, f"kid-{i}", "cancelled",
            result=f"child {i} partial result " + "x" * 400,
            root_task_id="t-root", parent_task_id="t-root",
        )
    projection = ostop._child_result_projection(q, "t-root")
    assert projection.startswith("[CHILD_RESULTS]")
    for i in range(3):
        assert f"kid-{i} (cancelled):" in projection
    # Each preview is bounded to the cap (240 chars + ellipsis).
    for line in projection.splitlines()[1:]:
        assert len(line) < 340
    # A childless root projects nothing (the control stays the bare reason).
    assert ostop._child_result_projection(q, "t-lonely") == ""


def test_owner_requested_finalization_is_a_best_effort_reason_and_bench_truncation_code():
    from devtools.benchmarks.common.result_index import RUNTIME_TRUNCATION_REASON_CODES
    from ouroboros.outcomes import BEST_EFFORT_REASON_CODES

    assert REASON_OWNER_REQUESTED_FINALIZATION in BEST_EFFORT_REASON_CODES
    assert REASON_OWNER_REQUESTED_FINALIZATION in RUNTIME_TRUNCATION_REASON_CODES


def test_owner_stop_deadline_is_immutable_from_requested_at(tmp_path):
    requested = utc_now_iso()
    intent = {"requested_at": requested, "stop_policy": ci.STOP_POLICY_FINALIZE}
    deadline = ostop.owner_stop_deadline_ts(intent, 120.0)
    assert deadline > 0
    # Progress/heartbeats never extend it: the same intent yields the same deadline.
    assert ostop.owner_stop_deadline_ts(intent, 120.0) == deadline
    assert ostop.owner_stop_deadline_ts({}, 120.0) == 0.0
