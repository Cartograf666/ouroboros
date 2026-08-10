"""BRANCH OFF and MERGE BACK — the two explicit operations behind A7.

A thread's LOCATION is never a stored toggle. It is DERIVED from whether a
durable worktree exists for it (:func:`thread_location`), so there is no flag
that can disagree with the filesystem and no state machine to keep in sync. The
owner performs two operations instead:

**BRANCH OFF** provisions a worktree for the thread from a base the OWNER picks
(:func:`branch_off_bases` lists the current branch, every other branch, every
tag, and the "exactly as it is now" option) and binds it through the durable
registry in :mod:`ouroboros.thread_worktrees`. Any commit-ish the owner types is
accepted too — the list is an offer, not a restriction (A8).

"Exactly as it is now" is the only base that does not already exist as a commit,
so it is made into one: a SNAPSHOT commit on the project's current branch, using
the same shape as the attach snapshot and the coop checkpoint (local identity, no
global config touched, credential-shaped files deliberately left out and
disclosed). Reused rather than reinvented, and never silent — the resulting sha
and the skipped paths come back in the receipt.

**MERGE BACK** merges the thread's branch into the project's own checkout under
A9's preconditions: nothing running anywhere in the project (the project-WIDE
activity query, NOT the writer lane) and a clean local tree. A conflict is SHOWN
and STOPS the operation — the merge is aborted so the owner's folder is left
exactly as it was, and the thread stays in its branch with every commit intact.
Merging never removes the worktree; removal is the separate, inspected act in
:mod:`ouroboros.thread_worktrees` (A10).

Every refusal here is a TYPED reason, never a raised string: these answer owner
gestures, and a UI cannot branch on a stack trace.
"""

from __future__ import annotations

import logging
import pathlib
import subprocess
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

#: The owner-chosen base meaning "exactly as it is now, including uncommitted
#: edits". Not a git ref — it is the request for a snapshot commit (A8).
BASE_SNAPSHOT = "@snapshot"

#: Typed refusals. Every one of these is an answer to an owner gesture, so it
#: names what to do next rather than what went wrong internally.
REASON_NO_FOLDER = "no_project_folder"
REASON_FOLDER_MISSING = "folder_missing"
REASON_GIT_INIT_REQUIRED = "git_init_required"
REASON_FOLDER_UNUSABLE = "folder_unusable"
REASON_UNKNOWN_PROJECT = "unknown_project"
REASON_UNKNOWN_THREAD = "unknown_thread"
REASON_ALREADY_BRANCHED = "already_branched"
REASON_NOT_BRANCHED = "not_branched"
REASON_UNKNOWN_BASE = "unknown_base"
REASON_SNAPSHOT_FAILED = "snapshot_failed"
REASON_BRANCH_FAILED = "branch_failed"
REASON_PROJECT_BUSY = "project_busy"
REASON_LOCAL_TREE_DIRTY = "local_tree_dirty"
REASON_MERGE_CONFLICT = "merge_conflict"
REASON_MERGE_FAILED = "merge_failed"
REASON_CHECKOUT_MISSING = "checkout_missing"
REASON_THREAD_NOT_LIVE = "thread_not_live"

#: Bounded like every other owner-facing git call on a request path.
_GIT_TIMEOUT_SEC = 120


def _git(root: Any, *args: str) -> subprocess.CompletedProcess:
    """One bounded git call. A spawn failure or timeout comes back as rc=124 so
    every caller can treat "did not succeed" uniformly instead of splitting
    between an exit code and an exception."""
    from ouroboros.platform_layer import bootstrap_process_path

    bootstrap_process_path()
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SEC,
        )
    except Exception as exc:  # noqa: BLE001 — includes TimeoutExpired
        return subprocess.CompletedProcess(
            ["git", *args], 124, stdout="", stderr=f"{type(exc).__name__}: {exc}"
        )


def _detail(proc: subprocess.CompletedProcess) -> str:
    return (proc.stderr or proc.stdout or "").strip()[:500]


def _refused(reason: str, message: str, **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "reason": reason, "message": message, **extra}


