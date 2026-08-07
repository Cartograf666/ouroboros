"""The nanny postcondition (owner decision, 2026-08-07).

A child dispatched onto the delegated substrate (executor=harness) that reaches
finalization with ZERO delegate_start calls silently unmade a substrate decision:
the wave-0 incident (task 21d1d220, 2026-08-06) burned $8.89 of metered opus
tokens under a dispatch that promised subscription execution, and only its own
prose admitted it. The obligation used to be a prompt note alone; the seam below
makes the FACT structural (one re-loop) while the decision stays the child's.
"""

from types import SimpleNamespace

from ouroboros.loop import _maybe_inject_finalization_nudges


def _run(ctx_obj, msgs, tool_calls):
    return _maybe_inject_finalization_nudges(
        SimpleNamespace(_ctx=ctx_obj), None or __import__("pathlib").Path("."), "t",
        {"reasoning_notes": [], "tool_calls": tool_calls}, "done", msgs, lambda *_: None,
    )


def test_harness_child_finalizing_without_delegation_gets_one_nudge():
    ctx = SimpleNamespace(_nanny_route_dispatched=True, _nanny_finalization_injected=False)
    msgs: list = []
    assert _run(ctx, msgs, []) is True
    assert any("NANNY_DID_NOT_DELEGATE" in m.get("content", "") for m in msgs)
    # One-shot: the latch suppresses a second injection — the child may still
    # finalize with a stated reason, never a hard gate on its judgment (P5).
    assert _run(ctx, [], []) is False


def test_a_delegating_nanny_and_a_native_child_are_not_nudged():
    # A single delegate_start call in the trace IS the receipt — even a refused
    # one proves the substrate decision was faced rather than ignored.
    delegating = SimpleNamespace(_nanny_route_dispatched=True,
                                 _nanny_finalization_injected=False)
    assert _run(delegating, [], [{"tool": "delegate_start", "args": {}}]) is False

    native = SimpleNamespace(_nanny_route_dispatched=False,
                             _nanny_finalization_injected=False)
    assert _run(native, [], []) is False

    undispatched = SimpleNamespace()  # a ctx that never saw a dispatch at all
    assert _run(undispatched, [], []) is False


def test_forced_finalization_carries_the_nanny_note_instead_of_relooping():
    """Forced finalization may not re-loop (that is its whole point), so the
    substrate fact rides the one final prompt instead: a harness-dispatched child
    that made zero delegate_start calls sees the note and can state why."""
    import pathlib
    from unittest.mock import patch

    from ouroboros.loop import _RoundLimitContext, _forced_final_answer

    class _Ctx:
        pass

    class _Tools:
        def __init__(self, nanny):
            self._ctx = _Ctx()
            self._ctx._nanny_route_dispatched = nanny

    def run(nanny, tool_calls):
        messages = []
        ctx = _RoundLimitContext(
            messages=messages, llm=None, active_model="m", active_effort="low",
            max_retries=0, drive_logs=pathlib.Path("."), task_id="t", round_idx=1,
            event_queue=None, accumulated_usage={}, task_type="task",
            active_use_local=False, max_rounds=1,
        )
        ctx.tools = _Tools(nanny)
        ctx.llm_trace = {"reasoning_notes": [], "tool_calls": tool_calls}
        with patch("ouroboros.loop._call_forced_model_once", return_value="done"), \
             patch("ouroboros.loop._finalize_forced_services"), \
             patch("ouroboros.loop._forced_swarm_router_result", return_value=None), \
             patch("ouroboros.loop._drain_forced_owner_directives", return_value=False):
            _forced_final_answer(ctx, prompt="wrap up", fallback_text="fb",
                                 reason_code="round_limit")
        return "\n".join(m.get("content", "") for m in messages)

    assert "delegated substrate" in run(True, [])
    assert "delegated substrate" not in run(False, [])
    assert "delegated substrate" not in run(True, [{"tool": "delegate_start"}])
