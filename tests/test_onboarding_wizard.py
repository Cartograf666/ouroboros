import pathlib

import pytest

from ouroboros.onboarding_wizard import build_onboarding_html, prepare_onboarding_settings
from ouroboros.settings_setup_contract import build_setup_bootstrap, build_setup_contract


REPO = pathlib.Path(__file__).resolve().parents[1]


def _base_payload() -> dict:
    return {
        "OPENROUTER_API_KEY": "",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "MINIMAX_API_KEY": "",
        "MINIMAX_REGION": "",
        "TOTAL_BUDGET": 10,
        "OUROBOROS_PER_TASK_COST_USD": 20,
        "OUROBOROS_REVIEW_ENFORCEMENT": "advisory",
        "LOCAL_MODEL_SOURCE": "",
        "LOCAL_MODEL_FILENAME": "",
        "LOCAL_MODEL_CONTEXT_LENGTH": 16384,
        "LOCAL_MODEL_N_GPU_LAYERS": -1,
        "LOCAL_MODEL_CHAT_FORMAT": "",
        "LOCAL_ROUTING_MODE": "cloud",
        "OUROBOROS_MODEL": "openai::gpt-5.5",
        "OUROBOROS_MODEL_HEAVY": "openai::gpt-5.5",
        "OUROBOROS_MODEL_LIGHT": "openai::gpt-5.5-mini",
        "OUROBOROS_MODEL_FALLBACKS": "openai::gpt-5.5-mini",
    }


def test_prepare_onboarding_settings_requires_runnable_config():
    prepared, error = prepare_onboarding_settings(_base_payload(), {})

    assert prepared == {}
    assert "Configure OpenRouter, OpenAI, OpenAI-compatible, Cloud.ru, MiniMax, Anthropic, or a local model" in error


def test_prepare_onboarding_settings_accepts_openai_only_setup():
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert error is None
    assert prepared["OPENAI_API_KEY"] == "sk-openai-1234567890"
    assert prepared["OUROBOROS_MODEL"] == "openai::gpt-5.5"
    assert prepared["TOTAL_BUDGET"] == 10.0
    assert prepared["OUROBOROS_PER_TASK_COST_USD"] == 20.0
    assert prepared["OUROBOROS_REVIEW_ENFORCEMENT"] == "advisory"
    assert prepared["OUROBOROS_MODEL_CONSCIOUSNESS"] == ""
    # Onboarding no longer manages auto-grant; the global SSOT default applies.
    assert "OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS" not in prepared


def test_settings_default_auto_grant_is_true():
    from ouroboros.config import SETTINGS_DEFAULTS

    assert SETTINGS_DEFAULTS["OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS"] == "true"


def test_prepare_onboarding_settings_preserves_existing_auto_grant_choice():
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"

    prepared, error = prepare_onboarding_settings(
        payload,
        {"OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS": "false"},
    )

    assert error is None
    # Onboarding does not override an explicit existing choice.
    assert prepared["OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS"] == "false"


@pytest.mark.parametrize(("key", "value", "error"), [
    ("TOTAL_BUDGET", 0, "Budget must be greater than zero."),
    ("TOTAL_BUDGET", "0", "Budget must be greater than zero."),
    ("TOTAL_BUDGET", -1, "Budget must be greater than zero."),
    ("TOTAL_BUDGET", 0.005, "Budget must be at least 0.01."),
    ("TOTAL_BUDGET", "nan", "Budget must be a number."),
    ("OUROBOROS_PER_TASK_COST_USD", 0, "Per-task cost cap must be greater than zero."),
    ("OUROBOROS_PER_TASK_COST_USD", "0", "Per-task cost cap must be greater than zero."),
    ("OUROBOROS_PER_TASK_COST_USD", -1, "Per-task cost cap must be greater than zero."),
    ("OUROBOROS_PER_TASK_COST_USD", 0.005, "Per-task cost cap must be at least 0.01."),
    ("OUROBOROS_PER_TASK_COST_USD", "nan", "Per-task cost cap must be a number."),
    ("OUROBOROS_PER_TASK_COST_USD", False, "Per-task cost cap must be a number."),
])
def test_prepare_onboarding_settings_rejects_invalid_budget_values(key, value, error):
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"
    payload[key] = value

    prepared, actual_error = prepare_onboarding_settings(payload, {})

    assert prepared == {}
    assert actual_error == error


def test_prepare_onboarding_settings_accepts_cloudru_only_setup():
    payload = _base_payload()
    payload["CLOUDRU_FOUNDATION_MODELS_API_KEY"] = "cloudru-key-1234567890"
    payload["OUROBOROS_MODEL"] = "cloudru::zai-org/GLM-4.7"
    payload["OUROBOROS_MODEL_HEAVY"] = "cloudru::zai-org/GLM-4.7"
    payload["OUROBOROS_MODEL_LIGHT"] = "cloudru::zai-org/GLM-4.7"
    payload["OUROBOROS_MODEL_FALLBACKS"] = "cloudru::zai-org/GLM-4.7"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert error is None
    assert prepared["CLOUDRU_FOUNDATION_MODELS_API_KEY"] == "cloudru-key-1234567890"
    assert prepared["OUROBOROS_MODEL"] == "cloudru::zai-org/GLM-4.7"