def _live_thread_refusal(thread: Dict[str, Any], project_id: str) -> Optional[Dict[str, Any]]:
    """Refuse a git operation on a thread that is fenced or already gone.

    A thread being deleted is closed to routing and having its tasks cancelled;
    provisioning a checkout for it, or merging its branch, would be work on a
    room the owner has written off. An ARCHIVED thread is fine — archiving hides
    a thread, it does not close it.
    """
    from ouroboros.project_threads_registry import THREAD_ACTIVE, THREAD_ARCHIVED

    lifecycle = str(thread.get("lifecycle") or THREAD_ACTIVE)
    if lifecycle in {THREAD_ACTIVE, THREAD_ARCHIVED}:
        return None
    return _refused(
        REASON_THREAD_NOT_LIVE,
        f"This thread is {lifecycle}; it cannot branch off or merge back.",
        project_id=str(project_id), thread_id=int(thread.get("id") or 0),
    )


# --------------------------------------------------------------------------- #
# The project's PLACE
# --------------------------------------------------------------------------- #

def resolve_project_repo(drive_root: Any, project_id: str) -> Dict[str, Any]:
    """The git worktree root a project's threads branch off from, or a refusal.

    A project with no designated place cannot branch (there is nothing to branch
    FROM), and a place that is not tracked by git gets T2's typed
    ``git_init_required`` OFFER rather than an error — the same decision object
    the task-admission path returns, built by the same function, so the owner
    sees one consistent answer no matter which surface asked (A12).
    """
    from ouroboros.config import DATA_DIR, REPO_DIR
    from ouroboros.projects_registry import get_project
    from ouroboros.workspace_admission import (
        GitInitRequiredError,
        WorkspaceRootError,
        validate_workspace_root,
    )

    project = get_project(drive_root, project_id)
    if project is None:
        return _refused(REASON_UNKNOWN_PROJECT, f"unknown project: {project_id}")
    pid = str(project.get("id") or "")
    working_dir = str(project.get("working_dir") or "").strip()
    if not working_dir:
        return _refused(
            REASON_NO_FOLDER,
            "This project has no working folder yet, so there is nothing to branch "
            "off from. Give it a place first — attach a folder, clone a repo, or "
            "create one.",
            project_id=pid,
        )
    if not pathlib.Path(working_dir).expanduser().is_dir():
        return _refused(
            REASON_FOLDER_MISSING,
            f"The project's folder is gone: {working_dir}",
            project_id=pid,
            working_dir=working_dir,
        )
    try:
        root = validate_workspace_root(
            working_dir, system_repo_dir=REPO_DIR, drive_root=DATA_DIR,
        )
    except GitInitRequiredError as exc:
        decision = dict(exc.decision)
        decision["project_id"] = pid
        # Branching is one of the three things the offer already names.
        return _refused(
            REASON_GIT_INIT_REQUIRED,
            str(decision.get("message") or ""),
            project_id=pid,
            working_dir=working_dir,
            decision=decision,
        )
    except WorkspaceRootError as exc:
        return _refused(
            REASON_FOLDER_UNUSABLE, str(exc), project_id=pid, working_dir=working_dir,
        )
    return {"ok": True, "project_id": pid, "repo_dir": str(root), "project": project}


def thread_location(data_dir: Any, project_id: str, thread_id: Any) -> Dict[str, Any]:
    """WHERE a thread works — derived, never stored (A7).

    ``{"where": "project_folder"}`` or ``{"where": "worktree", ...}``. The single
    question "does a durable worktree exist for this thread" is the whole state
    machine; there is no toggle that can drift out of agreement with it.
    """
    from ouroboros.thread_worktrees import get_thread_worktree

    row = get_thread_worktree(data_dir, project_id, thread_id)
    if not row:
        return {"where": "project_folder"}
    return {
        "where": "worktree",
        "path": str(row.get("path") or ""),
        "branch": str(row.get("branch") or ""),
        "base_sha": str(row.get("base_sha") or ""),
        "created_at": str(row.get("created_at_iso") or ""),
    }


# --------------------------------------------------------------------------- #
# BRANCH OFF
# --------------------------------------------------------------------------- #

