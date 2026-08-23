"""The owner's per-task execution allocation: record, dispatch, evidence.

What these pin is one property in three places — a destination the owner chose is
either honoured exactly or refused loudly, never quietly swapped for another.
"""

from __future__ import annotations

import json
import queue

import pytest


# --- The record ----------------------------------------------------------------

def _plan_payload(**overrides):
    payload = {
        "version": 1,
        "root_task_id": "root1",
        "items": [{
            "item_id": "frontend",
            "title": "Frontend",
            "route": {"kind": "agent_session", "target_id": "codex"},
        }],
    }
    payload.update(overrides)
    return payload


def test_plan_round_trips_and_names_its_destination():
    from ouroboros.routing_plan import parse_routing_plan

    plan = parse_routing_plan(_plan_payload())
    item = plan.item("frontend")
    assert item is not None
    assert item.route.kind == "agent_session"
    assert item.route.route_spec() == "codex"
    assert parse_routing_plan(json.dumps(plan.as_dict())).item("frontend").route == item.route


def test_a_session_route_with_a_model_uses_claudexors_own_spelling():
    from ouroboros.routing_plan import parse_routing_plan

    plan = parse_routing_plan(_plan_payload(items=[{
        "item_id": "fe",
        "route": {"kind": "agent_session", "target_id": "codex", "model": "gpt-5"},
    }]))
    assert plan.item("fe").route.route_spec() == "codex=gpt-5"


@pytest.mark.parametrize("payload,fragment", [
    (_plan_payload(version=2), "not supported"),
    (_plan_payload(items=[]), "non-empty"),
    (_plan_payload(items=[{"route": {"kind": "agent_session", "target_id": "codex"}}]),
     "item_id"),
    (_plan_payload(items=[
        {"item_id": "a", "route": {"kind": "agent_session", "target_id": "codex"}},
        {"item_id": "a", "route": {"kind": "agent_session", "target_id": "claude"}},
    ]), "twice"),
    (_plan_payload(items=[{"item_id": "a", "route": {"kind": "bogus", "target_id": "x"}}]),
     "unknown route kind"),
    (_plan_payload(items=[{"item_id": "a", "route": {"kind": "agent_session", "target_id": ""}}]),
     "target_id is empty"),
    (_plan_payload(items=[{"item_id": "a",
                           "route": {"kind": "agent_session", "target_id": "openai::gpt-5"}}]),
     "'::'"),
    (_plan_payload(items=[{"item_id": "a",
                           "route": {"kind": "agent_session", "target_id": "codex=gpt-5"}}]),
     "'='"),
    (_plan_payload(items=[{"item_id": "a",
                           "route": {"kind": "api_chat", "target_id": "m", "profile_id": "p"}}]),
     "credential profile"),
    (_plan_payload(items=[{"item_id": "a", "route": "codex"}]), "route must be an object"),
])
def test_a_malformed_plan_raises_rather_than_being_coerced(payload, fragment):
    """Coercing a typo either spends metered money the owner moved off, or
    delegates a row they never delegated. Neither is a recoverable default."""
    from ouroboros.routing_plan import parse_routing_plan

    with pytest.raises(ValueError, match=fragment):
        parse_routing_plan(payload)


def test_absence_is_none_and_corruption_raises(tmp_path, monkeypatch):
    from ouroboros.routing_plan import load_routing_plan, plan_pin_for_item

    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    assert load_routing_plan("root1") is None
    assert plan_pin_for_item("root1", "frontend") is None

    path = tmp_path / "task_trees" / "root1" / "routing_plan.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_routing_plan("root1")


def test_an_unknown_item_is_not_an_error(tmp_path, monkeypatch):
    """A child naming a stale item is a mistaken reference, not a broken plan —
    raising would kill a whole task tree over one typo."""
    from ouroboros.routing_plan import parse_routing_plan, plan_pin_for_item, write_routing_plan

    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    write_routing_plan(parse_routing_plan(_plan_payload()))
    assert plan_pin_for_item("root1", "frontend").target_id == "codex"
    assert plan_pin_for_item("root1", "backend") is None


# --- Dispatch ------------------------------------------------------------------

def _api_pin(model="google/gemini-3-pro"):
    return {"kind": "api_chat", "target_id": model, "model": "", "profile_id": ""}


