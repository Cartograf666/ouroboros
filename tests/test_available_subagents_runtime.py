"""Focused Phase-1B runtime tests for configured recursive actors."""

from __future__ import annotations

import json
import queue
from types import SimpleNamespace

import pytest


def _settings(*rows):
    return {
        "OUROBOROS_SUBAGENTS": json.dumps({"enabled": True, "items": list(rows)}),
    }


def _api_row(row_id="api-builder", model="openai/gpt-5.6-sol", effort="high"):
    return {
        "subagent_id": row_id,
        "name": "API builder",
        "recommended_use": "Exact recursive API actor.",
        "route": {"kind": "api_model", "target_id": model},
        "effort": effort,
    }


def _session_row(row_id="session-builder", target="codex=gpt-5.6-sol", effort="high"):
    return {
        "subagent_id": row_id,
        "name": "Session builder",
        "recommended_use": "Subscription-backed implementation.",
        "route": {
            "kind": "agent_session",
            "target_id": target,
            "credential_profile_id": "profile-1",
        },
        "effort": effort,
    }


def _snapshot(settings, row_id):
    from ouroboros.subagent_runtime import select_subagent_snapshot

    return select_subagent_snapshot(settings, subagent_id=row_id)[0]


def test_public_schema_is_id_only_and_real_registry_allows_hidden_d23_conflict(
    monkeypatch, tmp_path,
):
    from ouroboros.tools import control
    from ouroboros.tools.registry import ToolContext, ToolRegistry

    settings = _settings(_api_row())
    monkeypatch.setattr(control, "load_settings", lambda: settings)
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry.set_context(ToolContext(repo_dir=tmp_path, drive_root=tmp_path))
    schema = registry.get_schema_by_name("schedule_subagent")["function"]["parameters"]
    assert "subagent_id" in schema["properties"]
    assert "model_lane" not in schema["properties"]
    assert "executor" not in schema["properties"]
    assert {"subagent_id", "objective", "expected_output"} <= set(schema["required"])

    # Reaches the handler through ToolRegistry.execute: public validation did not
    # advertise the legacy arg, but the bounded hidden seam carried it for D23.
    result = registry.execute("schedule_subagent", {
        "subagent_id": "api-builder",
        "objective": "Implement it",
        "expected_output": "Patch",
        "executor": "native",
    })
    assert "subagent_selector_conflict" in result
    assert "unsupported" not in result


def test_d23_maps_one_migrated_row_and_refuses_ambiguous_auto(monkeypatch):
    from ouroboros.subagent_runtime import SubagentSelectionError, select_subagent_snapshot

    settings = {
        "OUROBOROS_SUBAGENT_HARNESS": "codex=gpt-5.6-sol:high",
        "OUROBOROS_SUBAGENT_PROFILE": "profile-1",
    }
    snapshot, legacy = select_subagent_snapshot(
        settings,
        legacy_executor="harness",
        legacy_executor_supplied=True,
    )
    assert legacy is True
    assert snapshot["selected_subagent_id"] == "legacy-session"
    assert snapshot["route"] == {
        "kind": "agent_session",
        "target_id": "codex=gpt-5.6-sol",
        "credential_profile_id": "profile-1",
    }
    try:
        select_subagent_snapshot(
            settings,
            legacy_executor="auto",
            legacy_executor_supplied=True,
        )
    except SubagentSelectionError as exc:
        assert exc.code == "subagent_selection_required"
    else:  # pragma: no cover - the assertion explains the invariant
        raise AssertionError("legacy auto must never select by order")


