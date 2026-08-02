"""Managed-update merge engine (P2): a REAL git 3-way merge in an isolated temp worktree,
the apply / rollback / smoke / finalize primitives, and a FAIL-CLOSED update lock.

Kept OUT of ``git_ops`` (module-size discipline) but depends on it for the live-repo git
helpers — referenced via the ``git_ops`` module object (``_g.X``) so a test that
monkeypatches ``git_ops.REPO_DIR`` / ``_managed_update_target`` / ``_git_dir`` /
``DRIVE_ROOT`` is followed by these primitives. Control plane: ``ouroboros.gateway.control``
orchestrates lock → kill workers → re-plan → rescue → tx marker → apply → smoke → restart.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.utils import append_jsonl, utc_now_iso
from supervisor import git_ops as _g

UPDATE_TX_MARKER_NAME = "ouroboros-update-tx.json"


def _git_run(
    cmd: List[str], *, cwd: Optional[str] = None, extra_env: Optional[Dict[str, str]] = None
) -> Tuple[int, str, str]:
    """Run a git command with an optional cwd / extra env (e.g. GIT_INDEX_FILE), WITHOUT
    the REPO_DIR pin and index-repair retry of ``git_capture``. For merge-planning in a
    temp index / temp worktree only — never the live-repo control path."""
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(cmd, cwd=str(cwd or _g.REPO_DIR), capture_output=True, text=True, env=env)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def plan_managed_update_merge(
    fetch: bool = False, branch: Optional[str] = None, build: bool = False
) -> Dict[str, Any]:
    """Dry-run the managed update as a REAL 3-way merge in an ISOLATED temp worktree and
    classify the result (P2). NEVER touches the live worktree or index. Returns a
    ``merge_plan`` dict: available/kind/auto_mergeable, the doc/code/protected conflict
    split, target_sha/base_sha, local_dirty_count, recommended_strategy. Best-effort:
    always cleans up the temp index + worktree; classification uses update_merge_policy.

    When ``build=True`` AND the merge is clean, the merged tree is committed as a real
    merge commit (parents = [local_snapshot, target]) whose sha is returned as
    ``merge_commit`` — a durable object in the shared DB that survives temp-worktree
    removal, ready for ``apply_managed_merge_update`` to land on the live repo."""
    import shutil
    import tempfile

    from supervisor.update_merge_policy import classify_conflicts

    branch_dev = branch or _g.BRANCH_DEV
    remote_name, _remote_branch, target_ref = _g._managed_update_target(branch_dev)
    if not target_ref:
        return {"available": False, "kind": "unavailable", "error": "no managed update remote"}
    if fetch and remote_name:
        # BOUNDED, like the sibling fetch in `compute_managed_update_status` — and more urgently
        # here, because `_apply_replace_family_fenced` re-plans through this call with the
        # exclusive update lock held AND the whole worker pool already stopped. A stalled remote
        # is not an exception any `except` can convert: the call simply never returns, leaving the
        # pool dead, the lock held and every later update answering a lock-held 409. The bound has
        # to be a WALL CLOCK rather than git's http low-speed knobs, because this site fetches
        # whatever the managed manifest resolved (nothing normalizes it to https here the way
        # `ensure_official_update_remote` does for the status check) — see `git_fetch_bounded`.
        rc_f, _fo, fetch_err = _g.git_fetch_bounded(remote_name)
        if rc_f == _g.FETCH_TIMEOUT_RC:
            # A fetch we KILLED says nothing about where the remote's head actually is, and this
            # is the fenced call site, so hand back a plan that cannot be applied: the caller
            # (`_replace_family_protected_gate` -> `_apply_replace_family_fenced`) answers it by
            # respawning the workers, and the lock is released on the way out.
            return {"available": False, "kind": "unavailable",
                    "error": f"managed fetch timed out: {fetch_err}"}
        if rc_f != 0:
            # A fetch that FAILED (offline, auth, no such remote) is deliberately NOT fatal here.
            # It leaves the tracking ref exactly where the disclosure read it, which both the
            # replace-family pin and `_post_stop_plan_drift` compare against, so the release that
            # can still be applied is the acknowledged one; `compute_managed_update_status` treats
            # the same condition as a warning. Failing closed here would only make every offline
            # apply of an already-reviewed release unreachable, buying no safety.
            _g.log.warning("plan_managed_update_merge: managed fetch failed: %s", fetch_err)

    rc_t, target_sha, _te = _g.git_capture(["git", "rev-parse", "--verify", f"{target_ref}^{{commit}}"])
    rc_h, base_sha, _he = _g.git_capture(["git", "rev-parse", "--verify", "HEAD"])
    if rc_t != 0 or rc_h != 0 or not target_sha or not base_sha:
        return {"available": False, "kind": "unavailable", "error": "could not resolve target/HEAD"}
    if target_sha == base_sha:
        return {"available": False, "kind": "current", "target_sha": target_sha, "base_sha": base_sha}

    # The dirty count is a SAFETY PROOF downstream (`_plan_worktree_is_clean` treats integer zero
    # as proof of an empty worktree and unlocks the unreviewed auto-merge fast path), so it may
    # only be emitted from a git status that actually SUCCEEDED. A failed command also prints
    # nothing on stdout, and counting those zero lines would forge exactly that proof.
    rc_dirty, dirty_out, dirty_err = _g.git_capture(["git", "status", "--porcelain"])
    if rc_dirty != 0:
        return {"available": True, "kind": "unknown", "target_sha": target_sha,
                "base_sha": base_sha, "error": f"status failed: {dirty_err or rc_dirty}"}
    local_dirty_count = len([ln for ln in dirty_out.splitlines() if ln.strip()])

    tmp_index_path = None
    tmp_wt = None
    try:
        # 1. Local snapshot commit = HEAD + tracked-dirty + untracked. Built in a TEMP
        #    index (GIT_INDEX_FILE), so the live index is untouched. ``git add -A`` honors
        #    .gitignore, so ignored build/secret junk is excluded from durable history.
        fd, tmp_index_path = tempfile.mkstemp(prefix="ouro-update-index-")
        os.close(fd)
        # `git read-tree` wants a NON-existent index path — an existing zero-byte file errors
        # ("index file smaller than expected") on some git versions. Unlink so git creates it
        # fresh; the finally block's unlink is OSError-guarded if it's already gone.
        os.unlink(tmp_index_path)
        env = {"GIT_INDEX_FILE": tmp_index_path}
        if _git_run(["git", "read-tree", "HEAD"], extra_env=env)[0] != 0:
            return {"available": True, "kind": "unknown", "target_sha": target_sha,
                    "base_sha": base_sha, "error": "read-tree failed"}
        # A failed `add -A` leaves the temp index at bare HEAD, so the snapshot would silently
        # OMIT the owner's dirty/untracked work while the plan still looks buildable — the merge
        # would then be landed over content nothing captured. Fail closed instead.
        rc_add, _ao, add_err = _git_run(["git", "add", "-A"], extra_env=env)
        if rc_add != 0:
            return {"available": True, "kind": "unknown", "target_sha": target_sha,
                    "base_sha": base_sha, "error": f"add -A failed: {add_err or rc_add}"}
        rc_wt, local_tree, _we = _git_run(["git", "write-tree"], extra_env=env)
        if rc_wt != 0 or not local_tree:
            return {"available": True, "kind": "unknown", "target_sha": target_sha,
                    "base_sha": base_sha, "error": "write-tree failed"}
        rc_ct, local_snapshot, _ce = _git_run(
            ["git", "commit-tree", local_tree, "-p", base_sha,
             "-m", "ouroboros local snapshot (update merge plan)"],
            extra_env=env,
        )
        if rc_ct != 0 or not local_snapshot:
            return {"available": True, "kind": "unknown", "target_sha": target_sha,
                    "base_sha": base_sha, "error": "commit-tree failed"}

        # 2. Isolated temp worktree at the snapshot; merge the target THERE (never live).
        #    Use a NON-existent child path (git worktree add refuses an existing dir).
        tmp_wt = os.path.join(tempfile.mkdtemp(prefix="ouro-update-wt-"), "wt")
        rc_add, _ao, add_err = _g.git_capture(["git", "worktree", "add", "--detach", tmp_wt, local_snapshot])
        if rc_add != 0:
            return {"available": True, "kind": "unknown", "target_sha": target_sha,
                    "base_sha": base_sha, "error": f"worktree add failed: {add_err}"}
        # --no-commit --no-ff: leave the merged/conflicted index in place to inspect. Conflicts
        # make git exit 1 — the EXPECTED outcome here, not a failure. Any OTHER nonzero rc (a
        # fatal merge: unrelated histories, an unusable temp worktree, ENOSPC) means the merge
        # never ran, and the temp index is then still sitting at `local_snapshot` with nothing
        # unmerged. Reading that as "no conflicts" would forge the same clean-merge proof as a
        # failed `git status` above: `classify_conflicts([])` returns kind "clean", and with
        # build=True the follow-on write-tree/commit-tree would record a 2-parent commit whose
        # TREE is the PRE-update snapshot. The release would then read as merged (behind == 0,
        # and the restart smoke passes because the code never changed) while none of its content
        # — safety-critical changes included — actually landed.
        rc_m, _mo, merge_err = _git_run(
            ["git", "-C", tmp_wt, "merge", "--no-commit", "--no-ff", target_sha]
        )
        if rc_m not in (0, 1):
            return {"available": True, "kind": "unknown", "target_sha": target_sha,
                    "base_sha": base_sha, "error": f"merge dry-run failed: {merge_err or rc_m}"}
        # Same trap one command later: this inventory IS the conflict set, so a failed run must
        # never be flattened into an empty (== clean) one. Plain `git diff` exits 0 on success.
        rc_u, unmerged_out, unmerged_err = _git_run(
            ["git", "-C", tmp_wt, "diff", "--name-only", "--diff-filter=U"]
        )
        if rc_u != 0:
            return {"available": True, "kind": "unknown", "target_sha": target_sha,
                    "base_sha": base_sha, "error": f"conflict inventory failed: {unmerged_err or rc_u}"}
        unmerged = [ln.strip() for ln in unmerged_out.splitlines() if ln.strip()]

        plan = classify_conflicts(unmerged)
        kind = str(plan["kind"])
        merge_commit = ""
        if build and kind == "clean":
            # Commit the (clean) merged tree as a real merge commit in the shared object
            # DB so it survives temp-worktree removal and can be landed on the live repo.
            rc_mt, merged_tree, _mte = _git_run(["git", "-C", tmp_wt, "write-tree"])
            if rc_mt == 0 and merged_tree:
                rc_mc, built, _mce = _git_run([
                    "git", "commit-tree", merged_tree,
                    "-p", local_snapshot, "-p", target_sha,
                    "-m", f"Merge official Ouroboros update {target_sha[:12]} (auto)",
                ])
                if rc_mc == 0 and built:
                    merge_commit = built
        return {
            "available": True,
            "kind": kind,
            "auto_mergeable": kind == "clean",
            "doc_conflict_paths": plan["doc_conflict_paths"],
            "code_conflict_paths": plan["code_conflict_paths"],
            "protected_conflict_paths": plan["protected_conflict_paths"],
            "hot_code_paths": plan["hot_code_paths"],
            "target_sha": target_sha,
            "base_sha": base_sha,
            "local_dirty_count": local_dirty_count,
            "local_snapshot": local_snapshot,
            "merge_commit": merge_commit,
            "recommended_strategy": "auto_merge" if kind == "clean" else "assisted",
        }
    except Exception as exc:  # pragma: no cover — planning is best-effort
        _g.log.warning("plan_managed_update_merge failed", exc_info=True)
        return {"available": True, "kind": "unknown", "target_sha": target_sha,
                "base_sha": base_sha, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if tmp_wt:
            _g.git_capture(["git", "worktree", "remove", "--force", tmp_wt])
            shutil.rmtree(os.path.dirname(tmp_wt), ignore_errors=True)
            _g.git_capture(["git", "worktree", "prune"])
        if tmp_index_path:
            try:
                os.unlink(tmp_index_path)
            except OSError:
                pass


def _update_tx_marker_path():
    return _g._git_dir() / UPDATE_TX_MARKER_NAME


def read_update_tx() -> Dict[str, Any]:
    import json

    path = _update_tx_marker_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def write_update_tx(payload: Dict[str, Any]) -> None:
    from ouroboros.utils import atomic_write_json

    atomic_write_json(_update_tx_marker_path(), payload, trailing_newline=True)


def clear_update_tx() -> bool:
    """Remove the update transaction marker and answer whether it is PROVEN absent afterwards.

    Same contract, and the same reason for it, as ``_g._clear_update_intent``: the marker is
    re-stat'ed rather than trusting the unlink's own outcome, because "we could not tell" is not "it
    is gone". Returning nothing let every cleanup caller report success over a marker still on disk
    — ``active_update_tx()`` would go on demanding recovery while rollback answered rolled-back,
    boot finalization answered finalized, and the preparation-failure path reopened the checkout to
    writers. Callers must keep writers locked down on a False.
    """
    path = _update_tx_marker_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except Exception:
        _g.log.warning("Failed to clear update tx marker", exc_info=True)
    try:
        return not path.exists()
    except Exception:
        _g.log.warning("Failed to verify update tx marker removal", exc_info=True)
        return False


_ASSISTED_PHASES = ("materializing_assisted", "assisted_resolution", "committing_assisted")


UPDATE_TX_GATE_BLOCKED = "gate_blocked"


def mark_update_tx_gate_blocked(reason: str) -> bool:
    """Re-phase a live update tx to the terminal ``gate_blocked``; True if the marker was rewritten.

    Only for the one path that needs it: a gate REJECTED the update and the rollback that should
    have erased the tx then failed. What that failure leaves behind is the pre-gate phase, and for
    an assisted update that is ``committing_assisted`` — which boot recovery reads as "the process
    died mid-commit", so it promotes the merge to ``pending_boot_smoke`` and finalizes it, landing
    the exact revision the gate refused, without ever rerunning the gate. ``gate_blocked`` appears
    in no recovery path, so the next boot logs it and stops instead."""
    tx = read_update_tx()
    if not tx:
        return False
    tx["phase"] = UPDATE_TX_GATE_BLOCKED
    tx["gate_blocked_reason"] = reason
    write_update_tx(tx)
    _log_supervisor({"type": "managed_update_gate_blocked", "reason": reason})
    return True


def read_update_tx_strict() -> Tuple[str, Dict[str, Any]]:
    """Strict tx read for safety-critical gates (commit authorization, tx-active rejection):
    return ``(status, tx)`` where status is ``"absent"`` / ``"valid"`` / ``"corrupt"``. A
    marker that exists but is unreadable/invalid is ``corrupt`` — callers MUST fail closed
    (block mutative update/commit ops) rather than treat it as ``absent``."""
    import json

    path = _update_tx_marker_path()
    if not path.is_file():
        return "absent", {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "corrupt", {}
    if not isinstance(raw, dict) or not raw:
        return "corrupt", {}
    return "valid", raw


def active_update_tx() -> Dict[str, Any]:
    """Return the active tx dict if a (valid or corrupt) marker is present, else ``{}``. A
    corrupt marker counts as ACTIVE (fail-closed) so a second apply cannot proceed over it."""
    status, tx = read_update_tx_strict()
    if status == "absent":
        return {}
    return tx or {"phase": "corrupt"}


def authorized_assisted_task(task_id: str) -> Dict[str, Any]:
    """Return the active assisted tx iff ``task_id`` is its authorized resolver, else ``{}``.
    The tx marker — never an LLM-supplied value — is the trust root for the managed merge."""
    status, tx = read_update_tx_strict()
    if status != "valid":
        return {}
    if str(tx.get("phase") or "") not in _ASSISTED_PHASES:
        return {}
    if str(tx.get("task_id") or "") != str(task_id or ""):
        return {}
    return tx


def _rev_parse(ref: str) -> str:
    rc, out, _e = _g.git_capture(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"])
    return out if rc == 0 else ""


def _merge_head_sha() -> str:
    rc, out, _e = _g.git_capture(["git", "rev-parse", "--verify", "-q", "MERGE_HEAD"])
    return out if rc == 0 else ""


def create_rescue_local_ref(local_snapshot: str) -> str:
    """Pin the local snapshot (the ONLY home of the owner's uncommitted+untracked work) to a
    durable branch so a later rollback / git-gc can never lose it. Returns the branch name."""
    short = (local_snapshot or "")[:12]
    name = f"rescue-local-{short}"
    if local_snapshot:
        _g.git_capture(["git", "branch", "-f", name, local_snapshot])
    return name


def materialize_assisted_merge_live(
    branch: str, local_snapshot: str, target_sha: str, pre_update_sha: str
) -> Tuple[bool, str]:
    """Stage a REAL ``git merge --no-commit --no-ff target`` into the LIVE worktree (MERGE_HEAD +
    a conflicted index + markers) for the agent to resolve and the unmodified ``commit_reviewed``
    to finalize as a reviewed 2-parent commit. Caller MUST hold the update lock with workers
    stopped. Conflicts make ``git merge`` exit nonzero — that is EXPECTED, not failure: success is
    judged by MERGE_HEAD == target_sha. Returns (ok, message).

    P3 immune integrity: the merge is computed FROM ``local_snapshot`` (which captures the owner's
    committed + dirty + untracked work, so nothing is lost), but the first parent is then re-based
    to ``pre_update_sha`` (the last REVIEWED committed state) via a soft reset, so the reviewed
    ``git diff --cached`` (pre_update_sha → resolved) INCLUDES the owner's uncommitted/untracked
    work — none of it reaches history as an unreviewed parent."""
    if not local_snapshot or not target_sha or not pre_update_sha:
        return False, "missing local_snapshot/target_sha/pre_update_sha"
    # Clean the worktree first (dirty + untracked are all captured in local_snapshot + the rescue
    # snapshot + the rescue-local ref) so `checkout -B` cannot fail on "untracked file would be
    # overwritten"; checkout restores them from local_snapshot as tracked content. A real 3-way
    # merge needs a clean tree to run.
    _g.git_capture(["git", "reset", "--hard", "HEAD"])
    _g.git_capture(["git", "clean", "-fd"])
    rc_c, _o, e_c = _g.git_capture(["git", "checkout", "-B", branch, local_snapshot])
    if rc_c != 0:
        return False, f"checkout -B {branch} {local_snapshot[:12]} failed: {e_c}"
    # Ignore the merge return code; conflicts are expected. Judge by MERGE_HEAD.
    _g.git_capture(["git", "merge", "--no-commit", "--no-ff", target_sha])
    mh = _merge_head_sha()
    if not mh:
        return False, "merge produced no MERGE_HEAD (nothing to merge or fatal error)"
    if mh != target_sha:
        return False, f"MERGE_HEAD {mh[:12]} != target {target_sha[:12]}"
    # Re-base the first parent to the reviewed pre-update state WITHOUT disturbing the merge
    # result: `git reset --soft` is refused mid-merge, so move the branch ref directly with
    # update-ref (HEAD follows the symbolic ref) — the index (conflicted/merged entries), the
    # worktree, and MERGE_HEAD are all untouched, so commit_reviewed still makes a 2-parent
    # commit [pre_update_sha, target] whose reviewed diff (pre_update_sha → resolved) includes
    # the owner's dirty/untracked work.
    rc_r, _ro, e_r = _g.git_capture(["git", "update-ref", f"refs/heads/{branch}", pre_update_sha])
    if rc_r != 0:
        return False, f"update-ref {branch} -> {pre_update_sha[:12]} failed: {e_r}"
    if _merge_head_sha() != target_sha:
        return False, "MERGE_HEAD lost after re-parenting the branch"
    return True, f"materialized merge of {target_sha[:12]} (parent={pre_update_sha[:12]}, MERGE_HEAD set)"


def _assisted_head_state(tx: Dict[str, Any]) -> str:
    """Classify the live HEAD vs the assisted tx for boot recovery — keyed on MERGE STATE. During
    resolution HEAD == pre_update_sha (the merge result is staged but uncommitted); the reviewed
    merge commit has pre_update_sha as its FIRST parent and target_sha as its second:
      - ``committed``  : HEAD is a 2-parent commit whose 2nd parent is target_sha (descends from
                         pre_update_sha), or tx.merge_commit is in HEAD.
      - ``in_progress``: HEAD == pre_update_sha (no commit yet — re-materialize/resume).
      - ``diverged``   : HEAD descends from pre_update_sha but is NOT the target merge (a real
                         reviewed commit landed on top — keep it, never reset over it).
      - ``unknown``    : cannot resolve (fail safe: keep)."""
    pre = str(tx.get("pre_update_sha") or "")
    target_sha = str(tx.get("target_sha") or "")
    merge_commit = str(tx.get("merge_commit") or "")
    rc_h, head, _he = _g.git_capture(["git", "rev-parse", "--verify", "HEAD"])
    if rc_h != 0 or not head:
        return "unknown"
    if merge_commit and (
        head == merge_commit
        or _g.git_capture(["git", "merge-base", "--is-ancestor", merge_commit, "HEAD"])[0] == 0
    ):
        return "committed"
    if pre and head == pre:
        return "in_progress"
    # A merge commit whose 2nd parent is the target and which descends from pre_update_sha.
    if pre and target_sha:
        rc_p, parents, _pe = _g.git_capture(["git", "rev-list", "--parents", "-n", "1", "HEAD"])
        descends = _g.git_capture(["git", "merge-base", "--is-ancestor", pre, "HEAD"])[0] == 0
        if rc_p == 0 and target_sha in parents.split()[1:] and descends:
            return "committed"
        if descends:
            return "diverged"
    return "unknown"


def acquire_update_lock():
    """Acquire the FAIL-CLOSED managed-update lock; return an open file handle that keeps
    the lock held. Raise RuntimeError if another update operation holds it — the update
    MUST NOT proceed unlocked (a self-mod write or owner-restart racing the reset has
    corrupted trees before). Release with ``release_update_lock(fh)``."""
    from ouroboros.platform_layer import file_lock_exclusive_nb

    lock_dir = _g.DRIVE_ROOT / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fh = (lock_dir / "managed_update.lock").open("a+")
    try:
        file_lock_exclusive_nb(fh.fileno())  # raises OSError if already held
    except OSError as exc:
        fh.close()
        raise RuntimeError("managed_update.lock is held by another update operation") from exc
    return fh


def release_update_lock(fh) -> None:
    from ouroboros.platform_layer import file_unlock

    try:
        file_unlock(fh.fileno())
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def apply_managed_merge_update(branch: str, merge_commit: str) -> Tuple[bool, str]:
    """Land a prepared merge commit on the LIVE repo. Caller MUST already hold the update
    lock, have stopped workers, and written the rescue + tx markers. The live dirty state
    is preserved in the merge's local-snapshot parent (and the rescue), so it is reset
    away here. Returns (ok, message)."""
    if not merge_commit:
        return False, "no merge_commit to apply"
    _g.git_capture(["git", "reset", "--hard", "HEAD"])
    _g.git_capture(["git", "clean", "-fd"])
    rc1, _o1, e1 = _g.git_capture(["git", "checkout", "-B", branch, merge_commit])
    if rc1 != 0:
        return False, f"checkout -B {branch} {merge_commit[:12]} failed: {e1}"
    rc2, _o2, e2 = _g.git_capture(["git", "reset", "--hard", merge_commit])
    if rc2 != 0:
        return False, f"reset --hard {merge_commit[:12]} failed: {e2}"
    _g.git_capture(["git", "clean", "-fd"])
    return True, f"applied merge {merge_commit[:12]} to {branch}"


def rollback_managed_update(reason: str = "update_rollback") -> Tuple[bool, str]:
    """Roll a failed managed update back to the pre-update SHA in the tx marker. Tags the
    bad candidate as ``failed-update-<sha>`` for forensics, hard-resets the branch to
    pre_update_sha, cleans, clears the update markers, and logs. Does NOT push (unlike
    rollback_to_version, which can push origin — wrong for an internal recovery).

    The `True` this returns is a PROOF, not a report that the commands were issued. Every caller
    treats it as the licence to re-admit in-process writers and start a fresh worker pool, so the
    restoring commands are all return-code checked and the RESULT is then re-read from the
    checkout: HEAD must resolve to exactly the transaction's `pre_update_sha`, and
    ``git status --porcelain`` must run and come back empty. A `reset --hard` or `clean -fd` that
    silently failed (a locked index, a permission error, an unmergeable untracked file) used to be
    ignored, after which this cleared the transaction and answered success — leaving writers on a
    half-updated tree with the only record of the pre-update SHA deleted."""
    tx = read_update_tx()
    pre = str(tx.get("pre_update_sha") or "")
    branch = str(tx.get("pre_update_branch") or _g.BRANCH_DEV)
    if not pre:
        return False, "no pre_update_sha in update tx marker"
    rc_h, cur_head, _he = _g.git_capture(["git", "rev-parse", "--short", "HEAD"])
    forensics = ""
    if rc_h == 0 and cur_head:
        # BEFORE anything destructive: the reset below leaves this ref as the only name the
        # rejected candidate still has, so writing it late would discard it.
        rc_b, _ob, e_b = _g.git_capture(
            ["git", "branch", "-f", f"failed-update-{cur_head}", "HEAD"])
        if rc_b != 0:
            # BEST-EFFORT, deliberately: this function's job is to get the machine back onto a
            # working revision. Losing the candidate's name is the smaller loss; it is reported
            # in the success message, never fatal.
            forensics = f" (failed-update-{cur_head} could not be recorded: {e_b})"
            _log_supervisor({"type": "managed_update_forensics_ref_failed",
                             "head": cur_head, "error": e_b, "reason": reason})
    _g.git_capture(["git", "reset", "--hard", "HEAD"])
    _g.git_capture(["git", "clean", "-fd"])
    rc1, _o1, e1 = _g.git_capture(["git", "checkout", "-B", branch, pre])
    if rc1 != 0:
        return False, f"rollback checkout -B {branch} {pre[:12]} failed: {e1}"
    rc2, _o2, e2 = _g.git_capture(["git", "reset", "--hard", pre])
    if rc2 != 0:
        return False, f"rollback reset --hard {pre[:12]} failed: {e2}"
    rc3, _o3, e3 = _g.git_capture(["git", "clean", "-fd"])
    if rc3 != 0:
        return False, f"rollback clean -fd failed: {e3}"
    # Re-read the checkout: the commands above reporting success is not the same fact as the tree
    # actually BEING the pre-update one, and the latter is what the callers act on.
    rc_head, head_now, e_head = _g.git_capture(["git", "rev-parse", "HEAD"])
    rc_pre, pre_full, e_pre = _g.git_capture(["git", "rev-parse", pre])
    if rc_head != 0 or rc_pre != 0 or not head_now or head_now != pre_full:
        return False, (
            f"rollback did not restore {pre[:12]}: HEAD is {head_now or '?'} "
            f"({e_head or e_pre or 'no error reported'})"
        )
    rc_st, porcelain, e_st = _g.git_capture(["git", "status", "--porcelain"])
    if rc_st != 0:
        return False, f"rollback could not verify a clean tree: {e_st}"
    if porcelain.strip():
        return False, (
            "rollback left the tree dirty: "
            f"{len(porcelain.strip().splitlines())} uncleaned path(s)"
        )
    # The intent goes FIRST, and only a proven removal lets the transaction go. Cleared the other
    # way round, a failed intent unlink left a restart-consumed "reset onto the update target"
    # instruction with no transaction to explain it, which the next bootstrap reads as an ordinary
    # checkout and applies — undoing the rollback we just proved.
    if not _g._clear_update_intent():
        return False, "rollback could not remove the update intent marker; transaction retained"
    # The marker is the ONLY record of `pre_update_sha`, so it is cleared last — after the undo it
    # describes has been proven — and never on a path that gives up above. Its removal is PROVEN
    # too: a marker still on disk keeps `active_update_tx()` answering "an update is in progress",
    # so reporting a rollback the callers act on (re-admitting writers, starting a fresh pool) would
    # hand them a checkout the boot path is still going to recover.
    if not clear_update_tx():
        return False, "rollback could not remove the update transaction marker"
    append_jsonl(
        _g.DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {"ts": utc_now_iso(), "type": "managed_update_rolled_back", "reason": reason,
         "pre_update_sha": pre, "branch": branch},
    )
    return True, f"rolled back to {pre[:12]}{forensics}"


def _assisted_objective(tx: Dict[str, Any]) -> str:
    target = str(tx.get("target_sha") or "")[:12]
    conflicts = list(tx.get("conflict_paths") or [])
    files = ", ".join(conflicts) if conflicts else "see `git status` for unmerged paths"
    return (
        f"A managed Ouroboros update (target {target}) has been merged into your working tree by the "
        "supervisor: MERGE_HEAD is set and conflicts are marked in the files. Do NOT run any git command "
        "(fetch/merge/commit/checkout are blocked) — the merge is already staged for you. For each "
        f"conflicting file ({files}) reconcile OUR local changes with the official version, preserving both "
        "intents where possible and removing every conflict marker (<<<<<<<, =======, >>>>>>>). Do NOT "
        "weaken BIBLE.md, docs/CHECKLISTS.md, or prompts/SAFETY.md. When every conflict is resolved, run "
        "`advisory_review` with the commit message, then `commit_reviewed` (it will create the reviewed "
        "2-parent merge commit), then `request_restart` to finish landing the update."
    )


def _drop_queued_task(task_id: str) -> None:
    """Remove a queued task from PENDING in place (the list object is shared with the worker
    module through ``init_queue_refs``, so it must be mutated, never rebound)."""
    from supervisor import queue as _queue

    if not task_id:
        return
    with _queue._queue_lock:
        _queue.PENDING[:] = [
            t for t in _queue.PENDING if str((t or {}).get("id") or "") != task_id
        ]


def enqueue_assisted_resolution_task(tx: Dict[str, Any], *, ready_timeout: float = 0.0) -> str:
    """Enqueue (front) the single authorized resolution task for an assisted merge and start a
    worker for it. Used by both the apply orchestration and boot recovery so the objective +
    structured metadata stay in one place. Returns the task id.

    ``ready_timeout`` bounds the handshake wait below; 0 takes the worker module's own default.
    The interactive apply passes a SHORTER one: that caller runs synchronously inside the async
    apply handler, so the ceiling is time the gateway event loop is blocked with the pool down and
    writer admission closed, on top of an already-synchronous fetch, plan and materialize. Boot
    recovery keeps the default — no request is waiting on it, and a pool that is merely slow to
    import is worth more patience there.

    This function NEVER tears the pool down. It is called from two places with opposite recovery
    needs, and tearing down here served only one of them: the apply path rolls the checkout back and
    respawns, but boot recovery is non-destructive and does not — so a slow import at boot would
    have emptied ``WORKERS``, driven every restored pending task to a terminal failure through the
    kill's drain, and left a pool the health loop cannot refill (it only tops up slots already
    present). Teardown is therefore the CALLER's decision, taken where the matching respawn is.

    RAISES ``RuntimeError`` when no worker could be started. This task is the ONLY thing that can
    finish an assisted merge, so a pool-start failure that was merely logged let the caller report
    `assisted_started` over a staged merge and a live transaction with nothing to resolve them —
    and the health loop only respawns slots already present in ``WORKERS``, so an empty pool does
    not self-recover. The queued task is removed again before raising, so a later recovery does not
    find a resolver that never had an executor.

    "Started" is established by the WORKER, not by the spawn call: this pool boots on code that was
    merged into the live worktree moments ago, so an import that the merge broke is exactly the
    likely failure — and it produces a populated ``WORKERS`` full of children that already exited.
    A bounded ready handshake is therefore required before returning."""
    from supervisor.queue import enqueue_task
    from supervisor.workers import (
        WORKER_READY_TIMEOUT_SEC,
        ensure_worker_pool_started,
        wait_for_ready_worker,
        worker_pool_admission_state,
    )

    task_id = str(tx.get("task_id") or "")
    task = {
        "id": task_id,
        "text": _assisted_objective(tx),
        "type": "task",
        "chat_id": int(tx.get("owner_chat_id") or 0),
        "metadata": {
            "managed_update": {
                "target_sha": str(tx.get("target_sha") or ""),
                "conflict_paths": list(tx.get("conflict_paths") or []),
                "local_snapshot": str(tx.get("local_snapshot") or ""),
            }
        },
    }
    enqueue_task(task, front=True)
    try:
        started = bool(ensure_worker_pool_started(allow_disabled_restart=True))
        # `ensure_worker_pool_started` answers "I did not refuse", not "a worker exists": re-read
        # the admission state so the return value rests on a pool that is actually populated.
        available = started and bool(worker_pool_admission_state().get("available"))
    except Exception as exc:
        _drop_queued_task(task_id)
        raise RuntimeError(f"worker pool start failed: {exc}") from exc
    if not available:
        _drop_queued_task(task_id)
        raise RuntimeError("no worker could be started for the assisted resolution task")
    if not wait_for_ready_worker(timeout=ready_timeout or WORKER_READY_TIMEOUT_SEC):
        _drop_queued_task(task_id)
        raise RuntimeError("no worker became ready to run the assisted resolution task")
    return task_id


def update_restart_smoke() -> Dict[str, Any]:
    """Stronger pre-restart smoke than ``import_test`` for gating an update apply: no
    unmerged index, ``py_compile server.py``, and an import of the core boot surface.
    pytest is intentionally NOT in this blocking gate (bloat/risk in a live self-updater)."""
    if getattr(sys, "frozen", False):
        return {"ok": True, "skipped": "frozen"}
    rc_u, unmerged, _ue = _g.git_capture(["git", "diff", "--name-only", "--diff-filter=U"])
    if rc_u == 0 and unmerged.strip():
        return {"ok": False, "stderr": f"unmerged paths remain: {unmerged}", "returncode": 1}
    compiled = subprocess.run(
        [sys.executable, "-m", "py_compile", "server.py"],
        cwd=str(_g.REPO_DIR), capture_output=True, text=True,
    )
    if compiled.returncode != 0:
        return {"ok": False, "stderr": compiled.stderr, "returncode": compiled.returncode}
    imported = subprocess.run(
        [sys.executable, "-c",
         "import server, ouroboros.gateway.router, supervisor.queue, "
         "supervisor.events, ouroboros.tools.registry; print('smoke_ok')"],
        cwd=str(_g.REPO_DIR), capture_output=True, text=True,
    )
    return {"ok": (imported.returncode == 0), "stdout": imported.stdout,
            "stderr": imported.stderr, "returncode": imported.returncode}


_ASSISTED_BOOT_ATTEMPT_CAP = 3


def _log_supervisor(payload: Dict[str, Any]) -> None:
    append_jsonl(_g.DRIVE_ROOT / "logs" / "supervisor.jsonl", {"ts": utc_now_iso(), **payload})


def _finalize_pending_boot_smoke(tx: Dict[str, Any], supervisor_ready: bool) -> Dict[str, Any]:
    """Health-check a committed-and-restarted update (auto_merge OR a committed assisted
    merge). Pre-restart smoke already ran inline; this is the post-boot backstop + boot-loop
    guard: clear on healthy boot, roll back to pre_update_sha on a genuine miss / brick-loop."""
    attempts = int(tx.get("boot_attempts") or 0) + 1
    merge_commit = str(tx.get("merge_commit") or "")
    rc_h, head, _he = _g.git_capture(["git", "rev-parse", "HEAD"])
    head_resolved = rc_h == 0 and bool(merge_commit)
    merge_in_head = head_resolved and (
        head == merge_commit
        or _g.git_capture(["git", "merge-base", "--is-ancestor", merge_commit, "HEAD"])[0] == 0
    )
    if bool(supervisor_ready) and merge_in_head:
        # Same ordering rule as the rollback path: the intent is the restart-consumed instruction,
        # so it must be proven gone BEFORE the transaction that explains it. A healthy boot that
        # cleared only the transaction left the intent behind, and the next bootstrap — seeing no
        # transaction — treated the checkout as ordinary and re-applied it. Both removals are
        # PROVEN; a marker still on disk is not a finalized update.
        cleanup_failure = ""
        if not _g._clear_update_intent():
            cleanup_failure = "update intent marker could not be removed"
        elif not clear_update_tx():
            cleanup_failure = "update transaction marker could not be removed"
        if not cleanup_failure:
            _log_supervisor({"type": "managed_update_finalized", "head": head})
            return {"finalized": True}
        # FALL THROUGH to the shared escalation below rather than returning here. Returning skipped
        # the `boot_attempts` counter, which is what the `attempts >= 2` rollback backstop reads —
        # so an unremovable marker, the one failure this branch reports and the one the backstop
        # exists for, deferred identically forever. Now the first boot defers with the counter
        # bumped and the second reaches the rollback.
        _log_supervisor({"type": "managed_update_finalize_deferred", "head": head,
                         "reason": cleanup_failure})
    if (bool(supervisor_ready) and head_resolved and not merge_in_head) or attempts >= 2:
        ok, msg = rollback_managed_update("post_boot_smoke_failed")
        _log_supervisor({"type": "managed_update_rollback_after_failed_boot",
                         "ok": ok, "msg": msg, "boot_attempts": attempts})
        return {"finalized": False, "rolled_back": ok, "msg": msg}
    tx["boot_attempts"] = attempts
    write_update_tx(tx)
    return {"finalized": False, "boot_attempts": attempts}


def _recover_assisted_on_boot(tx: Dict[str, Any], supervisor_ready: bool) -> Dict[str, Any]:
    """Recover an in-flight assisted merge after a restart/rescue — re-keyed on MERGE STATE
    (during resolution HEAD == pre_update_sha, the reviewed base) and strictly non-destructive:
    a real reviewed commit that landed on top is NEVER reset away."""
    state = _assisted_head_state(tx)
    if state == "committed":
        # Crash after commit before the phase flipped: recover by transitioning to the
        # committed-and-verify path (set merge_commit from HEAD if missing).
        if not str(tx.get("merge_commit") or ""):
            rc_h, head, _he = _g.git_capture(["git", "rev-parse", "HEAD"])
            if rc_h == 0:
                tx["merge_commit"] = head
        tx["phase"] = "pending_boot_smoke"
        write_update_tx(tx)
        _log_supervisor({"type": "managed_update_assisted_committed_recovered",
                         "merge_commit": str(tx.get("merge_commit") or "")[:12]})
        return _finalize_pending_boot_smoke(tx, supervisor_ready)
    if state == "diverged":
        # A real reviewed commit landed; keep it (never reset over reviewed work), abandon
        # this update — it is re-planned fresh later. Only a PROVEN removal is an abandonment: a
        # marker still on disk keeps the update active, and the next boot recovers it again.
        if not clear_update_tx():
            _log_supervisor({"type": "managed_update_assisted_abandon_deferred"})
            return {"finalized": False, "msg": "update transaction marker could not be removed"}
        _log_supervisor({"type": "managed_update_assisted_abandoned_diverged"})
        return {"finalized": False, "abandoned": True, "reason": "head_diverged"}
    if state == "in_progress":
        attempts = int(tx.get("resolution_attempts") or 0) + 1
        if attempts > _ASSISTED_BOOT_ATTEMPT_CAP:
            ok, msg = rollback_managed_update("assisted_resolution_expired")
            _log_supervisor({"type": "managed_update_assisted_expired", "ok": ok, "msg": msg})
            return {"finalized": False, "rolled_back": ok, "msg": msg}
        # Re-establish the merge state if the restart/rescue wiped it; preserve partial
        # progress when MERGE_HEAD + a dirty tree already survived.
        rc_d, dirty, _de = _g.git_capture(["git", "status", "--porcelain"])
        has_progress = bool(_merge_head_sha()) and rc_d == 0 and bool(dirty.strip())
        if not has_progress:
            ok, msg = materialize_assisted_merge_live(
                str(tx.get("pre_update_branch") or _g.BRANCH_DEV),
                str(tx.get("local_snapshot") or ""),
                str(tx.get("target_sha") or ""),
                str(tx.get("pre_update_sha") or ""),
            )
            if not ok:
                # Could not re-stage the merge — fail closed to a clean pre-update state.
                rb_ok, rb_msg = rollback_managed_update("assisted_rematerialize_failed")
                _log_supervisor({"type": "managed_update_assisted_rematerialize_failed",
                                 "materialize_msg": msg, "rollback": rb_msg})
                return {"finalized": False, "rolled_back": rb_ok, "msg": msg}
        tx["phase"] = "assisted_resolution"
        tx["resolution_attempts"] = attempts
        write_update_tx(tx)
        try:
            enqueue_assisted_resolution_task(tx)
        except Exception as exc:
            # Boot recovery is deliberately non-destructive: the merge state stays exactly as it is
            # and the NEXT boot retries (bounded by the attempt cap above), rather than rolling a
            # partially-resolved tree back because this boot could not start a pool.
            _log_supervisor({"type": "managed_update_assisted_resume_no_worker",
                             "resolution_attempts": attempts, "error": str(exc)})
            return {"finalized": False, "resumed": False,
                    "reason": "assisted_resolver_unavailable"}
        _log_supervisor({"type": "managed_update_assisted_resumed",
                         "resolution_attempts": attempts, "preserved_progress": has_progress})
        return {"finalized": False, "resumed": True, "resolution_attempts": attempts}
    # unknown: do not touch the tree; leave the tx for the owner / a later boot.
    _log_supervisor({"type": "managed_update_assisted_unknown_state"})
    return {"finalized": False, "reason": "unknown_assisted_state"}


def managed_assisted_tx_for(task_id: str) -> Tuple[Dict[str, Any], str]:
    """For ``commit_reviewed``: return ``(managed_tx, block_message)``. While a managed assisted
    tx is active, ONLY its authorized resolution task may commit. A CORRUPT marker blocks too
    (fail-closed). Returns ``(tx, "")`` for the authorized task, ``({}, msg)`` to block another
    task, ``({}, "")`` when no managed tx is active. The tx marker — never an LLM value — is the
    trust root for the managed merge."""
    status, tx = read_update_tx_strict()
    if status == "absent":
        return {}, ""
    if status == "valid" and str(tx.get("phase") or "") == UPDATE_TX_GATE_BLOCKED:
        # Terminal, and terminal for COMMITS too, not just for boot recovery. The merge under this
        # marker was rejected by a gate and could not be rolled back, so it is still in the tree:
        # an ordinary reviewed commit admitted here would build on the refused revision and push it.
        # Kept out of `_ASSISTED_PHASES` deliberately — no task is authorized to resolve it.
        return {}, (
            "⚠️ MANAGED_UPDATE_GATE_BLOCKED: a managed update was rejected by a gate and could not "
            "be rolled back, so the refused merge is still in the working tree and commits are "
            f"blocked until the owner clears it. Reason: {tx.get('gate_blocked_reason') or 'unknown'}"
        )
    if status == "valid" and str(tx.get("phase") or "") in _ASSISTED_PHASES:
        if str(tx.get("task_id") or "") == str(task_id or ""):
            return tx, ""
    elif status == "valid":
        return {}, ""  # a non-assisted tx (pending_boot_smoke) does not gate commits
    return {}, (
        "⚠️ MANAGED_UPDATE_IN_PROGRESS: a managed update merge is being resolved by another "
        "task (or the update tx is unreadable); commits are blocked until it completes or is "
        "rolled back."
    )


def managed_assisted_precommit_verify(tx: Dict[str, Any]) -> Tuple[bool, str]:
    """Verify the live merge state matches the tx before the reviewed commit: on the expected
    branch, MERGE_HEAD == tx.target_sha, HEAD == tx.pre_update_sha (the reviewed first parent)."""
    branch = str(tx.get("pre_update_branch") or _g.BRANCH_DEV)
    target = str(tx.get("target_sha") or "")
    pre = str(tx.get("pre_update_sha") or "")
    rc_b, cur, _e = _g.git_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if rc_b != 0 or cur != branch:
        return False, f"⚠️ MANAGED_UPDATE_ERROR: on branch {cur!r}, expected {branch!r}"
    mh = _merge_head_sha()
    if mh != target:
        return False, f"⚠️ MANAGED_UPDATE_ERROR: MERGE_HEAD {(mh[:12] or 'absent')} != target {target[:12]}"
    rc_h, head, _he = _g.git_capture(["git", "rev-parse", "--verify", "HEAD"])
    if rc_h != 0 or head != pre:
        return False, f"⚠️ MANAGED_UPDATE_ERROR: HEAD {head[:12]} != reviewed base {pre[:12]}"
    return True, ""


def managed_assisted_marker_check() -> Tuple[bool, str]:
    """Reject leftover conflict markers in the STAGED tree — the PRIMARY leakage gate: once the
    agent `git add`-s a marked file it is a 'resolved' (stage-0) entry, so `--diff-filter=U`
    no longer catches it. Scan the raw staged blob (no diff '+' prefix); flag a file only when
    BOTH a `<<<<<<<` and a `>>>>>>>` marker line are present (avoids false-positives on a lone
    markdown `=======` underline)."""
    import re

    start_re = re.compile(r"^<{7}")
    end_re = re.compile(r"^>{7}")
    rc_n, names, _e = _g.git_capture(["git", "diff", "--cached", "--name-only"])
    bad: List[str] = []
    if rc_n == 0:
        for path in [p for p in names.splitlines() if p.strip()]:
            rc_s, blob, _se = _g.git_capture(["git", "show", f":{path}"])
            if rc_s != 0:
                continue
            lines = blob.splitlines()
            if any(start_re.match(ln) for ln in lines) and any(end_re.match(ln) for ln in lines):
                bad.append(path)
    if bad:
        return False, (
            "⚠️ MANAGED_UPDATE_ERROR: unresolved conflict markers remain in: "
            + ", ".join(bad[:20])
            + " — remove every <<<<<<< / ======= / >>>>>>> before committing."
        )
    return True, ""


def reestablish_merge_head(target_sha: str) -> None:
    """Re-write ``.git/MERGE_HEAD`` so a BLOCKED managed-merge review can be fixed and re-committed
    — the review's index reset (``git reset HEAD``) clears the in-progress merge state, after which
    ``managed_assisted_precommit_verify`` would fail on the agent's retry. Best-effort."""
    if not target_sha:
        return
    try:
        (_g._git_dir() / "MERGE_HEAD").write_text(target_sha + "\n", encoding="utf-8")
    except Exception:
        _g.log.warning("reestablish_merge_head failed", exc_info=True)