def test_prepare_onboarding_settings_accepts_minimax_only_setup():
    payload = _base_payload()
    payload.update({
        "MINIMAX_API_KEY": "minimax-key-1234567890",
        "MINIMAX_REGION": "CN_ZH",
        "OUROBOROS_MODEL": "minimax::MiniMax-M3",
        "OUROBOROS_MODEL_HEAVY": "minimax::MiniMax-M3",
        "OUROBOROS_MODEL_LIGHT": "minimax::MiniMax-M2.7",
        "OUROBOROS_MODEL_FALLBACKS": "minimax::MiniMax-M2.7",
    })

    prepared, error = prepare_onboarding_settings(payload, {})

    assert error is None
    assert prepared["MINIMAX_API_KEY"] == "minimax-key-1234567890"
    assert prepared["MINIMAX_REGION"] == "cn_zh"
    assert prepared["OUROBOROS_MODEL"] == "minimax::MiniMax-M3"
    assert prepared["OUROBOROS_MODEL_LIGHT"] == "minimax::MiniMax-M2.7"


def test_prepare_onboarding_settings_rejects_unknown_minimax_region():
    payload = _base_payload()
    payload["MINIMAX_API_KEY"] = "minimax-key-1234567890"
    payload["MINIMAX_REGION"] = "unknown"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert prepared == {}
    assert error == "MiniMax Region must be global_en or cn_zh."


def test_prepare_onboarding_settings_accepts_anthropic_only_setup():
    payload = _base_payload()
    payload["ANTHROPIC_API_KEY"] = "sk-ant-1234567890"
    payload["OUROBOROS_MODEL"] = "anthropic::claude-opus-4-6"
    payload["OUROBOROS_MODEL_HEAVY"] = "anthropic::claude-opus-4-6"
    payload["OUROBOROS_MODEL_LIGHT"] = "anthropic::claude-sonnet-4-6"
    payload["OUROBOROS_MODEL_FALLBACKS"] = "anthropic::claude-sonnet-4-6"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert error is None
    assert prepared["ANTHROPIC_API_KEY"] == "sk-ant-1234567890"
    assert prepared["OUROBOROS_MODEL"] == "anthropic::claude-opus-4-6"


def test_prepare_onboarding_settings_rejects_local_only_cloud_routing():
    payload = _base_payload()
    payload["LOCAL_MODEL_SOURCE"] = "Qwen/Qwen2.5-7B-Instruct-GGUF"
    payload["LOCAL_MODEL_FILENAME"] = "qwen2.5-7b-instruct-q3_k_m.gguf"
    payload["LOCAL_ROUTING_MODE"] = "cloud"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert prepared == {}
    assert error == "Local-only setups must route at least one model to the local runtime."


def test_prepare_onboarding_settings_rejects_consciousness_only_local_routing():
    payload = _base_payload()
    payload["LOCAL_MODEL_SOURCE"] = "Qwen/Qwen2.5-7B-Instruct-GGUF"
    payload["LOCAL_MODEL_FILENAME"] = "qwen2.5-7b-instruct-q3_k_m.gguf"
    payload["LOCAL_ROUTING_MODE"] = "cloud"
    payload["USE_LOCAL_CONSCIOUSNESS"] = True

    prepared, error = prepare_onboarding_settings(payload, {})

    assert prepared == {}
    assert error == "Local-only setups must route at least one model to the local runtime."


def test_prepare_onboarding_settings_sets_all_local_routes():
    payload = _base_payload()
    payload["LOCAL_MODEL_SOURCE"] = "Qwen/Qwen2.5-7B-Instruct-GGUF"
    payload["LOCAL_MODEL_FILENAME"] = "qwen2.5-7b-instruct-q3_k_m.gguf"
    payload["LOCAL_ROUTING_MODE"] = "all"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert error is None
    assert prepared["USE_LOCAL_MAIN"] is True
    assert prepared["USE_LOCAL_HEAVY"] is True
    assert prepared["USE_LOCAL_LIGHT"] is True
    assert prepared["USE_LOCAL_FALLBACK"] is True


