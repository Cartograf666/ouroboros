"""Pre-first-model bootstrap for configured session nannies."""

from __future__ import annotations

import json
from typing import Any, Mapping

from ouroboros.subagent_work_order import compile_external_work_order


def bootstrap_before_context(ctx: Any, task: Mapping[str, Any], dispatch: Any) -> str:
    """Mark exact routing and atomically bootstrap before context/model work."""

    configured = isinstance(task.get("configured_subagent"), dict)
    ctx.exact_model_route = configured
    if not configured or dispatch is None or bool(getattr(dispatch, "blocked", False)):
        return ""
    return bootstrap_session_leaf(ctx, task, dispatch)


def append_startup_receipt(messages: list[dict[str, Any]], startup_wake: str) -> None:
    if not startup_wake:
        return
    messages.append({
        "role": "user",
        "content": (
            "[CONFIGURED SESSION STARTUP / WAKE RECEIPT]\n" + startup_wake
            + "\nThe external start already happened atomically. Judge this wake; do not "
            "repeat the start or perform the substantive assignment natively as fallback."
        ),
    })


def bootstrap_session_leaf(ctx: Any, task: Mapping[str, Any], dispatch: Any) -> str:
    """Start exact external custody, then sleep until the first meaningful wake."""

    snapshot = task.get("configured_subagent") if isinstance(task.get("configured_subagent"), dict) else {}
    route = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else {}
    if str(route.get("kind") or "") != "agent_session":
        return ""
    if dispatch is None or str(getattr(dispatch, "executor", "") or "") != "harness":
        return ""
    from ouroboros.delegate_recovery import adopt_handoff

    adoption = adopt_handoff(ctx, task)
    if adoption.get("status") == "recovery_required":
        return json.dumps({
            "status": "configured_session_recovery_wake",
            "recovery": adoption,
        }, ensure_ascii=False, indent=2)
    if adoption.get("status") == "adopted":
        run_id = str(adoption.get("run_id") or "")
        from ouroboros.delegate_supervision import supervised_wait

        wake_raw = supervised_wait(ctx, run_id)
        try:
            wake = json.loads(wake_raw)
        except (TypeError, ValueError):
            wake = {"status": "wake_fault", "detail": wake_raw}
        return json.dumps({
            "status": "configured_session_recovered_wake",
            "recovery": adoption,
            "wake": wake,
        }, ensure_ascii=False, indent=2)
    from ouroboros.tools.delegate import exact_start

    work_order = compile_external_work_order(task)
    started_raw = exact_start(ctx, work_order, {"snapshot": snapshot})
    try:
        started = json.loads(started_raw)
    except (TypeError, ValueError):
        return json.dumps({
            "status": "startup_fault",
            "reason": "unparseable_exact_start_result",
            "detail": str(started_raw or ""),
        }, ensure_ascii=False, indent=2)
    if not isinstance(started, dict):
        started = {"status": "startup_fault", "detail": str(started)}
    # A POST with no durable STARTED row is intentionally NOT treated as healthy
    # custody and does not enter quiet sleep. The nanny gets the exact evidence now.
    if str(started.get("status") or "") != "started":
        return json.dumps({
            "status": "configured_session_start_wake",
            "startup": started,
        }, ensure_ascii=False, indent=2)
    run_id = str(started.get("run_id") or "")
    if not run_id:
        return json.dumps({
            "status": "configured_session_start_wake",
            "startup": started,
            "reason": "started_without_run_id",
        }, ensure_ascii=False, indent=2)
    from ouroboros.delegate_supervision import supervised_wait

    wake_raw = supervised_wait(ctx, run_id)
    try:
        wake = json.loads(wake_raw)
    except (TypeError, ValueError):
        wake = {"status": "wake_fault", "detail": wake_raw}
    return json.dumps({
        "status": "configured_session_wake",
        "startup": started,
        "wake": wake,
    }, ensure_ascii=False, indent=2)


__all__ = ["append_startup_receipt", "bootstrap_before_context", "bootstrap_session_leaf"]
