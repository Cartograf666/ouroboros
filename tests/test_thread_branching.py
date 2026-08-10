"""BRANCH OFF / MERGE BACK against REAL git (A7-A10).

Every test here runs `git init`, `git worktree add` and — where it matters — a
real conflicting merge. Mocking git would pin our beliefs about git rather than
git's behaviour, and the two operations this module owns are exactly the place
where that difference destroys an owner's work.

What is pinned:

* a thread's LOCATION is derived from the worktree's existence, never stored;
* the owner CHOOSES the base, including "exactly as it is now" (a snapshot
  commit that leaves credential-shaped files out);
* a project with no folder and a project with no git are refused with the TYPED
  decisions T2 established, not with an error the owner has to decode;
* merge-back preconditions are the project-WIDE activity query and a clean local
  tree;
* a conflict is SHOWN, the merge is ABORTED, and the thread keeps its branch;
* a successful merge does NOT remove the checkout (A10).
"""

from __future__ import annotations

import subprocess

import pytest

from ouroboros.project_threads_registry import create_thread
from ouroboros.projects_registry import create_project
from ouroboros.thread_branching import (
    BASE_SNAPSHOT,
    REASON_ALREADY_BRANCHED,
    REASON_GIT_INIT_REQUIRED,
    REASON_LOCAL_TREE_DIRTY,
    REASON_MERGE_CONFLICT,
    REASON_NOT_BRANCHED,
    REASON_NO_FOLDER,
    REASON_PROJECT_BUSY,
    REASON_UNKNOWN_BASE,
    branch_off_bases,
    branch_off_thread,
    merge_back_thread,
    thread_location,
)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


@pytest.fixture()
def folder(tmp_path):
    """A real git repository standing in for the owner's project folder."""
    root = tmp_path / "owner_folder"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "app.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    return root


@pytest.fixture()
def drive(tmp_path):
    return tmp_path / "drive"


@pytest.fixture()
def wt_root(tmp_path):
    return tmp_path / "thread_worktrees"


def _project(drive, folder, pid="racer"):
    create_project(drive, pid, name="Racer", working_dir=str(folder))
    return create_thread(drive, pid, name="Side quest")


def _branch(drive, pid, tid, wt_root, base_ref=""):
    return branch_off_thread(
        drive, pid, tid, base_ref=base_ref, data_dir=drive, worktree_root=wt_root,
    )


# --------------------------------------------------------------------------- #
# Refusals BEFORE any git work
# --------------------------------------------------------------------------- #

def test_a_folderless_project_cannot_branch(drive):
    """A11: branching off needs a place to branch FROM."""
    create_project(drive, "placeless", name="Placeless")
    thread = create_thread(drive, "placeless", name="Side quest")

    out = branch_off_thread(drive, "placeless", thread["id"], data_dir=drive)

    assert out["ok"] is False
    assert out["reason"] == REASON_NO_FOLDER
    assert "no working folder" in out["message"]


def test_a_non_git_folder_gets_T2s_typed_offer_not_an_error(drive, tmp_path):
    """A12/X5: git is OFFERED. The refusal carries the SAME typed decision the
    task-admission path returns, so the owner is asked one question, not two."""
    plain = tmp_path / "plain_folder"
    plain.mkdir()
    (plain / "notes.txt").write_text("hi\n", encoding="utf-8")
    create_project(drive, "plain", name="Plain", working_dir=str(plain))
    thread = create_thread(drive, "plain", name="Side quest")

    out = branch_off_thread(drive, "plain", thread["id"], data_dir=drive)

    assert out["ok"] is False
    assert out["reason"] == REASON_GIT_INIT_REQUIRED
    decision = out["decision"]
    assert decision["decision"] == "git_init_required"
    assert decision["offer"] == "init_git"
    assert decision["project_id"] == "plain"
    assert "branching" in decision["enables"]


def test_an_unknown_base_is_refused_before_anything_is_provisioned(drive, folder, wt_root):
    thread = _project(drive, folder)

    out = _branch(drive, "racer", thread["id"], wt_root, base_ref="no-such-ref")

    assert out["ok"] is False
    assert out["reason"] == REASON_UNKNOWN_BASE
    assert thread_location(drive, "racer", thread["id"])["where"] == "project_folder"


