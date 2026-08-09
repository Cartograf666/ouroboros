"""Durable thread worktrees: no age GC, no force reset, inspected removal.

The subagent worktree machinery cannot back these. Its provisioning
force-removes a stale checkout and branch, its removal is unconditional
``--force``, and its startup sweep deletes on retention age alone — every one
of which would silently destroy an owner's branched-off work. These tests pin
the inverted guarantees and the registry separation that makes the age sweep
structurally unable to see a thread worktree.
"""

from __future__ import annotations

import subprocess

import pytest

from ouroboros.thread_worktrees import (
    get_thread_worktree,
    inspect_thread_worktree,
    list_thread_worktrees,
    provision_thread_worktree,
    remove_thread_worktree,
)


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-m", "seed")
    return root


@pytest.fixture()
def wt_root(tmp_path):
    return tmp_path / "thread_worktrees"


def _provision(repo, tmp_path, wt_root, thread_id=1, base_ref=""):
    return provision_thread_worktree(
        repo_dir=repo,
        project_id="racer",
        thread_id=thread_id,
        base_ref=base_ref,
        data_dir=tmp_path / "data",
        worktree_root=wt_root,
    )


def test_provision_registers_a_durable_checkout(repo, tmp_path, wt_root):
    handle = _provision(repo, tmp_path, wt_root)

    from pathlib import Path

    assert Path(handle.path).is_dir()
    assert (Path(handle.path) / "seed.txt").read_text(encoding="utf-8") == "seed\n"
    assert handle.branch.startswith("thread/")
    stored = get_thread_worktree(tmp_path / "data", "racer", 1)
    assert stored["path"] == handle.path
    assert stored["base_sha"] == handle.base_sha
    assert len(list_thread_worktrees(tmp_path / "data")) == 1


def test_provision_refuses_instead_of_force_resetting(repo, tmp_path, wt_root):
    """The subagent path clears a stale checkout+branch before creating. Doing
    that here would delete an owner's uncommitted work without a word."""
    handle = _provision(repo, tmp_path, wt_root)
    (tmp_path / "unrelated").mkdir()

    from pathlib import Path

    (Path(handle.path) / "work.txt").write_text("precious\n", encoding="utf-8")

    with pytest.raises(ValueError, match="already has a worktree"):
        _provision(repo, tmp_path, wt_root)
    assert (Path(handle.path) / "work.txt").read_text(encoding="utf-8") == "precious\n"


def test_provision_refuses_an_existing_branch(repo, tmp_path, wt_root):
    _git(repo, "branch", "thread/racer__2")
    with pytest.raises(ValueError, match="already exists"):
        _provision(repo, tmp_path, wt_root, thread_id=2)
    assert list_thread_worktrees(tmp_path / "data") == []


def test_removal_refuses_dirty_or_unmerged_work_until_acknowledged(repo, tmp_path, wt_root):
    handle = _provision(repo, tmp_path, wt_root)

    from pathlib import Path

    (Path(handle.path) / "draft.txt").write_text("unsaved\n", encoding="utf-8")

    refused = remove_thread_worktree(
        data_dir=tmp_path / "data", project_id="racer", thread_id=1,
        worktree_root=wt_root,
    )
    assert refused["removed"] is False
    assert refused["reason"] == "unmerged_work"
    assert refused["inspection"]["dirty"] is True
    assert Path(handle.path).is_dir()

    # Commit it: now the tree is clean but the commits never reached the base.
    _git(Path(handle.path), "config", "user.email", "t@example.com")
    _git(Path(handle.path), "config", "user.name", "T")
    _git(Path(handle.path), "add", "draft.txt")
    _git(Path(handle.path), "commit", "-m", "work")

    still_refused = remove_thread_worktree(
        data_dir=tmp_path / "data", project_id="racer", thread_id=1,
        worktree_root=wt_root,
    )
    assert still_refused["removed"] is False
    assert still_refused["inspection"]["dirty"] is False
    assert still_refused["inspection"]["unmerged_commits"] == 1

    done = remove_thread_worktree(
        data_dir=tmp_path / "data", project_id="racer", thread_id=1,
        acknowledge_unmerged=True, worktree_root=wt_root,
    )
    assert done["removed"] is True
    assert not Path(handle.path).exists()
    assert list_thread_worktrees(tmp_path / "data") == []


