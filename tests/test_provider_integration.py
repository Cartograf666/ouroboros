"""
Provider integration tests — real API calls to verify each LLM provider works.

These tests are marked with @pytest.mark.integration and excluded from the
default pytest run via pyproject.toml addopts. They run only on:
  - main / ouroboros / ouroboros-stable push (CI Tier 2.5)
  - workflow_dispatch (manual)
  - tag push (v*)

Optional provider smokes skip when their API key is absent. The shipped-default
OpenAI reasoning/tool canary requires ``OPENAI_API_KEY`` in the official GitHub
job, so deleting that secret cannot turn the contract alarm into a green no-op.
"""

from __future__ import annotations

import os
import uuid

import pytest

from ouroboros.provider_models import OPENAI_DIRECT_DEFAULTS
from tests.provider_contract_ci import (
    registry_openai_canary_tool,
    require_openai_canary_key,
    run_openai_reasoning_tool_canary,
    skip_on_provider_environmental_error,
    unique_openai_direct_defaults,
)

integration = pytest.mark.integration
_OPENAI_REASONING_TOOL_MODELS = unique_openai_direct_defaults()


def _get_llm_client():
    """Lazy import to avoid breaking collection when ouroboros is not installed."""
    from ouroboros.llm import LLMClient

    return LLMClient()


def _assert_basic_response(result, expected_provider=None):
    """Shared assertion: non-empty reply, token usage present."""
    if isinstance(result, tuple):
        msg, usage = result
    else:
        msg = result
        usage = result.get("usage", {}) if isinstance(result, dict) else {}

    text = ""
    if isinstance(msg, dict):
        text = msg.get("content", "") or ""
        if isinstance(text, list):
            text = " ".join(block.get("text", "") for block in text if isinstance(block, dict))
        if not text and expected_provider == "cloudru" and msg.get("reasoning"):
            pytest.skip(
                "Cloud.ru returned reasoning-only output without final content; "
                "provider route/auth/usage worked, but the hosted model did not "
                "emit a final answer for this smoke prompt."
            )
    assert text, f"Empty response from LLM: {result}"
    assert isinstance(usage, dict), f"Usage is not a dict: {type(usage)}"
    assert usage.get("prompt_tokens", 0) > 0, f"No prompt_tokens in usage: {usage}"
    assert usage.get("completion_tokens", 0) > 0, f"No completion_tokens in usage: {usage}"
    if expected_provider:
        resolved = usage.get("provider", "") or usage.get("resolved_model", "") or ""
        assert expected_provider.lower() in resolved.lower(), (
            f"Expected provider '{expected_provider}' in resolved model, got '{resolved}'"
        )


# The former direct-OpenAI gpt-4o-mini text row is intentionally absent. The
# registry/default-derived canary below asserts the same direct route plus exact
# shipped model identity, positive usage, custom+medium wire disclosure, and a
# real continuation. Keeping the stale text row would add cost without coverage.
_PROVIDER_MATRIX = [
    (
        "openrouter",
        "OPENROUTER_API_KEY",
        "anthropic/claude-sonnet-4.6",
        "openrouter",
    ),
    (
        "anthropic_direct",
        "ANTHROPIC_API_KEY",
        "anthropic::claude-sonnet-5",
        "anthropic",
    ),
    (
        "cloudru",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "cloudru::zai-org/GLM-4.7",
        "cloudru",
    ),
]


