"""Settings-page provider credential probe (POST /api/providers/test).

The endpoint exercises the ordinary catalog fetcher with entered-but-unsaved
values so a bad key is caught at paste time rather than on the first real task.
All literals here are placeholders that cannot be mistaken for real credentials.
"""

import asyncio
import json
import pathlib

import httpx
import ouroboros.gateway.models as model_catalog_api

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = REPO_ROOT / "web" / "modules"


class _Request:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def _post(payload):
    response = asyncio.run(model_catalog_api.api_provider_test(_Request(payload)))
    return response.status_code, json.loads(response.body.decode("utf-8"))


def test_provider_test_uses_unsaved_overrides(monkeypatch):
    monkeypatch.setattr(model_catalog_api, "load_settings", lambda: {})
    captured = {}

    async def fake_compatible(_client, provider_id, provider_label, api_key, base_url):
        captured.update({"api_key": api_key, "base_url": base_url})
        return [
            model_catalog_api._build_model_catalog_entry(provider_id, provider_label, "model-a", "model-a"),
            model_catalog_api._build_model_catalog_entry(provider_id, provider_label, "model-b", "model-b"),
        ]

    monkeypatch.setattr(model_catalog_api, "_fetch_openai_compatible_model_catalog", fake_compatible)

    status, payload = _post({
        "provider_id": "openai-compatible",
        "overrides": {
            "OPENAI_COMPATIBLE_API_KEY": "unit-test-credential",
            "OPENAI_COMPATIBLE_BASE_URL": "https://unit-test-base-url.example/v1",
        },
    })

    assert status == 200
    assert payload["ok"] is True
    assert payload["model_count"] == 2
    assert isinstance(payload["duration_ms"], int)
    assert captured == {
        "api_key": "unit-test-credential",
        "base_url": "https://unit-test-base-url.example/v1",
    }


def test_provider_test_rejects_unknown_override_without_echoing_value(monkeypatch):
    monkeypatch.setattr(model_catalog_api, "load_settings", lambda: {})

    status, payload = _post({
        "provider_id": "openai-compatible",
        "overrides": {"OUROBOROS_NOT_A_PROVIDER_KEY": "unit-test-credential"},
    })

    assert status == 400
    assert "OUROBOROS_NOT_A_PROVIDER_KEY" in payload["error"]
    # The rejection names key names only — never the submitted value.
    assert "unit-test-credential" not in json.dumps(payload)


def test_provider_test_requires_a_configured_provider(monkeypatch):
    monkeypatch.setattr(model_catalog_api, "load_settings", lambda: {})

    status, payload = _post({"provider_id": "openrouter"})

    assert status == 400
    assert payload["error"] == "provider is not configured (missing key or base URL)"


def test_provider_test_reports_fetch_failure_as_ok_false(monkeypatch):
    monkeypatch.setattr(model_catalog_api, "load_settings", lambda: {})

    async def fake_compatible(_client, _provider_id, _provider_label, _api_key, _base_url):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(model_catalog_api, "_fetch_openai_compatible_model_catalog", fake_compatible)

    status, payload = _post({
        "provider_id": "openai-compatible",
        "overrides": {
            # The credential rides along: an endpoint override alone is refused
            # by the exfiltration guard.
            "OPENAI_COMPATIBLE_BASE_URL": "https://unit-test-base-url.example/v1",
            "OPENAI_COMPATIBLE_API_KEY": "unit-test-credential",
        },
    })

    # The endpoint worked; the provider did not. That is a 200 with ok:false.
    assert status == 200
    assert payload["ok"] is False
    assert payload["stage"] == "connect"
    assert payload["error"] == "boom"


def test_provider_test_route_is_registered():
    source = (REPO_ROOT / "ouroboros" / "gateway" / "router.py").read_text(encoding="utf-8")
    assert "api_provider_test," in source
    assert 'Route("/api/providers/test", endpoint=api_provider_test, methods=["POST"])' in source