def managed_assisted_postcommit(tx: Dict[str, Any], commit_sha: str) -> Tuple[bool, str]:
    """After the reviewed 2-parent merge commit lands: record merge_commit + transition to
    ``pending_boot_smoke``, then run the pre-restart smoke INLINE (auto_merge parity). On smoke
    FAIL roll back to pre_update_sha (the agent's resolution survives on the failed-update tag +
    the rescue-local ref). On PASS the agent calls ``request_restart`` and boot finalize verifies
    the healthy boot. Returns (ok, message)."""
    tx = dict(tx)
    tx["phase"] = "pending_boot_smoke"
    tx["merge_commit"] = commit_sha
    write_update_tx(tx)
    smoke = update_restart_smoke()
    if smoke.get("ok"):
        return True, (
            "✅ Managed update committed as a reviewed 2-parent merge and passed the pre-restart "
            "smoke. Call `request_restart` now to finish landing the update."
        )
    ok, msg = rollback_managed_update("assisted_pre_restart_smoke_failed")
    # Preserve the FULL smoke trace durably (it explains why a self-modifying update rolled
    # back — never silently sliced); the chat message shows a head with an explicit omission note.
    _log_supervisor({
        "type": "managed_update_assisted_smoke_failed", "returncode": smoke.get("returncode"),
        "stdout": str(smoke.get("stdout") or ""), "stderr": str(smoke.get("stderr") or ""),
    })
    stderr = str(smoke.get("stderr") or "")
    shown = stderr if len(stderr) <= 400 else (
        stderr[:400] + f"… (+{len(stderr) - 400} more chars — full trace in data/logs/supervisor.jsonl)"
    )
    return False, (
        "⚠️ MANAGED_UPDATE_SMOKE_FAILED: the merged code failed the pre-restart smoke "
        f"({shown}). Rolled back to the prior version ({msg}). "
        "The resolved merge is preserved on a failed-update-* tag for inspection."
    )


