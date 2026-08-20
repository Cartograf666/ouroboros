"""Event-only supervising wait for configured session nannies."""

from __future__ import annotations

import json
import pathlib
import time
import uuid
from typing import Any, Callable, Optional

from ouroboros import delegate_custody as custody
from ouroboros.owner_mailbox import KIND_FINALIZE_NOW, KIND_HURRY, drain_owner_entries
from ouroboros.utils import atomic_write_json, utc_now_iso

_TICK_SEC = 3
_QUIET_STATUSES = {"progress", "no_progress"}
_LOOP_CONTROL_KINDS = {KIND_FINALIZE_NOW, KIND_HURRY}


def _state_path(ctx: Any) -> pathlib.Path:
    task_id = str(getattr(ctx, "task_id", "") or "")
    return custody.custody_root(ctx) / "state" / "delegate_supervision" / f"{task_id}.json"


def _load_state(ctx: Any, run_id: str) -> dict[str, Any]:
    path = _state_path(ctx)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or (
        str(run_id) and str(data.get("run_id") or "") != str(run_id)
    ):
        data = {"schema": 1, "run_id": str(run_id), "journal_cursor": 0}
    return data


def _save_state(ctx: Any, state: dict[str, Any]) -> None:
    path = _state_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = utc_now_iso()
    atomic_write_json(path, state)


def _emit(ctx: Any, kind: str, payload: dict[str, Any]) -> bool:
    return custody.emit(custody.custody_root(ctx), kind, {
        "task_id": str(getattr(ctx, "task_id", "") or ""), **payload,
    })


def _payload(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"status": "fault", "detail": raw}
    except (TypeError, ValueError):
        return {"status": "fault", "detail": str(raw or "")}


def _addressed_wakes(ctx: Any, state: dict[str, Any]) -> list[dict[str, Any]]:
    seen = {
        str(item) for item in (state.get("mailbox_acknowledged_ids") or []) if str(item)
    }
    seen.update(
        str(item) for item in (getattr(ctx, "_loop_mailbox_seen_ids", set()) or set())
        if str(item)
    )
    entries = drain_owner_entries(
        pathlib.Path(getattr(ctx, "drive_root", custody.custody_root(ctx))),
        str(getattr(ctx, "task_id", "") or ""),
        seen_ids=seen,
    )
    return [
        {
            "type": "addressed_message",
            "msg_id": str(entry.get("msg_id") or ""),
            "kind": str(entry.get("kind") or "owner_text"),
            "provenance": str(entry.get("provenance") or "owner"),
            "source_task_id": str(entry.get("source_task_id") or ""),
            "relayed_from_task_id": str(entry.get("relayed_from_task_id") or ""),
            "text": str(entry.get("text") or ""),
            "ts": str(entry.get("ts") or ""),
        }
        for entry in entries
    ]


def _interaction_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        identity = str(value.get("interaction_id") or value.get("interactionId") or "")
        if identity:
            found.add(identity)
        for child in value.values():
            found.update(_interaction_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_interaction_ids(child))
    return found


def _wake_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        identity = str(value.get("supervision_wake_id") or "")
        if identity:
            found.add(identity)
        for child in value.values():
            found.update(_wake_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_wake_ids(child))
    return found


def _pending_payload(state: dict[str, Any]) -> dict[str, Any]:
    pending = state.get("pending_wake") if isinstance(state.get("pending_wake"), dict) else {}
    payload = pending.get("payload") if isinstance(pending.get("payload"), dict) else {}
    if pending and not pending.get("acknowledged_at") and payload:
        return dict(payload)
    return {}


