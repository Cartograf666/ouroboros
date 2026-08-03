"""Subagent lane, cap, and metadata helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from ouroboros.config import (
    SETTINGS_DEFAULTS,
    get_heavy_model,
    get_light_model,
)

# A lane names POWER and nothing else. `review` and `scope` were members until
# v6.87.7, where they named a TOPOLOGY — "fan out across the configured reviewer
# slots" — smuggled in through a strength parameter. Nothing in the product ever
# asked for them: every review surface (commit, plan, acceptance, skill) reads its
# slots straight from config through get_review_models/get_scope_review_models and
# runs them on the review substrate, never through schedule_subagent. They are
# removed rather than kept as aliases (BIBLE P2: remove the class). Durable records
# that still carry the old values stay readable — build_subagent_envelope already
# coerces an unknown stored lane instead of raising.
SUBAGENT_MODEL_LANES: frozenset[str] = frozenset({
    "auto",
    "main",
    "heavy",
    "light",
})

# The third axis. POWER is the lane, AUTHORITY is the write surface, and this is
# WHO RUNS THE CHILD — the SUBSTRATE its cognition burns (metered API tokens vs. an
# already-paid subscription session hosted by Claudexor). It is deliberately not a
# harness name: Ouroboros asks for a capability and lets the owner's configuration
# decide which harness answers, so a harness identity never reaches core (AGENTS.md).
# `harness` is a pin, not a preference — when the configured harness is unavailable
# the request is refused rather than silently re-routed onto metered spend the caller
# did not ask for.
SUBAGENT_EXECUTORS: tuple[str, ...] = ("auto", "harness", "native")


def normalize_subagent_executor(value: Any) -> str:
    executor = str(value or "auto").strip().lower()
    if executor not in SUBAGENT_EXECUTORS:
        allowed = ", ".join(SUBAGENT_EXECUTORS)
        raise ValueError(f"executor must be one of: {allowed}")
    return executor


@dataclass(frozen=True)
class DelegatedRunShape:
    """The complete run shape a child's own authority entitles it to.

    Not a knob: every field follows from the ONE question ``delegated_run_shape``
    asks, and none of them appears in any tool schema, so the model has nothing to
    widen. It is derived here rather than at each consumer because the consumers are
    not one — the DISPATCHER health-checks the route before a token is spent and the
    NANNY builds the wire request — and a shape re-derived at each of them drifts:
    a change to the access profile that forgets the isolation, or to the isolation
    that forgets the delegated marker, is silent and unsafe in exactly one branch.
    """

    access: str
    mode: str
    isolation: str = ""
    delegated: bool = False


def delegated_run_shape(acting: bool) -> DelegatedRunShape:
    """The run shape for an acting (mutating) child, or for a read-only one.

    A MUTATING child runs ``live``: Claudexor edits the nanny's OWN worktree in place,
    so the nanny's existing workspace-patch capture sees the harness's edits with no
    new plumbing, and the same capture invalidates itself if the harness dared to
    commit. In place is also the ONE shape where Claudexor would otherwise hand the
    harness the operator's real ``$HOME`` — which holds the daemon control token — so
    ``delegated`` travels with it, inseparably, in the same record.

    A READ-ONLY child has nothing to write back, so it runs in Claudexor's default
    envelope, which is scoped already and needs no marker: that is one transport with
    one derived difference, not a second pipeline.
    """
    if acting:
        return DelegatedRunShape(access="workspace_write", mode="agent",
                                 isolation="live", delegated=True)
    return DelegatedRunShape(access="readonly", mode="ask")


@dataclass(frozen=True)
class DelegationRoute:
    """An OPAQUE Claudexor route plus optional model/effort.

    ``route_id`` is passed through to Claudexor verbatim as the primary harness.
    Ouroboros never interprets it — no ``if codex/claude/cursor`` anywhere (AGENTS.md).
    """

    route_id: str
    model: str = ""
    effort: str = ""


def parse_subagent_harness(value: Any) -> DelegationRoute | None:
    """Parse ``harness[=model][:effort]`` — Claudexor's own reviewer-panel spelling."""
    raw = str(value or "").strip()
    if not raw:
        return None
    route_id, _, tail = raw.partition("=")
    model, _, effort = tail.partition(":")
    route_id = route_id.strip()
    if not route_id:
        return None
    return DelegationRoute(route_id=route_id, model=model.strip(), effort=effort.strip())


