"""One-writer-per-WORKING-FOLDER lease helpers (multi-project, v6.32.0).

Pure functions consumed by ``supervisor/workers.py::assign_tasks`` under the
queue lock: a PENDING task whose LANE is already RUNNING is skipped this
assignment pass (a folder serializes internally; parallelism happens between
lanes and via subagent swarms WITHIN a task).

The lane key is the FOLDER whenever a task names one: a task carrying a
``workspace_root`` keys on ``("", root)``, so every writer in that folder
serializes REGARDLESS of which project or thread it belongs to. Two threads of
one project in the same folder share the key; two different PROJECTS attached to
the same folder share it too — which the earlier ``(project_id, workspace_root)``
key did not deliver, because folder exclusivity held only within one project
while the docstring claimed "one folder is one writer lane" (T0R2-5). A thread
branched off into its own git worktree names a different root and therefore runs
concurrently, which is the whole point of branching off.

A task that names NO folder keys on ``(project_id, "")``. That is honestly
narrower: nothing at this layer knows which folder such a task will write in
(the lane is a PURE function read under the queue lock and may never touch the
registry or the filesystem), so it serializes within its project only. The UI is
obliged to send ``workspace_root`` for project work (T2-8); a task that omits it
gets project-scoped serialization, not folder-scoped.

``project_id == ""`` means "no lane": ordinary unscoped tasks never serialize
against each other. Subagents carry their parent's stored ``project_id`` but
hold no lease of their own — the parent task IS the folder's writer and its
swarm must not deadlock against itself, so only top-level (non-subagent)
tasks count as lane occupants.

A task's lane is PINNED onto its record when it enters RUNNING
(:func:`pin_task_lane`) and read back from there afterwards. Deriving it on
demand meant a mid-run mutation of the task record — the post-hoc project
conversion is the live one — silently moved a running writer to a different
lane: it released the lane it actually held and admitted a second writer into
the same folder (T0R2-7). The pin is WRITE-ONCE: acquiring a lane a task never
had is a deliberate act, drifting out of one it holds is not.

``running_project_ids`` remains as the project-WIDE activity query (is anything
running anywhere in this project?), which merge/remove preconditions need. It
is deliberately NOT the lease key — do not reintroduce it as one.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, Iterable, Optional, Set, Tuple

# A lane key. Either ("", normalized workspace_root) — a FOLDER lane, held by
# every writer that named that folder — or (project_id, "") for a project-scoped
# task that named no folder at all.
LaneKey = Tuple[str, str]

#: The task field carrying the lane pinned at the RUNNING transition. Stored as
#: a plain list so it survives the JSON queue snapshot unchanged.
LANE_PIN_FIELD = "_lane_key"

#: Platforms whose default filesystem is case-INSENSITIVE. ``os.path.normcase``
#: lowercases on win32 only, so on macOS ``/Users/x/Repo`` and ``/Users/x/repo``
#: — the SAME folder — produced two lanes and admitted two writers onto it,
#: which is precisely what the lane exists to prevent (T0R2-4).
_CASE_INSENSITIVE_PLATFORMS = ("darwin", "win32")


def _as_task(item: Any) -> Any:
    """Unwrap the supervisor RUNNING meta shape ({"task": {...}, ...}) to the
    task dict; pass a bare task dict through unchanged."""
    if isinstance(item, dict) and isinstance(item.get("task"), dict):
        return item["task"]
    return item


def _task_project_id(task: Any) -> str:
    task = _as_task(task)
    if not isinstance(task, dict):
        return ""
    return str(task.get("project_id") or "").strip()


def _task_workspace_root(task: Any) -> str:
    """The folder a task writes in, normalized for comparison.

    Read from the task record first and then from its ``metadata`` mirror —
    both carriers exist in the queue (``_is_workspace_task_record`` reads the
    same pair). Normalization is PURE (``normpath``/``normcase``, plus a
    ``casefold`` on case-insensitive platforms): this runs under the queue lock
    on every assignment pass, so it must never touch the filesystem to resolve
    symlinks or ask the OS what a path really is.
    """
    task = _as_task(task)
    if not isinstance(task, dict):
        return ""
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    raw = str(task.get("workspace_root") or metadata.get("workspace_root") or "").strip()
    if not raw:
        return ""
    normalized = os.path.normcase(os.path.normpath(raw))
    if sys.platform in _CASE_INSENSITIVE_PLATFORMS:
        # normcase already lowercases on win32; on darwin it is a no-op and the
        # lane would otherwise split one folder into two by capitalization alone.
        normalized = normalized.casefold()
    return normalized


def _computed_lane(task: Any) -> LaneKey:
    """The lane a task's CURRENT fields describe (before any pin is consulted)."""
    root = _task_workspace_root(task)
    # A named folder is the lane, across projects and threads alike; a task that
    # named none can only be serialized against its own project.
    return ("", root) if root else (_task_project_id(task), "")


def _pinned_lane(task: Any) -> Optional[LaneKey]:
    task = _as_task(task)
    if not isinstance(task, dict):
        return None
    raw = task.get(LANE_PIN_FIELD)
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return (str(raw[0]), str(raw[1]))
    return None