def _session_pin(harness="claude"):
    return {"kind": "agent_session", "target_id": harness, "model": "", "profile_id": ""}


def test_no_pin_leaves_todays_resolution_untouched(monkeypatch):
    from ouroboros import subagents

    monkeypatch.setattr(subagents, "get_subagent_harness", lambda: None)
    resolution = subagents.dispatch_executor_resolution({"requested_executor": "auto"})
    assert resolution.executor == "native"
    assert resolution.route is None


def test_an_api_allocation_runs_native_on_the_pinned_model(monkeypatch):
    """The owner allocated the work to an API model. That is an ANSWER, so it must
    not fall through to the install-wide harness."""
    from ouroboros import subagents

    called = []
    monkeypatch.setattr(subagents, "get_subagent_harness",
                        lambda: called.append("asked") or None)
    task = {"requested_executor": "auto", "routing_pin": _api_pin()}
    resolution = subagents.dispatch_executor_resolution(task)
    assert resolution.executor == "native"
    assert resolution.reason == "routing_pin_api_model"
    # No daemon conversation at all for an api allocation.
    assert called == []


def test_a_session_allocation_overrides_the_install_wide_route(monkeypatch):
    from ouroboros import subagents

    seen = {}

    def fake_probe(requested, *, shape=None, route=None):
        seen["route"] = route
        return subagents.resolve_subagent_executor(requested, route=route)

    monkeypatch.setattr(subagents, "get_subagent_harness",
                        lambda: subagents.DelegationRoute(route_id="codex"))
    monkeypatch.setattr(subagents, "probe_subagent_executor", fake_probe)
    subagents.dispatch_executor_resolution(
        {"requested_executor": "auto", "routing_pin": _session_pin("claude")})
    assert seen["route"].route_id == "claude"


def test_an_unreadable_pin_blocks_instead_of_guessing():
    """Corruption in the frozen decision is refused. Falling back would spend on a
    destination nobody approved while the transcript showed an approval."""
    from ouroboros import subagents

    resolution = subagents.dispatch_executor_resolution(
        {"requested_executor": "auto", "routing_pin": {"kind": "nope", "target_id": "x"}})
    assert resolution.executor == "blocked"
    assert resolution.reason == "routing_pin_unreadable"


def test_a_native_pin_still_short_circuits(monkeypatch):
    from ouroboros import subagents

    resolution = subagents.dispatch_executor_resolution(
        {"requested_executor": "native", "routing_pin": _session_pin()})
    assert resolution.executor == "native"


# --- The lane, and what the record discloses ------------------------------------

def test_a_pinned_model_wins_without_faking_a_lane_reduction(monkeypatch):
    """`lane_ran_on_main` reads "differs from this lane's slot" as "the slot was
    empty" — false for a model the owner named by hand, and left in it would
    report a reduction on every pinned child."""
    from ouroboros.subagents import resolve_subagent_lane

    monkeypatch.setenv("OUROBOROS_MODEL", "main-model")
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "heavy-model")

    plain = resolve_subagent_lane("heavy")
    assert plain.model == "heavy-model"
    assert plain.model_source == "lane_slot"

    pinned = resolve_subagent_lane("heavy", pinned_model="google/gemini-3-pro")
    assert pinned.model == "google/gemini-3-pro"
    assert pinned.model_source == "allocation"
    assert pinned.effective_lane == "heavy"
    assert not pinned.reduced


def test_the_delta_discloses_the_allocation_and_an_unknown_item(monkeypatch, tmp_path):
    from ouroboros import subagents

    monkeypatch.setattr(subagents, "get_subagent_harness", lambda: None)
    monkeypatch.setenv("OUROBOROS_MODEL", "main-model")

    dispatch = subagents.resolve_subagent_dispatch({
        "requested_executor": "auto",
        "requested_model_lane": "main",
        "routing_plan_item": "frontend",
        "routing_pin": _api_pin("local-model"),
    })
    delta = dispatch.delta.as_dict()
    assert delta["allocation_item"] == "frontend"
    assert delta["model_source"] == "allocation"
    assert dispatch.lane.model == "local-model"

    orphan = subagents.resolve_subagent_dispatch({
        "requested_executor": "auto",
        "routing_plan_item": "frontend",
    }).delta.as_dict()
    assert "routing_plan_item_unknown=frontend" in orphan["reason"]
    assert orphan["reduced"] is True


