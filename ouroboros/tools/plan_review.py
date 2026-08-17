"""``plan_task`` — the plan-review engine: ONE review organ pointed at an INTENTION.

Owner-approved redesign (2026-08-15, plan §6/§7): the agent submits a SPEC (goal,
in_scope, non_goals, acceptance_claims, invariants, decisions, deferred,
affected_resources, evidence) plus prose; the host normalizes it (``plan_spec``),
resolves ONE structural fact (``constitutional``), attaches the declared evidence
bounded with every omission named, builds the lean packet (``plan_packet``), fans it
across the configured reviewer rows through the shared review substrate (api_chat
in-process packet OR agent_session retrieving reviewer — the transport is bound by
``review_execution._review_route_executor``, never here), validates each slot's typed
findings, computes the aggregate and records the wave in ``plan_review_state`` v2.

Cycles: ``review_cycles.review_max_cycles()`` bounds PAID panels per task
(``cycles_paid``); an identical fingerprint replays the recorded wave (no panel,
no cycle); a DEGRADED wave (no parseable quorum) pays nothing and is re-run by
re-calling. Closure (``plan_spec.closure_after_disposition``): GREEN closes;
REVIEW_REQUIRED closes by disposition at $0; REVISE_PLAN never closes by
disposition — accept ⇒ changed spec (next paid cycle), reject ⇒ rationale rides
into the next delta cycle. Under blocking enforcement an open wave HOLDS
finalization (``owner_hurry.force_plan_decision``); at the cap the typed
``plan_review_cycles_exhausted`` result + event leave the honest exits: owner
unstick or a ``blocked_with_evidence`` terminal. Advisory proceeds open under the
host's loud disclosure. Domain-neutral: a spec with zero paths is first-class.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from hashlib import sha256
import inspect
import json
import logging
import pathlib
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ouroboros.config import adaptive_quorum, get_review_enforcement
from ouroboros.review_cycles import (
    emit_review_cycles_exhausted,
    review_max_cycles,
)
from ouroboros.task_results import (
    load_plan_review_state,
    load_task_result,
    mark_current_plan_review_unavailable,
    mark_plan_review_cycles_exhausted,
    plan_review_wave,
    current_plan_review_wave,
    record_plan_review_attempt,
    record_plan_review_dispositions,
    record_plan_review_wave,
)
from ouroboros.tools import plan_evidence, plan_spec
from ouroboros.tools.plan_render import _next_step, _quote_control_lines, _render_wave  # noqa: F401 — engine renderers
from ouroboros.tools.plan_packet import (
    build_plan_review_system_prompt,
    build_plan_review_user_content,
)
from ouroboros.tools.plan_review_runtime import (
    PLAN_REVIEW_MAX_TOKENS as _PLAN_REVIEW_MAX_TOKENS,
    PLAN_REVIEW_SLOT_TIMEOUT_SEC as _PLAN_REVIEW_SLOT_TIMEOUT_SEC,
    plan_deadline_skip as _plan_deadline_skip,
    plan_payload_roots as _plan_payload_roots,
    plan_review_slots as _plan_review_slots,
    plan_slot_fit as _plan_slot_fit,
    record_raw_plan_request_attempt as _record_raw_plan_request_attempt,
    run_plan_review_slots as _run_plan_review_slots,
)
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.review_helpers import (
    load_checklist_section,
    load_governance_doc,
    review_wave_budget_gate,
)
from ouroboros.tools.review_synthesis import (
    PLAN_REVIEW_CONTROL_PREFIX,
)
from ouroboros.utils import truncate_review_artifact, utc_now_iso

log = logging.getLogger(__name__)

_PLAN_REVIEW_WRAPPER_TIMEOUT_SEC = _PLAN_REVIEW_SLOT_TIMEOUT_SEC + 60
_PLAN_TASK_TOOL_TIMEOUT_SEC = _PLAN_REVIEW_WRAPPER_TIMEOUT_SEC + 10
# Root exploration log (plan F3/S8): the task's OWN tool calls before this call,
# read from the task-local conversation — never a scan of tools.jsonl.
_EXPLORATION_TAIL_CALLS = 40
_EXPLORATION_ARGS_CHARS = 240
_EXPLORATION_RESULT_CHARS = 320
_TASK_EVIDENCE_RESULT_CHARS = 6_000
_RAW_TEXT_PREVIEW_CHARS = 2_000


@dataclass(frozen=True)
class _PlanRequest:
    goal: str
    plan: str
    spec: Any


_SPEC_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "description": (
        "The object under review — what the work will do, how it will be checked, what "
        "is deliberately deferred. Ids are minted by the host (claim_N, invariant_N, "
        "decision_N, deferred_N) and are the only targets a blocking finding may name."
    ),
    "properties": {
        "in_scope": {"type": "array", "items": {"type": "string"}},
        "non_goals": {"type": "array", "items": {"type": "string"}},
        "acceptance_claims": {
            # object form feeds acceptance's support_expected; a bare string is fine too
            "type": "array",
            "items": {"anyOf": [
                {"type": "string"},
                {"type": "object", "properties": {
                    "claim": {"type": "string"}, "surface": {"type": "string"},
                    "support": {"type": "string"},
                    "priority": {"type": "string", "enum": ["must", "should"]},
                }, "required": ["claim"], "additionalProperties": False},
            ]},
            "description": (
                "Concrete, checkable statements of what 'done' means — strings or "
                "{claim, surface, support, priority}. The CLOSED plan's claims bind task "
                "acceptance (host-minted ids claim_1..N in list order — link "
                "verify_and_record receipts via criterion_id)."
            ),
        },
        "invariants": {
            "type": "array", "items": {"type": "string"},
            "description": "Constraints that must hold: budget, deadline, safety, irreversibility, external commitments.",
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "choice": {"type": "string"},
                    "rejected": {"type": "array", "items": {"type": "string"}},
                    "why": {"type": "string"},
                },
                "required": ["choice"],
            },
            "description": "Load-bearing decisions that would be expensive to reverse, each with rejected alternatives and why.",
        },
        "deferred": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"what": {"type": "string"}, "why_safe_to_defer": {"type": "string"}},
                "required": ["what"],
            },
            "description": "Deliberately deferred until the work is underway; ids deferred_1..N — a blocking finding may name any spec id it breaks: goal, claim_N, invariant_N, decision_N or deferred_N itself.",
        },
        "affected_resources": {
            "type": "array", "items": {"type": "string"},
            "description": (
                "What the work will CHANGE (paths, systems, services). Paths resolving under "
                "the Ouroboros system repository make the plan constitutional (BIBLE goes to reviewers)."
            ),
        },
        "evidence": {
            "type": "array", "items": {"type": "string"},
            "description": (
                "What reviewers should LOOK AT: file paths, task:<id> of a prior task, URLs. "
                "The host attaches files bounded (never fetching URLs) and names every omission."
            ),
        },
    },
}

_DISPOSITION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "description": (
        "Disposition mode only (send ONLY this field): answer the findings of the wave "
        "named by review_fingerprint. note/need_evidence findings close at $0; a blocking "
        "finding you accept needs a changed spec (new call), one you reject rides with your "
        "rationale into the next paid cycle. Never closes REVISE_PLAN."
    ),
    "properties": {
        "review_fingerprint": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "finding_id": {"type": "string"},
                    "decision": {"type": "string", "enum": list(plan_spec.DISPOSITION_DECISIONS)},
                    "rationale": {"type": "string"},
                },
                "required": ["finding_id", "decision", "rationale"],
            },
        },
    },
    "required": ["review_fingerprint", "items"],
}


def get_tools():
    return [
        ToolEntry(
            name="plan_task",
            schema={
                "name": "plan_task",
                "description": (
                    "Multi-model design review of an INTENTION before the work starts — code, "
                    "research, a deliverable, a computer-use flow, or an action in the world. "
                    "Submit goal + spec (what/how-checked/deferred) + plan prose; independent "
                    "reviewers return typed findings against the spec (blocking findings must name "
                    "the spec element they break); the host aggregates: GREEN closes; "
                    "REVIEW_REQUIRED closes by your review_disposition at no cost; REVISE_PLAN needs "
                    "a changed spec (next paid cycle) or a reject-with-rationale judged in the next "
                    "cycle. Cycles are bounded by the owner's Max review cycles; an unchanged "
                    "envelope replays the recorded result for free (a locator a reviewer asked for "
                    "with need_evidence is attached by the host next time and makes the envelope "
                    "new). Under blocking enforcement an "
                    "open review holds finalization; under advisory you may proceed with the "
                    "review open and the host discloses it. Declare evidence reviewers need; "
                    "declare affected_resources so a self-modification gets the constitutional pack."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string", "description": "Why — the outcome the work serves."},
                        "plan": {"type": "string", "description": "Accompanying prose: how you intend to do it (context for reviewers; the spec is what is judged)."},
                        "spec": _SPEC_SCHEMA,
                        "review_disposition": _DISPOSITION_SCHEMA,
                    },
                    # Two exclusive modes: goal+plan+spec (review) or review_disposition alone.
                    "required": [],
                },
            },
            handler=_handle_plan_task,
            timeout_sec=_PLAN_TASK_TOOL_TIMEOUT_SEC,
        )
    ]


# --------------------------------------------------------------------------- handler


def _vacuous_disposition(value: object) -> bool:
    """A schema-shaped but empty disposition (models fill optional objects with defaults)."""
    if not isinstance(value, dict) or set(value) - {"review_fingerprint", "items"}:
        return False
    return not str(value.get("review_fingerprint") or "").strip() and not value.get("items")


def _handle_plan_task(ctx: ToolContext, **params) -> str:
    raw_disposition = params.get("review_disposition")
    envelope_fields = sorted(set(params) - {"review_disposition"})
    if raw_disposition is not None and not _vacuous_disposition(raw_disposition):
        if envelope_fields:
            return (
                "ERROR: PLAN_REVIEW_DISPOSITION_MIXED_ENVELOPE: disposition mode accepts "
                "review_disposition only; a changed plan needs a new review-mode call "
                "without review_disposition. No plan attempt was recorded."
            )
        if not isinstance(raw_disposition, dict):
            return "ERROR: PLAN_REVIEW_DISPOSITION_INVALID: review_disposition must be an object"
        return _apply_disposition(ctx, raw_disposition)
    if "review_disposition" in params and not envelope_fields:
        return (
            "ERROR: PLAN_REVIEW_DISPOSITION_EMPTY: submit goal, plan and spec for review "
            "mode, or a complete review_disposition as the only field. No plan attempt was recorded."
        )
    request = _PlanRequest(
        goal=str(params.get("goal") or ""), plan=str(params.get("plan") or ""), spec=params.get("spec"),
    )
    try:
        try:
            asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(
                    asyncio.run,
                    asyncio.wait_for(_run_plan_review_async(ctx, request), timeout=_PLAN_REVIEW_WRAPPER_TIMEOUT_SEC),
                ).result(timeout=_PLAN_REVIEW_WRAPPER_TIMEOUT_SEC + 5)
        except RuntimeError:
            return asyncio.run(
                asyncio.wait_for(_run_plan_review_async(ctx, request), timeout=_PLAN_REVIEW_WRAPPER_TIMEOUT_SEC)
            )
    except (concurrent.futures.TimeoutError, asyncio.TimeoutError):
        return _plan_unavailable(
            ctx, f"ERROR: Plan review timed out after {_PLAN_REVIEW_WRAPPER_TIMEOUT_SEC}s.", "review_timeout")
    except Exception as e:
        log.error("plan_task failed: %s", e, exc_info=True)
        return _plan_unavailable(ctx, f"ERROR: Plan review failed: {e}", "review_failed")


def _plan_unavailable(ctx: ToolContext, message: str, reason: str) -> str:
    """Persist a retryable availability outcome (the current fingerprint stays open-unavailable)."""
    try:
        root, task_id = _planning_state_location(ctx)
        mark_current_plan_review_unavailable(root, task_id, reason=reason)
    except (OSError, TimeoutError, ValueError) as exc:
        return f"{message}\nERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: {exc}"
    return message


def _planning_state_location(ctx: ToolContext) -> tuple[pathlib.Path, str]:
    root = pathlib.Path(str(getattr(ctx, "budget_drive_root", "") or ctx.drive_root))
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    if not task_id:
        raise ValueError("PLAN_REVIEW_TASK_ID_REQUIRED: durable review state must belong to a real task")
    return root, task_id


# ------------------------------------------------------------------- inputs / packet


def _evidence_deny_paths(ctx: ToolContext) -> list[str]:
    """Paths evidence may never attach, whatever root the caller declares (C-06): the runtime
    data plane and the live settings file are a boundary, not a heuristic — an operator subject
    root one level above them would otherwise make owner credentials reviewable."""
    from ouroboros import config as _config

    out: list[str] = []
    for value in (getattr(_config, "SETTINGS_PATH", ""), getattr(_config, "DATA_DIR", "")):
        text = str(value or "").strip()
        if text:
            out.append(text)
    try:
        from ouroboros.tool_access import canonical_data_root

        drive = canonical_data_root(ctx)
        if drive:
            out.append(str(drive))
    except Exception:
        pass
    return out


def _plan_fingerprint(goal: str, plan: str, spec: dict, manifest_hash: str, constitutional: bool) -> str:
    """Identity of one review request (F4): goal, prose, canonical spec, evidence identity,
    the constitutional fact — never the exploration log (it changes no obligation)."""
    payload = {"goal": goal, "plan": plan, "spec": spec, "evidence_manifest_hash": manifest_hash,
               "constitutional": bool(constitutional)}
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _task_objective(ctx: ToolContext) -> str:
    contract = getattr(ctx, "task_contract", {})
    if not isinstance(contract, dict) or not contract:
        meta = getattr(ctx, "task_metadata", {})
        contract = meta.get("task_contract") if isinstance(meta, dict) else {}
    contract = contract if isinstance(contract, dict) else {}
    return str(contract.get("objective") or contract.get("description") or "")


def _task_evidence_reader(root: pathlib.Path) -> Callable[[str], Optional[str]]:
    """``task:<id>`` locators → a bounded JSON summary of that task's durable result."""
    def _read(task_id: str) -> Optional[str]:
        try:
            record = load_task_result(root, task_id)
        except Exception:
            return None
        if not isinstance(record, dict):
            return None
        return json.dumps({
            "task_id": task_id,
            "status": record.get("status"),
            "reason_code": record.get("reason_code"),
            "ts": record.get("ts"),
            "result": truncate_review_artifact(str(record.get("result") or ""), limit=_TASK_EVIDENCE_RESULT_CHARS),
        }, ensure_ascii=False, indent=2, default=str)
    return _read