def test_unsaved_roster_is_off_but_d23_can_map_exact_owner_legacy(monkeypatch):
    from ouroboros.subagent_runtime import (
        SubagentSelectionError,
        effective_runtime_subagent_settings,
        select_subagent_snapshot,
    )

    runtime_keys = (
        "OUROBOROS_SUBAGENT_HARNESS", "OUROBOROS_SUBAGENT_PROFILE",
        "OUROBOROS_MODEL_HEAVY", "OUROBOROS_MODEL_LIGHT", "OUROBOROS_MODEL",
        "USE_LOCAL_HEAVY", "USE_LOCAL_LIGHT", "USE_LOCAL_MAIN", "LOCAL_MODEL_SOURCE",
    )
    for key in runtime_keys:
        monkeypatch.delenv(key, raising=False)
    effective = effective_runtime_subagent_settings({
        # A shipped value still present on disk was cleared by provider
        # normalization at task start, represented by its absent env projection.
        "OUROBOROS_MODEL_HEAVY": "google/gemini-3.5-flash",
    })
    assert effective["OUROBOROS_MODEL_HEAVY"] == ""
    with pytest.raises(SubagentSelectionError) as refused:
        select_subagent_snapshot(effective, subagent_id="legacy-heavy")
    assert refused.value.code == "subagent_configuration_unsaved"

    legacy = {"OUROBOROS_MODEL_HEAVY": "owner/custom-heavy"}
    with pytest.raises(SubagentSelectionError) as hidden_from_new_surface:
        select_subagent_snapshot(legacy, subagent_id="legacy-heavy")
    assert hidden_from_new_surface.value.code == "subagent_configuration_unsaved"
    snapshot, used_d23 = select_subagent_snapshot(
        legacy, legacy_model_lane="heavy", legacy_model_lane_supplied=True,
    )
    assert used_d23 is True
    assert snapshot["selected_subagent_id"] == "legacy-heavy"
    assert snapshot["route"]["target_id"] == "owner/custom-heavy"


def test_snapshot_is_immutable_across_queue_persistence(monkeypatch, tmp_path):
    from supervisor import queue as task_queue

    original = _settings(_api_row(model="openai/model-a"))
    snapshot = _snapshot(original, "api-builder")
    task = {
        "id": "child1",
        "type": "task",
        "chat_id": 1,
        "text": "work",
        "delegation_role": "subagent",
        "configured_subagent": snapshot,
        "parent_cognitive_route": {
            "model": "openai/parent",
            "effort": "high",
            "use_local_model": False,
        },
        "_attempt": 1,
    }
    pending, running = [task], {}
    task_queue.init(tmp_path, 600, 1800)
    task_queue.init_queue_refs(pending, running, {"value": 0})
    assert task_queue.persist_queue_snapshot(reason="test") is True
    pending.clear()
    assert task_queue.restore_pending_from_snapshot() == 1
    restored = pending[0]
    assert restored["configured_subagent"] == snapshot
    assert restored["configured_subagent"]["route"]["target_id"] == "openai/model-a"
    assert restored["parent_cognitive_route"]["model"] == "openai/parent"


def test_api_actor_dispatch_is_exact_recursive_route_without_slot_substitution(monkeypatch):
    from ouroboros.subagents import resolve_subagent_dispatch
    import ouroboros.provider_models as provider_models

    monkeypatch.setattr(provider_models, "model_has_credentials", lambda _model: True)
    snapshot = _snapshot(_settings(_api_row(model="openai/exact-model", effort="xhigh")), "api-builder")
    dispatch = resolve_subagent_dispatch({
        "id": "child1",
        "type": "task",
        "delegation_role": "subagent",
        "configured_subagent": snapshot,
        "task_constraint": {},
    }, task_type="task")
    assert dispatch.executor == "native"
    assert dispatch.route == "openai/exact-model"
    assert dispatch.lane.model == "openai/exact-model"
    assert dispatch.effort == "xhigh"
    assert dispatch.availability["route_kind"] == "api_model"


def test_session_dispatch_pins_exact_route_and_inherits_parent_cognition(monkeypatch):
    import ouroboros.subagents as subagents
    import ouroboros.claudexor_daemon as daemon

    class Gateway:
        def close(self):
            pass

    monkeypatch.setattr(daemon, "ensure_owned_gateway", lambda: Gateway())
    seen = {}

    def health(_gateway, route_id, _shape, *, route_model="", pinned_profile=""):
        seen.update(route_id=route_id, model=route_model, profile=pinned_profile)
        return "", ""

    monkeypatch.setattr(subagents, "route_health", health)
    snapshot = _snapshot(_settings(_session_row()), "session-builder")
    dispatch = subagents.resolve_subagent_dispatch({
        "id": "child1",
        "type": "task",
        "delegation_role": "subagent",
        "configured_subagent": snapshot,
        "parent_cognitive_route": {
            "model": "anthropic/parent-exact",
            "effort": "max",
            "use_local_model": False,
        },
        "task_constraint": {},
    }, task_type="task")
    assert dispatch.executor == "harness"
    assert dispatch.lane.model == "anthropic/parent-exact"
    assert dispatch.effort == "max"
    assert seen == {"route_id": "codex", "model": "gpt-5.6-sol", "profile": "profile-1"}


