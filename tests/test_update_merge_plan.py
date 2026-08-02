"""Tests for the managed-update merge planner (P2) — real 3-way merge in a temp repo."""

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
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    head = _git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
    return repo, head


def _point_at(monkeypatch, repo):
    monkeypatch.setattr(git_ops, "REPO_DIR", repo)
    monkeypatch.setattr(git_ops, "_managed_update_target", lambda branch=None: ("", "", "remote-sim"))


def test_plan_clean_when_disjoint(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "remote-sim")
    (repo / "b.txt").write_text("remote\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "remote adds b")
    _git(repo, "checkout", "-q", head)
    _point_at(monkeypatch, repo)

    plan = update_merge.plan_managed_update_merge(fetch=False)
    assert plan["available"] is True, plan
    assert plan["kind"] == "clean", plan
    assert plan["auto_mergeable"] is True
    assert plan["recommended_strategy"] == "auto_merge"


def test_plan_conflicting_on_code(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "remote-sim")
    (repo / "a.txt").write_text("remote change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "remote edits a")
    _git(repo, "checkout", "-q", head)
    (repo / "a.txt").write_text("local change\n")  # uncommitted local edit collides
    _point_at(monkeypatch, repo)

    plan = update_merge.plan_managed_update_merge(fetch=False)
    assert plan["available"] is True, plan
    assert plan["kind"] == "conflicting", plan
    assert "a.txt" in plan["code_conflict_paths"]
    assert plan["recommended_strategy"] == "assisted"


def test_plan_doc_reconcile(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    (repo / "README.md").write_text("base readme\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add readme")
    _git(repo, "checkout", "-q", "-b", "remote-sim")
    (repo / "README.md").write_text("remote readme\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "remote edits readme")
    _git(repo, "checkout", "-q", head)
    (repo / "README.md").write_text("local readme\n")  # uncommitted local doc edit collides
    _point_at(monkeypatch, repo)

    plan = update_merge.plan_managed_update_merge(fetch=False)
    assert plan["available"] is True, plan
    assert plan["kind"] == "doc_reconcile", plan
    assert "README.md" in plan["doc_conflict_paths"]


def test_plan_current_when_no_divergence(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _git(repo, "branch", "remote-sim")  # identical to HEAD
    _point_at(monkeypatch, repo)

    plan = update_merge.plan_managed_update_merge(fetch=False)
    assert plan["available"] is False
    assert plan["kind"] == "current"


def test_build_and_apply_clean_merge(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "remote-sim")
    (repo / "b.txt").write_text("remote\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "remote adds b")
    _git(repo, "checkout", "-q", head)
    (repo / "c.txt").write_text("local untracked\n")  # local dirty work to preserve
    _point_at(monkeypatch, repo)

    plan = update_merge.plan_managed_update_merge(fetch=False, build=True)
    assert plan["kind"] == "clean", plan
    assert plan["merge_commit"], plan

    ok, msg = update_merge.apply_managed_merge_update(head, plan["merge_commit"])
    assert ok, msg
    # the live repo now has BOTH the remote's new file AND the local dirty work.
    assert (repo / "b.txt").exists()
    assert (repo / "c.txt").read_text() == "local untracked\n"
    # HEAD is a merge commit (self + 2 parents = local snapshot + target).
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.strip().split()
    assert len(parents) == 3


def test_rollback_managed_update(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    pre = _git(repo, "rev-parse", "HEAD").stdout.strip()
    data_dir = tmp_path / "data"
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(git_ops, "REPO_DIR", repo)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", data_dir)
    monkeypatch.setattr(git_ops, "_git_dir", lambda: repo / ".git")
    # simulate a bad update landed on top.
    (repo / "bad.txt").write_text("bad\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bad update")
    update_merge.write_update_tx({"pre_update_sha": pre, "pre_update_branch": head})

    ok, msg = update_merge.rollback_managed_update("test")
    assert ok, msg
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == pre
    assert not (repo / "bad.txt").exists()
    assert update_merge.read_update_tx() == {}  # marker cleared


def _wire_git_ops(monkeypatch, repo, data_dir):
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(git_ops, "REPO_DIR", repo)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", data_dir)
    monkeypatch.setattr(git_ops, "_git_dir", lambda: repo / ".git")


def test_finalize_clears_marker_on_healthy_boot(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _wire_git_ops(monkeypatch, repo, tmp_path / "data")
    cur = _git(repo, "rev-parse", "HEAD").stdout.strip()
    update_merge.write_update_tx(
        {"phase": "pending_boot_smoke", "merge_commit": cur, "pre_update_sha": cur, "pre_update_branch": head}
    )
    res = update_merge.finalize_managed_update_on_boot(supervisor_ready=True)
    assert res["finalized"] is True, res
    assert update_merge.read_update_tx() == {}


def test_finalize_rolls_back_after_unhealthy_boot(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    pre = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _wire_git_ops(monkeypatch, repo, tmp_path / "data")
    (repo / "bad.txt").write_text("bad\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bad update")
    # merge_commit points at a sha that is NOT HEAD -> health check fails; attempts 1 -> 2 -> rollback.
    update_merge.write_update_tx(
        {"phase": "pending_boot_smoke", "merge_commit": "0" * 40, "pre_update_sha": pre,
         "pre_update_branch": head, "boot_attempts": 1}
    )
    res = update_merge.finalize_managed_update_on_boot(supervisor_ready=True)
    assert res.get("rolled_back") is True, res
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == pre


def test_rollback_still_resets_when_the_forensics_ref_cannot_be_written(tmp_path, monkeypatch):
    """Recovery must not be traded away for a forensics branch name.

    `rollback_managed_update` writes `failed-update-<sha>` before it resets, so the
    rejected candidate keeps a name. That write is BEST-EFFORT on purpose: this
    function's job is to get the machine back onto a working revision, and every
    caller but the commit gate is a recovery path that ignores the boolean and
    relies on the reset having happened. An earlier revision returned early when the
    ref could not be written, which left the box still running the bad update — and
    `_finalize_pending_boot_smoke` returned before persisting `boot_attempts`, so the
    next boot repeated the same failing attempt forever.

    The failure is made real rather than stubbed: an existing `failed-update-<sha>/child`
    ref makes `git branch -f failed-update-<sha>` impossible (a ref cannot be both a
    directory and a file), which is exactly the name-collision case in the finding.
    """
    repo, head = _init_repo(tmp_path)
    pre = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _wire_git_ops(monkeypatch, repo, tmp_path / "data")
    (repo / "bad.txt").write_text("bad\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bad update")

    short = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    blocked = _git(repo, "branch", f"failed-update-{short}/child", "HEAD")
    assert blocked.returncode == 0, blocked.stderr
    assert _git(repo, "branch", "-f", f"failed-update-{short}", "HEAD").returncode != 0, (
        "the collision did not actually make the forensics ref unwritable, so this "
        "test would pass without exercising the failure at all"
    )

    update_merge.write_update_tx({"pre_update_sha": pre, "pre_update_branch": head})
    ok, msg = update_merge.rollback_managed_update("test")

    assert ok, f"a forensics-ref failure aborted the recovery: {msg}"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == pre, (
        "the machine was left on the bad update because a branch name could not be written"
    )
    assert not (repo / "bad.txt").exists()
    assert update_merge.read_update_tx() == {}, (
        "the tx marker survived, so the next boot resumes the update that was just rolled back"
    )
    assert "could not be recorded" in msg, (
        f"the lost forensics ref must still be reported to the operator: {msg}"
    )


def test_a_gate_blocked_tx_is_terminal_for_boot_recovery_and_for_commits(tmp_path, monkeypatch):
    """`gate_blocked` is the terminal phase written when a rejected update could NOT be rolled back.

    The danger it exists to stop: an assisted tx sits in `committing_assisted` when the
    gate rejects it, and boot recovery reads that phase as "the process died
    mid-commit" — so it promotes the merge to `pending_boot_smoke` and finalizes the
    exact revision the gate refused, without ever rerunning the gate. Re-phasing to
    `gate_blocked` is what makes that impossible, so it has to be terminal in BOTH
    directions: boot recovery must not advance it, and `commit_reviewed` must not
    admit an ordinary commit on top of a refused merge that is still in the tree.

    Pinned against the real module rather than a fake, because the value of the phase
    is entirely in the real recovery branches keying off it.
    """
    repo, head = _init_repo(tmp_path)
    _wire_git_ops(monkeypatch, repo, tmp_path / "data")
    cur = _git(repo, "rev-parse", "HEAD").stdout.strip()
    update_merge.write_update_tx(
        {"phase": "committing_assisted", "task_id": "t-1", "merge_commit": cur,
         "pre_update_sha": cur, "pre_update_branch": head}
    )

    assert update_merge.mark_update_tx_gate_blocked("post_commit_gate_failed") is True
    tx = update_merge.read_update_tx()
    assert tx["phase"] == update_merge.UPDATE_TX_GATE_BLOCKED
    assert tx["gate_blocked_reason"] == "post_commit_gate_failed"

    # Boot recovery stops instead of promoting the refused merge.
    res = update_merge.finalize_managed_update_on_boot(supervisor_ready=True)
    assert res["finalized"] is False, res
    assert "gate_blocked" in str(res.get("reason") or ""), res
    assert update_merge.read_update_tx()["phase"] == update_merge.UPDATE_TX_GATE_BLOCKED, (
        "boot recovery advanced a terminal gate_blocked marker"
    )

    # ...and the commit path blocks, INCLUDING the task that was authorized before.
    for task_id in ("t-1", "some-other-task"):
        managed_tx, block = update_merge.managed_assisted_tx_for(task_id)
        assert managed_tx == {}, f"{task_id} was handed a gate-blocked tx to commit under"
        assert "MANAGED_UPDATE_GATE_BLOCKED" in block, block
        assert "post_commit_gate_failed" in block, block


def test_mark_update_tx_gate_blocked_does_not_invent_a_transaction(tmp_path, monkeypatch):
    """No live tx means nothing to re-phase — writing one would CREATE a blocking marker.

    The caller reaches this helper on a failed rollback, and a rollback fails for
    reasons that include "the marker was already cleared". Writing a `gate_blocked`
    tx from nothing there would brick every later commit on a machine whose update
    had actually finished.
    """
    repo, _head = _init_repo(tmp_path)
    _wire_git_ops(monkeypatch, repo, tmp_path / "data")

    assert update_merge.mark_update_tx_gate_blocked("post_commit_gate_failed") is False
    assert update_merge.read_update_tx() == {}, "a gate-blocked marker was invented from no tx"