def test_clean_fully_merged_worktree_removes_without_ceremony(repo, tmp_path, wt_root):
    handle = _provision(repo, tmp_path, wt_root)

    from pathlib import Path

    result = remove_thread_worktree(
        data_dir=tmp_path / "data", project_id="racer", thread_id=1,
        worktree_root=wt_root,
    )
    assert result["removed"] is True
    assert result["inspection"]["unmerged_commits"] == 0
    assert not Path(handle.path).exists()
    assert get_thread_worktree(tmp_path / "data", "racer", 1) is None


def test_removal_of_an_unknown_thread_is_a_typed_no_op(tmp_path):
    result = remove_thread_worktree(
        data_dir=tmp_path / "data", project_id="racer", thread_id=9,
        worktree_root=tmp_path / "thread_worktrees",
    )
    assert result == {
        "removed": False, "reason": "unknown", "inspection": {}, "branch_deleted": False,
    }


def test_a_survived_checkout_is_reported_as_not_removed_and_keeps_its_row(
    repo, tmp_path, wt_root,
):
    """``git worktree remove`` runs with ``check=False`` and ``force_rmtree``
    swallows its errors, so a checkout that CANNOT be deleted (git lock,
    read-only parent, busy file) used to be reported ``removed: True`` while its
    registry row was dropped — an orphaned checkout holding the branch, invisible
    to the registry, and impossible to re-provision or remove again."""
    import os
    import stat
    from pathlib import Path

    handle = _provision(repo, tmp_path, wt_root)
    # A read-only parent: entries inside it cannot be unlinked, so both git's
    # removal and the rmtree fallback fail while the checkout stays readable.
    original = stat.S_IMODE(os.stat(wt_root).st_mode)
    os.chmod(wt_root, 0o555)
    try:
        result = remove_thread_worktree(
            data_dir=tmp_path / "data", project_id="racer", thread_id=1,
            acknowledge_unmerged=True, worktree_root=wt_root,
        )
    finally:
        os.chmod(wt_root, original)
        # The failed rmtree left the surviving directory write-only; restoring
        # it is part of clearing the obstruction, not part of the guarantee.
        if Path(handle.path).exists():
            os.chmod(Path(handle.path), 0o755)

    assert Path(handle.path).exists(), "precondition: the checkout survived"
    assert result["removed"] is False
    assert result["reason"] == "removal_failed"
    assert result["inspection"]["exists"] is True
    # The row is RETAINED: the orphan stays visible and re-removable.
    assert get_thread_worktree(tmp_path / "data", "racer", 1) is not None

    # ...and once the obstruction is gone the same call actually removes it.
    done = remove_thread_worktree(
        data_dir=tmp_path / "data", project_id="racer", thread_id=1,
        acknowledge_unmerged=True, worktree_root=wt_root,
    )
    assert done["removed"] is True
    assert not Path(handle.path).exists()
    assert list_thread_worktrees(tmp_path / "data") == []


def test_a_malformed_row_can_never_delete_an_outside_path(repo, tmp_path, wt_root):
    import json

    from ouroboros.thread_worktrees import _registry_path

    victim = tmp_path / "not-a-worktree"
    victim.mkdir()
    (victim / "keepme.txt").write_text("keep\n", encoding="utf-8")
    path = _registry_path(tmp_path / "data")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"worktrees": [{
        "project_id": "racer", "thread_id": 1, "path": str(victim),
        "branch": "thread/x", "base_sha": "", "repo_dir": str(repo), "created_at": 0,
    }]}), encoding="utf-8")

    result = remove_thread_worktree(
        data_dir=tmp_path / "data", project_id="racer", thread_id=1,
        acknowledge_unmerged=True, worktree_root=wt_root,
    )
    assert result["removed"] is False
    assert result["reason"] == "path_outside_root"
    assert (victim / "keepme.txt").exists()