def test_selected_session_visibility_failure_blocks_without_native_substitution(monkeypatch):
    import ouroboros.agent as agent_module
    import ouroboros.subagents as subagents

    route = subagents.DelegationRoute(route_id="codex", model="gpt-5.6-sol")
    dispatch = subagents.SubagentDispatch(
        lane=subagents.SubagentLaneResolution(
            requested_lane="auto", effective_lane="main", model="openai/parent",
            resolved_from="main", provenance="configured_subagent",
        ),
        effort="high", executor="harness", route="codex", profile="local_readonly_subagent",
        delta=subagents.CapabilityDelta(
            derived_effort="high", effective_effort="high",
            requested_executor="harness", effective_executor="harness",
        ),
        executor_resolution=subagents.SubagentExecutorResolution(
            "harness", "harness", route, "harness_ready",
        ),
    )
    monkeypatch.setattr(agent_module, "envelope_from_task", lambda *_a, **_k: {})
    monkeypatch.setattr(
        subagents, "preflight_native_fallback_dispatch",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("a selected row must never host-fallback to native")
        ),
    )
    task = {"id": "child1", "configured_subagent": {"schema": 1}}
    amended, changed = agent_module.preflight_delegate_visibility(
        SimpleNamespace(available_tools=lambda: []), task, dispatch,
    )
    assert changed is True
    assert amended.executor == "blocked"
    assert amended.executor_resolution.reason == "delegate_tools_invisible"


def test_atomic_bootstrap_starts_before_wait_and_zero_nanny_rounds(monkeypatch, tmp_path):
    import ouroboros.tools.delegate as delegate
    import ouroboros.delegate_supervision as supervision
    from ouroboros.subagent_bootstrap import bootstrap_session_leaf

    calls = []
    monkeypatch.setattr(delegate, "exact_start", lambda ctx, prompt, spec: (
        calls.append(("start", prompt, spec))
        or json.dumps({"status": "started", "run_id": "run-1", "custody_durable": True})
    ))
    monkeypatch.setattr(supervision, "supervised_wait", lambda ctx, run_id: (
        calls.append(("wait", run_id))
        or json.dumps({"status": "completed", "run_id": run_id})
    ))
    snapshot = _snapshot(_settings(_session_row()), "session-builder")
    task = {
        "id": "child1",
        "objective": "Build",
        "expected_output": "Patch",
        "configured_subagent": snapshot,
        "task_contract": {"objective": "Build", "expected_output": "Patch"},
    }
    ctx = SimpleNamespace(
        task_id="child1", drive_root=tmp_path, budget_drive_root=str(tmp_path),
        task_metadata={},
    )
    out = json.loads(bootstrap_session_leaf(ctx, task, SimpleNamespace(executor="harness")))
    assert [call[0] for call in calls] == ["start", "wait"]
    assert out["status"] == "configured_session_wake"


def test_real_task_context_bootstraps_before_context_and_any_llm(monkeypatch, tmp_path):
    from ouroboros import agent as agent_module
    import ouroboros.claudexor_daemon as daemon
    import ouroboros.subagent_bootstrap as bootstrap
    import ouroboros.subagents as subagents
    from ouroboros.agent import Env, OuroborosAgent

    repo, drive = tmp_path / "repo", tmp_path / "drive"
    repo.mkdir()
    drive.mkdir()
    order = []

    class Gateway:
        def close(self):
            pass

    monkeypatch.setattr(daemon, "ensure_owned_gateway", lambda: Gateway())
    monkeypatch.setattr(
        subagents, "route_health",
        lambda *_a, **_k: ("", ""),
    )
    monkeypatch.setattr(OuroborosAgent, "_log_worker_boot_once", lambda self: None)
    monkeypatch.setattr(
        bootstrap, "bootstrap_session_leaf",
        lambda *_a, **_k: order.append("atomic_start_and_wait") or json.dumps({
            "status": "configured_session_wake", "wake": {"status": "completed"},
        }),
    )

    def build_context(**_kwargs):
        order.append("context_build")
        return [], {}

    monkeypatch.setattr(agent_module, "build_llm_messages", build_context)
    snapshot = _snapshot(_settings(_session_row()), "session-builder")
    agent = OuroborosAgent(Env(repo_dir=repo, drive_root=drive))
    agent.tools.available_tools = lambda: ["delegate_start", "delegate_wait", "delegate_cancel"]
    agent.llm.chat = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("no nanny LLM call is legal before external custody")
    )
    _ctx, messages, _caps = agent._prepare_task_context({
        "id": "child1", "type": "task", "chat_id": 1, "text": "Build",
        "delegation_role": "subagent", "configured_subagent": snapshot,
        "parent_cognitive_route": {
            "model": "openai/parent", "effort": "high", "use_local_model": False,
        },
        "task_constraint": {},
        "task_contract": {"objective": "Build", "expected_output": "Patch"},
        "drive_root": str(drive), "budget_drive_root": str(drive),
    })
    assert order == ["atomic_start_and_wait", "context_build"]
    assert any("CONFIGURED SESSION STARTUP / WAKE RECEIPT" in item["content"] for item in messages)
    assert _ctx._nanny_delegate_baseline == {"round": 0, "cost": 0.0}


