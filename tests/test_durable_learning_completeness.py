"""Phase 5A: destructive learning sees complete source state or abstains."""

from __future__ import annotations

import json
import pathlib


def test_pattern_register_rewrite_receives_complete_tail(tmp_path, monkeypatch):
    from ouroboros import reflection

    knowledge = tmp_path / "memory" / "knowledge"
    knowledge.mkdir(parents=True)
    tail = "DECISIVE_PATTERN_TAIL_MUST_SURVIVE"
    current = reflection._PATTERNS_HEADER + ("| old | 1 | cause | fix | open |\n" * 600) + tail
    path = knowledge / "patterns.md"
    path.write_text(current, encoding="utf-8")
    captured = {}
    monkeypatch.setattr("ouroboros.config.get_light_model", lambda: "light")
    monkeypatch.setattr("ouroboros.llm.LLMClient", lambda: object())

    def fake_chat(*args, **kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return ({"content": current}, {})

    monkeypatch.setattr("ouroboros.llm_observability.chat_observed", fake_chat)
    reflection._update_patterns(tmp_path, {
        "task_id": "task-pattern", "goal": "keep complete patterns",
        "key_markers": ["TOOL_ERROR"], "reflection": "A new occurrence.",
    })
    assert tail in captured["prompt"]
    assert tail in path.read_text(encoding="utf-8")


def test_backlog_fingerprint_uses_unsanitized_canonical_fields(tmp_path, monkeypatch):
    from ouroboros.improvement_backlog import append_backlog_items, load_backlog_items

    monkeypatch.setattr("ouroboros.semantic_dedup.find_semantic_duplicate_id", lambda *a, **k: None)
    prefix = "x" * 300
    assert append_backlog_items(tmp_path, [{
        "summary": prefix + "A" * 40, "category": "process", "source": "reflection",
    }]) == 1
    assert append_backlog_items(tmp_path, [{
        "summary": prefix + "B" * 40, "category": "process", "source": "reflection",
    }]) == 1
    items = load_backlog_items(tmp_path)
    assert len(items) == 2
    assert len({item["fingerprint"] for item in items}) == 2


def test_groom_receives_complete_records_and_preserves_on_unavailable(tmp_path, monkeypatch):
    from ouroboros import improvement_backlog as ib

    monkeypatch.setattr("ouroboros.semantic_dedup.find_semantic_duplicate_id", lambda *a, **k: None)
    for idx in range(35):
        ib.append_backlog_items(tmp_path, [{
            "id": f"ibl-{idx}", "fingerprint": f"fp-{idx}", "summary": f"item {idx}",
            "category": "process", "source": "reflection",
            "evidence": f"complete-evidence-{idx}", "context": f"complete-context-{idx}",
            "proposed_next_step": f"complete-next-step-{idx}",
        }])
    items = ib.load_backlog_items(tmp_path)
    keep = [{"id": i["id"], "fingerprint": i["fingerprint"], "summary": i["summary"]}
            for i in items[:20]]
    captured = {}
    monkeypatch.setattr("ouroboros.config.get_light_model", lambda: "light")
    monkeypatch.setattr("ouroboros.llm.LLMClient", lambda: object())

    def fake_chat(*args, **kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return ({"content": json.dumps(keep)}, {})

    monkeypatch.setattr("ouroboros.llm_observability.chat_observed", fake_chat)
    assert ib.groom_backlog(tmp_path, cap=30) == 20
    for expected in ("complete-evidence-34", "complete-context-34", "complete-next-step-34"):
        assert expected in captured["prompt"]

    before = ib.backlog_path(tmp_path).read_text(encoding="utf-8")
    real_locked = ib._locked_text_file

    def unavailable(path, mode, *, shared=False):
        if mode == "r":
            raise PermissionError("backlog unavailable")
        return real_locked(path, mode, shared=shared)

    monkeypatch.setattr(ib, "_locked_text_file", unavailable)
    assert ib.groom_backlog(tmp_path, cap=10) == 0
    assert ib.backlog_path(tmp_path).read_text(encoding="utf-8") == before


def test_closed_objective_before_old_horizon_reaches_chooser(tmp_path, monkeypatch):
    from ouroboros import post_task_evolution as pte

    state = tmp_path / "state"
    state.mkdir(parents=True)
    old_objective = "OLD CLOSED OBJECTIVE MUST NOT BE PROMOTED AGAIN"
    rows = [{"task_id": "old", "kind": "cycle_outcome", "cycle_outcome": "absorbed",
             "campaign_objective": old_objective}]
    rows.extend({"task_id": f"new-{i}", "kind": "cycle_outcome", "cycle_outcome": "absorbed",
                 "campaign_objective": f"new objective {i}"} for i in range(230))
    (state / "evolution_checkpoints.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8",
    )
    captured = {}

    def fake_chat(*args, **kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return ({"content": '{"promote": false, "objective": ""}'}, {})

    monkeypatch.setattr("ouroboros.llm_observability.chat_observed", fake_chat)
    monkeypatch.setattr(pte, "_active_campaign_objective", lambda: "")
    env = type("Env", (), {"drive_root": tmp_path})()
    decision = pte._decide_promotion(env, {"id": "root"}, {"reflection": "done"}, object(), force=False)
    assert decision and decision["promote"] is False
    assert old_objective in captured["prompt"]


def test_closed_objective_unavailable_abstains_before_chooser(tmp_path, monkeypatch):
    from ouroboros import post_task_evolution as pte

    state = tmp_path / "state"
    state.mkdir(parents=True)
    ledger = state / "evolution_checkpoints.jsonl"
    ledger.write_text("{}\n", encoding="utf-8")
    real_read_text = pathlib.Path.read_text

    def unreadable(self, *args, **kwargs):
        if self == ledger:
            raise PermissionError("ledger unavailable")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", unreadable)
    called = []

    def chooser(*args, **kwargs):
        called.append(True)
        return ({"content": '{"promote": true, "objective": "unsafe"}'}, {})

    monkeypatch.setattr("ouroboros.llm_observability.chat_observed", chooser)
    env = type("Env", (), {"drive_root": tmp_path})()
    assert pte._decide_promotion(env, {"id": "root"}, {"reflection": "done"}, object(), force=False) is None
    assert called == []


def _bg_fixture(tmp_path):
    from ouroboros.consciousness import BackgroundConsciousness
    from ouroboros.improvement_backlog import append_backlog_items

    repo_dir = pathlib.Path(__file__).parents[1]
    (tmp_path / "logs").mkdir(parents=True)
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "state.json").write_text("{}", encoding="utf-8")
    for idx in range(10):
        append_backlog_items(tmp_path, [{
            "id": f"ibl-bg-{idx}", "fingerprint": f"fp-bg-{idx}",
            "summary": f"background item {idx}", "category": "identity", "source": "reflection",
        }])
    return BackgroundConsciousness(tmp_path, repo_dir, None, lambda: None)


def _tool_call(name, args, call_id):
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}


def test_bgc_direct_identity_update_requires_complete_named_omission(tmp_path):
    bc = _bg_fixture(tmp_path)
    try:
        context = bc._build_context()
        assert "knowledge_read" in context and "improvement-backlog" in context
        content = "I remain directly self-authoring after complete source materialization."
        blocked = bc._execute_tool(_tool_call("update_identity", {"content": content}, "u1"), [])
        assert "IDENTITY_UPDATE_ABSTAINED" in blocked
        read = bc._execute_tool(_tool_call("knowledge_read", {"topic": "improvement-backlog"}, "r1"), [])
        assert "background item 9" in read
        updated = bc._execute_tool(_tool_call("update_identity", {"content": content}, "u2"), [])
        assert updated.startswith("OK: identity updated")
        journal = tmp_path / "memory" / "identity_journal.jsonl"
        assert journal.exists() and content in journal.read_text(encoding="utf-8")
    finally:
        bc._tool_executor.shutdown(wait=False, cancel_futures=True)


def test_bgc_unavailable_named_omission_abstains_without_approval_flow(tmp_path):
    bc = _bg_fixture(tmp_path)
    try:
        bc._build_context()
        (tmp_path / "memory" / "knowledge" / "improvement-backlog.md").unlink()
        content = "I retain direct authority but abstain when the named source is unavailable."
        result = bc._execute_tool(_tool_call("update_identity", {"content": content}, "u1"), [])
        assert "IDENTITY_UPDATE_ABSTAINED" in result
        assert "approval" not in result.lower()
        assert not (tmp_path / "memory" / "identity_journal.jsonl").exists()
    finally:
        bc._tool_executor.shutdown(wait=False, cancel_futures=True)
