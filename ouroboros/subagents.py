"""Subagent lane, cap, and metadata helpers.

THE OWNER-FACING AXES ARE THREE, AND EVERYTHING ELSE IS DERIVED.

A parent declares the WORK: ``write_surface`` (what the child may DO),
``model_lane`` (how good the answer must be) and ``executor`` (where it runs).
Model, effort, route and TOOL profile are consequences of those three plus the
owner's settings and what is live at the moment the child starts — never a second
thing the parent can ask for. The CREDENTIAL profile is NOT derived here at all
(see the DelegationRoute pin and the rotation note below): it is Claudexor's
choice, and the applied one comes back on the engine receipt.

``effort`` used to be a fourth public parameter and broke that twice. It was a
second knob for the question ``model_lane`` already answers, so ``model_lane:
light`` with ``effort: max`` was a request nobody could resolve (it pinned the
cheapest model to the strongest reasoning and no rule reconciled them). And a
harness route carries its OWN effort, so a parent asking ``low`` against a route
pinned to ``xhigh`` had no rule for who wins. It is removed rather than ranked
against the lane (BIBLE P2: remove the class). The owner still controls effort
exactly as before, through ``config.resolve_effort(task_type)``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping

from ouroboros.config import (
    SETTINGS_DEFAULTS,
    get_heavy_model,
    get_light_model,
)

log = logging.getLogger(__name__)

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

# The lane STRENGTH ordering (weak -> strong), and the only definition of "weaker
# than what was asked for" there is. Every axis in `capability_delta` compares
# requested against effective through a rank like this one — effort already had
# `config.effort_rank`, the lane had nothing, so each caller that wanted to know
# whether a lane had been lowered re-derived an answer locally or (more often)
# did not ask. `auto` is deliberately absent: it is a REQUEST to inherit, not a
# strength, so it has no rank and can never be compared.
LANE_STRENGTH: tuple[str, ...] = ("light", "main", "heavy")

# The lane a child gets when there is NO lane on record: the root agent's, which is
# Main. One name for it, because it is the answer in three places at once — the
# inheritance source for a root's child, the unknown-stored-lane coercion, and the
# completion-side envelope fallback.
LANE_OF_RECORD: str = "main"


def lane_rank(lane: str) -> int:
    """Index of a lane in LANE_STRENGTH (−1 for `auto`/unknown). Ordering SSOT."""
    value = str(lane or "").strip().lower()
    return LANE_STRENGTH.index(value) if value in LANE_STRENGTH else -1


def lane_is_weaker(lane: str, than: str) -> bool:
    """Whether ``lane`` is a weaker strength than ``than`` — THE definition of a lane
    reduction, and the only one.

    ``than`` is always a lane the resolution actually STARTED FROM: the request when
    it named a lane, the inherited parent lane when it did not. Never `auto`, which
    has no rank — comparing against a request to inherit answers nothing, and the
    v6.87.26 default made `auto` the COMMON request, so a comparison against it was
    a disclosure that could not fire on its own headline path.
    """
    return lane_rank(lane) < lane_rank(than)


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

    ``profile_id`` is the OPTIONAL manual credential pin (Q2-в, applied as the
    reversible default pending owner confirmation): empty means the daemon's
    own rotation policy picks the account (D28 — the default); a value rides
    the run request verbatim as ``credentialProfileId``. Reviewer-slot rows
    are the only author today.
    """

    route_id: str
    model: str = ""
    effort: str = ""
    profile_id: str = ""


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
    # A DELEGATED run is externally confined, and the engine rewrites its access to
    # `external_sandbox_full` before admitting it (`RequestRequirementsResolver.adapterAccess`)
    # — so the profile the route must declare is that one, not the literal the request
    # carries. Comparing the literal refused every route whose adapter stands its own
    # sandbox down in favour of the engine's boundary and therefore declares only the
    # confined profile: today opencode, which was given `external_sandbox_full` for
    # exactly this run. Refusing what the engine would admit turned `executor="harness"`
    # into a typed blocker and `auto` into a silent, metered drop to a native child.
    if shape.access not in supported and not (
        shape.delegated and "external_sandbox_full" in supported
    ):
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