def test_started_uncustodied_wakes_without_entering_quiet_wait(monkeypatch, tmp_path):
    import ouroboros.tools.delegate as delegate
    import ouroboros.delegate_supervision as supervision
    from ouroboros.subagent_bootstrap import bootstrap_session_leaf

    monkeypatch.setattr(delegate, "exact_start", lambda *_a, **_k: json.dumps({
        "status": "started_uncustodied", "run_id": "run-1", "custody_durable": False,
    }))
    monkeypatch.setattr(supervision, "supervised_wait", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("uncustodied starts must not sleep")))
    snapshot = _snapshot(_settings(_session_row()), "session-builder")
    ctx = SimpleNamespace(
        task_id="child1", drive_root=tmp_path, budget_drive_root=str(tmp_path),
        task_metadata={},
    )
    out = json.loads(bootstrap_session_leaf(
        ctx,
        {"id": "child1", "configured_subagent": snapshot},
        SimpleNamespace(executor="harness"),
    ))
    assert out["startup"]["status"] == "started_uncustodied"


def test_atomic_start_attempt_does_not_trigger_legacy_no_delegate_nudges(tmp_path):
    from ouroboros.delegate_evidence import record_start_blocked
    from ouroboros.loop import _forced_delegation_note, _nanny_finalization_message
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_id="child1")
    ctx.budget_drive_root = str(tmp_path)
    ctx._nanny_route_dispatched = True
    record_start_blocked(ctx, "child1", "configured_route_unavailable")
    tools = SimpleNamespace(
        _ctx=ctx,
        available_tools=lambda: ["delegate_start", "delegate_wait", "delegate_cancel"],
    )
    assert _nanny_finalization_message(tools, tmp_path, "child1") == ""
    assert _forced_delegation_note(ctx, {"tool_calls": []}) == ""


def test_quiet_windows_renew_and_terminal_plus_mailbox_coalesce(tmp_path):
    from ouroboros.delegate_supervision import supervised_wait
    from ouroboros.owner_mailbox import write_task_message

    ctx = SimpleNamespace(
        task_id="child1",
        drive_root=tmp_path,
        budget_drive_root=str(tmp_path),
        task_metadata={"configured_subagent": {"config_fingerprint": "fp"}},
    )
    calls = []

    def wait_once(_ctx, run_id, _sec, since):
        calls.append((run_id, since))
        if len(calls) < 3:
            return json.dumps({"status": "no_progress", "run_id": run_id, "last_seq": len(calls)})
        write_task_message(
            tmp_path, "new direction", "child1", source_task_id="parent1",
            provenance="ancestor_task", msg_id="m1",
        )
        return json.dumps({"status": "completed", "run_id": run_id, "last_seq": 3})

    out = json.loads(supervised_wait(ctx, "run-1", wait_once=wait_once))
    assert len(calls) == 3
    assert out["status"] == "completed"
    assert out["wake_events"] == [{
        "type": "addressed_message",
        "msg_id": "m1",
        "kind": "task_message",
        "provenance": "ancestor_task",
        "source_task_id": "parent1",
    }]


