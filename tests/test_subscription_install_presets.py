"""The install-time subscription preset compiler (plan §D.1, D-3/D-9).

Every assertion here is against the RATIFIED matrix, not against the code's own
idea of it: the expected table below is transcribed from the plan and the
resolved ids are checked to exist in the live discovery lists the daemon really
returned on 2026-08-09, so a silently renamed model id fails loudly instead of
being persisted into the owner's reviewer configuration.
"""

from __future__ import annotations

import json

import pytest

from ouroboros.reviewer_slot_config import parse_reviewer_slots
from ouroboros.subscription_install_presets import (
    PRESET_MARKER_KEY,
    REVIEWER_SLOTS_KEY,
    SUBAGENT_HARNESS_KEY,
    SUBSCRIPTION_PRESET_VERSION,
    HarnessDiscovery,
    compile_install_preset,
    matrix_combinations,
)

# Verbatim from the live Claudexor daemon (GET /v2/harnesses/<id>/models,
# 2026-08-09). Trimmed only of ids no seat can name.
LIVE_MODELS = {
    "claude": (
        "sonnet", "opus", "haiku", "fable", "best",
        "claude-fable-5", "claude-sonnet-5", "claude-opus-5", "claude-opus-4-8",
        "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
        "claude-sonnet-4-6", "claude-sonnet-4-5", "claude-haiku-4-5",
    ),
    "codex": (
        "gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
        "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark",
    ),
    "cursor": (
        "auto", "composer-2.5",
        "cursor-grok-4.5-low", "cursor-grok-4.5-medium", "cursor-grok-4.5-high",
        "cursor-grok-4.5-high-fast",
        "gemini-3.6-flash-minimal", "gemini-3.6-flash-low",
        "gemini-3.6-flash-medium", "gemini-3.6-flash-high",
        "gpt-5.6-sol-low", "gpt-5.6-sol-medium", "gpt-5.6-sol-high",
        "gpt-5.6-sol-xhigh", "gpt-5.6-sol-max",
        "gpt-5.6-terra-medium", "gpt-5.6-terra-high",
        "gpt-5.6-luna-medium",
        "claude-opus-5-medium", "claude-opus-5-high",
        "claude-fable-5-thinking-xhigh", "claude-sonnet-5-medium",
    ),
}

# The ratified matrix, transcribed from plan §D.1 as (target_id, effort) pairs.
EXPECTED = {
    ("claude",): {
        "subagent": "claude=claude-opus-5:medium",
        "advisory": ("claude=claude-sonnet-5", "low"),
        "triad": [("claude=claude-sonnet-5", "medium"),
                  ("claude=claude-opus-5", "medium"),
                  ("claude=claude-opus-4-6", "medium")],
        "scope": [("claude=claude-fable-5", "medium")],
    },
    ("codex",): {
        "subagent": "codex=gpt-5.6-sol:medium",
        "advisory": ("codex=gpt-5.6-terra", "medium"),
        "triad": [("codex=gpt-5.6-sol", "medium"),
                  ("codex=gpt-5.6-terra", "medium"),
                  ("codex=gpt-5.5", "medium")],
        "scope": [("codex=gpt-5.6-sol", "medium")],
    },
    ("cursor",): {
        # Cursor spells effort INSIDE the slug; the row/tail effort agrees with it.
        "subagent": "cursor=cursor-grok-4.5-high:high",
        "advisory": ("cursor=cursor-grok-4.5-medium", "medium"),
        "triad": [("cursor=cursor-grok-4.5-medium", "medium"),
                  ("cursor=gemini-3.6-flash-medium", "medium"),
                  ("cursor=gpt-5.6-sol-medium", "medium")],
        "scope": [("cursor=cursor-grok-4.5-high", "high")],
    },
    ("claude", "codex"): {
        "subagent": "claude=claude-opus-5:medium",
        "advisory": ("claude=claude-sonnet-5", "low"),
        "triad": [("claude=claude-opus-5", "medium"),
                  ("codex=gpt-5.6-sol", "medium"),
                  ("claude=claude-sonnet-5", "medium")],
        "scope": [("codex=gpt-5.6-sol", "medium")],
    },
    ("claude", "cursor"): {
        "subagent": "claude=claude-opus-5:medium",
        "advisory": ("claude=claude-sonnet-5", "low"),
        "triad": [("claude=claude-opus-5", "medium"),
                  ("cursor=cursor-grok-4.5-medium", "medium"),
                  ("claude=claude-sonnet-5", "medium")],
        "scope": [("claude=claude-fable-5", "medium")],
    },
    ("codex", "cursor"): {
        "subagent": "codex=gpt-5.6-sol:medium",
        "advisory": ("codex=gpt-5.6-terra", "medium"),
        "triad": [("codex=gpt-5.6-sol", "medium"),
                  ("cursor=cursor-grok-4.5-medium", "medium"),
                  ("codex=gpt-5.6-terra", "medium")],
        "scope": [("codex=gpt-5.6-sol", "medium")],
    },
    ("claude", "codex", "cursor"): {
        "subagent": "claude=claude-opus-5:medium",
        "advisory": ("claude=claude-sonnet-5", "low"),
        "triad": [("claude=claude-opus-5", "medium"),
                  ("codex=gpt-5.6-sol", "medium"),
                  ("cursor=cursor-grok-4.5-medium", "medium")],
        "scope": [("codex=gpt-5.6-sol", "medium")],
    },
}


