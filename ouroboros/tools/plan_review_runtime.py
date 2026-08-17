"""Reviewer-slot execution and envelope rails for ``plan_task``.

The plan-review engine (``tools/plan_review.py``) owns state, packet and verdict;
this module owns the thin runtime seams around it: the deadline rail, the raw
attempt supersession, the configured reviewer rows as ``ReviewSlot`` objects
(both delivery kinds — the D15 api_chat pin is gone), one substrate call, and the
structural helpers (payload exemption, error text) that need a ``ToolContext``.
Transport is NEVER chosen here: every slot carries its route and
``review_execution._review_route_executor`` binds it (plan §8.2).
"""

from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import logging
import pathlib
from typing import Any, Dict, List

from ouroboros.config import review_model_uses_local
from ouroboros.deadline_utils import parse_deadline_ts, utc_now
from ouroboros.llm import LLMClient
from ouroboros.tools.registry import ToolContext, active_repo_dir_for
from ouroboros.utils import utc_now_iso


PLAN_REVIEW_MAX_TOKENS = 65536
PLAN_REVIEW_EFFORT = "high"
PLAN_REVIEW_SLOT_TIMEOUT_SEC = 560
# Per-slot provenance of what the reviewer read (BIBLE P3, retrieving reviewers):
# an api_chat slot read exactly the host-assembled packet; an agent_session slot
# retrieved with its own tools and the host did not observe what it opened.
HOST_FILE_READ_ASSEMBLED = "host_assembled_packet"
HOST_FILE_READ_UNOBSERVED = "unobserved"

log = logging.getLogger(__name__)