def test_inspection_treats_unreadable_as_unsafe(tmp_path):
    """"Cannot tell" must never read as "nothing to lose"."""
    plain = tmp_path / "plain"
    plain.mkdir()
    report = inspect_thread_worktree({"path": str(plain), "base_sha": "deadbeef"})
    assert report["exists"] is True
    assert report["dirty"] is True
    assert report["error"]


def test_removal_deletes_the_thread_branch_so_reprovisioning_works(repo, tmp_path, wt_root):
    """Provisioning refuses to reuse an existing branch (an owner's work is
    never clobbered), so a removal that left ``thread/<name>`` behind turned
    every removal into a PERMANENT block on branching that thread off again —
    the owner would have had to delete a git branch by hand."""
    from pathlib import Path

    handle = _provision(repo, tmp_path, wt_root)
    (Path(handle.path) / "draft.txt").write_text("unsaved\n", encoding="utf-8")
    _git(Path(handle.path), "config", "user.email", "t@example.com")
    _git(Path(handle.path), "config", "user.name", "T")
    _git(Path(handle.path), "add", "draft.txt")
    _git(Path(handle.path), "commit", "-m", "work")

    done = remove_thread_worktree(
        data_dir=tmp_path / "data", project_id="racer", thread_id=1,
        acknowledge_unmerged=True, worktree_root=wt_root,
    )
    assert done["removed"] is True
    assert done["branch_deleted"] is True
    listed = subprocess.run(
        ["git", "branch", "--list", handle.branch],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    assert listed.stdout.strip() == ""

    # ...and the same thread can be branched off again.
    again = _provision(repo, tmp_path, wt_root)
    assert Path(again.path).is_dir()
    assert again.branch == handle.branch


def test_a_clean_removal_also_frees_the_branch(repo, tmp_path, wt_root):
    handle = _provision(repo, tmp_path, wt_root)
    result = remove_thread_worktree(
        data_dir=tmp_path / "data", project_id="racer", thread_id=1,
        worktree_root=wt_root,
    )
    assert result["removed"] is True and result["branch_deleted"] is True
    assert _provision(repo, tmp_path, wt_root).branch == handle.branch


def test_worktree_ops_lock_is_keyed_on_the_repo(repo, tmp_path, wt_root):
    """T0-8: `git worktree add|remove|prune` all rewrite the SAME
    <repo>/.git/worktrees metadata. Keying the cross-process lockfile on each
    registry's own worktree ROOT gave the subagent owner and the thread owner
    two different lockfiles over one .git — they never actually serialized."""
    from pathlib import Path

    from ouroboros.subagent_worktrees import _ops_lock_path

    expected = repo / ".git" / ".worktree_ops.lock"
    assert _ops_lock_path(repo) == expected
    # Both registries resolve to that ONE file...
    assert _ops_lock_path(str(repo)) == expected
    # ...while a plain (non-repo) directory keeps a lock of its own: those ops
    # contend for a NAME under the root, not for git metadata.
    assert _ops_lock_path(wt_root) == wt_root / ".worktree_ops.lock"

    # A linked worktree hands us a .git FILE; it must still meet the main repo.
    handle = _provision(repo, tmp_path, wt_root)
    assert (Path(handle.path) / ".git").is_file()
    assert _ops_lock_path(handle.path) == expected


def test_subagent_age_sweep_cannot_see_a_thread_worktree(repo, tmp_path, wt_root):
    """R2/X3 structurally: the sweep iterates the SUBAGENT registry, so a
    separate registry file is what makes a thread worktree unreachable by it."""
    from ouroboros.subagent_worktrees import prune_orphans
    from ouroboros.thread_worktrees import _registry_path

    handle = _provision(repo, tmp_path, wt_root)
    from pathlib import Path

    summary = prune_orphans(
        worktree_root=tmp_path / "subagent_worktrees",
        data_dir=tmp_path / "data",
        retention_days=0,
    )

    assert summary == {"removed": 0, "kept": 0}
    assert Path(handle.path).is_dir()
    assert _registry_path(tmp_path / "data").name == "thread_worktrees.json"
    assert list_thread_worktrees(tmp_path / "data")
