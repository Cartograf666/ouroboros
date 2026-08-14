"""Tests for the AUTOMATED assisted managed-update merge (P2/SC2) — native MERGE_HEAD staged
in a real temp repo, the tx authorization gate, the conflict-marker gate, merge-state
classification, non-destructive boot recovery, and the rescue-before-rollback hook."""

import json
import subprocess
from types import SimpleNamespace

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


def _authority_metadata(tx):
    return {
        "managed_update": {
            "authority_fingerprint": update_merge.assisted_authority_fingerprint(tx),
        }
    }


def _conflict_repo(tmp_path, monkeypatch):
    """A repo where the official target and a local uncommitted edit collide on a.txt."""
    repo, head = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "remote-sim")
    (repo / "a.txt").write_text("remote change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "remote edits a")
    _git(repo, "checkout", "-q", head)
    (repo / "a.txt").write_text("local change\n")  # uncommitted local edit collides
    _point_at(monkeypatch, tmp_path, repo, head)
    plan = update_merge.plan_managed_update_merge(fetch=False)
    return repo, head, plan


def test_materialize_sets_merge_head_and_markers(tmp_path, monkeypatch):
    repo, head, plan = _conflict_repo(tmp_path, monkeypatch)
    assert plan["kind"] == "conflicting", plan
    ok, msg = update_merge.materialize_assisted_merge_live(
        head, plan["local_snapshot"], plan["target_sha"], plan["base_sha"]
    )
    assert ok, msg
    # MERGE_HEAD points at the official target; HEAD is re-based to the REVIEWED pre-update
    # base (so the reviewed diff includes the owner's dirty work); a.txt carries markers.
    assert update_merge._merge_head_sha() == plan["target_sha"]
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == plan["base_sha"]
    body = (repo / "a.txt").read_text()
    assert "<<<<<<<" in body and ">>>>>>>" in body
    # The marker gate (after `git add`) must REJECT the unresolved markers.
    _git(repo, "add", "-A")
    ok2, err = update_merge.managed_assisted_marker_check()
    assert not ok2 and "conflict markers" in err
    # Resolve the conflict → the gate passes.
    (repo / "a.txt").write_text("reconciled\n")
    _git(repo, "add", "-A")
    ok3, _e = update_merge.managed_assisted_marker_check()
    assert ok3


def test_marker_gate_accepts_staged_deletion_and_binary_blob(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    (repo / "binary.bin").write_bytes(b"\x00<<<<<<< ours\n>>>>>>> theirs\n")
    _git(repo, "add", "binary.bin")
    _git(repo, "rm", "a.txt")

    ok, message = update_merge.managed_assisted_marker_check()

    assert ok, message


def test_assisted_head_state_in_progress_then_committed(tmp_path, monkeypatch):
    repo, head, plan = _conflict_repo(tmp_path, monkeypatch)
    update_merge.materialize_assisted_merge_live(
        head, plan["local_snapshot"], plan["target_sha"], plan["base_sha"]
    )
    tx = {"pre_update_sha": plan["base_sha"], "target_sha": plan["target_sha"]}
    # Before commit HEAD == pre_update_sha (the reviewed base) → in_progress.
    assert update_merge._assisted_head_state(tx) == "in_progress"
    # Resolve + commit (MERGE_HEAD makes it a real 2-parent merge) → committed.
    (repo / "a.txt").write_text("reconciled\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "merge resolved")
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert plan["target_sha"] in parents[1:]  # the official target is a real parent
    assert update_merge._assisted_head_state(tx) == "committed"


def test_managed_assisted_tx_for_authorizes_only_owner(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    tx_data = {"phase": "assisted_resolution", "task_id": "owner-task"}
    metadata = _authority_metadata(tx_data)
    update_merge.write_update_tx(tx_data)
    # The authorized task is allowed (no block); any other task is blocked.
    tx, block = update_merge.managed_assisted_tx_for("owner-task", metadata)
    assert tx and not block
    _tx2, block2 = update_merge.managed_assisted_tx_for("some-other-task", metadata)
    assert not _tx2 and "MANAGED_UPDATE_IN_PROGRESS" in block2
    for phase in ("pending_boot_smoke", "rolling_back"):
        update_merge.write_update_tx({"phase": phase, "task_id": "owner-task"})
        _tx3, block3 = update_merge.managed_assisted_tx_for("owner-task", metadata)
        assert not _tx3 and "MANAGED_UPDATE_IN_PROGRESS" in block3
    # No managed tx → never blocks.
    update_merge.clear_update_tx()
    assert update_merge.managed_assisted_tx_for("any") == ({}, "")


def test_managed_update_tool_gate_fails_closed_when_state_is_unavailable(monkeypatch):
    from ouroboros.tools.registry import _managed_update_code_tool_block

    monkeypatch.setattr(
        update_merge,
        "managed_assisted_tx_for",
        lambda *_args: (_ for _ in ()).throw(OSError("state unavailable")),
    )
    ctx = type("Context", (), {"task_id": "task", "task_metadata": {}})()

    block = _managed_update_code_tool_block(ctx, "write_file")

    assert "MANAGED_UPDATE_STATE_UNAVAILABLE" in block
    assert "write_file" in block


def test_authorized_resolver_can_edit_any_conflicting_official_file(tmp_path, monkeypatch):
    from ouroboros.tools import git as git_tool
    from ouroboros.tools.registry import ToolContext

    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    (repo / "BIBLE.md").write_text("local\n", encoding="utf-8")
    tx_data = {
        "phase": "assisted_resolution",
        "task_id": "update-resolver",
    }
    metadata = _authority_metadata(tx_data)
    update_merge.write_update_tx(tx_data)
    monkeypatch.setattr(git_tool, "_current_runtime_mode", lambda: "advanced")

    authorized = ToolContext(
        repo_dir=repo,
        drive_root=tmp_path / "data",
        task_id="update-resolver",
        task_metadata=metadata,
    )
    other = ToolContext(
        repo_dir=repo,
        drive_root=tmp_path / "data",
        task_id="unrelated-task",
    )

    result = git_tool._repo_write(authorized, path="BIBLE.md", content="reconciled\n")
    blocked = git_tool._repo_write(other, path="BIBLE.md", content="unrelated\n")

    assert "Written 1 file" in result
    assert (repo / "BIBLE.md").read_text(encoding="utf-8") == "reconciled\n"
    assert "CORE_PROTECTION_BLOCKED" in blocked


def test_forged_marker_without_host_metadata_cannot_authorize_or_rollback(
    tmp_path, monkeypatch
):
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    pre = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    update_merge.write_update_tx({
        "phase": "assisted_resolution",
        "task_id": "ordinary-task",
        "pre_update_sha": pre,
        "pre_update_branch": head,
        "target_sha": "b" * 40,
    })

    assert not update_merge.authorized_assisted_task("ordinary-task", {})
    managed, block = update_merge.managed_assisted_tx_for("ordinary-task", {})
    assert not managed and "MANAGED_UPDATE_IN_PROGRESS" in block
    result = update_merge.abort_orphaned_assisted_tx("ordinary-task", {})

    assert result == {"acted": False, "reason": "resolver authority mismatch"}
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == pre
    assert (repo / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def test_cancelled_resolver_task_done_keeps_event_authority(tmp_path, monkeypatch):
    from supervisor.events import _handle_task_done

    metadata = {"managed_update": {"authority_fingerprint": "host-bound"}}
    calls = {}
    monkeypatch.setattr(
        update_merge,
        "abort_orphaned_assisted_tx",
        lambda task_id, task_metadata: calls.setdefault(
            "abort", (task_id, task_metadata)
        ),
    )
    monkeypatch.setattr(
        update_merge,
        "release_assisted_writer_gate_after_task",
        lambda task_metadata: calls.setdefault("release", task_metadata),
    )
    ctx = SimpleNamespace(
        RUNNING={},
        WORKERS={},
        DRIVE_ROOT=tmp_path,
        REPO_DIR=tmp_path,
        persist_queue_snapshot=lambda reason="": None,
        bridge=SimpleNamespace(push_log=lambda event: None),
    )

    _handle_task_done(
        {
            "type": "task_done",
            "task_id": "update-resolver",
            "task_type": "task",
            "status": "cancelled",
            "metadata": metadata,
        },
        ctx,
    )

    assert calls == {
        "abort": ("update-resolver", metadata),
        "release": metadata,
    }


def test_assisted_objective_is_truthful_for_any_conflict_free_reviewed_merge():
    from supervisor.update_merge_policy import assisted_objective

    objective = assisted_objective({
        "target_sha": "b" * 40,
        "conflict_paths": [],
    })

    assert "merge itself is clean" in objective
    assert "combines local and official history" in objective
    assert "conflicts are marked" not in objective
    assert "see `git status` for unmerged paths" not in objective
    # No prior rescue on the tx → the objective must not invent one.
    assert "was rescued to" not in objective


def test_boot_resume_does_not_enqueue_a_duplicate_assisted_resolver(monkeypatch):
    import supervisor.queue as queue
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "ensure_worker_pool_started", lambda **_kwargs: True)
    pending = [{"id": "resolver-task", "type": "task", "legacy_field": "preserved"}]
    monkeypatch.setattr(workers, "PENDING", pending)
    monkeypatch.setattr(workers, "RUNNING", {})
    monkeypatch.setattr(
        queue,
        "enqueue_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing resolver must not be enqueued again")
        ),
    )

    tx = {
        "task_id": "resolver-task",
        "target_sha": "b" * 40,
        "owner_chat_id": 0,
    }
    task_id = update_merge.enqueue_assisted_resolution_task(tx)

    assert task_id == "resolver-task"
    assert pending[0]["legacy_field"] == "preserved"
    assert update_merge.assisted_task_metadata_authorizes(tx, pending[0]["metadata"])


def test_assisted_resolver_readiness_waits_for_clean_tree_boot(tmp_path, monkeypatch):
    import supervisor.workers as workers

    proc = SimpleNamespace(pid=1234, is_alive=lambda: True)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    monkeypatch.setattr(workers, "WORKERS", {})

    def start_pool(**_kwargs):
        workers.WORKERS[0] = SimpleNamespace(proc=proc)
        return True

    monkeypatch.setattr(workers, "ensure_worker_pool_started", start_pool)
    monkeypatch.setattr(
        workers,
        "_first_worker_event_since",
        lambda *_args: {"pid": 1234, "git_sha": "base-sha"},
    )

    assert update_merge.ensure_assisted_resolver_ready("base-sha", timeout_sec=0.1) is True


def test_assisted_resolver_readiness_rejects_wrong_sha(tmp_path, monkeypatch):
    import supervisor.workers as workers

    proc = SimpleNamespace(pid=1234, is_alive=lambda: True)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    monkeypatch.setattr(workers, "WORKERS", {})
    monkeypatch.setattr(
        workers,
        "ensure_worker_pool_started",
        lambda **_kwargs: workers.WORKERS.setdefault(0, SimpleNamespace(proc=proc)) is not None,
    )
    monkeypatch.setattr(
        workers,
        "_first_worker_event_since",
        lambda *_args: {"pid": 1234, "git_sha": "stale-sha"},
    )

    assert update_merge.ensure_assisted_resolver_ready("base-sha", timeout_sec=0.1) is False


def test_worker_ready_follows_update_authority_preload():
    import inspect
    import supervisor.workers as workers

    source = inspect.getsource(workers.worker_main)
    assert source.index("_prepare_worker_task_runtime()") < source.index('"worker_ready"')


def test_read_update_tx_strict_distinguishes_corrupt(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    assert update_merge.read_update_tx_strict()[0] == "absent"
    update_merge.write_update_tx({"phase": "assisted_resolution", "task_id": "x"})
    assert update_merge.read_update_tx_strict()[0] == "valid"
    update_merge._update_tx_marker_path().write_text("{ not json", encoding="utf-8")
    assert update_merge.read_update_tx_strict()[0] == "corrupt"
    # A corrupt marker counts as an ACTIVE tx (fail-closed) and blocks other tasks.
    assert update_merge.managed_assisted_tx_for("anyone")[1]


def test_pending_boot_smoke_not_finalized_on_failed_supervisor(tmp_path, monkeypatch):
    """A failed supervisor boot (supervisor_ready=False) must NOT clear a pending update as
    finalized, even when HEAD contains the merge — the boot-loop rollback must still fire later."""
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    cur = _git(repo, "rev-parse", "HEAD").stdout.strip()
    update_merge.write_update_tx({
        "phase": "pending_boot_smoke", "merge_commit": cur,
        "pre_update_sha": cur, "pre_update_branch": head,
    })
    res = update_merge.finalize_managed_update_on_boot(supervisor_ready=False)
    assert res.get("finalized") is not True, res
    assert update_merge.read_update_tx()["boot_attempts"] == 1
    res2 = update_merge.finalize_managed_update_on_boot(supervisor_ready=False)
    assert res2.get("rolled_back") is True, res2
    assert update_merge.read_update_tx_strict()[0] == "absent"


def test_healthy_boot_clears_replace_intent_before_finalizing(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    cur = _git(repo, "rev-parse", "HEAD").stdout.strip()
    git_ops._write_update_intent({"target_sha": cur})
    update_merge.write_update_tx({
        "phase": "pending_boot_smoke", "merge_commit": cur,
        "pre_update_sha": cur, "pre_update_branch": head,
    })

    result = update_merge.finalize_managed_update_on_boot(supervisor_ready=True)

    assert result["finalized"] is True
    assert not git_ops._update_intent_marker_path().exists()
    assert update_merge.read_update_tx_strict()[0] == "absent"


def test_boot_replays_unproven_pre_restart_smoke_before_finalizing(
    tmp_path, monkeypatch
):
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    cur = _git(repo, "rev-parse", "HEAD").stdout.strip()
    calls = []
    monkeypatch.setattr(
        update_merge,
        "update_restart_smoke",
        lambda: calls.append("smoke") or {"ok": True},
    )
    update_merge.write_update_tx({
        "phase": "pending_boot_smoke",
        "pre_restart_smoke": "pending",
        "merge_commit": cur,
        "pre_update_sha": cur,
        "pre_update_branch": head,
    })

    result = update_merge.finalize_managed_update_on_boot(supervisor_ready=True)

    assert result["finalized"] is True
    assert calls == ["smoke"]
    assert update_merge.read_update_tx_strict()[0] == "absent"


def test_boot_rolls_back_when_recovered_pre_restart_smoke_fails(
    tmp_path, monkeypatch
):
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    cur = _git(repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(
        update_merge,
        "update_restart_smoke",
        lambda: {"ok": False, "stderr": "broken", "returncode": 1},
    )
    update_merge.write_update_tx({
        "phase": "pending_boot_smoke",
        "pre_restart_smoke": "pending",
        "merge_commit": cur,
        "pre_update_sha": cur,
        "pre_update_branch": head,
    })

    result = update_merge.finalize_managed_update_on_boot(supervisor_ready=True)

    assert result["rolled_back"] is True
    assert update_merge.read_update_tx_strict()[0] == "absent"


def test_assisted_commit_publishes_smoke_proof_only_after_pass(monkeypatch):
    writes = []
    monkeypatch.setattr(update_merge, "write_update_tx", lambda tx: writes.append(dict(tx)))
    monkeypatch.setattr(update_merge, "update_restart_smoke", lambda: {"ok": True})

    ok, _message = update_merge.managed_assisted_postcommit(
        {"phase": "committing_assisted", "task_id": "resolver"},
        "c" * 40,
    )

    assert ok is True
    assert [tx["pre_restart_smoke"] for tx in writes] == ["pending", "passed"]


def test_assisted_commit_crash_before_gates_rolls_back(tmp_path, monkeypatch):
    repo, head, plan = _conflict_repo(tmp_path, monkeypatch)
    update_merge.materialize_assisted_merge_live(
        head, plan["local_snapshot"], plan["target_sha"], plan["base_sha"]
    )
    (repo / "a.txt").write_text("resolved but unproven\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "unproven merge")
    update_merge.write_update_tx({
        "phase": "committing_assisted", "task_id": "resolver",
        "pre_update_sha": plan["base_sha"], "pre_update_branch": head,
        "target_sha": plan["target_sha"],
    })

    result = update_merge.finalize_managed_update_on_boot(supervisor_ready=True)

    assert result.get("rolled_back") is True, result
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == plan["base_sha"]
    assert update_merge.read_update_tx_strict()[0] == "absent"


def test_replace_crash_before_checkout_preserves_dirty_tree(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    pre = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("owner dirty work\n")
    git_ops._write_update_intent({"target_sha": "b" * 40})
    update_merge.write_update_tx({
        "phase": "applying_replace", "pre_update_sha": pre,
        "pre_update_branch": head, "target_sha": "b" * 40,
        "merge_commit": "b" * 40, "pre_update_dirty_count": 1,
    })

    result = update_merge.finalize_managed_update_on_boot(supervisor_ready=True)

    assert result.get("abandoned") is True, result
    assert (repo / "a.txt").read_text() == "owner dirty work\n"
    assert update_merge.read_update_tx_strict()[0] == "absent"
    assert not git_ops._update_intent_marker_path().exists()


def test_replace_target_with_dirty_tree_is_rolled_back(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    pre = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("target\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "target")
    target = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("partial checkout dirt\n")
    git_ops._write_update_intent({"target_sha": target})
    update_merge.write_update_tx({
        "phase": "applying_replace", "pre_update_sha": pre,
        "pre_update_branch": head, "target_sha": target,
        "merge_commit": target, "pre_update_dirty_count": 0,
    })

    result = update_merge.finalize_managed_update_on_boot(supervisor_ready=True)

    assert result.get("rolled_back") is True, result
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == pre
    assert not _git(repo, "status", "--porcelain").stdout.strip()


def test_rollback_disarms_replay_before_touching_dirty_tree(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    pre = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("keep me\n")
    update_merge.write_update_tx({
        "phase": "pending_boot_smoke", "pre_update_sha": pre,
        "pre_update_branch": head, "target_sha": "b" * 40,
    })
    monkeypatch.setattr(git_ops, "_clear_update_intent", lambda: False)

    ok, _message = update_merge.rollback_managed_update("test")

    assert ok is False
    assert (repo / "a.txt").read_text() == "keep me\n"
    assert update_merge.read_update_tx()["phase"] == "rolling_back"
    detail = "rollback evidence " * 200
    assert update_merge.mark_update_tx_gate_blocked("test", detail) is True
    blocked = update_merge.read_update_tx()
    # The pre-gate phase is taken OFF the marker (a refused merge left in its
    # original phase reads as an interrupted step and gets resumed/promoted);
    # boot's gate_blocked branch retries the rollback, so recovery is preserved.
    assert blocked["phase"] == update_merge.GATE_BLOCKED_PHASE
    assert blocked["gate_blocked_from_phase"] == "rolling_back"
    assert blocked["gate_blocked_detail"] == detail

    monkeypatch.setattr(git_ops, "_clear_update_intent", lambda: True)
    recovered = update_merge.finalize_managed_update_on_boot(supervisor_ready=True)
    assert recovered["rolled_back"] is True
    assert update_merge.read_update_tx_strict()[0] == "absent"


def test_restart_smoke_syncs_dependencies_before_code_checks(monkeypatch):
    calls = []
    monkeypatch.setattr(update_merge, "managed_update_constitution_present", lambda _ref: True)
    monkeypatch.setattr(git_ops, "git_capture", lambda _cmd: (0, "", ""))
    monkeypatch.setattr(
        git_ops, "sync_runtime_dependencies",
        lambda reason: (calls.append(("deps", reason)) or (True, "ok")),
    )
    monkeypatch.setattr(
        update_merge, "_run_update_smoke",
        lambda cmd, timeout_sec=120.0: (calls.append(("smoke", cmd)) or {
            "ok": True, "stdout": "", "stderr": "", "returncode": 0,
        }),
    )

    result = update_merge.update_restart_smoke()

    assert result["ok"] is True
    assert calls[0] == ("deps", "managed_update_pre_restart")
    assert [kind for kind, _payload in calls] == ["deps", "smoke", "smoke"]


def test_restart_smoke_timeout_kills_process_tree(monkeypatch):
    import ouroboros.platform_layer as platform_layer
    from ouroboros.tools import shell

    killed = []

    class HungProcess:
        returncode = 1

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout=None):
            assert self in shell._active_subprocesses
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["python"], timeout)
            return "", ""

    proc = HungProcess()
    monkeypatch.setattr(update_merge.subprocess, "Popen", lambda *_a, **_k: proc)
    monkeypatch.setattr(platform_layer, "kill_process_tree", lambda value: killed.append(value))

    result = update_merge._run_update_smoke(["python"], timeout_sec=0.01)

    assert result["returncode"] == 124
    assert killed == [proc]
    assert proc not in shell._active_subprocesses


def test_boot_recovery_diverged_keeps_worker_commit(tmp_path, monkeypatch):
    """A real reviewed commit that landed on top during resolution is NEVER reset away."""
    repo, head, plan = _conflict_repo(tmp_path, monkeypatch)
    # A worker landed a real reviewed commit on top of the pre-update base during resolution.
    _git(repo, "reset", "--hard", plan["base_sha"])
    (repo / "a.txt").write_text("a worker's reviewed change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "unrelated reviewed commit")
    worker_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    update_merge.write_update_tx({
        "phase": "assisted_resolution", "task_id": "t",
        "pre_update_sha": plan["base_sha"], "pre_update_branch": head,
        "local_snapshot": plan["local_snapshot"], "target_sha": plan["target_sha"],
    })
    res = update_merge.finalize_managed_update_on_boot(supervisor_ready=True)
    assert res.get("abandoned") is True, res
    # The worker's commit survives; the tx is cleared (no destructive rollback).
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == worker_head
    assert update_merge.read_update_tx_strict()[0] == "absent"


def test_boot_recovery_rolls_back_interrupted_materialization(tmp_path, monkeypatch):
    repo, head, plan = _conflict_repo(tmp_path, monkeypatch)
    import supervisor.workers as workers

    gate_calls = []
    monkeypatch.setattr(
        workers,
        "close_repo_writer_admission",
        lambda reason: gate_calls.append(("close", reason)),
    )
    monkeypatch.setattr(
        workers,
        "open_repo_writer_admission",
        lambda expected_reason="": gate_calls.append(("open", expected_reason)),
    )
    _git(repo, "reset", "--hard", "HEAD")
    _git(repo, "clean", "-fd")
    _git(repo, "checkout", "-B", head, plan["local_snapshot"])
    _git(repo, "merge", "--no-commit", "--no-ff", plan["target_sha"])
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == plan["local_snapshot"]
    update_merge.write_update_tx({
        "phase": "materializing_assisted", "task_id": "t",
        "pre_update_sha": plan["base_sha"], "pre_update_branch": head,
        "local_snapshot": plan["local_snapshot"], "target_sha": plan["target_sha"],
    })

    result = update_merge.finalize_managed_update_on_boot(supervisor_ready=True)

    assert result.get("rolled_back") is True, result
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == plan["base_sha"]
    assert update_merge._merge_head_sha() == ""
    assert update_merge.read_update_tx_strict()[0] == "absent"
    assert gate_calls == [("close", "managed_update:rollback")]


def test_dirty_local_work_is_in_the_reviewed_diff(tmp_path, monkeypatch):
    """P3 regression: the owner's uncommitted/untracked local work must be part of the staged
    diff reviewed against pre_update_sha — never reachable in history as an unreviewed parent."""
    repo, head = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "remote-sim")
    (repo / "b.txt").write_text("official addition\n")  # disjoint official change (clean merge)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "official adds b")
    _git(repo, "checkout", "-q", head)
    (repo / "secret_local.txt").write_text("owner uncommitted work\n")  # untracked dirty work
    _point_at(monkeypatch, tmp_path, repo, head)
    plan = update_merge.plan_managed_update_merge(fetch=False)
    assert int(plan["local_dirty_count"]) > 0

    ok, msg = update_merge.materialize_assisted_merge_live(
        head, plan["local_snapshot"], plan["target_sha"], plan["base_sha"]
    )
    assert ok, msg
    _git(repo, "add", "-A")
    # The reviewed baseline is pre_update_sha — the dirty/untracked file appears in the diff,
    # so commit_reviewed's triad/scope WILL see it (it cannot slip in unreviewed).
    staged = _git(repo, "diff", "--cached", "--name-only", plan["base_sha"]).stdout.split()
    assert "secret_local.txt" in staged, staged
    assert "b.txt" in staged  # the official change is in the same reviewed diff


def _stub_worker_gates(monkeypatch):
    """Neutral worker-pool/admission stubs for rollback paths (parallel-safe)."""
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "ensure_worker_pool_started", lambda **_kwargs: True)
    monkeypatch.setattr(workers, "close_repo_writer_admission", lambda reason: None)
    monkeypatch.setattr(workers, "open_repo_writer_admission", lambda expected_reason="": None)


def _supervisor_events(tmp_path, event_type):
    path = tmp_path / "data" / "logs" / "supervisor.jsonl"
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("type") == event_type]


def _materialized_conflict_tx(tmp_path, monkeypatch):
    """A live materialized assisted merge with an UNCOMMITTED resolution in the worktree."""
    repo, head, plan = _conflict_repo(tmp_path, monkeypatch)
    ok, msg = update_merge.materialize_assisted_merge_live(
        head, plan["local_snapshot"], plan["target_sha"], plan["base_sha"]
    )
    assert ok, msg
    (repo / "a.txt").write_text("the resolver's precious resolution\n")
    tx = {
        "phase": "assisted_resolution", "task_id": "resolver",
        "pre_update_sha": plan["base_sha"], "pre_update_branch": head,
        "local_snapshot": plan["local_snapshot"], "target_sha": plan["target_sha"],
    }
    update_merge.write_update_tx(tx)
    return repo, head, plan, tx


def test_orphan_rollback_rescues_uncommitted_resolutions(tmp_path, monkeypatch):
    repo, head, plan, tx = _materialized_conflict_tx(tmp_path, monkeypatch)
    _stub_worker_gates(monkeypatch)
    # A rollback rescue must never flip an active evolution transaction to "abandoned".
    monkeypatch.setattr(
        git_ops, "_link_rescue_to_evolution_transaction",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("rollback rescue must not link to the evolution tx")
        ),
    )

    result = update_merge.abort_orphaned_assisted_tx("resolver", _authority_metadata(tx))

    assert result.get("rolled_back") is True, result
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == plan["base_sha"]
    rescue_dirs = list((tmp_path / "data" / "archive" / "rescue").iterdir())
    assert len(rescue_dirs) == 1
    assert "the resolver's precious resolution" in (
        rescue_dirs[0] / "changes.diff"
    ).read_text(encoding="utf-8")
    meta = json.loads((rescue_dirs[0] / "rescue_meta.json").read_text(encoding="utf-8"))
    assert meta["reason"] == "managed_update_rollback:assisted_resolution_orphaned"
    assert meta["merge_head"] == plan["target_sha"]
    assert int(meta["unmerged_count"]) > 0
    assert meta["rescue_stash_error"]  # stash create fails on an unmerged index — disclosed
    assert (rescue_dirs[0] / "unmerged.txt").read_text(encoding="utf-8").strip()
    # The hook writes its own durable line BEFORE the destructive reset — a crash
    # between clear_update_tx and the terminal event cannot hide the rescue.
    captured = _supervisor_events(tmp_path, "managed_update_rescue_captured")
    assert captured and captured[-1]["rescue_path"] == str(rescue_dirs[0])
    rolled = _supervisor_events(tmp_path, "managed_update_rolled_back")
    assert rolled and rolled[-1]["rescue_path"] == str(rescue_dirs[0])
    assert rolled[-1]["reason"] == "assisted_resolution_orphaned"
    assert rolled[-1].get("rescue_ts")


def test_boot_cap_rollback_rescues_before_reset(tmp_path, monkeypatch):
    repo, head, plan, tx = _materialized_conflict_tx(tmp_path, monkeypatch)
    tx["resolution_attempts"] = 4  # past _ASSISTED_BOOT_ATTEMPT_CAP on the next boot
    update_merge.write_update_tx(tx)
    _stub_worker_gates(monkeypatch)

    result = update_merge.finalize_managed_update_on_boot(supervisor_ready=True)

    assert result.get("rolled_back") is True, result
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == plan["base_sha"]
    rescue_dirs = list((tmp_path / "data" / "archive" / "rescue").iterdir())
    assert len(rescue_dirs) == 1
    assert "the resolver's precious resolution" in (
        rescue_dirs[0] / "changes.diff"
    ).read_text(encoding="utf-8")
    rolled = _supervisor_events(tmp_path, "managed_update_rolled_back")
    assert rolled and rolled[-1]["rescue_path"] == str(rescue_dirs[0])
    assert rolled[-1]["reason"] == "assisted_resolution_expired"


def test_rollback_on_clean_tree_creates_no_rescue(tmp_path, monkeypatch):
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    pre = _git(repo, "rev-parse", "HEAD").stdout.strip()
    update_merge.write_update_tx({
        "phase": "pending_boot_smoke", "pre_update_sha": pre, "pre_update_branch": head,
    })
    _stub_worker_gates(monkeypatch)

    ok, _message = update_merge.rollback_managed_update("clean_tree_test")

    assert ok is True
    assert not (tmp_path / "data" / "archive" / "rescue").exists()
    rolled = _supervisor_events(tmp_path, "managed_update_rolled_back")
    assert rolled
    assert "rescue_path" not in rolled[-1]
    assert "rescue_error" not in rolled[-1]


def test_rollback_replay_does_not_duplicate_rescue(tmp_path, monkeypatch):
    """No second snapshot when the tx ALREADY CARRIES a written rollback_rescue marker.

    Honest scope (accepted residual): the guarantee is at-least-once, not exactly-once —
    a crash in the window between creating the rescue dir and writing the tx marker
    replays the rescue and can leave one extra rescue dir on disk. That duplicate is
    cheap and durable; a two-phase planned/captured protocol was explicitly declined
    (Proportionality). This test pins the replay-with-marker case only."""
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    pre = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("dirt from the attempt that was already rescued\n")
    update_merge.write_update_tx({
        "phase": "rolling_back", "pre_update_sha": pre, "pre_update_branch": head,
        "rollback_rescue": {"path": "/rescued/earlier", "ref": "refs/rescue/x", "reason": "first"},
    })
    _stub_worker_gates(monkeypatch)
    monkeypatch.setattr(
        git_ops, "rescue_before_destructive_rollback",
        lambda reason, **_kw: (_ for _ in ()).throw(
            AssertionError("a rollback replay must not take a second rescue")
        ),
    )

    ok, _message = update_merge.rollback_managed_update("replay")

    assert ok is True
    rolled = _supervisor_events(tmp_path, "managed_update_rolled_back")
    assert rolled and rolled[-1]["rescue_path"] == "/rescued/earlier"
    assert rolled[-1]["rescue_ref"] == "refs/rescue/x"


def test_rescue_failure_is_fail_open_and_disclosed(tmp_path, monkeypatch):
    """Owner decision 4=A: a failed rescue never blocks the rollback — it is disclosed."""
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    pre = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("dirty work the rescue could not save\n")
    update_merge.write_update_tx({
        "phase": "pending_boot_smoke", "pre_update_sha": pre, "pre_update_branch": head,
    })
    _stub_worker_gates(monkeypatch)
    monkeypatch.setattr(
        git_ops, "_create_rescue_snapshot",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    ok, _message = update_merge.rollback_managed_update("rescue_fail")

    assert ok is True
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == pre
    rolled = _supervisor_events(tmp_path, "managed_update_rolled_back")
    assert rolled and "disk full" in rolled[-1]["rescue_error"]
    assert "rescue_path" not in rolled[-1]
    # The hook also wrote its own durable failure line before the reset.
    failed = _supervisor_events(tmp_path, "managed_update_rescue_failed")
    assert failed and "disk full" in failed[-1]["error"]
    assert failed[-1]["reason"] == "rescue_fail"


def test_failed_rollback_attempt_drops_marker_and_retry_rescues_fresh_tree(tmp_path, monkeypatch):
    """The rescue marker is per-ATTEMPT, not per-tx. A transient failure of the first
    destructive step must drop the just-written marker so the retry re-rescues the
    tree it actually finds — including second-generation work written in between."""
    import pathlib

    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    pre = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("first-generation resolution\n")
    update_merge.write_update_tx({
        "phase": "assisted_resolution", "task_id": "resolver",
        "pre_update_sha": pre, "pre_update_branch": head,
    })
    _stub_worker_gates(monkeypatch)
    real_git_capture = git_ops.git_capture
    armed = {"on": True}

    def flaky(cmd, *, timeout=None):  # one transient failure (index.lock class) on the first reset
        if armed["on"] and cmd == ["git", "reset", "--hard", "HEAD"]:
            armed["on"] = False
            return 1, "", "fatal: Unable to create '.git/index.lock': File exists."
        return real_git_capture(cmd, timeout=timeout)

    monkeypatch.setattr(git_ops, "git_capture", flaky)

    ok1, msg1 = update_merge.rollback_managed_update("attempt_one")

    assert ok1 is False and "reset failed" in msg1
    assert len(list((tmp_path / "data" / "archive" / "rescue").iterdir())) == 1
    # The stale first-attempt marker is gone — the retry re-runs the hook.
    assert "rollback_rescue" not in update_merge.read_update_tx()

    # The tree keeps moving before the retry (second-generation work).
    (repo / "a.txt").write_text("SECOND-GENERATION resolution\n")
    (repo / "brand_new_untracked.txt").write_text("also new\n")

    ok2, _msg2 = update_merge.rollback_managed_update("attempt_two")

    assert ok2 is True
    rescue_dirs = sorted((tmp_path / "data" / "archive" / "rescue").iterdir())
    assert len(rescue_dirs) == 2, "the retry must take a FRESH rescue of the moved tree"
    rolled = _supervisor_events(tmp_path, "managed_update_rolled_back")
    latest = pathlib.Path(rolled[-1]["rescue_path"])
    assert "SECOND-GENERATION resolution" in (latest / "changes.diff").read_text(encoding="utf-8")
    assert (latest / "untracked" / "brand_new_untracked.txt").exists()


def test_boot_rematerialize_rescues_dirty_work_and_points_resolver_at_it(tmp_path, monkeypatch):
    """The re-materialization reset (boot resume, has_progress=False) rescues surviving
    dirty resolutions, persists the tx pointer BEFORE materialize runs (a crash inside
    it must not lose the pointer), and the resumed resolver's objective points at it.
    A further boot keeps that pointer, because `materialize_assisted_merge_live` sets
    MERGE_HEAD and dirties the tree WITHOUT replaying the rescued edits — dropping the
    pointer on those two signals would lose the rescue nobody has read yet."""
    import supervisor.queue as queue
    import supervisor.workers as workers

    repo, head, plan, _tx = _materialized_conflict_tx(tmp_path, monkeypatch)
    (repo / "a.txt").write_text("half-finished resolution\n")
    # The residual class: MERGE_HEAD lost while dirty resolution work survives.
    (repo / ".git" / "MERGE_HEAD").unlink()
    assert update_merge._merge_head_sha() == ""
    _stub_worker_gates(monkeypatch)
    monkeypatch.setattr(workers, "PENDING", [])
    monkeypatch.setattr(workers, "RUNNING", {})
    captured = []
    monkeypatch.setattr(queue, "enqueue_task", lambda task, front=False: captured.append(task))
    persisted_before_materialize = []
    real_materialize = update_merge.materialize_assisted_merge_live

    def spying_materialize(*args, **kwargs):
        persisted_before_materialize.append(
            (update_merge.read_update_tx().get("progress_rescue") or {}).get("path")
        )
        return real_materialize(*args, **kwargs)

    monkeypatch.setattr(update_merge, "materialize_assisted_merge_live", spying_materialize)

    result = update_merge.finalize_managed_update_on_boot(supervisor_ready=True)

    assert result.get("resumed") is True, result
    rescue_dirs = list((tmp_path / "data" / "archive" / "rescue").iterdir())
    assert len(rescue_dirs) == 1
    assert "half-finished resolution" in (
        rescue_dirs[0] / "changes.diff"
    ).read_text(encoding="utf-8")
    # The durable pointer was already on disk when materialize started.
    assert persisted_before_materialize == [str(rescue_dirs[0])]
    stored = update_merge.read_update_tx()
    assert stored["progress_rescue"]["path"] == str(rescue_dirs[0])
    meta = json.loads((rescue_dirs[0] / "rescue_meta.json").read_text(encoding="utf-8"))
    assert meta["reason"] == "managed_update_rescue:assisted_rematerialize"  # not rollback:*
    assert captured, "the resumed resolver task must be enqueued"
    assert str(rescue_dirs[0]) in captured[0]["text"]
    assert "do not run git commands" in captured[0]["text"]
    # Second boot with the merge state intact (has_progress=True). "MERGE_HEAD +
    # dirty" is exactly what the materialize above just produced, and materialize
    # never re-applies the rescued edits — so this state is NOT evidence that the
    # work came back, and the pointer must survive into the next objective.
    result2 = update_merge.finalize_managed_update_on_boot(supervisor_ready=True)
    assert result2.get("resumed") is True, result2
    assert update_merge.read_update_tx()["progress_rescue"]["path"] == str(rescue_dirs[0])
    assert str(rescue_dirs[0]) in captured[-1]["text"]
    assert "was rescued to" in captured[-1]["text"]