# --------------------------------------------------------------------------- #
# BRANCH OFF
# --------------------------------------------------------------------------- #

def test_bases_offer_branches_tags_and_as_it_is_now(drive, folder):
    """A8: the list is an OFFER. "As it is now" is one entry in it, always
    present, and it discloses whether choosing it would create a commit."""
    _git(folder, "branch", "experiment")
    _git(folder, "tag", "v1")

    listed = branch_off_bases(folder)

    assert listed["current_branch"] == "main"
    refs = [row["ref"] for row in listed["bases"]]
    assert refs[0] == "main" and "(current)" in listed["bases"][0]["label"]
    assert {"experiment", "v1"} <= set(refs)
    assert {row["kind"] for row in listed["bases"]} == {"branch", "tag"}
    assert listed["snapshot"]["ref"] == BASE_SNAPSHOT
    assert listed["snapshot"]["dirty"] is False
    assert listed["snapshot"]["creates_commit"] is False

    (folder / "app.txt").write_text("edited\n", encoding="utf-8")
    dirty = branch_off_bases(folder)
    assert dirty["snapshot"]["dirty"] is True
    assert dirty["snapshot"]["creates_commit"] is True


def test_branch_off_provisions_a_real_worktree_and_derives_the_location(drive, folder, wt_root):
    from pathlib import Path

    thread = _project(drive, folder)
    assert thread_location(drive, "racer", thread["id"])["where"] == "project_folder"

    out = _branch(drive, "racer", thread["id"], wt_root)

    assert out["ok"] is True, out
    checkout = Path(out["path"])
    assert checkout.is_dir()
    assert (checkout / "app.txt").read_text(encoding="utf-8") == "one\n"
    listed = _git(folder, "worktree", "list").stdout
    assert str(checkout) in listed
    # A7: the location is derived from the worktree existing, not from a flag.
    where = thread_location(drive, "racer", thread["id"])
    assert where["where"] == "worktree"
    assert where["path"] == str(checkout)
    assert where["branch"] == out["branch"]


def test_branch_off_from_a_chosen_branch_uses_that_branchs_content(drive, folder, wt_root):
    from pathlib import Path

    _git(folder, "checkout", "-q", "-b", "experiment")
    (folder / "app.txt").write_text("experimental\n", encoding="utf-8")
    _git(folder, "commit", "-qam", "experiment work")
    _git(folder, "checkout", "-q", "main")
    thread = _project(drive, folder)

    out = _branch(drive, "racer", thread["id"], wt_root, base_ref="experiment")

    assert out["ok"] is True, out
    assert (Path(out["path"]) / "app.txt").read_text(encoding="utf-8") == "experimental\n"


def test_as_it_is_now_snapshots_uncommitted_work_and_leaves_secrets_out(drive, folder, wt_root):
    """A8's only special case. The snapshot is disclosed: its sha comes back, and
    so do the credential-shaped files deliberately kept out of git history."""
    from pathlib import Path

    (folder / "app.txt").write_text("unsaved edit\n", encoding="utf-8")
    (folder / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    thread = _project(drive, folder)

    out = _branch(drive, "racer", thread["id"], wt_root, base_ref=BASE_SNAPSHOT)

    assert out["ok"] is True, out
    snapshot = out["snapshot_commit"]
    assert snapshot["created"] is True
    assert snapshot["sha"]
    assert ".env" in snapshot["skipped_sensitive"]
    checkout = Path(out["path"])
    assert (checkout / "app.txt").read_text(encoding="utf-8") == "unsaved edit\n"
    assert not (checkout / ".env").exists(), "a snapshot must never bake a secret into history"
    tracked = _git(folder, "ls-files").stdout.split()
    assert ".env" not in tracked


def test_as_it_is_now_on_a_clean_tree_makes_no_commit(drive, folder, wt_root):
    """"Exactly as it is now" of a clean folder is already a commit — HEAD."""
    before = _git(folder, "rev-parse", "HEAD").stdout.strip()
    thread = _project(drive, folder)

    out = _branch(drive, "racer", thread["id"], wt_root, base_ref=BASE_SNAPSHOT)

    assert out["ok"] is True, out
    assert out["snapshot_commit"]["created"] is False
    assert out["snapshot_commit"]["sha"] == before
    assert _git(folder, "rev-parse", "HEAD").stdout.strip() == before


def test_branching_twice_is_refused_rather_than_resetting_the_first(drive, folder, wt_root):
    """The durable registry never clobbers an owner's checkout (X3)."""
    thread = _project(drive, folder)
    first = _branch(drive, "racer", thread["id"], wt_root)
    assert first["ok"] is True

    second = _branch(drive, "racer", thread["id"], wt_root)

    assert second["ok"] is False
    assert second["reason"] == REASON_ALREADY_BRANCHED
    assert second["location"]["path"] == first["path"]


# --------------------------------------------------------------------------- #
# MERGE BACK
# --------------------------------------------------------------------------- #

def _commit_in(checkout, name, body):
    from pathlib import Path

    Path(checkout, name).write_text(body, encoding="utf-8")
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "T")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", f"thread work on {name}")