def plan_deadline_skip(ctx: ToolContext, *, emit: bool = False) -> str:
    """Project the existing deadline rail without starting a paid reviewer panel."""
    from ouroboros.config import get_plan_task_deadline_min_sec

    metadata = getattr(ctx, "task_metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    deadline = parse_deadline_ts(metadata.get("deadline_at"))
    if deadline is None:
        return ""
    remaining = (deadline - utc_now()).total_seconds()
    scaled = max(0.0, remaining / 4.0)
    minimum = get_plan_task_deadline_min_sec()
    if remaining > 0 and scaled >= minimum:
        return ""
    if emit:
        try:
            event_queue = getattr(ctx, "event_queue", None)
            if event_queue is not None:
                event_queue.put_nowait({
                    "type": "plan_task_deadline_skip",
                    "task_id": str(getattr(ctx, "task_id", "") or ""),
                    "remaining_sec": round(remaining, 1),
                    "scaled_ceiling_sec": round(scaled, 1),
                    "min_useful_sec": minimum,
                    "ts": utc_now_iso(),
                })
        except Exception:
            pass
    cause = (
        "the task deadline has expired; no reviewer work was started."
        if remaining <= 0 else
        f"insufficient time for useful planning — remaining {int(remaining)}s gives a "
        f"review window of {int(scaled)}s (< {int(minimum)}s useful floor)."
    )
    return (
        f"PLAN_TASK_SKIPPED_DEADLINE: {cause} Proceed with your own best plan "
        "directly; do not re-call plan_task under this deadline."
    )


def record_raw_plan_request_attempt(
    envelope: dict, state_root: pathlib.Path, task_id: str, *, reason: str,
) -> str:
    """Supersede prior plan authority before semantic decoding can fail: an invalid
    envelope must not leave an older closed wave standing as the current one."""
    payload = {"domain": "invalid_plan_task_attempt", "envelope": envelope}
    fingerprint = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    from ouroboros.task_results import record_plan_review_attempt

    record_plan_review_attempt(
        state_root, task_id, fingerprint=fingerprint, status="open", reason=reason,
    )
    return fingerprint


def plan_review_slots() -> list:
    """The configured commit-triad rows as plan-review ``ReviewSlot`` objects.

    Both kinds ride: an ``api_chat`` row is one in-process call over the lean
    packet; an ``agent_session`` row is a delegated retrieving reviewer
    (``session_target``/``session_profile`` carried per row). Effort is the row's
    own when configured, else ``PLAN_REVIEW_EFFORT``. Slot ids are the rows' own
    (structured: owner-assigned; legacy: ``slot_N`` from the one mint).
    """
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewSlot
    from ouroboros.reviewer_slot_config import load_reviewer_slot_config

    return [
        ReviewSlot(
            slot_id=row.slot_id,
            model=row.target_id,
            effort=row.effort or PLAN_REVIEW_EFFORT,
            timeout_sec=PLAN_REVIEW_SLOT_TIMEOUT_SEC,
            max_tokens=PLAN_REVIEW_MAX_TOKENS,
            temperature=0.2,
            role_hint="plan reviewer",
            use_local=review_model_uses_local(row.target_id),
            route=ReviewRouteKind.AGENT_SESSION if row.is_session else ReviewRouteKind.API_CHAT,
            session_target=row.session_target,
            session_profile=row.profile_id,
        )
        for row in load_reviewer_slot_config().triad
    ]


def slot_is_session(slot: Any) -> bool:
    from ouroboros.review_execution import ReviewRouteKind

    return getattr(slot, "route", None) is ReviewRouteKind.AGENT_SESSION


async def run_plan_review_slots(
    ctx: ToolContext,
    slots: list,
    *,
    system_prompt: str,
    user_content: str,
    session_task: str = "",
    session_root: str = "",
    output_contract: str = "",
) -> list[dict]:
    """ONE ``ReviewRequest`` fanned across the configured rows through the substrate.

    api_chat rows read ``messages`` (system + user packet); agent_session rows read
    ``session_task``/``session_root``/``policy.output_contract`` and retrieve the
    rest themselves. Returns one raw row per slot, in slot order, carrying the id
    the substrate RAN under, its route, text/error, refs, usage and the
    ``host_file_read_attestation`` fact."""
    from ouroboros.review_substrate import ReviewRequest, run_review_request
    from ouroboros.tools.plan_packet import plan_user_stable_len
    from ouroboros.tools.review_synthesis import build_plan_review_messages

    request = ReviewRequest(
        surface="plan_review",
        goal="Review the proposed plan spec before the work starts.",
        messages=build_plan_review_messages(
            system_prompt, user_content, plan_user_stable_len(user_content),
        ),
        task_id=str(getattr(ctx, "task_id", "") or "plan_review"),
        call_type="plan_review",
        max_tokens=PLAN_REVIEW_MAX_TOKENS,
        temperature=0.2,
        no_proxy=True,
        session_task=session_task,
        session_root=session_root,
        policy={"output_contract": output_contract} if output_contract else {},
    )
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: run_review_request(
            request,
            slots=list(slots),
            drive_root=pathlib.Path(ctx.drive_root),
            llm=LLMClient(),
            usage_ctx=ctx,
        ),
    )
    by_id = {str(slot.slot_id): slot for slot in slots}
    rows = [_plan_row_from_actor(actor, by_id.get(str(actor.get("slot_id") or ""))) for actor in result.actors]
    answered = {row["slot_id"] for row in rows}
    for slot in slots:  # a slot the substrate never answered for is still a configured row
        if str(slot.slot_id) not in answered:
            rows.append(_plan_row_from_actor({"slot_id": slot.slot_id, "model": slot.model,
                                              "status": "error", "error": "no actor record"}, slot))
    return rows


def _plan_row_from_actor(actor: Dict[str, Any], slot: Any) -> dict:
    usage = actor.get("usage") or {}
    text = actor.get("raw_text") or ""
    error = actor.get("error") or ""
    if actor.get("status") not in {"ok", "empty"} and not error:
        error = str(actor.get("status") or "review failed")
    session = slot_is_session(slot) if slot is not None else False
    return {
        # Identity is CARRIED, never re-derived: the row keeps the slot_id the
        # substrate ran, so duplicate-model plan rows stay distinguishable.
        "slot_id": str(actor.get("slot_id") or getattr(slot, "slot_id", "") or ""),
        "model": str(usage.get("resolved_model") or actor.get("model") or getattr(slot, "model", "") or ""),
        "request_model": str(getattr(slot, "model", "") or actor.get("model") or ""),
        "route": "agent_session" if session else "api_chat",
        "host_file_read_attestation": HOST_FILE_READ_UNOBSERVED if session else HOST_FILE_READ_ASSEMBLED,
        "text": text,
        "error": error or None,
        "prompt_ref": actor.get("prompt_ref") or {},
        "response_ref": actor.get("response_ref") or {},
        "tokens_in": usage.get("prompt_tokens", 0),
        "tokens_out": usage.get("completion_tokens", 0),
        "cost": float(usage["cost"]) if usage.get("cost") is not None else None,
    }


