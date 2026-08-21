"""Thin Claudexor v3 thread operations for delegated plan reviewers."""

from __future__ import annotations

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
    if getattr(route, "profile_id", ""):
        request["credentialProfileId"] = route.profile_id
    return str(gateway.create_thread(request, idempotency_key=key).get("id") or "")


def start_review_thread_turn(
    gateway: Any, thread_id: str, run_request: Dict[str, Any], *, idempotency_key: str,
) -> Dict[str, Any]:
    """Strip run-only fields and append through the public thread pipeline."""
    request = {
        key: value for key, value in run_request.items()
        if key not in {
            "_use_thread", "_thread_id", "scope", "execution", "credentialProfileId",
        }
    }
    return gateway.start_thread_turn(thread_id, request, idempotency_key=idempotency_key)


def review_thread_receipt(gateway: Any, thread_id: str, run_id: str, turn_id: str) -> dict:
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