def test_provider_test_ui_surface_is_wired():
    settings_ui = (WEB / "settings_ui.js").read_text(encoding="utf-8")
    settings = (WEB / "settings.js").read_text(encoding="utf-8")
    api_client = (WEB / "api_client.js").read_text(encoding="utf-8")

    assert 'data-provider-test="${spec.testProvider}">Test</button>' in settings_ui
    assert 'data-provider-test-status="${spec.testProvider}"' in settings_ui
    assert "export const PROVIDER_TEST_INPUTS" in settings_ui
    assert "providerTest: (payload) => jsonPost('/api/providers/test', payload)," in api_client
    assert "closest('[data-provider-test]')" in settings
    assert "apiClient.providerTest({ provider_id: provider, overrides })" in settings
    # Mask-echo guard: saved secrets render masked; only owner-edited values may
    # become overrides, or every already-saved key would fail its own test.
    assert "el.dataset.appliedValue = el.value;" in settings
    assert "value !== (input?.dataset.appliedValue ?? '').trim()" in settings


def test_provider_test_unknown_provider_id_is_distinct(monkeypatch):
    monkeypatch.setattr(model_catalog_api, "load_settings", lambda: {})
    status, body = _post({"provider_id": "openrouterr"})
    assert status == 400
    assert "unknown provider id" in body.get("error", "")


def test_provider_test_rejects_non_object_bodies():
    class _BrokenRequest:
        async def json(self):
            raise ValueError("not json")

    response = asyncio.run(model_catalog_api.api_provider_test(_BrokenRequest()))
    assert response.status_code == 400

    status, body = _post(["provider_id", "openrouter"])
    assert status == 400
    assert "JSON object" in body.get("error", "")


def test_provider_test_empty_override_unsets_the_saved_value(monkeypatch):
    # The owner cleared the field: the probe must test the visible draft, not
    # the saved key — with the only credential unset, the provider is reported
    # not configured instead of a misleading OK against the old value.
    monkeypatch.setattr(
        model_catalog_api,
        "load_settings",
        lambda: {"OPENROUTER_API_KEY": "unit-test-credential"},
    )
    status, body = _post({
        "provider_id": "openrouter",
        "overrides": {"OPENROUTER_API_KEY": ""},
    })
    assert status == 400
    assert "not configured" in body.get("error", "")


def test_provider_test_rejects_an_unknown_minimax_region(monkeypatch):
    # resolve_minimax_base_url silently maps unknown regions to the default
    # endpoint; a typo'd region must not come back as an OK for a deployment
    # the owner never selected.
    monkeypatch.setattr(
        model_catalog_api,
        "load_settings",
        lambda: {"MINIMAX_API_KEY": "unit-test-credential"},
    )
    status, body = _post({
        "provider_id": "minimax",
        "overrides": {
            # The credential rides along: a region override alone is refused by
            # the exfiltration guard (a region change IS an endpoint change).
            "MINIMAX_REGION": "definitely-not-a-region",
            "MINIMAX_API_KEY": "unit-test-credential",
        },
    })
    assert status == 400
    assert "region" in body.get("error", "")


def test_provider_test_redacts_credentials_from_error_answers(monkeypatch):
    # Provider exceptions can embed the base URL with inlined credentials; the
    # endpoint promises credential values never reach the response or the log.
    monkeypatch.setattr(
        model_catalog_api,
        "load_settings",
        lambda: {
            "OPENAI_COMPATIBLE_API_KEY": "unit-test-credential",
            "OPENAI_COMPATIBLE_BASE_URL": "https://unit-test-base-url.example/v1",
        },
    )

    async def exploding(client, provider_id, provider_label, api_key, base_url):
        raise httpx.ConnectError(f"connect to {base_url}?key=unit-test-credential failed")

    monkeypatch.setattr(
        model_catalog_api, "_fetch_openai_compatible_model_catalog", exploding
    )
    status, body = _post({"provider_id": "openai-compatible"})
    assert status == 200
    assert body["ok"] is False and body["stage"] == "connect"
    assert "unit-test-credential" not in json.dumps(body)


