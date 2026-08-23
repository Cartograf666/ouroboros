"""Is a delegated route usable RIGHT NOW — and on whose account.

Split out of ``ouroboros/subagents.py`` at its size gate, as one coherent
concern rather than a slice taken to shed lines: the module it left owns the
subagent AXIS VOCABULARIES and the dispatch resolution, while everything here
answers a different question — whether the substrate a route names can actually
take the run about to start.

That question has two halves, and reading only the first is what made a
connected Antigravity unusable. The engine's harness status answers "is there a
DEFAULT credential"; for a profile-only adapter there never is one by design, so
the status says `unavailable` forever however many working accounts are
attached. ``routable_profile`` is the other half — the engine's own per-account
verdict — and ``route_health`` consults it before refusing.

``ouroboros.subagents`` re-exports every name here, so existing callers and
tests keep finding them there.
"""

from __future__ import annotations

import logging
from typing import Any, List, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ouroboros.subagents import DelegatedRunShape, DelegationRoute

log = logging.getLogger(__name__)


def routable_profile(gateway: Any, route_id: str) -> str:
    """The enabled, engine-verified named account for a route ('' when none).

    Some harnesses have NO default credential at all — the engine says so in the
    harness's own readiness ("accounts are named profiles") and keeps reporting
    the harness itself as `unavailable` forever, however many working accounts
    are attached. Antigravity is one: after a fully successful sign-in its
    harness row is byte-identical to before, while the credential-profile view
    reports the account `available` / `verification: passed`.

    So "can this route run" cannot be read off the harness status alone, and
    this is the other half of the answer. Availability is the ENGINE'S verdict
    (`status.availability`), never inferred from a credential's existence: an
    attached-but-unauthenticated profile is exactly the state that must NOT
    count. Fail-soft — an unreadable profile store answers '' and the caller
    falls back to the harness status it has always used.
    """
    try:
        body = gateway.credential_profiles()
    except Exception:
        log.debug("credential profiles unreadable for %s", route_id, exc_info=True)
        return ""
    wanted = str(route_id or "").strip()
    for wrapper in (body.get("profiles") if isinstance(body, dict) else None) or []:
        if not isinstance(wrapper, dict):
            continue
        profile = wrapper.get("profile")
        status = wrapper.get("status")
        if not isinstance(profile, dict) or not isinstance(status, dict):
            continue
        if str(profile.get("harness_id") or "") != wanted or not profile.get("enabled"):
            continue
        if str(status.get("availability") or "") == "available":
            return str(profile.get("profile_id") or "")
    return ""