def test_an_api_allocation_is_a_reduction_only_against_an_explicit_harness_pin(monkeypatch):
    from ouroboros import subagents

    monkeypatch.setattr(subagents, "get_subagent_harness", lambda: None)
    monkeypatch.setenv("OUROBOROS_MODEL", "main-model")
    task = {"requested_executor": "auto", "routing_pin": _api_pin("local-model")}
    assert not subagents.resolve_subagent_dispatch(task).delta.reduced

    pinned = dict(task, requested_executor="harness")
    delta = subagents.resolve_subagent_dispatch(pinned).delta
    assert delta.reduced
    assert "routing_pin_api_model" in delta.reason


# --- The schedule seam ----------------------------------------------------------

def _schedule_ctx(tmp_path):
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "parent1"
    ctx.task_depth = 0
    ctx.current_chat_id = 1
    ctx.event_queue = queue.Queue()
    ctx.task_metadata = {"root_task_id": "root1", "session_id": "sess1"}
    return ctx


def test_plan_item_id_is_published_and_freezes_the_destination(tmp_path, monkeypatch):
    from ouroboros.routing_plan import parse_routing_plan, write_routing_plan
    from ouroboros.tools.control import _schedule_task, schedule_subagent_properties

    assert "plan_item_id" in schedule_subagent_properties()
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    write_routing_plan(parse_routing_plan(_plan_payload()))

    ctx = _schedule_ctx(tmp_path)
    result = _schedule_task(
        ctx, objective="Build the frontend", expected_output="A patch",
        plan_item_id="frontend",
    )
    assert "TOOL_ARG_ERROR" not in result
    event = ctx.event_queue.get_nowait()
    assert event["routing_plan_item"] == "frontend"
    assert event["routing_pin"]["target_id"] == "codex"
    record = json.loads(
        (tmp_path / "task_results" / f"{event['task_id']}.json").read_text(encoding="utf-8"))
    assert record["routing_pin"]["kind"] == "agent_session"


def test_an_unknown_item_schedules_and_discloses_rather_than_refusing(tmp_path, monkeypatch):
    from ouroboros.tools.control import _schedule_task

    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    ctx = _schedule_ctx(tmp_path)
    result = _schedule_task(
        ctx, objective="Build it", expected_output="A patch", plan_item_id="nope")
    assert "TOOL_ARG_ERROR" not in result
    event = ctx.event_queue.get_nowait()
    assert event["routing_plan_item"] == "nope"
    assert event["routing_pin"] == {}


def test_an_unreadable_plan_refuses_the_schedule(tmp_path, monkeypatch):
    """Nothing is scheduled: falling back here would run the work somewhere the
    owner never approved."""
    from ouroboros.tools.control import _schedule_task

    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    path = tmp_path / "task_trees" / "root1" / "routing_plan.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 9}', encoding="utf-8")

    ctx = _schedule_ctx(tmp_path)
    result = _schedule_task(
        ctx, objective="Build it", expected_output="A patch", plan_item_id="frontend")
    assert "TOOL_ARG_ERROR" in result
    assert ctx.event_queue.empty()
    assert not any((tmp_path / "task_results").glob("*.json"))


def test_the_allocation_survives_a_restart(tmp_path):
    """A PENDING child has nothing else naming its destination, so the queue
    snapshot must carry it or the owner's decision dies with the process."""
    import inspect

    import supervisor.queue as queue_mod

    source = inspect.getsource(queue_mod.persist_queue_snapshot)
    assert '"routing_pin": t.get("routing_pin")' in source
    assert '"routing_plan_item": t.get("routing_plan_item")' in source


# --- The catalog ----------------------------------------------------------------

class _FakeGateway:
    def __init__(self, harnesses):
        self._harnesses = harnesses
        self.closed = False

    def agent_capabilities(self):
        return {"harnesses": self._harnesses}

    def harness_models(self, harness_id):
        return [{"id": f"{harness_id}-model"}]

    def close(self):
        self.closed = True


