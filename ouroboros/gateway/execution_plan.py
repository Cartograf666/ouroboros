"""The owner's side of an execution-plan proposal: read targets, answer with one.

Two thin routes. The catalog GET tells the proposal card what the owner may
choose between; the decision POST carries their answer — approved as proposed or
with rows changed — into the waiting task's mailbox.

Neither route routes anything itself. The decision is delivered as a typed
mailbox control and the task that asked is the party that applies it, for the
same reason the accounts panel proxies rather than decides: the surface that
renders a choice must not also be the one that acts on it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.gateway._helpers import json_error, request_json_or

log = logging.getLogger(__name__)


async def api_execution_targets(request: Request) -> JSONResponse:
    """GET /api/execution-targets — the Available subagents the owner configured.

    One list, from the owner's own catalog: the same rows `schedule_subagent`
    takes by id. An empty answer means the catalog is empty or disabled, which
    is a real state the card must render rather than guess around.
    """
    def _read() -> Dict[str, Any]:
        from ouroboros.tools.execution_plan import available_subagents

        rows = available_subagents()
        return {"subagents": list(rows.values()), "enabled": bool(rows)}

    try:
        return JSONResponse(await asyncio.to_thread(_read))
    except Exception as exc:
        log.exception("api_execution_targets failed")
        return json_error(f"{type(exc).__name__}: execution targets unavailable")


def _unreachable_rows(plan: Any) -> list:
    """Rows naming a subagent the catalog does not carry. [] when fine.

    The dropdown only offered configured rows, but the door is not the dropdown:
    a hand-made POST can name anything, and an approval is the last moment a bad
    id is still cheap to refuse.
    """
    from ouroboros.tools.execution_plan import available_subagents

    catalog = available_subagents()
    return [
        f"{item.item_id} -> {item.subagent_id}: not in the Available subagents catalog"
        for item in plan.items if item.subagent_id not in catalog
    ]


def _deliver(task_id: str, plan_text: str) -> Dict[str, Any]:
    from ouroboros.owner_mailbox import KIND_ROUTING_DECISION, write_owner_message
    from ouroboros.task_results import STATUS_RUNNING, validate_task_id
    from ouroboros.task_status import FINAL_STATUSES, load_effective_task_result
    from supervisor.queue import DRIVE_ROOT, _task_drive_for_task

    tid = validate_task_id(task_id)
    record = load_effective_task_result(pathlib.Path(DRIVE_ROOT), tid) or {}
    if not record:
        raise ValueError(f"task {tid} is not registered")
    status = str(record.get("status") or "").lower()
    if status in FINAL_STATUSES:
        # The task that asked has already finished, so nobody is waiting for this
        # answer. Delivering it anyway would leave an approved allocation on disk
        # that no run will ever honour.
        raise ValueError(f"task {tid} is already {status}; the proposal is closed")
    if status != STATUS_RUNNING:
        raise ValueError(f"task {tid} is {status or 'unknown'}, not running")
    drive = _task_drive_for_task(record, tid)
    if not write_owner_message(drive, plan_text, tid, kind=KIND_ROUTING_DECISION):
        raise ValueError("the decision could not be written durably; nothing was delivered")
    return {"ok": True, "task_id": tid}


async def api_execution_plan_decision(request: Request) -> JSONResponse:
    """POST /api/execution-plan/decision — the owner's approved allocation.

    The plan is PARSED here before it is delivered. A malformed allocation
    refused at the door is a fixable form error the owner sees immediately;
    delivered, it would reach the waiting task as an unreadable approval and
    strand the run — the strict-parse posture the record itself keeps.
    """
    body = await request_json_or(request, {})
    if not isinstance(body, dict):
        return json_error("body must be a JSON object", 400)
    task_id = str(body.get("task_id") or "").strip()
    if not task_id:
        return json_error("task_id is required", 400)
    plan = body.get("plan")
    try:
        from ouroboros.routing_plan import parse_routing_plan

        parsed = parse_routing_plan(plan if plan is not None else {})
    except ValueError as exc:
        return json_error(str(exc), 400)
    # The proposal only OFFERED reachable destinations, but the door is not the
    # dropdown: a hand-made POST can name anything, and an approval is the last
    # moment a bad destination is still cheap to refuse.
    unreachable = await asyncio.to_thread(_unreachable_rows, parsed)
    if unreachable:
        return json_error("; ".join(unreachable), 409)
    plan_text = json.dumps(parsed.as_dict(), ensure_ascii=False)
    try:
        return JSONResponse(await asyncio.to_thread(_deliver, task_id, plan_text))
    except ValueError as exc:
        return json_error(str(exc), 409)
    except Exception as exc:
        log.exception("api_execution_plan_decision failed")
        return json_error(f"{type(exc).__name__}: the decision could not be delivered")
