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

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

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
