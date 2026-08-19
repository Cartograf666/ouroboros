"""Canonical ``OUROBOROS_SUBAGENTS`` parser, normalizer, and legacy reader."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from ouroboros.route_spec import (
    ROUTE_KIND_AGENT_SESSION,
    ROUTE_KIND_API_MODEL,
    RouteSpec,
    parse_route_spec,
    route_spec_dict,
)

SUBAGENTS_SETTING = "OUROBOROS_SUBAGENTS"
SUBAGENTS_RECEIPT_KEY = "OUROBOROS_SUBAGENT_PRESET_RECEIPT"
MAX_CONFIGURED_SUBAGENTS = 10
# Removal marker, not a runtime gate: the singleton/Heavy reader is intentionally
# one compatibility window rather than a permanent second configuration system.
LEGACY_SUBAGENT_COMPATIBILITY = "remove_after_next_minor_release"

SOURCE_CONFIGURED = "configured"
SOURCE_ONBOARDING_DEFAULT = "onboarding_default"
SOURCE_LEGACY_MIGRATED = "legacy_migrated"
SOURCE_UNDECIDED = "undecided"
SOURCE_INVALID = "invalid"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TOP_KEYS = frozenset({"enabled", "items"})
_ROW_KEYS = frozenset({"subagent_id", "name", "recommended_use", "route", "effort"})
_ROUTE_ALIASES = {
    ROUTE_KIND_API_MODEL: ROUTE_KIND_API_MODEL,
    ROUTE_KIND_AGENT_SESSION: ROUTE_KIND_AGENT_SESSION,
}

PRIMARY_RECOMMENDATION = (
    "Use for substantial implementation, difficult debugging, research synthesis, and "
    "end-to-end ownership. Prefer when overall quality and continuity matter more than speed."
)
SCOUT_RECOMMENDATION = (
    "Use for fast repository exploration, web research, test triage, and concise evidence "
    "gathering. Prefer when speed and low cost matter more than deep implementation."
)
INDEPENDENT_RECOMMENDATION = (
    "Use for alternative designs, ambiguous product decisions, adversarial critique, and a "
    "genuinely independent second opinion. Ask it to challenge the current approach rather "
    "than repeat it."
)
ALTERNATIVE_RECOMMENDATION = (
    "Use as an alternative capable agent when its model, availability, or perspective fits "
    "the task better than the primary choice. State why you are choosing it."
)


@dataclass(frozen=True)
class ConfiguredSubagent:
    subagent_id: str
    name: str
    recommended_use: str
    route: RouteSpec
    effort: str = ""


@dataclass(frozen=True)
class ConfiguredSubagents:
    enabled: bool
    items: tuple[ConfiguredSubagent, ...]


@dataclass(frozen=True)
class ConfiguredSubagentsResolution:
    config: Optional[ConfiguredSubagents]
    source: str
    diagnostic: str = ""
    raw: Any = None


def _effort(raw: Any, where: str) -> str:
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"{SUBAGENTS_SETTING}: {where}.effort must be a string")
    effort = raw.strip().lower()
    from ouroboros.config import EFFORT_SCALE

    if effort not in EFFORT_SCALE:
        raise ValueError(
            f"{SUBAGENTS_SETTING}: {where} names an unknown effort {effort!r}; valid: {', '.join(EFFORT_SCALE)}"
        )
    return effort


def _compound_effort_conflict(route: RouteSpec, effort: str, where: str) -> None:
    if not route.is_session or not effort:
        return
    harness, sep, model = route.target_id.partition("=")
    if not sep or harness not in {"cursor", "agy"}:
        return
    from ouroboros.config import EFFORT_SCALE

    # Cursor's real catalog can append the transport modifier ``-fast`` after
    # the compound effort (for example ``...-high-fast``).  It is not a second
    # effort and must not hide the one the model id already encodes.
    compound_model = model[:-5] if model.lower().endswith("-fast") else model
    encoded = compound_model.rsplit("-", 1)[-1].lower()
    if encoded in EFFORT_SCALE and encoded != effort:
        raise ValueError(
            f"{SUBAGENTS_SETTING}: {where} effort {effort!r} conflicts with compound route effort {encoded!r}"
        )


def _validate_session_target(route: RouteSpec, where: str) -> None:
    """Validate the Available-subagent ``harness[=model]`` public grammar.

    Reviewer rows retain their established permissive parser.  New task actors
    are stricter because their target is snapshotted as executable intent: a
    legacy ``:effort`` suffix, an empty side of ``=``, or multiple separators
    would otherwise be canonically persisted and fail only after dispatch.
    """
    if not route.is_session:
        return
    target = route.target_id
    if any(ch.isspace() for ch in target) or ":" in target:
        raise ValueError(
            f"{SUBAGENTS_SETTING}: {where} session target must use harness[=model] without whitespace or legacy ':effort'"
        )
    if target.count("=") > 1:
        raise ValueError(
            f"{SUBAGENTS_SETTING}: {where} session target must contain at most one '='"
        )
    harness, separator, model = target.partition("=")
    if not _ID_RE.fullmatch(harness):
        raise ValueError(
            f"{SUBAGENTS_SETTING}: {where} session harness must match [A-Za-z0-9][A-Za-z0-9_.-]{{0,63}}"
        )
    if separator and not model:
        raise ValueError(f"{SUBAGENTS_SETTING}: {where} session model is empty")


def _parse_payload(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{SUBAGENTS_SETTING} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{SUBAGENTS_SETTING} must be a JSON object")
    return raw


def parse_configured_subagents(raw: Any) -> ConfiguredSubagents:
    """Strict parser for stored JSON strings and owner-supplied JSON objects."""
    payload = _parse_payload(raw)
    unknown = sorted(set(payload) - _TOP_KEYS)
    if unknown:
        raise ValueError(f"{SUBAGENTS_SETTING} has unknown top-level keys: {unknown}")
    if not isinstance(payload.get("enabled"), bool):
        raise ValueError(f"{SUBAGENTS_SETTING}.enabled must be a boolean")
    rows = payload.get("items")
    if not isinstance(rows, list):
        raise ValueError(f"{SUBAGENTS_SETTING}.items must be an array")
    if len(rows) > MAX_CONFIGURED_SUBAGENTS:
        raise ValueError(f"{SUBAGENTS_SETTING}.items has {len(rows)} rows; maximum is {MAX_CONFIGURED_SUBAGENTS}")

    seen: set[str] = set()
    items: list[ConfiguredSubagent] = []
    for index, row in enumerate(rows):
        where = f"items[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{SUBAGENTS_SETTING}: {where} is not an object")
        unknown = sorted(set(row) - _ROW_KEYS)
        if unknown:
            raise ValueError(f"{SUBAGENTS_SETTING}: {where} has unknown keys: {unknown}")
        row_id = row.get("subagent_id")
        if not isinstance(row_id, str) or not _ID_RE.fullmatch(row_id.strip()):
            raise ValueError(f"{SUBAGENTS_SETTING}: {where}.subagent_id must match [A-Za-z0-9][A-Za-z0-9_.-]{{0,63}}")
        row_id = row_id.strip()
        if row_id in seen:
            raise ValueError(f"{SUBAGENTS_SETTING}: subagent_id {row_id!r} appears twice")
        seen.add(row_id)
        raw_name = row.get("name", "")
        if not isinstance(raw_name, str):
            raise ValueError(f"{SUBAGENTS_SETTING}: {where}.name must be a string")
        if not isinstance(row.get("recommended_use"), str):
            raise ValueError(f"{SUBAGENTS_SETTING}: {where}.recommended_use must be a string")
        name = raw_name.strip() or row_id.replace("-", " ").replace("_", " ").title()
        route = parse_route_spec(
            row.get("route"),
            setting=SUBAGENTS_SETTING,
            where=where,
            kind_aliases=_ROUTE_ALIASES,
            pin_key="credential_profile_id",
            reject_unknown=True,
            strict_strings=True,
            reject_api_pin=True,
        )
        _validate_session_target(route, where)
        effort = _effort(row.get("effort"), where)
        _compound_effort_conflict(route, effort, where)
        items.append(
            ConfiguredSubagent(
                subagent_id=row_id,
                name=name,
                recommended_use=row["recommended_use"],
                route=route,
                effort=effort,
            )
        )
    return ConfiguredSubagents(enabled=payload["enabled"], items=tuple(items))


def configured_subagents_dict(config: ConfiguredSubagents) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for row in config.items:
        payload: dict[str, Any] = {
            "subagent_id": row.subagent_id,
            "name": row.name,
            "recommended_use": row.recommended_use,
            "route": route_spec_dict(
                row.route,
                api_kind=ROUTE_KIND_API_MODEL,
                pin_key="credential_profile_id",
            ),
        }
        if row.effort:
            payload["effort"] = row.effort
        items.append(payload)
    return {"enabled": config.enabled, "items": items}


def serialize_configured_subagents(config: ConfiguredSubagents) -> str:
    return json.dumps(
        configured_subagents_dict(config),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
    )


def normalize_configured_subagents(raw: Any) -> tuple[ConfiguredSubagents, str]:
    config = parse_configured_subagents(raw)
    return config, serialize_configured_subagents(config)


def configured_subagents_fingerprint(config: ConfiguredSubagents) -> str:
    return hashlib.sha256(serialize_configured_subagents(config).encode("utf-8")).hexdigest()


def _legacy_session(raw: str, profile_id: str) -> ConfiguredSubagent:
    text = raw.strip()
    if any(ch.isspace() for ch in text) or "::" in text:
        raise ValueError("legacy session route contains whitespace or provider syntax")
    route_part, sep, effort = text.rpartition(":")
    if sep and effort.lower() not in _legacy_efforts():
        raise ValueError(f"legacy session effort {effort!r} is invalid")
    if not sep:
        route_part, effort = text, ""
    harness = route_part.partition("=")[0]
    if not _ID_RE.fullmatch(harness) or route_part.count("=") > 1:
        raise ValueError("legacy session route is malformed")
    if "=" in route_part and not route_part.partition("=")[2]:
        raise ValueError("legacy session model is empty")
    return ConfiguredSubagent(
        subagent_id="legacy-session",
        name="Primary builder",
        recommended_use=PRIMARY_RECOMMENDATION,
        route=RouteSpec(ROUTE_KIND_AGENT_SESSION, route_part, profile_id),
        effort=effort.lower(),
    )


def _legacy_efforts() -> set[str]:
    from ouroboros.config import EFFORT_SCALE

    return set(EFFORT_SCALE)


def resolve_configured_subagents(
    settings: Mapping[str, Any],
    *,
    default_candidate: Optional[ConfiguredSubagents] = None,
) -> ConfiguredSubagentsResolution:
    """Read new config first, then the bounded singleton/Heavy/Light migration."""
    raw_new = settings.get(SUBAGENTS_SETTING)
    if raw_new not in (None, ""):
        try:
            return ConfiguredSubagentsResolution(
                parse_configured_subagents(raw_new),
                SOURCE_CONFIGURED,
                raw=raw_new,
            )
        except ValueError as exc:
            return ConfiguredSubagentsResolution(
                None,
                SOURCE_INVALID,
                str(exc),
                raw=raw_new,
            )

    raw_legacy = str(settings.get("OUROBOROS_SUBAGENT_HARNESS") or "").strip()
    if raw_legacy.lower() == "off":
        items: list[ConfiguredSubagent] = []
        _append_legacy_model_rows(items, settings)
        return ConfiguredSubagentsResolution(
            ConfiguredSubagents(enabled=False, items=tuple(items[:MAX_CONFIGURED_SUBAGENTS])),
            SOURCE_LEGACY_MIGRATED,
            raw=raw_legacy,
        )
    if raw_legacy:
        try:
            primary = _legacy_session(
                raw_legacy,
                str(settings.get("OUROBOROS_SUBAGENT_PROFILE") or "").strip(),
            )
        except ValueError as exc:
            return ConfiguredSubagentsResolution(
                None,
                SOURCE_INVALID,
                f"OUROBOROS_SUBAGENT_HARNESS migration failed: {exc}",
                raw=raw_legacy,
            )
        items = [primary]
        _append_legacy_model_rows(items, settings, include_ids={"legacy-heavy"})
        if default_candidate is not None:
            _append_candidate_rows(items, default_candidate.items)
        _append_legacy_model_rows(items, settings, include_ids={"fast-scout"})
        return ConfiguredSubagentsResolution(
            ConfiguredSubagents(enabled=True, items=tuple(items[:MAX_CONFIGURED_SUBAGENTS])),
            SOURCE_LEGACY_MIGRATED,
            raw=raw_legacy,
        )

    items: list[ConfiguredSubagent] = []
    _append_legacy_model_rows(items, settings, include_ids={"legacy-heavy"})
    if default_candidate is not None:
        _append_candidate_rows(items, default_candidate.items)
    _append_legacy_model_rows(items, settings, include_ids={"fast-scout"})
    if items:
        return ConfiguredSubagentsResolution(
            make_configured_subagents(
                items[:MAX_CONFIGURED_SUBAGENTS],
                enabled=default_candidate.enabled if default_candidate is not None else True,
            ),
            SOURCE_UNDECIDED,
            raw=raw_legacy,
        )
    return ConfiguredSubagentsResolution(None, SOURCE_UNDECIDED, raw=raw_legacy)


def resolve_settings_subagent_candidate(
    settings: Mapping[str, Any],
) -> tuple[ConfiguredSubagentsResolution, tuple[dict[str, Any], ...]]:
    """Resolve the read-only Settings draft, including pure API/local defaults.

    This is presentation-time candidate construction, not runtime enablement:
    ``SOURCE_UNDECIDED`` remains unsaved and invisible to new-ID dispatch until
    an explicit Settings save or onboarding completion materializes it.
    """
    default_candidate = None
    diagnostics: list[dict[str, Any]] = []
    legacy = str(settings.get("OUROBOROS_SUBAGENT_HARNESS") or "").strip()
    if settings.get(SUBAGENTS_SETTING) in (None, "") and legacy.lower() != "off":
        from ouroboros.subscription_install_presets import compile_install_preset

        preset = compile_install_preset((), settings=settings)
        if preset.ok:
            default_candidate = normalize_configured_subagents(preset.available_subagents)[0]
            diagnostics.extend(preset.diagnostics)
        elif preset.refusal is not None:
            diagnostics.append(preset.refusal.as_dict())
    resolution = resolve_configured_subagents(settings, default_candidate=default_candidate)
    if resolution.diagnostic:
        diagnostics.insert(0, {
            "code": "configured_subagents_invalid",
            "message": resolution.diagnostic,
        })
    return resolution, tuple(diagnostics)


def _append_legacy_model_rows(
    items: list[ConfiguredSubagent],
    settings: Mapping[str, Any],
    *,
    include_ids: Optional[set[str]] = None,
) -> None:
    from ouroboros.provider_models import model_has_credentials_in_settings

    seen = {(row.route.kind, row.route.target_id, row.route.credential_profile_id) for row in items}
    heavy = _legacy_model_target(settings, "HEAVY", "OUROBOROS_MODEL_HEAVY")
    light_key = "OUROBOROS_MODEL_LIGHT"
    light_slot = "LIGHT"
    if not str(settings.get(light_key) or "").strip():
        light_key, light_slot = "OUROBOROS_MODEL", "MAIN"
    light = _legacy_model_target(settings, light_slot, light_key)
    for row_id, name, recommendation, model, effort in (
        ("legacy-heavy", "Primary builder", PRIMARY_RECOMMENDATION, heavy, ""),
        ("fast-scout", "Fast scout", SCOUT_RECOMMENDATION, light, "low"),
    ):
        if include_ids is not None and row_id not in include_ids:
            continue
        identity = (ROUTE_KIND_API_MODEL, model, "")
        if (
            not model
            or identity in seen
            or (row_id == "fast-scout" and not model_has_credentials_in_settings(model, dict(settings)))
        ):
            continue
        used_ids = {row.subagent_id for row in items}
        safe_id = row_id
        suffix = 2
        while safe_id in used_ids:
            safe_id = f"{row_id}-{suffix}"
            suffix += 1
        items.append(
            ConfiguredSubagent(
                subagent_id=safe_id,
                name=name,
                recommended_use=recommendation,
                route=RouteSpec(ROUTE_KIND_API_MODEL, model),
                effort=effort,
            )
        )
        seen.add(identity)


def _append_candidate_rows(
    items: list[ConfiguredSubagent],
    candidates: Sequence[ConfiguredSubagent],
) -> None:
    """Merge compiler defaults after preserved legacy intent, by route identity."""
    seen = {(row.route.kind, row.route.target_id, row.route.credential_profile_id) for row in items}
    used_ids = {row.subagent_id for row in items}
    for candidate in candidates:
        identity = (
            candidate.route.kind,
            candidate.route.target_id,
            candidate.route.credential_profile_id,
        )
        if identity in seen:
            continue
        row_id = candidate.subagent_id
        suffix = 2
        while row_id in used_ids:
            row_id = f"{candidate.subagent_id}-{suffix}"
            suffix += 1
        items.append(
            ConfiguredSubagent(
                subagent_id=row_id,
                name=candidate.name,
                recommended_use=candidate.recommended_use,
                route=candidate.route,
                effort=candidate.effort,
            )
        )
        seen.add(identity)
        used_ids.add(row_id)


def _legacy_model_target(settings: Mapping[str, Any], slot: str, key: str) -> str:
    model = str(settings.get(key) or "").strip()
    local = str(settings.get(f"USE_LOCAL_{slot}") or "").strip().lower()
    if local not in {"1", "true", "yes", "on"}:
        return model
    if not model:
        return ""
    return model if model.endswith(" (local)") else f"{model} (local)"


def make_configured_subagents(
    rows: Sequence[ConfiguredSubagent],
    *,
    enabled: bool = True,
) -> ConfiguredSubagents:
    """Compiler helper that still validates through the public strict parser."""
    candidate = ConfiguredSubagents(enabled=enabled, items=tuple(rows))
    return parse_configured_subagents(configured_subagents_dict(candidate))


__all__ = [
    "ALTERNATIVE_RECOMMENDATION",
    "ConfiguredSubagent",
    "ConfiguredSubagents",
    "ConfiguredSubagentsResolution",
    "INDEPENDENT_RECOMMENDATION",
    "LEGACY_SUBAGENT_COMPATIBILITY",
    "MAX_CONFIGURED_SUBAGENTS",
    "PRIMARY_RECOMMENDATION",
    "SCOUT_RECOMMENDATION",
    "SOURCE_CONFIGURED",
    "SOURCE_INVALID",
    "SOURCE_LEGACY_MIGRATED",
    "SOURCE_ONBOARDING_DEFAULT",
    "SOURCE_UNDECIDED",
    "SUBAGENTS_RECEIPT_KEY",
    "SUBAGENTS_SETTING",
    "configured_subagents_dict",
    "configured_subagents_fingerprint",
    "make_configured_subagents",
    "normalize_configured_subagents",
    "parse_configured_subagents",
    "resolve_configured_subagents",
    "resolve_settings_subagent_candidate",
    "serialize_configured_subagents",
]