def _root_exploration_log(ctx: ToolContext) -> Optional[str]:
    """Bounded tail of THIS task's tool calls before this plan_task, from the task-local
    conversation (S8): exact omitted count, never a scan of the shared tools.jsonl.
    ``None`` when the host holds no conversation for this context (named omission)."""
    from ouroboros.review_evidence import _accept_redact_cap

    messages = getattr(ctx, "messages", None)
    if not isinstance(messages, list):
        return None
    results: Dict[str, str] = {}
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "tool":
            results[str(message.get("tool_call_id") or "")] = str(message.get("content") or "")
    calls: List[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            name = str((fn or {}).get("name") or "")
            if not name or name == "plan_task":
                continue
            # I-04: raw tool args/results must be redacted before reviewers see them;
            # reuse acceptance's projection (`_accept_redact_cap`), not a second path (P7).
            args = _accept_redact_cap(str((fn or {}).get("arguments") or ""), _EXPLORATION_ARGS_CHARS)
            result = _accept_redact_cap(
                results.get(str(call.get("id") or ""), ""), _EXPLORATION_RESULT_CHARS,
            ).replace("\n", " ")
            calls.append(f"- {name}({args}) → {result or '(no result recorded)'}")
    tail = calls[-_EXPLORATION_TAIL_CALLS:]
    header = (
        f"{len(calls)} tool call(s) by this task before this plan_task; showing the last "
        f"{len(tail)}; omitted {len(calls) - len(tail)} (bounded tail)."
    )
    return "\n".join([header, *tail])


def _packet_kwargs(fn: Callable, **candidates: Any) -> Dict[str, Any]:
    """Pass only the kwargs the packet builder accepts — the Phase B builders gain
    additive kwargs (``bible_locator``/``bible_nav_map``/``cycle_index``) that land by
    merge; the engine must run before and after that merge."""
    try:
        accepted = set(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return {}
    return {key: value for key, value in candidates.items() if key in accepted}


def _session_task_text(system_prompt: str, user_content: str, session_root: str) -> str:
    return (
        "RETRIEVING REVIEWER (agent session): you run read-only inside "
        f"{session_root or 'the active workspace'}. The evidence below is the host's REDACTED "
        "snapshot — the same bytes every reviewer sees — so do NOT re-read the raw evidence "
        "locators (a session reading originals would leak what the api route redacts, 4e133c8a); "
        "the ONE exception is the governance pack — BIBLE.md and docs/ARCHITECTURE.md are public "
        "repository documents you MAY read raw, and MUST read in full when the pack marks them "
        "MANDATORY FULL READS (a self-modification plan), even if the agent also declared them as "
        "evidence. Retrieve any OTHER repository context with your own tools.\n\n"
        + system_prompt + "\n\n" + user_content
    )


# W3 host attachment is bounded like the agent's own evidence list (MAX_LIST_ITEMS honoured
# locators per task); what the cap drops is a NAMED `reviewer_request_cap` omission, never silent.
_REVIEWER_REQUEST_CAP = plan_spec.MAX_LIST_ITEMS


def _reviewer_requested_locators(ctx: ToolContext, state_root: pathlib.Path) -> tuple[list[str], list[str]]:
    """``(honoured, dropped)`` `need_evidence` locators from this task's earlier cycles
    (`need_evidence_seen`, kept sorted): the cap keeps the lexicographically first ones,
    deterministically (disclosed residual: past the cap a later, earlier-sorting request can
    displace an honoured one — then a named `reviewer_request_cap` omission, never silent).
    Raises ``ValueError`` when the durable state cannot be read: the wave that would pay for
    the packet must not run without the reviewers' recorded requests (fail-closed, P1)."""
    _root, task_id = _planning_state_location(ctx)
    try:
        state = load_plan_review_state(state_root, task_id)
    except (OSError, TimeoutError) as exc:
        raise ValueError(f"PLAN_REVIEW_STATE_INVALID: {exc}") from exc
    seen: list[str] = []
    dropped: list[str] = []
    for raw in state.get("need_evidence_seen") or []:
        loc = str(raw or "").strip()
        if not loc or loc in seen or loc in dropped:
            continue
        if len(loc) > plan_spec.MAX_ITEM_CHARS or len(seen) >= _REVIEWER_REQUEST_CAP:
            dropped.append(loc)
        else:
            seen.append(loc)
    return seen, dropped


def _prepare_plan_inputs(ctx: ToolContext, request: "_PlanRequest", state_root: pathlib.Path) -> dict:
    """The ONE preamble the paid path and the dry-run seam share: normalize the spec (with the
    envelope's goal injected), resolve the subject roots, derive `constitutional`, attach the
    declared evidence, compose the fingerprint. Returns ``{"error": ...}`` on refusal — a second
    assembly path had already diverged from this one (I-11)."""
    raw_spec = dict(request.spec) if isinstance(request.spec, dict) else request.spec
    if isinstance(raw_spec, dict):
        raw_spec["goal"] = request.goal.strip() or raw_spec.get("goal")
    spec, errors = plan_spec.normalize_spec(raw_spec if isinstance(raw_spec, dict) else None)
    if not request.plan.strip():
        errors = ["plan: required non-empty prose", *errors]
    if errors:
        return {"error": "ERROR: PLAN_SPEC_INVALID: " + "; ".join(errors) + ". No reviewer was called."}
    from ouroboros.review_substrate import review_repo_dirs_for
    try:
        system_root, active_root = review_repo_dirs_for(ctx)
    except ValueError as exc:
        return {"error": f"ERROR: PLAN_SUBJECT_ROOT_INVALID: {exc}"}
    locators = list(spec["affected_resources"]) + list(spec["evidence"])
    constitutional, constitutional_note = plan_spec.resolve_constitutional(
        active_root=active_root, system_repo_root=system_root,
        affected_resources=spec["affected_resources"], evidence=spec["evidence"],
        payload_roots=_plan_payload_roots(ctx, locators),
    )
    reminder = (
        "REMINDER: affected_resources is empty; if this work will change Ouroboros's own body, "
        "declare those paths so reviewers receive the constitutional pack (BIBLE)."
        if active_root == system_root and not spec["affected_resources"] else ""
    )
    declared_evidence = list(spec["evidence"])  # W3: earlier-cycle need_evidence is HOST-attached
    try:
        reviewer_requested, request_dropped = _reviewer_requested_locators(ctx, state_root)
    except ValueError as exc:
        return {"error": f"ERROR: {exc}"}
    host_locators = [loc for loc in reviewer_requested if loc not in declared_evidence]
    # B-08/C-06: allowed roots are the active workspace and the system repo only; the runtime
    # data plane is denied outright (the sensitive-name policy is a residual, not a boundary).
    manifest = plan_evidence.resolve_evidence(
        declared_evidence + host_locators, active_root=active_root,
        allowed_roots=[active_root, system_root],
        resolve_task=_task_evidence_reader(state_root), deny_paths=_evidence_deny_paths(ctx),
    )
    manifest["declared"] = declared_evidence  # the AGENT's list; requests below (tagged+hashed)
    if reviewer_requested:
        manifest["reviewer_requested"] = list(reviewer_requested)
    if request_dropped:  # still reviewer requests (provenance), just not honoured by the cap
        manifest["reviewer_requested_dropped"] = list(request_dropped)
        manifest.setdefault("omissions", []).extend(
            {"locator": loc, "reason": "reviewer_request_cap"} for loc in request_dropped)
    manifest_hash = plan_evidence.evidence_manifest_hash(manifest)
    fingerprint = _plan_fingerprint(spec["goal"], request.plan, spec, manifest_hash, constitutional)
    return {
        "spec": spec, "system_root": system_root, "active_root": active_root,
        "constitutional": constitutional, "constitutional_note": constitutional_note,
        "manifest": manifest, "manifest_hash": manifest_hash, "reminder": reminder,
        "fingerprint": fingerprint,
    }


# --------------------------------------------------------------------------- review


async def _run_plan_review_async(ctx: ToolContext, request: _PlanRequest) -> str:
    try:
        state_root, task_id = _planning_state_location(ctx)
    except ValueError as exc:
        return f"ERROR: PLAN_REVIEW_STATE_INVALID: {exc}"
    prepared = _prepare_plan_inputs(ctx, request, state_root)
    if prepared.get("error"):
        if "PLAN_SPEC_INVALID" in prepared["error"]:
            try:
                _record_raw_plan_request_attempt(
                    {"goal": request.goal, "plan": request.plan, "spec": request.spec},
                    state_root, task_id, reason="plan_input_invalid",
                )
            except (OSError, TimeoutError, ValueError) as exc:
                return f"ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: {exc}"
        return prepared["error"]
    spec, manifest = prepared["spec"], prepared["manifest"]
    system_root, active_root = prepared["system_root"], prepared["active_root"]
    constitutional = prepared["constitutional"]
    constitutional_note = prepared["constitutional_note"]
    manifest_hash = prepared["manifest_hash"]
    reminder = prepared["reminder"]
    fingerprint = prepared["fingerprint"]
    try:
        state = load_plan_review_state(state_root, task_id)
    except (OSError, TimeoutError, ValueError) as exc:
        return f"ERROR: PLAN_REVIEW_STATE_INVALID: {exc}"
    enforcement = get_review_enforcement()
    cap = review_max_cycles()
    cycles_paid = int(state.get("cycles_paid") or 0)
    # C-01: every envelope supersedes prior authority BEFORE any cap/rail exit.
    try:
        record_plan_review_attempt(state_root, task_id, fingerprint=fingerprint)
    except (OSError, TimeoutError, ValueError) as exc:
        return f"ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: {exc}"
    previous_override: Optional[dict] = None
    existing = plan_review_wave(state, fingerprint)
    if existing is not None and not isinstance(existing.get("spec"), dict):
        existing = None  # C-09: a COMPACTED row (no frozen spec) is never authority
    if existing is not None and str(existing.get("aggregate") or "") != "DEGRADED":
        # Fb3: identical request ⇒ idempotent replay — no panel, no cycle. ONE exception
        # (4e133c8a; R9-5): an open wave whose BLOCKING findings all carry recorded REJECT
        # dispositions — REVISE_PLAN, or REVIEW_REQUIRED with a below-quorum blocking finding
        # (C-08) — has earned the promised delta cycle; replaying forever would make "reject
        # rides into the next paid cycle" unreachable without a cosmetic spec edit.
        earned_delta = (
            str(existing.get("aggregate")) in {"REVISE_PLAN", "REVIEW_REQUIRED"}
            and not existing.get("closed")
            # D1: only VALID rejections earn the panel — raw items are persisted for
            # disclosure, but an invalid or contradictory one must not buy a cycle.
            and plan_spec.blocking_fully_rejected(
                existing.get("findings"), existing.get("dispositions"))
        )
        if not earned_delta:
            return _render_wave(existing, cap=cap, cycles_paid=cycles_paid, enforcement=enforcement,
                                cached=True, reminder=reminder)
        # the rejected wave IS the previous cycle for the delta panel
        previous_override = existing
    deadline_skip = _plan_deadline_skip(ctx)
    if deadline_skip:
        try:
            record_plan_review_attempt(state_root, task_id, fingerprint=fingerprint,
                                       status="rail_degraded", reason="plan_task_deadline")
        except (OSError, TimeoutError, ValueError) as exc:
            return f"ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: {exc}"
        return _plan_deadline_skip(ctx, emit=True) or deadline_skip
    if cap is not None and cycles_paid >= cap:
        return _cycles_exhausted(ctx, state, state_root, task_id, cap=cap, cycles_paid=cycles_paid,
                                 enforcement=enforcement, reminder=reminder,
                                 request_fingerprint=fingerprint)
    # #116: a malformed structured reviewer-slot config must refuse loudly here
    # instead of running the panel on the silently projected default models.
    from ouroboros.reviewer_slot_config import reviewer_slot_config_error

    if err := reviewer_slot_config_error():
        return _plan_unavailable(
            ctx, f"ERROR: Invalid reviewer-slot configuration blocks plan review — {err}. "
            "Fix Review lanes on the Agents tab in Settings.", "reviewer_slot_config_invalid")
    slots = _plan_review_slots()
    if not slots:
        return _plan_unavailable(
            ctx, "ERROR: No review models configured. Set OUROBOROS_REVIEW_MODELS in settings.",
            "review_models_unconfigured")

    previous = previous_override if previous_override is not None else _last_paid_wave(state)
    cycle_index = cycles_paid + 1
    system_prompt, user_content, session_task = _build_packet(
        ctx, spec=spec, request=request, manifest=manifest, constitutional=constitutional,
        system_root=system_root, active_root=active_root, cycle_index=cycle_index,
        enforcement=enforcement, previous=previous,
    )
    # Per-slot input fit (P3): oversize = FREE typed row; below quorum = typed refusal.
    callable_slots, oversize_rows, fit_error = _plan_slot_fit(
        slots, prompt_chars=len(system_prompt) + len(user_content), quorum=adaptive_quorum(len(slots)))
    if fit_error:
        return _plan_unavailable(ctx, fit_error, "review_context_unavailable")
    admission = review_wave_budget_gate(  # priced on the slots that will actually be called
        ctx, surface="plan_review", models=[str(s.model) for s in callable_slots],
        prompt_chars=len(system_prompt) + len(user_content), max_completion_tokens=_PLAN_REVIEW_MAX_TOKENS,
    )
    if admission is not None:
        return _plan_unavailable(
            ctx,
            "⚠️ PLAN_REVIEW_SKIPPED_BUDGET: the reviewer wave was declined before dispatch — "
            f"estimated cost ~${admission.get('estimated_wave_usd')} exceeds the remaining root "
            f"budget ${admission.get('remaining_usd')} (limit ${admission.get('limit_usd')}). "
            "No reviewer was called. Shrink the evidence, split the plan, or raise the per-task budget.",
            "review_budget_unavailable")
    ctx.emit_progress_fn(
        f"📐 plan_task: cycle {cycle_index}{'' if cap is None else f'/{cap}'} — running "
        f"{len(slots)} reviewer slot(s) ({enforcement}; constitutional={constitutional})…"
    )
    rows = await _run_plan_review_slots(
        ctx, callable_slots, system_prompt=system_prompt, user_content=user_content,
        session_task=session_task, session_root=str(active_root),
        output_contract=plan_spec.PLAN_FINDINGS_ARRAY_CONTRACT,
    )
    rows = list(rows) + oversize_rows  # excluded slots stay configured rows: they count in the quorum denominator
    ids = plan_spec.spec_ids(spec)
    seen_after: set[str] = set(str(s) for s in state.get("need_evidence_seen") or [])
    slot_results: List[dict] = []
    slot_records: List[dict] = []
    for row in rows:
        findings: List[dict] = []
        disclosures: List[str] = []
        error = str(row.get("error") or "")
        ok = not error
        if ok:
            parsed, parse_error = plan_spec.parse_findings(str(row.get("text") or ""))
            if parse_error:
                ok, error = False, parse_error
            else:
                findings, disclosures, slot_seen = plan_spec.validate_findings(
                    parsed, spec_ids=ids, seen_locators=seen_after, slot=str(row.get("slot_id") or ""),
                )  # cumulative across the wave: the per-task memory cap is exact
                seen_after |= set(slot_seen)
        slot_results.append({"slot": row.get("slot_id"), "model": row.get("model"), "ok": ok,
                             "findings": findings, "error": error or None})
        slot_records.append({
            "slot_id": row.get("slot_id"), "model": row.get("model"), "route": row.get("route"),
            "host_file_read_attestation": row.get("host_file_read_attestation"),
            "ok": ok, "error": error or None, "disclosures": disclosures,
            "prompt_ref": row.get("prompt_ref") or {}, "response_ref": row.get("response_ref") or {},
            "tokens_in": row.get("tokens_in"), "tokens_out": row.get("tokens_out"), "cost": row.get("cost"),
            "raw_text_preview": (
                truncate_review_artifact(str(row.get("text") or ""), limit=_RAW_TEXT_PREVIEW_CHARS)
                if not ok else ""
            ),
        })
    agg = plan_spec.aggregate(slot_results, quorum=adaptive_quorum(len(slots)))
    aggregate = str(agg["aggregate"])
    wave = {
        "schema_version": 2,
        "cycle_index": cycle_index,
        "request_fingerprint": fingerprint,
        "previous_fingerprint": str((previous or {}).get("request_fingerprint") or ""),
        "goal": spec["goal"],
        "plan_prose_hash": sha256(request.plan.encode("utf-8")).hexdigest(),
        "spec": spec,
        "spec_hash": plan_spec.spec_hash(spec),
        "evidence_manifest": {
            "declared": list(manifest.get("declared") or []),
            "attached": [{"locator": a.get("locator"), "sha256": a.get("sha256"), "bytes": a.get("bytes"),
                          **({"secrets_redacted": True} if a.get("secrets_redacted") else {})}
                         for a in manifest.get("attached") or []],
            "omissions": list(manifest.get("omissions") or []),
            "reviewer_requested": list(manifest.get("reviewer_requested") or []),
            "reviewer_requested_dropped": list(manifest.get("reviewer_requested_dropped") or []),
        },
        "evidence_manifest_hash": manifest_hash,
        "constitutional": bool(constitutional),
        "constitutional_note": constitutional_note,
        "findings": list(agg["findings"]),
        "aggregate": aggregate,
        "reasons": list(agg["reasons"]),
        "counts": dict(agg["counts"]),
        "closed": aggregate == "GREEN",
        "dispositions": [],
        "actors": slot_records,
        "actors_degraded": [str(r["slot_id"]) for r in slot_records if not r["ok"]],
        "enforcement": enforcement,
        "cycle_cap": cap,
        "paid": aggregate != "DEGRADED",
        "reviewed_at": utc_now_iso(),
    }
    try:
        stored = record_plan_review_wave(state_root, task_id, wave, need_evidence_seen=sorted(seen_after))
        if stored.get("paid") and not wave.get("paid"):
            # D2: the durable authority stayed the paid predecessor; the tool answer still
            # describes the DEGRADED attempt that just ran.
            stored = wave
    except (OSError, TimeoutError, ValueError) as exc:
        return f"ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: {exc}"
    paid_now = cycles_paid + (1 if wave["paid"] else 0)
    if (cap is not None and paid_now >= cap and not stored.get("closed")):
        # Scope-gate finding (39c3a195): when the FINAL permitted cycle ends open, the typed
        # cap state must land NOW — not wait for a second envelope the agent may never send —
        # or the blocking gate holds a task that can never buy another panel (D27).
        try:
            stored = mark_plan_review_cycles_exhausted(
                state_root, task_id, fingerprint=fingerprint) or stored
            record_plan_review_attempt(
                state_root, task_id, fingerprint=fingerprint, status="cycles_exhausted",
                reason=f"{paid_now}/{cap} paid plan-review cycles spent")
        except (OSError, TimeoutError, ValueError) as exc:
            return f"ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: {exc}"
        emit_review_cycles_exhausted(
            getattr(ctx, "event_queue", None), state_root, surface="plan_review",
            task_id=task_id, cycles_paid=paid_now, cap=cap, enforcement=enforcement,
            fingerprint=fingerprint)
    ctx.emit_progress_fn(
        f"📐 plan_task: {aggregate} — {agg['counts']['blocking']} blocking / "
        f"{agg['counts']['note']} note / {agg['counts']['need_evidence']} need_evidence; "
        f"cycles paid {paid_now}{'' if cap is None else f'/{cap}'}"
    )
    return _render_wave(stored, cap=cap, cycles_paid=paid_now, enforcement=enforcement, reminder=reminder)


def _last_paid_wave(state: dict) -> Optional[dict]:
    for wave in reversed(state.get("waves") or []):
        if wave.get("paid") and not wave.get("compact"):
            return wave
    return None


def build_plan_review_packet_for_dry_run(ctx: ToolContext, request: "_PlanRequest") -> dict:
    """Assemble the packet SHAPE of a fresh cycle (cycle_index=1, no prior-cycle section) with the
    task's recorded reviewer requests attached (W3), WITHOUT dispatching or recording anything.

    The operator entry point runs outside a task, where the shared budget gate has no usage
    scope and therefore no cost ceiling; this is how the operator sees the bill before paying
    it. Read-only, and it shares `_prepare_plan_inputs` with the paid path (I-11)."""
    state_root, _task_id = _planning_state_location(ctx)
    prepared = _prepare_plan_inputs(ctx, request, state_root)
    if prepared.get("error"):
        raise ValueError(prepared["error"])
    system_prompt, user_content, _session_task = _build_packet(
        ctx, spec=prepared["spec"], request=request, manifest=prepared["manifest"],
        constitutional=prepared["constitutional"], system_root=prepared["system_root"],
        active_root=prepared["active_root"], cycle_index=1,
        enforcement=get_review_enforcement(), previous=None,
    )
    return {
        "constitutional": prepared["constitutional"],
        "constitutional_note": prepared["constitutional_note"],
        "manifest": prepared["manifest"], "system_prompt": system_prompt,
        "user_content": user_content, "fingerprint": prepared["fingerprint"],
    }


def _governance_text(system_root: pathlib.Path, rel_path: str) -> str:
    """A governance document's text, or "" when it is missing: `explicit` returns a truthy
    omission NOTE, which would ship AS the constitution/architecture; a constitutional packet
    without the real document must be an assembly failure (D26/W3), not a note."""
    text = load_governance_doc(system_root, rel_path, on_missing="explicit")
    return "" if text.startswith("[⚠️ OMISSION") else text


def _build_packet(
    ctx: ToolContext, *, spec: dict, request: _PlanRequest, manifest: dict, constitutional: bool,
    system_root: pathlib.Path, active_root: pathlib.Path, cycle_index: int, enforcement: str,
    previous: Optional[dict],
) -> tuple[str, str, str]:
    """(system_prompt, user_content, session_task) — the lean packet (plan §7.2 C)."""
    try:
        checklist = load_checklist_section("Plan Review Checklist")
    except Exception as exc:
        log.warning("Could not load Plan Review Checklist: %s", exc)
        checklist = ""
    bible_text = _governance_text(system_root, "BIBLE.md")
    architecture_text = _governance_text(system_root, "docs/ARCHITECTURE.md")
    bible_nav_map = architecture_nav_map = ""
    if not constitutional:
        from ouroboros.context_layout import generate_doc_nav_map

        if bible_text.strip():
            bible_nav_map = generate_doc_nav_map(bible_text, title="BIBLE.md", rel_path="BIBLE.md")
        if architecture_text.strip():
            architecture_nav_map = generate_doc_nav_map(
                architecture_text, title="ARCHITECTURE.md", rel_path="docs/ARCHITECTURE.md"
            )
    def _system(by_retrieval: bool) -> str:
        return build_plan_review_system_prompt(
            checklist_section=checklist, constitutional=constitutional,
            bible_text=bible_text if constitutional else None, cycle_index=cycle_index,
            enforcement=enforcement,
            **_packet_kwargs(build_plan_review_system_prompt,
                             bible_locator=str(system_root / "BIBLE.md"), bible_nav_map=bible_nav_map or None,
                             architecture_text=architecture_text if constitutional else None,
                             architecture_locator=str(system_root / "docs" / "ARCHITECTURE.md"),
                             architecture_nav_map=architecture_nav_map or None,
                             governance_by_retrieval=by_retrieval),
        )

    system_prompt = _system(False)
    # plan_packet renders CYCLE records; the previous paid wave is exactly one (B-03).
    prior_cycles = ([{
        "cycle_index": (previous or {}).get("cycle_index"),
        "aggregate": (previous or {}).get("aggregate"),
        "findings": list((previous or {}).get("findings") or []),
    }] if previous else [])
    dispositions = list((previous or {}).get("dispositions") or [])
    delta = (  # a truncated frozen body (durable-state fit) is never diffed element-wise
        {"unavailable": "previous frozen spec body truncated to fit the durable state; hashes name the original"}
        if previous and previous.get("spec_body_truncated")
        else plan_spec.spec_delta(previous.get("spec"), spec) if previous else None
    )
    common = dict(
        objective=_task_objective(ctx), goal=spec["goal"], plan_prose=request.plan, spec=spec,
        prior_cycles=prior_cycles, dispositions=dispositions, spec_delta=delta,
        root_exploration_log=_root_exploration_log(ctx),
        **_packet_kwargs(build_plan_review_user_content, cycle_index=cycle_index),
    )
    user_content = build_plan_review_user_content(manifest=manifest, **common)
    # session task = the executor's COMPACT form: governance by mandatory retrieval, not inline
    session_task = _session_task_text(_system(True), user_content, str(active_root))
    return system_prompt, user_content, session_task


def _cycles_exhausted(
    ctx: ToolContext, state: dict, state_root: pathlib.Path, task_id: str, *,
    cap: int, cycles_paid: int, enforcement: str, reminder: str,
    request_fingerprint: str = "",
) -> str:
    """The typed cap result (D10/D27): no panel, the current wave stays open, the typed
    event fires; blocking exits are owner unstick or a blocked_with_evidence terminal."""
    current = current_plan_review_wave(state)
    # C-01: a CLOSED wave recorded for a DIFFERENT envelope is history, never this
    # request's answer — rendering it would hand a changed, unreviewed spec the old
    # GREEN. An OPEN wave is the live obligation and still carries the hold, so it is
    # marked and rendered with the cap head above it.
    stale_closed = bool(
        current
        and current.get("closed")
        and str(current.get("request_fingerprint") or "") != str(request_fingerprint or "")
    )
    if stale_closed:
        current = None
    if current is None and request_fingerprint:
            # A NEW envelope at a spent cap has no wave of its own; the live obligation is
        # the last open wave, and the CURRENT attempt records the cap so the gate
        # releases finalization honestly (D27).
        current = next(
            (w for w in reversed(list(state.get("waves") or [])) if isinstance(w, dict) and not w.get("closed")),
            None,
        )
    fingerprint = str((current or {}).get("request_fingerprint") or "")
    if fingerprint and not (current or {}).get("closed"):
        try:
            current = mark_plan_review_cycles_exhausted(state_root, task_id, fingerprint=fingerprint) or current
        except (OSError, TimeoutError, ValueError) as exc:
            return f"ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: {exc}"
    if request_fingerprint:
        try:
            record_plan_review_attempt(
                state_root, task_id, fingerprint=request_fingerprint, status="cycles_exhausted",
                reason=f"{cycles_paid}/{cap} paid plan-review cycles spent")
        except (OSError, TimeoutError, ValueError) as exc:
            return f"ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: {exc}"
    emit_review_cycles_exhausted(
        getattr(ctx, "event_queue", None), state_root, surface="plan_review", task_id=task_id,
        cycles_paid=cycles_paid, cap=cap, enforcement=enforcement, fingerprint=fingerprint,
    )
    ctx.emit_progress_fn(
        f"📐 plan_task: PLAN_REVIEW_CYCLES_EXHAUSTED — {cycles_paid}/{cap} paid cycles spent ({enforcement})."
    )
    body = (
        _render_wave(current, cap=cap, cycles_paid=cycles_paid, enforcement=enforcement,
                     cached=True, reminder=reminder)
        if current
        # No live wave for THIS envelope (a fresh spec submitted at a spent cap): the
        # host still owns exactly one control line, and it can never be a closed one.
        else (f"{reminder}\n\n" if reminder else "") + PLAN_REVIEW_CONTROL_PREFIX + json.dumps(
            {"outcome": "REVISE_PLAN", "closed": False}, ensure_ascii=False
        )
    )
    head = (
        f"⚠️ PLAN_REVIEW_CYCLES_EXHAUSTED: {cycles_paid} of {cap} paid plan-review cycles are spent "
        "for this task; no reviewer was called and no cycle was consumed. "
    )
    if enforcement == "blocking":
        head += (
            "Blocking enforcement: the plan review stays OPEN, so implementation stays held — but "
            "finalization is RELEASED so the task can end honestly instead of waiting for a panel it "
            "can no longer buy (owner decision D27). Your exits are an owner unstick (Swarm/hurry), a "
            "revised spec once the owner raises OUROBOROS_REVIEW_MAX_CYCLES, or finalizing now with "
            "outcome_tier=blocked_with_evidence. Do not start the work under an open blocking review."
        )
    else:
        head += (
            "Advisory enforcement: you may proceed with the review open; the host records and "
            "discloses it loudly (typed event review_cycles_exhausted)."
        )
    return head + "\n\n" + body


# ---------------------------------------------------------------------- disposition


def _apply_disposition(ctx: ToolContext, disposition: dict) -> str:
    unknown = sorted(str(k) for k in disposition if k not in {"review_fingerprint", "items"})
    if unknown:
        return "ERROR: PLAN_REVIEW_DISPOSITION_INVALID: unknown fields: " + ", ".join(unknown)
    fingerprint = str(disposition.get("review_fingerprint") or "").strip()
    if not fingerprint:
        return "ERROR: PLAN_REVIEW_DISPOSITION_INVALID: review_fingerprint is required"
    try:
        root, task_id = _planning_state_location(ctx)
        state = load_plan_review_state(root, task_id)
    except (OSError, TimeoutError, ValueError) as exc:
        return "ERROR: PLAN_REVIEW_STATE_INVALID: " + str(exc)
    enforcement = get_review_enforcement()
    cap = review_max_cycles()
    cycles_paid = int(state.get("cycles_paid") or 0)
    wave = plan_review_wave(state, fingerprint)
    if wave is None or wave.get("compact"):
        return (
            "ERROR: PLAN_REVIEW_DISPOSITION_UNBINDABLE: no recorded plan-review wave holds "
            f"fingerprint {fingerprint} (compact history is not dispositionable). No plan attempt was recorded."
        )
    # I-01: a disposition closes ONLY the CURRENT attempt's wave (never a superseded one).
    attempt = state.get("current_attempt") if isinstance(state.get("current_attempt"), dict) else {}
    current_fp = str(attempt.get("fingerprint") or "")
    if current_fp and current_fp != fingerprint:
        return (
            "ERROR: PLAN_REVIEW_DISPOSITION_STALE: a disposition can close only the CURRENT "
            f"plan-review wave; a newer attempt supersedes it (current={current_fp}, "
            f"claimed={fingerprint}). Re-call plan_task with the spec you want reviewed. "
            "No plan attempt was recorded."
        )
    if wave.get("closed"):
        return _render_wave(wave, cap=cap, cycles_paid=cycles_paid, enforcement=enforcement, cached=True,
                            notes=["already_closed: this wave is closed; the disposition is not re-applied"])
    raw_items = disposition.get("items")
    if not isinstance(raw_items, list):
        return "ERROR: PLAN_REVIEW_DISPOSITION_INVALID: items must be an array"
    if len(raw_items) > 2 * len(wave.get("findings") or []) + 8:  # bounded like the findings they answer
        return "ERROR: PLAN_REVIEW_DISPOSITION_INVALID: more items than findings could need"
    items: List[dict] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            return f"ERROR: PLAN_REVIEW_DISPOSITION_INVALID: items[{index}] must be an object"
        items.append({
            "finding_id": str(item.get("finding_id") or "").strip()[:plan_spec.MAX_ID_CHARS * 2],
            "decision": str(item.get("decision") or "").strip().lower()[:40],  # enum-like, bounded
            "rationale": plan_spec.bounded_text(item.get("rationale"), plan_spec.MAX_FINDING_TEXT_CHARS),
        })
    known = {str(f.get("finding_id") or "") for f in wave.get("findings") or []}
    unknown_ids = sorted({i["finding_id"] for i in items if i["finding_id"] not in known})
    if unknown_ids:
        return (
            "ERROR: PLAN_REVIEW_DISPOSITION_INVALID: unknown finding ids " + ", ".join(unknown_ids)
            + "; valid ids: " + ", ".join(sorted(known))
        )
    closure = plan_spec.closure_after_disposition(
        str(wave.get("aggregate") or ""), wave.get("findings") or [], items, enforcement,
    )
    try:
        stored = record_plan_review_dispositions(
            root, task_id, fingerprint=fingerprint, dispositions=items,
            closed=bool(closure["closed"]), closure_notes=list(closure["notes"]),
        )
    except (OSError, TimeoutError, ValueError) as exc:
        return "ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: " + str(exc)
    ctx.emit_progress_fn(
        f"📐 plan_task: disposition recorded — {'closed' if closure['closed'] else 'still open'} "
        f"({len(closure['open_ids'])} open finding id(s); no reviewer call, no cycle)."
    )
    return _render_wave(stored, cap=cap, cycles_paid=cycles_paid, enforcement=enforcement,
                        notes=list(closure["notes"]))


# ------------------------------------------------------------------------ rendering


