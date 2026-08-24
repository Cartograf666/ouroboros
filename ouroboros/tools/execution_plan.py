"""Ask the owner WHERE a big task should run, and wait for their answer.

A task large enough to be split across several children is also large enough
that the destination matters: the same work costs a subscription window on one
harness, real metered dollars on another, and an hour of local GPU on a third.
Ouroboros can judge the work; only the owner can weigh what they want to spend
on it today. So for that class of task the agent PROPOSES an allocation — one
row per piece of work, each with a recommended destination and the evidence
behind it — and the owner approves it or edits any row first.

WHY IT BLOCKS. Nothing is spent until the answer arrives (owner decision:
«пока ждем ответ всегда»). Proceeding on the recommendation after a timeout
would spend money on a destination the owner never saw, which is the exact
failure the proposal exists to prevent. The tool's own registered timeout is a
transport bound, not a policy: it returns «still unanswered, nothing spent» and
the agent waits again.

The owner's answer rides the task mailbox as a typed control
(``KIND_ROUTING_DECISION``) — append-only, deduplicated, revocable, cleaned up
with the task, and structurally routed so it never reaches the model as prose.
Mailbox reads do not mutate, so waiting here cannot starve the loop's own drain.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

# How long ONE tool call parks before handing control back. It is not a decision
# timeout: the call returns "still unanswered" and the agent re-waits, which
# keeps a parked task visibly alive (and cancellable) instead of pinning a
# worker to a single unbounded read.
PROPOSAL_WAIT_SEC = 1800
_POLL_INTERVAL_SEC = 2.0

MAX_PROPOSAL_ITEMS = 32
_MAX_TEXT = 400


def _proposal_error(message: str) -> str:
    return f"⚠️ TOOL_ARG_ERROR (propose_execution_plan): {message}"


def available_subagents() -> Dict[str, Dict[str, str]]:
    """The owner's Available-subagents catalog, keyed by id. {} when unusable.

    THE list of what work can run on. It is the owner's standing configuration,
    so a proposal may only offer rows from it — and the row's own name and
    ``recommended_use`` are what the card shows, rather than a route the owner
    would have to decode.
    """
    from ouroboros.config import load_settings
    from ouroboros.configured_subagents import resolve_configured_subagents
    from ouroboros.subagent_runtime import effective_runtime_subagent_settings

    resolution = resolve_configured_subagents(
        effective_runtime_subagent_settings(load_settings()))
    config = resolution.config
    if config is None or not config.enabled:
        return {}
    return {
        row.subagent_id: {
            "subagent_id": row.subagent_id,
            "name": row.name,
            "recommended_use": row.recommended_use,
            "route_kind": row.route.kind,
            "target_id": row.route.target_id,
            "effort": row.effort,
        }
        for row in config.items
    }


def _validated_items(raw: Any) -> Tuple[List[Dict[str, Any]], str]:
    """Normalize the proposed rows. Returns ``(items, "")`` or ``([], refusal)``."""

    if not isinstance(raw, list) or not raw:
        return [], _proposal_error("items must be a non-empty array of work items.")
    if len(raw) > MAX_PROPOSAL_ITEMS:
        return [], _proposal_error(
            f"at most {MAX_PROPOSAL_ITEMS} items ({len(raw)} given) — an allocation "
            "the owner cannot read in one card is not a choice."
        )
    items: List[Dict[str, Any]] = []
    seen: set = set()
    for index, row in enumerate(raw):
        where = f"items[{index}]"
        if not isinstance(row, dict):
            return [], _proposal_error(f"{where} must be an object.")
        item_id = str(row.get("item_id") or "").strip()
        if not item_id:
            return [], _proposal_error(f"{where} needs an item_id — the child you "
                                       "schedule for it references that id.")
        if item_id in seen:
            return [], _proposal_error(f"item_id {item_id!r} appears twice.")
        seen.add(item_id)
        subagent_id = str(row.get("subagent_id") or "").strip()
        if not subagent_id:
            return [], _proposal_error(
                f"{where} needs a subagent_id from the Available subagents catalog.")
        items.append({
            "item_id": item_id,
            "title": str(row.get("title") or "")[:_MAX_TEXT],
            "why": str(row.get("why") or "")[:_MAX_TEXT],
            "subagent_id": subagent_id,
            "estimate": row.get("estimate") if isinstance(row.get("estimate"), dict) else {},
        })
    return items, ""


def _unchoosable(items: List[Dict[str, Any]]) -> List[str]:
    """Rows the owner could not act on, refused BEFORE they are rendered.

    A proposal is a decision request, and every option in it has to be real: an
    id the catalog does not carry would be approved in good faith and then fail
    at dispatch, after the owner believed they had decided.
    """
    catalog = available_subagents()
    if not catalog:
        return ["the Available subagents catalog is empty or disabled — "
                "configure subagents in Settings before allocating work across them"]
    return [
        f"{row['item_id']} -> {row['subagent_id']}: not in the Available subagents catalog"
        for row in items if row["subagent_id"] not in catalog
    ]


def _decision_entry(ctx: ToolContext, task_id: str, seen: set) -> Dict[str, Any] | None:
    """The owner's routing decision from the mailbox, or None if not there yet."""
    from ouroboros.owner_mailbox import KIND_ROUTING_DECISION, drain_owner_entries

    for entry in drain_owner_entries(ctx.drive_root, task_id=task_id, seen_ids=seen):
        if str(entry.get("kind") or "") == KIND_ROUTING_DECISION:
            return entry
        # Everything else stays for the loop's own drain; recording the id here
        # only keeps THIS poll from re-reading it.
        seen.add(str(entry.get("msg_id") or ""))
    return None


