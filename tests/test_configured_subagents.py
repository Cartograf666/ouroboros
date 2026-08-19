"""Focused contract tests for the Available-subagents settings foundation."""

from __future__ import annotations

import json

import pytest

from ouroboros.config import migrate_legacy_slot_keys
from ouroboros.configured_subagents import (
    MAX_CONFIGURED_SUBAGENTS,
    LEGACY_SUBAGENT_COMPATIBILITY,
    SOURCE_CONFIGURED,
    SOURCE_INVALID,
    SOURCE_LEGACY_MIGRATED,
    SOURCE_UNDECIDED,
    SUBAGENTS_SETTING,
    configured_subagents_fingerprint,
    normalize_configured_subagents,
    parse_configured_subagents,
    resolve_configured_subagents,
)
from ouroboros.server_runtime import apply_runtime_provider_defaults


def _row(row_id: str = "builder", **overrides):
    row = {
        "subagent_id": row_id,
        "name": "Builder",
        "recommended_use": "Use for substantial implementation.",
        "route": {
            "kind": "agent_session",
            "target_id": "codex=gpt-5.6-sol",
            "credential_profile_id": "",
        },
        "effort": "medium",
    }
    row.update(overrides)
    return row


def _config(*rows, enabled=True):
    return {"enabled": enabled, "items": list(rows or (_row(),))}


def test_strict_config_round_trips_object_and_json_to_one_canonical_string():
    config_from_object, canonical = normalize_configured_subagents(_config())
    config_from_json = parse_configured_subagents(canonical)

    assert config_from_object == config_from_json
    assert json.loads(canonical) == _config()
    assert canonical == normalize_configured_subagents(canonical)[1]
    assert configured_subagents_fingerprint(config_from_object) == (configured_subagents_fingerprint(config_from_json))
    assert LEGACY_SUBAGENT_COMPATIBILITY == "remove_after_next_minor_release"


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"enabled": "true", "items": []}, "enabled must be a boolean"),
        (_config(_row("bad id")), "subagent_id must match"),
        (_config(_row("same"), _row("same")), "appears twice"),
        (_config(_row(extra=True)), "unknown keys"),
        (_config(_row(route={"kind": "api_model", "target_id": "x", "extra": 1})), "route has unknown keys"),
        (
            _config(_row(route={"kind": "api_model", "target_id": "x", "credential_profile_id": "account"})),
            "meaningful only for agent_session",
        ),
        (_config(_row(route={"kind": "agent_session", "target_id": "codex::model"})), "session target"),
        (
            _config(_row(route={"kind": "agent_session", "target_id": "cursor=cursor-grok-4.6-high"}, effort="medium")),
            "conflicts with compound route effort",
        ),
        (
            _config(_row(route={"kind": "agent_session", "target_id": "cursor=cursor-grok-4.6-high-fast"}, effort="medium")),
            "conflicts with compound route effort",
        ),
        (_config(_row(route={"kind": "agent_session", "target_id": "=gpt-5.6-sol"})), "session harness"),
        (_config(_row(route={"kind": "agent_session", "target_id": "codex="})), "session model is empty"),
        (_config(_row(route={"kind": "agent_session", "target_id": "codex=a=b"})), "at most one"),
        (_config(_row(route={"kind": "agent_session", "target_id": "codex gpt-5.6-sol"})), "without whitespace"),
        (_config(_row(route={"kind": "agent_session", "target_id": "codex=gpt-5.6-sol:high"})), "legacy ':effort'"),
    ],
)
def test_strict_parser_rejects_ambiguous_or_lossy_shapes(payload, match):
    with pytest.raises(ValueError, match=match):
        parse_configured_subagents(payload)


def test_maximum_ten_is_real_and_row_precise():
    parse_configured_subagents(_config(*(_row(f"row-{i}") for i in range(10))))
    with pytest.raises(ValueError, match=f"maximum is {MAX_CONFIGURED_SUBAGENTS}"):
        parse_configured_subagents(_config(*(_row(f"row-{i}") for i in range(11))))