def test_one_shot_checkpoint_is_reasoned_and_consumed(monkeypatch, tmp_path):
    import ouroboros.delegate_supervision as supervision

    now = [100.0]
    monkeypatch.setattr(supervision.time, "time", lambda: now[0])
    ctx = SimpleNamespace(
        task_id="child1", drive_root=tmp_path, budget_drive_root=str(tmp_path),
        task_metadata={"configured_subagent": {"config_fingerprint": "fp"}},
    )

    def wait_once(_ctx, run_id, _sec, _since):
        now[0] += 2
        return json.dumps({"status": "no_progress", "run_id": run_id, "last_seq": 0})

    out = json.loads(supervision.supervised_wait(
        ctx, "run-1", checkpoint_after_sec=1,
        checkpoint_reason="inspect a promised artifact", wait_once=wait_once,
    ))
    assert out == {
        "status": "inspection_checkpoint",
        "run_id": "run-1",
        "reason": "inspect a promised artifact",
        "last_seq": 0,
    }
    state = supervision.supervision_checkpoint(ctx)
    assert state["checkpoint"]["consumed"] is True
    assert state["checkpoint"]["consumed_by"] == "scheduled_checkpoint"


def test_task_message_provenance_never_spoofs_owner(tmp_path):
    from ouroboros.loop import _drain_incoming_messages
    from ouroboros.owner_mailbox import write_task_message

    assert write_task_message(
        tmp_path, "peer finding", "child1", source_task_id="parent1",
        provenance="peer_via_ancestor", relayed_from_task_id="peer2", msg_id="m1",
    )
    messages = []
    _drain_incoming_messages(messages, queue.Queue(), tmp_path, "child1", None, set())
    rendered = json.dumps(messages, ensure_ascii=False)
    assert "Message from task peer2, relayed by ancestor parent1" in rendered
    assert "Message from my human" not in rendered


def test_ancestor_can_relay_verified_peer_output_without_owner_spoof(tmp_path):
    from ouroboros.owner_mailbox import drain_owner_entries
    from ouroboros.task_results import STATUS_COMPLETED, STATUS_RUNNING, write_task_result
    from ouroboros.tools.core import _forward_to_worker
    from ouroboros.tools.registry import ToolContext

    child_drive = tmp_path / "target-drive"
    child_drive.mkdir()
    write_task_result(
        tmp_path, "target", STATUS_RUNNING, parent_task_id="parent",
        root_task_id="parent", child_drive_root=str(child_drive), result="running",
    )
    write_task_result(
        tmp_path, "peer", STATUS_COMPLETED, parent_task_id="parent",
        root_task_id="parent", result="peer evidence",
    )
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_id="parent")
    ctx.budget_drive_root = str(tmp_path)
    assert _forward_to_worker(
        ctx, "target", "peer evidence", relayed_from_task_id="peer",
    ) == "Message forwarded to task target"
    [entry] = drain_owner_entries(child_drive, "target", seen_ids=set())
    assert entry["kind"] == "task_message"
    assert entry["provenance"] == "peer_via_ancestor"
    assert entry["source_task_id"] == "parent"
    assert entry["relayed_from_task_id"] == "peer"


def test_replacement_is_refused_before_gateway_or_post(monkeypatch, tmp_path):
    from ouroboros import delegate_custody as custody
    from ouroboros.tools.delegate import _delegate_start
    from ouroboros.tools.registry import ToolContext
    import ouroboros.claudexor_daemon as daemon

    custody._CUSTODY.clear()
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "codex=gpt-5.6-sol:high")
    custody.record_started(tmp_path, custody.RunCustody(
        run_id="run-old", task_id="child1", route_id="codex",
    ))
    monkeypatch.setattr(daemon, "ensure_owned_gateway", lambda: (_ for _ in ()).throw(
        AssertionError("gateway must not open before settlement guard")))
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "child1"
    out = json.loads(_delegate_start(ctx, "replacement work"))
    assert out["status"] == "refused"
    assert out["reason"] == "replacement_requires_settlement"