def test_prepare_onboarding_settings_preserves_non_wizard_provider_fields():
    """The wizard only edits fields it actually exposes. Settings fields
    that live in ``settings_ui.js`` but not in the wizard (``OPENAI_BASE_URL``,
    ``CLOUDRU_FOUNDATION_MODELS_BASE_URL``) must survive re-running onboarding.
    ``OPENAI_COMPATIBLE_*`` are now wizard-managed and come from the payload."""
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"
    payload["OPENAI_COMPATIBLE_BASE_URL"] = "https://compat.example/v1"
    payload["OPENAI_COMPATIBLE_API_KEY"] = "compat-secret-xyz"
    current = {
        "OPENAI_BASE_URL": "https://legacy.example/v1",
        "CLOUDRU_FOUNDATION_MODELS_BASE_URL": "https://cloud.example/v1",
    }

    prepared, error = prepare_onboarding_settings(payload, current)

    assert error is None
    # Non-wizard fields are preserved from current settings.
    assert prepared["OPENAI_BASE_URL"] == "https://legacy.example/v1"
    assert prepared["CLOUDRU_FOUNDATION_MODELS_BASE_URL"] == "https://cloud.example/v1"
    # Compatible fields come from the wizard payload.
    assert prepared["OPENAI_COMPATIBLE_BASE_URL"] == "https://compat.example/v1"
    assert prepared["OPENAI_COMPATIBLE_API_KEY"] == "compat-secret-xyz"


def test_prepare_onboarding_settings_accepts_openai_compatible_setup():
    """An OpenAI-compatible base URL alone (no key) is a valid remote provider."""
    payload = _base_payload()
    payload["OPENAI_COMPATIBLE_BASE_URL"] = "http://localhost:11434/v1"
    payload["OUROBOROS_MODEL"] = "openai-compatible::llama3"
    payload["OUROBOROS_MODEL_HEAVY"] = "openai-compatible::llama3"
    payload["OUROBOROS_MODEL_LIGHT"] = "openai-compatible::llama3"
    payload["OUROBOROS_MODEL_FALLBACKS"] = "openai-compatible::llama3"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert error is None
    assert prepared["OPENAI_COMPATIBLE_BASE_URL"] == "http://localhost:11434/v1"
    assert prepared["OPENAI_COMPATIBLE_API_KEY"] == ""
    assert prepared["OUROBOROS_MODEL"] == "openai-compatible::llama3"


def test_prepare_onboarding_settings_accepts_empty_heavy_and_light():
    """Role-model (v6.39): only Main is required; empty Heavy/Light fall back to Main, so
    the owner is not forced to fill every slot (mirrors the relaxed JS validateModelsStep
    and the live desktop launcher path)."""
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"
    payload["OUROBOROS_MODEL"] = "openai::gpt-5.5"
    payload["OUROBOROS_MODEL_HEAVY"] = ""
    payload["OUROBOROS_MODEL_LIGHT"] = ""

    prepared, error = prepare_onboarding_settings(payload, {})

    assert error is None
    assert prepared["OUROBOROS_MODEL"] == "openai::gpt-5.5"


def test_prepare_onboarding_settings_still_requires_main_model():
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"
    payload["OUROBOROS_MODEL"] = ""

    prepared, error = prepare_onboarding_settings(payload, {})

    assert prepared == {}
    assert "Main model" in error


def test_prepare_onboarding_settings_rejects_openai_compatible_key_without_base_url():
    payload = _base_payload()
    payload["OPENAI_COMPATIBLE_API_KEY"] = "compat-secret-xyz"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert prepared == {}
    assert "Configure OpenRouter, OpenAI, OpenAI-compatible, Cloud.ru, MiniMax, Anthropic, or a local model" in error


def test_onboarding_frontend_uses_base_url_first_compatible_validation():
    source = (REPO / "web/modules/onboarding_wizard.js").read_text(encoding="utf-8")

    assert "!['OPENAI_COMPATIBLE_API_KEY', 'MINIMAX_REGION'].includes(field.settingKey)" in source
    assert "const hasRemote = keyValues.some(([, value]) => value);" not in source


def test_build_onboarding_html_serves_a_real_page_with_linked_modules():
    """ONE onboarding host: the page LINKS its stylesheet and its ES module from
    /static instead of inlining them, so the wizard's steps can import ordinary
    web/modules/* code. The bootstrap stays inline (it is per-install state)."""
    html = build_onboarding_html({})

    assert '<script type="module" src="/static/modules/onboarding_wizard.js"></script>' in html
    assert '<link rel="stylesheet" href="/static/onboarding.css">' in html
    assert '"contract": {' in html
    assert '"providerFields": [' in html
    assert "openai::gpt-5.6-terra" in html
    assert "openai::gpt-5.6-luna" in html
    assert "anthropic::claude-sonnet-5" in html
    assert "minimax::MiniMax-M3" in html
    assert "minimax::MiniMax-M2.7" in html
    # The wizard's own source is no longer copied into the page body.
    assert "const STEP_ORDER = bootstrap.stepOrder" not in html


def test_onboarding_bootstrap_cannot_break_out_of_its_inline_script():
    """A stored provider value containing ``</script>`` must not close the
    element. ensure_ascii does not escape ``<``; the renderer does."""
    html = build_onboarding_html({"OPENAI_COMPATIBLE_BASE_URL": "https://x/</script><b>"})

    assert "</script><b>" not in html
    assert "\\u003c/script>\\u003cb>" in html


