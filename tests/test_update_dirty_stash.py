"""Q1=C: a clean auto-update with a DIRTY worktree never commits local work into
history — it is stashed before the apply and restored as uncommitted content
(boot finalize on success, rollback path on failure), with a disclosed, kept
stash entry when an automatic restore would conflict."""

import subprocess

import supervisor.git_ops as git_ops
import supervisor.update_merge as update_merge


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "a.txt").write_text("base\n")
    (repo / "BIBLE.md").write_text("constitution\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    head = _git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
    return repo, head


def _point_at(monkeypatch, tmp_path, repo, head):
    monkeypatch.setattr(git_ops, "REPO_DIR", repo)
    monkeypatch.setattr(git_ops, "BRANCH_DEV", head)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    monkeypatch.setattr(git_ops, "_managed_update_target", lambda branch=None: ("", "", "remote-sim"))
    monkeypatch.setattr(
        git_ops,
        "_resolve_managed_update_target",
        lambda *_args: (
            "remote-sim",
            _git(repo, "rev-parse", "remote-sim").stdout.strip(),
            "",
        ),
    )
    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)


def _diverged_clean_repo(tmp_path, monkeypatch):
    """Official target and local commit touch different files; dirty edits on top."""
    repo, head = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "remote-sim")
    (repo / "remote.txt").write_text("official\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "official")
    _git(repo, "checkout", "-q", head)
    (repo / "local.txt").write_text("local commit\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "local")
    _point_at(monkeypatch, tmp_path, repo, head)
    return repo, head


def test_clean_dirty_plan_builds_history_without_local_work(tmp_path, monkeypatch):
    repo, head = _diverged_clean_repo(tmp_path, monkeypatch)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    target = _git(repo, "rev-parse", "remote-sim").stdout.strip()
    (repo / "a.txt").write_text("dirty local edit\n")
    (repo / "untracked.txt").write_text("scratch\n")

    plan = update_merge.plan_managed_update_merge(build=True)

    assert plan["kind"] == "clean", plan
    merge_commit = plan["merge_commit"]
    assert merge_commit
    parents = _git(repo, "log", "-1", "--format=%P", merge_commit).stdout.split()
    assert parents == [base, target], parents
    # The committed tree carries NO local dirty work.
    committed_a = _git(repo, "show", f"{merge_commit}:a.txt").stdout
    assert committed_a == "base\n"
    ls = _git(repo, "ls-tree", "-r", "--name-only", merge_commit).stdout
    assert "untracked.txt" not in ls
    # The worktree itself was never touched by planning.
    assert (repo / "a.txt").read_text() == "dirty local edit\n"


def test_clean_dirty_fast_forward_plan_lands_official_history(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "remote-sim")
    (repo / "remote.txt").write_text("official\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "official")
    _git(repo, "checkout", "-q", head)
    _point_at(monkeypatch, tmp_path, repo, head)
    target = _git(repo, "rev-parse", "remote-sim").stdout.strip()
    (repo / "a.txt").write_text("dirty local edit\n")

    plan = update_merge.plan_managed_update_merge(build=True)

    assert plan["kind"] == "clean", plan
    assert plan["merge_commit"] == target


def test_stash_roundtrip_restores_local_work(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    (repo / "a.txt").write_text("dirty\n")
    (repo / "untracked.txt").write_text("scratch\n")

    ok, stash_sha, error = update_merge.stash_local_changes_for_update("t1")
    assert ok and stash_sha, error
    assert not _git(repo, "status", "--porcelain").stdout.strip()

    restored, note = update_merge.restore_update_stash(stash_sha, context="test")
    assert restored, note
    assert (repo / "a.txt").read_text() == "dirty\n"
    assert (repo / "untracked.txt").read_text() == "scratch\n"
    assert not _git(repo, "stash", "list").stdout.strip()


def test_stash_on_clean_tree_is_a_noop(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)

    ok, stash_sha, error = update_merge.stash_local_changes_for_update("t2")
    assert ok and stash_sha == "", (stash_sha, error)
    assert update_merge.restore_update_stash("", context="test") == (True, "")


def test_conflicting_restore_keeps_stash_and_discloses(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    (repo / "a.txt").write_text("dirty conflicting edit\n")
    ok, stash_sha, _error = update_merge.stash_local_changes_for_update("t3")
    assert ok and stash_sha
    # The updated tree rewrites the same lines the stash carries.
    (repo / "a.txt").write_text("official rewrite\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "official rewrite")

    restored, note = update_merge.restore_update_stash(stash_sha, context="test")

    assert restored is False
    assert stash_sha[:12] in note and "git stash apply" in note
    # Worktree left clean; the stash entry survives for manual recovery.
    assert not _git(repo, "status", "--porcelain").stdout.strip()
    assert stash_sha in _git(repo, "stash", "list", "--format=%H").stdout


def test_rollback_restores_stashed_local_work(tmp_path, monkeypatch):
    import supervisor.workers as workers

    repo, head = _diverged_clean_repo(tmp_path, monkeypatch)
    pre = _git(repo, "rev-parse", "HEAD").stdout.strip()
    target = _git(repo, "rev-parse", "remote-sim").stdout.strip()
    (repo / "a.txt").write_text("keep this local work\n")
    ok, stash_sha, _error = update_merge.stash_local_changes_for_update("t4")
    assert ok and stash_sha
    _git(repo, "checkout", "-q", "-B", head, target)
    update_merge.write_update_tx({
        "phase": "pending_boot_smoke", "pre_update_sha": pre,
        "pre_update_branch": head, "target_sha": target,
        "merge_commit": target, "stash_sha": stash_sha,
    })
    monkeypatch.setattr(workers, "close_repo_writer_admission", lambda reason: True)
    monkeypatch.setattr(workers, "open_repo_writer_admission", lambda expected_reason="": True)

    ok, message = update_merge.rollback_managed_update("test")

    assert ok, message
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == pre
    assert (repo / "a.txt").read_text() == "keep this local work\n"
    assert "restored" in message
