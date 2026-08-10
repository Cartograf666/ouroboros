"""F6 (2026-08-10 amendments): the promote/router turn sees the LIVE toolset.

The router turn authors objectives/contracts for a task it will never run. The
first F6 cut projected a static union that advertised credential-gated built-ins
real availability removes and omitted every registered non-workspace built-in —
so the router could still author impossible or over-restricted contracts. These
tests pin the projection to the registry's REAL ``available_tools()`` resolution
for both target shapes (workspace-mode task vs non-workspace).
"""

import json

from types import SimpleNamespace

import pytest


def _env(tmp_path):
    return SimpleNamespace(repo_dir=str(tmp_path / "repo"), drive_root=tmp_path)


def _toolset(tmp_path):
    from ouroboros.context import build_runtime_section

    task = {"id": "t1", "_ephemeral_turn": True, "metadata": {"force_plan": True}}
    section = build_runtime_section(_env(tmp_path), task)
    payload = json.loads(section.split("\n\n", 1)[1])
    assert "promoted_task_toolset" in payload, "the swarm-router turn must carry F6"
    return payload["promoted_task_toolset"]


@pytest.fixture()
def _github_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")


def test_credential_gated_tool_is_not_advertised_when_unavailable(tmp_path, monkeypatch):
    # web_search is credential-gated behind live backends; with none available
    # the router must not be able to demand it — it moves to the TYPED omission
    # list instead of silently disappearing.
    import ouroboros.tools.search as search

    monkeypatch.setattr(search, "_available_web_search_backends", lambda: [])
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    toolset = _toolset(tmp_path)
    assert "web_search" not in toolset["workspace_task_tools"]
    assert "web_search" not in toolset["non_workspace_extra_tools"]
    assert "missing_credential" in toolset["unavailable_builtin_tools"]["web_search"]
    # GitHub built-ins without a token: same typed omission, not "does not exist".
    assert "get_github_issue" not in toolset["non_workspace_extra_tools"]
    assert "missing_credential" in toolset["unavailable_builtin_tools"]["get_github_issue"]


def test_live_toolset_classifies_core_workspace_and_non_workspace_tools(tmp_path, monkeypatch, _github_token):
    import ouroboros.tools.search as search

    monkeypatch.setattr(search, "_available_web_search_backends", lambda: ["ddgs"])
    toolset = _toolset(tmp_path)
    workspace = set(toolset["workspace_task_tools"])
    extra = set(toolset["non_workspace_extra_tools"])
    # A representative core/workspace tool rides the workspace list.
    assert "read_file" in workspace
    assert "delegate_start" in workspace
    # A registered non-workspace built-in (invisible in the old static union)
    # is now advertised where a non-workspace task would really see it.
    assert "get_github_issue" in extra
    assert "get_github_issue" not in workspace
    # Workspace-only visibility: the two lists never overlap.
    assert not (workspace & extra)
    # With its credential present the gated tool is advertised normally.
    assert "web_search" in workspace
    assert "web_search" not in toolset.get("unavailable_builtin_tools", {})


def test_non_router_turns_do_not_pay_for_the_projection(tmp_path):
    from ouroboros.context import build_runtime_section

    section = build_runtime_section(_env(tmp_path), {"id": "t1", "type": "task"})
    assert "promoted_task_toolset" not in section