def test_onboarding_wizard_module_keeps_its_multistep_contract():
    source = (REPO / "web/modules/onboarding_wizard.js").read_text(encoding="utf-8")

    assert "const STEP_ORDER = bootstrap.stepOrder || (SETUP_CONTRACT.steps || []).map" in source
    assert "Add your access" in source or "Local model settings" in source
    assert "function detectProviderProfile()" in source
    assert "function activeProviderProfile()" in source
    assert "function profileLabel(profile)" in source
    assert "function nextButtonShouldBeDisabled()" in source
    assert "function syncCurrentStepActionState()" in source
    assert "return 'direct-multi';" in source
    assert "PROVIDER_FIELDS.map((field) => [field.settingKey, trim(state[field.stateKey])])" in source
    assert "MODEL_SLOTS.map((slot) => [slot.settingKey, trim(state[slot.stateKey])])" in source
    assert "LOCAL_ROUTING_MODE: trim(state.localSource) ? (trim(state.localRoutingMode) || 'cloud') : 'cloud'" in source


def test_onboarding_steps_and_stylesheet_keep_their_owner_facing_shape():
    contract = build_setup_contract("web")
    titles = {step["title"] for step in contract["steps"]}
    rails = {step["railCopy"] for step in contract["steps"]}
    css = (REPO / "web" / "onboarding.css").read_text(encoding="utf-8")

    assert {"Add your access", "Choose models", "Choose review mode", "Set your budget"} <= titles
    assert {"Keys + local", "model slots"} <= rails
    assert "@media (max-width: 720px)" in css
    assert "scroll-snap-type: x proximity;" in css


def test_build_onboarding_html_accepts_web_host_mode():
    html = build_onboarding_html({}, host_mode="web")

    assert '"hostMode": "web"' in html
    assert '"supportsLocalRuntimeControls": true' in html


def test_setup_contract_groups_rarely_used_providers():
    """The provider-field ``group`` column drives the onboarding layout:
    Cloud.ru and both OpenAI-compatible fields collapse under "More options"
    while OpenRouter stays pinned at index 0 of the always-visible group."""
    contract = build_setup_contract("web")
    fields = contract["providerFields"]

    assert fields[0]["settingKey"] == "OPENROUTER_API_KEY"
    assert {field["settingKey"]: field["group"] for field in fields} == {
        "OPENROUTER_API_KEY": "primary",
        "OPENAI_API_KEY": "primary",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY": "more",
        "MINIMAX_API_KEY": "more",
        "MINIMAX_REGION": "more",
        "ANTHROPIC_API_KEY": "primary",
        "OPENAI_COMPATIBLE_BASE_URL": "more",
        "OPENAI_COMPATIBLE_API_KEY": "more",
    }


def test_onboarding_wizard_collapses_rarely_used_providers():
    source = (REPO / "web/modules/onboarding_wizard.js").read_text(encoding="utf-8")

    # Second access-step collapse hosting the "more" provider group.
    assert 'data-collapse="more-providers"' in source
    assert 'data-collapse="local-model"' in source
    assert "More options" in source
    assert "hasMoreProviderValue()" in source
    # Both collapses bind through scoped data-collapse selectors; the old
    # singular selector only ever reached the FIRST details element.
    assert "root.querySelector('.wizard-collapse')" not in source
    assert "moreProvidersOpen" in source


def test_setup_contract_has_no_secret_values():
    contract = build_setup_contract("web")
    text = repr(contract)
    budget_fields = {field["settingKey"]: field for field in contract["budgetFields"]}

    assert contract["hostMode"] == "web"
    assert "providerFields" in contract
    assert budget_fields["TOTAL_BUDGET"]["settingsInputId"] == "s-total-budget"
    assert budget_fields["TOTAL_BUDGET"]["min"] == "0.01"
    assert budget_fields["TOTAL_BUDGET"]["step"] == "any"
    assert budget_fields["OUROBOROS_PER_TASK_COST_USD"]["settingsInputId"] == "s-settings-per-task-cost"
    assert "settingsInputId" in contract["providerFields"][0]
    assert "OPENROUTER_API_KEY" in text
    assert "sk-or-v1-super-secret" not in text
    assert "sk-ant-super-secret" not in text
    suggestions = build_setup_bootstrap({}, "web")["modelSuggestions"]
    assert "anthropic/claude-sonnet-5" in suggestions
    assert "anthropic::claude-sonnet-5" in suggestions
    assert "minimax::MiniMax-M3" in suggestions
    assert "minimax::MiniMax-M2.7" in suggestions

    configured_value = "minimax-hidden-value"
    bootstrap = build_setup_bootstrap({"MINIMAX_API_KEY": configured_value}, "web")
    initial = bootstrap["initialState"]
    assert initial["providerProfile"] == "minimax"
    assert initial["mainModel"] == "minimax::MiniMax-M3"
    assert initial["lightModel"] == "minimax::MiniMax-M2.7"


# --- The served page must not hand back a stored credential -----------------
# The onboarding page is an unauthenticated GET on every host, and a non-loopback
# bind without OUROBOROS_NETWORK_PASSWORD is a supported configuration — so any
# client on the LAN could read whatever the bootstrap carries.