def plan_payload_roots(ctx: ToolContext, locators: List[str]) -> list[pathlib.Path]:
    """Skill-payload exemption roots for ``plan_spec.resolve_constitutional``.

    Skill-payload paths live in the DATA plane (``data/skills/<bucket>/…``, not the
    system repo) and never make a plan a self-modification by themselves — the same
    frozen predicate the deleted ``resolve_plan_class`` applied. A recognized locator
    is exempt however it resolves: its data-plane payload root AND its literal
    resolution against the active root are both returned. Classification-only
    (write gates are unrelated code paths); a drive-resolution failure skips the
    exemption, as before."""
    from ouroboros.contracts.skill_payload_policy import resolve_skill_payload_target
    from ouroboros.tool_access import canonical_data_root

    try:
        drive = canonical_data_root(ctx)
        active = pathlib.Path(active_repo_dir_for(ctx)).resolve(strict=False)
    except Exception:
        return []
    roots: list[pathlib.Path] = []
    for raw in locators or []:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            target = resolve_skill_payload_target(drive, text)
        except ValueError:
            continue
        roots.append(target.payload_root)
        try:
            candidate = pathlib.Path(text)
            roots.append((candidate if candidate.is_absolute() else active / candidate).resolve(strict=False))
        except (OSError, ValueError):
            continue
    return roots


def plan_slot_fit(slots: list, *, prompt_chars: int, quorum: int) -> tuple[list, list[dict], str]:
    """``(callable_slots, oversize_rows, error)`` for ONE shared packet fanned across
    mixed-window slots — the review organ's calibrated per-slot input caps
    (`review_synthesis.per_slot_input_token_limits`, Capability Evidence windows) against
    the packet's estimated tokens. An excluded slot is a typed `preflight_oversize` row
    (ok=False, $0) so it is REPORTED as not participating; fewer callable slots than the
    review quorum is a loud typed refusal, never a silent absence of review."""
    from ouroboros.tools.review_synthesis import per_slot_input_token_limits

    # Only api_chat rows are sized: a RETRIEVING (agent_session) row's model id is an opaque
    # harness target, not a provider route (`reviewer_window.reviewer_route(session=True)`), and
    # the review organ's convention (triad: "session rows are not constrained by this pack")
    # is that such a row is never fit-excluded — it retrieves with its own tools.
    api_models = [str(getattr(slot, "model", "") or "") for slot in slots if not slot_is_session(slot)]
    limits = per_slot_input_token_limits(
        api_models, output_reserve=PLAN_REVIEW_MAX_TOKENS, tokenizer_margin=155_000)
    estimated = max(1, (max(0, int(prompt_chars)) + 3) // 4)  # utils.estimate_tokens on the packet
    callable_slots, oversize = [], []
    for slot in slots:
        cap = int(limits.get(str(getattr(slot, "model", "") or ""), 0) or 0)
        if slot_is_session(slot) or estimated <= cap:
            callable_slots.append(slot)
            continue
        oversize.append({
            "slot_id": str(getattr(slot, "slot_id", "") or ""), "model": str(getattr(slot, "model", "") or ""),
            "request_model": str(getattr(slot, "model", "") or ""),
            "route": "agent_session" if slot_is_session(slot) else "api_chat",
            "host_file_read_attestation": None, "text": "",
            "error": (f"preflight_oversize: assembled packet ~{estimated:,} estimated tokens exceeds "
                      f"this slot's calibrated input cap {cap:,}"),
            "prompt_ref": {}, "response_ref": {}, "tokens_in": 0, "tokens_out": 0, "cost": 0.0,
        })
    error = ""
    if slots and len(callable_slots) < int(quorum):
        error = (
            "⚠️ PLAN_REVIEW_DEGRADED_PREFLIGHT_OVERSIZE: the assembled packet "
            f"(~{estimated:,} estimated tokens) exceeds the calibrated input cap of too many reviewer "
            "slots (" + ", ".join(f"{m}<={int(limits.get(m, 0) or 0):,}" for m in api_models)
            + "), so fewer than the review quorum remain callable and NO reviewer was called. "
            "A constitutional plan carries BIBLE.md and ARCHITECTURE.md in full (W3): configure "
            "reviewer slots with a larger context window, or shrink the declared evidence."
        )
    return callable_slots, oversize, error