def test_provider_test_never_pairs_a_saved_key_with_an_unsaved_endpoint(monkeypatch):
    # Accepting a caller-supplied base URL alone would send the STORED key to a
    # server the owner never configured — an exfiltration primitive, not a probe.
    monkeypatch.setattr(
        model_catalog_api,
        "load_settings",
        lambda: {
            "OPENAI_COMPATIBLE_API_KEY": "unit-test-credential",
            "OPENAI_COMPATIBLE_BASE_URL": "https://unit-test-base-url.example/v1",
        },
    )
    status, body = _post({
        "provider_id": "openai-compatible",
        "overrides": {"OPENAI_COMPATIBLE_BASE_URL": "https://unit-test-attacker.example/v1"},
    })
    assert status == 400
    assert "re-entering the credential" in body.get("error", "")

    # Same endpoint value as saved: nothing new is being paired — allowed.
    async def fake_fetch(client, provider_id, provider_label, api_key, base_url):
        return [{"value": "openai-compatible::unit-test-model"}]

    monkeypatch.setattr(
        model_catalog_api, "_fetch_openai_compatible_model_catalog", fake_fetch
    )
    status, body = _post({
        "provider_id": "openai-compatible",
        "overrides": {"OPENAI_COMPATIBLE_BASE_URL": "https://unit-test-base-url.example/v1"},
    })
    assert status == 200 and body["ok"] is True

    # New endpoint WITH a re-entered credential: the owner typed both — allowed.
    status, body = _post({
        "provider_id": "openai-compatible",
        "overrides": {
            "OPENAI_COMPATIBLE_BASE_URL": "https://unit-test-other.example/v1",
            "OPENAI_COMPATIBLE_API_KEY": "unit-test-other-credential",
        },
    })
    assert status == 200 and body["ok"] is True


def test_provider_test_redacts_percent_encoded_credential_forms(monkeypatch):
    monkeypatch.setattr(
        model_catalog_api,
        "load_settings",
        lambda: {
            "OPENAI_COMPATIBLE_API_KEY": "unit test credential@x",
            "OPENAI_COMPATIBLE_BASE_URL": "https://unit-test-base-url.example/v1",
        },
    )

    async def exploding(client, provider_id, provider_label, api_key, base_url):
        import urllib.parse

        raise httpx.ConnectError(
            "connect to https://"
            + urllib.parse.quote("unit test credential@x", safe="")
            + "@host failed"
        )

    monkeypatch.setattr(
        model_catalog_api, "_fetch_openai_compatible_model_catalog", exploding
    )
    status, body = _post({"provider_id": "openai-compatible"})
    assert status == 200 and body["ok"] is False
    joined = json.dumps(body)
    assert "unit%20test%20credential%40x" not in joined
    assert "unit test credential@x" not in joined


def test_provider_test_constants_stay_in_sync_with_provider_specs():
    # Both constants are hand-maintained mirrors of _provider_specs; this pin
    # fails the moment a provider is added there without updating them.
    import inspect

    settings = {
        "OPENROUTER_API_KEY": "unit-test-credential",
        "OPENAI_API_KEY": "unit-test-credential",
        "ANTHROPIC_API_KEY": "unit-test-credential",
        "MINIMAX_API_KEY": "unit-test-credential",
        "OPENAI_COMPATIBLE_API_KEY": "unit-test-credential",
        "OPENAI_COMPATIBLE_BASE_URL": "https://unit-test-base-url.example/v1",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY": "unit-test-credential",
        "GIGACHAT_CREDENTIALS": "unit-test-credential",
    }
    emitted = {pid for pid, _loader in model_catalog_api._provider_specs(settings)}
    assert emitted == set(model_catalog_api._PROVIDER_TEST_KNOWN_IDS)

    source = inspect.getsource(model_catalog_api._provider_specs)
    for key in model_catalog_api._PROVIDER_TEST_OVERRIDE_KEYS:
        assert f'"{key}"' in source, f"allowlisted key {key} is not read by _provider_specs"
    assert model_catalog_api._PROVIDER_TEST_SECRET_KEYS <= model_catalog_api._PROVIDER_TEST_OVERRIDE_KEYS
    assert set(model_catalog_api._PROVIDER_TEST_ENDPOINT_GUARDS) <= model_catalog_api._PROVIDER_TEST_OVERRIDE_KEYS


def test_endpoint_guard_requires_every_configured_credential_re_entered(monkeypatch):
    # Satisfying the guard with one re-entered secret while another saved one
    # rides along (password submitted, OAuth credentials kept) is the same
    # exfiltration through a side door.
    monkeypatch.setattr(
        model_catalog_api,
        "load_settings",
        lambda: {
            "GIGACHAT_CREDENTIALS": "unit-test-credential",
            "GIGACHAT_PASSWORD": "unit-test-password",
            "GIGACHAT_USER": "unit-test-user",
        },
    )
    status, body = _post({
        "provider_id": "gigachat",
        "overrides": {
            "GIGACHAT_BASE_URL": "https://unit-test-attacker.example/v1",
            "GIGACHAT_PASSWORD": "unit-test-password",
        },
    })
    assert status == 400
    assert "re-entering the credential" in body.get("error", "")

    async def fake_gigachat(_credentials, _scope, _base_url, _verify, _user="", _password=""):
        return [{"value": "gigachat::unit-test-model"}]

    monkeypatch.setattr(model_catalog_api, "_fetch_gigachat_model_catalog", fake_gigachat)
    status, body = _post({
        "provider_id": "gigachat",
        "overrides": {
            "GIGACHAT_BASE_URL": "https://unit-test-other.example/v1",
            "GIGACHAT_CREDENTIALS": "unit-test-other-credential",
            "GIGACHAT_PASSWORD": "unit-test-other-password",
        },
    })
    assert status == 200 and body["ok"] is True


