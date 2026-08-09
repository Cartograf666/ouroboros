"""POST /api/onboarding/complete — the ONE atomic onboarding transaction (D-8).

These tests use the REAL persist path against an isolated settings file, so
"nothing was saved" is proved by the file on disk rather than by a mock that was
not called.
"""

from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros.subscription_install_presets import PRESET_MARKER_KEY

_PROVIDER_ENV = (
    "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MINIMAX_API_KEY",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY", "GIGACHAT_CREDENTIALS", "GIGACHAT_USER",
    "GIGACHAT_PASSWORD", "OPENAI_COMPATIBLE_BASE_URL", "OPENAI_BASE_URL",
    "USE_LOCAL_MAIN", "USE_LOCAL_HEAVY", "USE_LOCAL_LIGHT", "USE_LOCAL_FALLBACK",
    "USE_LOCAL_CONSCIOUSNESS", "LOCAL_MODEL_SOURCE", PRESET_MARKER_KEY,
    "OUROBOROS_REVIEWER_SLOTS", "OUROBOROS_SUBAGENT_HARNESS", "OUROBOROS_SAFETY_MODE",
)

LIVE_SNAPSHOT = {
    "daemon": {"state": "running"},
    "harnesses": [
        {
            "id": "claude", "status": "ok", "enabled": True,
            "access_profiles_supported": ["readonly", "workspace_write"],
            "models": [{"id": "claude-opus-5"}, {"id": "claude-sonnet-5"},
                       {"id": "claude-fable-5"}, {"id": "claude-opus-4-6"}],
        },
        {
            "id": "codex", "status": "ok", "enabled": True,
            "models": [{"id": "gpt-5.6-sol"}, {"id": "gpt-5.6-terra"}, {"id": "gpt-5.5"}],
        },
    ],
    "profiles": {
        "harnessAccounts": [
            {"harness_id": "claude", "native_login_detected": True},
            {"harness_id": "codex", "native_login_detected": True},
        ],
        "profiles": [],
    },
}

WIZARD_PAYLOAD = {
    "OPENROUTER_API_KEY": "sk-or-v1-abcdefghijklmnop",
    "OUROBOROS_MODEL": "openai/gpt-5.6-luna",
    "OUROBOROS_REVIEW_ENFORCEMENT": "advisory",
    "OUROBOROS_RUNTIME_MODE": "advanced",
    "TOTAL_BUDGET": 25.0,
    "OUROBOROS_PER_TASK_COST_USD": 5.0,
}


class _Harness:
    """One isolated onboarding server plus the calls it made."""

    def __init__(self, client, settings_path, calls):
        self.client = client
        self.settings_path = settings_path
        self.calls = calls

    def saved(self) -> dict:
        if not self.settings_path.exists():
            return {}
        return json.loads(self.settings_path.read_text(encoding="utf-8"))


@pytest.fixture
def onboarding(monkeypatch, tmp_path):
    """Build the endpoint over an isolated settings file (real writes)."""
    import ouroboros.config as config
    import ouroboros.gateway.onboarding as gw_onboarding
    import ouroboros.gateway.settings as gw_settings

    for key in _PROVIDER_ENV:
        monkeypatch.delenv(key, raising=False)
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_PATH", settings_path)

    calls: dict = {"snapshot": 0, "env": [], "supervisor": 0, "side_effects": 0}

    def _snapshot():
        calls["snapshot"] += 1
        payload = calls.get("snapshot_payload", LIVE_SNAPSHOT)
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(gw_onboarding, "_read_harness_snapshot", _snapshot)
    monkeypatch.setattr(config, "apply_settings_to_env",
                        lambda settings: calls["env"].append(dict(settings)))

    def _supervisor(_request, _settings):
        calls["supervisor"] += 1
        return True

    def _side_effects(*_a, **_k):
        calls["side_effects"] += 1

    monkeypatch.setattr(gw_settings, "_start_supervisor_if_needed_for_request", _supervisor)
    monkeypatch.setattr(gw_settings, "_apply_settings_save_side_effects", _side_effects)

    app = Starlette(routes=[
        Route("/api/onboarding/complete",
              endpoint=gw_onboarding.api_onboarding_complete, methods=["POST"]),
    ])
    app.state.drive_root = tmp_path
    app.state.repo_dir = tmp_path / "repo"
    with TestClient(app) as client:
        yield _Harness(client, settings_path, calls)


