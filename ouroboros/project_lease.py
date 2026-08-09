"""One-writer-per-WORKING-FOLDER lease helpers (multi-project, v6.32.0).

Pure functions consumed by ``supervisor/workers.py::assign_tasks`` under the
queue lock: a PENDING task whose LANE is already RUNNING is skipped this
assignment pass (a folder serializes internally; parallelism happens between
lanes and via subagent swarms WITHIN a task).

The lane key is ``(project_id, workspace_root)``, not ``project_id`` alone.
What must not overlap is two writers in one FOLDER; two threads of the same
project working in the SAME folder still serialize (identical key), while a
thread branched off into its own git worktree gets its own key and runs
concurrently. Keying on the project alone made "branch off for parallel work"
a promise the queue could not keep.

``project_id == ""`` means "no lane": ordinary unscoped tasks never serialize
against each other. Subagents carry their parent's stored ``project_id`` but
hold no lease of their own — the parent task IS the folder's writer and its
swarm must not deadlock against itself, so only top-level (non-subagent)
tasks count as lane occupants.

``running_project_ids`` remains as the project-WIDE activity query (is anything
running anywhere in this project?), which merge/remove preconditions need. It
is deliberately NOT the lease key — do not reintroduce it as one.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, Set, Tuple

# A lane key: (project_id, normalized workspace_root). "" workspace_root means
# "the project's own folder" — every task without an explicit workspace shares
# that one lane.
LaneKey = Tuple[str, str]


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
    same pair). Normalization is PURE (``normpath``/``normcase``): this runs
    under the queue lock on every assignment pass, so it must never touch the
    filesystem to resolve symlinks.
    """
    task = _as_task(task)
    if not isinstance(task, dict):
        return ""
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    raw = str(task.get("workspace_root") or metadata.get("workspace_root") or "").strip()
    if not raw:
        return ""
    return os.path.normcase(os.path.normpath(raw))


def _task_lane(task: Any) -> LaneKey:
    return (_task_project_id(task), _task_workspace_root(task))


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

    ``running_lanes`` MUST come from :func:`running_project_lanes` — a set of
    bare project ids would silently never match a lane tuple, disabling the
    lease entirely.
    """
    if not _is_lane_occupant(candidate):
        return True
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
    (then the durable bind alone is correct — there is no live lane to occupy)."""
    key = str(tid or "")
    project = str(pid or "").strip()
    if not key or not project:
        return False
    updated = False
    meta = running.get(key) if hasattr(running, "get") else None
    rtask = _as_task(meta) if isinstance(meta, dict) else None
    if isinstance(rtask, dict):
        rtask["project_id"] = project
        updated = True
    for item in (pending or ()):
        ptask = _as_task(item)
        if isinstance(ptask, dict) and str(ptask.get("id") or "") == key:
            ptask["project_id"] = project
            updated = True
    return updated


__all__ = [
    "LaneKey",
    "candidate_is_leasable",
    "mark_task_project",
    "running_project_ids",
    "running_project_lanes",
]
