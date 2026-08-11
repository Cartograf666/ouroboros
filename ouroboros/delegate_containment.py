"""Containment verification for ONE delegated run (extracted from tools/delegate.py
for the module-size gate — same logic, same names, no behavioural change).

The nanny asks the ENGINE's own artifacts what a delegated run actually ran under
— the applied access profile and the applied scoped HOME — and compares them with
what the host's authority entitled the child to. A recorded WIDER profile or an
unexcused operator-home landing is a containment breach the nanny cancels on; an
absent fact stays absence ("unproven"), reported by the evidence reader instead of
enforced as a fault.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ouroboros import delegate_custody as custody
from ouroboros.utils import resolve_path_allow_missing

_TERMINAL_STATES = custody.TERMINAL_STATES

# Ordering, not a set: honest verification needs to tell "narrower than asked" (fine)
# from "wider than asked" (a refusal). An unlisted profile ranks above everything
# known, so a profile added by a future engine is treated as widening, not ignored.
_ACCESS_RANK: Dict[str, int] = {
    "readonly": 0,
    "workspace_write": 1,
    "full": 2,
    "external_sandbox_full": 2,
    "inherit_native": 3,
}
_ACCESS_UNVERIFIED = "access_unverified"
_UNKNOWN_ACCESS_RANK = 99


def _resolved(path: Any) -> Optional[pathlib.Path]:
    try:
        return (
            resolve_path_allow_missing(pathlib.Path(str(path)))
            if str(path or "").strip()
            else None
        )
    except (OSError, ValueError, RuntimeError, TypeError):
        return None


def _widened_access(detail: Dict[str, Any], expected: str) -> str:
    """The effective profile when the engine ran WIDER than the host asked, else ''.

    Claudexor derives effective access itself, so the request is a request. Honest
    verification means reading back what was enforced instead of trusting the echo; a
    narrower effective profile is fine, a wider one is a containment breach.
    """
    summary = custody.summary_of(detail)
    # `access` is NOT an independent witness: the daemon computes it as
    # `effectiveAccess ?? the client's own parsed request`, so falling back to it compares
    # our request against itself and always passes. Only the derived field testifies.
    effective = str(summary.get("effectiveAccess") or "")
    state = str(summary.get("state") or "")
    # The journal cursor is the honest "has this run produced anything yet" signal: a
    # freshly dequeued run has not written its contract, so it cannot have disclosed.
    # It lives on the run DETAIL, not on the summary — reading it from the summary made
    # this whole branch unreachable against the real wire shape while its test, whose
    # fixture put it in the summary, went on asserting the gate worked.
    try:
        seq_seen = int(detail.get("lastSeq") or 0) > 0
    except (TypeError, ValueError):
        seq_seen = False
    if not effective:
        # An UNDISCLOSED profile is not a verified narrow one — but absence only means
        # "no evidence" while the run can still act. The daemon marks a run `running` at
        # DEQUEUE, before the orchestrator writes the contract that derives the profile,
        # and a run that failed or was cancelled before that write never has one at all.
        # Treating those as breaches cancelled healthy runs and reported a failed start
        # as a containment fault. Judge only a run that is admitted, past its first
        # disclosure, and not already over.
        if state in ("", "queued") or state in _TERMINAL_STATES:
            return ""
        if not seq_seen:
            return ""
        return _ACCESS_UNVERIFIED
    if _ACCESS_RANK.get(effective, _UNKNOWN_ACCESS_RANK) > _ACCESS_RANK.get(expected, 0):
        return effective
    return ""


@dataclass(frozen=True)
class _Breach:
    """A containment guarantee the ENGINE did not deliver. Typed, with its evidence."""

    code: str
    detail: str
    facts: Dict[str, Any]


def _home_isolation_breach(detail: Dict[str, Any]) -> Optional[_Breach]:
    """Did the scoped harness HOME the host ASKED for actually get APPLIED?

    Asking is not evidence: ``execution.delegated`` is a request, and a request that
    the engine accepted and then did not honour leaves the harness holding the
    operator's real ``$HOME`` — with ``~/.claudexor/v3/daemon/token`` in it, which
    grants the entire ``/v2`` control API. Claudexor records the APPLIED fact on each
    attempt (``harness_home_isolated`` / ``harness_home_dir``) and projects it onto no
    ``/v2`` response, so the artifact is the only witness there is.

    A FAULT NEEDS A FACT, and the fact can legitimately be absent. Current Claudexor
    spreads the applied facts into ``attemptFailureRecord`` too, so an errored attempt
    usually carries them — but ``harness_home_isolated`` is the one optional member, left
    out when the attempt died before its home was decided, and an older engine wrote no
    HOME fields on a failure record at all. "a01 errored, a02 repaired it" is the ordinary
    path of the converge loop an ``agent`` run takes, so reading a missing fact as a fault
    cancels a
    correctly confined, finished, successful run and tells the nanny an ordinary harness
    failure was a containment fault it must not retry. That is the same line
    ``_widened_access`` draws for an undisclosed access profile: absence of evidence is
    not evidence. What is UNPROVEN is reported as unproven, by ``_containment_evidence``
    — it is not enforced as a breach.

    A MISSING OS BOUNDARY IS NOT A BREACH EITHER, and that is a decision rather than an
    omission. The engine applies one only where it has a mechanism for the host, so
    faulting on its absence would refuse the lane on every host that has none — cutting
    a capability to avoid a risk the child already carries, since it holds a shell in
    this worktree either way. It is disclosed instead, in all three places
    (AGENTS.md "Disclose instead of forbid"). Only a recorded FALSE stays a fault.
    """
    from ouroboros.gateways.claudexor import attempt_containment, operator_home

    run_dir = str(custody.summary_of(detail).get("runDir") or "")
    if not run_dir:
        return None
    attempts = attempt_containment(run_dir)
    if not attempts:
        return None
    real_home = _resolved(operator_home())
    for attempt in attempts:
        if attempt.home_isolated is None:
            continue
        applied = (_resolved(attempt.home_dir)
                   if attempt.home_isolated and attempt.home_dir else None)
        # A scoped home NESTED under $HOME with a PROVEN OS boundary on the same
        # attempt is the engine's own layout, not a breach: it roots every scoped
        # home under its runtime dir, which lives under $HOME on every supported
        # host, so the any-depth spatial test alone refused every real delegated
        # agent run (2026-08-07: a seatbelt-confined attempt whose verified DENIED
        # path was the daemon token directory was cancelled). Without the proven
        # boundary the spatial rule stands, and EQUALITY is never excused — an
        # "isolated" home naming the operator's own verbatim is the lie itself.
        inside = real_home is not None and _inside_operator_home(applied, real_home) \
            if applied is not None else False
        proven_nested = bool(attempt.boundary_mechanism) and applied != real_home
        if applied is None or (inside and not proven_nested):
            return _Breach(
                "home_isolation_not_applied",
                "The delegated run asked for a scoped harness HOME and the engine ran "
                "it in the operator's own home instead, where the Claudexor daemon "
                "token grants the entire control API.",
                {"attempt_id": attempt.attempt_id, "harness_home_dir": attempt.home_dir,
                 "harness_home_isolated": attempt.home_isolated},
            )
    return None


def _inside_operator_home(applied: pathlib.Path, real_home: pathlib.Path) -> bool:
    """Did the 'scoped' HOME land in the operator's own home, at any depth?

    Equality alone was the whole check, so ``$HOME/tmp/harness`` passed as isolated —
    while ``docs/DELEGATED_ADMISSION.md`` §8 calls that a breach, and rightly:
    ``~/.claudexor/v3/daemon/token`` stays reachable by a relative walk, which is the
    entire /v2 control API. Both resolved, so a symlink cannot launder it.
    """
    return applied == real_home or real_home in applied.parents

