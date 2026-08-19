#!/usr/bin/env python3
"""Shared single-model benchmark helper.

A single-model benchmark run pins every model slot to one model and lightens the
review triad to ``review_slots`` copies of that model (default 1). Three identical
reviewers add latency/cost but no diversity, and a single-model run cannot achieve
reviewer-model diversity anyway; the loud ``single_reviewer_no_diversity`` signal
stays on. This is a BENCHMARK convenience, NOT a claim that review got more reliable.

Delegation has the same purity boundary: the run gets one explicit ``api_model``
Available-subagent row on the measured model. It never inherits install defaults,
an API scout, or a session route. Construction and serialization deliberately use
the runtime's canonical ``OUROBOROS_SUBAGENTS`` encoder rather than a benchmark copy.

Generalized here so the SWE-bench Pro adapter (which builds a settings DICT written
to the container's settings.json) and Terminal-Bench (which mutates ``os.environ``
for a harbor subprocess) can share one definition: pass ``target=<dict>`` for the
former, leave it ``None`` for the latter.
"""
from __future__ import annotations

import json
import os
import pathlib
from typing import Any, MutableMapping, Optional

from ouroboros.configured_subagents import (
    PRIMARY_RECOMMENDATION,
    SUBAGENTS_SETTING,
    ConfiguredSubagent,
    configured_subagents_dict,
    make_configured_subagents,
    parse_configured_subagents,
    serialize_configured_subagents,
)
from ouroboros.route_spec import ROUTE_KIND_API_MODEL, RouteSpec

# Every model slot a single-model run pins. Superset that is correct for both the
# settings.json-profile path (SWE-bench Pro) and the forwarded-env path
# (Terminal-Bench); pinning a slot a given adapter ignores is a harmless no-op.
SINGLE_MODEL_SLOT_KEYS = (
    "OUROBOROS_MODEL",
    "OUROBOROS_MODEL_LIGHT",
    "OUROBOROS_MODEL_FALLBACKS",
    "OUROBOROS_MODEL_DEEP_SELF_REVIEW",
    "OUROBOROS_MODEL_CONSCIOUSNESS",
    "OUROBOROS_MODEL_VISION",
    "OUROBOROS_WEBSEARCH_MODEL",
    "OUROBOROS_SCOPE_REVIEW_MODELS",
    "OUROBOROS_SCOPE_REVIEW_MODEL",
    "CLAUDE_CODE_MODEL",
)

BENCHMARK_SUBAGENT_ID = "benchmark-model"
BENCHMARK_SUBAGENT_NAME = "Benchmark model"


def single_model_subagents_setting(model: str) -> str:
    """Canonical one-row Available-subagents value for a fixed-model run."""
    target = str(model or "").strip()
    if not target:
        raise ValueError("single-model benchmark subagent requires a model")
    config = make_configured_subagents((
        ConfiguredSubagent(
            subagent_id=BENCHMARK_SUBAGENT_ID,
            name=BENCHMARK_SUBAGENT_NAME,
            recommended_use=PRIMARY_RECOMMENDATION,
            route=RouteSpec(ROUTE_KIND_API_MODEL, target),
        ),
    ))
    return serialize_configured_subagents(config)


def disabled_subagents_setting() -> str:
    """Canonical explicit-off value for benches where delegation is out of scope."""
    return serialize_configured_subagents(make_configured_subagents((), enabled=False))


def configured_subagents_snapshot(
    settings_path: pathlib.Path | None = None,
    *,
    env_overrides: bool = True,
    exact_model: str = "",
) -> dict[str, Any]:
    """Canonical non-secret run-manifest projection of the effective actor list.

    ``exact_model`` is the post-CLI-override authority used by fixed-model launchers.
    Otherwise resolution mirrors ``model_slot_snapshot``: environment first for a
    same-process server, settings only for a fresh container. Invalid present config
    raises rather than letting a benchmark record an invented empty/default list.
    """
    if exact_model:
        raw: Any = single_model_subagents_setting(exact_model)
    else:
        settings: dict[str, Any] = {}
        if settings_path and pathlib.Path(settings_path).exists():
            loaded = json.loads(pathlib.Path(settings_path).read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("benchmark settings must be a JSON object")
            settings = loaded
        raw = os.environ.get(SUBAGENTS_SETTING) if env_overrides else None
        if raw is None:
            raw = settings.get(SUBAGENTS_SETTING)
    if raw in (None, ""):
        return {}
    return configured_subagents_dict(parse_configured_subagents(raw))


def pin_single_model(
    model: str,
    review_slots: int = 1,
    review_effort: str = "",
    target: Optional[MutableMapping[str, str]] = None,
) -> MutableMapping[str, str]:
    """Pin every model slot to ``model`` and set the review triad to ``review_slots``
    copies of it.

    ``target=None`` mutates ``os.environ`` (host-subprocess path, e.g. Terminal-Bench);
    pass a settings dict to update it instead (e.g. SWE-bench Pro ``derive_run_settings``).
    ``review_effort`` (when non-empty) pins review + scope-review effort. Returns the
    mutated mapping. A single configured reviewer is intentionally loud
    (``single_reviewer_no_diversity``); this helper does not suppress that.
    """
    sink: MutableMapping[str, str] = os.environ if target is None else target
    sink.pop("OUROBOROS_MODEL_HEAVY", None)
    sink.pop("USE_LOCAL_HEAVY", None)
    for key in SINGLE_MODEL_SLOT_KEYS:
        sink[key] = model
    sink[SUBAGENTS_SETTING] = single_model_subagents_setting(model)
    sink["OUROBOROS_REVIEW_MODELS"] = ",".join([model] * max(1, int(review_slots)))
    if review_effort:
        sink["OUROBOROS_EFFORT_REVIEW"] = review_effort
        sink["OUROBOROS_EFFORT_SCOPE_REVIEW"] = review_effort
    return sink