_SECRET_CANARIES = {
    "OPENROUTER_API_KEY": "sk-or-SECRETCANARY123",
    "OPENAI_API_KEY": "sk-openai-SECRETCANARY124",
    "OPENAI_COMPATIBLE_API_KEY": "compat-SECRETCANARY125",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY": "cloudru-SECRETCANARY126",
    "MINIMAX_API_KEY": "minimax-SECRETCANARY127",
    "ANTHROPIC_API_KEY": "sk-ant-SECRETCANARY128",
    "GIGACHAT_CREDENTIALS": "giga-SECRETCANARY129",
    "GIGACHAT_PASSWORD": "gigapw-SECRETCANARY130",
    "GITHUB_TOKEN": "ghp-SECRETCANARY131",
    "OUROBOROS_NETWORK_PASSWORD": "netpw-SECRETCANARY132",
}


def test_every_secret_class_field_is_covered_by_the_canary_sweep():
    """The sweep below is only a class proof while it actually covers the class.

    Both authorities: the canonical credential keys, and every password-typed
    provider field (a new provider is usually added as the latter first).
    """
    from ouroboros.settings_setup_contract import (
        SECRET_SETTING_KEYS,
        secret_provider_setting_keys,
    )

    assert SECRET_SETTING_KEYS <= set(_SECRET_CANARIES)
    assert secret_provider_setting_keys() <= set(_SECRET_CANARIES)


def test_the_served_wizard_never_carries_a_stored_credential():
    """Not one masked field — every secret-class field, on the renderer both
    page routes share."""
    html = build_onboarding_html(dict(_SECRET_CANARIES), host_mode="web")

    leaked = sorted(key for key, value in _SECRET_CANARIES.items() if value in html)
    assert leaked == [], f"credential served verbatim in the onboarding page: {leaked}"


def test_a_configured_credential_is_reported_as_a_marker_not_a_prefix():
    """The wizard still learns WHICH providers are configured — that is the only
    fact it needs — and the marker leaks nothing about the value: no prefix, no
    length, nothing that narrows a guess."""
    from ouroboros.settings_setup_contract import (
        CONFIGURED_SECRET_PLACEHOLDER,
        secret_provider_setting_keys,
    )

    bootstrap = build_setup_bootstrap(dict(_SECRET_CANARIES), "web")
    state = bootstrap["initialState"]
    by_key = {field["settingKey"]: field for field in bootstrap["contract"]["providerFields"]}

    assert bootstrap["secretPlaceholder"] == CONFIGURED_SECRET_PLACEHOLDER
    for setting_key in secret_provider_setting_keys():
        value = state[by_key[setting_key]["stateKey"]]
        assert value == CONFIGURED_SECRET_PLACEHOLDER, setting_key
        # Not a redaction of the secret: no leading characters of it survive.
        assert not _SECRET_CANARIES[setting_key].startswith(value[:4])

    # An UNCONFIGURED provider still reads as unconfigured, or the wizard would
    # claim every key is set.
    empty = build_setup_bootstrap({}, "web")["initialState"]
    assert empty[by_key["OPENROUTER_API_KEY"]["stateKey"]] == ""


def test_a_wizard_save_that_touches_no_credential_leaves_the_stored_one_intact():
    """The untouched field posts the MARKER back. It must resolve to the stored
    secret — byte for byte — and the marker must never reach settings.json."""
    from ouroboros.settings_setup_contract import (
        CONFIGURED_SECRET_PLACEHOLDER,
        secret_provider_setting_keys,
    )

    stored = dict(_SECRET_CANARIES)
    payload = _base_payload()
    for setting_key in secret_provider_setting_keys():
        payload[setting_key] = CONFIGURED_SECRET_PLACEHOLDER

    prepared, error = prepare_onboarding_settings(payload, stored)

    assert error is None
    for setting_key in secret_provider_setting_keys():
        assert prepared[setting_key] == stored[setting_key], setting_key
    assert CONFIGURED_SECRET_PLACEHOLDER not in prepared.values()


def test_the_marker_can_never_become_a_credential():
    """With nothing stored, the marker resolves to EMPTY rather than being
    written as if the owner had typed it — otherwise a client echoing back what
    it was served could author a fake key, and the install would look configured
    while every provider call failed."""
    from ouroboros.settings_setup_contract import CONFIGURED_SECRET_PLACEHOLDER

    payload = _base_payload()
    payload["OPENROUTER_API_KEY"] = CONFIGURED_SECRET_PLACEHOLDER
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert error is None
    assert prepared["OPENROUTER_API_KEY"] == ""
    assert prepared["OPENAI_API_KEY"] == "sk-openai-1234567890"


