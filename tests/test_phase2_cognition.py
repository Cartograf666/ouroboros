from __future__ import annotations

import json
from types import SimpleNamespace


def _chat_rows(root):
    path = root / "logs" / "chat.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_split_project_root_summary_lands_canonically_once_before_child_gc(tmp_path):
    from ouroboros import agent_task_pipeline as pipeline

    canonical = tmp_path / "canonical"
    child = tmp_path / "child"
    (canonical / "logs").mkdir(parents=True)
    (child / "logs").mkdir(parents=True)
    env = SimpleNamespace(repo_dir=tmp_path / "repo", drive_root=child)
    task = {
        "id": "project-root",
        "root_task_id": "project-root",
        "project_id": "launch",
        "chat_id": 41,
        "type": "task",
        "text": "Ship the release",
        "budget_drive_root": str(canonical),
    }

    for _ in range(2):
        pipeline._run_task_summary(
            env, object(), task, {"rounds": 1, "cost": 0},
            {"tool_calls": []}, child / "logs",
        )

    rows = [row for row in _chat_rows(canonical) if row.get("type") == "task_summary"]
    assert len(rows) == 1
    assert rows[0]["task_id"] == "project-root"
    assert rows[0]["project_id"] == "launch"
    assert rows[0]["result_ref"] == {
        "kind": "task_result", "task_id": "project-root", "reader": "get_task_result",
    }

    # The execution drive is disposable; canonical biography is not.
    import shutil

    shutil.rmtree(child)
    assert len([row for row in _chat_rows(canonical) if row.get("task_id") == "project-root"]) == 1


def test_existing_authored_root_summary_prevents_a_second_llm_call(tmp_path):
    from ouroboros import agent_task_pipeline as pipeline

    class Llm:
        def __init__(self):
            self.calls = 0

        def chat(self, **_kwargs):
            self.calls += 1
            return {"content": "Authored once"}, {"cost": 0}

    env = SimpleNamespace(repo_dir=tmp_path / "repo", drive_root=tmp_path)
    task = {"id": "root-llm", "root_task_id": "root-llm", "chat_id": 1,
            "type": "task", "text": "Nontrivial task"}
    llm = Llm()
    for _ in range(2):
        pipeline._run_task_summary(
            env, llm, task, {"rounds": 2, "cost": 0},
            {"tool_calls": [{"tool": "read_file"}]}, tmp_path / "logs",
        )

    assert llm.calls == 1
    assert len([row for row in _chat_rows(tmp_path) if row.get("task_id") == "root-llm"]) == 1


def test_legacy_root_summary_without_identity_still_prevents_retry_duplicate(tmp_path):
    from ouroboros.project_dialogue import append_terminal_task_projection

    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    (logs / "chat.jsonl").write_text(
        json.dumps({"type": "task_summary", "task_id": "legacy-root", "text": "Legacy"}) + "\n",
        encoding="utf-8",
    )
    assert not append_terminal_task_projection(
        tmp_path, "legacy-root", {},
        {"task_id": "legacy-root", "root_task_id": "legacy-root", "status": "failed"},
        {"status": "failed", "chat_id": 1},
    )
    assert len(_chat_rows(tmp_path)) == 1


def test_terminal_child_projection_is_idempotent_and_honest_for_all_outcomes(tmp_path):
    from ouroboros.project_dialogue import append_terminal_task_projection

    cases = [
        ("ok", "completed", {"execution": {"status": "ok"}}, "Completed"),
        ("failed", "failed", {"execution": {"status": "failed"}}, "Failed"),
        ("cancelled", "cancelled", {"execution": {"status": "ok"}}, "Cancelled"),
        ("degraded", "completed", {"execution": {"status": "best_effort"}}, "Completed with limitations"),
    ]
    for suffix, status, axes, label in cases:
        task_id = f"child-{suffix}"
        task = {
            "id": task_id,
            "parent_task_id": "project-root",
            "root_task_id": "project-root",
            "project_id": "launch",
            "chat_id": 41,
            "delegation_role": "subagent",
            "role": "reviewer",
        }
        result = {
            **task, "task_id": task_id, "status": status,
            "outcome_axes": axes, "reason_code": f"reason-{suffix}",
            "result": f"result-{suffix}",
        }
        done = {"chat_id": 41, "status": status, "outcome_axes": axes}
        assert append_terminal_task_projection(tmp_path, task_id, task, result, done)
        assert not append_terminal_task_projection(tmp_path, task_id, task, result, done)

        row = next(row for row in _chat_rows(tmp_path) if row.get("task_id") == task_id)
        assert row["parent_task_id"] == "project-root"
        assert row["root_task_id"] == "project-root"
        assert row["project_id"] == "launch"
        assert row["role"] == "reviewer"
        assert row["status"] == status
        assert row["outcome"] == label
        assert row["reason_code"] == f"reason-{suffix}"
        assert row["result_ref"] == {
            "kind": "task_result", "task_id": task_id, "reader": "get_task_result",
        }
        assert f'get_task_result(task_id="{task_id}")' in row["text"]


