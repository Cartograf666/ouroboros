"""Phase T2 — a project always has a PLACE, and git is offered rather than forced.

A11: attaching an existing folder, cloning, or auto-provisioning are all legitimate
ways to give a project its working folder. A12: an attached folder that is not under
git STAYS not under git until the owner says yes — auto-``git init`` in someone
else's folder is forbidden, so admission stops before the first FILE task with the
typed ``git_init_required`` decision instead of either refusing the folder outright
(what it used to do) or quietly initialising it.

Sibling coverage: the entry-point admissions live in
``tests/test_v6590_projects_entry.py`` and the admission SSOT itself in
``tests/test_v6580_projects_foundation.py``.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import types

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


def _init_git_repo(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    (path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@local", "commit", "-qm", "init"],
        cwd=str(path), check=True,
    )


class _ProjectsReq:
    """The minimal request shape the gateway projects handlers read."""

    def __init__(self, body, *, drive_root, repo_dir, path_params=None):
        self._body = body
        self.path_params = dict(path_params or {})
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(drive_root=drive_root, repo_dir=repo_dir)
        )

    async def json(self):
        return self._body


# --- the direct task API returns the decision instead of queueing ------------------

def test_api_tasks_create_returns_the_typed_git_offer_and_queues_nothing(tmp_path, monkeypatch):
    """The owner asked for file work in an untracked folder. Neither answer on its
    own is right: queueing it would mean editing files with no diff and no way back,
    and running `git init` for them would mutate a folder Ouroboros does not own. So
    the task is NOT queued and the browser gets a typed OFFER it can render."""
    from ouroboros.gateway.tasks import api_tasks_create

    repo = tmp_path / "repo"
    repo.mkdir()
    data = tmp_path / "data"
    (data / "memory").mkdir(parents=True)
    plain = tmp_path / "plain_workspace"
    plain.mkdir()

    enqueued: list = []
    monkeypatch.setattr("supervisor.workers.WORKERS", {0: object()})
    monkeypatch.setattr("supervisor.workers._WORKER_POOL_DISABLED_REASON", "")
    monkeypatch.setattr("supervisor.queue.enqueue_task", lambda task: enqueued.append(dict(task)) or task)
    monkeypatch.setattr("supervisor.queue.persist_queue_snapshot", lambda reason="": True)

    app = Starlette(routes=[Route("/api/tasks", endpoint=api_tasks_create, methods=["POST"])])
    app.state.drive_root = data
    app.state.repo_dir = repo
    resp = TestClient(app).post(
        "/api/tasks", json={"description": "edit the files", "workspace_root": str(plain)}
    )

    assert resp.status_code == 400
    payload = resp.json()
    assert payload["error_code"] == "git_init_required"
    decision = payload["decision"]
    assert decision["decision"] == "git_init_required"
    assert decision["workspace_root"] == str(plain.resolve())
    assert decision["offer"] == "init_git"
    assert decision["enables"] == ["diff", "rollback", "branching"]
    assert "not tracked by git" in decision["message"]
    assert enqueued == [], "the task must not be queued while the offer is unanswered"
    assert not (plain / ".git").exists(), "admission must NEVER initialise git itself"


def test_api_tasks_create_still_admits_a_git_worktree_root(tmp_path, monkeypatch):
    """Guard on the change above: the ordinary git workspace is untouched."""
    from ouroboros.gateway.tasks import api_tasks_create

    repo = tmp_path / "repo"
    repo.mkdir()
    data = tmp_path / "data"
    (data / "memory").mkdir(parents=True)
    ws = tmp_path / "tracked"
    _init_git_repo(ws)

    enqueued: list = []
    monkeypatch.setattr("supervisor.workers.WORKERS", {0: object()})
    monkeypatch.setattr("supervisor.workers._WORKER_POOL_DISABLED_REASON", "")
    monkeypatch.setattr("supervisor.queue.enqueue_task", lambda task: enqueued.append(dict(task)) or task)
    monkeypatch.setattr("supervisor.queue.persist_queue_snapshot", lambda reason="": True)

    app = Starlette(routes=[Route("/api/tasks", endpoint=api_tasks_create, methods=["POST"])])
    app.state.drive_root = data
    app.state.repo_dir = repo
    resp = TestClient(app).post(
        "/api/tasks", json={"description": "edit the files", "workspace_root": str(ws)}
    )
    assert resp.status_code == 200, resp.text
    assert enqueued and enqueued[0]["workspace_root"] == str(ws.resolve())


# --- the project-room promote path carries the same decision ----------------------

def test_promote_into_an_untracked_room_offers_git_and_does_not_queue(tmp_path, monkeypatch):
    """The agent-side sibling of the gateway case. The task is halted with the SAME
    decision object, and the owner-facing message says GIT_INIT_REQUIRED rather than
    WORKSPACE_UNUSABLE — the folder is not broken, the answer is simply missing."""
    import supervisor.workers as workers
    from ouroboros.projects_registry import create_project, get_project, update_project

    # The drive root is a SIBLING of the owner's folder: a workspace overlapping the
    # Ouroboros data drive is refused by a guard that sits ahead of the git offer.
    drive = tmp_path / "data"
    drive.mkdir()
    monkeypatch.setattr(workers, "DRIVE_ROOT", drive)
    plain = tmp_path / "owner_folder"
    plain.mkdir()
    create_project(drive, "plainroom", name="Plain Room")
    update_project(drive, "plainroom", working_dir=str(plain), provenance="attached")

    enqueued: list = []
    sent: list = []
    ctx = types.SimpleNamespace(
        enqueue_task=lambda task: enqueued.append(task),
        persist_queue_snapshot=lambda **_kwargs: True,
        load_state=lambda: {"owner_chat_id": 1},
        send_with_budget=lambda chat_id, text: sent.append((chat_id, text)),
    )
    outcome = workers.promote_chat_to_task({
        "type": "promote_chat_to_task",
        "task_id": "untracked1",
        "objective": "Refactor the site",
        "project_id": "plainroom",
        "chat_id": 1,
    }, ctx)

    assert outcome["status"] == "needs_manual_target"
    assert outcome["reason"] == "git_init_required"
    assert outcome["decision"]["workspace_root"] == str(plain.resolve())
    assert outcome["decision"]["project_id"] == "plainroom"
    assert enqueued == []
    assert sent and "GIT_INIT_REQUIRED" in sent[0][1]
    assert not (plain / ".git").exists()
    # The attached folder is PRESERVED, never replaced by a fresh auto-provisioned
    # repo: auto-provisioning fires only for a project with no folder at all.
    assert get_project(drive, "plainroom")["working_dir"] == str(plain)
    result = json.loads((drive / "task_results" / "untracked1.json").read_text(encoding="utf-8"))
    assert result["reason_code"] == "git_init_required"


def test_promote_workspace_none_opts_out_of_the_git_offer_too(tmp_path, monkeypatch):
    """A folder-less task in an untracked room is legitimate work (chat, research),
    so the explicit opt-out must sit AHEAD of the offer, not behind it."""
    import supervisor.workers as workers
    from ouroboros.projects_registry import create_project, update_project

    drive = tmp_path / "data"
    drive.mkdir()
    monkeypatch.setattr(workers, "DRIVE_ROOT", drive)
    plain = tmp_path / "owner_folder2"
    plain.mkdir()
    create_project(drive, "plainroom2", name="Plain Room 2")
    update_project(drive, "plainroom2", working_dir=str(plain), provenance="attached")

    enqueued: list = []
    outcome = workers.promote_chat_to_task({
        "type": "promote_chat_to_task",
        "task_id": "untracked2",
        "objective": "Think about the site",
        "project_id": "plainroom2",
        "workspace": "none",
        "chat_id": 1,
    }, types.SimpleNamespace(
        enqueue_task=lambda task: enqueued.append(task),
        persist_queue_snapshot=lambda **_kwargs: True,
        load_state=lambda: {"owner_chat_id": 1},
    ))

    assert outcome["status"] == "scheduled"
    assert enqueued and not enqueued[0].get("workspace_root")