# Phases that END in a process restart: the merge is already committed (``pending_boot_smoke``) or
# the commit is in flight (``committing_assisted``). The restart is what re-admits in-process
# writers there, so nothing else may hand the checkout back while a tx sits in one of them.
_RESTART_PENDING_PHASES = ("committing_assisted", "pending_boot_smoke")


def managed_update_repository_is_recovered() -> bool:
    """Whether the checkout is PROVEN to carry no leftover managed-update mutation: no transaction
    marker, no update INTENT marker, no unmerged index entries and no half-finished merge
    (``MERGE_HEAD``).

    Fail-closed by construction — a check we could not run counts as unproven, because every caller
    uses this to decide whether it is safe to let writers back onto the checkout at all. The intent
    counts as leftover mutation even though it changes no file: it is consumed by the next
    `checkout_and_reset`, so reopening the checkout with one still on disk hands writers a tree that
    is scheduled to be hard-reset out from under them.
    """
    try:
        if active_update_tx():
            return False
        if _g._update_intent_marker_path().exists():
            return False
        rc_u, unmerged, _ue = _g.git_capture(["git", "diff", "--name-only", "--diff-filter=U"])
        if rc_u != 0 or unmerged.strip():
            return False
        rc_m, _mo, _me = _g.git_capture(["git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD"])
        return rc_m != 0  # a resolvable MERGE_HEAD means the staged merge is still in the tree
    except Exception:
        _g.log.warning("managed update: could not verify repository recovery", exc_info=True)
        return False


