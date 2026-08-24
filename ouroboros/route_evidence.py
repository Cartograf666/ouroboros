"""What each execution target has actually DONE here — cost, time, outcome.

The owner's per-task allocation is only as good as the facts behind it. "Codex
is faster" and "the local model is free" are guesses until this install has
watched both do the work; this is where that watching accumulates, one folded
observation per finished task.

AN AGGREGATE, NOT A QUERY. The raw material — usage rows, task-eval events —
is garbage-collected after ``OUROBOROS_GC_RETENTION_DAYS`` (7 by default), so a
projection that recomputed on demand would quietly forget everything older than
a week and call that "no evidence". Each finished task is folded in once, at the
moment all three of its facts are known, and the aggregate is what survives.

FACTS ONLY — this module ranks nothing and recommends nothing. It answers "what
happened", and the LLM reading the digest decides what that means for the work
in front of it (BIBLE P5: no if-else for behavior selection). A scoring function
here would be a routing policy in Python wearing a statistics costume, and the
first thing it would do is disagree with the model that has the actual context.

Modeled on ``capability_evidence``: fingerprinted per route, sourced, aged, and
fail-soft on every path — an unwritten observation must never disturb a task
that has already finished its work.
"""

from __future__ import annotations

import logging
import pathlib
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ouroboros.deadline_utils import parse_deadline_ts, utc_now
from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso

log = logging.getLogger(__name__)

_STORE_LOCK = threading.RLock()

# How many observations back the aggregate remembers per route. Old enough to
# smooth a single unlucky run, short enough that a route which improved (a new
# model, a fixed adapter) is not judged forever on how it used to behave.
_WINDOW = 25
# A route nobody has used in this long is reported as STALE rather than dropped:
# "we have not tried it lately" is a different fact from "we have no evidence",
# and the second is what deleting the row would claim.
_STALE_AFTER_DAYS = 30.0

# The review lexicon (`ouroboros.outcomes._OUTCOME_TIERS`). A tier exists only
# where a REVIEW produced one, which is a minority of tasks — so it is recorded
# beside, never instead of, the plain did-the-execution-succeed fact. Treating an
# unreviewed success as `solved` would put a verdict nobody reached on the record.
_TIER_KEYS = ("solved", "best_effort", "blocked_with_evidence")


def _store_path(drive_root: Any) -> pathlib.Path:
    return pathlib.Path(drive_root) / "state" / "route_evidence.json"


def route_fingerprint(kind: str, target_id: str, model: str = "") -> str:
    """The identity an observation is filed under.

    The MODEL is part of it: the same harness running a small model and a large
    one are two different answers to "how fast, how much", and merging them
    would produce an average describing neither.
    """
    parts = [str(kind or "").strip().lower(), str(target_id or "").strip()]
    model = str(model or "").strip()
    if model:
        parts.append(model)
    return "|".join(parts)


@dataclass(frozen=True)
class RouteStats:
    """One route's accumulated record. Every field is an observation, not a score."""

    fingerprint: str
    kind: str
    target_id: str
    model: str = ""
    samples: int = 0
    ok_samples: int = 0
    median_duration_sec: Optional[float] = None
    median_cost_usd: Optional[float] = None
    cost_known_samples: int = 0
    tiers: Dict[str, int] = None  # type: ignore[assignment]
    last_seen: str = ""

    @property
    def stale(self) -> bool:
        parsed = parse_deadline_ts(self.last_seen)
        if parsed is None:
            return True
        return (utc_now() - parsed).total_seconds() > _STALE_AFTER_DAYS * 86400


def _median(values: List[float]) -> Optional[float]:
    """The middle observation, or None when there are none.

    Median, not mean: one delegated run that hung for an hour before failing
    would drag a mean far enough to misrepresent every other run on that route,
    and the tail is exactly the kind of thing a single outlier produces.
    """
    rows = sorted(float(v) for v in values if v is not None)
    if not rows:
        return None
    middle = len(rows) // 2
    if len(rows) % 2:
        return rows[middle]
    return (rows[middle - 1] + rows[middle]) / 2.0


def _load(drive_root: Any) -> Dict[str, Any]:
    data = read_json_dict(_store_path(drive_root))
    if isinstance(data, dict) and isinstance(data.get("routes"), dict):
        return data
    return {"routes": {}}


def _save(drive_root: Any, data: Dict[str, Any]) -> None:
    path = _store_path(drive_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, data)
    except OSError:
        log.debug("route evidence save failed", exc_info=True)


