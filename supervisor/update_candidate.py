"""Managed-update CANDIDATE primitives: private-index tree serialization, the
pinned mechanical-merge baseline's merge flags, the deterministic failed-update
preservation branch, and the single-run tests-evidence contract (Q10).

Split out of ``supervisor.update_merge`` by the module-size discipline; the
import direction is one-way (``update_merge`` imports THIS module and re-exports
the names its callers and tests already use). Functions that need the update-tx
marker import ``update_merge`` lazily at call time — never at module level.
Depends on ``git_ops`` via the module object (``_g.X``) so monkeypatched
``REPO_DIR``/``DRIVE_ROOT`` are followed.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from supervisor import git_ops as _g

_MERGE_NEUTRAL_FLAGS = ("-c", "rerere.enabled=false")
"""Every mechanical merge in the update flow disables rerere explicitly: the live
deployment may carry ``rerere.enabled=true`` plus a populated rr-cache in the COMMON
git dir (shared by linked worktrees), and a remembered resolution replayed into one
invocation but not another would make the plan merge, the materialized merge, and
the pinned M0 baseline silently disagree. Merge drivers from .gitattributes are
deliberately NOT neutralized — they are part of the repository's own contract and
apply identically to every invocation."""


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


def _rev_parse(ref: str) -> str:
    rc, out, _e = _g.git_capture(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"])
    return out if rc == 0 else ""


def _merge_head_sha() -> str:
    rc, out, _e = _g.git_capture(["git", "rev-parse", "--verify", "-q", "MERGE_HEAD"])
    return out if rc == 0 else ""


def worktree_snapshot_tree(base_ref: str, *, cwd: Optional[str] = None) -> Tuple[str, str]:
    """Serialize the LIVE worktree (tracked edits, deletions, untracked files —
    conflict markers as plain file content) into a tree object via a PRIVATE
    index. The repository's real index (which may hold unmerged conflict stages
    that ``git write-tree`` refuses to serialize) and the worktree itself are
    never touched. Returns ``(tree_sha, error)``."""
    import tempfile

    fd, tmp_index_path = tempfile.mkstemp(prefix="ouro-snapshot-index-")
    os.close(fd)
    # `git read-tree` wants a NON-existent index path.
    os.unlink(tmp_index_path)
    env = {"GIT_INDEX_FILE": tmp_index_path}
    try:
        rc_r, _ro, read_error = _git_run(["git", "read-tree", base_ref], cwd=cwd, extra_env=env)
        if rc_r != 0:
            return "", read_error or f"read-tree {base_ref[:12]} failed"
        rc_a, _ao, add_error = _git_run(["git", "add", "-A"], cwd=cwd, extra_env=env)
        if rc_a != 0:
            return "", add_error or "git add -A failed"
        rc_w, tree, write_error = _git_run(["git", "write-tree"], cwd=cwd, extra_env=env)
        if rc_w != 0 or not tree:
            return "", write_error or "git write-tree failed"
        return tree, ""
    finally:
        try:
            os.unlink(tmp_index_path)
        except OSError:
            pass


def live_unmerged_paths() -> Optional[List[str]]:
    """The live repo's unmerged (conflicted) paths; ``None`` when Git itself
    failed (an unreadable inventory must never masquerade as "no conflicts" —
    callers keep their previous list or fail safe)."""
    rc_u, unmerged_out, _ue = _g.git_capture(["git", "diff", "--name-only", "--diff-filter=U"])
    if rc_u != 0:
        return None
    return [ln.strip() for ln in unmerged_out.splitlines() if ln.strip()]


def existing_failed_update_ref(target_sha: str, *, not_at: str = "") -> str:
    """Return ``failed-update-<target12>`` when a prior attempt at this exact target
    left its preserved branch behind (a retry's resolver is pointed at it), else "".
    ``not_at`` filters out a branch that merely sits on the given sha (e.g. the
    pre-update base): such a ref preserves no attempt and must not be advertised."""
    if not target_sha:
        return ""
    name = f"failed-update-{target_sha[:12]}"
    resolved = _rev_parse(name)
    if not resolved or (not_at and resolved == not_at):
        return ""
    return name


def _preserve_failed_update_attempt(tx: Dict[str, Any]) -> str:
    """Publish the failed attempt on the DETERMINISTIC branch ``failed-update-<target12>``
    (keyed by the update target, so a retry of the same update overwrites one branch
    instead of scattering per-attempt refs) BEFORE the destructive reset. An UNCOMMITTED
    resolution (dirty tree and/or live MERGE_HEAD) is first serialized as a synthetic
    commit via a private index — plain ``write-tree`` is fatal on an unmerged index —
    with the natural parents ``[HEAD, MERGE_HEAD]`` (during resolution HEAD is the
    reviewed pre-update base and MERGE_HEAD is the target, preserving the [local,
    target] order). A committed candidate is preserved as-is. Best-effort by design:
    on any failure returns "" and the rescue snapshot remains the carrier of record —
    rollback itself never depends on this ref."""
    target = str(tx.get("target_sha") or "")
    rc_h, head, _he = _g.git_capture(["git", "rev-parse", "--verify", "HEAD"])
    if rc_h != 0 or not head:
        return ""
    name = f"failed-update-{target[:12]}" if target else f"failed-update-{head[:12]}"
    rc_s, dirty, _se = _g.git_capture(["git", "status", "--porcelain"])
    merge_head = _merge_head_sha()
    # Nothing beyond the pre-update state exists: a clean tree at the pre-update
    # sha with no live merge is NOT an attempt. Touching the branch here would
    # either mint a junk ref (rollback before any materialization) or — worse —
    # let a REPLAYED rollback (crash after the destructive reset) overwrite a
    # real preserved attempt with the bare base. Leave whatever the branch
    # already holds.
    if (
        head == str(tx.get("pre_update_sha") or "")
        and rc_s == 0 and not dirty.strip()
        and not merge_head
    ):
        return name if _rev_parse(name) else ""
    keep = head
    if (rc_s == 0 and dirty.strip()) or merge_head:
        tree, tree_error = worktree_snapshot_tree("HEAD")
        if not tree:
            _g.log.warning("failed-update snapshot tree failed: %s", tree_error)
            return ""
        parents: List[str] = ["-p", head]
        if merge_head:
            parents += ["-p", merge_head]
        rc_c, synthetic, commit_error = _git_run(
            ["git", "commit-tree", tree, *parents,
             "-m", f"ouroboros failed-update attempt (target {target[:12] or 'unknown'})"],
        )
        if rc_c != 0 or not synthetic:
            _g.log.warning("failed-update synthetic commit failed: %s", commit_error)
            return ""
        keep = synthetic
    rc_b, _bo, _be = _g.git_capture(["git", "branch", "-f", name, keep])
    if rc_b != 0 or _rev_parse(name) != keep:
        return ""
    return name


def record_managed_tests_evidence(
    task_id: str, task_metadata: Optional[Dict[str, Any]] = None
) -> str:
    """After a GREEN full hermetic pytest run inside the authorized resolver's flow,
    pin the exact candidate tree the suite ran against (the live worktree projection —
    what ``run_hermetic_pytest`` actually tests) into the tx as ``tests_evidence``.
    The managed commit gate then reuses this proof instead of paying a second full
    run when the committed tree is byte-identical (single-run contract, Q10). Skips
    recording when the suite was env-disabled (no run happened — recording would
    forge a proof). Returns the recorded tree sha, '' when not applicable."""
    if os.environ.get("OUROBOROS_PRE_PUSH_TESTS", "1") != "1":
        return ""
    from supervisor import update_merge as _um

    tx = _um.authorized_assisted_task(task_id, task_metadata)
    if not tx:
        return ""
    # Fidelity guard: the hermetic runner materializes UNTRACKED files by
    # copying, which does not faithfully reproduce symlinks or other
    # non-regular entries — while `git add -A` records them as symlink blobs.
    # A tree hash covering content the suite never saw would be a forged
    # proof, so such candidates simply keep the mandatory gate run instead.
    rc_u, untracked_out, _uue = _g.git_capture(
        ["git", "ls-files", "--others", "--exclude-standard"]
    )
    if rc_u != 0:
        return ""
    for rel in untracked_out.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        full = os.path.join(str(_g.REPO_DIR), rel)
        if os.path.islink(full) or not os.path.isfile(full):
            _g.log.info("managed tests evidence: non-regular untracked entry %s — no proof recorded", rel)
            return ""
    tree, tree_error = worktree_snapshot_tree("HEAD")
    if not tree:
        _g.log.warning("managed tests evidence: snapshot failed: %s", tree_error)
        return ""
    from ouroboros.utils import utc_now_iso

    tx["tests_evidence"] = {"tree": tree, "at": utc_now_iso()}
    _um.write_update_tx(tx)
    return tree


def update_tx_phase(base_tx: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge-write a managed phase transition onto the FRESH durable tx.

    The commit flow holds a tx snapshot taken at attempt start, while
    in-attempt writers merge keys into the DURABLE marker mid-attempt (e.g.
    the compensating tests preflight recording ``tests_evidence``). Writing
    the stale snapshot wholesale silently drops those keys — the post-commit
    gate would then re-buy the full hermetic suite it already holds proof for
    (and a flaky red there rolls back a green-proven candidate). This helper
    re-reads the durable tx and applies ONLY the caller's intended key changes
    on top, refusing to drop keys the durable record carries. When the durable
    marker is absent or corrupt, the caller's snapshot is the only substrate
    left — fall back to it (downstream strict readers still fail closed on a
    corrupt marker). Returns the tx dict actually written."""
    from supervisor import update_merge as _um

    status, current = _um.read_update_tx_strict()
    merged = dict(current) if status == "valid" else dict(base_tx)
    merged.update(patch)
    _um.write_update_tx(merged)
    return merged


def managed_tests_evidence_covers(committed_tree: str) -> bool:
    """True when the durable tx carries a green-suite proof for EXACTLY this tree."""
    if not committed_tree:
        return False
    from supervisor import update_merge as _um

    status, tx = _um.read_update_tx_strict()
    if status != "valid":
        return False
    evidence = tx.get("tests_evidence")
    return isinstance(evidence, dict) and str(evidence.get("tree") or "") == committed_tree


def lookup_update_stash(attempt_id: str) -> Tuple[bool, str, str]:
    """Typed stash lookup: ``(ok, sha, error)``. ``ok=False`` means the stash
    STORAGE could not be read — callers must treat that as UNKNOWN and keep
    their durable pointers, never as "nothing was stashed" (ok=True, sha="")."""
    marker = f"managed-update-{attempt_id}"
    rc_l, listing, list_error = _g.git_capture(["git", "stash", "list", "--format=%H %gs"])
    if rc_l != 0:
        return False, "", list_error or "git stash list failed"
    for line in listing.splitlines():
        sha, _sep, subject = line.strip().partition(" ")
        if marker in subject:
            return True, sha, ""
    return True, "", ""


def find_update_stash_sha(attempt_id: str) -> str:
    """Legacy shape over ``lookup_update_stash`` (error collapses to "")."""
    ok, sha, _error = lookup_update_stash(attempt_id)
    return sha if ok else ""

def stash_local_changes_for_update(attempt_id: str) -> Tuple[str, str, str]:
    """Stash tracked+untracked local work before a clean auto-update apply
    (owner decision Q1=C: dirty work rides the stash, never committed history).
    Returns ``(status, stash_sha, error)`` with status in {"ok", "push_failed",
    "lookup_unknown"}; "ok" with an empty sha means nothing to stash, and
    "lookup_unknown" means the entry EXISTS but could not be listed — callers
    must keep their durable pointers for a later retry."""
    marker = f"managed-update-{attempt_id}"
    rc, _out, error = _g.git_capture(
        ["git", "stash", "push", "--include-untracked", "-m", marker]
    )
    if rc != 0:
        return "push_failed", "", error or "git stash push failed"
    ok_l, sha, lookup_error = lookup_update_stash(attempt_id)
    if not ok_l:
        # The push succeeded but the entry cannot be listed: the entry EXISTS
        # somewhere — the caller must KEEP its durable tx pointer so boot can
        # retry the lookup, never clear it as if nothing was stashed.
        return "lookup_unknown", "", (
            "git stash list failed after a successful push; the entry named "
            f"{marker} exists — recovery retries on the next boot — "
            + (lookup_error or "unreadable stash")
        )
    if sha:
        return "ok", sha, ""
    # "No local changes to save" — the worktree raced clean; nothing to restore
    # later. The caller fail-closes if the tree still reports dirty.
    return "ok", "", ""

def restore_update_stash(
    stash_sha: str, context: str = "", on_applied=None
) -> Tuple[bool, str]:
    """Apply-then-drop the exact update stash entry (matched by SHA).

    On an apply conflict the partial apply is reset away and the stash entry is
    KEPT, so local work is never lost — the returned note tells the owner the
    exact `git stash apply` command. Restoring onto the pre-update tree (the
    rollback path) always applies cleanly because the stash was taken there.
    apply+drop (NOT pop) keeps this crash-idempotent: dying between apply and
    drop leaves the entry in place for the replay. ``on_applied`` runs between
    the successful apply and the drop so the caller can persist a durable
    "restored" marker that survives a crash in that window."""
    if not stash_sha:
        return True, ""
    rc_l, listing, list_error = _g.git_capture(["git", "stash", "list", "--format=%H %gd"])
    if rc_l != 0:
        return False, list_error or "could not list stash entries"
    ref = ""
    for line in listing.splitlines():
        sha, _sep, name = line.strip().partition(" ")
        if sha == stash_sha and name:
            ref = name
            break
    if not ref:
        return True, (
            "stash entry not found in the stash list (already consumed after a "
            "verified restore, or dropped externally)"
        )
    # Apply by EXACT SHA (git stash apply accepts any stash-shaped commit): a
    # concurrent stash push/drop can shift the stash@{n} selector between the
    # list and the apply, and a selector-based apply would then touch someone
    # else's entry.
    rc_p, _po, apply_error = _g.git_capture(["git", "stash", "apply", stash_sha])
    if rc_p == 0:
        if on_applied is not None:
            try:
                on_applied()
            except Exception:
                _g.log.warning("restore_update_stash on_applied hook failed", exc_info=True)
        # Drop needs a selector, and selectors shift under concurrent stash
        # traffic. Drop ONLY when the list is byte-identical to the pre-apply
        # snapshot (nothing external happened) and the selector still names our
        # exact SHA; otherwise keep the entry — an undropped own entry is
        # harmless litter, a dropped foreign entry is someone's lost work.
        rc_l2, listing2, _le2 = _g.git_capture(["git", "stash", "list", "--format=%H %gd"])
        drop_ref = ""
        if rc_l2 == 0 and listing2 == listing:
            for line in listing2.splitlines():
                sha2, _sep2, name2 = line.strip().partition(" ")
                if sha2 == stash_sha and name2:
                    drop_ref = name2
                    break
        if drop_ref:
            _g.git_capture(["git", "stash", "drop", drop_ref])
        else:
            _g.log.info("stash entry %s kept (list changed during restore)", stash_sha[:12])
        from supervisor.update_merge import _log_supervisor

        _log_supervisor({
            "type": "managed_update_stash_restored",
            "context": context,
            "stash_sha": stash_sha,
        })
        return True, "local changes restored"
    _g.git_capture(["git", "reset", "--hard", "HEAD"])
    _g.git_capture(["git", "clean", "-fd"])
    note = (
        "local changes could not be restored automatically "
        f"({(apply_error or '').strip() or 'conflict with the updated tree'}); they are "
        f"preserved in git stash entry {stash_sha[:12]} — recover with "
        f"`git stash apply {stash_sha}`"
    )
    from supervisor.update_merge import _log_supervisor

    _log_supervisor({
        "type": "managed_update_stash_restore_failed",
        "context": context,
        "stash_sha": stash_sha,
        "error": (apply_error or "").strip(),
    })
    return False, note

def restore_stash_with_marker(tx: Dict[str, Any], context: str) -> str:
    """Marker-guarded stash restore for every tx-clearing path: skips when a prior
    attempt already restored (a crash between apply and drop must not let a REPLAY
    conflict against the already-restored copy and reset it away), and persists the
    ``stash_restored`` marker between apply and drop. Returns the disclosure note."""
    stash_sha = str(tx.get("stash_sha") or "")
    if not stash_sha or bool(tx.get("stash_restored")):
        return ""
    # Restoring onto a DIRTY tree is never safe: a conflicting apply's cleanup
    # (reset --hard + clean -fd) would wipe whatever made the tree dirty — late
    # human edits in an abort-unwind, an operator's work on a diverged head.
    # Rollback/finalize call this on verified-clean trees, so this guard only
    # fires where the destructive cleanup would actually cost something.
    rc_s, dirty, _se = _g.git_capture(["git", "status", "--porcelain"])
    if rc_s != 0 or dirty.strip():
        return (
            "local changes are present, so the stashed work was NOT auto-applied; it is "
            f"preserved in git stash entry {stash_sha[:12]} — recover with "
            f"`git stash apply {stash_sha}`"
        )

    def _mark() -> None:
        from supervisor import update_merge as _um

        tx["stash_restored"] = True
        _um.write_update_tx(tx)

    _restored, note = restore_update_stash(stash_sha, context=context, on_applied=_mark)
    return note


def destructive_apply_guard(branch: str, pre_update_sha: str) -> str:
    """Return "" when the live checkout is EXACTLY the state the apply was
    planned against, else the human-readable reason. Called under the update
    lock IMMEDIATELY before the first destructive command of an apply or a
    first materialization: the writer fence stops Ouroboros's own writers, not
    humans, and the resolver-boot wait leaves a seconds-to-minutes window where
    a late edit or commit would otherwise be reset/checked-out away without
    riding the stash or the rescue."""
    rc_b, cur, _be = _g.git_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if rc_b != 0 or cur != branch:
        return f"on branch {cur!r}, expected {branch!r}"
    rc_h, head, _he = _g.git_capture(["git", "rev-parse", "--verify", "HEAD"])
    if rc_h != 0 or head != pre_update_sha:
        return f"HEAD moved to {(head or 'unknown')[:12]} (expected {pre_update_sha[:12]})"
    rc_s, dirty, _se = _g.git_capture(["git", "status", "--porcelain"])
    if rc_s != 0:
        return "could not read the worktree status"
    if dirty.strip():
        return "local changes appeared after the stash"
    if _merge_head_sha():
        return "a merge is already in progress"
    return ""


def project_version_carriers(target_sha: str, *, cwd: Optional[str] = None) -> Tuple[bool, str, str]:
    """Mandatory Q8 projection, shared by BOTH lanes: VERSION := the target's
    blob (conflicted or fork-only-drifted alike), then every purely mechanical
    carrier token is rewritten by the release-sync SSOT and staged — only for
    files that are not conflicted (adding an unmerged file would silently
    "resolve" it). Returns ``(ok, note, error)``; ok=False means the projection
    could not be completed and the caller must NOT freeze the half-projected
    tree as a merge commit or an M0 baseline (typed failure over silent drift).
    A target without a VERSION blob mandates nothing (ok=True, empty note)."""
    workdir = str(cwd or _g.REPO_DIR)
    rc_tv, target_blob, _tve = _git_run(["git", "rev-parse", f"{target_sha}:VERSION"], cwd=workdir)
    if rc_tv != 0 or not target_blob:
        return True, "", ""
    note = ""
    rc_sv, staged_blob, _sve = _git_run(["git", "rev-parse", ":VERSION"], cwd=workdir)
    if rc_sv != 0 or staged_blob != target_blob:
        rc_co, _co, co_error = _git_run(["git", "checkout", target_sha, "--", "VERSION"], cwd=workdir)
        if rc_co != 0:
            return False, "", f"VERSION checkout failed: {co_error}"
        note = ", VERSION projected to the target's version"
    rc_u, unmerged_out, u_error = _git_run(
        ["git", "diff", "--name-only", "--diff-filter=U"], cwd=workdir
    )
    if rc_u != 0:
        return False, note, f"conflict inventory unreadable: {u_error}"
    unmerged = {ln.strip() for ln in unmerged_out.splitlines() if ln.strip()}
    try:
        from ouroboros.tools.release_sync import sync_release_metadata

        changed_carriers = list(sync_release_metadata(workdir) or [])
    except Exception as exc:
        return False, note, f"carrier token sync failed: {type(exc).__name__}: {exc}"
    addable = [p for p in changed_carriers if p not in unmerged]
    if addable:
        rc_ca, _cao, ca_error = _git_run(["git", "add", "--", *addable], cwd=workdir)
        if rc_ca != 0:
            return False, note, f"carrier staging failed: {ca_error}"
        note += f", {len(addable)} carrier file(s) token-synced"
    # POSTCONDITION: the sync SSOT silently no-ops on shapes it does not
    # recognize (invalid VERSION, exotic token spellings). Verify every
    # NON-conflicted carrier actually agrees with the projected VERSION; a
    # carrier the sync cannot fix is a typed failure, never a silent success.
    try:
        from ouroboros.tools.release_sync import version_carrier_desyncs

        import pathlib as _pl

        root = _pl.Path(workdir)

        def _read(rel: str) -> str:
            path = root / rel
            return path.read_text(encoding="utf-8") if path.exists() and rel not in unmerged else ""

        version_str = (root / "VERSION").read_text(encoding="utf-8").strip()
        from ouroboros.tools.release_sync import is_release_version

        if not is_release_version(version_str):
            # Nothing enforceable for a non-release version string; the sync
            # SSOT skipped it too — that is the target's own shape, not drift.
            return True, note, ""
        readme_text = _read("README.md")
        desyncs = version_carrier_desyncs(
            version_str,
            pyproject_text=_read("pyproject.toml"),
            uv_lock_text=_read("uv.lock"),
            web_package_text=_read("web/package.json"),
            readme_text=readme_text,
            arch_text=_read("docs/ARCHITECTURE.md"),
            api_types_text=_read("web/modules/api_types.js"),
            download_readme_text=readme_text,
            site_install_text=_read("site/install/index.html"),
            docs_install_text=_read("docs/install/index.html"),
            detailed=True,
        )
    except Exception as exc:
        return False, note, f"carrier postcondition check failed: {type(exc).__name__}: {exc}"
    if desyncs:
        return False, note, "carriers still desynced after token sync: " + "; ".join(desyncs)
    return True, note, ""
