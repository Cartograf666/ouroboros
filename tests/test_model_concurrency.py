"""#4 per-model concurrency cap (model_concurrency.model_call_slot).

Covers: the cap actually serializes concurrent calls, the slot is released even when
the wrapped body raises, the disabled mode is a pass-through, and acquisition is
deadline-bounded fail-soft (a slot that can't be acquired before the deadline proceeds
WITHOUT throttling rather than blocking the task).
"""

from __future__ import annotations

import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    from ouroboros import model_concurrency

    model_concurrency.reset_for_tests()
    yield
    model_concurrency.reset_for_tests()


def test_cap_serializes_concurrent_calls(monkeypatch):
    from ouroboros import model_concurrency

    monkeypatch.setenv("OUROBOROS_MODEL_MAX_CONCURRENCY", "2")
    model_concurrency.reset_for_tests()
    live = []
    peak = [0]
    lock = threading.Lock()

    def worker():
        with model_concurrency.model_call_slot("z-ai/glm-5.2", False, deadline_ts=time.time() + 30):
            with lock:
                live.append(1)
                peak[0] = max(peak[0], len(live))
            time.sleep(0.15)
            with lock:
                live.pop()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] <= 2, f"cap=2 violated, peak={peak[0]}"


def test_slot_released_on_exception(monkeypatch):
    from ouroboros import model_concurrency

    monkeypatch.setenv("OUROBOROS_MODEL_MAX_CONCURRENCY", "1")
    model_concurrency.reset_for_tests()
    with pytest.raises(ValueError):
        with model_concurrency.model_call_slot("m", False, deadline_ts=time.time() + 30):
            raise ValueError("boom")
    # If the slot leaked, this second acquire (cap=1) would block past the test timeout.
    acquired = []
    with model_concurrency.model_call_slot("m", False, deadline_ts=time.time() + 5):
        acquired.append(True)
    assert acquired == [True]


def test_disabled_is_passthrough(monkeypatch):
    from ouroboros import model_concurrency

    monkeypatch.setenv("OUROBOROS_MODEL_MAX_CONCURRENCY", "0")
    model_concurrency.reset_for_tests()
    assert model_concurrency.enabled() is False
    ran = []
    with model_concurrency.model_call_slot("m", False, None):
        ran.append(True)
    assert ran == [True]


def test_deadline_failsoft_does_not_block(monkeypatch):
    """With cap=1 and a slot already held, a second acquire whose deadline is already
    past must NOT block — it proceeds without the slot (fail-soft)."""
    from ouroboros import model_concurrency

    monkeypatch.setenv("OUROBOROS_MODEL_MAX_CONCURRENCY", "1")
    model_concurrency.reset_for_tests()
    held = threading.Event()
    release = threading.Event()

    def holder():
        with model_concurrency.model_call_slot("m", False, deadline_ts=time.time() + 30):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(2)
    # Deadline already in the past -> acquire times out fast -> proceeds without throttle.
    t0 = time.time()
    ran = []
    with model_concurrency.model_call_slot("m", False, deadline_ts=time.time() - 1):
        ran.append(True)
    assert ran == [True]
    assert time.time() - t0 < 2.0, "fail-soft acquire must not block on a past deadline"
    release.set()
    t.join()


