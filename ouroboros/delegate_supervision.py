"""Event-only supervising wait for configured session nannies."""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any, Callable, Optional

from ouroboros import delegate_custody as custody
from ouroboros.owner_mailbox import drain_owner_entries
from ouroboros.utils import atomic_write_json, utc_now_iso

_TICK_SEC = 3
_QUIET_STATUSES = {"progress", "no_progress"}


def _state_path(ctx: Any) -> pathlib.Path:
    task_id = str(getattr(ctx, "task_id", "") or "")
    return custody.custody_root(ctx) / "state" / "delegate_supervision" / f"{task_id}.json"


def _load_state(ctx: Any, run_id: str) -> dict[str, Any]:
    path = _state_path(ctx)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or str(data.get("run_id") or "") != str(run_id):
        data = {"schema": 1, "run_id": str(run_id), "journal_cursor": 0}
    return data


def _save_state(ctx: Any, state: dict[str, Any]) -> None:
    path = _state_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = utc_now_iso()
    atomic_write_json(path, state)


def _payload(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"status": "fault", "detail": raw}
    except (TypeError, ValueError):
        return {"status": "fault", "detail": str(raw or "")}


def _addressed_wakes(ctx: Any, state: dict[str, Any]) -> list[dict[str, Any]]:
    seen = {str(item) for item in (state.get("mailbox_seen_ids") or []) if str(item)}
    entries = drain_owner_entries(
        pathlib.Path(getattr(ctx, "drive_root", custody.custody_root(ctx))),
        str(getattr(ctx, "task_id", "") or ""),
        seen_ids=seen,
    )
    state["mailbox_seen_ids"] = sorted(seen)
    return [
        {
            "type": "addressed_message",
            "msg_id": str(entry.get("msg_id") or ""),
            "kind": str(entry.get("kind") or "owner_text"),
            "provenance": str(entry.get("provenance") or "owner"),
            "source_task_id": str(entry.get("source_task_id") or ""),
        }
        for entry in entries
    ]


def _control_wakes(ctx: Any) -> list[dict[str, Any]]:
    wakes: list[dict[str, Any]] = []
    task_id = str(getattr(ctx, "task_id", "") or "")
    try:
        from ouroboros.cancel_intents import cancel_pending

        if cancel_pending(custody.custody_root(ctx), task_id):
            wakes.append({"type": "cancellation_intent"})
    except Exception:
        pass
    try:
        from ouroboros.deadline_utils import deadline_remaining_sec, parse_deadline_ts

        metadata = getattr(ctx, "task_metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        if parse_deadline_ts(metadata.get("deadline_at")) is not None and deadline_remaining_sec(ctx) <= 0:
            wakes.append({"type": "deadline"})
    except Exception:
        pass
    return wakes


def supervised_wait(
    ctx: Any,
    run_id: str,
    *,
    since_seq: Optional[int] = None,
    checkpoint_after_sec: Optional[int] = None,
    checkpoint_reason: str = "",
    wait_once: Optional[Callable[..., str]] = None,
) -> str:
    """Renew quiet windows internally and return only a meaningful wake batch."""

    if (checkpoint_after_sec is None) != (not str(checkpoint_reason or "").strip()):
        return json.dumps({
            "status": "refused",
            "reason": "checkpoint_requires_time_and_reason",
            "detail": "checkpoint_after_sec and non-empty checkpoint_reason must be supplied together.",
        }, ensure_ascii=False, indent=2)
    if wait_once is None:
        from ouroboros.tools.delegate import _delegate_wait

        wait_once = _delegate_wait
    state = _load_state(ctx, run_id)
    snapshot = getattr(ctx, "task_metadata", {})
    snapshot = snapshot.get("configured_subagent") if isinstance(snapshot, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    state["config_fingerprint"] = str(snapshot.get("config_fingerprint") or "")
    state["status"] = "sleeping"
    if since_seq is not None:
        state["journal_cursor"] = max(int(state.get("journal_cursor") or 0), int(since_seq))
    if checkpoint_after_sec is not None:
        delay = max(1, min(604_800, int(checkpoint_after_sec)))
        state["checkpoint"] = {
            "due_at_unix": time.time() + delay,
            "reason": str(checkpoint_reason).strip(),
            "requested_at": utc_now_iso(),
            "consumed": False,
        }
    _save_state(ctx, state)

    while True:
        raw = wait_once(
            ctx,
            run_id,
            _TICK_SEC,
            int(state.get("journal_cursor") or 0),
        )
        payload = _payload(raw)
        cursor = payload.get("last_seq")
        if isinstance(cursor, int):
            state["journal_cursor"] = max(int(state.get("journal_cursor") or 0), cursor)
        wakes = _addressed_wakes(ctx, state) + _control_wakes(ctx)
        checkpoint = state.get("checkpoint") if isinstance(state.get("checkpoint"), dict) else {}
        due = bool(
            checkpoint
            and not checkpoint.get("consumed")
            and float(checkpoint.get("due_at_unix") or 0) <= time.time()
        )
        meaningful = str(payload.get("status") or "") not in _QUIET_STATUSES
        if meaningful or wakes or due:
            if checkpoint and not checkpoint.get("consumed"):
                checkpoint["consumed"] = True
                checkpoint["consumed_at"] = utc_now_iso()
                checkpoint["consumed_by"] = (
                    "real_event" if meaningful or wakes else "scheduled_checkpoint"
                )
                state["checkpoint"] = checkpoint
            if due and not meaningful and not wakes:
                payload = {
                    "status": "inspection_checkpoint",
                    "run_id": str(run_id),
                    "reason": str(checkpoint.get("reason") or ""),
                    "last_seq": int(state.get("journal_cursor") or 0),
                }
            if wakes:
                payload["wake_events"] = wakes
            state["status"] = "wake_pending"
            state["last_wake"] = payload
            _save_state(ctx, state)
            return json.dumps(payload, ensure_ascii=False, indent=2)
        _save_state(ctx, state)


def supervision_checkpoint(ctx: Any) -> dict[str, Any]:
    """Read the durable checkpoint used by selective recovery/restart."""

    try:
        data = json.loads(_state_path(ctx).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def delegate_wait_entry(
    ctx: Any,
    run_id: str,
    wait_sec: Optional[int] = None,
    since_seq: Optional[int] = None,
    checkpoint_after_sec: Optional[int] = None,
    checkpoint_reason: str = "",
) -> str:
    """Wire-compatible entry; ``wait_sec`` is no longer a model-wake cadence."""

    return supervised_wait(
        ctx, run_id, since_seq=since_seq,
        checkpoint_after_sec=checkpoint_after_sec,
        checkpoint_reason=checkpoint_reason,
    )


__all__ = ["delegate_wait_entry", "supervised_wait", "supervision_checkpoint"]
