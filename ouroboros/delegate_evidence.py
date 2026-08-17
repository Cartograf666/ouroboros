"""The read-side execution-evidence projection over the delegate custody rows.

Extracted from ``ouroboros/delegate_custody.py`` at its module-size ceiling: this
is one coherent READ concern — what a task's delegated runs provably did — with a
single completion-seam consumer (``subagents.envelope_from_task``).
``delegate_custody`` re-exports it (same object), so every existing reference and
monkeypatch target keeps the one historical name. Custody primitives are imported
lazily inside the function so this leaf never participates in an import cycle
with the module that owns the rows.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

log = logging.getLogger(__name__)

# The nanny finalization-nudge stamp (B3, owner decision 3A): written by the
# WORKER at the loop's injection seam through ``delegate_custody.emit`` — only
# when the composed reminder was NON-EMPTY (the ctx `_nanny_finalization_injected`
# flag is set even on suppression, so it can never be the signal). Task-scoped
# (no run_id — the run-custody replay skips it by design); the type keeps the
# ``delegate_run`` prefix so the custody scan prefilter still yields it. Defined
# HERE, beside its one reader, because ``delegate_custody`` sits exactly at its
# module-size ceiling.
NANNY_NUDGE_STAMP = "delegate_run_nanny_nudge_injected"


def record_nanny_nudge_stamp(ctx: Any, task_id: str, nanny_code: str) -> None:
    """Durably stamp "the finalization nudge was really injected" for this task.

    Synchronous same-process append on the CANONICAL (budget) root — the same
    root ``task_execution_evidence`` reads back at the completion seam, so the
    fact needs no supervisor round-trip and no O(history) side scan. Fail-soft:
    a stamp that cannot land loses only the completion-seam disclosure.
    """
    try:
        from ouroboros import delegate_custody as custody

        custody.emit(custody.custody_root(ctx), NANNY_NUDGE_STAMP, {
            "task_id": str(task_id or ""), "nanny_code": str(nanny_code or "")})
    except Exception:
        log.debug("nanny nudge stamp failed", exc_info=True)


def task_execution_evidence(drive_root: Any, task_id: str) -> Dict[str, Any]:
    """Aggregate ONE task's delegated-run facts from the durable custody rows.

    The completion-seam reconciliation reads this: `executor_route` is a DISPATCH
    decision, these rows are the EVIDENCE of what actually ran, and the two are
    compared exactly once (``subagents.envelope_from_task``) instead of in every
    reader. ``subscription_cost_usd`` is the sum of DISCLOSED settled spend and
    ``None`` while nothing settled or any settled run left its spend undisclosed —
    unknown never renders as zero.
    """
    from ouroboros import delegate_custody as custody

    tid = str(task_id or "")
    started: set = set()
    settled: set = set()
    succeeded: set = set()
    failure_states: List[str] = []
    models: List[str] = []
    cost_total, cost_known, cost_estimated = 0.0, True, False
    # Scope finding (a5e59bdf gate): an UNREADABLE log must not collapse into
    # the same zero-count result as a proven empty one — a reader would then
    # accuse a nanny of "zero attempts" on evidence it never saw. A missing
    # file IS a positively-established empty state (no row could exist);
    # existing-but-unreadable is not, and _iter_rows swallows its own OSError.
    evidence_read_failed = False
    nudge_recorded = False
    _log_path = custody.event_log_path(drive_root)
    try:
        if _log_path.exists():
            with _log_path.open("rb"):
                pass
    except OSError:
        evidence_read_failed = True
    for row in custody._iter_rows(_log_path):
        if str(row.get("task_id") or "") != tid:
            continue
        if str(row.get("type") or "") == NANNY_NUDGE_STAMP:
            # Task-scoped stamp, no run_id — read here so the completion seam
            # gets the fact from the scan it already pays for (B3).
            nudge_recorded = True
            continue
        run_id = str(row.get("run_id") or "")
        if not run_id:
            continue
        kind = str(row.get("type") or "")
        if kind == custody.STARTED:
            started.add(run_id)
        elif kind == custody.CLOSED_ABSENT and run_id not in settled:
            # Closed-without-settlement is still TERMINAL: leaving it in the
            # started-minus-settled gap would read as "still executing" to the
            # pending/settled readers (nanny reminder) forever. No ledger row was
            # written, so its spend is undisclosed, never zero.
            settled.add(run_id)
            failure_states.append("closed_absent")
            cost_known = False
        elif kind == custody.SETTLED and run_id not in settled:
            settled.add(run_id)
            state = str(row.get("state") or "")
            if state in custody.SUCCEEDED_STATES:
                succeeded.add(run_id)
            elif state:
                failure_states.append(state)
            if row.get("spend_disclosed") and row.get("cost_usd") is not None:
                try:
                    cost_total += float(row.get("cost_usd") or 0.0)
                except (TypeError, ValueError):
                    cost_known = False
                if row.get("spend_estimated"):
                    cost_estimated = True
            else:
                cost_known = False
        # ENGINE-reported models only (SETTLED rows): a STARTED row carries the
        # requested pin, and with an owner default model that pin is routinely
        # non-empty — listing it would name a model that never executed.
        if kind == custody.SETTLED:
            model = str(row.get("model") or "")
            if model and model not in models:
                models.append(model)
    return {
        # A settled row whose started row fell out of the log is still a run that ran.
        "delegated_runs_started": len(started | settled),
        "delegated_runs_settled": len(settled),
        # The terminal-state axis (F4, 2026-08-10 saga): a run that STARTED and
        # FAILED is an ATTEMPTED route, not a refusal to delegate. Readers (the
        # nanny nudge, the forced-path note, the completion seam) must be able to
        # tell "never tried" from "tried and the run died" without re-parsing the
        # event log; the forced-path nanny note keys on the succeeded count to
        # stop nagging over finished work.
        "delegated_runs_succeeded": len(succeeded),
        # C3 (additive raw counter): settled runs that did NOT succeed, including
        # closed-absent. Counts are delegated-run facts only — the NATIVE (metered)
        # contribution beside them is unknown, and no share/ratio is derivable here.
        "delegated_runs_failed": len(settled) - len(succeeded),
        "delegated_run_failure_states": sorted(set(failure_states)),
        # True only when the canonical log EXISTS but could not be opened —
        # zero counts are then "unknown", not "established" (additive key).
        "evidence_read_failed": evidence_read_failed,
        # B3 (additive key): a non-empty finalization nudge was durably stamped
        # for this task. The completion seam combines it with completed status
        # + zero started runs into the typed substrate disclosure.
        "nanny_nudge_recorded": nudge_recorded,
        "subscription_cost_usd": round(cost_total, 6) if (settled and cost_known) else None,
        # The settlement row's own estimated/final distinction, carried instead of
        # dropped: an estimated sum must never render as an exact receipt.
        "subscription_cost_estimated": bool(settled and cost_known and cost_estimated),
        "harness_models": models,
    }


__all__ = ["NANNY_NUDGE_STAMP", "record_nanny_nudge_stamp", "task_execution_evidence"]