def test_reentrant_route_passes_through(monkeypatch):
    """A nested slot on the same route must not wait or consume a second permit.

    The guard lives at the one provider seam (``LLMClient.chat``); the four callers
    that already open a slot would otherwise make one logical call wait twice and
    hold two permits of a cap-3 route. With cap=1 a non-reentrant implementation
    deadlocks here until the wait ceiling.
    """
    from ouroboros import model_concurrency

    monkeypatch.setenv("OUROBOROS_MODEL_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("OUROBOROS_MODEL_SLOT_MAX_WAIT_SEC", "3")
    model_concurrency.reset_for_tests()

    started = time.time()
    with model_concurrency.model_call_slot("m", False, deadline_ts=time.time() + 30):
        with model_concurrency.model_call_slot("m", False, deadline_ts=time.time() + 30):
            pass
    assert time.time() - started < 1.0, "nested same-route slot waited instead of passing through"

    # The outer slot released exactly one permit, so the route is free again.
    acquired = model_concurrency._semaphore_for("m", False).acquire(timeout=1)
    assert acquired, "re-entry leaked a permit"
    model_concurrency._semaphore_for("m", False).release()


def test_reentry_marks_are_per_thread_and_per_route(monkeypatch):
    from ouroboros import model_concurrency

    monkeypatch.setenv("OUROBOROS_MODEL_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("OUROBOROS_MODEL_SLOT_MAX_WAIT_SEC", "2")
    model_concurrency.reset_for_tests()

    # A DIFFERENT route nested inside is a real acquisition, not a pass-through.
    with model_concurrency.model_call_slot("route-a", False, deadline_ts=time.time() + 30):
        other = model_concurrency._semaphore_for("route-b", False)
        with model_concurrency.model_call_slot("route-b", False, deadline_ts=time.time() + 30):
            assert not other.acquire(timeout=0.2), "nested different route did not take its own permit"

    # Another THREAD holding the same route must still be capped, not passed through.
    blocked = []
    with model_concurrency.model_call_slot("route-c", False, deadline_ts=time.time() + 30):
        def worker():
            start = time.time()
            with model_concurrency.model_call_slot("route-c", False, deadline_ts=time.time() + 1.0):
                blocked.append(time.time() - start)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)
    assert blocked and blocked[0] >= 0.5, (
        f"a second thread bypassed the cap for a held route (waited {blocked})"
    )


def _stub_client(monkeypatch, calls, hold=0.0):
    """LLMClient whose remote dispatch is stubbed, so `chat` exercises only the seam."""
    from ouroboros.llm import LLMClient

    client = LLMClient()
    monkeypatch.setattr(client, "_resolve_remote_target", lambda model: {"provider": "stub", "model": model})

    def fake_remote(*args, **kwargs):
        calls.append(time.time())
        if hold:
            time.sleep(hold)
        return {"role": "assistant", "content": "ok"}, {}

    monkeypatch.setattr(client, "_chat_remote", fake_remote)
    return client


def test_llm_chat_holds_a_route_slot(monkeypatch):
    """The seam is inside `LLMClient.chat`, so every call site inherits the cap.

    Twelve runtime call sites reach a provider; only four opened a slot, leaving the
    review lanes, deep self-review, consolidation, semantic dedup and skill publish
    unthrottled against a documented cap of 3.
    """
    from ouroboros import model_concurrency

    monkeypatch.setenv("OUROBOROS_MODEL_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("OUROBOROS_MODEL_SLOT_MAX_WAIT_SEC", "5")
    model_concurrency.reset_for_tests()

    calls: list = []
    client = _stub_client(monkeypatch, calls, hold=0.3)

    held = model_concurrency._semaphore_for("stub/model", False)
    assert held.acquire(timeout=1)
    try:
        done = []

        def worker():
            client.chat(messages=[{"role": "user", "content": "x"}], model="stub/model")
            done.append(True)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=1.0)
        assert not calls, "chat reached the provider while the route's only permit was held"
    finally:
        held.release()
    thread.join(timeout=10)
    assert calls, "chat never proceeded after the permit was released"


def test_outer_slot_plus_chat_seam_is_one_permit(monkeypatch):
    """The existing outer callers keep working unchanged — no double-hold, no stall."""
    from ouroboros import model_concurrency

    monkeypatch.setenv("OUROBOROS_MODEL_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("OUROBOROS_MODEL_SLOT_MAX_WAIT_SEC", "3")
    model_concurrency.reset_for_tests()

    calls: list = []
    client = _stub_client(monkeypatch, calls)

    started = time.time()
    with model_concurrency.model_call_slot("stub/model", False, deadline_ts=time.time() + 30):
        client.chat(messages=[{"role": "user", "content": "x"}], model="stub/model")
    assert calls, "the wrapped call never reached the provider"
    assert time.time() - started < 1.0, "outer slot + inner seam waited on itself"

    free = model_concurrency._semaphore_for("stub/model", False).acquire(timeout=1)
    assert free, "the double-wrapped call leaked a permit"
    model_concurrency._semaphore_for("stub/model", False).release()
