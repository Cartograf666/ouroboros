"""HTTP contract for the thread branching / checkout routes (T3).

    GET  /api/projects/{project_id}/threads/{thread_id}/branch-bases
    POST /api/projects/{project_id}/threads/{thread_id}/branch-off
    POST /api/projects/{project_id}/threads/{thread_id}/merge-back
    GET  /api/projects/{project_id}/threads/{thread_id}/worktree
    POST /api/projects/{project_id}/threads/{thread_id}/worktree/remove
    GET  /api/projects/{project_id}/threads/{thread_id}/diff

Owner surfaces, gateway-only and deliberately NOT LLM-callable tools: these
gestures touch the owner's own folder and history.

Hermetic against a REAL git repository. What is pinned is the transport
contract — statuses, the shared refusal envelope, and the fact that a refusal
carries a typed reason the UI can branch on rather than a stack trace.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros.gateway.project_threads import (
    api_thread_branch_bases,
    api_thread_branch_off,
    api_thread_diff,
    api_thread_merge_back,
    api_thread_worktree_inspect,
    api_thread_worktree_remove,
)
from ouroboros.project_threads_registry import create_thread
from ouroboros.projects_registry import create_project


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


@pytest.fixture(autouse=True)
def worktrees_under_tmp(tmp_path, monkeypatch):
    """Keep every provisioned checkout inside the test's own tmp tree."""
    monkeypatch.setenv("OUROBOROS_THREAD_WORKTREE_ROOT", str(tmp_path / "thread_worktrees"))


@pytest.fixture(autouse=True)
def quiet_broadcast(monkeypatch):
    import ouroboros.gateway.project_threads as gw

    monkeypatch.setattr(gw, "_broadcast_thread_change", lambda *a, **k: None)


@pytest.fixture()
def folder(tmp_path):
    root = tmp_path / "owner_folder"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "app.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    return root


def _client(drive_root: pathlib.Path) -> TestClient:
    base = "/api/projects/{project_id}/threads/{thread_id}"
    app = Starlette(routes=[
        Route(f"{base}/branch-bases", api_thread_branch_bases, methods=["GET"]),
        Route(f"{base}/branch-off", api_thread_branch_off, methods=["POST"]),
        Route(f"{base}/merge-back", api_thread_merge_back, methods=["POST"]),
        Route(f"{base}/worktree", api_thread_worktree_inspect, methods=["GET"]),
        Route(f"{base}/worktree/remove", api_thread_worktree_remove, methods=["POST"]),
        Route(f"{base}/diff", api_thread_diff, methods=["GET"]),
    ])
    app.state.drive_root = drive_root
    app.state.repo_dir = drive_root
    return TestClient(app)


@pytest.fixture()
def wired(tmp_path, folder):
    drive = tmp_path / "drive"
    create_project(drive, "racer", name="Racer", working_dir=str(folder))
    thread = create_thread(drive, "racer", name="Side quest")
    return _client(drive), drive, thread["id"], folder


def test_branch_bases_lists_the_offer(wired):
    client, _drive, tid, folder = wired
    _git(folder, "tag", "v1")

    body = client.get(f"/api/projects/racer/threads/{tid}/branch-bases").json()

    assert body["project_id"] == "racer"
    assert body["thread_id"] == tid
    assert body["current_branch"] == "main"
    assert body["location"]["where"] == "project_folder"
    assert "v1" in [row["ref"] for row in body["bases"]]
    assert body["snapshot"]["ref"] == "@snapshot"