def branch_off_bases(repo_dir: Any) -> Dict[str, Any]:
    """Every base the owner may branch off from, in offer order (A8).

    The current branch first because it is the common answer, then the other
    branches, then tags. The "exactly as it is now" entry is ALWAYS present — it
    is one option in the list, not a restriction — and it discloses whether the
    tree actually has uncommitted work, so the owner can tell whether choosing it
    would create a snapshot commit or simply reuse HEAD.

    A commit-ish the owner types instead is accepted by :func:`branch_off_thread`
    and deliberately not enumerated here: listing every commit is not an offer.
    """
    root = pathlib.Path(str(repo_dir))
    head = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    current = (head.stdout or "").strip() if head.returncode == 0 else ""
    bases: List[Dict[str, Any]] = []
    seen: set = set()
    if current and current != "HEAD":
        bases.append({"ref": current, "kind": "branch", "label": f"{current} (current)"})
        seen.add(current)
    for kind, pattern in (("branch", "refs/heads"), ("tag", "refs/tags")):
        listed = _git(root, "for-each-ref", "--format=%(refname:short)", pattern)
        if listed.returncode != 0:
            continue
        for name in (listed.stdout or "").splitlines():
            name = name.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            bases.append({"ref": name, "kind": kind, "label": name})
    status = _git(root, "status", "--porcelain")
    dirty = status.returncode == 0 and bool((status.stdout or "").strip())
    return {
        "current_branch": current,
        "bases": bases,
        "snapshot": {
            "ref": BASE_SNAPSHOT,
            "kind": "snapshot",
            "label": "Exactly as it is now (including uncommitted edits)",
            "dirty": dirty,
            "creates_commit": dirty,
        },
    }


def _snapshot_commit(repo_dir: pathlib.Path, label: str) -> Dict[str, Any]:
    """Commit the project folder EXACTLY as it stands, so it can be branched from.

    Same shape as ``project_sources.attach_snapshot_init`` and the coop
    checkpoint: a local identity (the owner's global git config is never
    touched), and credential-shaped files unstaged through the ONE
    ``_sensitive_untracked_reason`` authority so a snapshot never bakes a secret
    into history. The skipped paths are returned, never swallowed.

    A clean tree needs no commit at all and simply reports the current HEAD:
    "as it is now" is already a commit in that case.
    """
    from ouroboros.project_sources import _unstage_sensitive_paths

    status = _git(repo_dir, "status", "--porcelain")
    if status.returncode != 0:
        return {"ok": False, "detail": _detail(status)}
    if not (status.stdout or "").strip():
        head = _git(repo_dir, "rev-parse", "HEAD")
        if head.returncode != 0:
            return {"ok": False, "detail": _detail(head)}
        return {"ok": True, "sha": (head.stdout or "").strip(), "created": False, "skipped_sensitive": []}
    add = _git(repo_dir, "add", "-A")
    if add.returncode != 0:
        return {"ok": False, "detail": _detail(add)}
    skipped = _unstage_sensitive_paths(repo_dir)
    commit = _git(
        repo_dir,
        "-c", "user.name=Ouroboros", "-c", "user.email=ouroboros@local",
        "commit", "-q", "-m", f"ouroboros: snapshot before branching off {label}".strip(),
    )
    if commit.returncode != 0:
        detail = _detail(commit)
        if "nothing to commit" not in detail.lower():
            return {"ok": False, "detail": detail}
    head = _git(repo_dir, "rev-parse", "HEAD")
    if head.returncode != 0:
        return {"ok": False, "detail": _detail(head)}
    return {
        "ok": True,
        "sha": (head.stdout or "").strip(),
        "created": True,
        "skipped_sensitive": skipped,
    }


