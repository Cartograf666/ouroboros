"""``schedule_followup`` — one-shot deferred follow-up through the EXISTING scheduler.

The W=A wait affordance (rotation sprint, owner-approved): when waiting for an
external instant (a subscription window reset, an embargo, a slow dependency) beats
burning rounds, the agent registers ONE deferred follow-up in the supervisor's
scheduled-task table (``state/scheduled_tasks.json``) with a ``{"type": "once",
"run_at": <ISO>}`` trigger. The supervisor's ordinary scheduler tick fires it once
at/after ``run_at`` as an ordinary queued ROOT task (normal admission, normal
budget), then marks the record done. No second scheduler exists: this module only
WRITES the table the supervisor already consumes.

Authority is narrower than the parent's, not wider: a delegated subagent may not
mint future root tasks (typed refusal), the objective is the agent's own plain
text (no host template), and a task may hold at most ``_MAX_PENDING_FOLLOWUPS``
pending follow-ups — past the cap the refusal is typed and discloses the pending
records.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

from ouroboros.deadline_utils import parse_deadline_ts
from ouroboros.tools.registry import ToolContext, ToolEntry

_MAX_PENDING_FOLLOWUPS = 2
FOLLOWUP_SOURCE = "task_followup"
_MAX_OBJECTIVE_CHARS = 4_000
_MAX_CONTEXT_CHARS = 8_000


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="schedule_followup",
            schema={
                "name": "schedule_followup",
                "description": (
                    "Register ONE one-shot deferred follow-up task that the supervisor "
                    "scheduler enqueues as an ordinary root task at/after run_at (ISO 8601; "
                    "naive times read as UTC), instead of waiting in-task or re-polling. "
                    "Use it when an external instant (e.g. a reviewer-lane quota reset) is "
                    "the honest unblock time. Write the objective in your own words — it "
                    "becomes the future task's text verbatim. The record is durable "
                    "(state/scheduled_tasks.json), fires exactly once, and the owner can "
                    "disable or delete it from the Schedules surface. A task may hold at "
                    f"most {_MAX_PENDING_FOLLOWUPS} pending follow-ups."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_at": {
                            "type": "string",
                            "description": "ISO 8601 instant to fire at/after (naive = UTC).",
                        },
                        "objective": {
                            "type": "string",
                            "description": (
                                "Plain-language objective for the future task, in your own words "
                                f"(max {_MAX_OBJECTIVE_CHARS} chars; longer is a typed refusal, never truncated)."
                            ),
                        },
                        "context": {
                            "type": "string",
                            "description": (
                                "Optional context the future task should start from (facts, ids, paths; "
                                f"max {_MAX_CONTEXT_CHARS} chars; longer is a typed refusal, never truncated)."
                            ),
                        },
                    },
                    "required": ["run_at", "objective"],
                },
            },
            handler=_handle_schedule_followup,
            timeout_sec=30,
        )
    ]


def _is_delegated_subagent(ctx: ToolContext) -> bool:
    for attr in ("task_metadata", "task_contract"):
        data = getattr(ctx, attr, None)
        if isinstance(data, dict) and str(data.get("delegation_role") or "").strip() == "subagent":
            return True
    return False


def _pending_followups(records: List[Dict[str, Any]], task_id: str) -> List[Dict[str, Any]]:
    out = []
    for record in records:
        if not isinstance(record, dict) or not record.get("enabled", True):
            continue
        if str(record.get("source") or "") != FOLLOWUP_SOURCE:
            continue
        template = record.get("task") if isinstance(record.get("task"), dict) else {}
        metadata = template.get("metadata") if isinstance(template.get("metadata"), dict) else {}
        if str(metadata.get("origin_task_id") or "") == task_id:
            out.append(record)
    return out


def _handle_schedule_followup(ctx: ToolContext, **params) -> str:
    if _is_delegated_subagent(ctx):
        return (
            "ERROR: FOLLOWUP_SUBAGENT_REFUSED: a delegated subagent holds narrower-than-parent "
            "authority and may not mint future root tasks. Report the wait instant to your "
            "parent instead; the parent (or the owner) decides whether to schedule a follow-up."
        )
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    if not task_id:
        return "ERROR: FOLLOWUP_TASK_ID_REQUIRED: a durable follow-up must belong to a real task."
    run_at_raw = str(params.get("run_at") or "").strip()
    instant = parse_deadline_ts(run_at_raw)
    if instant is None:
        return (
            f"ERROR: FOLLOWUP_RUN_AT_INVALID: {run_at_raw!r} is not a parseable ISO 8601 "
            "instant. Example: 2026-08-19T12:20:00+03:00 (naive times read as UTC)."
        )
    objective = str(params.get("objective") or "").strip()
    if not objective:
        return "ERROR: FOLLOWUP_OBJECTIVE_REQUIRED: write the future task's objective in plain language."
    # Typed refusal, never a silent cut: the text rides VERBATIM into the future
    # task, so truncating it here would silently change what that task is.
    if len(objective) > _MAX_OBJECTIVE_CHARS:
        return (
            f"ERROR: FOLLOWUP_TEXT_TOO_LONG: objective is {len(objective)} chars; the limit is "
            f"{_MAX_OBJECTIVE_CHARS}. Shorten it — nothing was truncated and nothing was scheduled."
        )
    context = str(params.get("context") or "").strip()
    if len(context) > _MAX_CONTEXT_CHARS:
        return (
            f"ERROR: FOLLOWUP_TEXT_TOO_LONG: context is {len(context)} chars; the limit is "
            f"{_MAX_CONTEXT_CHARS}. Shorten it — nothing was truncated and nothing was scheduled."
        )
    from ouroboros.tool_access import canonical_data_root
    from supervisor.queue import list_scheduled_tasks, upsert_scheduled_task

    try:
        drive_root = canonical_data_root(ctx)
    except Exception as exc:
        return f"ERROR: FOLLOWUP_DATA_ROOT_UNRESOLVED: {exc}"
    records = [r for r in (list_scheduled_tasks(drive_root).get("tasks") or []) if isinstance(r, dict)]
    pending = _pending_followups(records, task_id)
    if len(pending) >= _MAX_PENDING_FOLLOWUPS:
        listing = "; ".join(
            f"{r.get('id')} (fires at/after { (r.get('trigger') or {}).get('run_at') })" for r in pending
        )
        return (
            f"ERROR: FOLLOWUP_CAP_REACHED: this task already holds {len(pending)} pending "
            f"follow-up(s) of the {_MAX_PENDING_FOLLOWUPS} allowed: {listing}. Each fires once; "
            "wait for one to fire, or the owner can disable/delete records from the Schedules surface."
        )
    run_at_iso = instant.isoformat()
    metadata_src = getattr(ctx, "task_metadata", None)
    root_task_id = metadata_src.get("root_task_id") if isinstance(metadata_src, dict) else None
    record = {
        "id": f"followup-{task_id}-{uuid.uuid4().hex[:6]}",
        "name": f"Follow-up of task {task_id}",
        "description": objective,
        "source": FOLLOWUP_SOURCE,
        "enabled": True,
        # Blank timezone: run_at is stored normalized to UTC, so the local-zone
        # fallback in `timezone_for_schedule` cannot move the instant.
        "trigger": {"type": "once", "run_at": run_at_iso},
        "task": {
            "type": "task",
            "text": objective,
            "description": objective,
            **({"context": context} if context else {}),
            "metadata": {
                "source": FOLLOWUP_SOURCE,
                "origin_task_id": task_id,
                # `or ""` before str(): an absent/None root_task_id must fall back
                # to task_id, never become the literal string "None".
                "origin_root_task_id": str(root_task_id or "") or task_id,
            },
        },
    }
    try:
        stored = upsert_scheduled_task(record, drive_root=drive_root)
    except Exception as exc:
        return f"ERROR: FOLLOWUP_PERSIST_FAILED: {type(exc).__name__}: {exc}"
    return (
        f"FOLLOWUP_SCHEDULED: one-shot follow-up {stored.get('id')} registered to fire at/after "
        f"{run_at_iso} (next scheduler tick at/after that instant). It will enqueue an ordinary "
        f"root task through the supervisor scheduler under normal admission; pending follow-ups "
        f"for this task: {len(pending) + 1}/{_MAX_PENDING_FOLLOWUPS}. The record is durable in "
        "state/scheduled_tasks.json and fires exactly once; the owner can disable or delete it "
        "from the Schedules surface."
    )