def test_terminal_root_fallback_covers_cancel_without_preempting_open_synthesis(tmp_path):
    from ouroboros.project_dialogue import append_terminal_task_projection

    cancelled = {
        "task_id": "cancelled-root", "root_task_id": "cancelled-root",
        "project_id": "launch", "status": "cancelled", "result": "Stopped by owner",
    }
    assert append_terminal_task_projection(
        tmp_path, "cancelled-root", {}, cancelled,
        {"chat_id": 41, "status": "cancelled"},
    )
    row = next(row for row in _chat_rows(tmp_path) if row.get("task_id") == "cancelled-root")
    assert row["summary_kind"] == "terminal_root_projection"
    assert row["outcome"] == "Cancelled"

    pending = {
        "task_id": "normal-root", "root_task_id": "normal-root", "status": "completed",
        "root_phase_checkpoint": {"post_task_synthesis": "pending_once"},
    }
    assert not append_terminal_task_projection(
        tmp_path, "normal-root", {}, pending,
        {"chat_id": 1, "status": "completed"},
    )


def test_duplicate_task_done_after_child_copyback_appends_one_canonical_projection(tmp_path):
    from ouroboros.task_results import STATUS_COMPLETED, write_task_result
    from supervisor import events

    child_drive = tmp_path / "state" / "headless_tasks" / "child-copy" / "data"
    write_task_result(
        child_drive, "child-copy", STATUS_COMPLETED,
        result="Integrated the reviewed change",
        parent_task_id="parent-root", root_task_id="parent-root",
        project_id="launch", delegation_role="subagent", role="implementer",
        outcome_axes={"execution": {"status": "ok"}},
    )
    task = {
        "id": "child-copy", "drive_root": str(child_drive), "chat_id": 41,
        "parent_task_id": "parent-root", "root_task_id": "parent-root",
        "project_id": "launch", "delegation_role": "subagent", "role": "implementer",
        "task_constraint": {"mode": "local_readonly_subagent"},
    }
    worker = SimpleNamespace(busy_task_id="child-copy", reaping=False)
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING={"child-copy": {"task": task}}, WORKERS={7: worker},
        bridge=SimpleNamespace(push_log=lambda _row: None),
        send_with_budget=lambda *_args, **_kwargs: None,
        persist_queue_snapshot=lambda **_kwargs: None,
    )
    event = {"task_id": "child-copy", "worker_id": 7, "task_type": "task",
             "chat_id": 41, "status": "completed"}

    events._handle_task_done(event, ctx)
    events._handle_task_done(event, ctx)

    rows = [row for row in _chat_rows(tmp_path) if row.get("task_id") == "child-copy"]
    assert len(rows) == 1
    assert rows[0]["result_ref"]["reader"] == "get_task_result"
    assert not child_drive.joinpath("task_results", "child-copy.json").samefile(
        tmp_path / "task_results" / "child-copy.json"
    )


