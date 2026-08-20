"""v6.101.0 — the per-project model: registry field, endpoint, and runtime route.

The route is deliberately narrow: a Project's model governs the project's OWN
turns. Subagents keep the global Main/Heavy/Light lane economics, and an
explicitly routed subagent still wins over the project's preference.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest


def _request(drive_root, project_id: str = "", body: dict | None = None):
    async def _json():
        return {} if body is None else body

    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(drive_root=drive_root)),
        path_params={"project_id": project_id},
        json=_json,
    )


@pytest.fixture
def drive(tmp_path, monkeypatch):
    import ouroboros.gateway.projects as gateway

    monkeypatch.setattr(gateway, "_broadcast_projects_changed", lambda *a, **k: None)
    root = tmp_path / "data"
    root.mkdir()
    return root


# --- registry -----------------------------------------------------------------

def test_new_project_inherits_main_by_default(drive):
    from ouroboros.projects_registry import create_project, project_model

    entry = create_project(drive, "p1", name="P1", origin="test")
    assert entry["model"] == ""
    assert project_model(drive, "p1") == ""


def test_model_round_trips_and_clears(drive):
    from ouroboros.projects_registry import create_project, project_model, update_project

    create_project(drive, "p1", name="P1", origin="test")
    update_project(drive, "p1", model="anthropic/claude-opus-5")
    assert project_model(drive, "p1") == "anthropic/claude-opus-5"
    # Empty is a legal write, not a no-op: it clears back to the Main slot.
    update_project(drive, "p1", model="")
    assert project_model(drive, "p1") == ""


def test_model_validation_refuses_junk(drive):
    from ouroboros.projects_registry import PROJECT_MODEL_MAX, create_project, update_project

    create_project(drive, "p1", name="P1", origin="test")
    with pytest.raises(ValueError):
        update_project(drive, "p1", model="x" * (PROJECT_MODEL_MAX + 1))
    with pytest.raises(ValueError):
        update_project(drive, "p1", model="anthropic/claude opus")


def test_legacy_row_without_model_reads_as_inherit(drive):
    from ouroboros.projects_registry import get_project, project_model

    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "projects.json").write_text(
        json.dumps({"projects": [{"id": "old", "name": "Old", "chat_id": 7}]}),
        encoding="utf-8",
    )
    assert (get_project(drive, "old") or {}).get("model") == ""
    assert project_model(drive, "old") == ""


def test_project_model_is_failure_tolerant(drive):
    from ouroboros.projects_registry import project_model

    assert project_model(drive, "") == ""
    assert project_model(drive, "never-registered") == ""


def test_summary_surfaces_the_model_for_the_sidebar(drive):
    from ouroboros.projects_registry import create_project, projects_summary, update_project

    create_project(drive, "p1", name="P1", origin="owner_ui")
    update_project(drive, "p1", model="x-ai/grok-4.5")
    row = next(r for r in projects_summary(drive) if r["id"] == "p1")
    assert row["model"] == "x-ai/grok-4.5"


# --- endpoint -----------------------------------------------------------------

def test_endpoint_sets_and_clears_the_model(drive):
    from ouroboros.gateway.projects import api_project_update
    from ouroboros.projects_registry import create_project, project_model

    create_project(drive, "p1", name="P1", origin="owner_ui")

    resp = asyncio.run(api_project_update(_request(drive, "p1", {"model": "anthropic/claude-opus-5"})))
    assert resp.status_code == 200
    assert project_model(drive, "p1") == "anthropic/claude-opus-5"
    # The name is untouched by a model-only call.
    assert json.loads(resp.body)["project"]["name"] == "P1"

    resp = asyncio.run(api_project_update(_request(drive, "p1", {"model": ""})))
    assert resp.status_code == 200
    assert project_model(drive, "p1") == ""


def test_endpoint_still_renames_and_still_requires_a_name(drive):
    from ouroboros.gateway.projects import api_project_update
    from ouroboros.projects_registry import create_project, get_project

    create_project(drive, "p1", name="P1", origin="owner_ui")
    assert asyncio.run(api_project_update(_request(drive, "p1", {"name": "P2"}))).status_code == 200
    assert (get_project(drive, "p1") or {})["name"] == "P2"
    # A body with neither key keeps the pre-v6.101.0 error rather than writing nothing.
    resp = asyncio.run(api_project_update(_request(drive, "p1", {})))
    assert resp.status_code == 400
    assert "name is required" in json.loads(resp.body)["error"]


def test_endpoint_rejects_a_junk_model(drive):
    from ouroboros.gateway.projects import api_project_update
    from ouroboros.projects_registry import PROJECT_MODEL_MAX, create_project, project_model

    create_project(drive, "p1", name="P1", origin="owner_ui")
    resp = asyncio.run(api_project_update(_request(drive, "p1", {"model": "a b"})))
    assert resp.status_code == 400
    resp = asyncio.run(api_project_update(_request(drive, "p1", {"model": "x" * (PROJECT_MODEL_MAX + 1)})))
    assert resp.status_code == 400
    assert project_model(drive, "p1") == ""


# --- runtime route ------------------------------------------------------------

def test_subagents_never_inherit_the_project_model(drive):
    """resolve_project_id() is the gate agent.py reads: a subagent resolves to no
    project, so the project's model can never silently re-route a delegated child."""
    from ouroboros.project_facts import resolve_project_id

    assert resolve_project_id({"project_id": "p1", "delegation_role": "subagent"}) == "p1"
    # ...but a child that only INHERITS a workspace resolves to no project scope.
    assert resolve_project_id({"workspace_root": str(drive), "delegation_role": "subagent"}) == ""