def test_the_generic_settings_merge_also_refuses_to_store_a_mask():
    """The other save path the wizard can take. Same rule, so the class is closed
    wherever a masked value is echoed back, not just in the setup validator."""
    from ouroboros.gateway.settings import _merge_settings_payload

    merged = _merge_settings_payload(
        {"OPENROUTER_API_KEY": "sk-or-v1-stored"},
        {"OPENROUTER_API_KEY": "***set***", "ANTHROPIC_API_KEY": "***set***"},
    )

    assert merged["OPENROUTER_API_KEY"] == "sk-or-v1-stored"
    assert merged.get("ANTHROPIC_API_KEY", "") == ""


def test_clearing_a_credential_field_still_clears_the_stored_secret():
    """The marker means "untouched", never "locked": an emptied field is an
    explicit owner decision and must still delete the stored key."""
    stored = {"OPENROUTER_API_KEY": "sk-or-v1-stored", "OPENAI_API_KEY": "sk-openai-1234567890"}
    payload = _base_payload()
    payload["OPENROUTER_API_KEY"] = ""
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"

    prepared, error = prepare_onboarding_settings(payload, stored)

    assert error is None
    assert prepared["OPENROUTER_API_KEY"] == ""


def test_api_settings_exposes_setup_contract_without_secrets(tmp_path):
    from unittest.mock import patch

    import server as srv
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    secret = "sk-or-v1-super-secret-token"
    patches = [
        patch.object(srv, "load_settings", return_value={"OPENROUTER_API_KEY": secret}),
        patch.object(srv, "apply_runtime_provider_defaults", lambda settings: (dict(settings), False, [])),
        patch("ouroboros.server_auth.get_configured_network_password", return_value=""),
    ]
    for item in patches:
        item.start()
    try:
        app = Starlette(routes=[Route("/api/settings", endpoint=srv.api_settings_get, methods=["GET"])])
        app.state.drive_root = tmp_path
        with TestClient(app) as client:
            response = client.get("/api/settings")
        assert response.status_code == 200
        assert secret not in response.text
        contract = response.json()["_meta"]["setup_contract"]
        assert contract["providerFields"][0]["settingKey"] == "OPENROUTER_API_KEY"
        assert secret not in repr(contract)
    finally:
        for item in patches:
            item.stop()


def test_onboarding_wizard_keeps_the_claude_runtime_cta_on_http_transports():
    """The Claude-runtime card stays, but its status/repair calls are ordinary
    endpoints on every host now — there is no parallel desktop bridge for them."""
    source = (REPO / "web/modules/onboarding_wizard.js").read_text(encoding="utf-8")

    assert "Claude Runtime" in source or "Claude runtime" in source
    assert "Skip for now" in source
    assert "/api/claude-code/status" in source
    assert "/api/claude-code/install" in source


def _launcher_hosts_onboarding_on_the_live_server() -> bool:
    host = REPO / "ouroboros" / "launcher_onboarding.py"
    if not host.exists() or not (REPO / "launcher.py").exists():
        return False
    source = host.read_text(encoding="utf-8")
    return all(marker in source for marker in (
        "has_startup_ready_provider(settings)",
        "prepare_onboarding_settings(data, settings)",
        "def present_first_run_onboarding(",
    ))

_LAUNCHER_HOSTS_ONBOARDING = _launcher_hosts_onboarding_on_the_live_server()

@pytest.mark.skipif(
    not _LAUNCHER_HOSTS_ONBOARDING,
    reason="launcher.py does not host onboarding (may be an older bundle or post-refactor version)",
)
def test_launcher_points_first_run_onboarding_at_the_live_server():
    """The desktop setup window loads the SAME served page a browser owner sees,
    at the authoritative loopback port — not a detached pre-server document."""
    source = (REPO / "ouroboros" / "launcher_onboarding.py").read_text(encoding="utf-8")
    launcher_source = (REPO / "launcher.py").read_text(encoding="utf-8")

    assert "_present_first_run_onboarding(" in launcher_source
    assert 'url=f"http://127.0.0.1:{port}/onboarding"' in source
    assert "has_startup_ready_provider(settings)" in source
    assert "prepare_onboarding_settings(data, settings)" in source
    # The desktop bridge is window lifecycle + the disclosed legacy save only.
    assert "def claude_code_status(self) -> dict:" not in source
    assert "def install_claude_code(self) -> dict:" not in source
    assert "def fetch_compatible_models(self" not in source


def test_web_style_contains_onboarding_overlay_shell():
    style = (REPO / "web" / "style.css").read_text(encoding="utf-8")

    assert ".onboarding-overlay {" in style
    assert ".onboarding-frame {" in style
    assert ".onboarding-overlay-backdrop {" in style
    assert ".onboarding-restart-card {" in style


def test_onboarding_overlay_surfaces_restart_required_message():
    source = (REPO / "web" / "modules" / "onboarding_overlay.js").read_text(encoding="utf-8")

    assert "showRestartRequiredOverlay" in source
    assert "event.data.restart_required" in source
    assert "Continue in current mode" in source