def branch_off_thread(
    drive_root: Any,
    project_id: str,
    thread_id: Any,
    *,
    base_ref: str = "",
    data_dir: Optional[Any] = None,
    worktree_root: Optional[Any] = None,
) -> Dict[str, Any]:
    """Provision this thread's own checkout from an owner-chosen base (A7/A8).

    ``base_ref`` is a branch, a tag, any commit-ish, or :data:`BASE_SNAPSHOT`;
    empty means the project's current HEAD. The worktree is bound to the thread
    through the durable registry, which refuses to clobber an existing checkout
    or branch — so a second branch-off is an error the owner sees, never a silent
    reset of work they already did.
    """
    from ouroboros.projects_registry import get_thread
    from ouroboros.thread_worktrees import provision_thread_worktree

    resolved = resolve_project_repo(drive_root, project_id)
    if not resolved.get("ok"):
        return resolved
    pid = str(resolved["project_id"])
    repo_dir = pathlib.Path(str(resolved["repo_dir"]))
    data_root = data_dir if data_dir is not None else drive_root

    thread = get_thread(drive_root, pid, thread_id)
    if thread is None:
        return _refused(
            REASON_UNKNOWN_THREAD, f"unknown thread {thread_id!r} in project {pid!r}",
            project_id=pid,
        )
    not_live = _live_thread_refusal(thread, pid)
    if not_live is not None:
        return not_live
    tid = int(thread["id"])
    location = thread_location(data_root, pid, tid)
    if location["where"] == "worktree":
        return _refused(
            REASON_ALREADY_BRANCHED,
            "This thread is already working in its own branch. Merge it back or "
            "remove the checkout before branching off again.",
            project_id=pid, thread_id=tid, location=location,
        )

    wanted = str(base_ref or "").strip()
    snapshot: Dict[str, Any] = {}
    if wanted == BASE_SNAPSHOT:
        snapshot = _snapshot_commit(repo_dir, str(thread.get("name") or f"thread {tid}"))
        if not snapshot.get("ok"):
            return _refused(
                REASON_SNAPSHOT_FAILED,
                "Could not snapshot the folder as it is now: "
                f"{snapshot.get('detail') or 'git refused'}",
                project_id=pid, thread_id=tid,
            )
        base = str(snapshot["sha"])
    elif wanted:
        verified = _git(repo_dir, "rev-parse", "--verify", f"{wanted}^{{commit}}")
        if verified.returncode != 0:
            return _refused(
                REASON_UNKNOWN_BASE,
                f"{wanted!r} is not a branch, tag or commit in this repository.",
                project_id=pid, thread_id=tid, base_ref=wanted,
            )
        base = wanted
    else:
        base = ""

    try:
        handle = provision_thread_worktree(
            repo_dir=repo_dir,
            project_id=pid,
            thread_id=tid,
            base_ref=base,
            data_dir=data_root,
            worktree_root=worktree_root,
        )
    except Exception as exc:  # noqa: BLE001 — provisioning refusals are typed answers
        return _refused(
            REASON_BRANCH_FAILED, str(exc)[:500], project_id=pid, thread_id=tid,
        )
    out: Dict[str, Any] = {
        "ok": True,
        "project_id": pid,
        "thread_id": tid,
        "base_ref": wanted or "HEAD",
        "location": thread_location(data_root, pid, tid),
        "branch": handle.branch,
        "path": handle.path,
        "base_sha": handle.base_sha,
    }
    if snapshot:
        out["snapshot_commit"] = {
            "sha": str(snapshot.get("sha") or ""),
            "created": bool(snapshot.get("created")),
            "skipped_sensitive": list(snapshot.get("skipped_sensitive") or []),
        }
    return out


# --------------------------------------------------------------------------- #
# MERGE BACK
# --------------------------------------------------------------------------- #

def project_is_busy(project_id: str) -> bool:
    """Is ANY task running anywhere in this project? (A9's first precondition.)

    Reads the project-WIDE activity query, deliberately NOT the writer lane: a
    merge touches the project as a whole, so a task running in a DIFFERENT folder
    of the same project still blocks it. Fail-CLOSED — if the queue cannot be
    read, the project counts as busy, because "cannot tell" must never license a
    merge into a folder something might be writing in.
    """
    try:
        from ouroboros.project_lease import running_project_ids
        from supervisor.queue import _queue_lock
        from supervisor.workers import RUNNING

        with _queue_lock:
            running = list(RUNNING.values())
        return str(project_id) in running_project_ids(running)
    except Exception:
        log.debug("project_is_busy could not read the queue for %s", project_id, exc_info=True)
        return True


#: A14's copy, in ONE place. Every surface that tells the owner their work will
#: wait says exactly this, and says the true thing: the task is QUEUED behind the
#: running one and will run when it finishes. It is not rejected, not dropped,
#: and not silently reordered. The remedy is offered in the same breath, because
#: "you have to wait" without "here is how not to" is a dead end.
QUEUE_NOTICE = (
    "Another thread in this project is working in the same folder right now. "
    "A task you start here will be QUEUED behind it and will run as soon as that "
    "one finishes — it is not rejected. Branching this thread off gives it its "
    "own copy of the folder, so both can run at the same time."
)
#: The same fact for a thread already in its own checkout, where waiting means
#: something is running in THAT checkout — branching again would not help.
QUEUE_NOTICE_OWN_CHECKOUT = (
    "This thread already has a task running in its own checkout. A new task here "
    "will be QUEUED behind it and will run as soon as that one finishes."
)