def get_subagent_harness() -> DelegationRoute | None:
    """Read the NARROW ``OUROBOROS_SUBAGENT_HARNESS`` setting.

    This is the ONLY reader. The key is deliberately absent from
    ``provider_models.MODEL_SETTING_KEYS``: a session-only route is not an API model
    identity, and letting it into that sweep would poison credential planning,
    pricing, and bench provenance.
    """
    return parse_subagent_harness(
        os.environ.get("OUROBOROS_SUBAGENT_HARNESS", "")
        or SETTINGS_DEFAULTS.get("OUROBOROS_SUBAGENT_HARNESS", "")
    )


@dataclass(frozen=True)
class SubagentExecutorResolution:
    """Outcome of the execution rule table (TZ 3.5)."""

    requested: str
    executor: str  # native | harness | blocked
    route: DelegationRoute | None = None
    reason: str = ""
    reset_at: str = ""

    @property
    def blocked(self) -> bool:
        return self.executor == "blocked"


def resolve_subagent_executor(
    requested: Any = "auto",
    *,
    route: DelegationRoute | None = None,
    unavailable_reason: str = "",
    reset_at: str = "",
) -> SubagentExecutorResolution:
    """The execution rule table. Pure — health facts are inputs, never probed here.

    | requested | state                    | behaviour                                  |
    |-----------|--------------------------|--------------------------------------------|
    | auto      | harness not configured   | native child                               |
    | auto      | configured and healthy   | nanny on the harness                       |
    | auto      | every profile exhausted  | native + LOUD typed fallback (D28)         |
    | auto      | otherwise unavailable    | native child WITH a visible marker         |
    | harness   | unavailable              | typed blocker — never silently spend API $ |
    | native    | any                      | native child                               |

    The executor AXIS — the module-level ``SUBAGENT_EXECUTORS`` vocabulary and its
    ``normalize_subagent_executor`` schema normalizer — belongs to the
    ``schedule_subagent`` schema work, which owns where the value ENTERS the system.
    This module deliberately does not define a second copy of either name: two
    top-level definitions of the same name in one module merge CLEANLY in git and
    then silently shadow each other. The guard below is a local fail-closed floor over
    the three cases this table actually specifies, so an unrecognized executor raises
    here instead of quietly behaving like ``auto``.
    """
    executor = str(requested or "auto").strip().lower()
    if executor not in ("auto", "harness", "native"):
        raise ValueError("executor must be one of: auto, harness, native")
    if executor == "native":
        return SubagentExecutorResolution(executor, "native", None, "requested_native")
    if route is None:
        reason = "harness_not_configured"
        if executor == "harness":
            return SubagentExecutorResolution(executor, "blocked", None, reason)
        return SubagentExecutorResolution(executor, "native", None, reason)
    if reset_at:
        # EVERY profile of the route is spent (the readiness predicate only reports a
        # reset when none has room left). Owner decision D28: `auto` FALLS BACK TO THE
        # METERED API, loudly — a typed decision made HERE, at the one point that costs
        # nothing yet, never a silent drift and never a wait. It used to dispatch the
        # child as a NANNY anyway, whose very first `delegate_start` was then refused
        # with this same fact: a spent dispatch, and the child left to improvise a
        # fallback in prose. Exhaustion heals on a timer, so `reset_at` rides along —
        # the child can say "I could have waited until X" and the parent can read it.
        #
        # An explicit `harness` request is the opposite answer and stays untouched: a
        # PIN exists precisely to avoid metered spend, so it blocks WITH the reset time.
        return SubagentExecutorResolution(
            executor,
            "blocked" if executor == "harness" else "native",
            route,
            "subscription_window_exhausted",
            reset_at,
        )
    if unavailable_reason:
        return SubagentExecutorResolution(
            executor,
            "blocked" if executor == "harness" else "native",
            route,
            unavailable_reason,
        )
    return SubagentExecutorResolution(executor, "harness", route, "harness_ready")