def test_fresh_install_applies_the_preset_in_one_write(onboarding):
    response = onboarding.client.post(
        "/api/onboarding/complete",
        json={**WIZARD_PAYLOAD, "subscriptionsConnected": True},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["preset"]["applied"] is True
    assert body["preset"]["reason"] == "applied"
    assert body["preset"]["harnesses"] == ["claude", "codex"]
    assert onboarding.calls["snapshot"] == 1  # exactly ONE daemon read

    saved = onboarding.saved()
    assert saved[PRESET_MARKER_KEY] == "1"
    assert saved["OUROBOROS_SUBAGENT_HARNESS"] == "claude=claude-opus-5:medium"
    slots = json.loads(saved["OUROBOROS_REVIEWER_SLOTS"])
    assert [row["route"]["target_id"] for row in slots["triad"]] == [
        "claude=claude-opus-5", "codex=gpt-5.6-sol", "claude=claude-sonnet-5"]
    assert slots["scope"][0]["route"]["target_id"] == "codex=gpt-5.6-sol"
    assert slots["advisory"]["route"]["target_id"] == "claude=claude-sonnet-5"
    # Everything else of the transaction landed in the SAME file.
    assert saved["OUROBOROS_RUNTIME_MODE"] == "advanced"
    assert saved["OPENROUTER_API_KEY"] == WIZARD_PAYLOAD["OPENROUTER_API_KEY"]
    assert saved["OUROBOROS_SAFETY_MODE"] == "light"  # fresh-install default
    # D-2: the API model slots are untouched by the preset.
    assert saved["OUROBOROS_MODEL"] == "openai/gpt-5.6-luna"
    assert onboarding.calls["supervisor"] == 1


def test_daemon_unavailable_persists_nothing_and_keeps_the_wizard_open(onboarding):
    onboarding.calls["snapshot_payload"] = {
        "daemon": {"state": "unreachable", "last_error": "connect_failed: boom"},
        "harnesses": [], "profiles": {},
    }

    response = onboarding.client.post(
        "/api/onboarding/complete",
        json={**WIZARD_PAYLOAD, "subscriptionsConnected": True},
    )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "daemon_unavailable"
    assert body["can_skip"] is True
    assert body["saved"] is False
    assert "could not be verified" in body["error"]
    # NOTHING was written: no settings file, no supervisor, no env projection.
    assert not onboarding.settings_path.exists()
    assert onboarding.calls["supervisor"] == 0
    assert onboarding.calls["env"] == []


def test_unresolvable_model_refuses_before_any_write(onboarding):
    onboarding.calls["snapshot_payload"] = {
        "daemon": {"state": "running"},
        "harnesses": [{"id": "claude", "status": "ok",
                       "models": [{"id": "claude-opus-5"}, {"id": "claude-sonnet-5"},
                                  {"id": "claude-fable-5"}]}],
        "profiles": {"harnessAccounts": [
            {"harness_id": "claude", "native_login_detected": True}]},
    }

    response = onboarding.client.post(
        "/api/onboarding/complete",
        json={**WIZARD_PAYLOAD, "subscriptionsConnected": True},
    )

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "model_not_in_discovery"
    assert not onboarding.settings_path.exists()


def test_a_signed_in_account_without_discovery_is_a_typed_failure(onboarding):
    onboarding.calls["snapshot_payload"] = {
        "daemon": {"state": "running"},
        "harnesses": [{"id": "claude", "models": [], "models_error": "harness_unavailable"}],
        "profiles": {"harnessAccounts": [
            {"harness_id": "claude", "native_login_detected": True}]},
    }

    response = onboarding.client.post(
        "/api/onboarding/complete",
        json={**WIZARD_PAYLOAD, "subscriptionsConnected": True},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "models_unavailable"
    assert not onboarding.settings_path.exists()


def test_skip_flag_completes_without_any_preset_key(onboarding):
    response = onboarding.client.post(
        "/api/onboarding/complete",
        json={**WIZARD_PAYLOAD, "subscriptionsConnected": True,
              "skipSubscriptionPresets": True},
    )

    assert response.status_code == 200, response.text
    assert response.json()["preset"] == {
        "applied": False, "reason": "skipped_by_owner", "harnesses": [], "receipt": {}}
    assert onboarding.calls["snapshot"] == 0  # the daemon is never even asked

    saved = onboarding.saved()
    assert saved  # onboarding DID complete
    assert not saved.get(PRESET_MARKER_KEY)
    assert not saved.get("OUROBOROS_REVIEWER_SLOTS")
    assert not saved.get("OUROBOROS_SUBAGENT_HARNESS")
    assert onboarding.calls["supervisor"] == 1


def test_no_subscription_declared_never_reads_the_daemon(onboarding):
    response = onboarding.client.post("/api/onboarding/complete", json=dict(WIZARD_PAYLOAD))

    assert response.status_code == 200, response.text
    assert response.json()["preset"]["reason"] == "not_requested"
    assert onboarding.calls["snapshot"] == 0


def test_a_desktop_shaped_completion_still_authors_light_on_a_fresh_install(onboarding):
    """D-8 closure proof. The desktop `save_wizard` bridge existed for exactly
    ONE reason: a genuinely fresh install had to author the new-install `light`
    safety coverage, and neither the shared validator nor the generic settings
    endpoint may lower safety. Removing that bridge is only honest if the same
    completion the desktop window now posts still lands `light`."""
    assert not onboarding.settings_path.exists()   # genuinely fresh

    response = onboarding.client.post("/api/onboarding/complete", json=dict(WIZARD_PAYLOAD))

    assert response.status_code == 200, response.text
    assert onboarding.saved()["OUROBOROS_SAFETY_MODE"] == "light"


def test_a_completion_over_an_existing_install_can_never_lower_safety(onboarding):
    """The other half: the ratchet. With a settings file already on disk the
    install is not fresh, so completion authors nothing — and a payload that
    tries to carry the lowering itself gets no further, because
    OUROBOROS_SAFETY_MODE is not a wizard field at all."""
    onboarding.settings_path.write_text(
        json.dumps({"OUROBOROS_SAFETY_MODE": "full", "OPENROUTER_API_KEY": "sk-or-v1-existing"}),
        encoding="utf-8")

    response = onboarding.client.post(
        "/api/onboarding/complete",
        json={**WIZARD_PAYLOAD, "OUROBOROS_SAFETY_MODE": "light"})

    assert response.status_code == 200, response.text
    assert onboarding.saved()["OUROBOROS_SAFETY_MODE"] == "full"


def test_old_install_is_not_retro_presetted(onboarding):
    """An install that already has a provider is past install time — the missing
    marker is NOT permission to preset it (every pre-preset install lacks one)."""
    onboarding.settings_path.write_text(json.dumps({
        "OPENROUTER_API_KEY": "sk-or-v1-existing-install-key",
        "OUROBOROS_MODEL": "openai/gpt-5.6-luna",
        "OUROBOROS_SAFETY_MODE": "full",
    }), encoding="utf-8")

    response = onboarding.client.post(
        "/api/onboarding/complete",
        json={**WIZARD_PAYLOAD, "subscriptionsConnected": True},
    )

    assert response.status_code == 200, response.text
    assert response.json()["preset"]["applied"] is False
    assert response.json()["preset"]["reason"] == "not_install_time"
    assert onboarding.calls["snapshot"] == 0
    saved = onboarding.saved()
    assert not saved.get(PRESET_MARKER_KEY)
    assert not saved.get("OUROBOROS_REVIEWER_SLOTS")
    # The fresh-install safety default is likewise not re-authored over it.
    assert saved["OUROBOROS_SAFETY_MODE"] == "full"


def test_a_presetted_install_is_never_presetted_twice(onboarding):
    first = onboarding.client.post(
        "/api/onboarding/complete",
        json={**WIZARD_PAYLOAD, "subscriptionsConnected": True})
    assert first.json()["preset"]["applied"] is True

    second = onboarding.client.post(
        "/api/onboarding/complete",
        json={**WIZARD_PAYLOAD, "subscriptionsConnected": True})

    assert second.status_code == 200, second.text
    assert second.json()["preset"]["reason"] == "not_install_time"
    assert onboarding.calls["snapshot"] == 1  # still only the FIRST read


def test_install_time_status_cannot_be_forged_from_the_payload(onboarding):
    """A browser boolean is a request, never an authority: neither the marker
    nor a claimed subscription can make a configured install install-time."""
    onboarding.settings_path.write_text(json.dumps({
        "OPENROUTER_API_KEY": "sk-or-v1-existing-install-key",
        "OUROBOROS_MODEL": "openai/gpt-5.6-luna",
    }), encoding="utf-8")

    response = onboarding.client.post("/api/onboarding/complete", json={
        **WIZARD_PAYLOAD,
        "subscriptionsConnected": True,
        PRESET_MARKER_KEY: "",              # try to clear the latch
        "OUROBOROS_SUBSCRIPTION_PRESET_VERSION ": "1",
        "OUROBOROS_REVIEWER_SLOTS": '{"triad": [], "scope": []}',
        "OUROBOROS_SAFETY_MODE": "off",
    })

    assert response.status_code == 200, response.text
    assert response.json()["preset"]["applied"] is False
    saved = onboarding.saved()
    assert not saved.get(PRESET_MARKER_KEY)
    # Neither the reviewer slots nor safety mode ride through the wizard payload:
    # the shared setup validator only copies the setup contract's own keys.
    assert not saved.get("OUROBOROS_REVIEWER_SLOTS")
    assert saved.get("OUROBOROS_SAFETY_MODE", "") != "off"


def test_subscription_alone_does_not_satisfy_the_launch_gate(onboarding):
    """D-1: at least one API key or a local model. A subscription amplifies.

    The shared setup validator is the first gate and refuses with its own
    provider-list wording; nothing is written and the daemon is never asked."""
    payload = {k: v for k, v in WIZARD_PAYLOAD.items() if k != "OPENROUTER_API_KEY"}

    response = onboarding.client.post(
        "/api/onboarding/complete", json={**payload, "subscriptionsConnected": True})

    assert response.status_code == 400, response.text
    assert "local model" in response.json()["error"]
    assert not onboarding.settings_path.exists()
    assert onboarding.calls["snapshot"] == 0


def test_startup_gate_is_re_checked_after_normalization(monkeypatch, onboarding):
    """Defence in depth on the SAME invariant: even if a payload passed the
    shared validator, an install that would not be startup-ready is refused
    before anything is saved — a subscription never fills that gap."""
    import ouroboros.gateway.onboarding as gw_onboarding

    monkeypatch.setattr(
        gw_onboarding, "apply_runtime_provider_defaults",
        lambda settings: ({k: ("" if k == "OPENROUTER_API_KEY" else v)
                           for k, v in settings.items()}, False, []))

    response = onboarding.client.post(
        "/api/onboarding/complete", json={**WIZARD_PAYLOAD, "subscriptionsConnected": True})

    assert response.status_code == 400, response.text
    assert "API key or a local model" in response.json()["error"]
    assert not onboarding.settings_path.exists()
    assert onboarding.calls["snapshot"] == 0


def test_non_object_body_is_refused(onboarding):
    response = onboarding.client.post("/api/onboarding/complete", json=["nope"])

    assert response.status_code == 400
    assert not onboarding.settings_path.exists()


def test_running_process_keeps_its_boot_runtime_mode(onboarding):
    """The pending next-boot mode is persisted; the live env is not elevated —
    the same split the two-write flow had."""
    from ouroboros.config import get_runtime_mode

    response = onboarding.client.post(
        "/api/onboarding/complete", json={**WIZARD_PAYLOAD, "OUROBOROS_RUNTIME_MODE": "pro"})

    assert response.status_code == 200, response.text
    assert response.json()["runtime_mode"] == "pro"
    assert onboarding.saved()["OUROBOROS_RUNTIME_MODE"] == "pro"
    assert onboarding.calls["env"][0]["OUROBOROS_RUNTIME_MODE"] == get_runtime_mode()


# ---------------------------------------------------------------------------
# Pure helpers (no server).
# ---------------------------------------------------------------------------


def test_only_daemon_vouched_accounts_become_discoveries():
    from ouroboros.gateway.onboarding import verified_harness_discoveries

    snapshot = {
        "daemon": {"state": "running"},
        "harnesses": [
            {"id": "claude", "models": [{"id": "claude-opus-5"}]},
            {"id": "codex", "models": [{"id": "gpt-5.6-sol"}]},
            {"id": "cursor", "models": [{"id": "cursor-grok-4.5-high"}]},
        ],
        "profiles": {
            # claude: a live local session. codex: a verified named profile.
            # cursor: a profile that exists but never verified -> NOT connected.
            "harnessAccounts": [{"harness_id": "claude", "native_login_detected": True},
                                {"harness_id": "cursor", "native_login_detected": False}],
            "profiles": [
                {"profile": {"harness_id": "codex", "enabled": True},
                 "status": {"verification": "passed"}},
                {"profile": {"harness_id": "cursor", "enabled": True},
                 "status": {"verification": "failed"}},
            ],
        },
    }

    discoveries, failure = verified_harness_discoveries(snapshot)

    assert failure is None
    assert [d.harness_id for d in discoveries] == ["claude", "codex"]


def test_no_vouched_account_is_a_typed_failure():
    from ouroboros.gateway.onboarding import verified_harness_discoveries

    discoveries, failure = verified_harness_discoveries({
        "daemon": {"state": "running"}, "harnesses": [], "profiles": {}})

    assert discoveries == ()
    assert failure is not None and failure.code == "no_verified_account"


def test_generic_settings_save_cannot_author_or_clear_the_marker():
    """The marker is owner-only: a Settings POST neither writes nor wipes it."""
    from ouroboros.gateway.settings import _merge_settings_payload

    merged = _merge_settings_payload({PRESET_MARKER_KEY: "1", "TOTAL_BUDGET": 10.0},
                                     {PRESET_MARKER_KEY: "99", "TOTAL_BUDGET": 20.0})

    assert merged[PRESET_MARKER_KEY] == "1"
    assert merged["TOTAL_BUDGET"] == 20.0

    fresh = _merge_settings_payload({"TOTAL_BUDGET": 10.0}, {PRESET_MARKER_KEY: "1"})
    assert not fresh.get(PRESET_MARKER_KEY)


def test_get_onboarding_read_writes_nothing(monkeypatch, tmp_path):
    """A READ must never be the first author of settings.json (D-8)."""
    import ouroboros.config as config
    import ouroboros.gateway.settings as gw_settings

    for key in _PROVIDER_ENV:
        monkeypatch.delenv(key, raising=False)
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SETTINGS_PATH", settings_path)

    app = Starlette(routes=[Route("/api/onboarding", endpoint=gw_settings.api_onboarding)])
    with TestClient(app) as client:
        first = client.get("/api/onboarding")

    assert first.status_code == 200  # the blocking overlay
    assert not settings_path.exists(), "GET /api/onboarding created settings.json"

    # And with a settings file present, its bytes and mtime are untouched.
    settings_path.write_text(json.dumps({"OUROBOROS_MODEL": "openai/gpt-5.6-luna"}),
                             encoding="utf-8")
    before_bytes = settings_path.read_bytes()
    before_mtime = settings_path.stat().st_mtime_ns
    with TestClient(app) as client:
        second = client.get("/api/onboarding")

    assert second.status_code == 200
    assert settings_path.read_bytes() == before_bytes
    assert settings_path.stat().st_mtime_ns == before_mtime