def acknowledge_pending_wake(ctx: Any, delivered: Any = None) -> bool:
    """Durably ack one wake only after its full payload entered the transcript."""

    state = _load_state(ctx, "")
    pending = state.get("pending_wake") if isinstance(state.get("pending_wake"), dict) else {}
    if not pending or pending.get("acknowledged_at"):
        return True
    expected = str(pending.get("wake_id") or "")
    if delivered:
        try:
            payload = json.loads(delivered) if isinstance(delivered, str) else delivered
        except (TypeError, ValueError):
            return False
        payload = payload if isinstance(payload, dict) else {}
        if expected and expected not in _wake_ids(payload):
            return False
    from ouroboros.owner_mailbox import acknowledge_task_messages

    mailbox_ids = [str(item) for item in (pending.get("mailbox_ids") or []) if str(item)]
    if mailbox_ids and not acknowledge_task_messages(
        pathlib.Path(getattr(ctx, "drive_root", custody.custody_root(ctx))),
        str(getattr(ctx, "task_id", "") or ""), mailbox_ids, wake_id=expected,
    ):
        return False
    if not _emit(ctx, "delegate_supervision_wake_acknowledged", {
        "run_id": str(state.get("run_id") or ""), "wake_id": expected,
        "mailbox_ids": mailbox_ids,
        "interaction_ids": list(pending.get("interaction_ids") or []),
    }):
        return False
    interaction_ids = frozenset(
        str(item) for item in (pending.get("interaction_ids") or []) if str(item)
    )
    if interaction_ids and str(state.get("run_id") or ""):
        from ouroboros.delegate_interactions import _REPORTED_INTERACTIONS

        run_id = str(state.get("run_id") or "")
        _REPORTED_INTERACTIONS[run_id] = frozenset(
            set(_REPORTED_INTERACTIONS.get(run_id, frozenset())) | set(interaction_ids)
        )
    state["mailbox_acknowledged_ids"] = sorted({
        *(str(item) for item in (state.get("mailbox_acknowledged_ids") or []) if str(item)),
        *mailbox_ids,
    })
    state["interaction_acknowledged_ids"] = sorted({
        *(str(item) for item in (state.get("interaction_acknowledged_ids") or []) if str(item)),
        *interaction_ids,
    })
    pending["acknowledged_at"] = utc_now_iso()
    state["last_acknowledged_wake"] = pending
    state["pending_wake"] = {}
    state["status"] = "awake"
    _save_state(ctx, state)
    return True


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
    replay = _pending_payload(state)
    if replay:
        _emit(ctx, "delegate_supervision_wake_replayed", {
            "run_id": str(run_id),
            "wake_id": str(replay.get("supervision_wake_id") or ""),
        })
        return json.dumps(replay, ensure_ascii=False, indent=2)
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
        _emit(ctx, "delegate_supervision_checkpoint_scheduled", {
            "run_id": str(run_id), "due_at_unix": state["checkpoint"]["due_at_unix"],
            "reason": state["checkpoint"]["reason"],
        })
    _save_state(ctx, state)
    _emit(ctx, "delegate_supervision_wait_entered", {
        "run_id": str(run_id), "journal_cursor": int(state.get("journal_cursor") or 0),
        "checkpoint_scheduled": bool(checkpoint_after_sec is not None),
    })

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
                _emit(ctx, "delegate_supervision_checkpoint_consumed", {
                    "run_id": str(run_id), "reason": str(checkpoint.get("reason") or ""),
                    "consumed_by": str(checkpoint.get("consumed_by") or ""),
                })
            if due and not meaningful and not wakes:
                payload = {
                    "status": "inspection_checkpoint",
                    "run_id": str(run_id),
                    "reason": str(checkpoint.get("reason") or ""),
                    "last_seq": int(state.get("journal_cursor") or 0),
                }
            if wakes:
                payload["wake_events"] = wakes
            wake_id = uuid.uuid4().hex
            payload["supervision_wake_id"] = wake_id
            state["status"] = "wake_pending"
            state["last_wake"] = payload
            state["pending_wake"] = {
                "wake_id": wake_id,
                "payload": payload,
                "mailbox_ids": [
                    str(item.get("msg_id") or "") for item in wakes
                    if str(item.get("msg_id") or "")
                    and str(item.get("kind") or "") not in _LOOP_CONTROL_KINDS
                ],
                "interaction_ids": sorted(_interaction_ids(payload)),
                "created_at": utc_now_iso(),
            }
            _save_state(ctx, state)
            _emit(ctx, "delegate_supervision_wake_pending", {
                "run_id": str(run_id), "wake_id": wake_id,
                "payload": payload,
                "mailbox_ids": list(state["pending_wake"]["mailbox_ids"]),
                "interaction_ids": list(state["pending_wake"]["interaction_ids"]),
            })
            return json.dumps(payload, ensure_ascii=False, indent=2)
        state["quiet_renewals"] = int(state.get("quiet_renewals") or 0) + 1
        renewals = int(state["quiet_renewals"])
        if renewals == 1 or renewals & (renewals - 1) == 0:
            _emit(ctx, "delegate_supervision_wait_renewed", {
                "run_id": str(run_id), "quiet_renewals": renewals,
                "journal_cursor": int(state.get("journal_cursor") or 0),
                "event_only": True,
            })
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
    """Event-only wait; hidden ``wait_sec`` is accepted only for old transcripts."""

    acknowledge_pending_wake(ctx)
    if wait_sec is not None:
        _emit(ctx, "delegate_supervision_legacy_wait_ignored", {
            "run_id": str(run_id), "wait_sec": int(wait_sec),
            "contract": "event_only",
        })

    return supervised_wait(
        ctx, run_id, since_seq=since_seq,
        checkpoint_after_sec=checkpoint_after_sec,
        checkpoint_reason=checkpoint_reason,
    )


__all__ = [
    "acknowledge_pending_wake", "delegate_wait_entry", "supervised_wait",
    "supervision_checkpoint",
]