def test_merge_back_brings_the_threads_commits_home_and_keeps_the_checkout(drive, folder, wt_root):
    """A9 happy path + A10: merging never removes the worktree."""
    from pathlib import Path

    thread = _project(drive, folder)
    out = _branch(drive, "racer", thread["id"], wt_root)
    _commit_in(out["path"], "feature.txt", "from the thread\n")

    merged = merge_back_thread(drive, "racer", thread["id"], data_dir=drive, busy=False)

    assert merged["ok"] is True, merged
    assert merged["merged"] is True
    assert (folder / "feature.txt").read_text(encoding="utf-8") == "from the thread\n"
    assert merged["worktree_kept"] is True
    assert Path(out["path"]).is_dir()
    assert thread_location(drive, "racer", thread["id"])["where"] == "worktree"


def test_merge_back_refuses_while_the_project_is_busy(drive, folder, wt_root):
    """A9's first precondition, and A14's honesty: the copy explains WAITING."""
    thread = _project(drive, folder)
    out = _branch(drive, "racer", thread["id"], wt_root)
    _commit_in(out["path"], "feature.txt", "from the thread\n")

    refused = merge_back_thread(drive, "racer", thread["id"], data_dir=drive, busy=True)

    assert refused["ok"] is False
    assert refused["reason"] == REASON_PROJECT_BUSY
    assert "until that task finishes" in refused["message"]
    assert not (folder / "feature.txt").exists()


def test_merge_back_refuses_a_dirty_local_tree_and_names_the_files(drive, folder, wt_root):
    thread = _project(drive, folder)
    out = _branch(drive, "racer", thread["id"], wt_root)
    _commit_in(out["path"], "feature.txt", "from the thread\n")
    (folder / "app.txt").write_text("owner is mid-edit\n", encoding="utf-8")

    refused = merge_back_thread(drive, "racer", thread["id"], data_dir=drive, busy=False)

    assert refused["ok"] is False
    assert refused["reason"] == REASON_LOCAL_TREE_DIRTY
    assert any("app.txt" in row for row in refused["dirty_files"])
    assert (folder / "app.txt").read_text(encoding="utf-8") == "owner is mid-edit\n"