def test_a_fourth_harness_needs_no_code_change(monkeypatch):
    """Antigravity — or anything after it — appears the moment the engine lists
    it, spelled by the engine's own display name."""
    from ouroboros import execution_targets

    gateway = _FakeGateway([
        {"id": "codex", "displayName": "Codex"},
        {"id": "antigravity", "displayName": "Antigravity"},
    ])
    monkeypatch.setattr("ouroboros.claudexor_daemon.ensure_owned_gateway", lambda: gateway)
    monkeypatch.setattr("ouroboros.subagents.route_health", lambda *a, **k: ("", ""))

    rows, read, error = execution_targets.session_targets(include_models=True)
    assert read == "ok" and not error
    assert [row.target_id for row in rows] == ["codex", "antigravity"]
    assert rows[1].label == "Antigravity"
    assert rows[1].models == ("antigravity-model",)
    assert gateway.closed


def test_an_unreachable_daemon_is_a_gap_not_an_empty_catalog(monkeypatch):
    from ouroboros import execution_targets
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    def boom():
        raise ClaudexorUnavailable("daemon_unreachable")

    monkeypatch.setattr("ouroboros.claudexor_daemon.ensure_owned_gateway", boom)
    rows, read, error = execution_targets.session_targets()
    assert rows == [] and read == "failed" and error


def test_a_pin_is_refused_when_its_target_is_not_choosable(monkeypatch):
    from ouroboros.execution_targets import (
        ExecutionTargetCatalog,
        ExecutionTarget,
        validate_pin,
    )
    from ouroboros.routing_plan import RoutePin

    catalog = ExecutionTargetCatalog(
        api_chat=[ExecutionTarget(kind="api_chat", target_id="m1", label="Main · m1")],
        agent_session=[ExecutionTarget(
            kind="agent_session", target_id="codex", label="Codex",
            available=False, unavailable_reason="subscription_window_exhausted")],
        session_read="ok",
    )
    assert validate_pin(RoutePin(kind="api_chat", target_id="m1"), catalog=catalog) == ""
    assert validate_pin(
        RoutePin(kind="agent_session", target_id="codex"), catalog=catalog,
    ) == "subscription_window_exhausted"
    assert validate_pin(
        RoutePin(kind="agent_session", target_id="antigravity"), catalog=catalog,
    ) == "route_not_in_capability_catalog"


def test_an_unread_catalog_refuses_every_delegated_pin():
    """Approving a delegated row nobody could confirm parks the run on a route the
    engine may not carry — after the owner has already signed off."""
    from ouroboros.execution_targets import ExecutionTargetCatalog, validate_pin
    from ouroboros.routing_plan import RoutePin

    catalog = ExecutionTargetCatalog(session_read="failed", session_error="daemon_unreachable")
    assert validate_pin(RoutePin(kind="agent_session", target_id="codex"), catalog=catalog)


# --- Accumulated evidence --------------------------------------------------------

def test_folding_the_same_task_twice_does_not_double_count(tmp_path):
    from ouroboros.route_evidence import record_route_outcome, route_stats

    for _ in range(3):
        record_route_outcome(tmp_path, kind="agent_session", target_id="codex",
                             task_id="t1", duration_sec=120, cost_usd=None, ok=True)
    stats = route_stats(tmp_path)[0]
    assert stats.samples == 1


def test_an_undisclosed_cost_is_unknown_not_free(tmp_path):
    """`$0.00` for a route nobody measured is the single most misleading number a
    routing digest could print."""
    from ouroboros.route_evidence import (
        format_route_evidence_digest,
        record_route_outcome,
        route_stats,
    )

    record_route_outcome(tmp_path, kind="agent_session", target_id="codex",
                         task_id="t1", duration_sec=120, cost_usd=None, ok=True)
    stats = route_stats(tmp_path)[0]
    assert stats.median_cost_usd is None
    assert stats.cost_known_samples == 0
    assert "cost undisclosed" in format_route_evidence_digest(tmp_path)


def test_a_review_verdict_is_never_invented_from_a_clean_exit(tmp_path):
    from ouroboros.route_evidence import format_route_evidence_digest, record_route_outcome

    record_route_outcome(tmp_path, kind="api_chat", target_id="m1",
                         task_id="t1", duration_sec=10, cost_usd=0.5, ok=True)
    digest = format_route_evidence_digest(tmp_path)
    assert "1/1 finished cleanly" in digest
    assert "solved" not in digest

    record_route_outcome(tmp_path, kind="api_chat", target_id="m1", task_id="t2",
                         duration_sec=10, cost_usd=0.5, ok=True, outcome_tier="solved")
    assert "1/1 reviewed solved" in format_route_evidence_digest(tmp_path)


