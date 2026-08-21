"""Thin Claudexor v3 thread operations for delegated plan reviewers."""

from __future__ import annotations

import json
from typing import Any, Dict


def ensure_review_thread(
    gateway: Any, custody: Any, thread_id: str, *, route: Any, root: str,
    surface: str, slot_id: str, task_id: str,
) -> str:
    """Create the initial sticky read-only thread; otherwise retain its id."""
    if thread_id:
        return thread_id
    key = custody.idempotency_key(
        "review_thread", surface, slot_id, task_id, route.route_id,
        str(getattr(route, "profile_id", "") or ""), root,
    )
    request: Dict[str, Any] = {
        "title": f"Ouroboros {surface} · {slot_id}",
        "scope": {"kind": "project", "root": root},
        "mode": "ask", "workspace": "in_place", "authPreference": "subscription",
        "primaryHarness": route.route_id, "eligibleHarnesses": [route.route_id],
        "access": "readonly",
    }
    # A thread turn must never fall back to the daemon's sticky/pool precedence
    # by omission. ``None`` is the engine's typed default-subject pin; a string
    # is the exact expected profile.
    request["credentialProfileId"] = getattr(route, "profile_id", "") or None
    return str(gateway.create_thread(request, idempotency_key=key).get("id") or "")


def start_review_thread_turn(
    gateway: Any, thread_id: str, run_request: Dict[str, Any], *, idempotency_key: str,
) -> Dict[str, Any]:
    """Strip run-only fields and append through the public thread pipeline."""
    request = {
        key: value for key, value in run_request.items()
        if key not in {"_use_thread", "_thread_id", "scope", "execution"}
    }
    return gateway.start_thread_turn(thread_id, request, idempotency_key=idempotency_key)


def profile_rotation_receipts(gateway: Any, run_id: str) -> list[dict]:
    """Read the engine's typed profile-rotation events for one settled run."""
    try:
        raw = gateway.get_run_artifact(run_id, "events.jsonl").decode("utf-8")
    except (OSError, UnicodeError, ValueError, RuntimeError):
        return []
    receipts = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(row, dict) or row.get("type") != "route.profile.rotated":
            continue
        payload = (
            row.get("payload") if isinstance(row.get("payload"), dict)
            else row.get("data") if isinstance(row.get("data"), dict)
            else row
        )
        receipts.append({
            "type": "route.profile.rotated",
            "from_profile_id": payload.get("from_profile_id"),
            "to_profile_id": payload.get("to_profile_id"),
            "reason": str(payload.get("reason") or ""),
            "attempt_id": str(payload.get("attempt_id") or ""),
            "resets_at": str(payload.get("resets_at") or ""),
        })
    return receipts


def profile_continuity_receipt(
    expected_profile: str, applied_profile: str, rotations: list[dict],
) -> dict:
    """Bind expected and applied profiles, accepting only an engine rotation receipt."""
    expected, applied = str(expected_profile or ""), str(applied_profile or "")
    matched = next((
        dict(row) for row in rotations if isinstance(row, dict)
        and row.get("type") == "route.profile.rotated"
        and str(row.get("from_profile_id") or "") == expected
        and str(row.get("to_profile_id") or "") == applied
        and str(row.get("reason") or "")
    ), None)
    status = "matched" if expected == applied else ("typed_rotation" if matched else "cannot_verify")
    return {
        "expected_profile": expected,
        "applied_profile": applied,
        "status": status,
        "rotation_receipt": matched or {},
    }


def review_thread_receipt(
    gateway: Any, thread_id: str, run_id: str, turn_id: str, *,
    expected_profile: str = "", applied_profile: str = "",
) -> dict:
    """Require the completed run's exact turn/session/continuity binding."""
    detail = gateway.get_thread(thread_id)
    turns = [row for row in (detail.get("turns") or []) if isinstance(row, dict)]
    turn = next(
        (row for row in reversed(turns)
         if str(row.get("runId") or "") == run_id
         or (turn_id and str(row.get("id") or "") == turn_id)),
        None,
    )
    if turn is None:
        from ouroboros.review_execution import ReviewRouteUnavailable

        raise ReviewRouteUnavailable(
            f"Claudexor thread {thread_id} does not bind completed run {run_id}",
            code="review_thread_receipt_missing",
        )
    turn_id = str(turn.get("id") or turn_id)
    return {
        "thread_id": thread_id, "turn_id": turn_id, "run_id": run_id,
        "head_run_id": str((detail.get("thread") or {}).get("headRunId") or ""),
        "continuity": turn.get("continuity") or {},
        "profile_continuity": profile_continuity_receipt(
            expected_profile, applied_profile, profile_rotation_receipts(gateway, run_id),
        ),
        "sessions": [
            {
                "id": str(row.get("id") or ""),
                "harness_id": str(row.get("harnessId") or ""),
                "profile_id": str(row.get("profileId") or ""),
                "state": str(row.get("state") or ""),
            }
            for row in (detail.get("sessions") or []) if isinstance(row, dict)
        ],
    }