def _discoveries(*harnesses, models=None):
    catalog = models or LIVE_MODELS
    return [HarnessDiscovery(harness_id=h, model_ids=tuple(catalog[h])) for h in harnesses]


def test_matrix_covers_exactly_the_seven_non_empty_combinations():
    combos = {tuple(sorted(key)) for key in matrix_combinations()}
    assert combos == {tuple(sorted(key)) for key in EXPECTED}
    assert len(combos) == 7


@pytest.mark.parametrize("connected", sorted(EXPECTED, key=len))
def test_every_combination_matches_the_ratified_matrix(connected):
    preset = compile_install_preset(_discoveries(*connected))

    assert preset.ok, preset.refusal
    expected = EXPECTED[connected]
    assert preset.subagent_harness == expected["subagent"]

    config = parse_reviewer_slots(preset.reviewer_slots)  # the ONE strict parser
    assert [(row.target_id, row.effort) for row in config.triad] == expected["triad"]
    assert [(row.target_id, row.effort) for row in config.scope] == expected["scope"]
    assert (config.advisory.target_id, config.advisory.effort) == expected["advisory"]
    assert config.advisory.enabled is True
    assert config.advisory.kind == "agent_session"
    assert all(row.is_session for row in config.triad + config.scope)


@pytest.mark.parametrize("connected", sorted(EXPECTED, key=len))
def test_only_exact_discovery_ids_are_ever_written(connected):
    """No owner shorthand (``opus-4.6``, ``terra``, ``grok-4.5``) may survive
    into the saved value — every model must exist in that harness's live list."""
    preset = compile_install_preset(_discoveries(*connected))
    config = parse_reviewer_slots(preset.reviewer_slots)

    targets = [row.target_id for row in config.triad + config.scope]
    targets.append(config.advisory.target_id)
    targets.append(preset.subagent_harness.split(":")[0])
    for target in targets:
        harness, _, model = target.partition("=")
        assert model in LIVE_MODELS[harness], f"{model!r} is not in {harness} discovery"


@pytest.mark.parametrize("connected", sorted(EXPECTED, key=len))
def test_credential_profile_is_never_pinned(connected):
    """D28: the daemon rotates accounts; an install-time pin would outlive one."""
    preset = compile_install_preset(_discoveries(*connected))

    assert "profile_id" not in preset.reviewer_slots
    config = parse_reviewer_slots(preset.reviewer_slots)
    assert all(row.profile_id == "" for row in config.triad + config.scope)
    assert config.advisory.profile_id == ""


def test_settings_keys_are_exactly_the_three_install_keys():
    preset = compile_install_preset(_discoveries("claude"))

    assert set(preset.settings_keys()) == {
        REVIEWER_SLOTS_KEY, SUBAGENT_HARNESS_KEY, PRESET_MARKER_KEY}
    assert preset.settings_keys()[PRESET_MARKER_KEY] == SUBSCRIPTION_PRESET_VERSION
    # The API model slots are NOT among them (owner decision D-2).
    assert "OUROBOROS_MODEL" not in preset.settings_keys()