@integration
@pytest.mark.parametrize(
    "provider_id,env_key,model,expected_provider",
    _PROVIDER_MATRIX,
    ids=[entry[0] for entry in _PROVIDER_MATRIX],
)
def test_provider_basic_chat(provider_id, env_key, model, expected_provider):
    """Verify each optional provider responds to a bounded text request."""
    if not os.environ.get(env_key):
        pytest.skip(f"{env_key} not set")
    try:
        result = _get_llm_client().chat(
            messages=[{"role": "user", "content": "Respond with exactly: OK"}],
            model=model,
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001
        skip_on_provider_environmental_error(provider_id, exc)
        raise
    _assert_basic_response(result, expected_provider=expected_provider)


@integration
@pytest.mark.parametrize(
    "model",
    _OPENAI_REASONING_TOOL_MODELS,
    ids=[model.split("::", 1)[-1] for model in _OPENAI_REASONING_TOOL_MODELS],
)
def test_openai_shipped_reasoning_models_custom_tool_continuation(model, tmp_path):
    """Alarm on the exact shipped direct-OpenAI reasoning/tool contract.

    Every unique model from the provider-default SSOT must preserve requested
    ``medium`` on the production custom-tool path. The Main row additionally
    replays the returned canonical assistant call, submits an ordinary
    ``role=tool`` result carrying a nonce, and requires the exact final marker
    that was absent from the initial prompt.
    Continuation is one provider/API/dialect protocol class, so repeating that
    second turn for every model would add cost and flake surface without a new
    contract; each model still gets its own first-turn wire acceptance alarm.
    Quota/429/5xx/timeout are typed inconclusive; every contract/auth/model/tool/
    reasoning 4xx remains red.
    """
    require_openai_canary_key()
    try:
        run_openai_reasoning_tool_canary(
            _get_llm_client(),
            model=model,
            tool=registry_openai_canary_tool(tmp_path),
            nonce=uuid.uuid4().hex,
            continue_to_final=model == OPENAI_DIRECT_DEFAULTS["main"],
        )
    except Exception as exc:  # noqa: BLE001
        skip_on_provider_environmental_error("openai_direct", exc)
        raise


@integration
@pytest.mark.skipif(
    not os.environ.get("GIGACHAT_CREDENTIALS"),
    reason="GIGACHAT_CREDENTIALS not set",
)
def test_gigachat_basic_chat():
    """Verify GigaChat direct routing via the gigachat library works."""
    result = _get_llm_client().chat(
        messages=[{"role": "user", "content": "Respond with exactly: OK"}],
        model="gigachat::GigaChat-3-Ultra",
    )
    _assert_basic_response(result, expected_provider="gigachat")


_COMPETING_KEYS = [
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_COMPATIBLE_API_KEY",
    "OPENAI_COMPATIBLE_BASE_URL",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY",
    "GIGACHAT_CREDENTIALS",
    "ANTHROPIC_API_KEY",
]

# Direct OpenAI needs no duplicate gpt-4o-mini isolation row: its explicit
# ``openai::`` route and exact usage identity are asserted by the stronger canary.
_ISOLATION_MATRIX = [
    (
        "openrouter",
        "OPENROUTER_API_KEY",
        "anthropic/claude-sonnet-4.6",
    ),
    (
        "anthropic_direct",
        "ANTHROPIC_API_KEY",
        "anthropic::claude-sonnet-5",
    ),
    (
        "cloudru",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "cloudru::zai-org/GLM-4.7",
    ),
]


@integration
@pytest.mark.parametrize(
    "provider_id,env_key,model",
    _ISOLATION_MATRIX,
    ids=[entry[0] for entry in _ISOLATION_MATRIX],
)
def test_provider_isolation(provider_id, env_key, model, monkeypatch):
    """Each optional provider works when it is the only configured provider."""
    if not os.environ.get(env_key):
        pytest.skip(f"{env_key} not set")
    for key in _COMPETING_KEYS:
        if key != env_key:
            monkeypatch.delenv(key, raising=False)
    try:
        result = _get_llm_client().chat(
            messages=[{"role": "user", "content": "Say hello"}],
            model=model,
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001
        skip_on_provider_environmental_error(provider_id, exc)
        raise
    _assert_basic_response(result)


@integration
@pytest.mark.skipif(
    not os.environ.get("GIGACHAT_CREDENTIALS"),
    reason="GIGACHAT_CREDENTIALS not set",
)
def test_gigachat_isolation(monkeypatch):
    """GigaChat works when it is the only configured provider."""
    for key in _COMPETING_KEYS:
        if key != "GIGACHAT_CREDENTIALS":
            monkeypatch.delenv(key, raising=False)
    result = _get_llm_client().chat(
        messages=[{"role": "user", "content": "Say hello"}],
        model="gigachat::GigaChat-3-Ultra",
    )
    _assert_basic_response(result)