def test_tls_verification_toggle_is_endpoint_guarded(monkeypatch):
    # Turning off certificate verification changes the connection's trust
    # boundary exactly like a URL change: saved credentials must not ride.
    monkeypatch.setattr(
        model_catalog_api,
        "load_settings",
        lambda: {"GIGACHAT_CREDENTIALS": "unit-test-credential"},
    )
    status, body = _post({
        "provider_id": "gigachat",
        "overrides": {"GIGACHAT_VERIFY_SSL_CERTS": "false"},
    })
    assert status == 400
    assert "re-entering the credential" in body.get("error", "")


def test_short_credentials_are_redacted_as_standalone_tokens(monkeypatch):
    monkeypatch.setattr(
        model_catalog_api,
        "load_settings",
        lambda: {
            "OPENAI_COMPATIBLE_API_KEY": "abc",
            "OPENAI_COMPATIBLE_BASE_URL": "https://unit-test-base-url.example/v1",
        },
    )

    async def exploding(client, provider_id, provider_label, api_key, base_url):
        raise httpx.ConnectError("bad token abc rejected; abcdef is a different word")

    monkeypatch.setattr(
        model_catalog_api, "_fetch_openai_compatible_model_catalog", exploding
    )
    status, body = _post({"provider_id": "openai-compatible"})
    assert status == 200 and body["ok"] is False
    assert " abc " not in f' {body["error"]} '.replace("***", " *** ")
    # An innocent longer word sharing the prefix stays legible.
    assert "abcdef" in body["error"]


def test_an_explicit_empty_endpoint_override_is_still_an_endpoint_change(monkeypatch):
    # Unsetting the saved endpoint reroutes the probe to a default or legacy
    # destination — a destination change like any other while a saved key rides.
    monkeypatch.setattr(
        model_catalog_api,
        "load_settings",
        lambda: {
            "CLOUDRU_FOUNDATION_MODELS_API_KEY": "unit-test-credential",
            "CLOUDRU_FOUNDATION_MODELS_BASE_URL": "https://unit-test-base-url.example/v1",
        },
    )
    status, body = _post({
        "provider_id": "cloudru",
        "overrides": {"CLOUDRU_FOUNDATION_MODELS_BASE_URL": ""},
    })
    assert status == 400
    assert "re-entering the credential" in body.get("error", "")


def test_verify_ssl_guard_ignores_semantic_noops(monkeypatch):
    # Unset means verify (the loader default), so an explicit 'true' confirms
    # the existing trust boundary; only an actual flip demands re-entry.
    monkeypatch.setattr(
        model_catalog_api,
        "load_settings",
        lambda: {"GIGACHAT_CREDENTIALS": "unit-test-credential"},
    )

    async def fake_gigachat(_credentials, _scope, _base_url, _verify, _user="", _password=""):
        return [{"value": "gigachat::unit-test-model"}]

    monkeypatch.setattr(model_catalog_api, "_fetch_gigachat_model_catalog", fake_gigachat)
    status, body = _post({
        "provider_id": "gigachat",
        "overrides": {"GIGACHAT_VERIFY_SSL_CERTS": "true"},
    })
    assert status == 200 and body["ok"] is True


def test_provider_card_clear_buttons_dispatch_input_for_verdict_expiry():
    # BOTH clear handlers (provider cards' .secret-clear and custom secret
    # rows') must fire 'input': verdict expiry listens for it exclusively.
    settings_ui = (WEB / "settings_ui.js").read_text(encoding="utf-8")
    settings = (WEB / "settings.js").read_text(encoding="utf-8")
    assert settings_ui.count("dispatchEvent(new Event('input', { bubbles: true }))") == 1
    assert settings.count("dispatchEvent(new Event('input', { bubbles: true }))") == 1