def test_branch_off_then_the_checkout_diff_shows_that_checkouts_work(wired):
    """A13/X9: the per-task diff route structurally cannot answer this, so the
    thread route does — with the same envelope, same statuses, same patch bytes."""
    client, _drive, tid, _folder = wired

    empty = client.get(f"/api/projects/racer/threads/{tid}/diff").json()
    assert empty["status"] == "blocked"
    assert empty["blockers"] == ["thread_not_branched"]
    assert empty["source"] == "thread_checkout"

    branched = client.post(f"/api/projects/racer/threads/{tid}/branch-off", json={}).json()
    assert branched["ok"] is True, branched
    checkout = pathlib.Path(branched["path"])

    clean = client.get(f"/api/projects/racer/threads/{tid}/diff").json()
    assert clean["status"] == "empty"
    assert clean["patch"] == ""

    (checkout / "app.txt").write_text("changed by the thread\n", encoding="utf-8")
    (checkout / "brand_new.txt").write_text("new file\n", encoding="utf-8")

    ready = client.get(f"/api/projects/racer/threads/{tid}/diff").json()
    assert ready["status"] == "ready"
    assert ready["project_id"] == "racer" and ready["thread_id"] == tid
    assert ready["source"] == "thread_checkout"
    assert ready["base_commit"] == branched["base_sha"]
    # Unsaved edits AND untracked new files, because that is what the owner sees
    # when they open that folder.
    assert "changed by the thread" in ready["patch"]
    assert "brand_new.txt" in ready["patch"]
    assert ready["patch_sha256"]


def test_diff_of_an_unknown_thread_is_the_only_404(wired):
    client, _drive, _tid, _folder = wired
    assert client.get("/api/projects/racer/threads/999/diff").status_code == 404


def test_merge_back_conflict_is_a_409_with_its_paths(wired):
    client, _drive, tid, folder = wired
    branched = client.post(f"/api/projects/racer/threads/{tid}/branch-off", json={}).json()
    checkout = pathlib.Path(branched["path"])
    (checkout / "app.txt").write_text("thread version\n", encoding="utf-8")
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "T")
    _git(checkout, "commit", "-qam", "thread edit")
    (folder / "app.txt").write_text("owner version\n", encoding="utf-8")
    _git(folder, "commit", "-qam", "owner edit")

    response = client.post(f"/api/projects/racer/threads/{tid}/merge-back", json={})

    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert body["reason"] == "merge_conflict"
    assert body["conflicts"] == ["app.txt"]
    assert body["message"]
    # The thread is still where it was, with its branch intact.
    assert body["branch"]
    assert (folder / "app.txt").read_text(encoding="utf-8") == "owner version\n"


def test_removal_refuses_unmerged_work_until_the_owner_acknowledges_it(wired):
    """A10, end to end: the inspection is SHOWN, the refusal names the stakes,
    and the acknowledgement is the only way through."""
    client, _drive, tid, _folder = wired
    branched = client.post(f"/api/projects/racer/threads/{tid}/branch-off", json={}).json()
    checkout = pathlib.Path(branched["path"])
    (checkout / "app.txt").write_text("unsaved thread work\n", encoding="utf-8")

    inspected = client.get(f"/api/projects/racer/threads/{tid}/worktree").json()
    assert inspected["inspection"]["dirty"] is True
    assert inspected["location"]["where"] == "worktree"

    refused = client.post(f"/api/projects/racer/threads/{tid}/worktree/remove", json={})
    assert refused.status_code == 409
    body = refused.json()
    assert body["removed"] is False
    assert body["reason"] == "unmerged_work"
    assert "Removing it deletes that work" in body["message"]
    assert checkout.is_dir()

    removed = client.post(
        f"/api/projects/racer/threads/{tid}/worktree/remove",
        json={"acknowledge_unmerged": True},
    )
    assert removed.status_code == 200
    assert removed.json()["removed"] is True
    assert removed.json()["location"]["where"] == "project_folder"
    assert not checkout.exists()


