"""The owner's per-task execution allocation: record, schedule seam, evidence.

What these pin is one property in three places — the configured subagent the
owner picked for a piece of work is either honoured exactly or refused loudly,
never quietly swapped for another.

The allocation names a row in the Available-subagents catalog; it does not
define a route. WHICH agents exist is the owner's standing configuration, so
there is exactly one vocabulary for "where work can run" and this record only
says which row applies to which piece of work.
"""

from __future__ import annotations

import json
import queue

import pytest


# --- The record ----------------------------------------------------------------

def _plan_payload(**overrides):
    payload = {
        "version": 2,
        "root_task_id": "root1",
        "items": [{"item_id": "frontend", "title": "Frontend", "subagent_id": "primary"}],
    }
    payload.update(overrides)
    return payload


def test_a_plan_row_references_a_catalog_id_and_round_trips():
    from ouroboros.routing_plan import parse_routing_plan

    plan = parse_routing_plan(_plan_payload())
    item = plan.item("frontend")
    assert item is not None and item.subagent_id == "primary"
    assert parse_routing_plan(json.dumps(plan.as_dict())).item("frontend") == item


@pytest.mark.parametrize("payload,fragment", [
    (_plan_payload(version=1), "not supported"),
    (_plan_payload(items=[]), "non-empty"),
    (_plan_payload(items=[{"subagent_id": "primary"}]), "item_id"),
    (_plan_payload(items=[
        {"item_id": "a", "subagent_id": "primary"},
        {"item_id": "a", "subagent_id": "scout"},
    ]), "twice"),
    (_plan_payload(items=[{"item_id": "a"}]), "subagent_id"),
    (_plan_payload(items=[{"item_id": "a", "subagent_id": ""}]), "subagent_id"),
    (_plan_payload(items=[{"item_id": "a", "subagent_id": "x", "source": "nope"}]), "unknown source"),
    (_plan_payload(items=["not-an-object"]), "not an object"),
])
def test_a_malformed_plan_raises_rather_than_being_coerced(payload, fragment):
    """Coercing a typo would run a piece of work on an agent the owner never
    picked for it. Not a recoverable default."""
    from ouroboros.routing_plan import parse_routing_plan

    with pytest.raises(ValueError, match=fragment):
        parse_routing_plan(payload)


def test_a_v1_plan_is_refused_rather_than_reinterpreted():
    """v1 rows carried their own route. Reading one as a catalog reference would
    dispatch work to whatever id that text happened to match."""
    from ouroboros.routing_plan import parse_routing_plan

    v1 = {"version": 1, "root_task_id": "root1", "items": [
        {"item_id": "fe", "route": {"kind": "agent_session", "target_id": "codex"}}]}
    with pytest.raises(ValueError, match="not supported"):
        parse_routing_plan(v1)


def test_absence_is_none_and_corruption_raises(tmp_path, monkeypatch):
    from ouroboros.routing_plan import load_routing_plan, planned_subagent_id

    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    assert load_routing_plan("root1") is None
    assert planned_subagent_id("root1", "frontend") == ""

    path = tmp_path / "task_trees" / "root1" / "routing_plan.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_routing_plan("root1")


def test_an_unknown_item_is_not_an_error(tmp_path, monkeypatch):
    """A child naming a stale item is a mistaken reference, not a broken plan —
    raising would kill a whole task tree over one typo."""
    from ouroboros.routing_plan import parse_routing_plan, planned_subagent_id, write_routing_plan

    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    write_routing_plan(parse_routing_plan(_plan_payload()))
    assert planned_subagent_id("root1", "frontend") == "primary"
    assert planned_subagent_id("root1", "backend") == ""


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

    from ouroboros import route_evidence

    assert "record_task_eval(" in inspect.getsource(agent_task_pipeline.emit_task_results)
    assert "fold_task_outcome(" in inspect.getsource(route_evidence.record_task_eval)




# --- The schedule seam ------------------------------------------------------------

def _schedule_ctx(tmp_path):
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "parent1"
    ctx.task_depth = 0
    ctx.current_chat_id = 1
    ctx.event_queue = queue.Queue()
    ctx.task_metadata = {"root_task_id": "root1", "session_id": "sess1"}
    return ctx