def _worker_fence_survivors_present() -> bool:
    """Whether a managed-update fence latched a worker it could NOT prove dead.

    Fail CLOSED: a latch we cannot read is not evidence that nothing survived.
    """
    try:
        from supervisor.workers import unproven_worker_survivors

        return bool(unproven_worker_survivors())
    except Exception:
        _g.log.warning("managed update: could not read the worker survivor latch", exc_info=True)
        return True


def _repo_writer_blockers_present() -> bool:
    """Whether a managed-update fence latched a repository-rooted SERVICE it could not clear.

    The blocked refusal is the survivor refusal for a writer that is not a worker, so it needs the
    same treatment here. Fail CLOSED for the same reason.
    """
    try:
        from supervisor.workers import latched_repo_writer_blockers

        return bool(latched_repo_writer_blockers())
    except Exception:
        _g.log.warning("managed update: could not read the repo writer blocker latch", exc_info=True)
        return True


def _reconcile_repo_writer_admission_locked() -> Dict[str, Any]:
    """The admission-reconcile DECISION. The caller must already hold the managed-update lock (or
    be certain no apply can run), because the tx marker read below is only meaningful under it."""
    if not _repo_writer_admission_is_closed():
        return {"reopened": False, "reason": "already_open"}
    if _worker_fence_survivors_present():
        # A survivor fence stages nothing and writes no transaction, so every other condition below
        # reads as "no update owns this closure" and the reconcile would hand the checkout back
        # inside a minute — contradicting the 409 that told the owner it stays disabled until the
        # server is restarted. The latch is the only thing that remembers that case (``WORKERS`` was
        # cleared, the update lock was released), so it is what has to be consulted here.
        return {"reopened": False, "reason": "worker_fence_survivors"}
    if _repo_writer_blockers_present():
        # Same case, other writer class: a blocked fence stages nothing and writes no transaction
        # either, so without this the reconcile would hand the checkout back to new writers while an
        # arbitrary service command is still running inside it.
        return {"reopened": False, "reason": "repo_service_fence_blocked"}
    status, tx = read_update_tx_strict()
    if status != "absent":  # a CORRUPT marker counts as live: fail closed
        phase = str(tx.get("phase") or "")
        return {
            "reopened": False,
            "reason": "restart_pending" if phase in _RESTART_PENDING_PHASES else "update_active",
        }
    if not managed_update_repository_is_recovered():
        return {"reopened": False, "reason": "repository_unrecovered"}
    try:
        from supervisor.workers import open_repo_writer_admission

        open_repo_writer_admission()
    except Exception:
        _g.log.warning("managed update: could not re-open writer admission", exc_info=True)
        return {"reopened": False, "reason": "reopen_failed"}
    _g.log.info("managed update: re-opened repository writer admission (no update owns it)")
    return {"reopened": True, "reason": "no_active_update"}