def test_removal_refuses_while_a_task_is_running_in_the_project(wired, monkeypatch):
    """T3R2-H5 at the route: removal deletes a folder a task may be writing in,
    and the route has a sentence for that reason instead of a bare fallback."""
    from supervisor import workers

    client, _drive, tid, folder = wired
    branched = client.post(f"/api/projects/racer/threads/{tid}/branch-off", json={}).json()
    checkout = pathlib.Path(branched["path"])
    monkeypatch.setitem(
        workers.RUNNING, "live",
        {"task": {"id": "live", "project_id": "racer", "workspace_root": str(checkout)}},
    )
    try:
        refused = client.post(
            f"/api/projects/racer/threads/{tid}/worktree/remove",
            json={"acknowledge_unmerged": True},
        )
    finally:
        workers.RUNNING.pop("live", None)

    assert refused.status_code == 409
    body = refused.json()
    assert body["removed"] is False
    assert body["reason"] == "project_busy"
    assert "until that task finishes" in body["message"]
    assert checkout.is_dir()
    # A WAIT, not a dead end: with the task gone it removes.
    assert client.post(
        f"/api/projects/racer/threads/{tid}/worktree/remove",
        json={"acknowledge_unmerged": True},
    ).json()["removed"] is True


def test_a_folderless_project_refuses_with_a_typed_reason_not_a_500(tmp_path):
    drive = tmp_path / "drive"
    create_project(drive, "placeless", name="Placeless")
    thread = create_thread(drive, "placeless", name="Side quest")
    client = _client(drive)

    response = client.post(
        f"/api/projects/placeless/threads/{thread['id']}/branch-off", json={},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False and body["reason"] == "no_project_folder"
    assert body["message"]


def test_an_unknown_base_is_a_400(wired):
    client, _drive, tid, _folder = wired

    response = client.post(
        f"/api/projects/racer/threads/{tid}/branch-off", json={"base_ref": "nope"},
    )

    assert response.status_code == 400
    assert response.json()["reason"] == "unknown_base"


# --------------------------------------------------------------------------- #
# Lifecycle (D4 / X10) over HTTP
# --------------------------------------------------------------------------- #

def _lifecycle_client(drive_root: pathlib.Path) -> TestClient:
    from ouroboros.gateway.project_threads import (
        api_thread_archive,
        api_thread_delete,
        api_thread_restore,
    )

    base = "/api/projects/{project_id}/threads/{thread_id}"
    app = Starlette(routes=[
        Route(f"{base}/archive", api_thread_archive, methods=["POST"]),
        Route(f"{base}/restore", api_thread_restore, methods=["POST"]),
        Route(f"{base}/delete", api_thread_delete, methods=["POST"]),
    ])
    app.state.drive_root = drive_root
    app.state.repo_dir = drive_root
    return TestClient(app)


def test_archive_and_restore_round_trip_over_http(tmp_path):
    drive = tmp_path / "drive"
    create_project(drive, "racer", name="Racer")
    thread = create_thread(drive, "racer", name="Side quest")
    client = _lifecycle_client(drive)

    archived = client.post(f"/api/projects/racer/threads/{thread['id']}/archive", json={}).json()
    assert archived["ok"] is True
    assert archived["lifecycle"] == "archived"
    assert archived["archived_at"]
    # Nothing is running, so nothing is being kept visible against the owner's wish.
    assert archived["visible_until_terminal"] is False

    restored = client.post(f"/api/projects/racer/threads/{thread['id']}/restore", json={}).json()
    assert restored["lifecycle"] == "active"


def test_archiving_thread_zero_is_a_409_that_says_where_the_operation_lives(tmp_path):
    drive = tmp_path / "drive"
    create_project(drive, "racer", name="Racer")
    client = _lifecycle_client(drive)

    response = client.post("/api/projects/racer/threads/0/archive", json={})

    assert response.status_code == 409
    body = response.json()
    assert body["reason"] == "thread_zero_is_the_project"
    assert "project" in body["message"].lower()


def test_delete_takes_a_CLEAN_checkout_with_it_and_says_so(tmp_path, folder, monkeypatch):
    """T3R2-M2, owner-directed: deleting a thread must delete its worktree too.

    A tombstoned thread is invisible on every surface, `list_thread_worktrees` has
    no route and no UI consumer, and branch/merge refuse `thread_not_live` — so a
    checkout left behind is a folder AND a branch that A10's explicit removal can
    no longer reach, on durable state exempt from every GC. A CLEAN one (nothing
    uncommitted, no commit the project folder lacks) is exactly what A10/D4
    already offer one-click removal for, so it goes with the thread and the answer
    says it did. X10 is unchanged: the fence is up, the tombstone is not.
    """
    started: list = []
    monkeypatch.setattr(
        "supervisor.task_lifecycle.start_thread_deletion",
        lambda drive_root, pid, tid, chat_id: started.append((pid, tid, chat_id)) or True,
    )
    drive = tmp_path / "drive"
    create_project(drive, "racer", name="Racer", working_dir=str(folder))
    thread = create_thread(drive, "racer", name="Doomed")
    branched = _client(drive).post(
        f"/api/projects/racer/threads/{thread['id']}/branch-off", json={},
    ).json()
    assert branched["ok"] is True

    body = _lifecycle_client(drive).post(
        f"/api/projects/racer/threads/{thread['id']}/delete", json={},
    ).json()

    assert body["ok"] is True
    # Fenced, NOT yet tombstoned: its tasks are still being cancelled.
    assert body["lifecycle"] == "deleting"
    assert started and started[0][1] == int(thread["id"])
    assert body["journal_rows_retained"] is True
    # The checkout AND its branch went with it — disclosed, never silent.
    assert body["worktree_removed"] is True
    assert body["worktree_kept"] is False
    assert body["branch"] == branched["branch"]
    assert body["branch_removed"] is True
    assert not pathlib.Path(branched["path"]).exists()
    assert branched["branch"] not in _git(folder, "branch", "--list").stdout


def test_delete_REFUSES_while_the_checkout_still_holds_work(tmp_path, folder, monkeypatch):
    """...and only a CLEAN one. Work the owner has not seen is never destroyed by
    a gesture aimed at something else; the refusal names the explicit route."""
    started: list = []
    monkeypatch.setattr(
        "supervisor.task_lifecycle.start_thread_deletion",
        lambda drive_root, pid, tid, chat_id: started.append(tid) or True,
    )
    drive = tmp_path / "drive"
    create_project(drive, "racer", name="Racer", working_dir=str(folder))
    thread = create_thread(drive, "racer", name="Doomed")
    branched = _client(drive).post(
        f"/api/projects/racer/threads/{thread['id']}/branch-off", json={},
    ).json()
    checkout = pathlib.Path(branched["path"])
    (checkout / "unsaved.txt").write_text("hours of work\n", encoding="utf-8")

    response = _lifecycle_client(drive).post(
        f"/api/projects/racer/threads/{thread['id']}/delete", json={},
    )
    body = response.json()

    assert response.status_code == 409
    assert body["ok"] is False
    assert body["reason"] == "checkout_holds_work"
    assert "cannot be deleted" in body["message"]
    assert body["inspection"]["dirty"] is True
    # Nothing happened: not fenced, not tombstoned, checkout intact.
    assert started == []
    assert (checkout / "unsaved.txt").is_file()
    from ouroboros.projects_registry import get_thread

    assert get_thread(drive, "racer", thread["id"])["lifecycle"] == "active"


def test_branch_bases_carries_the_honest_queue_notice(wired, monkeypatch):
    """A14: the sentence says QUEUED, not rejected, and offers branching."""
    from supervisor import workers

    client, _drive, tid, folder = wired

    quiet = client.get(f"/api/projects/racer/threads/{tid}/branch-bases").json()
    assert quiet["queue_notice"]["queued"] is False
    assert quiet["queue_notice"]["message"] == ""

    monkeypatch.setitem(
        workers.RUNNING, "t1",
        {"task": {"id": "t1", "project_id": "racer", "workspace_root": str(folder)}},
    )
    try:
        busy = client.get(f"/api/projects/racer/threads/{tid}/branch-bases").json()
    finally:
        workers.RUNNING.pop("t1", None)

    notice = busy["queue_notice"]
    assert notice["queued"] is True
    assert "QUEUED" in notice["message"]
    assert "rejected" in notice["message"]  # ...as the thing it explicitly is NOT
    assert "is not rejected" in notice["message"]
    assert notice["remedy"] == "branch_off"


def test_the_queue_notice_names_the_FOLDER_not_a_guess_about_who_holds_it(wired, monkeypatch):
    """T3R-15. After T0R2-5 the writer lane is keyed on the FOLDER alone, across
    projects and threads alike — so whatever is holding it may belong to another
    thread, another project, or no project at all.

    "Another thread in this project" was a guess about the occupant, and a wrong
    guess sends the owner looking for a room that is not the one making them wait.
    """
    from ouroboros.thread_branching import QUEUE_NOTICE
    from supervisor import workers

    client, _drive, tid, folder = wired
    # The occupant belongs to a DIFFERENT project, in the same folder.
    monkeypatch.setitem(
        workers.RUNNING, "other",
        {"task": {"id": "other", "project_id": "someone-else", "workspace_root": str(folder)}},
    )
    try:
        notice = client.get(
            f"/api/projects/racer/threads/{tid}/branch-bases"
        ).json()["queue_notice"]
    finally:
        workers.RUNNING.pop("other", None)

    assert notice["queued"] is True
    assert notice["message"] == QUEUE_NOTICE
    assert "Another task is working in this folder" in notice["message"]
    assert "this project" not in notice["message"], "the occupant's project is not known here"


def test_a_broken_queue_notice_never_takes_the_BASES_LIST_down_with_it(wired, monkeypatch):
    """T3R-13. The notice is the least important thing on this answer — an
    advisory beside the list of bases the owner actually came for.

    Its fail-open guard covered only the queue READ. The `project_lease` import
    and the `candidate_is_leasable` call sat OUTSIDE it — and that call raises
    TypeError by contract on a malformed lane — so anything wrong there 500'd the
    whole route and the owner lost their bases list to a sentence about waiting.
    """
    import ouroboros.project_lease as lease

    client, _drive, tid, folder = wired
    monkeypatch.setitem(
        workers_module().RUNNING, "t1",
        {"task": {"id": "t1", "project_id": "racer", "workspace_root": str(folder)}},
    )

    def _explode(*_a, **_kw):
        raise TypeError("candidate_is_leasable expects lane keys")

    monkeypatch.setattr(lease, "candidate_is_leasable", _explode)
    try:
        response = client.get(f"/api/projects/racer/threads/{tid}/branch-bases")
    finally:
        workers_module().RUNNING.pop("t1", None)

    assert response.status_code == 200
    body = response.json()
    assert body["queue_notice"] == {"queued": False, "reason": "", "message": "", "remedy": ""}
    # The answer the owner came for is intact.
    assert body["current_branch"] == "main"
    assert body["snapshot"]["ref"] == "@snapshot"


def test_a_queue_notice_whose_own_IMPORT_fails_is_still_only_a_missing_sentence(wired, monkeypatch):
    """The import itself was outside the guard too, so a `project_lease` that
    would not load took the route with it."""
    import sys

    from ouroboros.thread_branching import queue_notice

    client, drive, tid, _folder = wired
    monkeypatch.setitem(sys.modules, "ouroboros.project_lease", None)

    assert queue_notice(drive, "racer", tid) == {
        "queued": False, "reason": "", "message": "", "remedy": "",
    }
    assert client.get(f"/api/projects/racer/threads/{tid}/branch-bases").status_code == 200


def workers_module():
    from supervisor import workers

    return workers
