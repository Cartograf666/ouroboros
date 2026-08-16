"""Shared review-cycle cap — SSOT for ``OUROBOROS_REVIEW_MAX_CYCLES``.

ONE owner-facing knob bounds how many PAID review cycles each review gate may
spend before it stops and discloses (owner decisions D9/D10/D19/D20/D26-D28).
The setting is a STRING — a positive integer ("1", "2", "5", …) or
``"unlimited"`` (aliases ``inf``/``∞``); anything else fails closed to the
shipped default with one loud log line. It is a string on purpose:
``config._coerce_setting_value`` casts by the default's type, so an ``int``
default would silently swallow ``"unlimited"``. ``review_max_cycles()`` returns
``Optional[int]`` — ``None`` means unlimited.

Per-gate meaning of the ONE number:

* plan review — paid reviewer-panel cycles per task (the engine consumes the
  getter; this module only exposes it);
* task acceptance — paid panel runs per task, ``passes = cycles - 1``
  (``acceptance_max_improvement_passes_from_cycles``), so the default 2 equals
  the historical default of 1 improvement pass; unlimited → None;
* commit gate — consecutive review-blocks on a BYTE-IDENTICAL staged diff before
  the identical-diff attempt cap refuses another triad+scope run. Semantics
  unchanged (a changed diff starts a fresh streak); only the number's source
  changed, and the default moved 3 → 2 (owner-approved, disclosed).

Values are read from ``os.environ`` (``config.apply_settings_to_env`` projects
saved settings there) falling back to ``SETTINGS_DEFAULTS`` — no second
mechanism. The deprecated alias ``OUROBOROS_ACCEPTANCE_MAX_IMPROVEMENT_PASSES`` does not bind
at runtime at all: a customized value is MIGRATED into this knob when settings load
(``config.load_settings``, the same rename-alias shape the retention keys use), so the
visible setting is always the authority. Residual, disclosed: a legacy value supplied
only through the environment is not migrated and no longer binds. No new state files; the only other side effect is
``emit_review_cycles_exhausted``, the typed D27 escalation event on the existing
``log_event``/``events.jsonl`` rail.
"""

from __future__ import annotations

import logging
import os
import pathlib
from typing import Any, Optional

from ouroboros.config import SETTINGS_DEFAULTS
# The ONE typed reason/event name for "the shared cap is spent under blocking
# enforcement" (owner D10/D27) — SSOT beside the acceptance-decision vocabulary.
from ouroboros.outcomes import REASON_REVIEW_CYCLES_EXHAUSTED  # noqa: F401 — re-export
from ouroboros.utils import append_jsonl, emit_log_event, utc_now_iso

log = logging.getLogger(__name__)

REVIEW_MAX_CYCLES_KEY = "OUROBOROS_REVIEW_MAX_CYCLES"
# Deprecated alias (task acceptance only). Kept as a settings key so an explicit
# owner customization keeps binding; removal is a separate owner decision.
ACCEPTANCE_PASSES_LEGACY_KEY = "OUROBOROS_ACCEPTANCE_MAX_IMPROVEMENT_PASSES"
# Canonical persisted token for "no cap"; the UI's ∞ choice saves this string.
UNLIMITED = "unlimited"
# "none" is deliberately NOT an alias: it reads as "no cycles" as easily as "no cap".
UNLIMITED_ALIASES = frozenset({UNLIMITED, "inf", "∞"})
# Legacy passes clamp, unchanged from the former config.py getter.

_WARNED: set = set()


def _warn_once(tag: str, message: str) -> None:
    if tag in _WARNED:
        return
    _WARNED.add(tag)
    log.warning(message)