def dispatch_executor_resolution(task: Mapping[str, Any]) -> SubagentExecutorResolution:
    """The executor axis of ONE dispatch: the typed rule table fed with live health.

    The whole shape, from the one owner of it: health is checked against the run
    this child would really start — a mutating child probes the `workspace_write`
    + delegated shape, a read-only one the narrow default — not against a profile
    string reassembled by the caller. `native` asks the daemon nothing. Tolerant
    of stored garbage: the schema refused anything invalid at schedule time, so an
    unknown STORED executor is stale data, not an argument error.
    """
    requested = str(task.get("requested_executor") or "auto").strip().lower() or "auto"
    if requested not in SUBAGENT_EXECUTORS:
        requested = "auto"
    if requested == "native":
        return resolve_subagent_executor("native")  # nothing to ask the daemon
    from ouroboros.contracts.task_constraint import normalize_task_constraint
    from ouroboros.tool_access import predicted_subagent_profile

    constraint = normalize_task_constraint(task.get("task_constraint"))
    surface = str(getattr(constraint, "surface", "") or "")
    shape = delegated_run_shape(predicted_subagent_profile(write_surface=surface) == "acting_subagent")
    return probe_subagent_executor(requested, shape=shape)


@dataclass(frozen=True)
class SubagentLaneResolution:
    requested_lane: str
    effective_lane: str
    model: str
    use_local_model: bool = False
    # `slot_index`/`slot_count`/`downgrade_note` were removed in v6.87.28. The first
    # two carried a lane fan-out that no lane has had since v6.87.7 (a lane names
    # strength, and strength is one model), and the third had been the empty string
    # at every call site since the depth cap was deleted. A degenerate seam is still
    # a seam readers must reason about.
    # The concrete lane the resolution STARTED FROM — the request when it named a
    # lane, the inherited parent lane when it said `auto`. Without it the only
    # baseline available was the literal request, so an INHERITED heavy that landed
    # on Main compared `main` against `auto` and reported no reduction at all: the
    # v6.87.26 default was the one case its own invariant could not see.
    resolved_from: str = ""

    @property
    def reduced(self) -> bool:
        """Whether this lane landed below the lane it resolved from."""
        return lane_is_weaker(self.effective_lane, self.resolved_from)


def intended_lane(requested_lane: Any, parent_lane: Any = "") -> str:
    """The concrete lane a request MEANS: itself, or the parent's when it says `auto`.

    Pure INTENT — no environment, no live availability, no record. It is the half of
    the lane question that is answerable the moment the request is made, and it is
    named here because two places need it and neither may own it: the resolution
    (which measures the effective lane against it) and the ADMISSION gate for a
    `require_lane` delegation constraint, which runs before the child is dispatched
    and so cannot ask what lane the child ended up on. An admission gate that
    re-derived "auto means the parent's" on its own would be the second copy this
    module keeps deleting.

    Tolerant of stored garbage on both sides: a durable lane outlives the schema
    that wrote it, and neither a stale parent lane nor a stale request may make a
    child unschedulable.
    """
    try:
        requested = normalize_subagent_model_lane(requested_lane)
    except ValueError:
        requested = "auto"
    try:
        inherited = normalize_subagent_model_lane(parent_lane or LANE_OF_RECORD)
    except ValueError:
        inherited = LANE_OF_RECORD
    if inherited == "auto":
        # A stored `auto` is "no lane on record" wearing the request's clothes. Left
        # as-is it reaches `_lane_model` as an unknown lane and the child silently
        # drops to Light — the one value that is not a strength must not become one.
        inherited = LANE_OF_RECORD
    return inherited if requested == "auto" else requested


def normalize_subagent_model_lane(value: Any) -> str:
    lane = str(value or "auto").strip().lower()
    if lane not in SUBAGENT_MODEL_LANES:
        allowed = ", ".join(sorted(SUBAGENT_MODEL_LANES))
        raise ValueError(f"model_lane must be one of: {allowed}")
    return lane


def _slot_model(key: str) -> str:
    return str(os.environ.get(key, "") or SETTINGS_DEFAULTS.get(key, "") or "").strip()


_LANE_SLOT_KEYS = {
    "main": ("OUROBOROS_MODEL", "USE_LOCAL_MAIN"),
    "heavy": ("OUROBOROS_MODEL_HEAVY", "USE_LOCAL_HEAVY"),
    "light": ("OUROBOROS_MODEL_LIGHT", "USE_LOCAL_LIGHT"),
}