def reconcile_repo_writer_admission() -> Dict[str, Any]:
    """Re-open in-process writer admission when nothing owns the closure any more — the OWNER OF
    LAST RESORT for a flag the fence closes process-wide.

    The fence shuts direct and ephemeral chat out, and for most of an update's life exactly one
    event gives that back. A single missed call therefore made the lockout permanent: a resolver
    worker that dies without emitting ``task_done`` never reaches the watchdog at all, and the
    watchdog's own early returns (the tx already went away, a phase the apply never reached) decline
    without re-admitting anyone. Availability rather than safety — the closed flag is fail-closed —
    but nothing self-heals it, and no restart is pending to clear it.

    Safe to call on a timer: it re-opens ONLY when admission is closed, no worker survived a fence,
    no managed-update transaction is live, and the repository is provably recovered.

    The update lock is what makes the marker read trustworthy. An apply holds it across the fence,
    the tx write and the apply itself, so without it a reconcile could slip into the window between
    "admission closed" and "marker written", see an absent tx on a still-clean tree, and re-admit
    chat writers BEHIND the fence — precisely the race the fence exists to close. A held lock is
    therefore an answer, not an obstacle: an update is in flight and legitimately owns the closure.
    """
    if not _repo_writer_admission_is_closed():
        return {"reopened": False, "reason": "already_open"}  # avoid the lock in the common case
    lock_fh = None
    try:
        try:
            lock_fh = acquire_update_lock()
        except RuntimeError:
            return {"reopened": False, "reason": "update_in_flight"}
        return _reconcile_repo_writer_admission_locked()
    except Exception:
        _g.log.warning("managed update: writer-admission reconcile failed", exc_info=True)
        return {"reopened": False, "reason": "reconcile_failed"}
    finally:
        if lock_fh is not None:
            release_update_lock(lock_fh)