def test_unresolvable_model_refuses_typed_and_emits_nothing():
    models = dict(LIVE_MODELS)
    models["claude"] = tuple(m for m in LIVE_MODELS["claude"] if m != "claude-opus-4-6")

    preset = compile_install_preset(_discoveries("claude", models=models))

    assert not preset.ok
    assert preset.refusal is not None
    assert preset.refusal.code == "model_not_in_discovery"
    assert preset.refusal.seat is not None
    assert (preset.refusal.seat.surface, preset.refusal.seat.position) == ("triad", 3)
    assert preset.refusal.seat.preference == "opus-4.6"
    assert "claude-opus-4-6" in preset.refusal.candidates
    # Nothing partial: no slots, no subagent value, no settings keys at all.
    assert preset.reviewer_slots == ""
    assert preset.subagent_harness == ""
    assert preset.settings_keys() == {}


def test_cursor_gpt56_family_resolves_in_flagship_order():
    """Cursor publishes no bare ``gpt-5.6`` slug, so the owner's "gpt-5.6" seat
    is the 5.6 FAMILY — sol first, and only from live discovery."""
    models = dict(LIVE_MODELS)
    models["cursor"] = tuple(m for m in LIVE_MODELS["cursor"] if not m.startswith("gpt-5.6-sol"))

    preset = compile_install_preset(_discoveries("cursor", models=models))

    assert preset.ok, preset.refusal
    config = parse_reviewer_slots(preset.reviewer_slots)
    assert config.triad[2].target_id == "cursor=gpt-5.6-terra-medium"

    models["cursor"] = tuple(
        m for m in models["cursor"] if not m.startswith(("gpt-5.6-terra", "gpt-5.6-luna")))
    refused = compile_install_preset(_discoveries("cursor", models=models))
    assert not refused.ok
    assert refused.refusal.code == "model_not_in_discovery"
    assert refused.refusal.seat.preference == "gpt-5.6"


def test_cursor_effort_rides_the_slug_and_the_row_field_together():
    preset = compile_install_preset(_discoveries("cursor"))
    config = parse_reviewer_slots(preset.reviewer_slots)

    # scope is the high-effort cursor seat: the slug tail and the field agree,
    # so nothing downstream can materialize a DIFFERENT effort by default.
    assert config.scope[0].target_id == "cursor=cursor-grok-4.5-high"
    assert config.scope[0].effort == "high"
    assert preset.receipt["surfaces"]["scope"][0]["effort_in_model_id"] is True
    assert preset.receipt["surfaces"]["subagent"]["effort_in_model_id"] is True


def test_claude_and_codex_rows_carry_effort_only_in_the_field():
    preset = compile_install_preset(_discoveries("claude", "codex"))
    config = parse_reviewer_slots(preset.reviewer_slots)

    for row in config.triad + config.scope:
        model = row.target_id.partition("=")[2]
        assert not model.endswith((":medium", "-medium")), model
        assert row.effort == "medium"
    assert preset.receipt["surfaces"]["triad"][0]["effort_in_model_id"] is False


def test_no_connected_preset_harness_refuses():
    preset = compile_install_preset([HarnessDiscovery("opencode", ("whatever",))])

    assert not preset.ok
    assert preset.refusal.code == "no_preset_harness_connected"
    assert preset.settings_keys() == {}


def test_empty_discovery_refuses_instead_of_guessing():
    preset = compile_install_preset([HarnessDiscovery("claude", ())])

    assert not preset.ok
    assert preset.refusal.code == "discovery_empty"
    assert "claude" in preset.refusal.message


def test_receipt_records_what_was_resolved_and_from_where():
    preset = compile_install_preset(
        _discoveries("claude", "codex"),
        capability={"claude": {"status": "ok"}},
    )

    receipt = preset.receipt
    assert receipt["version"] == SUBSCRIPTION_PRESET_VERSION
    assert receipt["connected"] == ["claude", "codex"]
    assert receipt["profile_pinned"] is False
    assert receipt["discovery_counts"]["codex"] == len(LIVE_MODELS["codex"])
    assert receipt["capability"]["claude"]["status"] == "ok"
    assert receipt["surfaces"]["advisory"]["model"] == "claude-sonnet-5"
    assert len(receipt["surfaces"]["triad"]) == 3
    # The receipt must be JSON-serializable — it rides an API response.
    json.dumps(receipt)


