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


# --- the owner's YES ---------------------------------------------------------------

def test_init_git_route_answers_the_offer_and_then_the_task_admits(tmp_path):
    """The whole loop: attach a plain folder, get the offer instead of a queued
    task, say yes through the route, and the same folder now admits file work."""
    import asyncio

    from ouroboros.gateway.projects import api_project_init_git, api_projects_create
    from ouroboros.projects_registry import get_project
    from ouroboros.workspace_admission import GitInitRequiredError, validate_workspace_root

    data = tmp_path / "data"
    data.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    plain = tmp_path / "owner_site"
    plain.mkdir()
    (plain / "index.html") .write_text("<h1>hi</h1>\n", encoding="utf-8")

    created = asyncio.run(api_projects_create(
        _ProjectsReq({"name": "Site", "path": str(plain)}, drive_root=data, repo_dir=repo)
    ))
    assert created.status_code == 200
    pid = json.loads(created.body)["project"]["id"]

    # Before the answer: admission refuses with the offer, folder untouched.
    try:
        validate_workspace_root(str(plain), system_repo_dir=repo, drive_root=data)
        raise AssertionError("an untracked folder must not admit a file task")
    except GitInitRequiredError:
        pass
    assert not (plain / ".git").exists()

    resp = asyncio.run(api_project_init_git(
        _ProjectsReq({}, drive_root=data, repo_dir=repo, path_params={"project_id": pid})
    ))
    payload = json.loads(resp.body)
    assert resp.status_code == 200, payload
    assert payload["working_dir"] == str(plain.resolve())
    assert (plain / ".git").exists()
    # The owner's existing files are in the snapshot, not silently ignored.
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(plain), capture_output=True, text=True, check=True
    ).stdout.split()
    assert "index.html" in tracked
    # The folder binding is unchanged — saying yes tracks the place, it never moves it.
    assert get_project(data, pid)["working_dir"] == str(plain.resolve())
    # And the same folder now admits a file task.
    assert validate_workspace_root(
        str(plain), system_repo_dir=repo, drive_root=data
    ) == plain.resolve()


def test_init_git_route_refuses_what_it_cannot_safely_touch(tmp_path):
    """This route's whole job is to write into a folder, so it re-establishes the
    attach guards against the CURRENT working_dir instead of trusting a registry
    value that could have been edited or gone stale."""
    import asyncio

    from ouroboros.gateway.projects import api_project_init_git
    from ouroboros.projects_registry import create_project, update_project

    data = tmp_path / "data"
    data.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()

    def _call(pid):
        return asyncio.run(api_project_init_git(
            _ProjectsReq({}, drive_root=data, repo_dir=repo, path_params={"project_id": pid})
        ))

    assert _call("ghost").status_code == 404

    create_project(data, "fileless", name="Fileless")
    resp = _call("fileless")
    assert resp.status_code == 400
    assert json.loads(resp.body)["error_code"] == "no_working_dir"

    create_project(data, "gone", name="Gone")
    update_project(data, "gone", working_dir=str(tmp_path / "no-such-folder"))
    resp_gone = _call("gone")
    assert resp_gone.status_code == 400
    assert "does not exist" in json.loads(resp_gone.body)["error"]

    # A working_dir that overlaps the Ouroboros system repo is refused even though
    # the registry claims it: the guard is re-run, not remembered.
    create_project(data, "inrepo", name="InRepo")
    update_project(data, "inrepo", working_dir=str(repo))
    resp_repo = _call("inrepo")
    assert resp_repo.status_code == 400
    assert "Ouroboros system repo" in json.loads(resp_repo.body)["error"]
    assert not (repo / ".git").exists()


def test_init_git_route_keeps_credential_shaped_files_out_of_the_snapshot(tmp_path):
    """Same disclosed omission the create-dialog init_git makes: secrets are never
    baked into git history, and the owner is TOLD which files were left out."""
    import asyncio

    from ouroboros.gateway.projects import api_project_init_git
    from ouroboros.projects_registry import create_project, update_project

    data = tmp_path / "data"
    data.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    plain = tmp_path / "with_secrets"
    plain.mkdir()
    (plain / "app.py").write_text("print('x')\n", encoding="utf-8")
    (plain / ".env").write_text("API_KEY=supersecret\n", encoding="utf-8")

    create_project(data, "secrets", name="Secrets")
    update_project(data, "secrets", working_dir=str(plain), provenance="attached")
    resp = asyncio.run(api_project_init_git(
        _ProjectsReq({}, drive_root=data, repo_dir=repo, path_params={"project_id": "secrets"})
    ))
    payload = json.loads(resp.body)
    assert resp.status_code == 200, payload
    assert ".env" in payload["init_git_skipped"]
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(plain), capture_output=True, text=True, check=True
    ).stdout.split()
    assert "app.py" in tracked and ".env" not in tracked