def test_worker_crash_mismatch_vetoes_without_post_and_cause_matrix(monkeypatch, tmp_path):
    from ouroboros import delegate_custody as custody
    import ouroboros.delegate_recovery as recovery
    from ouroboros.subagent_work_order import work_order_fingerprint
    from ouroboros.tools.registry import ToolContext

    custody._CUSTODY.clear()
    snapshot = _snapshot(_settings(_session_row()), "session-builder")
    task = {
        "id": "child1",
        "_attempt": 2,
        "configured_subagent": snapshot,
        "task_constraint": {},
        "task_contract": {},
        "drive_root": str(tmp_path),
    }
    authority = recovery.authority_fingerprint_from_task(task)
    custody.record_started(tmp_path, custody.RunCustody(
        run_id="run-1", task_id="child1", route_id="codex",
        selected_subagent_id="session-builder",
        config_fingerprint=snapshot["config_fingerprint"],
        authority_fingerprint=authority,
        work_order_fingerprint=work_order_fingerprint(task),
    ))
    handoff = recovery.prepare_handoff(
        tmp_path, task, cause=recovery.CAUSE_WORKER_CRASH,
        old_attempt=1, new_attempt=2, worker_id=1, exitcode=1,
    )
    assert handoff["run_id"] == "run-1"
    assert "child1" in recovery.recoverable_task_ids(tmp_path)
    monkeypatch.setattr(custody, "reconcile_task_runs", lambda *_a, **_k: [])
    bad = {**task, "configured_subagent": {**snapshot, "config_fingerprint": "different"}}
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "child1"
    ctx.budget_drive_root = str(tmp_path)
    result = recovery.adopt_handoff(ctx, bad)
    assert result == {"status": "recovery_required", "reason": "successor_binding_mismatch"}
    assert recovery._read(tmp_path, "child1")["status"] == "vetoed"

    planned = recovery.prepare_handoff(
        tmp_path, task, cause=recovery.CAUSE_PLANNED_SELF_RESTART,
        old_attempt=1, new_attempt=2,
    )
    assert planned["cause"] == recovery.CAUSE_PLANNED_SELF_RESTART
    assert "child1" in recovery.recoverable_task_ids(tmp_path)


def test_same_run_crash_adoption_uses_exact_binding_and_never_posts(monkeypatch, tmp_path):
    from ouroboros import delegate_custody as custody
    import ouroboros.claudexor_daemon as daemon
    import ouroboros.delegate_recovery as recovery
    import ouroboros.tools.delegate as delegate
    from ouroboros.contracts.task_constraint import normalize_task_constraint
    from ouroboros.subagent_work_order import work_order_fingerprint
    from ouroboros.tools.registry import ToolContext

    custody._CUSTODY.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    snapshot = _snapshot(_settings(_session_row()), "session-builder")
    task = {
        "id": "child1", "_attempt": 2,
        "configured_subagent": snapshot,
        "workspace_root": str(workspace), "workspace_mode": "workspace_write",
        "drive_root": str(tmp_path), "task_constraint": {},
        "task_contract": {"objective": "Build", "expected_output": "Patch"},
    }
    ctx = ToolContext(
        repo_dir=workspace, drive_root=tmp_path, workspace_root=workspace,
        workspace_mode="workspace_write", task_id="child1",
        task_metadata={
            "workspace_root": str(workspace), "workspace_mode": "workspace_write",
            "drive_root": str(tmp_path), "configured_subagent": snapshot,
        },
        task_constraint=normalize_task_constraint({}),
        task_contract=task["task_contract"],
    )
    ctx.budget_drive_root = str(tmp_path)
    authority = recovery.authority_fingerprint_from_context(ctx)
    assert authority == recovery.authority_fingerprint_from_task(task)
    custody.record_started(tmp_path, custody.RunCustody(
        run_id="run-1", task_id="child1", route_id="codex",
        selected_subagent_id="session-builder",
        config_fingerprint=snapshot["config_fingerprint"],
        authority_fingerprint=authority,
        work_order_fingerprint=work_order_fingerprint(task),
    ))
    handoff = recovery.prepare_handoff(
        tmp_path, task, cause=recovery.CAUSE_WORKER_CRASH,
        old_attempt=1, new_attempt=2, worker_id=1, exitcode=1,
    )
    assert handoff["run_id"] == "run-1"

    class Gateway:
        def get_run(self, run_id):
            assert run_id == "run-1"
            return {"id": run_id, "state": "running"}

        def close(self):
            pass

    monkeypatch.setattr(daemon, "ensure_owned_gateway", lambda: Gateway())
    monkeypatch.setattr(delegate, "exact_start", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("a verified live run must be adopted without a second POST")
    ))
    assert recovery.adopt_handoff(ctx, task) == {
        "status": "adopted", "run_id": "run-1", "cause": recovery.CAUSE_WORKER_CRASH,
    }