def test_a_cold_install_pays_no_context_for_the_digest(tmp_path):
    from ouroboros.route_evidence import format_route_evidence_digest

    assert format_route_evidence_digest(tmp_path) == ""


def test_one_slow_run_does_not_redefine_a_route(tmp_path):
    from ouroboros.route_evidence import record_route_outcome, route_stats

    for index, seconds in enumerate([60, 60, 60, 3600, 60]):
        record_route_outcome(tmp_path, kind="agent_session", target_id="codex",
                             task_id=f"t{index}", duration_sec=seconds, ok=True)
    assert route_stats(tmp_path)[0].median_duration_sec == 60


# --- The rule that keeps this generic ---------------------------------------------

def test_no_new_harness_name_branch_in_the_core():
    """docs/DELEGATED_ADMISSION.md names exactly ONE harness-name branch in the
    core, and it is a login-transport rule. Route ids stay opaque everywhere else
    — that is what lets a new family arrive with no code change at all."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    pattern = re.compile(
        r"""(harness|route|route_id|target_id|executor_route)\w*\s*(==|!=)\s*"""
        r"""["'](codex|claude|cursor|opencode|antigravity)["']""")
    files = [
        *(root / "ouroboros").rglob("*.py"),
        *(root / "supervisor").rglob("*.py"),
        root / "server.py",
        root / "launcher.py",
    ]
    offenders = set()
    for path in files:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if pattern.search(line):
                offenders.add(str(path.relative_to(root)))
    # The FILE, not the line: the documented residual may move within its own
    # module, but a second module growing one is the regression this guards.
    assert offenders == {"ouroboros/gateway/claudexor_accounts.py"}, sorted(offenders)


def test_the_fold_credits_what_actually_ran(tmp_path, monkeypatch):
    """A child dispatched to a harness that fell back to native ran on metered
    tokens; crediting the harness would teach the evidence the opposite."""
    from ouroboros.route_evidence import fold_task_outcome, route_stats

    monkeypatch.setattr(
        "ouroboros.subagents.envelope_from_task",
        lambda task, **kw: {"actual_substrate": "native_only"})
    fold_task_outcome(
        tmp_path,
        {"id": "t1", "executor_route": "codex", "model": "main-model"},
        {}, 12.0, {"cost_usd": 0.4}, ok=True,
    )
    stats = route_stats(tmp_path)
    assert [(row.kind, row.target_id) for row in stats] == [("api_chat", "main-model")]

    fold_task_outcome(
        tmp_path,
        {"id": "t2", "executor_route": "codex", "model": "main-model",
         "actual_substrate": "harness_used"},
        {}, 30.0, {"cost_usd": None}, ok=True,
    )
    assert ("agent_session", "codex") in [(row.kind, row.target_id) for row in route_stats(tmp_path)]


def test_the_finalization_seam_still_folds():
    """One writer, and it is wired. Without this the store silently stops filling
    and every later proposal quietly loses its evidence."""
    import inspect

    from ouroboros import agent_task_pipeline

    source = inspect.getsource(agent_task_pipeline.emit_task_results)
    assert "fold_task_outcome(" in source


# --- The proposal tool -------------------------------------------------------------

def _tool_ctx(tmp_path, *, live=True):
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "task1"
    ctx.task_metadata = {"root_task_id": "root1"}
    if live:
        ctx.event_queue = queue.Queue()
    return ctx


_ITEMS = [{
    "item_id": "frontend",
    "title": "Frontend",
    "recommended_route": {"kind": "agent_session", "target_id": "codex"},
}]


def test_a_proposal_with_no_live_channel_refuses_instead_of_parking(tmp_path, monkeypatch):
    """Blocking on a question nobody was shown is the one outcome worse than not
    asking at all."""
    from ouroboros.tools import execution_plan

    monkeypatch.setattr(execution_plan, "_unchoosable", lambda items: [])
    result = execution_plan._propose_execution_plan(_tool_ctx(tmp_path, live=False), items=_ITEMS)
    assert "TOOL_ARG_ERROR" in result and "no live channel" in result


def test_an_unreachable_destination_never_reaches_the_owner(tmp_path, monkeypatch):
    from ouroboros.tools import execution_plan

    monkeypatch.setattr(
        execution_plan, "_unchoosable", lambda items: ["frontend -> codex: window_spent"])
    ctx = _tool_ctx(tmp_path)
    result = execution_plan._propose_execution_plan(ctx, items=_ITEMS)
    assert "window_spent" in result
    assert ctx.event_queue.empty()


def test_an_unanswered_proposal_returns_typed_and_spends_nothing(tmp_path, monkeypatch):
    from ouroboros.tools import execution_plan

    monkeypatch.setattr(execution_plan, "_unchoosable", lambda items: [])
    monkeypatch.setattr(execution_plan, "_await_decision", lambda *a, **k: None)
    ctx = _tool_ctx(tmp_path)
    result = execution_plan._propose_execution_plan(ctx, items=_ITEMS)
    assert result.startswith("WAITING_FOR_OWNER")
    emitted = ctx.event_queue.get_nowait()
    assert emitted["data"]["type"] == "execution_plan_proposal"


def test_the_owner_taking_or_overruling_the_recommendation_is_recorded(tmp_path, monkeypatch):
    """Derived from what was PROPOSED, never trusted from the client — a surface
    that could label its own override as an acceptance would corrupt the one
    record of the owner's judgment."""
    from ouroboros.routing_plan import SOURCE_EDITED, SOURCE_RECOMMENDED, load_routing_plan
    from ouroboros.tools import execution_plan

    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    monkeypatch.setattr(execution_plan, "_unchoosable", lambda items: [])

    def decision(route_target, source_lie):
        return {"ts": "2026-08-20T00:00:00Z", "text": json.dumps({
            "version": 1,
            "root_task_id": "root1",
            "items": [{"item_id": "frontend", "title": "Frontend", "source": source_lie,
                       "route": {"kind": "agent_session", "target_id": route_target}}],
        })}

    monkeypatch.setattr(execution_plan, "_await_decision",
                        lambda *a, **k: decision("codex", "owner_edit"))
    result = execution_plan._propose_execution_plan(_tool_ctx(tmp_path), items=_ITEMS)
    assert result.startswith("APPROVED")
    assert load_routing_plan("root1").item("frontend").source == SOURCE_RECOMMENDED

    monkeypatch.setattr(execution_plan, "_await_decision",
                        lambda *a, **k: decision("claude", "owner_accepted_recommendation"))
    execution_plan._propose_execution_plan(_tool_ctx(tmp_path), items=_ITEMS)
    plan = load_routing_plan("root1")
    assert plan.item("frontend").source == SOURCE_EDITED
    assert plan.item("frontend").route.target_id == "claude"


def test_an_unreadable_decision_schedules_nothing(tmp_path, monkeypatch):
    from ouroboros.routing_plan import load_routing_plan
    from ouroboros.tools import execution_plan

    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    monkeypatch.setattr(execution_plan, "_unchoosable", lambda items: [])
    monkeypatch.setattr(execution_plan, "_await_decision",
                        lambda *a, **k: {"ts": "", "text": "{broken"})
    result = execution_plan._propose_execution_plan(_tool_ctx(tmp_path), items=_ITEMS)
    assert result.startswith("OWNER_DECISION_UNREADABLE")
    assert load_routing_plan("root1") is None


def test_a_routing_decision_is_never_injected_as_owner_prose():
    """Delivered as prose it would reach the model as a wall of JSON to interpret
    — and the one thing that must not be re-interpreted is which destination the
    owner approved."""
    import inspect

    from ouroboros import loop
    from ouroboros.owner_mailbox import KIND_ROUTING_DECISION

    source = inspect.getsource(loop)
    assert "KIND_ROUTING_DECISION" in source
    assert KIND_ROUTING_DECISION == "routing_decision"


# --- The owner's door ---------------------------------------------------------------

def _decision_client(monkeypatch, tmp_path):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from ouroboros.gateway.execution_plan import api_execution_plan_decision

    app = Starlette(routes=[Route("/api/execution-plan/decision",
                                  api_execution_plan_decision, methods=["POST"])])
    return TestClient(app)


def test_the_door_refuses_a_malformed_plan_before_it_can_strand_a_run(monkeypatch, tmp_path):
    client = _decision_client(monkeypatch, tmp_path)
    resp = client.post("/api/execution-plan/decision",
                       json={"task_id": "t1", "plan": {"version": 1, "items": []}})
    assert resp.status_code == 400
    assert "non-empty" in resp.json()["error"]


def test_the_door_refuses_a_hand_made_unreachable_destination(monkeypatch, tmp_path):
    """The dropdown only offered reachable rows, but the door is not the dropdown."""
    import ouroboros.gateway.execution_plan as gateway

    monkeypatch.setattr(gateway, "_unreachable_rows",
                        lambda plan: ["frontend -> ghost: route_not_in_capability_catalog"])
    client = _decision_client(monkeypatch, tmp_path)
    resp = client.post("/api/execution-plan/decision",
                       json={"task_id": "t1", "plan": _plan_payload()})
    assert resp.status_code == 409
    assert "route_not_in_capability_catalog" in resp.json()["error"]


def test_the_door_refuses_a_decision_for_a_task_that_already_finished(monkeypatch, tmp_path):
    """Nobody is waiting for it, and an approved allocation no run will honour is
    worse than no answer at all."""
    import ouroboros.gateway.execution_plan as gateway

    monkeypatch.setattr(gateway, "_unreachable_rows", lambda plan: [])
    monkeypatch.setattr("supervisor.queue.DRIVE_ROOT", tmp_path)
    monkeypatch.setattr("ouroboros.task_status.load_effective_task_result",
                        lambda root, tid: {"status": "completed"})
    client = _decision_client(monkeypatch, tmp_path)
    resp = client.post("/api/execution-plan/decision",
                       json={"task_id": "t1", "plan": _plan_payload()})
    assert resp.status_code == 409
    assert "already completed" in resp.json()["error"]


def test_an_accepted_decision_lands_in_the_waiting_tasks_mailbox(monkeypatch, tmp_path):
    import ouroboros.gateway.execution_plan as gateway
    from ouroboros.owner_mailbox import KIND_ROUTING_DECISION, drain_owner_entries

    monkeypatch.setattr(gateway, "_unreachable_rows", lambda plan: [])
    monkeypatch.setattr("supervisor.queue.DRIVE_ROOT", tmp_path)
    monkeypatch.setattr("supervisor.queue._task_drive_for_task", lambda record, tid: tmp_path)
    monkeypatch.setattr("ouroboros.task_status.load_effective_task_result",
                        lambda root, tid: {"status": "running"})
    client = _decision_client(monkeypatch, tmp_path)
    resp = client.post("/api/execution-plan/decision",
                       json={"task_id": "t1", "plan": _plan_payload()})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    entries = drain_owner_entries(tmp_path, "t1", seen_ids=set())
    assert [e["kind"] for e in entries] == [KIND_ROUTING_DECISION]
    assert json.loads(entries[0]["text"])["items"][0]["item_id"] == "frontend"


# --- A harness with no default credential ------------------------------------------

class _ProfileGateway:
    """A daemon whose harness is UNAVAILABLE while its named account works.

    Not hypothetical: this is Antigravity's live 3.8.0 shape. Its readiness carries
    `default_credential: fail` ("accounts are named profiles"), so the harness row
    reads `unavailable` forever — byte-identical before and after a fully successful
    sign-in — while the credential-profile view reports the account verified.
    """

    engine_version = "3.8.0"

    def __init__(self, *, status="unavailable", profiles=(), quota=()):
        self._status = status
        self._profiles = list(profiles)
        self._quota = list(quota)
        self.closed = False

    def agent_capabilities(self):
        return {"harnesses": [{
            "id": "agy", "displayName": "Antigravity", "enabled": True,
            "status": self._status,
            "accessProfilesSupported": ["readonly", "workspace_write", "full",
                                        "external_sandbox_full", "inherit_native"],
        }]}

    def credential_profiles(self):
        return {"profiles": self._profiles}

    def quota_snapshots(self):
        return self._quota

    def quota_absences(self):
        return []

    def close(self):
        self.closed = True


def _profile(profile_id, *, enabled=True, availability="available"):
    return {
        "profile": {"profile_id": profile_id, "harness_id": "agy", "enabled": enabled},
        "status": {"profile_id": profile_id, "harness_id": "agy",
                   "availability": availability},
    }


def test_a_verified_named_account_is_read_as_the_engines_verdict():
    from ouroboros.subagents import routable_profile

    gateway = _ProfileGateway(profiles=[
        _profile("stale", availability="unknown"),
        _profile("off", enabled=False),
        _profile("work"),
    ])
    assert routable_profile(gateway, "agy") == "work"
    # An attached-but-unauthenticated account is exactly what must NOT count.
    assert routable_profile(_ProfileGateway(profiles=[_profile("w", availability="unknown")]),
                            "agy") == ""
    assert routable_profile(_ProfileGateway(profiles=[_profile("w", enabled=False)]), "agy") == ""
    assert routable_profile(_ProfileGateway(), "other") == ""


def test_an_unreadable_profile_store_falls_back_instead_of_inventing_health():
    from ouroboros.subagents import routable_profile

    class Broken(_ProfileGateway):
        def credential_profiles(self):
            raise RuntimeError("store unreadable")

    assert routable_profile(Broken(), "agy") == ""


def test_a_profile_only_harness_is_routable_on_its_named_account():
    """Reading the harness status alone refused a route whose account the engine
    had just confirmed working — Antigravity could be connected but never used."""
    from ouroboros.subagents import delegated_run_shape, route_health

    shape = delegated_run_shape(False)
    healthy = _ProfileGateway(profiles=[_profile("work")])
    assert route_health(healthy, "agy", shape) == ("", "")

    # No verified account: the status still decides, exactly as before.
    bare = _ProfileGateway(profiles=[_profile("work", availability="unknown")])
    assert route_health(bare, "agy", shape)[0] == "route_status_unavailable"


def test_a_disabled_harness_is_refused_whatever_its_accounts_say():
    """`enabled: false` is the OWNER turning a route off; a working account must
    not talk them out of it."""
    from ouroboros.subagents import delegated_run_shape, route_health

    gateway = _ProfileGateway(profiles=[_profile("work")])
    gateway.agent_capabilities = lambda: {"harnesses": [{
        "id": "agy", "enabled": False, "status": "ok",
        "accessProfilesSupported": ["readonly"],
    }]}
    assert route_health(gateway, "agy", delegated_run_shape(False))[0] == "route_status_disabled"


def test_the_pin_is_resolved_only_for_a_harness_the_engine_will_not_route_itself():
    """A healthy harness keeps an empty pin so the daemon's own rotation (D28)
    is never overridden; a profile-only one gets its account named."""
    from ouroboros.subagents import DelegationRoute, _pinned_for_profile_only_route

    profiles = [_profile("work")]
    unavailable = _ProfileGateway(status="unavailable", profiles=profiles)
    pinned = _pinned_for_profile_only_route(unavailable, DelegationRoute(route_id="agy"))
    assert pinned.profile_id == "work"

    ok = _ProfileGateway(status="ok", profiles=profiles)
    assert _pinned_for_profile_only_route(ok, DelegationRoute(route_id="agy")).profile_id == ""

    # An owner's explicit pin is never second-guessed.
    explicit = DelegationRoute(route_id="agy", profile_id="mine")
    assert _pinned_for_profile_only_route(unavailable, explicit).profile_id == "mine"


def test_a_delegated_run_actually_carries_the_credential_pin():
    """Until now only the REVIEW path sent it, so a subagent run on a named-account
    route started with no account at all and the engine refused it as unroutable."""
    from ouroboros.subagents import DelegationRoute, delegated_run_shape
    from ouroboros.tools.delegate import _start_request

    shape = delegated_run_shape(False)
    pinned = _start_request(
        None, DelegationRoute(route_id="agy", profile_id="work"), shape,
        "/tmp/root", "do it", 0, "instructions")
    assert pinned["credentialProfileId"] == "work"
    assert pinned["harnesses"] == ["agy"]

    # Empty stays empty: rotation is the documented default.
    unpinned = _start_request(
        None, DelegationRoute(route_id="codex"), shape,
        "/tmp/root", "do it", 0, "instructions")
    assert "credentialProfileId" not in unpinned
