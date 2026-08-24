"""The owner-approved execution allocation for ONE task tree.

A big task is not one piece of work on one substrate: the owner may want the
frontend built by the strong implementation agent, the tests run by the fast
scout, and the research done by an independent second opinion. WHICH agents
exist is the owner's standing configuration (``configured_subagents`` — the
Available-subagents catalog in Settings). WHICH ONE each piece of a particular
task gets is the decision this record holds, taken once, in the moment, with the
cost and time evidence in front of the owner.

So a row here is deliberately NOT a route: it is a reference to a catalog row by
``subagent_id``. The route, model, effort and credential pin all come from that
row and are snapshotted onto the child by the scheduler, exactly as a directly
chosen ``subagent_id`` would be — this record only says which row applies to
which piece of work. One vocabulary for "where work can run", not two.

MALFORMED RAISES, item-precise — the same posture and the same reason as
``configured_subagents`` and ``reviewer_slot_config``: coercing a typo would run
a piece of work on an agent the owner never picked for it.
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from ouroboros.task_results import validate_task_id
from ouroboros.utils import atomic_write_json, utc_now_iso

log = logging.getLogger(__name__)

ROUTING_PLAN_FILENAME = "routing_plan.json"
ROUTING_PLAN_VERSION = 2

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
# The catalog's own id grammar (`configured_subagents._ID_RE`), bounded the same.
_SUBAGENT_ID_MAX_CHARS = 64


@dataclass(frozen=True)
class RoutingPlanItem:
    """One approved piece of work and which configured subagent runs it."""

    item_id: str
    title: str
    subagent_id: str
    source: str = SOURCE_RECOMMENDED

    def as_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "subagent_id": self.subagent_id,
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
        reference, not a malformed plan: it falls back to the ordinary
        subagent selection and the fallback is disclosed. Raising here would
        kill a task tree over a typo in one child.
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
            "could not tell which agent the owner meant"
        )
    seen_ids.add(item_id)
    subagent_id = _bounded(raw.get("subagent_id"), _SUBAGENT_ID_MAX_CHARS, "subagent_id", where)
    if not subagent_id:
        raise ValueError(
            f"routing plan: {where} needs a subagent_id from the Available-subagents "
            "catalog — this record references a configured row, it does not define a route"
        )
    source = str(raw.get("source") or SOURCE_RECOMMENDED).strip().lower()
    if source not in _SOURCES:
        raise ValueError(
            f"routing plan: {where} names an unknown source {source!r}; "
            f"valid: {', '.join(_SOURCES)}"
        )
    return RoutingPlanItem(
        item_id=item_id,
        title=_bounded(raw.get("title"), _TITLE_MAX_CHARS, "title", where),
        subagent_id=subagent_id,
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
        # A version this build does not read is refused rather than read
        # optimistically: v1 rows carried their own route, and reading one as a
        # catalog reference would dispatch work to whatever id that text matched.
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
    treats them differently: no plan means "select the subagent the ordinary
    way", while an unreadable one means the owner's decision cannot be honoured
    and no work may be dispatched guessing at it.
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


def planned_subagent_id(
    root_id: Any, item_id: Any, *, data_root: pathlib.Path | None = None,
) -> str:
    """The configured subagent the owner approved for one item ('' when none).

    The ONE reader the schedule path uses, so "which agent did the owner pick
    for this child" has a single answer no caller re-derives.
    """
    plan = load_routing_plan(root_id, data_root=data_root)
    if plan is None:
        return ""
    entry = plan.item(item_id)
    return entry.subagent_id if entry is not None else ""