def route_health(gateway: Any, route_id: str, shape: DelegatedRunShape) -> tuple[str, str]:
    """Return ``(unavailable_reason, reset_at)`` for a route about to run ``shape``.

    One reader, so the answer the DISPATCHER acts on and the answer the nanny's own
    ``delegate_start`` gets cannot drift into disagreeing about the same route. Health
    is asked about the SHAPE, not about a route in the abstract: a route that can only
    read is not a usable substrate for a child that must write, and an ENGINE that
    would reject the delegated marker outright is not a usable substrate for one either.
    """
    from ouroboros.config import CLAUDEXOR_DELEGATED_MARKER_MIN_VERSION
    from ouroboros.gateways.claudexor import engine_at_least

    catalog = gateway.agent_capabilities()
    entry = None
    for row in catalog.get("harnesses") or []:
        if isinstance(row, dict) and str(row.get("id") or "") == route_id:
            entry = row
            break
    if entry is None:
        return "route_not_in_capability_catalog", ""
    if not entry.get("enabled") or str(entry.get("status") or "") != "ok":
        return f"route_status_{entry.get('status') or 'disabled'}", ""
    supported = [str(v) for v in entry.get("accessProfilesSupported") or []]
    if shape.access not in supported:
        return f"access_profile_unsupported:{shape.access}", ""
    # An engine below the marker floor REJECTS `execution.delegated` outright — the field
    # is absent from a `.strict()` schema, so the start is a 400 and no run exists. That
    # is the only thing this version answers, and it is asked here so the refusal is typed
    # and arrives before a token is spent instead of as an opaque HTTP error mid-dispatch.
    # It says NOTHING about whether an admitted engine applies an OS boundary: that is a
    # per-attempt fact, read back from the run's own artifacts by
    # `tools.delegate._containment_evidence` and DISCLOSED rather than refused. The floor
    # cannot be a capability probe either — the marker is nested under `execution`, and
    # the catalog derives its key list from TOP-LEVEL request keys only.
    if shape.delegated and not engine_at_least(
        str(getattr(gateway, "engine_version", "") or ""),
        CLAUDEXOR_DELEGATED_MARKER_MIN_VERSION,
    ):
        return "engine_rejects_delegated_marker", ""
    return "", _exhausted_window_reset_at(gateway, route_id)


def _exhausted_window_reset_at(gateway: Any, route_id: str) -> str:
    """Reset instant for a route whose EVERY credential profile is spent ('' otherwise).

    A window counts as spent when the harness reports it fully used or explicitly
    cooling down. Stale snapshots are ignored — an old reading must not block a lane.

    ANY LIVE SNAPSHOT MEANS THE LANE IS USABLE (D28). A harness commonly fronts several
    credential profiles, each reporting its own snapshot; answering with a blocker as
    soon as ONE of them was spent took the whole harness offline while its siblings were
    live — an outage invented out of a healthy substrate, and `harness` is a PIN, so the
    caller was refused rather than re-routed. Only when NO fresh snapshot of this route
    has room left is there something to wait for, and then the honest instant is the
    EARLIEST, because the first window to heal makes the harness usable again.

    A snapshot with a spent constraint counts as spent even if another of ITS OWN
    constraints has room: a 5-hour window at 100% blocks that profile now, whatever its
    weekly window says. WHICH profile a run lands on is Claudexor's business — rotation
    stays there and no profile identity is interpreted here (a profile-keyed version of
    this predicate was written and then removed: an executed mutant proved the bucketing
    changed no answer this function can give, and dead structure on a readiness path is
    the premature abstraction P7 forbids).
    """
    spent: List[str] = []
    any_live = False
    for snapshot in gateway.quota_snapshots():
        subject = snapshot.get("subject") if isinstance(snapshot.get("subject"), dict) else {}
        if str(subject.get("harness") or "") != route_id:
            continue
        if str(snapshot.get("freshness") or "") != "fresh":
            continue
        resets = [
            (str(c.get("cooldown_until") or "") or str(c.get("resets_at") or ""))
            for c in (snapshot.get("constraints") or [])
            if isinstance(c, dict) and (
                bool(c.get("cooldown_until"))
                or (isinstance(c.get("used_ratio"), (int, float))
                    and float(c.get("used_ratio")) >= 1.0))
        ]
        if resets:
            spent.extend(reset for reset in resets if reset)
        else:
            any_live = True
    if any_live or not spent:
        return ""
    return min(spent)


def probe_subagent_executor(
    requested: Any = "auto", *, shape: DelegatedRunShape | None = None,
) -> SubagentExecutorResolution:
    """Impure companion to the pure table: gather the health facts, then apply it.

    Kept separate so the rule table itself stays a pure function of stated facts. No
    route configured means no daemon call at all — the ordinary install pays nothing
    for an axis it does not use. ``shape`` defaults to the read-only shape: a caller
    that states nothing is asking for the narrowest run there is.
    """
    route = get_subagent_harness()
    if route is None:
        return resolve_subagent_executor(requested, route=None)
    from ouroboros.gateways.claudexor import ClaudexorGateway, ClaudexorUnavailable

    run_shape = shape if shape is not None else delegated_run_shape(False)
    gateway = None
    try:
        gateway = ClaudexorGateway()
        gateway.handshake()
        unavailable, reset_at = route_health(gateway, route.route_id, run_shape)
    except ClaudexorUnavailable as exc:
        return resolve_subagent_executor(
            requested, route=route, unavailable_reason=exc.code,
            reset_at=str(getattr(exc, "reset_at", "") or ""),
        )
    finally:
        if gateway is not None:
            gateway.close()
    return resolve_subagent_executor(
        requested, route=route, unavailable_reason=unavailable, reset_at=reset_at,
    )