def queue_notice(
    drive_root: Any,
    project_id: str,
    thread_id: Any,
    *,
    data_dir: Optional[Any] = None,
    running: Optional[Any] = None,
) -> Dict[str, Any]:
    """Would a task started in THIS thread wait, and what should the owner hear?

    Returns ``{queued, reason, message, remedy}``. ``remedy`` is ``branch_off``
    only when branching would actually help — a thread already working in its own
    checkout is waiting on ITSELF, and offering to branch again there would be
    advice that does not work.

    A14 exists because the earlier copy said a second thread's task was rejected.
    It never was: the writer lane SERIALIZES, it does not refuse, and telling an
    owner their work was thrown away when it is sitting in the queue is the kind
    of wrong that makes people stop trusting the queue entirely.

    Fail-OPEN, unlike the merge precondition: if the queue cannot be read this
    says nothing rather than warning about a wait that may not exist. A false
    warning here costs trust; a missing one costs a few seconds of surprise.
    """
    from ouroboros.project_lease import candidate_is_leasable, running_project_lanes

    data_root = data_dir if data_dir is not None else drive_root
    quiet = {"queued": False, "reason": "", "message": "", "remedy": ""}
    resolved = resolve_project_repo(drive_root, project_id)
    location = thread_location(data_root, project_id, thread_id)
    if location["where"] == "worktree":
        workspace = str(location.get("path") or "")
    elif resolved.get("ok"):
        workspace = str(resolved.get("repo_dir") or "")
    else:
        # No usable folder means no folder lane to contend for; whatever else is
        # wrong with this project, waiting is not it.
        return quiet
    try:
        if running is None:
            from supervisor.queue import _queue_lock
            from supervisor.workers import RUNNING

            with _queue_lock:
                running = list(RUNNING.values())
        lanes = running_project_lanes(running)
    except Exception:
        log.debug("queue_notice could not read the queue for %s", project_id, exc_info=True)
        return quiet
    candidate = {"id": "", "project_id": str(project_id), "workspace_root": workspace}
    if candidate_is_leasable(candidate, lanes):
        return quiet
    own = location["where"] == "worktree"
    return {
        "queued": True,
        "reason": "folder_busy",
        "message": QUEUE_NOTICE_OWN_CHECKOUT if own else QUEUE_NOTICE,
        "remedy": "" if own else "branch_off",
    }