def test_planned_restart_selectively_restores_sleeping_leaf_and_wait_state(monkeypatch, tmp_path):
    from ouroboros import delegate_custody as custody
    import ouroboros.claudexor_daemon as daemon
    import ouroboros.delegate_recovery as recovery
    import ouroboros.delegate_interactions as interactions
    import ouroboros.tools.delegate as delegate
    from ouroboros.contracts.task_constraint import normalize_task_constraint
    from ouroboros.subagent_work_order import work_order_fingerprint
    from ouroboros.tools.registry import ToolContext
    from ouroboros.utils import atomic_write_json

    custody._CUSTODY.clear()
    interactions._REPORTED_INTERACTIONS.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    snapshot = _snapshot(_settings(_session_row()), "session-builder")
    task = {
        "id": "child1", "_attempt": 1, "configured_subagent": snapshot,
        "workspace_root": str(workspace), "workspace_mode": "workspace_write",
        "drive_root": str(tmp_path), "task_constraint": {},
        "task_contract": {"objective": "Build", "expected_output": "Patch"},
    }
    authority = recovery.authority_fingerprint_from_task(task)
    custody.record_started(tmp_path, custody.RunCustody(
        run_id="run-1", task_id="child1", route_id="codex",
        selected_subagent_id="session-builder",
        config_fingerprint=snapshot["config_fingerprint"],
        authority_fingerprint=authority,
        work_order_fingerprint=work_order_fingerprint(task),
    ))
    wait_path = tmp_path / "state" / "delegate_supervision" / "child1.json"
    wait_path.parent.mkdir(parents=True)
    atomic_write_json(wait_path, {
        "schema": 1, "run_id": "run-1", "status": "sleeping", "journal_cursor": 7,
        "mailbox_seen_ids": ["m-old"],
        "last_wake": {"pending_interactions": [{"interaction_id": "quiz-1"}]},
        "checkpoint": {"reason": "inspect artifact", "consumed": False},
    })
    [preserved] = recovery.prepare_planned_restart_handoffs(
        tmp_path, {"child1": {"task": task, "attempt": 1, "worker_id": 3}},
    )
    assert preserved == "child1"
    handoff = recovery._read(tmp_path, "child1")
    assert handoff["no_resume_veto_causes"] == list(recovery.NO_RESUME_CAUSES)

    old_pid = handoff["supervisor_pid"]
    monkeypatch.setattr(recovery.os, "getpid", lambda: old_pid + 100_000)
    monkeypatch.setattr(recovery, "_pid_alive", lambda _pid: False)

    class Gateway:
        def get_run(self, run_id):
            assert run_id == "run-1"
            return {"id": run_id, "state": "running"}

        def close(self):
            pass

    monkeypatch.setattr(daemon, "ensure_owned_gateway", lambda: Gateway())
    successor = {**task, "_attempt": 2}
    assert recovery.pre_adopt_planned_handoffs(tmp_path, [successor]) == {"child1"}
    ctx = ToolContext(
        repo_dir=workspace, drive_root=tmp_path, workspace_root=workspace,
        workspace_mode="workspace_write", task_id="child1",
        task_metadata={"drive_root": str(tmp_path)},
        task_constraint=normalize_task_constraint({}), task_contract=task["task_contract"],
    )
    monkeypatch.setattr(delegate, "exact_start", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("planned handoff must adopt, never POST")
    ))
    assert recovery.adopt_handoff(ctx, successor)["status"] == "adopted"
    restored = json.loads(wait_path.read_text(encoding="utf-8"))
    assert restored["journal_cursor"] == 7
    assert restored["mailbox_seen_ids"] == ["m-old"]
    assert restored["checkpoint"]["reason"] == "inspect artifact"
    assert interactions._REPORTED_INTERACTIONS["run-1"] == frozenset({"quiz-1"})


