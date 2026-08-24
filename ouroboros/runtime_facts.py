"""What the runtime says about ITSELF: which model serves this task, and what
delegation has actually been observed to do.

Both are facts the runtime section states about its own execution, both are
read-only and fail-soft, and both are consumed by exactly one caller
(``context.build_runtime_section``). They live here rather than in ``context``
because ``context`` composes the prompt — it should not also be the authority on
what the runtime is. Each answers a question the agent would otherwise have to
GUESS at, which is the one thing P6 forbids it to do about itself.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

log = logging.getLogger("ouroboros.runtime_facts")


def _runtime_model_route(ctx: Any) -> Dict[str, Any]:
    """The route this task runs on, so the agent can answer truthfully about itself.

    Nothing else in the prompt names the model. Asked "which model are you", the
    agent had to GUESS, and it guessed from the nearest number in reach: a live
    reply reported "gpt-5.2" because that is the default of the `model` parameter
    on the `web_search` tool schema, while the request was actually served by
    gemini-3.1-flash-lite. Fabricating a fact about itself is exactly what P6
    forbids, and it was unavoidable while the fact was absent.

    This is the route the task STARTS on. A cross-model fallback or an in-task
    `switch_model` can move it, so the note says so rather than presenting a
    per-round receipt the caller cannot honour.
    """
    override = str(getattr(ctx, "task_model_override", "") or "").strip()
    try:
        from ouroboros.config import _main_model

        configured = _main_model()
    except Exception:
        configured = ""
    model = override or configured
    if override:
        use_local = bool(getattr(ctx, "task_use_local_override", False))
    else:
        use_local = str(os.environ.get("USE_LOCAL_MAIN", "") or "").strip().lower() in {"1", "true", "yes", "on"}
    return {
        "model": model or "unknown",
        "is_local": use_local,
        "source": "task_override" if override else "configured_main_slot",
        "rule": (
            "This is the model serving this task; state it when asked instead of "
            "inferring one from tool schemas or prompt text. A fallback or "
            "switch_model may change it mid-task."
        ),
    }


def _delegation_capability_fact() -> Optional[Dict[str, Any]]:
    """B4-lite: honestly-labeled HISTORICAL delegation observations.

    Deliberately NOT live health — receipts prove what the last execution did,
    not what a lane can do now; live lane facts arrive from plan-review wave
    rows and typed delegate refusals. Pure bounded file reads over the existing
    receipt projections: no daemon probes, no new health authority. Absent
    receipt files mean absent observations, never "healthy". Fail-soft on its
    own (None on any failure) so a problem here never drops the surrounding
    capabilities digest.
    """
    try:
        from ouroboros.reviewer_slot_config import reviewer_slot_last_executions
        from ouroboros.subagents import subagent_last_delegation

        def _observed_label(ts: Any) -> str:
            # Timestamp only: the verbatim "historical, not live health" disclaimer
            # lives ONCE in the note below, never repeated per row.
            return f"last observed at {str(ts or '').strip() or 'unknown time'}"

        delegation: Dict[str, Any] = {
            "note": (
                "Every row here is historical, not live health (the last "
                "recorded execution per reviewer slot / delegated run): "
                "live lane facts arrive from plan-review wave rows and typed "
                "delegate refusals. A missing row means no observation on "
                "record — never healthy."
            ),
        }
        slot_rows: List[Dict[str, Any]] = []
        for slot_id, row in sorted(reviewer_slot_last_executions().items()):
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").strip()
            fact: Dict[str, Any] = {
                "slot": str(slot_id),
                "outcome": (("ok" if status == "ok" else "failed") if status
                            else "unknown"),
                "observed": _observed_label(row.get("ts")),
            }
            requested = row.get("requested") if isinstance(row.get("requested"), dict) else {}
            effective = row.get("effective") if isinstance(row.get("effective"), dict) else {}
            if requested.get("profile_id"):
                fact["requested_profile"] = str(requested["profile_id"])
            if effective.get("profile_id"):
                fact["applied_profile"] = str(effective["profile_id"])
            # B1's typed failure facts, forwarded only when recorded (a dated
            # window carries reset_at without a code and an undated one the
            # code without a reset — read both independently).
            for key in ("failure_code", "reset_at"):
                if row.get(key):
                    fact[key] = row[key]
            slot_rows.append(fact)
        if slot_rows:
            delegation["reviewer_slots_last"] = slot_rows
        last = subagent_last_delegation()
        if isinstance(last, dict) and last:
            last_fact = {
                "route": str(last.get("route") or ""),
                "requested_model": str(last.get("requested_model") or ""),
                "applied_model": str(last.get("applied_model") or ""),
                "observed": _observed_label(last.get("ts")),
            }
            if last.get("requested_profile"):
                last_fact["requested_profile"] = str(last["requested_profile"])
            if last.get("applied_profile"):
                last_fact["applied_profile"] = str(last["applied_profile"])
            if last.get("selected_subagent_id"):
                last_fact["selected_subagent_id"] = str(last["selected_subagent_id"])
            delegation["subagent_last_delegation"] = last_fact
        if len(delegation) == 1:
            return None
        return delegation
    except Exception:
        log.debug("Failed to build delegation capability fact", exc_info=True)
        return None
