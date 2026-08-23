"""What a piece of work can actually run ON, in ONE vocabulary.

Ouroboros can execute a child in two ways that used to be described in two
different languages: on a metered API model (a model id, possibly served by the
local GGUF lane) or on a subscription harness through Claudexor (an opaque route
id). Every surface that had to offer BOTH — the reviewer rows first, and now the
owner's per-task allocation — needs one list, so this module answers the single
question «what may I choose from right now», in the reviewer-slot spelling
``{kind: api_chat | agent_session, target_id}``.

It DERIVES; it decides nothing. Availability comes from the readers that already
own it — ``route_health`` for a delegated route, ``provider_has_credentials``
for an API one — because a second opinion about whether a route is usable is how
a dispatcher and a picker end up disagreeing about the same route.

There is no harness NAME anywhere in here: session rows are whatever the engine
lists, spelled by the engine's own ``displayName``. A fourth (or fifth) family
appears with no change to this file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.reviewer_slot_config import ROUTE_KIND_API, ROUTE_KIND_SESSION
from ouroboros.routing_plan import RoutePin

log = logging.getLogger(__name__)

# The lane slots an api_chat row can come from, and the local-routing flag that
# decides whether that slot runs on the machine rather than a provider. The
# pairing is `subagents._LANE_SLOT_KEYS`' fact; it is imported rather than
# retyped so a renamed slot cannot mean two things.
_LANE_ORDER: Tuple[str, ...] = ("main", "heavy", "light")

# Why a target cannot be chosen. Typed so the UI and the proposal validator say
# the same words, and so a refusal is never free prose.
REASON_NO_CREDENTIALS = "provider_has_no_credentials"
REASON_NOT_IN_CATALOG = "route_not_in_capability_catalog"
REASON_DAEMON_UNREACHABLE = "claudexor_unreachable"


@dataclass(frozen=True)
class ExecutionTarget:
    """One choosable destination.

    ``available`` is the answer for the NARROWEST run (a read-only child). A
    mutating item is re-checked against its real shape at dispatch by
    ``route_health`` — this row is a picker's view, never an admission.
    """

    kind: str
    target_id: str
    label: str
    available: bool = True
    unavailable_reason: str = ""
    # api_chat only
    lane: str = ""
    provider: str = ""
    use_local: bool = False
    # agent_session only
    status: str = ""
    access_profiles: Tuple[str, ...] = ()
    reset_at: str = ""
    models: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "kind": self.kind,
            "target_id": self.target_id,
            "label": self.label,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }
        if self.kind == ROUTE_KIND_API:
            row.update({"lane": self.lane, "provider": self.provider,
                        "use_local": self.use_local})
        else:
            row.update({"status": self.status,
                        "access_profiles": list(self.access_profiles),
                        "reset_at": self.reset_at,
                        "models": list(self.models)})
        return row


@dataclass
class ExecutionTargetCatalog:
    """Both families plus the provenance of the delegated read.

    ``session_read`` is the disclosure that matters: an EMPTY session list with
    ``session_read="failed"`` means "we could not ask", which is not the same
    answer as "you have no harnesses" — the distinction the accounts panel
    already makes per facet, kept here for the same reason.
    """

    api_chat: List[ExecutionTarget] = field(default_factory=list)
    agent_session: List[ExecutionTarget] = field(default_factory=list)
    session_read: str = "not_read"  # ok | not_read | failed
    session_error: str = ""

    def all_targets(self) -> List[ExecutionTarget]:
        return [*self.api_chat, *self.agent_session]

    def find(self, kind: str, target_id: str) -> Optional[ExecutionTarget]:
        wanted_kind = str(kind or "").strip().lower()
        wanted_id = str(target_id or "").strip()
        for row in self.all_targets():
            if row.kind == wanted_kind and row.target_id == wanted_id:
                return row
        return None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "api_chat": [row.as_dict() for row in self.api_chat],
            "agent_session": [row.as_dict() for row in self.agent_session],
            "session_read": self.session_read,
            "session_error": self.session_error,
        }


def _lane_label(lane: str, model: str, use_local: bool) -> str:
    """What the owner is choosing, in their own words.

    The lane is named because that is what the owner configured in Settings, and
    the model beside it because two lanes can hold the same model. `(local)` is
    the existing spelling for a slot the machine serves — the one
    `provider_for_model` already recognises — so nothing new is invented here.
    """
    suffix = " (local)" if use_local else ""
    return f"{lane.capitalize()} · {model}{suffix}" if model else lane.capitalize()


def api_chat_targets() -> List[ExecutionTarget]:
    """The owner's configured model lanes, as choosable rows.

    The lanes are the honest list: a model is reachable exactly when the owner
    has configured it in a slot with credentials (or pointed the slot at the
    local runtime). Enumerating a provider's whole catalogue here would offer
    hundreds of rows nobody configured — the same finding that put routes, not
    models, in the reviewer-row select.
    """
    from ouroboros.provider_models import provider_for_model, provider_has_credentials
    from ouroboros.subagents import _lane_model, _use_local_for_lane

    rows: List[ExecutionTarget] = []
    seen: set = set()
    for lane in _LANE_ORDER:
        model = str(_lane_model(lane) or "").strip()
        if not model or model in seen:
            # An empty Heavy/Light slot IS the Main model; listing it twice would
            # offer the owner a choice that is not one.
            continue
        seen.add(model)
        use_local = bool(_use_local_for_lane(lane, model))
        provider = "local" if use_local else provider_for_model(model)
        has_creds = True if use_local else provider_has_credentials(provider)
        rows.append(ExecutionTarget(
            kind=ROUTE_KIND_API,
            target_id=model,
            label=_lane_label(lane, model, use_local),
            available=has_creds,
            unavailable_reason="" if has_creds else REASON_NO_CREDENTIALS,
            lane=lane,
            provider=provider,
            use_local=use_local,
        ))
    return rows


def _session_row(
    gateway: Any, row: Dict[str, Any], *, include_models: bool,
) -> ExecutionTarget:
    from ouroboros.gateways.claudexor import ClaudexorUnavailable
    from ouroboros.subagents import delegated_run_shape, route_health

    route_id = str(row.get("id") or "")
    label = str(row.get("displayName") or row.get("display_name") or route_id)
    # Health is asked of the READ-ONLY shape: it is the weakest run there is, so
    # a route that fails it fails everything. The acting shape is re-checked at
    # dispatch against the child's real authority.
    try:
        reason, reset_at = route_health(gateway, route_id, delegated_run_shape(False))
    except ClaudexorUnavailable as exc:
        reason, reset_at = exc.code, str(getattr(exc, "reset_at", "") or "")
    models: Tuple[str, ...] = ()
    if include_models and route_id and not reason:
        try:
            models = tuple(
                str(entry.get("id") or entry.get("name") or "").strip()
                for entry in gateway.harness_models(route_id)
                if isinstance(entry, dict)
            )
            models = tuple(name for name in models if name)
        except ClaudexorUnavailable as exc:
            log.debug("harness models unreadable for %s: %s", route_id, exc.code)
    return ExecutionTarget(
        kind=ROUTE_KIND_SESSION,
        target_id=route_id,
        label=label,
        available=not reason,
        unavailable_reason=reason,
        status=str(row.get("status") or ""),
        access_profiles=tuple(str(v) for v in row.get("accessProfilesSupported") or []),
        reset_at=reset_at,
        models=models,
    )


def session_targets(*, include_models: bool = False) -> Tuple[List[ExecutionTarget], str, str]:
    """Every delegated route the engine lists. Returns ``(rows, read, error)``.

    One daemon conversation for the whole list — the catalog once, then health
    per row off the same gateway — so a picker costs the owner one round trip,
    not one per harness.
    """
    from ouroboros.claudexor_daemon import ensure_owned_gateway
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    gateway = None
    try:
        gateway = ensure_owned_gateway()
        catalog = gateway.agent_capabilities()
        rows = [
            _session_row(gateway, row, include_models=include_models)
            for row in (catalog.get("harnesses") or [])
            if isinstance(row, dict) and str(row.get("id") or "")
        ]
        return rows, "ok", ""
    except ClaudexorUnavailable as exc:
        # An unreachable daemon is a GAP, never an empty catalog: the caller must
        # be able to say "could not ask" instead of "you have none".
        return [], "failed", f"{exc.code}: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("execution targets: session read failed", exc_info=True)
        return [], "failed", f"{type(exc).__name__}: {exc}"
    finally:
        if gateway is not None:
            gateway.close()


def execution_targets(*, include_models: bool = False) -> ExecutionTargetCatalog:
    """The whole choosable set, both families, with the delegated read's provenance."""
    rows, read, error = session_targets(include_models=include_models)
    return ExecutionTargetCatalog(
        api_chat=api_chat_targets(),
        agent_session=rows,
        session_read=read,
        session_error=error,
    )


