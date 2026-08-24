"""What a running task is doing RIGHT NOW, for the owner-facing progress ticker.

The chat already renders progress lines (``agent._emit_progress`` -> a
``send_message`` with ``is_progress``, collapsed into the live card by the web
UI), but every one of those lines is emitted at a DECISION point: a fallback, a
checkpoint, a review verdict, an injected reminder. Nothing speaks while the
loop is simply blocked, so the whole span between two decisions is silent to the
owner. Measured on a local 27B route that is minutes per round: the owner saw a
started task, then nothing at all until the round finished or timed out.

This module is the missing fact source. The loop STAMPS its current phase here
(no I/O, no allocation worth counting), and the agent's existing 30s heartbeat
thread RENDERS the stamp into a progress line when the task has otherwise been
quiet. Two deliberate properties:

* The ticker is a silence filler, not a metronome. A task emitting real progress
  is already telling the owner what it is doing, and a fixed-interval line on top
  of that is noise — so the caller gates on time since the LAST progress line.
* A stamp is a FACT about the current phase, never a guess. An unstamped task
  renders nothing at all rather than an invented "still working".

Keyed by task id, so a parent and its in-process children never overwrite each
other's phase, and dropped on task exit.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

# Phases the loop stamps. Each renders its own clause; an unknown phase falls
# back to its own name so a new call site is never silently invisible.
PHASE_MODEL = "model"
PHASE_TOOL = "tool"
PHASE_REVIEW = "review"
PHASE_CHILDREN = "children"
PHASE_COMPACTION = "compaction"


@dataclass
class Activity:
    """One task's current phase, plus the round facts worth repeating."""

    phase: str
    detail: str = ""
    round_idx: int = 0
    max_rounds: int = 0
    model: str = ""
    started_ts: float = field(default_factory=time.time)

    def elapsed_sec(self) -> float:
        return max(0.0, time.time() - self.started_ts)


_lock = threading.Lock()
_activities: Dict[str, Activity] = {}


def mark(
    task_id: str,
    phase: str,
    *,
    detail: str = "",
    round_idx: int = 0,
    max_rounds: int = 0,
    model: str = "",
) -> None:
    """Record that *task_id* just entered *phase*.

    Re-stamping the SAME phase with the same detail keeps the original start
    time, so "waiting on the model" reports how long the wait has actually run
    rather than resetting to zero on every bookkeeping call. Omitted round/model
    facts inherit the previous stamp instead of blanking it — a tool stamp should
    not erase which round it belongs to."""
    key = str(task_id or "").strip()
    if not key:
        return
    with _lock:
        previous = _activities.get(key)
        unchanged = (
            previous is not None
            and previous.phase == phase
            and previous.detail == detail
        )
        _activities[key] = Activity(
            phase=phase,
            detail=detail,
            round_idx=round_idx or (previous.round_idx if previous else 0),
            max_rounds=max_rounds or (previous.max_rounds if previous else 0),
            model=model or (previous.model if previous else ""),
            started_ts=previous.started_ts if unchanged and previous else time.time(),
        )


def snapshot(task_id: str) -> Optional[Activity]:
    with _lock:
        return _activities.get(str(task_id or "").strip())


def clear(task_id: str) -> None:
    with _lock:
        _activities.pop(str(task_id or "").strip(), None)


def format_duration(seconds: float) -> str:
    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def render(task_id: str, *, extra: str = "") -> str:
    """Human status line for *task_id*, or "" when nothing was ever stamped.

    ``extra`` is the caller's own already-formatted clause (the live child
    summary, which needs a drive read this module deliberately does not do)."""
    activity = snapshot(task_id)
    if activity is None:
        return ""
    elapsed = format_duration(activity.elapsed_sec())
    if activity.phase == PHASE_MODEL:
        head = f"waiting on {activity.model or 'the model'}"
    elif activity.phase == PHASE_TOOL:
        head = f"running {activity.detail or 'a tool'}"
    elif activity.phase == PHASE_REVIEW:
        head = f"review: {activity.detail}" if activity.detail else "review in progress"
    elif activity.phase == PHASE_CHILDREN:
        head = activity.detail or "waiting on subagents"
    elif activity.phase == PHASE_COMPACTION:
        head = "compacting context"
    else:
        head = activity.detail or activity.phase
    parts = [f"{head} — {elapsed}"]
    if activity.round_idx and activity.max_rounds:
        parts.append(f"round {activity.round_idx}/{activity.max_rounds}")
    if extra:
        parts.append(extra)
    return " · ".join(parts)


# The heartbeat's own tick, moved out of `agent.py` under the size ratchet.
# It belongs here: everything it does is this module's business — read the
# phase, decide whether the silence is long enough to be worth breaking, and
# render one line. The agent supplies only what it alone knows (how to emit,
# and when it last spoke).


def live_children_clause(task_id: str, *, metadata: Any, drive_root: Any) -> str:
    """``N of M subagents still running`` for the ticker, or "" when there are none.

    Read from the SAME status root ``get_task_result`` uses, so a forked child
    drive is not mistaken for an empty subtree. Never raises: a status line is
    not worth failing a heartbeat tick over."""
    try:
        import pathlib as _pathlib

        from ouroboros.task_status import FINAL_STATUSES, find_child_tasks

        meta = metadata if isinstance(metadata, dict) else {}
        status_root = _pathlib.Path(
            str(meta.get("budget_drive_root") or "") or str(drive_root)
        )
        children = [
            row for row in find_child_tasks(
                status_root,
                parent_task_id=task_id,
                root_task_id=str(meta.get("root_task_id") or task_id),
                exclude_task_id=task_id,
                scope="direct",
                materialize_artifacts=False,
            )
            if isinstance(row, dict)
        ]
        live = [
            child for child in children
            if str(child.get("status") or "") not in FINAL_STATUSES
        ]
        if not live:
            return ""
        return f"{len(live)} of {len(children)} subagents still running"
    except Exception:
        log.debug("Live-children clause unavailable for the progress ticker", exc_info=True)
        return ""


def emit_tick(task_id: str, *, emit: Any, quiet_since: Any,
              metadata: Any = None, drive_root: Any = None) -> None:
    """Emit the current phase when the task has gone quiet for the ticker window.

    Gated on time since the LAST progress line, not on a fixed schedule: a task
    already narrating itself (tool results, review verdicts, fallbacks) does not
    need a second voice on top. An unstamped task renders "" and stays silent
    rather than inventing a reassuring "still working". Swallows its own
    failures so a heartbeat thread never dies of a status line."""
    try:
        window = get_progress_ticker_sec()
        if window <= 0:
            return
        if time.time() - float(quiet_since or 0.0) < window:
            return
        line = render(task_id, extra=live_children_clause(
            task_id, metadata=metadata, drive_root=drive_root))
        if line:
            emit(line)
    except Exception:
        log.debug("Progress ticker failed", exc_info=True)


# The ticker's own silence window. Default in config, reader here — the
# module that decides when to break a silence should be the one that knows
# how long a silence has to be.
def get_progress_ticker_sec() -> float:
    """Silence window before the heartbeat thread emits a task's current phase.

    Returns 0.0 when the ticker is disabled. Not clamped to a floor above zero:
    the disable value has to survive, and the heartbeat tick interval already
    bounds how often this can actually fire."""
    from ouroboros.config import _clamped_number_setting

    value = _clamped_number_setting("OUROBOROS_PROGRESS_TICKER_SEC", low=0.0, high=3600.0)
    return 0.0 if value <= 0 else max(value, 15.0)