def test_optional_name_is_derived_but_an_explicit_non_string_is_rejected():
    row = _row("owner_builder")
    row.pop("name")
    parsed = parse_configured_subagents(_config(row))
    assert parsed.items[0].name == "Owner Builder"

    row["name"] = 7
    with pytest.raises(ValueError, match="name must be a string"):
        parse_configured_subagents(_config(row))


def test_valid_new_setting_wins_over_every_legacy_selector():
    raw = _config(_row("new"), enabled=False)
    resolution = resolve_configured_subagents(
        {
            SUBAGENTS_SETTING: json.dumps(raw),
            "OUROBOROS_SUBAGENT_HARNESS": "claude=claude-opus-5:high",
            "OUROBOROS_MODEL_HEAVY": "openai/gpt-5.6-sol",
        }
    )

    assert resolution.source == SOURCE_CONFIGURED
    assert resolution.config is not None
    assert resolution.config.enabled is False
    assert [row.subagent_id for row in resolution.config.items] == ["new"]


def test_legacy_singleton_and_account_pin_migrate_without_persisting():
    settings = {
        "OUROBOROS_SUBAGENT_HARNESS": "claude=claude-opus-5:high",
        "OUROBOROS_SUBAGENT_PROFILE": "owner-account",
        "OUROBOROS_MODEL_HEAVY": "openai/gpt-5.6-sol",
        "OUROBOROS_MODEL_LIGHT": "openai/gpt-5.6-luna",
        "OPENROUTER_API_KEY": "configured",
    }
    resolution = resolve_configured_subagents(settings)

    assert resolution.source == SOURCE_LEGACY_MIGRATED
    assert SUBAGENTS_SETTING not in settings
    assert resolution.config is not None and resolution.config.enabled is True
    primary = resolution.config.items[0]
    assert primary.route.target_id == "claude=claude-opus-5"
    assert primary.route.credential_profile_id == "owner-account"
    assert primary.effort == "high"
    assert [row.route.target_id for row in resolution.config.items[1:]] == [
        "openai/gpt-5.6-sol",
        "openai/gpt-5.6-luna",
    ]


def test_legacy_off_is_explicit_false_and_never_becomes_default_candidate():
    candidate = parse_configured_subagents(_config(_row("candidate")))
    resolution = resolve_configured_subagents(
        {"OUROBOROS_SUBAGENT_HARNESS": " OFF "},
        default_candidate=candidate,
    )

    assert resolution.source == SOURCE_LEGACY_MIGRATED
    assert resolution.config is not None
    assert resolution.config.enabled is False
    assert resolution.config.items == ()


def test_legacy_off_preserves_custom_heavy_rows_without_reenabling_defaults():
    candidate = parse_configured_subagents(_config(_row("must-not-default")))
    resolution = resolve_configured_subagents(
        {
            "OUROBOROS_SUBAGENT_HARNESS": "off",
            "OUROBOROS_MODEL_HEAVY": "owner/custom-heavy",
        },
        default_candidate=candidate,
    )

    assert resolution.config is not None
    assert resolution.config.enabled is False
    assert [row.subagent_id for row in resolution.config.items] == ["legacy-heavy"]
    assert resolution.config.items[0].route.target_id == "owner/custom-heavy"


def test_empty_is_undecided_and_candidate_remains_unsaved():
    settings = {"OUROBOROS_SUBAGENT_HARNESS": ""}
    candidate = parse_configured_subagents(_config(_row("candidate")))
    resolution = resolve_configured_subagents(settings, default_candidate=candidate)

    assert resolution.source == SOURCE_UNDECIDED
    assert resolution.config == candidate
    assert settings["OUROBOROS_SUBAGENT_HARNESS"] == ""
    assert SUBAGENTS_SETTING not in settings