def validate_pin(
    pin: RoutePin, *, catalog: Optional[ExecutionTargetCatalog] = None,
) -> str:
    """'' when this destination is choosable, else the typed reason it is not.

    Used BEFORE the owner ever sees a proposal, so a row they cannot act on is
    never rendered as a choice. It answers about the narrowest run for the same
    reason ``_session_row`` does: the child's real shape is re-checked at
    dispatch, where refusing costs nothing yet.
    """
    catalog = catalog if catalog is not None else execution_targets()
    if pin.kind == ROUTE_KIND_SESSION and catalog.session_read != "ok":
        # Refusing on an UNREAD catalog is the safe direction here: approving a
        # delegated row nobody could confirm would park the run on a route the
        # engine may not carry, after the owner has already signed off on it.
        return catalog.session_error or REASON_DAEMON_UNREACHABLE
    row = catalog.find(pin.kind, pin.target_id)
    if row is None:
        if pin.kind == ROUTE_KIND_API:
            # An api target the owner has not put in a slot is still legitimate —
            # they may pin any model whose provider is credentialed, exactly as a
            # reviewer row's free-text model field allows.
            from ouroboros.provider_models import (
                provider_for_model,
                provider_has_credentials,
            )

            provider = provider_for_model(pin.target_id)
            return "" if provider_has_credentials(provider) else REASON_NO_CREDENTIALS
        return REASON_NOT_IN_CATALOG
    return "" if row.available else (row.unavailable_reason or REASON_NOT_IN_CATALOG)