def route_health(
    gateway: Any, route_id: str, shape: DelegatedRunShape, *, route_model: str = "",
) -> tuple[str, str]:
    """Return ``(unavailable_reason, reset_at)`` for a route about to run ``shape``.

    One reader, so the answer the DISPATCHER acts on and the answer the nanny's own
    ``delegate_start`` gets cannot drift into disagreeing about the same route. Health
    is asked about the SHAPE, not about a route in the abstract: a route that can only
    read is not a usable substrate for a child that must write, and an ENGINE that
    would reject the delegated marker outright is not a usable substrate for one either.

    ``route_model`` is the route's pinned model (``DelegationRoute.model``): quota
    windows scoped to OTHER models must not take this route offline, so exhaustion is
    judged against the model the run would actually use. A full-window exhaustion that
    names no reset instant still reports ``subscription_window_exhausted`` — as the
    REASON with an empty ``reset_at``, since an unknown healing time is not health.
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
    if not entry.get("enabled"):
        return "route_status_disabled", ""
    if str(entry.get("status") or "") != "ok":
        # A harness the engine will not route BY DEFAULT is not necessarily a
        # harness that cannot run: a profile-only adapter reports `unavailable`
        # for as long as it exists, because the status answers "is there a
        # default credential", and by design there never is one. Its named
        # account is the substrate, and the engine verifies that separately.
        # Reading only the status refused a route whose account the engine had
        # just confirmed working.
        if not routable_profile(gateway, route_id):
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
    exhausted, reset_at = _exhausted_window(gateway, route_id, route_model)
    if exhausted and not reset_at:
        # Spent with no named healing instant: still spent. The old shape carried
        # exhaustion ONLY in a non-empty reset, so a window the harness reports as
        # fully used but undated read back as a healthy route and the child was
        # dispatched onto a substrate that was going to refuse it.
        return "subscription_window_exhausted", ""
    return "", reset_at


def _model_scope_matches(route_model: str, applies_to_models: Any) -> bool:
    """Does a quota constraint's model scope cover the route's pinned model?

    An empty/absent scope is a GLOBAL window — it always applies. An unpinned route
    (no model in ``OUROBOROS_SUBAGENT_HARNESS``) can land on any model, so every
    scoped window applies to it too. Otherwise the scope's aliases are matched by
    case-insensitive containment either way ("opus" ↔ "claude-opus-5"): the harness
    names windows by its own alias vocabulary, which this module must not enumerate.
    """
    aliases = [str(a).strip().lower() for a in (applies_to_models or []) if str(a).strip()]
    if not aliases:
        return True
    model = str(route_model or "").strip().lower()
    if not model:
        return True
    return any(a == model or a in model or model in a for a in aliases)


def _exhausted_window(gateway: Any, route_id: str, route_model: str = "") -> tuple[bool, str]:
    """``(exhausted, reset_at)`` for a route judged against its OWN model.

    A window counts as spent when the harness reports it fully used or explicitly
    cooling down AND its model scope covers the route's model — a window scoped to a
    model this route never uses (the live incident: a Fable-only weekly window taking
    an opus-pinned route offline for days) is someone else's exhaustion, not this
    route's. Stale snapshots are ignored — an old reading must not block a lane.

    ANY LIVE SNAPSHOT MEANS THE LANE IS USABLE (D28). And exhaustion needs POSITIVE
    evidence for the WHOLE route: a profile whose quota could not be read at all
    (absent — a 429 on the usage endpoint, a failed refresh) is UNKNOWN, not spent,
    so it fail-opens the route: the daemon owns rotation and answers a genuinely
    empty route with its own typed refusal at start time, which costs nothing here.
    Only when every readable profile is spent and none is unreadable is there
    something to wait for; the honest instant is the EARLIEST named reset (possibly
    none — spent windows are not obliged to carry one).

    A snapshot with an applicable spent constraint counts as spent even if another of
    ITS OWN constraints has room: a 5-hour window at 100% blocks that profile now,
    whatever its weekly window says. WHICH profile a run lands on is Claudexor's
    business — rotation stays there and no profile identity is interpreted here.
    """
    resets: List[str] = []
    any_live = False
    any_spent = False
    for snapshot in gateway.quota_snapshots():
        subject = snapshot.get("subject") if isinstance(snapshot.get("subject"), dict) else {}
        if str(subject.get("harness") or "") != route_id:
            continue
        if str(snapshot.get("freshness") or "") != "fresh":
            continue
        spent_here = [
            (str(c.get("cooldown_until") or "") or str(c.get("resets_at") or ""))
            for c in (snapshot.get("constraints") or [])
            if isinstance(c, dict)
            and (bool(c.get("cooldown_until"))
                 or (isinstance(c.get("used_ratio"), (int, float))
                     and float(c.get("used_ratio")) >= 1.0))
            and _model_scope_matches(route_model, c.get("applies_to_models"))
        ]
        if spent_here:
            any_spent = True
            resets.extend(reset for reset in spent_here if reset)
        else:
            any_live = True
    if any_live or not any_spent:
        return False, ""
    absences = getattr(gateway, "quota_absences", None)
    if callable(absences):
        for row in absences() or []:
            subject = row.get("subject") if isinstance(row, dict) else None
            if isinstance(subject, dict) and str(subject.get("harness") or "") == route_id:
                return False, ""
    return True, min(resets) if resets else ""


def _pinned_for_profile_only_route(gateway: Any, route: "DelegationRoute") -> "DelegationRoute":
    """Pin the named account for a route the engine will not route by itself.

    Narrow by construction: it fires only when the harness status is NOT ok yet
    the route passed health, which is exactly the profile-only case
    (``routable_profile`` is what let it pass). A healthy harness keeps an empty
    pin, so the daemon's own rotation policy — the documented default (D28) —
    is never overridden for the routes that have one.
    """
    from dataclasses import replace

    if route.profile_id:
        # An owner who named an account is never second-guessed. The caller also
        # short-circuits on this, to skip the catalog read — but the contract
        # belongs to the function, or the next caller re-learns it the hard way.
        return route
    try:
        catalog = gateway.agent_capabilities()
        entry = next(
            (row for row in catalog.get("harnesses") or []
             if isinstance(row, dict) and str(row.get("id") or "") == route.route_id),
            None,
        )
        if entry is None or str(entry.get("status") or "") == "ok":
            return route
        profile = routable_profile(gateway, route.route_id)
        return replace(route, profile_id=profile) if profile else route
    except Exception:
        log.debug("profile pin resolution failed for %s", route.route_id, exc_info=True)
        return route