def record_route_outcome(
    drive_root: Any,
    *,
    kind: str,
    target_id: str,
    model: str = "",
    task_id: str = "",
    duration_sec: Optional[float] = None,
    cost_usd: Optional[float] = None,
    ok: bool = True,
    outcome_tier: str = "",
) -> None:
    """Fold ONE finished task into its route's record. Never raises.

    Idempotent per task: a replayed settle (a restart re-finalizing the same
    task) must not double-count a route's history, so the task id rides in the
    window and a repeat overwrites its own entry instead of appending a second.

    ``cost_usd=None`` is UNKNOWN, not zero — a subscription run that disclosed
    nothing costs an unknown amount, and averaging it in as free would make
    every delegated route look cheaper than the evidence supports.
    """
    target_id = str(target_id or "").strip()
    if not target_id:
        return
    fingerprint = route_fingerprint(kind, target_id, model)
    entry = {
        "task_id": str(task_id or ""),
        "ts": utc_now_iso(),
        "duration_sec": None if duration_sec is None else max(0.0, float(duration_sec)),
        "cost_usd": None if cost_usd is None else max(0.0, float(cost_usd)),
        "ok": bool(ok),
        "tier": str(outcome_tier or "").strip().lower(),
    }
    try:
        with _STORE_LOCK:
            data = _load(drive_root)
            row = data["routes"].get(fingerprint)
            if not isinstance(row, dict):
                row = {"kind": str(kind or ""), "target_id": target_id,
                       "model": str(model or ""), "window": []}
            window = [
                item for item in row.get("window") or []
                if isinstance(item, dict)
                and not (entry["task_id"] and item.get("task_id") == entry["task_id"])
            ]
            window.append(entry)
            row["window"] = window[-_WINDOW:]
            row["last_seen"] = entry["ts"]
            data["routes"][fingerprint] = row
            _save(drive_root, data)
    except Exception:
        log.debug("route evidence fold failed for %s", fingerprint, exc_info=True)


def fold_task_outcome(
    drive_root: Any,
    task: Dict[str, Any],
    outcome_axes: Dict[str, Any],
    duration_sec: float,
    cost_fields: Dict[str, Any],
    *,
    ok: bool,
) -> None:
    """Fold ONE finished task into its execution route's record, from the record.

    Called from the finalization seam, which is the one place all three facts
    exist together: the wall clock is measured there, the cost authority has just
    been reconstructed there, and the outcome has just been derived there. It
    lives HERE rather than in the pipeline because the mapping from a task record
    to an observation is this module's business — and because the pipeline is at
    its size gate, which is the honest signal that it was the wrong home.

    Attribution follows ``actual_substrate`` — what the custody rows PROVE ran —
    not the dispatch plan. A child routed to a harness that fell back to native
    executed on metered tokens, and crediting the harness with that time would
    teach the evidence the opposite of what happened.

    Fail-soft in full: a lost observation costs a future proposal some precision,
    while an exception here would damage a task that has already done its work.
    """
    try:
        from ouroboros.reviewer_slot_config import ROUTE_KIND_API, ROUTE_KIND_SESSION
        from ouroboros.subagents import SUBSTRATE_HARNESS_USED, envelope_from_task

        substrate = str(task.get("actual_substrate") or "")
        if not substrate and str(task.get("executor_route") or "").strip():
            # Not yet stamped on the record at this point in finalization, and
            # only a route-dispatched task can have delegated evidence at all —
            # so the custody read is asked for exactly when it can answer, and a
            # native task pays nothing for it.
            substrate = str(
                envelope_from_task(task, status="completed").get("actual_substrate") or "")
        if substrate == SUBSTRATE_HARNESS_USED:
            kind, target = ROUTE_KIND_SESSION, str(task.get("executor_route") or "")
        else:
            kind, target = ROUTE_KIND_API, str(task.get("model") or "")
        review = outcome_axes.get("review") if isinstance(outcome_axes, dict) else {}
        record_route_outcome(
            drive_root,
            kind=kind,
            target_id=target,
            task_id=str(task.get("id") or ""),
            duration_sec=duration_sec,
            # UNKNOWN stays None. `cost_usd` is absent on an unavailable ledger
            # and null on an undisclosed subscription run; writing either as 0.0
            # would make the cheapest-looking route the one nobody measured.
            cost_usd=(cost_fields or {}).get("cost_usd"),
            ok=ok,
            outcome_tier=str((review or {}).get("outcome_tier") or ""),
        )
    except Exception:
        log.debug("route evidence task fold failed", exc_info=True)