def _await_decision(ctx: ToolContext, task_id: str, deadline: float) -> Dict[str, Any] | None:
    seen: set = set()
    while time.monotonic() < deadline:
        entry = _decision_entry(ctx, task_id, seen)
        if entry is not None:
            return entry
        time.sleep(_POLL_INTERVAL_SEC)
    return None


def _propose_execution_plan(ctx: ToolContext, **params: Any) -> str:
    items, refusal = _validated_items(params.get("items"))
    if refusal:
        return refusal
    task_id = str(getattr(ctx, "task_id", "") or "")
    if not task_id:
        return _proposal_error(
            "this call has no task to park (a conversational turn cannot hold a "
            "proposal open). Promote the work to a task first."
        )
    metadata = getattr(ctx, "task_metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    root_task_id = str(metadata.get("root_task_id") or "") or task_id
    try:
        problems = _unchoosable(items)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("execution target validation failed", exc_info=True)
        return _proposal_error(
            f"the available execution targets could not be read ({type(exc).__name__}). "
            "Nothing was proposed."
        )
    if problems:
        return _proposal_error(
            "these rows point at destinations the owner cannot choose right now: "
            + "; ".join(problems)
            + ". Propose reachable targets, or say plainly that the work must wait "
              "for one to come back."
        )

    proposal = {
        "type": "execution_plan_proposal",
        "task_id": task_id,
        "root_task_id": root_task_id,
        "headline": str(params.get("headline") or "")[:_MAX_TEXT],
        "items": items,
        "ts": utc_now_iso(),
    }
    if not _emit_proposal(ctx, proposal):
        return _proposal_error(
            "this task has no live channel to the owner, so the proposal could not "
            "be shown and waiting for it would block on a question nobody was "
            "asked. Nothing was scheduled — decide with the owner's standing "
            "policy instead, or run this from a supervised task."
        )
    ctx.emit_progress_fn(
        f"🧭 execution plan proposed: {len(items)} item(s) — waiting for your allocation")

    entry = _await_decision(ctx, task_id, time.monotonic() + PROPOSAL_WAIT_SEC)
    if entry is None:
        return (
            "WAITING_FOR_OWNER: the execution plan is on screen and unanswered. "
            "Nothing has been scheduled and nothing has been spent. Call "
            "propose_execution_plan again with the SAME items to keep waiting, or "
            "do work that does not depend on the allocation in the meantime."
        )
    return _apply_decision(entry, root_task_id, items)


def _emit_proposal(ctx: ToolContext, proposal: Dict[str, Any]) -> bool:
    """Put the proposal on the task's live card. False when it could not be shown.

    It rides the LIVE event queue and nothing else. The round-end
    ``pending_events`` fallback every other control event enjoys is useless
    here: this call blocks, so the round does not end until the answer arrives,
    and a proposal parked in that list would be delivered only after the wait it
    exists to start. A failure to show it is therefore a refusal to wait, not a
    silent degradation — the alternative is a task blocked on a question nobody
    was ever asked.
    """
    event_queue = getattr(ctx, "event_queue", None)
    if event_queue is None:
        return False
    try:
        event_queue.put_nowait({"type": "log_event", "data": dict(proposal)})
        return True
    except Exception:
        log.warning("execution plan proposal emit failed", exc_info=True)
        return False


def _apply_decision(
    entry: Dict[str, Any], root_task_id: str, items: List[Dict[str, Any]],
) -> str:
    """Persist the owner's approved allocation, or report why it could not be."""
    from ouroboros.routing_plan import (
        SOURCE_EDITED,
        SOURCE_RECOMMENDED,
        RoutingPlan,
        RoutingPlanItem,
        parse_routing_plan,
        write_routing_plan,
    )

    raw = str(entry.get("text") or "").strip()
    try:
        payload = json.loads(raw) if raw else {}
        plan = parse_routing_plan(payload, root_task_id=root_task_id)
    except ValueError as exc:
        # The decision arrived but is unusable. Say so instead of falling back:
        # a fallback here would run the work somewhere the owner did not pick,
        # while the transcript showed an approval.
        return (
            f"OWNER_DECISION_UNREADABLE: {exc}. Nothing was scheduled. Re-propose "
            "the allocation so the owner can answer again."
        )
    recommended = {row["item_id"]: row["subagent_id"] for row in items}
    catalog = available_subagents()
    stamped = RoutingPlan(
        root_task_id=root_task_id,
        approved_at=str(entry.get("ts") or utc_now_iso()),
        items=tuple(
            # Whether the owner TOOK the recommendation or overrode it is the
            # fact the routing evidence needs about the owner's own judgment, so
            # it is derived here — from what was proposed — rather than trusted
            # from a client that could label an override as an acceptance.
            RoutingPlanItem(
                item_id=row.item_id,
                title=row.title,
                subagent_id=row.subagent_id,
                source=(
                    SOURCE_RECOMMENDED
                    if recommended.get(row.item_id) == row.subagent_id
                    else SOURCE_EDITED
                ),
            )
            for row in plan.items
        ),
    )
    write_routing_plan(stamped)
    lines = [
        f"- {row.item_id}: "
        f"{(catalog.get(row.subagent_id) or {}).get('name') or row.subagent_id}"
        f" ({'as proposed' if row.source == SOURCE_RECOMMENDED else 'owner changed it'})"
        for row in stamped.items
    ]
    return (
        "APPROVED — schedule each child with schedule_subagent(plan_item_id=<item_id>) "
        "and it lands where the owner said:\n" + "\n".join(lines)
    )


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry("propose_execution_plan", {
            "name": "propose_execution_plan",
            "description": (
                "Propose HOW A BIG TASK IS ALLOCATED across execution targets and WAIT for the "
                "owner to approve or edit it. Use it when the work is large enough to split into "
                "several children AND the destination is a real choice — a subscription harness, "
                "a metered API model, or the local model each cost the owner something different. "
                "You judge the work; the owner weighs the spend. Give one item per piece of work "
                "with a concrete recommendation and the reason behind it (speed, price, quota, "
                "what the accumulated route evidence says). "
                "This call BLOCKS and spends nothing while it waits. It returns the APPROVED "
                "allocation — which may differ from what you proposed — and then you schedule one "
                "child per item with schedule_subagent(plan_item_id=<item_id>). "
                "Propose only targets that are reachable right now; unreachable rows are refused "
                "before the owner sees them. Do not use it for ordinary work, and never as a way "
                "to ask permission for something else."
            ),
            "parameters": {"type": "object", "properties": {
                "headline": {"type": "string", "description": "One line naming the whole task, as the owner would say it."},
                "items": {
                    "type": "array",
                    "description": "The pieces of work and where each should run.",
                    "items": {"type": "object", "properties": {
                        "item_id": {"type": "string", "description": "Short stable id you will pass as schedule_subagent(plan_item_id=...) later, e.g. 'frontend'."},
                        "title": {"type": "string", "description": "What this piece of work IS, in the owner's words."},
                        "why": {"type": "string", "description": "Why you recommend this destination for it — speed, cost, quota, past results."},
                        "subagent_id": {"type": "string", "description": "Exact id from the Available subagents catalog — the same ids schedule_subagent takes. Propose only rows that exist."},
                        "estimate": {"type": "object", "properties": {
                            "cost_usd": {"type": "number", "description": "What you expect this item to cost there."},
                            "duration_sec": {"type": "number", "description": "How long you expect it to take there."},
                            "basis": {"type": "string", "description": "Where the estimate comes from — say 'no evidence yet' when there is none."},
                        }},
                    }, "required": ["item_id", "subagent_id"]},
                },
            }, "required": ["items"]},
        }, _propose_execution_plan, timeout_sec=PROPOSAL_WAIT_SEC + 120),
    ]