def test_route_helper_leaves_the_context_alone_when_it_cannot_decide(drive):
    """A missing project, an unset model, or a broken registry must never invent a
    route — the task falls back to the global Main slot, the pre-v6.101.0 behavior."""
    from ouroboros.project_facts import apply_project_model_route
    from ouroboros.projects_registry import create_project

    create_project(drive, "p1", name="P1", origin="owner_ui")
    ctx = SimpleNamespace(task_model_override=None)

    assert apply_project_model_route(ctx, drive, "") == ""
    assert apply_project_model_route(ctx, drive, "never-registered") == ""
    assert apply_project_model_route(ctx, drive, "p1") == ""
    assert ctx.task_model_override is None

    (drive / "state" / "projects.json").write_text("{ not json", encoding="utf-8")
    assert apply_project_model_route(ctx, drive, "p1") == ""
    assert ctx.task_model_override is None


def test_route_helper_keeps_the_task_and_context_in_lockstep(drive):
    """context_fit resolves the exact route (and the context window) from the
    TASK's model, so writing only the context would probe the wrong model."""
    from ouroboros.project_facts import apply_project_model_route
    from ouroboros.projects_registry import create_project, update_project

    create_project(drive, "p1", name="P1", origin="owner_ui")
    update_project(drive, "p1", model="anthropic/claude-opus-5")

    ctx = SimpleNamespace(task_model_override=None)
    task = {"id": "t1", "project_id": "p1"}
    assert apply_project_model_route(ctx, drive, "p1", task) == "anthropic/claude-opus-5"
    assert ctx.task_model_override == "anthropic/claude-opus-5"
    assert task["model"] == "anthropic/claude-opus-5"

    # A delegated child's own resolved model is never overwritten by the project.
    child_ctx = SimpleNamespace(task_model_override=None)
    child = {"id": "t2", "project_id": "p1", "delegation_role": "subagent", "model": "google/gemini-3.6-flash"}
    apply_project_model_route(child_ctx, drive, "p1", child)
    assert child["model"] == "google/gemini-3.6-flash"


def _run_task_capturing_route(tmp_path, monkeypatch, drive, task: dict) -> str:
    """Drive the REAL agent path with the tool loop stubbed, and report the model
    the loop would have run (``ctx.task_model_override``; empty = inherit Main)."""
    from ouroboros import agent as agent_module
    from ouroboros.agent import Env, OuroborosAgent

    monkeypatch.setattr(OuroborosAgent, "_log_worker_boot_once", lambda self: None)
    monkeypatch.setattr("ouroboros.agent.build_llm_messages", lambda **kwargs: ([], {}))

    seen: list[str] = []

    def _capture(**kwargs):
        ctx = kwargs["tools"]._ctx
        seen.append(str(getattr(ctx, "task_model_override", "") or "").strip())
        return "done", {}, {"reasoning_notes": [], "tool_calls": []}

    monkeypatch.setattr(agent_module, "run_llm_loop", _capture)

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    agent = OuroborosAgent(Env(repo_dir=repo, drive_root=drive))
    agent._handle_task_scoped(dict(task))
    assert seen, "the stubbed loop was never reached"
    return seen[-1]


def test_project_model_reaches_the_loop(tmp_path, monkeypatch, drive):
    from ouroboros.projects_registry import create_project, update_project

    create_project(drive, "p1", name="P1", origin="owner_ui")
    update_project(drive, "p1", model="anthropic/claude-opus-5")

    route = _run_task_capturing_route(tmp_path, monkeypatch, drive, {
        "id": "t1",
        "type": "task",
        "chat_id": 1,
        "objective": "o",
        "drive_root": str(drive),
        "project_id": "p1",
    })
    assert route == "anthropic/claude-opus-5"


def test_unset_project_model_leaves_the_route_on_main(tmp_path, monkeypatch, drive):
    from ouroboros.projects_registry import create_project

    create_project(drive, "p1", name="P1", origin="owner_ui")

    route = _run_task_capturing_route(tmp_path, monkeypatch, drive, {
        "id": "t2",
        "type": "task",
        "chat_id": 1,
        "objective": "o",
        "drive_root": str(drive),
        "project_id": "p1",
    })
    assert route == "", "an unset project model must not pin a route"


def test_a_delegated_child_is_routed_by_its_lane_not_the_project(tmp_path, monkeypatch, drive):
    """A subagent's model is the delegation lane's decision. Even for a child that
    carries the project scope EXPLICITLY (the one case resolve_project_id keeps),
    the project's preference must not become the child's route — the delegate
    resolution downstream of it is the authority, and it wins."""
    from ouroboros.projects_registry import create_project, update_project

    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main")
    create_project(drive, "p1", name="P1", origin="owner_ui")
    update_project(drive, "p1", model="anthropic/claude-opus-5")

    route = _run_task_capturing_route(tmp_path, monkeypatch, drive, {
        "id": "t3",
        "type": "task",
        "chat_id": 1,
        "objective": "o",
        "drive_root": str(drive),
        "project_id": "p1",
        "delegation_role": "subagent",
        "model": "google/gemini-3.6-flash",
    })
    assert route != "anthropic/claude-opus-5", "the project model leaked into a subagent"
    assert route == "provider::main", "the child must ride the lane-resolved model"