def test_only_approved_restart_causes_reserve_and_abrupt_gap_vetoes(monkeypatch, tmp_path):
    from ouroboros import delegate_custody as custody
    import ouroboros.delegate_recovery as recovery
    from ouroboros.subagent_work_order import work_order_fingerprint

    custody._CUSTODY.clear()
    snapshot = _snapshot(_settings(_session_row()), "session-builder")
    task = {
        "id": "child1", "_attempt": 1, "configured_subagent": snapshot,
        "drive_root": str(tmp_path), "task_constraint": {}, "task_contract": {},
    }
    authority = recovery.authority_fingerprint_from_task(task)
    custody.record_started(tmp_path, custody.RunCustody(
        run_id="run-1", task_id="child1", route_id="codex",
        selected_subagent_id="session-builder",
        config_fingerprint=snapshot["config_fingerprint"],
        authority_fingerprint=authority,
        work_order_fingerprint=work_order_fingerprint(task),
    ))
    for cause in recovery.NO_RESUME_CAUSES:
        assert recovery.prepare_handoff(
            tmp_path, task, cause=cause, old_attempt=1, new_attempt=2,
        ) == {}
    row = recovery.prepare_handoff(
        tmp_path, task, cause=recovery.CAUSE_PLANNED_SELF_RESTART,
        old_attempt=1, new_attempt=2,
    )
    assert recovery.recoverable_task_ids(tmp_path) == {"child1"}
    monkeypatch.setattr(recovery, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(recovery.os, "getpid", lambda: int(row["supervisor_pid"]) + 1)
    assert recovery.recoverable_task_ids(tmp_path) == set()
    monkeypatch.setattr(custody, "reconcile_task_runs", lambda *_a, **_k: [])
    assert recovery.pre_adopt_planned_handoffs(tmp_path, []) == set()
    assert recovery._read(tmp_path, "child1")["veto_reason"] == "startup_restored_task_missing"


def test_planned_restart_kill_keeps_selected_child_even_if_parent_is_interrupted(
    monkeypatch, tmp_path,
):
    from supervisor import queue as task_queue
    from supervisor import workers

    repo = tmp_path / "repo"
    repo.mkdir()
    workers.init(repo, tmp_path, 2, 600, 1800, 100.0)
    workers.WORKERS.clear()
    workers.PENDING.clear()
    workers.RUNNING.clear()
    workers.RUNNING.update({
        "parent": {"task": {"id": "parent", "root_task_id": "parent"}, "attempt": 1},
        "child": {"task": {
            "id": "child", "parent_task_id": "parent", "root_task_id": "parent",
        }, "attempt": 1},
    })
    monkeypatch.setattr(workers, "_write_failure_result", lambda *_a, **_k: "cancelled")
    monkeypatch.setattr(workers, "_emit_task_done_terminal", lambda *_a, **_k: True)
    monkeypatch.setattr(task_queue, "persist_queue_snapshot", lambda *a, **k: True)
    workers.kill_workers(
        terminal_status="cancelled", preserve_pending=True,
        preserve_running_task_ids={"child"},
    )
    assert [task["id"] for task in workers.PENDING] == ["child"]
    assert workers.PENDING[0]["_attempt"] == 2
    workers.PENDING.clear()
    workers.RUNNING.clear()


def test_api_row_is_refused_by_root_direct_exact_start_before_daemon(monkeypatch, tmp_path):
    import ouroboros.tools.delegate as delegate
    import ouroboros.claudexor_daemon as daemon
    from ouroboros.tools.registry import ToolContext

    settings = _settings(_api_row())
    monkeypatch.setattr("ouroboros.config.load_settings", lambda: settings)
    monkeypatch.setattr(daemon, "ensure_owned_gateway", lambda: (_ for _ in ()).throw(
        AssertionError("API rows never POST to Claudexor")))
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "root1"
    out = json.loads(delegate.exact_start(
        ctx, "bounded leaf", {"subagent_id": "api-builder"},
    ))
    assert out["reason"] == "api_actor_requires_schedule_subagent"
    retry = json.loads(delegate.exact_start(
        ctx, "bounded leaf", {"subagent_id": "api-builder", "retry_of": "inv-old"},
    ))
    assert retry["reason"] == "retry_selector_conflict"


def test_heavy_is_absent_from_active_runtime_and_vision_consumers(monkeypatch):
    from ouroboros.llm import LLMClient
    from ouroboros.tools.vision import _vision_capable_slot_candidates

    monkeypatch.setenv("OUROBOROS_MODEL", "openai/main")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "openai/light")
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "openai/legacy-heavy")
    assert "openai/legacy-heavy" not in LLMClient().available_models()
    assert "openai/legacy-heavy" not in _vision_capable_slot_candidates(LLMClient())