def test_a_real_conflict_is_shown_stops_the_merge_and_leaves_both_sides_intact(drive, folder, wt_root):
    """A9's hard rule, against a REAL conflicting merge.

    The owner's folder must come out byte-for-byte as it went in — no conflict
    markers, no half-merge, no MERGE_HEAD — and the thread must keep its branch
    and every commit in it.
    """
    from pathlib import Path

    thread = _project(drive, folder)
    out = _branch(drive, "racer", thread["id"], wt_root)
    _commit_in(out["path"], "app.txt", "the thread's version\n")
    (folder / "app.txt").write_text("the owner's version\n", encoding="utf-8")
    _git(folder, "commit", "-qam", "owner edit")
    owner_head = _git(folder, "rev-parse", "HEAD").stdout.strip()
    thread_head = _git(out["path"], "rev-parse", "HEAD").stdout.strip()

    refused = merge_back_thread(drive, "racer", thread["id"], data_dir=drive, busy=False)

    assert refused["ok"] is False
    assert refused["reason"] == REASON_MERGE_CONFLICT
    assert refused["conflicts"] == ["app.txt"]
    # The folder is exactly as it was: same HEAD, clean tree, no merge in flight.
    assert _git(folder, "rev-parse", "HEAD").stdout.strip() == owner_head
    assert _git(folder, "status", "--porcelain").stdout.strip() == ""
    assert (folder / "app.txt").read_text(encoding="utf-8") == "the owner's version\n"
    assert not (Path(folder) / ".git" / "MERGE_HEAD").exists()
    # The thread stays in its branch with its work intact.
    assert _git(out["path"], "rev-parse", "HEAD").stdout.strip() == thread_head
    assert thread_location(drive, "racer", thread["id"])["where"] == "worktree"


def test_an_unbranched_thread_has_nothing_to_merge(drive, folder, wt_root):
    thread = _project(drive, folder)

    refused = merge_back_thread(drive, "racer", thread["id"], data_dir=drive, busy=False)

    assert refused["ok"] is False
    assert refused["reason"] == REASON_NOT_BRANCHED


def test_project_is_busy_reads_the_project_WIDE_activity_query(monkeypatch):
    """A9 reads "is anything running anywhere in this project", NOT the writer
    lane: a task running in a DIFFERENT folder of the same project still blocks
    a merge, because a merge touches the project as a whole."""
    import ouroboros.project_lease as lease
    import ouroboros.thread_branching as branching
    from supervisor import workers

    elsewhere = {"id": "t1", "project_id": "racer", "workspace_root": "/somewhere/else"}
    monkeypatch.setitem(workers.RUNNING, "t1", {"task": elsewhere})
    try:
        # The lane would say "different folder, go ahead"; the activity query
        # says the project is busy, and that is the one merge-back reads.
        assert lease.running_project_lanes(workers.RUNNING.values()) == {
            ("", __import__("os").path.normcase("/somewhere/else"))
        }
        assert branching.project_is_busy("racer") is True
        assert branching.project_is_busy("other-project") is False
    finally:
        workers.RUNNING.pop("t1", None)


def test_project_is_busy_fails_closed(monkeypatch):
    """"Cannot tell" must never license a merge into a folder something may be
    writing in."""
    import ouroboros.project_lease as lease
    import ouroboros.thread_branching as branching

    def _explode(_running):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(lease, "running_project_ids", _explode)

    assert branching.project_is_busy("racer") is True


def test_a_thread_being_deleted_cannot_branch_off_or_merge_back(drive, folder, wt_root):
    """A fenced thread is closed to routing and having its tasks cancelled.
    Provisioning a checkout for it — or merging its branch — would be work on a
    room the owner has already written off."""
    from ouroboros.projects_registry import begin_thread_deletion
    from ouroboros.thread_branching import REASON_THREAD_NOT_LIVE

    thread = _project(drive, folder)
    branched = _branch(drive, "racer", thread["id"], wt_root)
    assert branched["ok"] is True
    begin_thread_deletion(drive, "racer", thread["id"])

    merged = merge_back_thread(drive, "racer", thread["id"], data_dir=drive, busy=False)
    assert merged["ok"] is False
    assert merged["reason"] == REASON_THREAD_NOT_LIVE

    other = create_thread(drive, "racer", name="Also doomed")
    begin_thread_deletion(drive, "racer", other["id"])
    refused = _branch(drive, "racer", other["id"], wt_root)
    assert refused["ok"] is False
    assert refused["reason"] == REASON_THREAD_NOT_LIVE


def test_an_ARCHIVED_thread_can_still_branch_off(drive, folder, wt_root):
    """Archiving hides a thread; it does not close it."""
    from ouroboros.projects_registry import archive_thread

    thread = _project(drive, folder)
    archive_thread(drive, "racer", thread["id"])

    out = _branch(drive, "racer", thread["id"], wt_root)

    assert out["ok"] is True, out