def test_onboarding_overlay_frames_the_served_page_not_an_inlined_document():
    """The blocking overlay of an unconfigured running app still appears, but it
    frames the one onboarding host so the wizard can import web/modules/*."""
    source = (REPO / "web" / "modules" / "onboarding_overlay.js").read_text(encoding="utf-8")

    assert "frame.src = '/onboarding'" in source
    assert "frame.srcdoc" not in source
    # 204 from the readiness probe still means "configured — no overlay", and it
    # is the ONLY answer that takes the blocking shell down (see
    # web/tests/onboarding_overlay.test.js for the behavioural pin).
    assert "if (response.status === 204) {" in source
    assert "removeOverlay();" in source


# --- v6.82.0 rev.3-2: the wizard save authors safety "light" for NEW installs ---


def _fresh_settings_path(monkeypatch, tmp_path):
    from ouroboros import config as cfg

    monkeypatch.setattr(cfg, "SETTINGS_PATH", tmp_path / "settings.json")
    return cfg


def test_fresh_install_is_eligible_for_light_but_the_shared_validator_never_authors_it(
    monkeypatch, tmp_path,
):
    """A fresh install (no settings file) is ELIGIBLE for the new-install "light"
    coverage, but only the DESKTOP launcher authors it: the shared validator is also
    the web/Docker path, which posts through the non-owner generic /api/settings.
    SETTINGS_DEFAULTS itself stays "full" (rev.3-2)."""
    from ouroboros.config import SETTINGS_DEFAULTS
    from ouroboros.settings_setup_contract import wizard_authors_safety_light

    assert SETTINGS_DEFAULTS["OUROBOROS_SAFETY_MODE"] == "full"
    _fresh_settings_path(monkeypatch, tmp_path)  # no file on disk
    assert wizard_authors_safety_light() is True

    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"
    prepared, error = prepare_onboarding_settings(payload, {"OUROBOROS_SAFETY_MODE": "full"})

    assert error is None
    # The validator passes the CURRENT value through untouched — no authorship here.
    assert prepared["OUROBOROS_SAFETY_MODE"] == "full"


def test_wizard_save_respects_explicitly_stored_safety_mode(monkeypatch, tmp_path):
    """An install whose settings file explicitly carries a safety mode keeps it —
    re-running the wizard never lowers (or raises) an explicit owner choice."""
    import json

    cfg = _fresh_settings_path(monkeypatch, tmp_path)
    cfg.SETTINGS_PATH.write_text(json.dumps({"OUROBOROS_SAFETY_MODE": "full"}), encoding="utf-8")

    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"
    prepared, error = prepare_onboarding_settings(payload, {"OUROBOROS_SAFETY_MODE": "full"})

    assert error is None
    assert prepared["OUROBOROS_SAFETY_MODE"] == "full"


def test_wizard_authored_light_persists_and_generic_save_still_refuses(monkeypatch, tmp_path):
    """End-to-end persist seam: the launcher's wizard save names the key as authored
    and is allowed past the full->light ratchet; a plain (non-authored) save of the
    same lowering keeps raising PermissionError (the ratchet is untouched)."""
    import json

    from ouroboros import config as cfg

    monkeypatch.setattr(cfg, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.delenv("OUROBOROS_SAFETY_MODE", raising=False)

    with pytest.raises(PermissionError, match="OUROBOROS_SAFETY_MODE lowering refused"):
        cfg.save_settings({"OUROBOROS_SAFETY_MODE": "light", "TOTAL_BUDGET": 10})

    cfg.save_settings(
        {"OUROBOROS_SAFETY_MODE": "light", "TOTAL_BUDGET": 10},
        onboarding_safety_default=True,
    )
    stored = json.loads(cfg.SETTINGS_PATH.read_text(encoding="utf-8"))
    assert stored["OUROBOROS_SAFETY_MODE"] == "light"

    # The narrow flag authorizes EXACTLY the fresh-install light authorship: once a
    # settings file exists it cannot lower again, and it can never authorize "off".
    with pytest.raises(PermissionError, match="OUROBOROS_SAFETY_MODE lowering refused"):
        cfg.save_settings(
            {"OUROBOROS_SAFETY_MODE": "off", "TOTAL_BUDGET": 10},
            onboarding_safety_default=True,
        )
    cfg.SETTINGS_PATH.unlink()
    with pytest.raises(PermissionError, match="OUROBOROS_SAFETY_MODE lowering refused"):
        cfg.save_settings(
            {"OUROBOROS_SAFETY_MODE": "off", "TOTAL_BUDGET": 10},
            onboarding_safety_default=True,
        )


def test_web_onboarding_host_never_authors_light(monkeypatch, tmp_path):
    """The web/Docker wizard posts the SAME payload through generic /api/settings,
    which drops the owner-only safety key — so a fresh web setup keeps Full. Pinned
    because the shared validator (used by both hosts) must never author it."""
    from ouroboros.gateway import settings as gw_settings

    _fresh_settings_path(monkeypatch, tmp_path)
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"
    prepared, error = prepare_onboarding_settings(payload, {"OUROBOROS_SAFETY_MODE": "full"})
    assert error is None
    assert prepared["OUROBOROS_SAFETY_MODE"] == "full"
    # And the generic settings surface (the web wizard's save path) drops the key.
    source = pathlib.Path(gw_settings.__file__).read_text(encoding="utf-8")
    assert '"OUROBOROS_SAFETY_MODE",' in source


def test_settings_save_refuses_lock_timeout_without_deleting_the_owner_lock(
    monkeypatch, tmp_path,
):
    """A contending writer keeps both its lock and the prior settings bytes."""
    from ouroboros import config as cfg

    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"TOTAL_BUDGET": 7}', encoding="utf-8")
    lock_path = tmp_path / "settings.json.lock"
    lock_path.write_text("other-writer", encoding="utf-8")
    monkeypatch.setattr(cfg, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "_acquire_settings_lock", lambda timeout=2.0: None)

    with pytest.raises(TimeoutError, match="settings lock"):
        cfg.save_settings({"TOTAL_BUDGET": 99})

    assert settings_path.read_text(encoding="utf-8") == '{"TOTAL_BUDGET": 7}'
    assert lock_path.read_text(encoding="utf-8") == "other-writer"


