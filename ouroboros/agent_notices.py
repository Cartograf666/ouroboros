"""The sentences the agent is shown about its OWN run.

Four of them, all pure prose over facts someone else gathered: what the
provider's last error was, whether waiting cures it, what this nanny has spent,
and whether it should have delegated at all. No control flow, no I/O of their
own — which is why they do not belong in the loop that happens to print them.

Split out of ``ouroboros/loop.py`` under the size ratchet (BIBLE P7: net
complexity growth per cycle approaches zero). The two tests that pinned the
provider hints import them from here.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Dict, Tuple

from ouroboros.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


def _provider_failure_hint(accumulated_usage: Dict[str, Any]) -> str:
    detail = " ".join(str(accumulated_usage.get("_last_llm_error") or "").split()).strip()
    if not detail:
        return ""
    return f" Last provider error: {detail}"


def _provider_recovery_hint(accumulated_usage: Dict[str, Any]) -> str:
    """Explain whether retrying later is likely to help."""
    kind = str(accumulated_usage.get("_last_llm_error_kind") or "").strip()
    if kind == "subscription_window_exhausted":
        reset_at = str(accumulated_usage.get("_last_llm_reset_at") or "").strip()
        when = f" It resets at {reset_at}." if reset_at else ""
        return (
            " The subscription window for the delegated route is spent. This is "
            f"TRANSIENT, not a billing refusal — waiting cures it.{when} Retrying is "
            "scheduled against that reset time, not the ordinary short backoff."
        )
    if kind in {"quota_exhausted", "auth_error", "request_too_large", "bad_request", "context_overflow"}:
        guidance = {
            "quota_exhausted": "The provider rejected the request for quota/billing reasons; retrying the same request will not help until the key/account limit changes.",
            "auth_error": "The provider rejected authentication/authorization; retrying the same request will not help until the configured key or provider access is fixed.",
            "request_too_large": "The provider rejected the request size/output-token shape; retrying the same request will not help without reducing context/output demand or changing model capacity.",
            "bad_request": "The provider rejected the request shape; retrying the same request will not help until the transcript/tool payload is fixed.",
            "context_overflow": "The context overflowed the model window; retrying the same request will not help without reducing context or changing model capacity.",
        }.get(kind, "Retrying the same provider request will not help until the underlying request/account issue changes.")
        return f" {guidance}"
    detail = str(accumulated_usage.get("_last_llm_error") or "").lower()
    if "prefill" in detail or "conversation must end with a user message" in detail:
        return (
            " This looks like a client-side transcript-shape error, not a "
            "provider outage; retrying the same input will not help."
        )
    if "provider returned incomplete response" in detail or "finish_reason=null" in detail:
        return (
            " The provider returned incomplete responses repeatedly; this may "
            "be transient, but it can also indicate malformed client input."
        )
    return " If background consciousness is running, it will retry when the provider recovers."


# What a nanny is told it has spent. Prose over two numbers, in the same
# agent-facing register as the hints above.
def _nanny_burn_phrase(rounds: int, cost: float) -> str:
    return (f"{rounds} of your own metered LLM rounds (~${cost:.2f})" if cost > 0
            else f"{rounds} of your own metered LLM rounds")


def _nanny_metered_since_delegate_activity(ctx: Any) -> Tuple[int, float]:
    """(rounds, dollars) this task's OWN metered loop has spent since the last
    delegate-verb call — zero before the first round is marked."""
    progress = getattr(ctx, "_nanny_metered_progress", None)
    progress = progress if isinstance(progress, dict) else {}
    baseline = getattr(ctx, "_nanny_delegate_baseline", None)
    baseline = baseline if isinstance(baseline, dict) else {}
    try:
        rounds = max(0, int(progress.get("round") or 0) - int(baseline.get("round") or 0))
    except (TypeError, ValueError):
        rounds = 0
    try:
        cost = max(0.0, float(progress.get("cost") or 0.0) - float(baseline.get("cost") or 0.0))
    except (TypeError, ValueError):
        cost = 0.0
    return rounds, cost


def _nanny_finalization_message(
    tools: ToolRegistry, drive_root: pathlib.Path, task_id: str,
    trace_attempted: bool = False,
) -> str:
    """The honest nanny reminder for a harness-dispatched child at finalization —
    or '' when no reminder is deserved.

    F4 (2026-08-10 saga): the old reminder accused children whose delegated runs
    CRASHED of "choosing" not to delegate, and fired even when the delegate verbs
    were policy-hidden. Two structural facts fix both: the task's own visible
    toolset, and durable custody evidence (delegate_custody.
    task_execution_evidence), which spans the WHOLE task — per-execution
    llm_trace resets on continuation. `trace_attempted` is the third fact: a
    delegate_start in THIS execution's trace. It must not suppress the failure
    message (triad finding on e84475f2: delegate, run dies, finish by hand,
    finalize — all inside ONE execution), only the accusation when custody has
    no rows yet (a pending/uncustodied start is an attempt, not a choice)."""
    try:
        if "delegate_start" not in set(tools.available_tools()):
            return ""  # the verbs are invisible here; "you chose not to" would be false
    except Exception:
        log.debug("nanny nudge: toolset visibility check failed", exc_info=True)
    evidence: Dict[str, Any] = {}
    try:
        from ouroboros.delegate_custody import custody_root, task_execution_evidence

        # Split-root fix (2026-08-10 amendments): custody WRITES land on the
        # CANONICAL (budget) root, but this read used the loop's drive_root —
        # a split-root subagent's child drive has no custody rows, leaving
        # the nanny blind. Resolve the SAME root the writers use; the passed
        # drive_root stays the fallback (e.g. unit-test stubs).
        try:
            evidence_root = custody_root(tools._ctx)
        except Exception:
            evidence_root = drive_root
        evidence = task_execution_evidence(evidence_root, str(task_id or ""))
    except Exception:
        log.debug("nanny nudge: custody evidence read failed", exc_info=True)
    if evidence.get("delegated_runs_succeeded"):
        # The route WAS used and worked — but "used once" is not a permanent
        # license: the poltergeist children each ran ONE successful $0 run,
        # then co-built for tens of opus rounds while this early return kept
        # the nudge silent. Silence is now proportional to the measured burn
        # since the last delegated-run activity.
        rounds, cost = _nanny_metered_since_delegate_activity(tools._ctx)
        from ouroboros.task_pacing import NANNY_REMINDER_ROUNDS, NANNY_REMINDER_USD

        if rounds < NANNY_REMINDER_ROUNDS and cost < NANNY_REMINDER_USD:
            return ""
        return (
            "⚠️ NANNY_METERED_OVERRUN: your delegated run(s) succeeded, but you have "
            f"since spent {_nanny_burn_phrase(rounds, cost)} with no delegated-run "
            "activity. A successful run is verified and integrated, not rebuilt. If "
            "the remaining work is substantive, delegate it (a new delegate_start); "
            "if you are wrapping up, keep the wrap-up short and account for the "
            "metered spend honestly in your result."
        )
    started = int(evidence.get("delegated_runs_started") or 0)
    if not started and (evidence.get("evidence_read_failed") or not evidence):
        # Zero attempts is an ACCUSATION and needs positively-established
        # evidence: an unreadable custody log (or a failed read above) proves
        # nothing (scope finding on a5e59bdf).
        return ""
    if not started and trace_attempted:
        # A start this execution's trace saw but custody has no row for: pending
        # settlement or an uncustodied start. An attempt either way — neither
        # accusation fits, and the wait/cancel path owns its own disclosure.
        return ""
    settled = int(evidence.get("delegated_runs_settled") or 0)
    failure_states = [str(s) for s in (evidence.get("delegated_run_failure_states") or [])]
    pending = max(0, started - settled)
    if pending:
        # PENDING ≠ FAILED (sol review on b49f8192): a STARTED row with no
        # settlement may still be executing — calling it failed invites a
        # duplicate run, and finalizing over it orphans the result. Takes
        # precedence over the failed message: with a run in flight, "retry"
        # is wrong even when an earlier sibling died (still a fact below).
        failed_note = (
            f" {len(failure_states)} earlier run(s) already ended: {', '.join(failure_states)}."
            if failure_states else ""
        )
        return (
            "⚠️ NANNY_DELEGATED_RUN_PENDING: you routed work onto the delegated "
            f"substrate and {pending} delegated run(s) have started but not "
            "settled — they may still be executing. Do not finalize over an "
            "in-flight delegated run (its result would be orphaned) and do not "
            "start a duplicate: wait for or check it (delegate_wait) before "
            "finalizing, or cancel it (delegate_cancel) and say so." + failed_note
        )
    if started:
        states = ", ".join(failure_states) or "settled without a recorded terminal state"
        return (
            "⚠️ NANNY_DELEGATED_RUN_FAILED: you DID route work onto the delegated "
            f"substrate ({started} run(s) started), but none succeeded — your "
            f"delegated run(s) ended: {states}. Do not finalize as if delegation "
            "was never attempted: either retry it (delegate_start / delegate_wait) "
            "or state in your final answer that the delegated run failed and why "
            "the remaining work ran on metered API tokens."
        )
    return (
        "⚠️ NANNY_DID_NOT_DELEGATE: this task was dispatched onto the delegated "
        "substrate (executor=harness), but you are finalizing with ZERO "
        "delegate_start calls — the work would end up billed to metered API "
        "tokens the parent asked to avoid. Either delegate the remaining work "
        "now (delegate_start / delegate_wait), or finalize with an explicit "
        "statement of WHY delegation was not used (route refused, work shape "
        "unsuited, deadline) so your parent sees the substrate decision."
    )