@dataclass(frozen=True)
class SubagentLaneResolution:
    requested_lane: str
    effective_lane: str
    model: str
    use_local_model: bool = False
    slot_index: int = 0
    slot_count: int = 1
    downgrade_note: str = ""


def normalize_subagent_model_lane(value: Any) -> str:
    lane = str(value or "auto").strip().lower()
    if lane not in SUBAGENT_MODEL_LANES:
        allowed = ", ".join(sorted(SUBAGENT_MODEL_LANES))
        raise ValueError(f"model_lane must be one of: {allowed}")
    return lane


def _slot_model(key: str) -> str:
    return str(os.environ.get(key, "") or SETTINGS_DEFAULTS.get(key, "") or "").strip()


def _use_local_for_lane(lane: str, model: str) -> bool:
    checks = {
        "main": ("OUROBOROS_MODEL", "USE_LOCAL_MAIN"),
        "heavy": ("OUROBOROS_MODEL_HEAVY", "USE_LOCAL_HEAVY"),
        "light": ("OUROBOROS_MODEL_LIGHT", "USE_LOCAL_LIGHT"),
    }
    pair = checks.get(lane)
    if not pair:
        return False
    model_key, local_key = pair
    # ENV PRESENCE decides, not string equality: an absent/empty Heavy or Light slot
    # is the inherit-from-Main case even when Main happens to equal that slot's
    # shipped default, so substituting the default here would silently demand the
    # lane's own local flag for a lane that is really running Main.
    env_slot = str(os.environ.get(model_key, "") or "").strip()
    slot_value = env_slot if lane in {"heavy", "light"} else (
        env_slot or str(SETTINGS_DEFAULTS.get(model_key, "") or "").strip()
    )
    if lane in {"heavy", "light"} and (not slot_value or (model and model != slot_value)):
        # A Heavy/Light lane that RESOLVED TO THE MAIN MODEL — because its slot is
        # empty, or because the slot value is not what the lane actually runs —
        # follows Main's local flag, so USE_LOCAL_MAIN governs the effective model
        # rather than being silently ignored (v6.82: Light ships a real default, so
        # "empty slot" alone no longer identifies the inherit-from-Main case).
        return _use_local_for_lane("main", model)
    return (
        bool(model)
        and model == slot_value
        and str(os.environ.get(local_key, "") or "").strip().lower() in {"1", "true", "yes", "on"}
    )


def _lane_model(lane: str) -> str:
    if lane == "main":
        return _slot_model("OUROBOROS_MODEL")
    if lane == "heavy":
        return get_heavy_model()  # empty heavy slot -> main
    return get_light_model()  # empty light slot -> main


def resolve_subagent_lane(
    requested_lane: str,
    *,
    depth: int,
    slot_index: int = 0,
    slot_count: int = 1,
) -> SubagentLaneResolution:
    """Resolve a subagent's effective lane + model.

    AUTHORITY AND POWER ARE ORTHOGONAL. What a child may DO comes from its
    ``task_constraint`` through ``tool_access.active_tool_profile``; how STRONG a
    child is comes from the lane the parent asked for, and from nothing else.

    Until v6.87.7 one line coupled them: ``auto`` resolved to ``heavy`` when the
    child was "mutating", where mutating meant ``write_surface OR may_mutate`` —
    and ``may_mutate`` per its own schema grants the right to spawn mutating
    DESCENDANTS. A read-only child therefore drew the expensive model because of
    a permission about its unborn grandchildren. That coupling arrived as a
    by-product of the CODE->HEAVY slot rename (952e210) and never had a stated
    rationale; it is deleted rather than re-tuned (BIBLE P2: remove the class).

    Omission is a decision, not missing information: an unspecified lane resolves
    to ``light``. The parent that spawned this child knows whether the work is
    heavy; silence means it is not, and a child that starts cheap and finds
    otherwise raises its own power with ``switch_model`` (BIBLE P5). Depth no
    longer rewrites the lane — recursion is bounded by the structural depth cap
    and by concurrency, which are the limits that actually bind.
    """
    requested = normalize_subagent_model_lane(requested_lane)
    effective = "light" if requested == "auto" else requested
    model = _lane_model(effective)
    return SubagentLaneResolution(
        requested_lane=requested,
        effective_lane=effective,
        model=model,
        use_local_model=_use_local_for_lane(effective, model),
        slot_index=int(slot_index or 0),
        slot_count=max(1, int(slot_count or 1)),
        downgrade_note="",
    )