def parse_review_max_cycles(raw: Any) -> Optional[int]:
    """Strict parser: positive-integer text → int; an unlimited alias → None.

    Raises ``ValueError`` for anything else (empty, zero, negative, non-integer,
    unknown word) so callers decide between fail-closed default and 400."""
    text = str(raw if raw is not None else "").strip().lower()
    if text in UNLIMITED_ALIASES:
        return None
    if not text:
        raise ValueError("empty review-cycle cap")
    value = int(text)  # ValueError on non-integer text (incl. "true"/"1.5")
    if value < 1:
        raise ValueError(f"review-cycle cap must be a positive integer, got {value}")
    return value


def is_valid_review_max_cycles(raw: Any) -> bool:
    """SSOT predicate for the settings write boundary (mirrors
    ``config.is_valid_post_task_evolution_cadence``): True iff ``raw`` is a
    positive integer or an unlimited alias."""
    try:
        parse_review_max_cycles(raw)
    except (TypeError, ValueError):
        return False
    return True


def normalize_review_max_cycles(raw: Any) -> str:
    """Canonical persisted form of a VALID value: ``"unlimited"`` for every
    alias, else the integer text. Callers validate first (``is_valid_...``)."""
    parsed = parse_review_max_cycles(raw)
    return UNLIMITED if parsed is None else str(parsed)


def default_review_max_cycles() -> int:
    """The shipped default as an int (the default is bounded by construction)."""
    return int(str(SETTINGS_DEFAULTS[REVIEW_MAX_CYCLES_KEY]))


def review_max_cycles() -> Optional[int]:
    """The shared cap: ``None`` = unlimited, else a positive int.

    Env-or-default like every other getter; a malformed value fails CLOSED to
    the shipped default (bounded) and is reported once per process."""
    default_text = str(SETTINGS_DEFAULTS[REVIEW_MAX_CYCLES_KEY])
    raw = os.environ.get(REVIEW_MAX_CYCLES_KEY, "") or default_text
    try:
        return parse_review_max_cycles(raw)
    except (TypeError, ValueError):
        _warn_once(
            f"invalid:{raw!r}",
            f"{REVIEW_MAX_CYCLES_KEY}={raw!r} is not a positive integer or "
            f"'unlimited'; using the shipped default {default_text} (bounded).",
        )
        return default_review_max_cycles()


def acceptance_max_improvement_passes_from_cycles() -> Optional[int]:
    """Pure formula: task-acceptance improvement passes = shared cycles - 1
    (2 cycles → 1 pass); ``None`` when the shared cap is unlimited."""
    cycles = review_max_cycles()
    return None if cycles is None else max(0, cycles - 1)


def get_acceptance_max_improvement_passes() -> Optional[int]:
    """Acceptance improvement-pass cap = the shared review-cycle cap minus one.

    The deprecated ``OUROBOROS_ACCEPTANCE_MAX_IMPROVEMENT_PASSES`` no longer binds at runtime:
    a customized value is MIGRATED into the shared knob when settings load (``config``), the
    same rename-alias shape the retention keys use. Disclosed residual: a legacy value supplied
    only through the environment (never saved) is not migrated and no longer binds."""
    return acceptance_max_improvement_passes_from_cycles()

def emit_review_cycles_exhausted(
    event_queue: Any, drive_root: Any, *, surface: str, task_id: str,
    cycles_paid: int, cap: int, enforcement: str, **extra: Any,
) -> None:
    """The typed escalation event (D27): queue when live, else durable append.

    Reuses the existing ``log_event`` emitter / ``events.jsonl`` — no new ledger."""
    row = {"type": REASON_REVIEW_CYCLES_EXHAUSTED, "surface": str(surface), "task_id": str(task_id or ""),
           "cycles_paid": int(cycles_paid), "cap": int(cap), "enforcement": str(enforcement or ""), **extra}
    try:
        if event_queue is not None:
            emit_log_event(event_queue, {"ts": utc_now_iso(), **row}, log_label="review cycles")
        elif drive_root:
            append_jsonl(pathlib.Path(str(drive_root)) / "logs" / "events.jsonl", {"ts": utc_now_iso(), **row})
    except Exception:
        log.debug("review_cycles_exhausted emission failed for %s", task_id, exc_info=True)