def route_stats(drive_root: Any) -> List[RouteStats]:
    """Every route this install has evidence for, most recently used first."""
    data = _load(drive_root)
    rows: List[RouteStats] = []
    for fingerprint, row in (data.get("routes") or {}).items():
        if not isinstance(row, dict):
            continue
        window = [item for item in row.get("window") or [] if isinstance(item, dict)]
        if not window:
            continue
        costs = [item["cost_usd"] for item in window if item.get("cost_usd") is not None]
        tiers = {key: 0 for key in _TIER_KEYS}
        for item in window:
            tier = str(item.get("tier") or "")
            if tier in tiers:
                tiers[tier] += 1
        rows.append(RouteStats(
            fingerprint=str(fingerprint),
            kind=str(row.get("kind") or ""),
            target_id=str(row.get("target_id") or ""),
            model=str(row.get("model") or ""),
            samples=len(window),
            ok_samples=sum(1 for item in window if item.get("ok")),
            median_duration_sec=_median(
                [item["duration_sec"] for item in window
                 if item.get("duration_sec") is not None]),
            median_cost_usd=_median(costs),
            cost_known_samples=len(costs),
            tiers=tiers,
            last_seen=str(row.get("last_seen") or ""),
        ))
    rows.sort(key=lambda r: r.last_seen, reverse=True)
    return rows


def _duration_phrase(seconds: Optional[float]) -> str:
    if seconds is None:
        return "time unknown"
    if seconds < 90:
        return f"~{int(round(seconds))}s"
    return f"~{seconds / 60:.0f}m"


def _cost_phrase(stats: RouteStats) -> str:
    """What it cost, and how much of that is actually known.

    A route whose runs disclosed no spend says so. Printing `$0.00` for it would
    be the single most misleading number on the digest — it is the shape that
    makes a subscription route look free when nobody measured it.
    """
    if stats.median_cost_usd is None:
        return "cost undisclosed"
    phrase = f"${stats.median_cost_usd:.2f}"
    if stats.cost_known_samples < stats.samples:
        phrase += f" ({stats.cost_known_samples}/{stats.samples} disclosed)"
    return phrase


def format_route_evidence_digest(drive_root: Any, *, limit: int = 8) -> str:
    """The compact facts block for the agent's context, '' when there is none.

    Deliberately without a recommendation. It is the evidence the model weighs
    when it proposes an allocation — and it must be able to say "no evidence
    yet" out loud, which a row that invented a default never could.
    """
    try:
        rows = route_stats(drive_root)
    except Exception:
        log.debug("route evidence digest failed", exc_info=True)
        return ""
    if not rows:
        return ""
    lines = []
    for stats in rows[:limit]:
        name = stats.target_id + (f" · {stats.model}" if stats.model else "")
        parts = [
            _duration_phrase(stats.median_duration_sec),
            _cost_phrase(stats),
            f"{stats.ok_samples}/{stats.samples} finished cleanly",
        ]
        reviewed = sum((stats.tiers or {}).values())
        if reviewed:
            # Named separately because it is a different claim: "finished" is
            # what the execution did, "solved" is what a reviewer judged.
            parts.append(f"{(stats.tiers or {}).get('solved', 0)}/{reviewed} reviewed solved")
        if stats.stale:
            parts.append("not used lately")
        lines.append(f"- {name} ({stats.kind}): " + " · ".join(parts))
    return (
        "## Execution route evidence (median of recent runs on this machine)\n\n"
        + "\n".join(lines)
        + "\n\nFacts, not a recommendation, and only about routes already tried here — "
          "a target missing from this list has no history yet, which is worth saying "
          "plainly when you propose it."
    )


def record_task_eval(
    drive_root: Any,
    drive_logs: Any,
    task: Dict[str, Any],
    outcome_axes: Dict[str, Any],
    cost_fields: Dict[str, Any],
    *,
    reason_code: Any,
    duration_sec: float,
    n_tool_calls: int,
    n_tool_errors: int,
    response_len: int,
    loop_outcome: Dict[str, Any],
    ok: bool,
) -> None:
    """Write what one finished task cost and did — the eval row and the fold.

    Both readings come off the SAME seam, the one moment where the wall clock,
    the reconstructed cost and the outcome are all known at once, so they live
    together rather than being re-derived at two call sites that could drift.
    The eval row is the raw per-task record (GC'd on the owner's retention);
    the fold is the aggregate that has to outlive it. A failure to append the
    row must not cost the aggregate, so they fail independently.
    """
    from ouroboros.utils import append_jsonl

    try:
        append_jsonl(pathlib.Path(drive_logs) / "events.jsonl", {
            "ts": utc_now_iso(), "type": "task_eval", "ok": ok,
            "task_id": task.get("id"), "task_type": task.get("type"),
            "outcome_axes": outcome_axes,
            "reason_code": reason_code,
            "review_eligibility": str(loop_outcome.get("review_eligibility") or ""),
            "review_trigger": str(loop_outcome.get("review_trigger") or ""),
            "duration_sec": duration_sec,
            "tool_calls": n_tool_calls,
            "tool_errors": n_tool_errors,
            "response_len": response_len,
        })
    except Exception:
        log.warning("Failed to log task eval event", exc_info=True)
    fold_task_outcome(drive_root, task, outcome_axes, duration_sec, cost_fields, ok=ok)
