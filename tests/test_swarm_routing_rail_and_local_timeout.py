"""Regression tests for the Swarm routing turn that never terminated.

Field failure (task 4ba3fea8, local Qwen3 27B GGUF): an owner message sent with
the Swarm toggle produced nothing at all. Two independent defects stacked:

1. ``_enforce_swarm_actions`` held finalization and re-sent a routing reminder
   every round with no bound of its own, so a model that never emitted the
   routing tool call would nudge until MAX_ROUNDS (200).
2. The local llama.cpp lane carried a hardcoded 180s per-request timeout AND
   resent a timed-out request three times, so one round burned 3x180s and
   surfaced a single ``APITimeoutError`` after nine minutes.
"""
from __future__ import annotations

import types

import pytest


# --- 1. the routing turn is bounded -----------------------------------------

def _router_tools(**ctx_fields):
    ctx = types.SimpleNamespace(
        task_metadata={"force_plan": True},
        is_ephemeral_turn=True,
        **ctx_fields,
    )
    return types.SimpleNamespace(_ctx=ctx)


def _trace():
    return {"reasoning_notes": []}


def test_routing_reminder_counts_toward_a_finite_budget():
    """Each held round increments the nudge counter; below the budget the rail
    stays silent and the loop asks for one more model round."""
    from ouroboros import loop as loop_mod

    tools = _router_tools()
    messages: list = []
    progress: list = []
    llm_trace = _trace()

    for expected in range(1, loop_mod._SWARM_ROUTING_NUDGE_BUDGET + 1):
        held = loop_mod._enforce_swarm_actions(
            "here is an inline answer instead of a routing call",
            messages, tools, llm_trace, progress.append,
        )
        assert held is True
        assert loop_mod._swarm_routing_nudges(tools._ctx) == expected

    assert progress == ["Swarm routing action required before final response."] * (
        loop_mod._SWARM_ROUTING_NUDGE_BUDGET
    )


def test_rail_fires_once_the_routing_budget_is_spent(monkeypatch):
    """Past the budget the host stops asking and ends the turn on the typed
    routing rail instead of nudging to MAX_ROUNDS."""
    from ouroboros import loop as loop_mod

    seen = {}

    def _fake_forced(ctx, llm_trace, reason_code):
        seen["reason_code"] = reason_code
        return ("forced text", {"execution_status": "failed"}, llm_trace)

    monkeypatch.setattr(loop_mod, "_forced_swarm_router_result", _fake_forced)

    tools = _router_tools(_swarm_routing_nudge_count=loop_mod._SWARM_ROUTING_NUDGE_BUDGET)
    llm_trace = _trace()

    rail = loop_mod._swarm_routing_exhausted_rail(object(), tools, llm_trace)

    assert rail is not None
    assert rail[0] == "forced text"
    assert seen["reason_code"] == "swarm_routing_unfulfilled"
    assert any("unfulfilled routing reminders" in n for n in llm_trace["reasoning_notes"])


def test_rail_stays_silent_below_the_budget(monkeypatch):
    from ouroboros import loop as loop_mod

    monkeypatch.setattr(
        loop_mod, "_forced_swarm_router_result",
        lambda *a, **k: pytest.fail("rail fired below the nudge budget"),
    )
    tools = _router_tools(
        _swarm_routing_nudge_count=loop_mod._SWARM_ROUTING_NUDGE_BUDGET - 1)

    assert loop_mod._swarm_routing_exhausted_rail(object(), tools, _trace()) is None


def test_rail_never_preempts_a_real_handoff(monkeypatch):
    """A routing turn that DID attempt a handoff owns its own finalization; the
    exhaustion rail must not overwrite a durable admission with a failure."""
    from ouroboros import loop as loop_mod

    monkeypatch.setattr(
        loop_mod, "_forced_swarm_router_result",
        lambda *a, **k: pytest.fail("rail preempted a real routing attempt"),
    )
    tools = _router_tools(
        _swarm_routing_nudge_count=loop_mod._SWARM_ROUTING_NUDGE_BUDGET + 5,
        _swarm_handoff_attempt={"status": "scheduled", "task_id": "t1"},
    )

    assert loop_mod._swarm_routing_exhausted_rail(object(), tools, _trace()) is None


def test_rail_ignores_ordinary_non_router_turns(monkeypatch):
    from ouroboros import loop as loop_mod

    monkeypatch.setattr(
        loop_mod, "_forced_swarm_router_result",
        lambda *a, **k: pytest.fail("rail fired on a non-router turn"),
    )
    ordinary = types.SimpleNamespace(
        _ctx=types.SimpleNamespace(
            task_metadata={"force_plan": True},
            is_ephemeral_turn=False,          # a full Swarm initiative, not routing
            _swarm_routing_nudge_count=99,
        )
    )

    assert loop_mod._swarm_routing_exhausted_rail(object(), ordinary, _trace()) is None


