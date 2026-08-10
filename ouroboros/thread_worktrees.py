"""Durable git worktrees owned by project THREADS.

Deliberately SEPARATE from ``subagent_worktrees`` even though both wrap the
same git primitive, because every lifecycle rule is inverted:

===================  ==============================  ==========================
                     subagent worktree               thread worktree
===================  ==============================  ==========================
create over a stale  force-removes checkout+branch   REFUSES (an owner's work
checkout                                             is never clobbered)
removal              ``--force``, unconditional      requires INSPECTION; a
                                                     dirty tree or unmerged
                                                     commits must be
                                                     acknowledged explicitly
startup GC           age sweep past the retention    NONE. A thread's worktree
                     window deletes the checkout     is durable and is only
                                                     removed by an explicit act
===================  ==============================  ==========================

Only the git-op lock and the path-containment guards are reused (imported as
public names from ``subagent_worktrees``); none of its provisioning, removal or
prune behaviour is. The registry lives in its own durable file, so the subagent
orphan sweep — which iterates ITS registry, not the filesystem — can never see
a thread worktree at all.

State: ``data/state/thread_worktrees.json`` via the canonical durable-JSON
pattern, keyed by ``(project_id, thread_id)``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from ouroboros.contracts.schema_versions import with_schema_version
from ouroboros.subagent_worktrees import (
    assert_worktree_root_isolated,
    force_rmtree,
    path_is_within,
    run_git,
    safe_path_component,
    worktree_ops_lock,
)
from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso

log = logging.getLogger(__name__)

_REGISTRY_NAME = "thread_worktrees.json"
_SCHEMA_VERSION = 1
_BRANCH_PREFIX = "thread/"
_LOCK = threading.RLock()

# Not a config.py knob in T0: no owner-facing surface reaches it yet (branch-off
# is a later phase) and config.py is documented as having no reclaimable line.
# Env-overridable so a relocated Ouroboros home still works.
_ROOT_ENV = "OUROBOROS_THREAD_WORKTREE_ROOT"


def thread_worktree_root() -> Path:
    """Durable root for thread checkouts — outside ``repo/`` and ``data/``."""
    raw = str(os.environ.get(_ROOT_ENV, "") or "").strip()
    root = raw or os.path.expanduser(os.path.join("~", "Ouroboros", "thread_worktrees"))
    return Path(root).expanduser().resolve()


@dataclass(frozen=True)
class ThreadWorktree:
    project_id: str
    thread_id: int
    path: str
    branch: str
    base_sha: str
    repo_dir: str
    created_at: float
    created_at_iso: str = ""
    #: The root this checkout was PROVISIONED under, recorded so removal can
    #: validate containment against the same boundary that admitted it (T0R2-9).
    #: Validating against the root resolved at removal time made every existing
    #: row `path_outside_root` — permanently unremovable through the API — the
    #: moment `OUROBOROS_THREAD_WORKTREE_ROOT` changed.
    worktree_root: str = ""


def _registry_path(data_dir: Any) -> Path:
    return Path(data_dir) / "state" / _REGISTRY_NAME


def _load(data_dir: Any) -> List[Dict[str, Any]]:
    data = read_json_dict(_registry_path(data_dir))
    rows = data.get("worktrees") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("path")]


def _save(data_dir: Any, rows: List[Dict[str, Any]]) -> None:
    path = _registry_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, with_schema_version({"worktrees": rows}, _SCHEMA_VERSION))


def _key(project_id: Any, thread_id: Any) -> tuple:
    try:
        return str(project_id or "").strip(), int(thread_id)
    except (TypeError, ValueError):
        return str(project_id or "").strip(), -1


def _matches(row: Dict[str, Any], key: tuple) -> bool:
    try:
        return (str(row.get("project_id") or ""), int(row.get("thread_id"))) == key
    except (TypeError, ValueError):
        return False


def list_thread_worktrees(data_dir: Any) -> List[Dict[str, Any]]:
    """Every registered thread worktree (never age-filtered)."""
    with _LOCK:
        return [dict(row) for row in _load(data_dir)]


def get_thread_worktree(data_dir: Any, project_id: Any, thread_id: Any) -> Optional[Dict[str, Any]]:
    key = _key(project_id, thread_id)
    with _LOCK:
        for row in _load(data_dir):
            if _matches(row, key):
                return dict(row)
    return None


def provision_thread_worktree(
    *,
    repo_dir: Any,
    project_id: str,
    thread_id: Any,
    base_ref: str = "",
    data_dir: Any,
    worktree_root: Optional[Any] = None,
) -> ThreadWorktree:
    """Create a durable worktree for one thread, or REFUSE.

    Unlike the subagent path this never force-removes a stale checkout or
    branch: an existing registration, an existing directory or an existing
    branch is the owner's work, and clobbering it silently is the exact failure
    this registry exists to prevent. Re-provisioning is therefore an error, not
    a reset — remove the worktree explicitly first.
    """
    key = _key(project_id, thread_id)
    if not key[0] or key[1] < 0:
        raise ValueError(f"unusable thread key: {project_id!r}#{thread_id!r}")
    repo = Path(repo_dir).resolve()
    root = Path(worktree_root).expanduser().resolve() if worktree_root else thread_worktree_root()
    assert_worktree_root_isolated(root, repo, Path(data_dir))
    name = safe_path_component(f"{key[0]}__{key[1]}")
    wt_path = (root / name).resolve()
    branch = f"{_BRANCH_PREFIX}{name}"
    with _LOCK, worktree_ops_lock(root):
        rows = _load(data_dir)
        if any(_matches(row, key) for row in rows):
            raise ValueError(
                f"thread {key[0]}#{key[1]} already has a worktree — remove it explicitly first"
            )
        if wt_path.exists():
            raise ValueError(f"refusing to reuse an existing path: {wt_path}")
        existing_branch = run_git(repo, "rev-parse", "--verify", branch, check=False)
        if existing_branch.returncode == 0:
            raise ValueError(
                f"branch {branch!r} already exists — delete it deliberately before branching off again"
            )
        if base_ref:
            run_git(repo, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
            base_sha = run_git(repo, "rev-parse", base_ref).stdout.strip()
        else:
            base_sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        # No --force: git must refuse rather than take over a foreign checkout.
        run_git(repo, "worktree", "add", "-b", branch, str(wt_path), base_sha)
        handle = ThreadWorktree(
            project_id=key[0],
            thread_id=key[1],
            path=str(wt_path),
            branch=branch,
            base_sha=base_sha,
            repo_dir=str(repo),
            created_at=time.time(),
            created_at_iso=utc_now_iso(),
            worktree_root=str(root),
        )
        _save(data_dir, [*rows, asdict(handle)])
        log.info("Thread worktree provisioned: %s#%s at %s", key[0], key[1], wt_path)
        return handle


def inspect_thread_worktree(row: Dict[str, Any]) -> Dict[str, Any]:
    """What removing this worktree would DESTROY — the evidence a removal needs.

    Returns ``{exists, dirty, dirty_files, unmerged_commits, error}``. Never
    raises: an unreadable checkout reports ``error`` and is treated as unsafe
    (``dirty``), because "cannot tell" must never read as "nothing to lose".
    """
    out: Dict[str, Any] = {
        "exists": False, "dirty": False, "dirty_files": [], "unmerged_commits": 0, "error": "",
    }
    wt_path = Path(str(row.get("path") or ""))
    if not wt_path.is_dir():
        return out
    out["exists"] = True
    try:
        status = run_git(wt_path, "status", "--porcelain", check=False)
        if status.returncode != 0:
            # Not a checkout any more (or git refused): unsafe by construction.
            out["error"] = (status.stderr or "git status failed").strip()[:500]
            out["dirty"] = True
            return out
        files = [line for line in status.stdout.splitlines() if line.strip()]
        out["dirty"] = bool(files)
        out["dirty_files"] = files[:200]
        base = str(row.get("base_sha") or "")
        if base:
            ahead = run_git(wt_path, "rev-list", "--count", f"{base}..HEAD", check=False)
            if ahead.returncode != 0:
                out["error"] = (ahead.stderr or "git rev-list failed").strip()[:500]
                out["dirty"] = True
                return out
            out["unmerged_commits"] = int((ahead.stdout or "0").strip() or 0)
    except Exception as exc:
        out["error"] = str(exc)[:500]
        out["dirty"] = True
    return out


def remove_thread_worktree(
    *,
    data_dir: Any,
    project_id: str,
    thread_id: Any,
    acknowledge_unmerged: bool = False,
    worktree_root: Optional[Any] = None,
) -> Dict[str, Any]:
    """Remove a thread worktree AFTER inspecting what that would destroy.

    Returns ``{removed, reason, inspection}``. A dirty tree or commits the base
    never received refuse the removal unless ``acknowledge_unmerged`` is passed
    — the caller must have SHOWN the owner the inspection first. There is no
    silent path and no timer that reaches this function.

    Containment is checked against the root the row was PROVISIONED under
    (T0R2-9), not against whatever this process resolves today. Resolving it at
    removal time meant relocating ``OUROBOROS_THREAD_WORKTREE_ROOT`` — or simply
    passing a different ``worktree_root`` — turned every existing row into
    ``path_outside_root``: unremovable through the API forever, with the mirror
    hazard that a moved root would ADMIT a path it should never have admitted.
    A pre-T3 row carries no provisioning root; it falls back to the resolved one,
    which is exactly the behaviour it was written under.
    """
    key = _key(project_id, thread_id)
    root = Path(worktree_root).expanduser().resolve() if worktree_root else thread_worktree_root()
    with _LOCK, worktree_ops_lock(root):
        rows = _load(data_dir)
        match = next((row for row in rows if _matches(row, key)), None)
        if match is None:
            return {"removed": False, "reason": "unknown", "inspection": {}}
        inspection = inspect_thread_worktree(match)
        unsafe = bool(inspection["dirty"]) or int(inspection["unmerged_commits"]) > 0
        if unsafe and not acknowledge_unmerged:
            return {"removed": False, "reason": "unmerged_work", "inspection": inspection}
        wt_path = Path(str(match.get("path") or ""))
        stored_root = str(match.get("worktree_root") or "").strip()
        guard_root = Path(stored_root).expanduser().resolve() if stored_root else root
        if not str(wt_path).strip() or not path_is_within(wt_path, guard_root):
            # A malformed registry row must never delete an arbitrary path.
            return {"removed": False, "reason": "path_outside_root", "inspection": inspection}
        repo = Path(str(match.get("repo_dir") or "."))
        run_git(repo, "worktree", "remove", "--force", str(wt_path), check=False)
        if wt_path.exists():
            force_rmtree(wt_path)
        run_git(repo, "worktree", "prune", check=False)
        _save(data_dir, [row for row in rows if not _matches(row, key)])
        log.info("Thread worktree removed: %s#%s (%s)", key[0], key[1], wt_path)
        return {"removed": True, "reason": "", "inspection": inspection}


__all__ = [
    "ThreadWorktree",
    "get_thread_worktree",
    "inspect_thread_worktree",
    "list_thread_worktrees",
    "provision_thread_worktree",
    "remove_thread_worktree",
    "thread_worktree_root",
]