def test_plan_item_id_is_published_and_selects_the_owners_agent(tmp_path, monkeypatch):
    """The plan says WHICH configured row this piece of work gets; the scheduler
    then snapshots that row exactly as a directly chosen subagent_id would be."""
    from ouroboros.routing_plan import parse_routing_plan, write_routing_plan
    from ouroboros.tools.control import schedule_subagent_properties

    assert "plan_item_id" in schedule_subagent_properties()
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    write_routing_plan(parse_routing_plan(_plan_payload()))

    seen = {}

    def fake_select(settings, *, subagent_id="", **kw):
        seen["subagent_id"] = subagent_id
        raise RuntimeError("stop after selection")

    monkeypatch.setattr("ouroboros.subagent_runtime.select_subagent_snapshot", fake_select)
    from ouroboros.tools.control import _schedule_task

    with pytest.raises(RuntimeError):
        _schedule_task(_schedule_ctx(tmp_path), objective="Build it",
                       expected_output="A patch", plan_item_id="frontend")
    assert seen["subagent_id"] == "primary"


def test_an_explicit_subagent_id_always_wins_over_the_plan(tmp_path, monkeypatch):
    """The parent naming a row is doing it on purpose; a stale plan must not
    override a deliberate choice."""
    from ouroboros.routing_plan import parse_routing_plan, write_routing_plan

    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    write_routing_plan(parse_routing_plan(_plan_payload()))

    seen = {}

    def fake_select(settings, *, subagent_id="", **kw):
        seen["subagent_id"] = subagent_id
        raise RuntimeError("stop after selection")

    monkeypatch.setattr("ouroboros.subagent_runtime.select_subagent_snapshot", fake_select)
    from ouroboros.tools.control import _schedule_task

    with pytest.raises(RuntimeError):
        _schedule_task(_schedule_ctx(tmp_path), objective="Build it",
                       expected_output="A patch", plan_item_id="frontend",
                       subagent_id="scout")
    assert seen["subagent_id"] == "scout"


def test_an_unreadable_plan_refuses_the_schedule(tmp_path, monkeypatch):
    """Nothing is scheduled: falling back would run the work on an agent the
    owner never approved for it."""
    from ouroboros.tools.control import _schedule_task

    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    path = tmp_path / "task_trees" / "root1" / "routing_plan.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 9}', encoding="utf-8")

    ctx = _schedule_ctx(tmp_path)
    result = _schedule_task(ctx, objective="Build it", expected_output="A patch",
                            plan_item_id="frontend")
    assert "TOOL_ARG_ERROR" in result
    assert ctx.event_queue.empty()
    assert not any((tmp_path / "task_results").glob("*.json"))


def test_an_unknown_item_is_disclosed_at_the_moment_it_is_known():
    """The scheduler knows the item did not resolve; the dispatch record only
    reports it. A quiet fallback would run the work on an agent the owner did
    not choose and show nothing unusual."""
    from ouroboros.subagents import SUBAGENT_INTENT_FIELDS, resolve_subagent_dispatch

    assert "routing_plan_item_unresolved" in SUBAGENT_INTENT_FIELDS
    delta = resolve_subagent_dispatch({
        "requested_executor": "native",
        "routing_plan_item": "frontend",
        "routing_plan_item_unresolved": "frontend",
    }).delta
    assert "routing_plan_item_unknown=frontend" in delta.reason
    assert delta.reduced is True


def test_the_allocation_survives_a_restart():
    """A PENDING child has nothing else naming the item it was scheduled for."""
    from supervisor.queue_snapshot_rows import pending_snapshot_row

    row = pending_snapshot_row({"routing_plan_item": "frontend",
                                "routing_plan_item_unresolved": "frontend"})["task"]
    assert row["routing_plan_item"] == "frontend"
    assert row["routing_plan_item_unresolved"] == "frontend"


# --- The proposal, before the owner sees it ------------------------------------

_CATALOG = {
    "primary": {"subagent_id": "primary", "name": "Primary", "recommended_use": "big edits",
                "route_kind": "agent_session", "target_id": "claude", "effort": "high"},
    "scout": {"subagent_id": "scout", "name": "Scout", "recommended_use": "quick reads",
              "route_kind": "api_model", "target_id": "openai/gpt-5-mini", "effort": "low"},
}


def _proposal_ctx(*, event_queue=...):
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=".", drive_root=".")
    ctx.task_id = "t1"
    ctx.current_chat_id = 1
    ctx.task_metadata = {"root_task_id": "root1"}
    ctx.event_queue = queue.Queue() if event_queue is ... else event_queue
    return ctx


def _items(*pairs):
    return [{"item_id": i, "title": i.title(), "subagent_id": s} for i, s in pairs]


