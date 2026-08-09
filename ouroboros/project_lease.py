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

**An absent workspace_root is not a second folder.** Only the promote/room path
stamps ``workspace_root`` on the task record; a task scoped POST-HOC through
:func:`mark_task_project` (the SSOT behind both the supervisor's in-task
``ensure_project_scope`` and the UI's ``api_project_from_task``) carries the
project id and nothing else. Comparing the raw field would split ONE project
folder into two lanes — ``(pid, "/w/alpha")`` and ``(pid, "")`` — and let two
top-level writers into it concurrently, which is strictly worse than the
project-wide lease this key replaced. So an empty ``workspace_root`` resolves
to the project's REGISTERED ``working_dir`` (supplied by the caller, see
below), and when even that is unknown the lane is a WILDCARD that conflicts
with every lane of the same project. Fail-safe: an unknown folder queues, it
never runs parallel by accident. An explicitly workspace-less task
(``workspace="none"``) therefore still serializes against its project's folder
— deliberate; "I write nowhere" is not a claim this module can verify.

**Purity.** These functions run under the supervisor queue lock on every
assignment pass, so they NEVER touch the filesystem: normalization here is
``normpath`` + ``normcase`` only. ``normcase`` is a NO-OP on POSIX (it matters
on case-insensitive Windows/macOS spellings); SYMLINK resolution happens at
RECORD-WRITE time instead — ``workspace_admission.validate_workspace_root``
resolves the path before it is stored on a task record, and
``projects_registry.create_project``/``update_project`` resolve ``working_dir``
before storing it. Both carriers therefore arrive here already realpath'd, and
the caller's project->folder map needs no FS access to build.

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
from typing import Any, Dict, Iterable, Mapping, Optional, Set, Tuple

# A lane key: (project_id, normalized workspace_root). WILDCARD_WORKSPACE means
# "this task's folder is unknown" — it conflicts with EVERY lane of the same
# project rather than quietly becoming a lane of its own.
LaneKey = Tuple[str, str]

WILDCARD_WORKSPACE = "*"


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


def _normalized(raw: Any) -> str:
    """Pure comparison spelling of a path: ``normcase(normpath(...))``.

    NO filesystem access (see the module docstring): ``normcase`` is a no-op on
    POSIX and symlinks were already resolved when the value was WRITTEN.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(text))


def _task_workspace_root(task: Any) -> str:
    """The folder a task RECORD claims, normalized — ``""`` when it claims none.

    Read from the task record first and then from its ``metadata`` mirror —
    both carriers exist in the queue (``_is_workspace_task_record`` reads the
    same pair). This is the raw claim; :func:`_task_lane` is what turns an
    absent claim into the project's folder or a wildcard.
    """
    task = _as_task(task)
    if not isinstance(task, dict):
        return ""
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    return _normalized(task.get("workspace_root") or metadata.get("workspace_root"))


def _task_lane(
    task: Any, project_workspaces: Optional[Mapping[str, str]] = None
) -> LaneKey:
    """The lane a task occupies.

    ``project_workspaces`` maps ``project_id -> registered working_dir`` and is
    supplied by the CALLER (the supervisor reads it from the registry; this
    module stays FS-free). A task whose record carries no ``workspace_root``
    resolves to its project's folder through that map; with no answer there
    either, the lane is :data:`WILDCARD_WORKSPACE` and conflicts with every
    lane of the same project.
    """
    pid = _task_project_id(task)
    if not pid:
        return ("", "")
    workspace = _task_workspace_root(task)
    if not workspace and project_workspaces:
        workspace = _normalized(project_workspaces.get(pid))
    return (pid, workspace or WILDCARD_WORKSPACE)


def _lanes_conflict(left: LaneKey, right: LaneKey) -> bool:
    """Two lanes conflict when they are the same project AND the same folder —
    or either folder is unknown (wildcard), which is never assumed disjoint."""
    if left[0] != right[0]:
        return False
    if WILDCARD_WORKSPACE in (left[1], right[1]):
        return True
    return left[1] == right[1]


def _is_lane_occupant(task: Any) -> bool:
    """Top-level project-scoped tasks occupy the lane; subagents do not."""
    task = _as_task(task)
    if not isinstance(task, dict):
        return False
    if str(task.get("delegation_role") or "") == "subagent":
        return False
    return bool(_task_project_id(task))


def running_project_lanes(
    running: Iterable[Any], project_workspaces: Optional[Mapping[str, str]] = None
) -> Set[LaneKey]:
    """Writer lanes currently held: ``{(project_id, workspace_root), ...}``.

    ``running`` is the supervisor's RUNNING mapping values (or any iterable of
    task dicts); read under the queue lock by the caller.
    ``project_workspaces`` (``project_id -> registered working_dir``) resolves
    tasks whose record carries no workspace of its own — see :func:`_task_lane`.
    """
    out: Set[LaneKey] = set()
    for task in running or ():
        if _is_lane_occupant(task):
            out.add(_task_lane(task, project_workspaces))
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


def candidate_is_leasable(
    candidate: Dict[str, Any],
    running_lanes: Set[LaneKey],
    project_workspaces: Optional[Mapping[str, str]] = None,
) -> bool:
    """True when ``candidate`` may be assigned now under the one-writer rule.

    ``running_lanes`` MUST come from :func:`running_project_lanes`. A set of
    bare project ids would never match a lane tuple, so every candidate would
    read as leasable and TWO writers could enter one folder — a silent
    data-corruption path. Misuse raises instead, and the shape check runs
    BEFORE the unscoped-candidate short-circuit: otherwise a caller passing
    bare project ids would be told nothing at all as long as the first
    candidates happened to be unscoped, and would learn about it only when a
    project task finally slipped through.
    """
    for lane in running_lanes or ():
        if not (isinstance(lane, tuple) and len(lane) == 2):
            raise TypeError(
                "candidate_is_leasable expects lane keys from running_project_lanes "
                f"((project_id, workspace_root) tuples), got {lane!r}"
            )
    if not _is_lane_occupant(candidate):
        return True
    lane = _task_lane(candidate, project_workspaces)
    return not any(_lanes_conflict(lane, held) for held in running_lanes or ())


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

    It deliberately stamps the project id ALONE: this module has no filesystem
    access and the task may not write in the project's folder at all. Resolving
    that task's LANE is :func:`_task_lane`'s job — an absent ``workspace_root``
    becomes the project's registered ``working_dir``, or a wildcard conflicting
    with every lane of the project. Do NOT "fix" this by guessing a folder
    here."""
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
    "WILDCARD_WORKSPACE",
    "candidate_is_leasable",
    "mark_task_project",
    "running_project_ids",
    "running_project_lanes",
]