def test_child_projection_enters_main_cognition_and_project_lineage_not_main_ui(tmp_path):
    from ouroboros.context import build_recent_sections
    from ouroboros.gateway.history import make_chat_history_endpoint
    from ouroboros.memory import Memory
    from ouroboros.project_dialogue import append_terminal_task_projection
    from ouroboros.projects_registry import create_project

    project = create_project(tmp_path, "launch", name="Launch")
    project_chat = int(project["chat_id"])
    child = {
        "id": "child-review", "parent_task_id": "root", "root_task_id": "root",
        "project_id": "launch", "chat_id": project_chat,
        "delegation_role": "subagent", "role": "reviewer",
    }
    result = {**child, "task_id": "child-review", "status": "completed",
              "outcome_axes": {"execution": {"status": "ok"}}, "result": "Reviewed exact SHA"}
    assert append_terminal_task_projection(
        tmp_path, "child-review", child, result,
        {"chat_id": project_chat, "status": "completed"},
    )

    main_context = "\n\n".join(build_recent_sections(Memory(tmp_path), env=None))
    project_context = "\n\n".join(
        build_recent_sections(Memory(tmp_path), env=None, thread_chat_id=project_chat)
    )
    assert "Reviewed exact SHA" in main_context
    assert "parent=root" in main_context
    assert "Reviewed exact SHA" in project_context
    assert "parent=root" in project_context

    import asyncio

    endpoint = make_chat_history_endpoint(tmp_path)
    main_rows = json.loads(asyncio.run(endpoint(SimpleNamespace(
        query_params={"chat_id": "1"},
    ))).body)["messages"]
    project_rows = json.loads(asyncio.run(endpoint(SimpleNamespace(
        query_params={"chat_id": str(project_chat)},
    ))).body)["messages"]
    assert not any(row.get("task_id") == "child-review" for row in main_rows)
    assert any(row.get("task_id") == "child-review" for row in project_rows)

    unscoped = {
        "id": "child-main", "parent_task_id": "main-root", "root_task_id": "main-root",
        "chat_id": 1, "delegation_role": "subagent", "role": "researcher",
    }
    assert append_terminal_task_projection(
        tmp_path, "child-main", unscoped,
        {**unscoped, "task_id": "child-main", "status": "completed",
         "result": "Unscoped child truth", "outcome_axes": {"execution": {"status": "ok"}}},
        {"chat_id": 1, "status": "completed"},
    )
    main_context = "\n\n".join(build_recent_sections(Memory(tmp_path), env=None))
    assert "Unscoped child truth" in main_context
    main_rows = json.loads(asyncio.run(endpoint(SimpleNamespace(
        query_params={"chat_id": "1"},
    ))).body)["messages"]
    assert not any(row.get("task_id") == "child-main" for row in main_rows)


def test_project_build_reads_canonical_scratchpad_and_mutates_only_project_workpad(
    tmp_path, monkeypatch,
):
    import ouroboros.config as config

    from ouroboros.context import build_llm_messages
    from ouroboros.memory import Memory
    from ouroboros.tools.project_journal import _workpad_write

    canonical = tmp_path / "canonical"
    child = tmp_path / "child"
    repo = tmp_path / "repo"
    for path in (repo / "prompts", repo / "docs", canonical / "memory", canonical / "logs",
                 canonical / "state", child / "memory", child / "logs", child / "state"):
        path.mkdir(parents=True, exist_ok=True)
    (repo / "prompts" / "SYSTEM.md").write_text("System", encoding="utf-8")
    (repo / "BIBLE.md").write_text("Bible", encoding="utf-8")
    (repo / "docs" / "ARCHITECTURE.md").write_text("Architecture", encoding="utf-8")
    (repo / "docs" / "DEVELOPMENT.md").write_text("Development", encoding="utf-8")
    (repo / "docs" / "CHECKLISTS.md").write_text("Checklists", encoding="utf-8")
    (repo / "README.md").write_text("Readme", encoding="utf-8")
    (repo / "VERSION").write_text("1.0.0", encoding="utf-8")
    (repo / "pyproject.toml").write_text('version = "1.0.0"', encoding="utf-8")
    (canonical / "state" / "state.json").write_text('{"spent_usd": 0}', encoding="utf-8")
    (canonical / "memory" / "scratchpad.md").write_text(
        "CANONICAL BIOGRAPHY NOTE", encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE", "low")
    monkeypatch.setattr(config, "DATA_DIR", child)

    class Env:
        drive_root = child
        repo_dir = repo
        budget_drive_root = canonical

        def drive_path(self, rel):
            return child / rel

        def repo_path(self, rel):
            return repo / rel

    messages, _ = build_llm_messages(
        Env(), Memory(child, repo_dir=repo),
        {"id": "project-root", "text": "continue", "project_id": "launch",
         "budget_drive_root": str(canonical)},
    )
    assert "CANONICAL BIOGRAPHY NOTE" in json.dumps(messages, ensure_ascii=False)

    ctx = SimpleNamespace(project_id="launch", drive_root=child)
    assert _workpad_write(ctx, "LOCAL PROJECT WORK", "launch").startswith("OK:")
    assert (canonical / "memory" / "scratchpad.md").read_text(encoding="utf-8") == "CANONICAL BIOGRAPHY NOTE"
    from ouroboros.project_facts import project_workpad_path

    assert project_workpad_path("launch").read_text(encoding="utf-8") == "LOCAL PROJECT WORK"