def merge_back_thread(
    drive_root: Any,
    project_id: str,
    thread_id: Any,
    *,
    data_dir: Optional[Any] = None,
    busy: Optional[bool] = None,
) -> Dict[str, Any]:
    """Merge a branched thread's work back into the project's own checkout (A9).

    Preconditions, both refused with a typed reason and honest copy: nothing
    running anywhere in the project, and a clean local tree. A conflict is SHOWN
    with its paths and STOPS the operation — the merge is aborted, so the owner's
    folder is left byte-for-byte as it was and the thread keeps every commit in
    its own branch.

    The worktree SURVIVES a successful merge. Removing it is a separate, inspected
    act (A10) so the owner is always the one who decides that the checkout has
    served its purpose.

    ``busy`` overrides the live activity query (tests, and callers that already
    hold the answer).
    """
    from ouroboros.projects_registry import get_thread
    from ouroboros.thread_worktrees import get_thread_worktree

    resolved = resolve_project_repo(drive_root, project_id)
    if not resolved.get("ok"):
        return resolved
    pid = str(resolved["project_id"])
    repo_dir = pathlib.Path(str(resolved["repo_dir"]))
    data_root = data_dir if data_dir is not None else drive_root

    thread = get_thread(drive_root, pid, thread_id)
    if thread is None:
        return _refused(
            REASON_UNKNOWN_THREAD, f"unknown thread {thread_id!r} in project {pid!r}",
            project_id=pid,
        )
    not_live = _live_thread_refusal(thread, pid)
    if not_live is not None:
        return not_live
    tid = int(thread["id"])
    row = get_thread_worktree(data_root, pid, tid)
    if not row:
        return _refused(
            REASON_NOT_BRANCHED,
            "This thread works in the project folder, so there is nothing to merge back.",
            project_id=pid, thread_id=tid,
        )
    branch = str(row.get("branch") or "")
    checkout = pathlib.Path(str(row.get("path") or ""))
    if not checkout.is_dir():
        return _refused(
            REASON_CHECKOUT_MISSING,
            f"The thread's checkout is gone from disk: {checkout}. Its branch "
            f"{branch!r} still holds the commits.",
            project_id=pid, thread_id=tid, branch=branch,
        )

    if project_is_busy(pid) if busy is None else bool(busy):
        return _refused(
            REASON_PROJECT_BUSY,
            "A task is running in this project right now. Merging while something "
            "is writing could mix half-finished work into the folder, so it waits "
            "until that task finishes.",
            project_id=pid, thread_id=tid, branch=branch,
        )
    status = _git(repo_dir, "status", "--porcelain")
    if status.returncode != 0:
        return _refused(
            REASON_MERGE_FAILED, _detail(status), project_id=pid, thread_id=tid, branch=branch,
        )
    dirty = [line for line in (status.stdout or "").splitlines() if line.strip()]
    if dirty:
        return _refused(
            REASON_LOCAL_TREE_DIRTY,
            "The project folder has uncommitted changes. Commit or stash them "
            "first — merging on top of them would blur which work came from where.",
            project_id=pid, thread_id=tid, branch=branch, dirty_files=dirty[:200],
        )

    before = _git(repo_dir, "rev-parse", "HEAD")
    head_before = (before.stdout or "").strip() if before.returncode == 0 else ""
    merge = _git(
        repo_dir,
        "-c", "user.name=Ouroboros", "-c", "user.email=ouroboros@local",
        "merge", "--no-ff", "--no-edit", branch,
    )
    if merge.returncode != 0:
        conflicted = _git(repo_dir, "diff", "--name-only", "--diff-filter=U")
        paths = [p for p in (conflicted.stdout or "").splitlines() if p.strip()]
        # STOP, and leave the folder exactly as it was. The thread keeps its
        # branch and every commit in it; nothing is discarded by aborting.
        _git(repo_dir, "merge", "--abort")
        if paths:
            return _refused(
                REASON_MERGE_CONFLICT,
                "These files changed on both sides, so the merge was stopped and "
                "the folder left as it was. The thread keeps its branch and all "
                "its commits — resolve the overlap and merge again.",
                project_id=pid, thread_id=tid, branch=branch, conflicts=paths[:200],
            )
        return _refused(
            REASON_MERGE_FAILED, _detail(merge),
            project_id=pid, thread_id=tid, branch=branch,
        )
    after = _git(repo_dir, "rev-parse", "HEAD")
    head_after = (after.stdout or "").strip() if after.returncode == 0 else ""
    return {
        "ok": True,
        "project_id": pid,
        "thread_id": tid,
        "branch": branch,
        "merged": head_after != head_before,
        "head_before": head_before,
        "head_after": head_after,
        # A10: merging never removes the checkout. The owner removes it, or not.
        "worktree_kept": True,
        "location": thread_location(data_root, pid, tid),
    }


__all__ = [
    "BASE_SNAPSHOT",
    "QUEUE_NOTICE",
    "QUEUE_NOTICE_OWN_CHECKOUT",
    "REASON_ALREADY_BRANCHED",
    "REASON_BRANCH_FAILED",
    "REASON_CHECKOUT_MISSING",
    "REASON_FOLDER_MISSING",
    "REASON_FOLDER_UNUSABLE",
    "REASON_GIT_INIT_REQUIRED",
    "REASON_LOCAL_TREE_DIRTY",
    "REASON_MERGE_CONFLICT",
    "REASON_MERGE_FAILED",
    "REASON_NOT_BRANCHED",
    "REASON_NO_FOLDER",
    "REASON_PROJECT_BUSY",
    "REASON_SNAPSHOT_FAILED",
    "REASON_THREAD_NOT_LIVE",
    "REASON_UNKNOWN_BASE",
    "REASON_UNKNOWN_PROJECT",
    "REASON_UNKNOWN_THREAD",
    "branch_off_bases",
    "branch_off_thread",
    "merge_back_thread",
    "project_is_busy",
    "queue_notice",
    "resolve_project_repo",
    "thread_location",
]