def lane_ran_on_main(lane: str, model: str) -> bool:
    """True when a Heavy/Light lane RESOLVED TO THE MAIN MODEL.

    ENV PRESENCE decides, not string equality: an absent/empty Heavy or Light slot
    is the inherit-from-Main case even when Main happens to equal that slot's
    shipped default, so substituting the default here would silently demand the
    lane's own local flag for a lane that is really running Main (v6.82: Light
    ships a real default, so "empty slot" alone no longer identifies the case).

    This predicate was buried inside ``_use_local_for_lane``, which is why the
    fact never reached anything else: the resolution kept reporting
    ``effective_lane="heavy"`` for a child that was really running Main.
    """
    if lane not in {"heavy", "light"}:
        return False
    env_slot = str(os.environ.get(_LANE_SLOT_KEYS[lane][0], "") or "").strip()
    return not env_slot or bool(model and model != env_slot)


def _use_local_for_lane(lane: str, model: str) -> bool:
    pair = _LANE_SLOT_KEYS.get(lane)
    if not pair:
        return False
    model_key, local_key = pair
    if lane_ran_on_main(lane, model):
        # Follows Main's local flag, so USE_LOCAL_MAIN governs the effective model
        # rather than being silently ignored.
        return _use_local_for_lane("main", model)
    slot_value = str(os.environ.get(model_key, "") or "").strip()
    if lane == "main":
        slot_value = slot_value or str(SETTINGS_DEFAULTS.get(model_key, "") or "").strip()
    return (
        bool(model)
        and model == slot_value
        and str(os.environ.get(local_key, "") or "").strip().lower() in {"1", "true", "yes", "on"}
    )


def _lane_model(lane: str) -> str:
    if lane == "heavy":
        return get_heavy_model()  # empty heavy slot -> main
    if lane == "light":
        return get_light_model()  # empty light slot -> main
    # `main` AND anything else. The fall-through used to be Light, so a lane that
    # slipped past normalization silently drew the CHEAPEST model — the opposite of
    # the default this module states everywhere else. An unknown lane is "no lane on
    # record", and that is LANE_OF_RECORD.
    return _slot_model("OUROBOROS_MODEL")