def _repo_writer_admission_is_closed() -> bool:
    """Read the admission flag without letting an unreachable worker module raise into a caller
    whose whole job is best-effort self-healing."""
    try:
        from supervisor.workers import repo_writer_admission_closed

        return bool(repo_writer_admission_closed())
    except Exception:
        _g.log.debug("managed update: could not read writer admission", exc_info=True)
        return False


def abort_orphaned_assisted_tx(task_id: str) -> Dict[str, Any]:
    """Watchdog called when a task ENDS: if it was the authorized assisted-resolution task and
    the tx is still mid-resolution (the merge never committed — failed / cancelled / gave up),
    roll back to pre_update_sha so the live worktree AND the commit-exclusivity guard are freed
    immediately (no starvation until a restart). A COMMITTED merge (phase pending_boot_smoke) or
    an in-flight commit (committing_assisted) is left for the restart / boot finalize.

    It is also one of the places that hands IN-PROCESS writer admission back, so every branch that
    declines to act has to answer for it: a branch that walks away from a closed flag nobody else
    owns leaves direct and ephemeral chat refused for the life of the process. The branches that
    decline because the assisted tx is no longer live therefore run the reconcile, which re-admits
    only when no update owns the closure. The lock-contention branch deliberately does not — an
    apply IS in flight there and owns it — and the periodic maintenance reconcile covers the case
    where that apply ends without re-opening."""
    status, tx = read_update_tx_strict()
    if status != "valid" or str(tx.get("phase") or "") not in ("materializing_assisted", "assisted_resolution"):
        return {"acted": False, "admission": reconcile_repo_writer_admission()}
    if str(tx.get("task_id") or "") != str(task_id or ""):
        return {"acted": False, "admission": reconcile_repo_writer_admission()}
    lock_fh = None
    try:
        try:
            lock_fh = acquire_update_lock()
        except RuntimeError:
            return {"acted": False, "reason": "lock held by an active apply"}
        s2, tx2 = read_update_tx_strict()  # re-read under the lock (it may have just committed)
        if s2 != "valid" or str(tx2.get("phase") or "") not in ("materializing_assisted", "assisted_resolution"):
            return {"acted": False, "admission": _reconcile_repo_writer_admission_locked()}
        if str(tx2.get("task_id") or "") != str(task_id or ""):
            return {"acted": False, "admission": _reconcile_repo_writer_admission_locked()}
        ok, msg = rollback_managed_update("assisted_resolution_orphaned")
        # `rollback_managed_update` returns False when it could not restore the pre-update checkout,
        # so its boolean is the gate on everything below — and it is not enough on its own: the tree
        # is re-verified independently before a single writer is let back in.
        recovered = bool(ok) and managed_update_repository_is_recovered()
        _log_supervisor({"type": "managed_update_assisted_orphaned_rollback",
                         "ok": ok, "msg": msg, "recovered": recovered})
        if not recovered:
            # The checkout may still hold the failed assisted merge, its conflict markers or a live
            # transaction. Re-admitting writers would drop direct/ephemeral chat turns straight into
            # it, and starting the general pool would add workers to the same tree. Both stay down
            # for the restart / boot recovery path, which is the only thing that can finish this.
            _g.log.error(
                "abort_orphaned_assisted_tx: rollback not proven (ok=%s, %s); writer admission "
                "stays closed and the pool stays down until a restart", ok, msg,
            )
            return {"acted": True, "rolled_back": bool(ok), "recovered": False,
                    "reason": "update_recovery_failed", "msg": msg}
        try:
            from supervisor.workers import ensure_worker_pool_started, open_repo_writer_admission

            # The apply closed IN-PROCESS writer admission for the fence and deliberately held it
            # closed through the whole resolution — the resolver was the only writer authorized on
            # a worktree carrying MERGE_HEAD. This is where a resolution that never committed ends
            # and the rollback above has been VERIFIED to have restored the tree, so this is where
            # direct chat gets the checkout back. Done before the pool start, which may itself fail.
            open_repo_writer_admission()
            if not ensure_worker_pool_started(allow_disabled_restart=True):
                _g.log.warning(
                    "abort_orphaned_assisted_tx: worker pool remains explicitly disabled"
                )
        except Exception:
            _g.log.warning("abort_orphaned_assisted_tx: worker pool start failed", exc_info=True)
        return {"acted": True, "rolled_back": ok, "recovered": True, "msg": msg}
    finally:
        if lock_fh is not None:
            release_update_lock(lock_fh)