def expand_subagent_lane_slots(
    requested_lane: str, *, depth: int
) -> List[SubagentLaneResolution]:
    """Resolve the slots one scheduling request expands into.

    No lane fans out any more: a lane names strength, and strength is one model.
    The list shape is the scheduler's, not the lane's — ``_schedule_task`` builds
    drives, events and a task group from a sequence, and that plumbing is generic.
    Keeping the seam here means a future multi-slot source plugs in without
    reopening the scheduler; today every lane yields exactly one slot.
    """
    requested = normalize_subagent_model_lane(requested_lane)
    return [resolve_subagent_lane(requested, depth=depth, slot_count=1)]


def build_subagent_envelope(
    *,
    task_id: str,
    parent_task_id: str = "",
    root_task_id: str = "",
    task_group_id: str = "",
    depth: int = 0,
    role: str = "",
    requested_lane: str = "auto",
    effective_lane: str = "light",
    model: str = "",
    reasoning_effort: str = "",
    executor: str = "",
    resolved_executor: str = "",
    executor_reason: str = "",
    status: str = "",
    usage: Dict[str, Any] | None = None,
    cost_usd: float | None = None,
) -> Dict[str, Any]:
    usage_data = dict(usage or {})
    if cost_usd is None:
        try:
            raw_cost = usage_data.get("cost")
            if raw_cost is None:
                raw_cost = usage_data.get("cost_usd")
            cost_usd = float(raw_cost) if raw_cost is not None else None
        except (TypeError, ValueError):
            cost_usd = None
    return {
        "task_id": str(task_id or ""),
        "lineage": {
            "parent_task_id": str(parent_task_id or ""),
            "root_task_id": str(root_task_id or ""),
            "depth": int(depth or 0),
        },
        "task_group_id": str(task_group_id or ""),
        "role": str(role or ""),
        # Durable-data tolerance: this envelope is built for an ALREADY-RAN task from its
        # stored record, which may carry a legacy/unknown lane (e.g. a pre-v6.39 "code")
        # that the public schema now rejects. Coerce an unknown stored lane to a safe
        # default instead of raising (the public schedule_subagent schema stays strict —
        # this is NOT a "code"->"heavy" alias, just metadata robustness, symmetric with
        # the effective_lane guard that already existed).
        "requested_lane": normalize_subagent_model_lane(requested_lane if requested_lane in SUBAGENT_MODEL_LANES else "auto"),
        "effective_lane": normalize_subagent_model_lane(effective_lane if effective_lane in SUBAGENT_MODEL_LANES else "light"),
        "model": str(model or ""),
        # The envelope is the subagent's public description, so the axes it was
        # scheduled on belong in it. Empty means "not requested" and stays empty —
        # substituting the resolved default here would report a decision the parent
        # never made, and the envelope is read as evidence of what WAS asked.
        "reasoning_effort": str(reasoning_effort or ""),
        "executor": str(executor or ""),
        # WHAT WAS ASKED and WHAT ACTUALLY RAN are two facts (P34P1 / D4's p4-owned
        # half). `executor` is the parent's REQUEST, which for `auto` says nothing about
        # where the cognition was actually paid for; the dispatch resolution knows, and
        # without carrying it here the durable envelope and the parent's terminal result
        # both reported `auto` for a child that had silently fallen back to metered
        # native spend. Empty stays empty: a task that never reached dispatch has no
        # resolved value, and substituting the request would be the same lie one field
        # over. `executor_reason` carries WHY, because "native" alone cannot distinguish
        # "no harness configured" from "the harness was exhausted".
        "resolved_executor": str(resolved_executor or ""),
        "executor_reason": str(executor_reason or ""),
        "executor_diverged": bool(resolved_executor
                                  and str(executor or "") not in ("", str(resolved_executor))),
        "status": str(status or ""),
        "usage": usage_data,
        "cost_usd": round(float(cost_usd), 6) if cost_usd is not None else None,
    }


def compact_task_group(
    *,
    group_id: str,
    task_ids: Iterable[str],
    requested_lane: str,
    parent_task_id: str = "",
    root_task_id: str = "",
    role: str = "",
) -> Dict[str, Any]:
    ids = [str(task_id) for task_id in task_ids if str(task_id).strip()]
    return {
        "id": str(group_id or ""),
        "kind": "subagent_group",
        "task_ids": ids,
        "size": len(ids),
        "requested_lane": normalize_subagent_model_lane(requested_lane),
        "parent_task_id": str(parent_task_id or ""),
        "root_task_id": str(root_task_id or ""),
        "role": str(role or ""),
    }