def resolve_subagent_lane(
    requested_lane: str,
    *,
    parent_lane: str = "",
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

    An omitted lane INHERITS THE PARENT'S effective lane. Until v6.87.26 it
    collapsed to ``light`` on the theory that silence means cheap work — but the
    parent that judges the work cheap can SAY ``light``, and silently demoting
    every unspecified child made the declaration invisible and the default wrong:
    a Heavy parent handing a child a piece of its own job got a Light child and no
    signal that it had happened. Inheritance makes the weak lane a stated
    decision instead of a hidden one, and a child that starts cheap and finds
    otherwise still raises its own power with ``switch_model`` (BIBLE P5). Depth
    does not rewrite the lane — recursion is bounded by the structural depth cap
    and by concurrency, which are the limits that actually bind — so depth is
    not an input here at all (the dead parameter was removed; the signature
    guard in test_resolver_takes_no_authority_argument watches for regrowth).

    ``parent_lane`` is the caller's OWN effective lane; empty means "no lane on
    record", which is the root agent, and the root runs Main.
    """
    requested = normalize_subagent_model_lane(requested_lane)
    resolved_from = intended_lane(requested, parent_lane)
    effective = resolved_from
    model = _lane_model(effective)
    if lane_ran_on_main(effective, model):
        # Report the slot the model ACTUALLY came from. Asking for Heavy on an
        # install with no Heavy slot gets the Main model, and calling that
        # `effective_lane="heavy"` made the record claim a strength nobody
        # configured — the reduction `capability_delta` now announces.
        effective = "main"
    return SubagentLaneResolution(
        requested_lane=requested,
        effective_lane=effective,
        model=model,
        use_local_model=_use_local_for_lane(effective, model),
        resolved_from=resolved_from,
    )


@dataclass(frozen=True)
class CapabilityDelta:
    """What was ASKED for versus what was GIVEN, on every axis at once.

    A child could land below its request in several unrelated places — an inherited
    lane, a lane slot that is not configured, an effort the route caps, an executor
    pin no route can honor — and not one of them announced itself. The reductions
    were spread across the resolver, the model getters, the LLM client and (for the
    executor) nowhere at all, so "what was asked" and "what was given" were never
    compared in the same place. This record is that place, and
    ``resolve_subagent_dispatch`` is its only author.

    The effort axis names a DERIVED value, not a request: since v6.87.28 no parent
    can ask for an effort, so what a route's learned band is measured against is the
    effort ``config.resolve_effort`` gives this task type. A route that caps it below
    the owner's own setting is still a reduction, and still the owner's business.
    """

    requested_lane: str = "auto"
    # What `requested_lane` MEANT: itself, or the parent's lane when it was `auto`.
    # The effective lane is measured against this, never against a bare `auto`.
    resolved_lane: str = "main"
    effective_lane: str = "main"
    derived_effort: str = ""
    effective_effort: str = ""
    requested_executor: str = "auto"
    effective_executor: str = "native"
    reason: str = ""
    reduced: bool = False
    # A field that was accepted once, is still on durable records, and is now
    # IGNORED — stated here rather than dropped, because a value that silently stops
    # meaning anything is the same class of defect as a reduction nobody announces.
    # Not a reduction: nothing was taken away, the field simply no longer exists.
    legacy_note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "requested_lane": self.requested_lane,
            "resolved_lane": self.resolved_lane,
            "effective_lane": self.effective_lane,
            "derived_effort": self.derived_effort,
            "effective_effort": self.effective_effort,
            "requested_executor": self.requested_executor,
            "effective_executor": self.effective_executor,
            "reason": self.reason,
            "reduced": bool(self.reduced),
            "legacy_note": self.legacy_note,
        }


def capability_delta_disclosures(delta: Mapping[str, Any]) -> List[str]:
    """The human phrases a delta discloses — THE renderer, reading the plain dict.

    Both surfaces that show a delta read it from a dict off a durable record, and
    they used to render it twice: the parent's notice built its phrases from the
    dataclass while the child's prompt block re-assembled its own from the event,
    which is how the child could be told `auto->main` (its parent asked for nothing)
    while the parent read `auto(inherited heavy)->main`. One body, two callers.
    """
    parts: List[str] = []
    if lane_is_weaker(str(delta.get("effective_lane") or ""), str(delta.get("resolved_lane") or "")):
        parts.append(f"model_lane {lane_delta_phrase(delta)}")
    # RANK, not inequality: `effective_effort` is the whole learned band, so a route
    # with a floor can land ABOVE the derived effort, and "runs BELOW what was asked
    # for — effort none->low" is a false alarm the moment any OTHER axis puts this
    # delta into a reduction.
    from ouroboros.config import effort_rank

    derived = str(delta.get("derived_effort") or "")
    effective_effort = str(delta.get("effective_effort") or "")
    if derived and effort_rank(effective_effort) < effort_rank(derived):
        parts.append(f"effort {derived}->{effective_effort}")
    # `auto` is "no preference", so resolving it to a concrete route takes nothing
    # away — only an unhonored PIN belongs in the disclosure.
    requested_executor = str(delta.get("requested_executor") or "auto")
    effective_executor = str(delta.get("effective_executor") or "")
    if requested_executor != "auto" and effective_executor != requested_executor:
        parts.append(f"executor {requested_executor}->{effective_executor}")
    return parts


def lane_delta_phrase(delta: Mapping[str, Any]) -> str:
    """``asked->got`` for the lane, naming what an omitted request RESOLVED FROM.

    Takes the DICT because every surface that renders a delta reads it from a
    durable record, and a lane that reads `auto->main` in one of them tells the
    child its parent asked for nothing.
    """
    requested = str(delta.get("requested_lane") or "?")
    resolved = str(delta.get("resolved_lane") or "")
    effective = str(delta.get("effective_lane") or "?")
    if resolved and resolved != requested:
        return f"{requested}(inherited {resolved})->{effective}"
    return f"{requested}->{effective}"


def _route_effort(model: str, effort: str) -> str:
    """The effort this route will ACTUALLY run: the request clamped into the route's
    learned band by the SAME call the dispatcher makes.

    It used to read the ceiling alone, which is half of the band the dispatcher
    clamps to — so a route with a learned FLOOR (v6.73.2, endpoints where reasoning
    is mandatory) had its delta report the request verbatim while the call ran
    something else. Falls back to the request on any failure: a missing evidence
    store must never block scheduling.
    """
    try:
        from ouroboros.llm import LLMClient

        return str(LLMClient.clamp_effort_for_route(str(model or ""), effort) or effort)
    except Exception:  # pragma: no cover - evidence store is advisory
        log.debug("Effort-band lookup failed for %r", model, exc_info=True)
        return effort


# The durable keys a scheduling request may state. Everything the dispatch
# resolution derives is written under DIFFERENT keys, so "what was asked" can never
# be overwritten by "what was given" — the asymmetry `requested_model_lane` /
# `effective_model_lane` already had, extended to every axis.
SUBAGENT_INTENT_FIELDS: tuple[str, ...] = (
    "requested_model_lane",
    "parent_model_lane",
    "requested_executor",
)

# Fields a stored subagent record may still carry from a schema that no longer
# exists. They are IGNORED at dispatch and the reason is written onto the record;
# a load never fails over one (BIBLE P1: no silent loss, and no crash either).
LEGACY_SUBAGENT_FIELDS: Dict[str, str] = {
    "reasoning_effort": (
        "effort is no longer an owner-facing axis: it is derived from the owner's "
        "configured effort for this task type, because a public effort was a second "
        "knob for the question model_lane already answers"
    ),
}

# Everything the dispatch resolution writes onto the task record —
# ``SubagentDispatch.record_fields()``'s keys plus the envelope rebuilt from them.
# This is the merge set the worker reports back over the supervisor event channel
# (``task_dispatch_resolved``): the worker resolves on a process-local clone of the
# task, so the supervisor-owned RUNNING copy — the one persist_queue_snapshot
# serializes — must be told, or a restart loses the decision (XG-2R.1). The merge
# is key-scoped to exactly this set so a worker report can never overwrite
# scheduling intent or supervisor bookkeeping. Pinned against record_fields() by
# tests/test_model_slot_role_model.py.
SUBAGENT_RESOLUTION_FIELDS: tuple[str, ...] = (
    "effective_model_lane",
    "model",
    "use_local_model",
    "reasoning_effort",
    "effective_executor",
    "executor_route",
    "tool_profile",
    "capability_delta",
    "subagent_envelope",
)


@dataclass(frozen=True)
class SubagentDispatch:
    """Everything a child's execution is decided by, resolved ONCE, at dispatch.

    Model, effort, route and profile are DERIVED — from the three declared axes,
    from the owner's settings, and from what is live at the moment the child starts.
    They are not separate decisions taken in separate places at separate times,
    which is what they had become: the lane and a dead availability boolean resolved
    inside the scheduling tool call, while WHO actually ran the child was decided in
    the worker, and both wrote a record. Two resolvers with two records cannot be
    kept in agreement; there is one of each.
    """

    lane: SubagentLaneResolution
    effort: str
    executor: str
    route: str
    profile: str
    delta: CapabilityDelta
    # The typed executor resolution this dispatch was decided by (p34's rule
    # table). Carried whole so the surfaces that speak about the executor — the
    # child's substrate note, the blocked-outcome terminal, the events record —
    # read the SAME facts (reason, reset_at, route) this resolution produced,
    # instead of re-deriving them from strings.
    executor_resolution: SubagentExecutorResolution | None = None
    legacy_ignored: Dict[str, str] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        """Whether this child MUST NOT run: an explicit executor pin no route honors.

        One predicate, so no reader compares against the literal `"blocked"` and no
        surface can forget that this outcome is not a kind of `native`.
        """
        return self.executor == "blocked"

    def record_fields(self) -> Dict[str, Any]:
        """The durable projection of this resolution — ONE writer for the one record.

        Every surface that persists a dispatched child (the running task result, the
        task-metadata projection, the completion write, the envelope) reads these
        keys off the task, so an added axis is one edit here instead of a field-by-
        field mapping repeated in four modules that drift apart one release later.
        """
        return {
            "effective_model_lane": self.lane.effective_lane,
            "model": self.lane.model,
            "use_local_model": self.lane.use_local_model,
            "reasoning_effort": self.effort,
            "effective_executor": self.executor,
            "executor_route": self.route,
            "tool_profile": self.profile,
            "capability_delta": self.delta.as_dict(),
        }


def resolve_subagent_dispatch(
    task: Mapping[str, Any], *, task_type: str = "",
) -> SubagentDispatch:
    """THE resolution point: where power, effort, route, profile and executor meet.

    It runs at DISPATCH, from the child's durable record, because that is the last
    moment at which the answer is still true. Two of its inputs are live — whether a
    harness route exists, and what band that route's model has learned — and a queued
    child can wait out a whole outage. Resolving at schedule time produced a record
    that claimed an answer about a moment that had already passed; resolving twice
    produced two records that disagreed about the same child.

    The record states INTENT only (``SUBAGENT_INTENT_FIELDS`` plus the task
    constraint); everything here is derived from it. A stored field from a retired
    schema is ignored and SAID SO rather than obeyed or dropped.
    """
    requested_lane = str(task.get("requested_model_lane") or task.get("model_lane") or "auto")
    try:
        requested_lane = normalize_subagent_model_lane(requested_lane)
    except ValueError:
        # Durable-data tolerance: the schema refused anything invalid when it was
        # asked for, so an unknown STORED lane is stale, not an argument error.
        requested_lane = "auto"
    requested_executor = str(task.get("requested_executor") or "auto")
    try:
        requested_executor = normalize_subagent_executor(requested_executor)
    except ValueError:
        requested_executor = "auto"

    lane = resolve_subagent_lane(
        requested_lane, parent_lane=str(task.get("parent_model_lane") or ""),
    )
    # The executor axis lands on p34's typed rule table (H1: one resolver, one
    # vocabulary, `blocked` a typed third outcome), fed with LIVE route health at
    # this same dispatch moment. The p2-era `harness_delegation_route()` stub and
    # its tuple-returning twin resolver are deleted rather than kept beside it.
    executor_resolution = dispatch_executor_resolution(task)
    executor = executor_resolution.executor
    route = executor_resolution.route.route_id if executor_resolution.route else ""
    # Only a REDUCTION belongs in the delta. The table also names the ordinary
    # outcomes (`requested_native`, `harness_ready`) and the no-preference case
    # (`auto` with no route configured, D28: nothing was asked for, no delta) —
    # none of which took anything away from the request.
    executor_reason = executor_resolution.reason
    if executor_reason in ("requested_native", "harness_ready") or (
        requested_executor == "auto" and executor_reason == "harness_not_configured"
    ):
        executor_reason = ""
    from ouroboros.config import effort_rank, resolve_effort

    derived_effort = resolve_effort(task_type or str(task.get("type") or "task"))
    effective_effort = _route_effort(lane.model, derived_effort)

    reasons: List[str] = []
    if effort_rank(effective_effort) < effort_rank(derived_effort):
        reasons.append(f"route_effort_ceiling={effective_effort}")
    if lane.reduced:
        reasons.append(f"lane_slot_unavailable={lane.resolved_from}")
    if executor_reason:
        reasons.append(executor_reason)

    # A record that carries its own capability_delta was ALREADY resolved once:
    # the stored reasoning_effort is the residue record_fields() wrote — which now
    # survives the queue snapshot and a crash-requeue — not a request from a
    # retired schema. A replay must not disclose a false "ignored" legacy note
    # about the resolver's own writing (XG-2R.1); a genuinely legacy record
    # pre-dates capability_delta and still gets the note.
    prior_resolution = bool(task.get("capability_delta")) and isinstance(task.get("capability_delta"), dict)
    legacy_ignored = {} if prior_resolution else {
        name: f"{name}={task.get(name)!r} ignored: {why}"
        for name, why in LEGACY_SUBAGENT_FIELDS.items()
        if str(task.get(name) or "").strip()
    }
    delta = CapabilityDelta(
        requested_lane=lane.requested_lane,
        resolved_lane=lane.resolved_from,
        effective_lane=lane.effective_lane,
        derived_effort=derived_effort,
        # The effort the route will ACTUALLY run — the whole learned band through
        # the same call the dispatcher clamps with, so a route with a learned FLOOR
        # is reported honestly and is not called a reduction.
        effective_effort=effective_effort,
        requested_executor=requested_executor,
        effective_executor=executor,
        reason="; ".join(reasons),
        reduced=bool(reasons),
        legacy_note="; ".join(sorted(legacy_ignored.values())),
    )
    from ouroboros.tools.control_delegation import profile_from_task_constraint

    constraint = task.get("task_constraint") if isinstance(task.get("task_constraint"), dict) else {}
    return SubagentDispatch(
        lane=lane,
        # The DERIVED effort, not the clamped one. The dispatcher re-clamps per
        # model on every call, and a fallback route with a wider band must not
        # inherit this route's ceiling through the stored value; what this route
        # will really run is reported by the delta.
        effort=derived_effort,
        executor=executor,
        route=route if executor == "harness" else "",
        profile=profile_from_task_constraint(constraint),
        delta=delta,
        executor_resolution=executor_resolution,
        legacy_ignored=legacy_ignored,
    )


def build_subagent_envelope(
    *,
    task_id: str,
    parent_task_id: str = "",
    root_task_id: str = "",
    task_group_id: str = "",
    depth: int = 0,
    role: str = "",
    requested_lane: str = "auto",
    effective_lane: str = "",
    model: str = "",
    reasoning_effort: str = "",
    executor: str = "",
    effective_executor: str = "",
    executor_route: str = "",
    tool_profile: str = "",
    capability_delta: Dict[str, Any] | None = None,
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
        # the effective_lane guard that already existed). The unknown-lane default is
        # LANE_OF_RECORD, the same answer resolve_subagent_lane gives for "no lane on
        # record" — it was a hardcoded "light" here and another hardcoded "light" in the
        # completion-side caller, so the old default outlived itself in two copies.
        "requested_lane": normalize_subagent_model_lane(requested_lane if requested_lane in SUBAGENT_MODEL_LANES else "auto"),
        # EMPTY means NOT RESOLVED YET, and stays empty. A child between `requested`
        # and dispatch has an intent and no answer: the lane, the model and the
        # effort are derived when the child starts, from what is live then. The old
        # default said `light`, so a queued child's envelope named a lane, a slot and
        # a strength that no resolution had produced — a claim, not a record.
        "effective_lane": (
            normalize_subagent_model_lane(effective_lane if effective_lane in SUBAGENT_MODEL_LANES else LANE_OF_RECORD)
            if str(effective_lane or "").strip() else ""
        ),
        "model": str(model or ""),
        # DERIVED at dispatch (`resolve_subagent_dispatch`), not requested: no parent
        # can ask for an effort. Empty means the child has not been dispatched.
        "reasoning_effort": str(reasoning_effort or ""),
        "executor": str(executor or ""),
        # `executor` is the REQUEST (empty means "not requested"); this is who
        # actually ran the child. They were one field until v6.87.26, so a pin no
        # route could honor was reported back as if it had been honored — the
        # asymmetry `requested_model_lane`/`effective_model_lane` had avoided all
        # along, sitting one line above.
        "effective_executor": str(effective_executor or ""),
        # WHICH route ran it, and what the child could DO — the two remaining
        # derivations. A record naming `harness` without naming the route cannot be
        # audited, and the profile is the authority half of the same description.
        "executor_route": str(executor_route or ""),
        "tool_profile": str(tool_profile or ""),
        "capability_delta": dict(capability_delta or {}),
        "status": str(status or ""),
        "usage": usage_data,
        "cost_usd": round(float(cost_usd), 6) if cost_usd is not None else None,
    }


def envelope_from_task(
    task: Dict[str, Any],
    *,
    status: str,
    usage: Dict[str, Any] | None = None,
    cost_usd: float | None = None,
) -> Dict[str, Any]:
    """Build a child's envelope from its durable task record — the ONE mapping.

    The scheduler builds the same envelope from live locals; this is its twin, and
    the two were separate field-by-field mappings living in different modules. They
    had already drifted: the completion side re-derived the effective-lane fallback
    as a hardcoded ``"light"``, so a record missing that field came back describing
    a lane the resolver would never produce. Keeping the record->envelope mapping
    beside the envelope schema means an added axis is one edit, not two.

    The derived half of the record is exactly ``SubagentDispatch.record_fields()``,
    so a child that has not been dispatched yet reports empty derivations rather
    than a substituted default.
    """
    usage = usage or {}
    return build_subagent_envelope(
        task_id=str(task.get("id") or ""),
        parent_task_id=str(task.get("parent_task_id") or ""),
        root_task_id=str(task.get("root_task_id") or ""),
        task_group_id=str(task.get("task_group_id") or ""),
        depth=int(task.get("depth") or 0),
        role=str(task.get("role") or ""),
        requested_lane=str(task.get("requested_model_lane") or task.get("model_lane") or "auto"),
        effective_lane=str(task.get("effective_model_lane") or ""),
        model=str(task.get("model") or ""),
        reasoning_effort=str(task.get("reasoning_effort") or ""),
        executor=str(task.get("requested_executor") or ""),
        effective_executor=str(task.get("effective_executor") or ""),
        executor_route=str(task.get("executor_route") or ""),
        tool_profile=str(task.get("tool_profile") or ""),
        capability_delta=task.get("capability_delta") if isinstance(task.get("capability_delta"), dict) else {},
        status=status,
        usage={
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "rounds": int(usage.get("rounds") or 0),
        },
        cost_usd=cost_usd,
    )