def pin_task_lane(task: Any) -> Optional[LaneKey]:
    """Freeze a task's lane onto its record at the RUNNING transition (T0R2-7).

    WRITE-ONCE, and only for a task that actually occupies a lane. A running
    writer must never drift out of the lane it holds because its record was
    edited underneath it — that releases the folder while the writer is still in
    it. A task that was NOT a lane occupant and later becomes one (the post-hoc
    project conversion) has nothing to drift out of, so it pins then instead.

    The caller MUST hold the queue lock. Returns the pinned key, or ``None`` when
    the task holds no lane.
    """
    task = _as_task(task)
    if not isinstance(task, dict):
        return None
    existing = _pinned_lane(task)
    if existing is not None:
        return existing
    if not _is_lane_occupant(task):
        return None
    lane = _computed_lane(task)
    task[LANE_PIN_FIELD] = list(lane)
    return lane


def _task_lane(task: Any) -> LaneKey:
    """The lane a task holds: the pinned key if it has one, else the computed one.

    PENDING candidates have no pin yet — they are compared by what they describe.
    """
    return _pinned_lane(task) or _computed_lane(task)


def _is_lane_occupant(task: Any) -> bool:
    """Top-level project-scoped tasks occupy the lane; subagents do not."""
    task = _as_task(task)
    if not isinstance(task, dict):
        return False
    if str(task.get("delegation_role") or "") == "subagent":
        return False
    return bool(_task_project_id(task))


def running_project_lanes(running: Iterable[Any]) -> Set[LaneKey]:
    """Writer lanes currently held: ``{(project_id, workspace_root), ...}``.

    ``running`` is the supervisor's RUNNING mapping values (or any iterable of
    task dicts); read under the queue lock by the caller.
    """
    out: Set[LaneKey] = set()
    for task in running or ():
        if _is_lane_occupant(task):
            out.add(_task_lane(task))
    return out


def running_project_ids(running: Iterable[Any]) -> Set[str]:
    """Project ids with ANY running writer — the project-WIDE activity query.

    NOT the lease key (that is :func:`running_project_lanes`). This answers
    "is anything running anywhere in this project?", which is the precondition
    a merge-back or a worktree removal needs: those touch the project as a
    whole, not one folder.
    """
    out: Set[str] = set()
    for task in running or ():
        if _is_lane_occupant(task):
            out.add(_task_project_id(task))
    return out


def candidate_is_leasable(candidate: Dict[str, Any], running_lanes: Set[LaneKey]) -> bool:
    """True when ``candidate`` may be assigned now under the one-writer rule.

    ``running_lanes`` MUST come from :func:`running_project_lanes`. A set of
    bare project ids would never match a lane tuple, so every candidate would
    read as leasable and TWO writers could enter one folder — a silent
    data-corruption path. Misuse raises instead.
    """
    if not _is_lane_occupant(candidate):
        return True
    for lane in running_lanes or ():
        if not (isinstance(lane, tuple) and len(lane) == 2):
            raise TypeError(
                "candidate_is_leasable expects lane keys from running_project_lanes "
                f"((project_id, workspace_root) tuples), got {lane!r}"
            )
    return _task_lane(candidate) not in running_lanes


def mark_task_project(running: Any, pending: Any, tid: Any, pid: Any) -> bool:
    """Set a task's ``project_id`` wherever it currently lives in the supervisor queue
    state — the live RUNNING map (``{tid: {"task": {...}}}``) AND the PENDING list (bare
    task dicts) — so a POST-HOC project conversion/scope makes it a one-writer lane
    occupant whether it has started yet or not. The lease + assignment read
    ``task['project_id']`` from these IN-MEMORY structures (assign_tasks checks the
    pending candidate's own dict, then copies it into RUNNING), NOT the durable bindings —
    so a converted PENDING task that is only bound durably would still start unscoped and
    miss its lane. This is the SSOT for both post-hoc convert paths — the supervisor
    in-task ``ensure_project_scope`` and the UI ``api_project_from_task`` — so they cannot
    drift apart again. The caller MUST hold the queue lock. Returns True if any in-memory
    task dict was updated; a no-op (False) when the task is neither running nor pending
    (then the durable bind alone is correct — there is no live lane to occupy).

    A RUNNING task converted here ACQUIRES a lane it did not hold, so its lane is
    pinned on the spot. That is the one case the write-once pin admits: it takes a
    lane rather than drifting out of one, and leaving it unpinned would let a
    registry edit later move a live writer off the folder it is writing in."""
    key = str(tid or "")
    project = str(pid or "").strip()
    if not key or not project:
        return False
    updated = False
    meta = running.get(key) if hasattr(running, "get") else None
    rtask = _as_task(meta) if isinstance(meta, dict) else None
    if isinstance(rtask, dict):
        rtask["project_id"] = project
        pin_task_lane(rtask)
        updated = True
    for item in (pending or ()):
        ptask = _as_task(item)
        if isinstance(ptask, dict) and str(ptask.get("id") or "") == key:
            ptask["project_id"] = project
            updated = True
    return updated


__all__ = [
    "LANE_PIN_FIELD",
    "LaneKey",
    "candidate_is_leasable",
    "mark_task_project",
    "pin_task_lane",
    "running_project_ids",
    "running_project_lanes",
]