def test_malformed_nonempty_new_or_legacy_bytes_fail_closed_and_are_preserved():
    bad_new = "{not-json"
    new_resolution = resolve_configured_subagents({SUBAGENTS_SETTING: bad_new})
    legacy_resolution = resolve_configured_subagents(
        {
            "OUROBOROS_SUBAGENT_HARNESS": "=no-harness",
        }
    )

    assert new_resolution.source == SOURCE_INVALID
    assert new_resolution.raw == bad_new
    assert new_resolution.config is None
    assert legacy_resolution.source == SOURCE_INVALID
    assert legacy_resolution.raw == "=no-harness"
    assert legacy_resolution.config is None


def test_legacy_light_requires_a_truthful_provider_but_custom_heavy_is_preserved():
    resolution = resolve_configured_subagents(
        {
            "OUROBOROS_SUBAGENT_HARNESS": "codex=gpt-5.6-sol",
            "OUROBOROS_MODEL_HEAVY": "owner/custom-heavy",
            "OUROBOROS_MODEL_LIGHT": "openai::gpt-5.6-luna",
        }
    )

    assert resolution.config is not None
    assert [row.route.target_id for row in resolution.config.items] == [
        "codex=gpt-5.6-sol",
        "owner/custom-heavy",
    ]


def test_custom_heavy_without_singleton_is_an_unsaved_undecided_migration_candidate():
    settings = {"OUROBOROS_MODEL_HEAVY": "owner/custom-heavy"}
    resolution = resolve_configured_subagents(settings)

    assert resolution.source == SOURCE_UNDECIDED
    assert resolution.config is not None
    assert [row.route.target_id for row in resolution.config.items] == [
        "owner/custom-heavy",
    ]
    assert SUBAGENTS_SETTING not in settings


def test_saved_local_heavy_intent_survives_a_temporarily_missing_source():
    resolution = resolve_configured_subagents({
        "OUROBOROS_MODEL_HEAVY": "owner-model",
        "USE_LOCAL_HEAVY": True,
        "LOCAL_MODEL_SOURCE": "",
    })

    assert resolution.config is not None
    assert resolution.config.items[0].route.target_id == "owner-model (local)"


def test_legacy_code_plus_local_flag_migrates_to_an_explicit_local_actor():
    settings = {
        "OUROBOROS_MODEL_CODE": "anthropic/claude-opus-4.7",
        "USE_LOCAL_CODE": True,
    }
    migrate_legacy_slot_keys(settings)
    normalized, changed, changed_keys = apply_runtime_provider_defaults(settings)
    resolution = resolve_configured_subagents(normalized)

    assert not changed
    assert changed_keys == []
    assert "OUROBOROS_MODEL_CODE" not in normalized
    assert "USE_LOCAL_CODE" not in normalized
    assert resolution.config is not None
    assert [(row.subagent_id, row.route.target_id) for row in resolution.config.items] == [
        ("legacy-heavy", "anthropic/claude-opus-4.7 (local)"),
    ]


def test_legacy_intent_precedes_and_deduplicates_compiler_defaults():
    candidate = parse_configured_subagents(_config(
        _row("primary-builder", route={
            "kind": "agent_session",
            "target_id": "claude=claude-opus-5",
            "credential_profile_id": "",
        }),
        _row("fast-scout", route={
            "kind": "api_model",
            "target_id": "openai/gpt-5.6-luna",
        }),
    ))
    resolution = resolve_configured_subagents({
        "OUROBOROS_SUBAGENT_HARNESS": "claude=claude-opus-5",
        "OUROBOROS_MODEL_HEAVY": "owner/custom-heavy",
        "OUROBOROS_MODEL_LIGHT": "openai/gpt-5.6-luna",
        "OPENROUTER_API_KEY": "configured",
    }, default_candidate=candidate)

    assert resolution.config is not None
    assert [row.route.target_id for row in resolution.config.items] == [
        "claude=claude-opus-5",
        "owner/custom-heavy",
        "openai/gpt-5.6-luna",
    ]