# --- 2. the local lane gets a workable deadline ------------------------------

def _local_client(monkeypatch, llm_mod):
    client = llm_mod.LLMClient.__new__(llm_mod.LLMClient)
    monkeypatch.setattr(client, "_get_local_client", lambda: object(), raising=False)
    monkeypatch.setattr(
        client, "_normalize_system_message_placement", lambda m: list(m), raising=False)
    monkeypatch.setattr(
        client, "_strip_openrouter_roundtrip_metadata", lambda m: list(m), raising=False)
    monkeypatch.setattr(
        client, "_copy_messages_with_cache_policy",
        lambda m, **k: [dict(x) for x in m], raising=False)
    return client


class _Timeout(Exception):
    """Stands in for openai.APITimeoutError, matched by name and wording."""

    def __init__(self):
        super().__init__("Request timed out.")


_Timeout.__name__ = "APITimeoutError"


@pytest.mark.parametrize("exc, expected", [
    (_Timeout(), True),
    (RuntimeError("Request timed out."), True),
    (RuntimeError("Connection refused"), False),
    (RuntimeError("Error code: 400 - prompt is too long"), False),
    (RuntimeError("max_tokens 65536 exceeds maximum context length 32768"), False),
])
def test_timeout_classification(exc, expected):
    from ouroboros import llm as llm_mod

    assert llm_mod._is_request_timeout_exception(exc) is expected


def test_local_timeout_is_configurable_and_not_the_old_180s(monkeypatch):
    """The per-request deadline comes from settings. The local lane is the
    SLOWEST route the loop takes, so it must not carry the shortest deadline."""
    from ouroboros import llm as llm_mod
    from ouroboros.config import SETTINGS_DEFAULTS

    assert SETTINGS_DEFAULTS["OUROBOROS_LOCAL_REQUEST_TIMEOUT_SEC"] > 180

    seen = {}

    def _fake_execute(request, send, before):
        raise RuntimeError("boom")

    monkeypatch.setattr(llm_mod, "_execute_candidate", _fake_execute)
    monkeypatch.setattr(llm_mod, "_attempt_request", lambda *a, **k: None)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        llm_mod, "_physical_candidate",
        lambda payload: seen.setdefault("payload", dict(payload)) or dict(payload))
    monkeypatch.setenv("OUROBOROS_LOCAL_REQUEST_TIMEOUT_SEC", "900")

    client = _local_client(monkeypatch, llm_mod)
    with pytest.raises(RuntimeError):
        client._chat_local([{"role": "user", "content": "hi"}], None, 512, "auto")

    assert seen["payload"]["timeout"] == 900.0


def test_explicit_caller_timeout_may_only_raise_the_local_floor(monkeypatch):
    from ouroboros import llm as llm_mod

    seen = {}
    monkeypatch.setattr(
        llm_mod, "_execute_candidate",
        lambda request, send, before: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(llm_mod, "_attempt_request", lambda *a, **k: None)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        llm_mod, "_physical_candidate",
        lambda payload: seen.setdefault("payload", dict(payload)) or dict(payload))
    monkeypatch.setenv("OUROBOROS_LOCAL_REQUEST_TIMEOUT_SEC", "600")

    client = _local_client(monkeypatch, llm_mod)
    with pytest.raises(RuntimeError):
        client._chat_local(
            [{"role": "user", "content": "hi"}], None, 512, "auto", timeout=120.0)

    # A caller asking for LESS than the local floor does not shorten it.
    assert seen["payload"]["timeout"] == 600.0


def test_a_timed_out_local_request_is_never_resent(monkeypatch):
    """llama.cpp is still generating in its single slot after a timeout; the old
    three-attempt resend serialized into 3x the deadline and returned nothing."""
    from ouroboros import llm as llm_mod

    calls = {"n": 0}

    def _fake_execute(request, send, before):
        calls["n"] += 1
        raise _Timeout()

    monkeypatch.setattr(llm_mod, "_execute_candidate", _fake_execute)
    monkeypatch.setattr(llm_mod, "_attempt_request", lambda *a, **k: None)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda _s: None)

    client = _local_client(monkeypatch, llm_mod)
    with pytest.raises(Exception) as excinfo:
        client._chat_local([{"role": "user", "content": "hi"}], None, 512, "auto")

    assert type(excinfo.value).__name__ == "APITimeoutError"
    assert calls["n"] == 1
