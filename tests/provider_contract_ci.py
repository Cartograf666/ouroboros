"""Shared, secretless contracts for the trusted provider integration lane."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from enum import Enum

import pytest

from ouroboros.provider_models import OPENAI_DIRECT_DEFAULTS, normalize_model_identity
from ouroboros.utils import sanitize_tool_result_for_log

CANARY_TIMEOUT_SEC = 120.0
CANARY_MAX_TOKENS = 1024
CANARY_CONTINUATION_MAX_TOKENS = 2048
CANARY_TOOL_NAME = "delegate_start"
CANARY_SUBAGENT_ID = "provider-contract-canary"


@dataclass(frozen=True)
class ProviderCanary:
    """One physical provider/API surface in the trusted full-registry matrix."""

    canary_id: str
    model: str
    expected_provider: str
    credential_env: str
    credential_required: bool
    reasoning_effort: str
    named_tool_choice: bool = True
    continue_to_final: bool = False


_OPENROUTER_CANARIES = (
    ProviderCanary(
        "openrouter_gemini", "google/gemini-3.7-flash", "openrouter",
        "OPENROUTER_API_KEY", True, "medium",
    ),
    ProviderCanary(
        "openrouter_opus", "anthropic/claude-opus-5", "openrouter",
        "OPENROUTER_API_KEY", True, "medium",
    ),
    ProviderCanary(
        "openrouter_gpt", "openai/gpt-5.6-luna", "openrouter",
        "OPENROUTER_API_KEY", True, "medium",
    ),
    ProviderCanary(
        "openrouter_grok", "x-ai/grok-4.6", "openrouter",
        "OPENROUTER_API_KEY", True, "medium",
    ),
    ProviderCanary(
        "openrouter_deepseek", "deepseek/deepseek-v4-pro-0813", "openrouter",
        "OPENROUTER_API_KEY", True, "medium",
    ),
)

_DIRECT_ANTHROPIC_CANARY = ProviderCanary(
    "anthropic_direct", "anthropic::claude-sonnet-5", "anthropic",
    "ANTHROPIC_API_KEY", True, "medium",
)

_OPTIONAL_DIRECT_CANARIES = (
    ProviderCanary(
        "minimax_direct", "minimax::MiniMax-M3", "minimax",
        "MINIMAX_API_KEY", False, "none",
    ),
    ProviderCanary(
        "cloudru_direct", "cloudru::zai-org/GLM-4.7", "cloudru",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY", False, "none",
    ),
    ProviderCanary(
        "gigachat_direct", "gigachat::GigaChat-2-Max", "gigachat",
        "GIGACHAT_CREDENTIALS", False, "none", named_tool_choice=False,
    ),
)


def unique_openai_direct_defaults():
    """Derive the live direct-OpenAI matrix from the shipped provider SSOT."""
    return tuple(dict.fromkeys(model for model in OPENAI_DIRECT_DEFAULTS.values() if model))


def _openai_direct_canaries():
    seen = set()
    rows = []
    main_model = OPENAI_DIRECT_DEFAULTS.get("main")
    for role, model in OPENAI_DIRECT_DEFAULTS.items():
        if not model or model in seen:
            continue
        seen.add(model)
        rows.append(ProviderCanary(
            canary_id=f"openai_direct_{role}",
            model=model,
            expected_provider="openai",
            credential_env="OPENAI_API_KEY",
            credential_required=True,
            reasoning_effort="medium",
            continue_to_final=model == main_model,
        ))
    return tuple(rows)


def provider_canary_matrix():
    """Return the exact paid matrix in stable physical-call order."""
    return (
        *_OPENROUTER_CANARIES,
        *_openai_direct_canaries(),
        _DIRECT_ANTHROPIC_CANARY,
        *_OPTIONAL_DIRECT_CANARIES,
    )


class ProviderFailureKind(str, Enum):
    RED = "red"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class ProviderFailureClassification:
    kind: ProviderFailureKind
    reason: str
    status_code: int | None = None


def _exception_chain(exc: BaseException):
    chain = []
    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return tuple(chain)


def provider_error_evidence(exc: BaseException):
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None)
    if type(status) is not int and response is not None:
        status = getattr(response, "status_code", None)
    status = status if type(status) is int else None
    body = ""
    if response is not None:
        try:
            body = str(response.text or "")
        except Exception:
            body = ""
    structured_body = getattr(exc, "body", None)
    if structured_body:
        try:
            body = "\n".join(filter(None, (body, json.dumps(structured_body))))
        except (TypeError, ValueError):
            body = "\n".join(filter(None, (body, str(structured_body))))
    chain = _exception_chain(exc)
    message = "\n".join(str(item) for item in chain)
    return status, body, message, chain


def classify_provider_failure(
    provider_id: str,
    exc: BaseException,
) -> ProviderFailureClassification:
    """Classify only explicit non-code alarms as typed inconclusive.

    Contract/auth/model/tool/reasoning 4xx stay RED. In particular, the #229
    function-tools + reasoning 400 must never be hidden by a broad text match.
    """
    del provider_id
    status, body, message, chain = provider_error_evidence(exc)
    lowered = "\n".join((body, message)).lower()
    if any(
        marker in lowered
        for marker in (
            "insufficient_quota",
            "credit balance is too low",
            "billing_hard_limit_reached",
            "billing_not_active",
            "payment_required",
            "exceeded your current quota",
            "requires more credits",
            "can only afford",
        )
    ):
        return ProviderFailureClassification(
            ProviderFailureKind.INCONCLUSIVE, "quota_or_billing", status,
        )
    if status == 429:
        return ProviderFailureClassification(
            ProviderFailureKind.INCONCLUSIVE, "rate_limit_429", status,
        )
    if status is not None and 500 <= status < 600:
        return ProviderFailureClassification(
            ProviderFailureKind.INCONCLUSIVE, "provider_5xx", status,
        )
    if status is None and any(
        isinstance(item, TimeoutError) or "timeout" in type(item).__name__.lower()
        for item in chain
    ):
        return ProviderFailureClassification(
            ProviderFailureKind.INCONCLUSIVE, "transport_timeout",
        )
    return ProviderFailureClassification(
        ProviderFailureKind.RED, "provider_contract_or_unclassified", status,
    )


def skip_on_provider_environmental_error(
    provider_id: str,
    exc: BaseException,
) -> None:
    """Skip only classifier-approved typed inconclusive provider outcomes."""
    import sys

    classification = classify_provider_failure(provider_id, exc)
    status, body, message, _chain = provider_error_evidence(exc)
    safe_body = sanitize_tool_result_for_log(body)
    safe_message = sanitize_tool_result_for_log(message)
    if safe_body:
        print(f"[{provider_id}] HTTP {status} body: {safe_body[:500]}", file=sys.stderr)
    if classification.kind is ProviderFailureKind.INCONCLUSIVE:
        detail = safe_body[:200] if safe_body else safe_message[:200]
        pytest.skip(
            f"[{provider_id}] inconclusive provider alarm "
            f"({classification.reason}): {detail}"
        )


def official_provider_integration_job():
    return (
        os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"
        and os.environ.get("GITHUB_REPOSITORY", "").strip() == "razzant/ouroboros"
    )


def require_provider_canary_credential(canary: ProviderCanary):
    if str(os.environ.get(canary.credential_env, "") or "").strip():
        return
    if canary.credential_required and official_provider_integration_job():
        pytest.fail(
            f"{canary.credential_env} is required by the official integration job "
            f"for {canary.canary_id} ({canary.model})"
        )
    policy = "required core" if canary.credential_required else "optional"
    pytest.skip(
        f"{canary.credential_env} not set; {policy} provider canary "
        f"{canary.canary_id} ({canary.model}) was not run"
    )


def full_registry_canary_tools():
    """Return the one built-in-only catalog shared with static portability tests."""
    from tests.provider_contract_catalog import shipped_builtin_tool_schemas

    tools = shipped_builtin_tool_schemas()
    names = [str((tool.get("function") or {}).get("name") or "") for tool in tools]
    assert names == sorted(names) and len(names) == len(set(names)), names
    assert CANARY_TOOL_NAME in names, names
    return copy.deepcopy(tools)


def delegate_start_canary_arguments(nonce: str):
    return {
        "prompt": (
            f"Provider contract canary {nonce}. Do not perform external actions; "
            "the returned call will be validated but never executed."
        ),
        "subagent_id": CANARY_SUBAGENT_ID,
    }


def _delegate_start_tool(tools):
    return next(
        tool
        for tool in tools
        if str((tool.get("function") or {}).get("name") or "") == CANARY_TOOL_NAME
    )


def _named_tool_choice():
    return {"type": "function", "function": {"name": CANARY_TOOL_NAME}}


def assert_openai_canary_usage(usage, model):
    assert isinstance(usage, dict), usage
    assert usage.get("provider") == "openai", usage
    assert usage.get("resolved_model") == normalize_model_identity(model), usage
    assert int(usage.get("prompt_tokens") or 0) > 0, usage
    assert int(usage.get("completion_tokens") or 0) > 0, usage
    assert usage.get("reasoning_effort_clamped") is None, usage

    disclosure = usage.get("request_wire")
    assert isinstance(disclosure, dict), (
        "Public direct-OpenAI request-wire disclosure is missing from usage; "
        f"usage={usage}"
    )
    assert disclosure["requested_effort"] == "medium"
    assert disclosure["applied_effort"] == "medium"
    assert disclosure["requested_tool_dialect"] == "function"
    assert disclosure["applied_tool_dialect"] == "openai_chat_custom"
    assert disclosure["reason_code"] == "requested_wire_form"
    assert disclosure["ladder_ordinal"] == 1
    assert disclosure["task_local"] is False
    actions = disclosure.get("applied_actions")
    assert isinstance(actions, list)
    for applied in actions:
        assert isinstance(applied, dict) and applied.get("source") != "task_local"
        action = applied.get("action")
        assert isinstance(action, dict)
        assert action.get("kind") != "replace_dialect"
        assert not (action.get("kind") == "set_value" and action.get("field") == "effort")
        assert not set(action.get("fields") or ()).intersection({
            "reasoning_effort", "thinking", "output_config", "extra_body.reasoning",
        })
    assert disclosure["attempt_id"]
    for key in (
        "source_profile_fingerprint",
        "accepted_profile_fingerprint",
        "candidate_sha256",
    ):
        value = str(disclosure.get(key) or "")
        assert len(value) == 64 and not (
            set(value.lower()) - set("0123456789abcdef")
        )
    return disclosure


def assert_canary_usage(usage, canary: ProviderCanary):
    assert isinstance(usage, dict), usage
    assert usage.get("provider") == canary.expected_provider, usage
    assert usage.get("resolved_model") == normalize_model_identity(canary.model), usage
    assert int(usage.get("prompt_tokens") or 0) > 0, usage
    assert int(usage.get("completion_tokens") or 0) > 0, usage
    if canary.reasoning_effort == "medium":
        assert usage.get("reasoning_effort_clamped") is None, usage
        disclosure = usage.get("request_wire")
        assert isinstance(disclosure, dict), usage
        assert disclosure.get("requested_effort") == "medium", disclosure
        assert disclosure.get("applied_effort") == "medium", disclosure
    if canary.expected_provider == "openai":
        return assert_openai_canary_usage(usage, canary.model)
    return usage.get("request_wire")


def assert_normalized_canary_call(message, tools, expected_arguments):
    from jsonschema import validators

    calls = message.get("tool_calls") if isinstance(message, dict) else None
    assert isinstance(calls, list) and calls, message
    schema = _delegate_start_tool(tools)["function"]["parameters"]
    validator_class = validators.validator_for(schema)
    validator_class.check_schema(schema)
    seen_ids = set()
    for call in calls:
        assert isinstance(call, dict), call
        assert call.get("type") == "function", call
        call_id = call.get("id")
        assert isinstance(call_id, str) and call_id and call_id == call_id.strip(), call
        assert call_id not in seen_ids, calls
        seen_ids.add(call_id)
        function = call.get("function")
        assert isinstance(function, dict), call
        assert function.get("name") == CANARY_TOOL_NAME, call
        raw_arguments = function.get("arguments")
        assert isinstance(raw_arguments, str), call
        arguments = json.loads(raw_arguments)
        assert not list(validator_class(schema).iter_errors(arguments))
        assert set(arguments) <= set((schema.get("properties") or {}).keys()), arguments
        for key, expected in expected_arguments.items():
            assert arguments.get(key) == expected, arguments
    return calls


def run_provider_contract_canary(
    client,
    *,
    canary: ProviderCanary,
    tools,
    nonce: str,
):
    """Exercise the public chat seam without executing the returned tool call."""
    expected_arguments = delegate_start_canary_arguments(nonce)
    arguments_json = json.dumps(expected_arguments, ensure_ascii=False, sort_keys=True)
    conversation = [{
        "role": "user",
        "content": (
            f"Call {CANARY_TOOL_NAME} exactly once with exactly this JSON object "
            f"as its arguments: {arguments_json}. Do not add, omit, or change a field. "
            "Return the tool call now and no prose."
        ),
    }]
    tool_choice = _named_tool_choice() if canary.named_tool_choice else "auto"
    message, usage = client.chat(
        messages=conversation,
        model=canary.model,
        tools=copy.deepcopy(tools),
        tool_choice=tool_choice,
        reasoning_effort=canary.reasoning_effort,
        max_tokens=CANARY_MAX_TOKENS,
        no_proxy=True,
        timeout=CANARY_TIMEOUT_SEC,
    )
    calls = assert_normalized_canary_call(message, tools, expected_arguments)
    first_disclosure = assert_canary_usage(usage, canary)
    if not canary.continue_to_final:
        return message, usage, None, None

    final_marker = f"FULL_REGISTRY_CONTINUED_{nonce}"
    continuation = [
        *conversation,
        message,
        *[
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps({
                    "ok": True,
                    "nonce": nonce,
                    "expected_final_marker": final_marker,
                }, sort_keys=True),
            }
            for call in calls
        ],
    ]
    final_message, final_usage = client.chat(
        messages=continuation,
        model=canary.model,
        tools=[copy.deepcopy(_delegate_start_tool(tools))],
        tool_choice="none",
        reasoning_effort=canary.reasoning_effort,
        max_tokens=CANARY_CONTINUATION_MAX_TOKENS,
        no_proxy=True,
        timeout=CANARY_TIMEOUT_SEC,
    )
    final_disclosure = assert_canary_usage(final_usage, canary)
    if isinstance(first_disclosure, dict) and isinstance(final_disclosure, dict):
        assert final_disclosure.get("candidate_sha256") != first_disclosure.get(
            "candidate_sha256"
        )
    assert not final_message.get("tool_calls"), final_message
    assert str(final_message.get("content") or "").strip() == final_marker, final_message
    return message, usage, final_message, final_usage