def finalize_managed_update_on_boot(supervisor_ready: bool = True) -> Dict[str, Any]:
    """Post-boot finalization of a managed update (P2). Called ONCE after the new process
    boots and the supervisor is ready. Acquires the update lock (skips if an apply holds it),
    strict-reads the tx, and dispatches by phase: ``pending_boot_smoke`` (committed +
    restarted) → health-check + boot-loop guard; an assisted phase → non-destructive
    merge-state recovery (resume / abandon-on-divergence / rollback-on-expiry). A CORRUPT
    marker fails closed (left for the owner). Best-effort; never raises."""
    lock_fh = None
    try:
        try:
            lock_fh = acquire_update_lock()
        except RuntimeError:
            return {"finalized": False, "reason": "update lock held by an active apply"}
        status, tx = read_update_tx_strict()
        if status == "absent":
            return {"finalized": False, "reason": "no pending update"}
        if status == "corrupt":
            _log_supervisor({"type": "managed_update_tx_corrupt_on_boot"})
            return {"finalized": False, "reason": "corrupt tx marker — left for owner"}
        phase = str(tx.get("phase") or "")
        if phase == "pending_boot_smoke":
            return _finalize_pending_boot_smoke(tx, supervisor_ready)
        if phase == UPDATE_TX_GATE_BLOCKED:
            # Terminal by construction. A gate rejected this update and the rollback that should
            # have erased the tx failed, so the merge is still in the tree — advancing it is the
            # one thing the gate refused. Recovery stops and leaves the marker for the owner.
            _log_supervisor({"type": "managed_update_gate_blocked_on_boot",
                             "reason": str(tx.get("gate_blocked_reason") or "")})
            return {"finalized": False, "reason": "gate_blocked — left for owner"}
        if phase in _ASSISTED_PHASES:
            return _recover_assisted_on_boot(tx, supervisor_ready)
        return {"finalized": False, "reason": f"unhandled phase {phase}"}
    except Exception:
        _g.log.warning("finalize_managed_update_on_boot failed", exc_info=True)
        return {"finalized": False, "error": "exception"}
    finally:
        if lock_fh is not None:
            release_update_lock(lock_fh)