def test_launcher_binds_the_settings_writer_the_wizard_callback_calls():
    """The desktop wizard save callback calls `save_settings(...)` directly; pin that
    the name is BOUND in launcher's namespace (a NameError there would break every
    fresh desktop onboarding, and launcher.py is outside most deterministic gates)."""
    from ouroboros import launcher_onboarding

    assert callable(getattr(launcher_onboarding, "save_settings", None))
    source = pathlib.Path(launcher_onboarding.__file__).read_text(encoding="utf-8")
    assert "onboarding_safety_default=wizard_authors_safety" in source


def test_wizard_rejects_a_newly_typed_short_key():
    """The length check still guards a credential the owner actually authored."""
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"
    payload["OPENAI_COMPATIBLE_API_KEY"] = "ollama"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert prepared == {}
    assert error == "OpenAI-compatible API key looks too short."


def test_wizard_is_not_deadlocked_by_a_short_key_already_on_disk():
    """A stored too-short key must not veto the save that replaces it.

    build_initial_setup_state prefills every provider field from disk, so an
    untouched field posts the stored value back. Rejecting it discards the WHOLE
    payload — including the replacement typed in the same form — which makes the
    offending value the one value the wizard can never overwrite.

    The stored fixture deliberately carries ONLY the short key: a stored
    OPENAI_COMPATIBLE_BASE_URL would make has_startup_ready_provider() true and
    the wizard would never open for this install in the first place.
    """
    stored = {
        "OPENAI_COMPATIBLE_API_KEY": "ollama",
    }

    # 1. An unrelated change saves even though the short key rides along untouched.
    payload = _base_payload()
    payload["OPENAI_COMPATIBLE_API_KEY"] = "ollama"
    payload["OPENAI_COMPATIBLE_BASE_URL"] = "http://127.0.0.1:4000/v1"
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"

    prepared, error = prepare_onboarding_settings(payload, stored)

    assert error is None
    assert prepared["OPENAI_COMPATIBLE_API_KEY"] == "ollama"

    # 2. The owner can replace the short key with a real one.
    payload["OPENAI_COMPATIBLE_API_KEY"] = "sk-replacement-key-1234"

    prepared, error = prepare_onboarding_settings(payload, stored)

    assert error is None
    assert prepared["OPENAI_COMPATIBLE_API_KEY"] == "sk-replacement-key-1234"


def test_wizard_still_rejects_shortening_a_stored_key():
    """Editing a stored key DOWN to a too-short value is authorship, not a prefill."""
    stored = {"OPENAI_API_KEY": "sk-openai-1234567890"}
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-short"

    prepared, error = prepare_onboarding_settings(payload, stored)

    assert prepared == {}
    assert error == "OpenAI API key looks too short."


def test_onboarding_frontend_exempts_unchanged_prefilled_keys_from_length_check():
    """The client-side mirror of the length check carries the same authorship rule.

    validateProvidersStep() runs the identical <10 rule against state prefilled
    from disk and blocks Next/Save BEFORE the payload reaches the server, so the
    server-side exemption alone leaves the wizard deadlocked. The JS must skip
    the length check for a value equal to its INITIAL_STATE prefill — and only
    for that value, so a newly typed short key is still rejected client-side.
    """
    source = (REPO / "web/modules/onboarding_wizard.js").read_text(encoding="utf-8")

    assert "value.length < 10 && value !== trim(INITIAL_STATE[field.stateKey])" in source

    # The exemption is now load-bearing for EVERY configured install, not just
    # legacy short keys: a prefilled field holds the configured-marker, which is
    # shorter than the minimum. Without the exemption the wizard would refuse to
    # advance on any install that already has a provider key.
    from ouroboros.settings_setup_contract import CONFIGURED_SECRET_PLACEHOLDER

    assert len(CONFIGURED_SECRET_PLACEHOLDER) < 10
