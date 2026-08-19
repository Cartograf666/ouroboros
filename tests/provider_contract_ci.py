"""Shared, secretless contracts for the trusted provider integration lane."""

from __future__ import annotations

import copy
import json
import os
import pathlib
from dataclasses import dataclass
from enum import Enum

import pytest

from ouroboros.provider_models import OPENAI_DIRECT_DEFAULTS, normalize_model_identity
from ouroboros.utils import sanitize_tool_result_for_log

OPENAI_CANARY_TIMEOUT_SEC = 120.0
OPENAI_CANARY_MAX_TOKENS = 1024
OPENAI_CANARY_TOOL_NAME = "read_file"
OPENAI_CANARY_ARGUMENTS = {
    "path": "BIBLE.md",
    "root": "system_repo",
    "start_line": 1,
    "max_lines": 1,
}


def unique_openai_direct_defaults():
    """Derive the live matrix from the shipped provider-default SSOT."""
    return tuple(dict.fromkeys(model for model in OPENAI_DIRECT_DEFAULTS.values() if model))


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
        )
    ):
        return ProviderFailureClassification(
            ProviderFailureKind.INCONCLUSIVE,
            "quota_or_billing",
            status,
        )
    if status == 429:
        return ProviderFailureClassification(
            ProviderFailureKind.INCONCLUSIVE,
            "rate_limit_429",
            status,
        )
    if status is not None and 500 <= status < 600:
        return ProviderFailureClassification(
            ProviderFailureKind.INCONCLUSIVE,
            "provider_5xx",
            status,
        )
    if status is None and any(
        isinstance(item, TimeoutError) or "timeout" in type(item).__name__.lower() for item in chain
    ):
        return ProviderFailureClassification(
            ProviderFailureKind.INCONCLUSIVE,
            "transport_timeout",
        )
    return ProviderFailureClassification(
        ProviderFailureKind.RED,
        "provider_contract_or_unclassified",
        status,
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
        pytest.skip(f"[{provider_id}] inconclusive provider alarm ({classification.reason}): {detail}")


def official_openai_integration_job():
    return (
        os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"
        and os.environ.get("GITHUB_REPOSITORY", "").strip() == "razzant/ouroboros"
    )


def require_openai_canary_key():
    if str(os.environ.get("OPENAI_API_KEY", "") or "").strip():
        return
    if official_openai_integration_job():
        pytest.fail("OPENAI_API_KEY is required by the official integration job")
    pytest.skip("OPENAI_API_KEY not set")


def registry_openai_canary_tool(drive_root):
    """Take one real shipped tool schema, then request its strict custom twin."""
    from ouroboros.tools.registry import ToolRegistry

    repo_dir = pathlib.Path(__file__).resolve().parents[1]
    registered = ToolRegistry(
        repo_dir=repo_dir,
        drive_root=pathlib.Path(drive_root),
    ).get_schema_by_name(OPENAI_CANARY_TOOL_NAME)
    assert registered is not None
    tool = copy.deepcopy(registered)
    tool["function"]["strict"] = True
    tool["function"]["parameters"]["additionalProperties"] = False
    return tool


def assert_openai_canary_usage(usage, model):
    assert isinstance(usage, dict), usage
    assert usage.get("provider") == "openai", usage
    assert usage.get("resolved_model") == normalize_model_identity(model), usage
    assert int(usage.get("prompt_tokens") or 0) > 0, usage
    assert int(usage.get("completion_tokens") or 0) > 0, usage
    assert usage.get("reasoning_effort_clamped") is None, usage

    disclosure = usage.get("request_wire")
    assert isinstance(disclosure, dict), f"Phase 2A public request-wire disclosure is missing from usage; usage={usage}"
    assert disclosure["requested_effort"] == "medium"
    assert disclosure["applied_effort"] == "medium"
    assert disclosure["requested_tool_dialect"] == "function"
    assert disclosure["applied_tool_dialect"] == "openai_chat_custom"
    assert disclosure["reason_code"] == "requested_wire_form"
    assert disclosure["ladder_ordinal"] == 1
    assert disclosure["applied_actions"] == []
    assert disclosure["task_local"] is False
    assert disclosure["attempt_id"]
    for key in (
        "source_profile_fingerprint",
        "accepted_profile_fingerprint",
        "candidate_sha256",
    ):
        value = str(disclosure.get(key) or "")
        assert len(value) == 64 and not (set(value.lower()) - set("0123456789abcdef"))
    return disclosure


def assert_normalized_canary_call(message, tool):
    from jsonschema import validators

    calls = message.get("tool_calls") if isinstance(message, dict) else None
    assert isinstance(calls, list) and calls, message
    schema = tool["function"]["parameters"]
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
        assert function.get("name") == OPENAI_CANARY_TOOL_NAME, call
        raw_arguments = function.get("arguments")
        assert isinstance(raw_arguments, str), call
        arguments = json.loads(raw_arguments)
        assert arguments == OPENAI_CANARY_ARGUMENTS
        assert not list(validator_class(schema).iter_errors(arguments))
    return calls


def run_openai_reasoning_tool_canary(
    client,
    *,
    model,
    tool,
    nonce,
    continue_to_final,
):
    """Exercise only the public production chat seam, never a raw SDK client."""
    final_marker = f"PHASE2C_CONTINUED_{nonce}"
    expected_arguments = json.dumps(OPENAI_CANARY_ARGUMENTS, sort_keys=True)
    conversation = [
        {
            "role": "user",
            "content": (
                f"Call {OPENAI_CANARY_TOOL_NAME} exactly once with arguments "
                f"{expected_arguments}. After its tool result, read the "
                "expected_final_marker field and reply with exactly that value. "
                "Do not call another tool."
            ),
        }
    ]
    message, usage = client.chat(
        messages=conversation,
        model=model,
        tools=[tool],
        tool_choice={
            "type": "function",
            "function": {"name": OPENAI_CANARY_TOOL_NAME},
        },
        reasoning_effort="medium",
        max_tokens=OPENAI_CANARY_MAX_TOKENS,
        no_proxy=True,
        timeout=OPENAI_CANARY_TIMEOUT_SEC,
    )
    calls = assert_normalized_canary_call(message, tool)
    first_disclosure = assert_openai_canary_usage(usage, model)
    if not continue_to_final:
        return message, usage, None, None

    continuation = [
        *conversation,
        message,
        *[
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(
                    {
                        "ok": True,
                        "nonce": nonce,
                        "expected_final_marker": final_marker,
                    },
                    sort_keys=True,
                ),
            }
            for call in calls
        ],
    ]
    final_message, final_usage = client.chat(
        messages=continuation,
        model=model,
        tools=[tool],
        tool_choice="none",
        reasoning_effort="medium",
        max_tokens=OPENAI_CANARY_MAX_TOKENS,
        no_proxy=True,
        timeout=OPENAI_CANARY_TIMEOUT_SEC,
    )
    final_disclosure = assert_openai_canary_usage(final_usage, model)
    assert final_disclosure["candidate_sha256"] != first_disclosure["candidate_sha256"]
    assert not final_message.get("tool_calls"), final_message
    assert str(final_message.get("content") or "").strip() == final_marker, final_message
    return message, usage, final_message, final_usage
