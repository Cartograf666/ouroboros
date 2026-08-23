"""The owner-approved execution allocation for ONE task tree.

A big task is not one piece of work on one substrate: the owner may want the
frontend built on a subscription harness, the tests run on a local model and the
research done on a cheap metered one. That decision is the OWNER'S, and this
module is the durable record of it — one file per root task, written once the
owner approves (or edits) the allocation the agent proposed.

WHY THIS IS NOT A FOURTH SUBAGENT AXIS. A parent declares the WORK
(``write_surface``, ``model_lane``, ``executor``) and never the machinery — the
route, model and effort are consequences of the OWNER'S settings plus what is
live (see ``ouroboros/subagents.py``). An approved plan is exactly such an owner
setting, only scoped to one task tree instead of the whole install: the parent
names WHICH approved item a child is, and the route it lands on comes from the
owner, the same way ``OUROBOROS_SUBAGENT_HARNESS`` supplies it today. Nothing
here lets the model widen its own reach.

MALFORMED RAISES, item-precise — the same posture and the same reason as
``reviewer_slot_config``: coercing a typo either spends metered money the owner
deliberately moved off, or delegates a piece of work they never delegated.

The route vocabulary is NOT redefined here. ``{kind: api_chat | agent_session,
target_id}`` already exists as the reviewer-slot spelling and is imported from
its owner, so an install cannot end up with two dialects of the same fact.
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from ouroboros.reviewer_slot_config import (
    ROUTE_KIND_API,
    ROUTE_KIND_SESSION,
)
from ouroboros.task_results import validate_task_id
from ouroboros.utils import atomic_write_json, utc_now_iso

log = logging.getLogger(__name__)

ROUTING_PLAN_FILENAME = "routing_plan.json"
ROUTING_PLAN_VERSION = 1

_ROUTE_KINDS = (ROUTE_KIND_API, ROUTE_KIND_SESSION)

# Where an approved row came from, so a receipt can tell "the owner took my
# recommendation" from "the owner overruled me" — the difference the routing
# evidence needs in order to be evidence about the OWNER'S judgment too.
SOURCE_RECOMMENDED = "owner_accepted_recommendation"
SOURCE_EDITED = "owner_edit"
_SOURCES = (SOURCE_RECOMMENDED, SOURCE_EDITED)

# Bounds. An allocation is a human decision about a handful of pieces of work;
# these are the sizes a person can actually read in a card, not storage limits.
MAX_ITEMS = 32
_ITEM_ID_MAX_CHARS = 64
_TITLE_MAX_CHARS = 200
_TARGET_MAX_CHARS = 200


@dataclass(frozen=True)
class RoutePin:
    """One approved destination: the reviewer-slot route plus its optional pins.

    ``model`` is meaningful on BOTH kinds and means the same thing on each — the
    model the owner pinned for this piece of work: the API model id on
    ``api_chat``, the harness's own model on ``agent_session`` (where it also
    rides the ``harness=model`` spelling). ``profile_id`` is the manual
    credential pin and is session-only, exactly as on a reviewer row.
    """

    kind: str
    target_id: str
    model: str = ""
    profile_id: str = ""

    @property
    def is_session(self) -> bool:
        return self.kind == ROUTE_KIND_SESSION

    def route_spec(self) -> str:
        """The opaque Claudexor route spec (``harness[=model]``), '' for api rows.

        The spelling is Claudexor's own — the same one ``parse_subagent_harness``
        reads — so a pinned session row reaches dispatch through the existing
        route parser instead of a second grammar.
        """
        if not self.is_session:
            return ""
        return f"{self.target_id}={self.model}" if self.model else self.target_id

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "target_id": self.target_id,
            "model": self.model,
            "profile_id": self.profile_id,
        }


@dataclass(frozen=True)
class RoutingPlanItem:
    """One approved piece of work and where the owner said it runs."""

    item_id: str
    title: str
    route: RoutePin
    source: str = SOURCE_RECOMMENDED

    def as_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "route": self.route.as_dict(),
            "source": self.source,
        }


@dataclass(frozen=True)
class RoutingPlan:
    """The whole approved allocation for one root task."""

    root_task_id: str
    items: Tuple[RoutingPlanItem, ...]
    approved_at: str = ""
    version: int = ROUTING_PLAN_VERSION

    def item(self, item_id: Any) -> Optional[RoutingPlanItem]:
        """The approved item by id, or None — an unknown id is never an error.

        A child naming an id this plan does not carry is a STALE or mistaken
        reference, not a malformed plan: it falls back to the install-wide
        policy and the fallback is disclosed on the child's capability delta.
        Raising here would kill a task tree over a typo in one child.
        """
        wanted = str(item_id or "").strip()
        if not wanted:
            return None
        for entry in self.items:
            if entry.item_id == wanted:
                return entry
        return None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "root_task_id": self.root_task_id,
            "approved_at": self.approved_at,
            "approved_by": "owner",
            "items": [entry.as_dict() for entry in self.items],
        }


def routing_plan_path(
    root_id: str, *, data_root: pathlib.Path | None = None,
) -> pathlib.Path:
    """``<data>/task_trees/<root>/routing_plan.json``.

    Beside the tree's coordination ledger, because it is scoped to exactly the
    same thing: ONE swarm run. ``validate_task_id`` raises on a malformed id, so
    a typo can never build a bogus path (the ledger's own rule).
    """
    from ouroboros.config import DATA_DIR

    root = pathlib.Path(data_root) if data_root is not None else pathlib.Path(DATA_DIR)
    return root / "task_trees" / validate_task_id(root_id) / ROUTING_PLAN_FILENAME


def _bounded(value: Any, limit: int, field: str, where: str) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise ValueError(f"routing plan: {where} {field} exceeds {limit} characters")
    return text


def parse_route_pin(raw: Any, where: str) -> RoutePin:
    """Strict parse of ONE ``{kind, target_id, model?, profile_id?}``."""
    if not isinstance(raw, dict):
        raise ValueError(f"routing plan: {where} route must be an object "
                         "{kind, target_id}")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in _ROUTE_KINDS:
        raise ValueError(
            f"routing plan: {where} names an unknown route kind {kind!r}; "
            f"valid: {', '.join(_ROUTE_KINDS)}"
        )
    target = _bounded(raw.get("target_id"), _TARGET_MAX_CHARS, "target_id", where)
    if not target:
        raise ValueError(f"routing plan: {where} route.target_id is empty")
    if kind == ROUTE_KIND_SESSION and "::" in target:
        # The harness IS the provider on a delegated row, so the `provider::model`
        # spelling has no meaning there — the same refusal a reviewer row gives.
        raise ValueError(
            f"routing plan: {where} session target {target!r} uses '::' — a "
            "delegated row is spelled harness[=model]"
        )
    if kind == ROUTE_KIND_SESSION and "=" in target:
        # The model has its own field. Accepting it inside `target_id` too would
        # give one fact two carriers that can disagree.
        raise ValueError(
            f"routing plan: {where} session target {target!r} carries '=' — put "
            "the harness model in the route's own `model` field"
        )
    model = _bounded(raw.get("model"), _TARGET_MAX_CHARS, "model", where)
    profile = _bounded(raw.get("profile_id"), _TARGET_MAX_CHARS, "profile_id", where)
    if profile and kind != ROUTE_KIND_SESSION:
        raise ValueError(
            f"routing plan: {where} pins a credential profile on an {kind} route; "
            "profiles exist only on delegated (agent_session) routes"
        )
    return RoutePin(kind=kind, target_id=target, model=model, profile_id=profile)


def _parse_item(raw: Any, where: str, seen_ids: set) -> RoutingPlanItem:
    if not isinstance(raw, dict):
        raise ValueError(f"routing plan: {where} is not an object")
    item_id = _bounded(raw.get("item_id"), _ITEM_ID_MAX_CHARS, "item_id", where)
    if not item_id:
        raise ValueError(
            f"routing plan: {where} needs a stable non-empty item_id "
            f"(≤{_ITEM_ID_MAX_CHARS} chars) — a child references it by name, "
            "never by position"
        )
    if item_id in seen_ids:
        raise ValueError(
            f"routing plan: item_id {item_id!r} appears twice; a child naming it "
            "could not tell which destination the owner meant"
        )
    seen_ids.add(item_id)
    source = str(raw.get("source") or SOURCE_RECOMMENDED).strip().lower()
    if source not in _SOURCES:
        raise ValueError(
            f"routing plan: {where} names an unknown source {source!r}; "
            f"valid: {', '.join(_SOURCES)}"
        )
    return RoutingPlanItem(
        item_id=item_id,
        title=_bounded(raw.get("title"), _TITLE_MAX_CHARS, "title", where),
        route=parse_route_pin(raw.get("route"), where),
        source=source,
    )


def parse_routing_plan(payload: Any, *, root_task_id: str = "") -> RoutingPlan:
    """Strict parse of a plan object (or its JSON text). Raises ValueError."""
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise ValueError(f"routing plan is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("routing plan must be a JSON object")
    version = payload.get("version")
    if int(version or 0) != ROUTING_PLAN_VERSION:
        # A future version is refused rather than read optimistically: an
        # unknown shape that happens to parse would route real money by guess.
        raise ValueError(
            f"routing plan version {version!r} is not supported "
            f"(this build reads version {ROUTING_PLAN_VERSION})"
        )
    stored_root = str(payload.get("root_task_id") or "").strip()
    wanted_root = str(root_task_id or "").strip()
    if wanted_root and stored_root and stored_root != wanted_root:
        raise ValueError(
            f"routing plan belongs to root {stored_root!r}, not {wanted_root!r}"
        )
    rows = payload.get("items")
    if not isinstance(rows, list) or not rows:
        raise ValueError("routing plan: items must be a non-empty list")
    if len(rows) > MAX_ITEMS:
        raise ValueError(f"routing plan: at most {MAX_ITEMS} items ({len(rows)} given)")
    seen: set = set()
    items = tuple(
        _parse_item(row, f"item[{index}]", seen) for index, row in enumerate(rows)
    )
    return RoutingPlan(
        root_task_id=stored_root or wanted_root,
        items=items,
        approved_at=str(payload.get("approved_at") or ""),
        version=ROUTING_PLAN_VERSION,
    )


def write_routing_plan(
    plan: RoutingPlan, *, data_root: pathlib.Path | None = None,
) -> pathlib.Path:
    """Persist an approved plan atomically. Raises on an unwritable root."""
    if not plan.items:
        raise ValueError("routing plan: refusing to write an empty allocation")
    path = routing_plan_path(plan.root_task_id, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = plan.as_dict()
    payload["approved_at"] = plan.approved_at or utc_now_iso()
    atomic_write_json(path, payload)
    return path


def load_routing_plan(
    root_id: Any, *, data_root: pathlib.Path | None = None,
) -> Optional[RoutingPlan]:
    """The approved plan for a tree, or None when there is none.

    ABSENCE is None; MALFORMED raises. Those are different facts and the caller
    treats them differently: no plan means "use the install-wide policy", while
    an unreadable one means the owner's decision cannot be honoured and no money
    may be spent guessing at it.
    """
    try:
        path = routing_plan_path(str(root_id or ""), data_root=data_root)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"routing plan at {path} could not be read: {exc}") from exc
    return parse_routing_plan(raw, root_task_id=str(root_id or ""))


def plan_pin_for_item(
    root_id: Any, item_id: Any, *, data_root: pathlib.Path | None = None,
) -> Optional[RoutePin]:
    """The approved destination for one item, or None when nothing applies.

    The ONE reader the schedule path uses, so "which route did the owner approve
    for this child" has a single answer no caller re-derives.
    """
    plan = load_routing_plan(root_id, data_root=data_root)
    if plan is None:
        return None
    entry = plan.item(item_id)
    return entry.route if entry is not None else None