def test_compiler_reads_no_settings_and_carries_no_transport(monkeypatch):
    """The compiler is pure: everything it knows arrives as an argument.

    Structural, not behavioural: a transport import in this module would be the
    second discovery path the endpoint is built to avoid, and a settings read
    would make the compiler's answer depend on state its caller already owns."""
    import pathlib

    import ouroboros.config as config

    monkeypatch.setattr(
        config, "load_settings",
        lambda: (_ for _ in ()).throw(AssertionError("the compiler must not read settings")))
    assert compile_install_preset(_discoveries("codex")).ok

    source = (pathlib.Path(__file__).resolve().parents[1]
              / "ouroboros" / "subscription_install_presets.py").read_text(encoding="utf-8")
    for forbidden in ("import httpx", "import requests", "import socket",
                      "import urllib", "ClaudexorGateway", "load_settings"):
        assert forbidden not in source, f"{forbidden} has no place in a pure compiler"


# Verbatim from the Antigravity CLI the Claudexor 3.5.0 agy adapter pins
# (AGY_KNOWN_MODELS, verified against agy 1.1.13). Fourteen ids; effort rides
# inside the slug, and gemini-3.1-pro exists ONLY at high/low.
AGY_LIVE_MODELS = (
    "gemini-3.7-flash-high", "gemini-3.7-flash-medium", "gemini-3.7-flash-low",
    "gemini-3.6-flash-high", "gemini-3.6-flash-medium", "gemini-3.6-flash-low",
    "gemini-3.5-flash-high", "gemini-3.5-flash-medium", "gemini-3.5-flash-low",
    "gemini-3.1-pro-high", "gemini-3.1-pro-low",
    "claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium",
)


def _catalog_with_agy():
    return {**LIVE_MODELS, "agy": AGY_LIVE_MODELS}


@pytest.mark.parametrize("connected", [
    ("agy",),
    ("claude", "agy"),
    ("codex", "agy"),
    ("cursor", "agy"),
    ("claude", "codex", "cursor", "agy"),
])
def test_agy_combination_refuses_typed_matrix_row_absent(connected):
    preset = compile_install_preset(_discoveries(*connected, models=_catalog_with_agy()))
    assert preset.refusal is not None
    assert preset.refusal.code == "matrix_row_absent"
    assert "agy" in preset.connected
    assert preset.settings_keys() == {}
    assert not preset.ok


def test_matrix_row_absent_wins_over_empty_discovery():
    # An unratified combination refuses as unratified whatever its discovery
    # holds — the guard runs before discovery validation on purpose.
    preset = compile_install_preset([HarnessDiscovery(harness_id="agy", model_ids=())])
    assert preset.refusal is not None
    assert preset.refusal.code == "matrix_row_absent"


def test_no_recognized_combination_can_raise():
    # The KeyError class: every non-empty subset of PRESET_HARNESSES must come
    # back as a compiled preset or a typed refusal, never an exception.
    import itertools

    from ouroboros.subscription_install_presets import PRESET_HARNESSES

    catalog = _catalog_with_agy()
    for size in range(1, len(PRESET_HARNESSES) + 1):
        for combo in itertools.combinations(PRESET_HARNESSES, size):
            preset = compile_install_preset(_discoveries(*combo, models=catalog))
            assert preset.ok or preset.refusal is not None


def test_agy_alias_table_spells_effort_inside_the_id():
    from ouroboros.subscription_install_presets import (
        _EFFORT_IN_MODEL_ID,
        _MODEL_ALIASES,
        HARNESS_AGY,
    )

    assert HARNESS_AGY in _EFFORT_IN_MODEL_ID
    aliases = _MODEL_ALIASES[HARNESS_AGY]
    # Every alias candidate formats to an id the pinned vendor CLI really
    # publishes, so a future matrix row resolves exact discovery ids.
    assert aliases["gemini-3.7-flash"][0].format(effort="high") in AGY_LIVE_MODELS
    assert aliases["gemini-3.1-pro"][0].format(effort="high") in AGY_LIVE_MODELS
    assert aliases["gemini-3.1-pro"][0].format(effort="low") in AGY_LIVE_MODELS
    # Documented trap for the future dictation: pro has no -medium slug.
    assert aliases["gemini-3.1-pro"][0].format(effort="medium") not in AGY_LIVE_MODELS