def test_a_proposal_may_only_offer_agents_the_owner_configured(monkeypatch):
    """An id the catalog does not carry would be approved in good faith and then
    fail at dispatch — after the owner believed the decision was made."""
    from ouroboros.tools import execution_plan as mod

    monkeypatch.setattr(mod, "available_subagents", lambda: _CATALOG)
    assert mod._unchoosable(_items(("a", "primary"), ("b", "scout"))) == []
    refusals = mod._unchoosable(_items(("a", "primary"), ("b", "ghost")))
    assert len(refusals) == 1 and "b -> ghost" in refusals[0]


def test_an_empty_catalog_refuses_the_whole_proposal(monkeypatch):
    """Nothing to allocate ACROSS. Rendering an empty dropdown would ask the
    owner to choose between no options and then block on their answer."""
    from ouroboros.tools import execution_plan as mod

    monkeypatch.setattr(mod, "available_subagents", lambda: {})
    refusals = mod._unchoosable(_items(("a", "primary")))
    assert len(refusals) == 1 and "Settings" in refusals[0]


@pytest.mark.parametrize("raw,fragment", [
    ([], "non-empty"),
    ("nope", "non-empty"),
    ([{"item_id": "a"}], "subagent_id"),
    ([{"subagent_id": "primary"}], "item_id"),
    ([{"item_id": "a", "subagent_id": "primary"}, {"item_id": "a", "subagent_id": "scout"}], "twice"),
    (["not-an-object"], "must be an object"),
])
def test_a_malformed_proposal_is_refused_without_blocking(raw, fragment):
    """The refusal has to come back as a tool answer the agent can fix. Blocking
    on an unreadable proposal would hang the round on a question nobody sees."""
    from ouroboros.tools.execution_plan import _validated_items

    items, refusal = _validated_items(raw)
    assert items == [] and fragment in refusal


def test_too_many_items_is_refused():
    from ouroboros.tools.execution_plan import MAX_PROPOSAL_ITEMS, _validated_items

    rows = [{"item_id": f"i{n}", "subagent_id": "primary"} for n in range(MAX_PROPOSAL_ITEMS + 1)]
    items, refusal = _validated_items(rows)
    assert items == [] and "at most" in refusal


def test_the_wait_is_unbounded_by_re_asking_not_by_assuming(monkeypatch):
    """An unanswered proposal returns «nothing spent» — never the recommendation.
    Proceeding would spend on an agent the owner never saw."""
    from ouroboros.tools import execution_plan as mod

    monkeypatch.setattr(mod, "available_subagents", lambda: _CATALOG)
    monkeypatch.setattr(mod, "_emit_proposal", lambda ctx, proposal: True)
    monkeypatch.setattr(mod, "_await_decision", lambda ctx, task_id, deadline: None)
    answer = mod._propose_execution_plan(
        _proposal_ctx(), headline="Ship it", items=_items(("a", "primary")))
    assert "nothing" in answer.lower() and "primary" not in answer


def test_a_proposal_that_cannot_be_shown_is_a_refusal_not_a_silent_park(monkeypatch):
    """This call BLOCKS the round, so the round-end `pending_events` fallback will
    never run. A proposal parked there is a question the owner is never asked
    while the task waits forever for its answer."""
    from ouroboros.tools import execution_plan as mod

    monkeypatch.setattr(mod, "available_subagents", lambda: _CATALOG)
    waited = []
    monkeypatch.setattr(mod, "_await_decision", lambda *a, **k: waited.append(1))
    answer = mod._propose_execution_plan(
        _proposal_ctx(event_queue=None), headline="Ship it",
        items=_items(("a", "primary")))
    assert "TOOL_ARG_ERROR" in answer or "could not" in answer.lower()
    assert waited == []


# --- The owner's door ----------------------------------------------------------

def test_the_door_refuses_an_agent_the_catalog_does_not_carry(monkeypatch):
    """The dropdown only offered configured rows, but a hand-made POST can name
    anything, and approval is the last moment a bad id is cheap to refuse."""
    from ouroboros.gateway import execution_plan as door
    from ouroboros.routing_plan import parse_routing_plan

    monkeypatch.setattr("ouroboros.tools.execution_plan.available_subagents", lambda: _CATALOG)
    good = parse_routing_plan(_plan_payload())
    assert door._unreachable_rows(good) == []
    bad = parse_routing_plan(_plan_payload(
        items=[{"item_id": "frontend", "subagent_id": "ghost"}]))
    assert len(door._unreachable_rows(bad)) == 1


def test_the_door_serves_the_catalog_the_scheduler_uses(monkeypatch):
    """One list. A picker with its own idea of what exists is how a chooser and a
    dispatcher end up disagreeing about the same actor."""
    import inspect

    from ouroboros.gateway import execution_plan as door

    assert "available_subagents" in inspect.getsource(door.api_execution_targets)
