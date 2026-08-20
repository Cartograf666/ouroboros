"""Secretless CI contracts for shipped provider-wire alarms."""

from __future__ import annotations

import copy
import json

import pytest

from ouroboros.provider_models import OPENAI_DIRECT_DEFAULTS, normalize_model_identity
from ouroboros.request_wire_contract import canonical_sha256
from ouroboros.request_wire_receipts import (
    WireCandidateSpec,
    bind_wire_candidate,
    observe_wire_semantics,
)
from ouroboros.usage_accounting import PhysicalAttemptCapture
from tests.provider_contract_ci import (
    OPENAI_CANARY_ARGUMENTS,
    OPENAI_CANARY_MAX_TOKENS,
    OPENAI_CANARY_TIMEOUT_SEC,
    OPENAI_CANARY_TOOL_NAME,
    ProviderFailureClassification,
    ProviderFailureKind,
    assert_normalized_canary_call,
    assert_openai_canary_usage,
    classify_provider_failure,
    registry_openai_canary_tool,
    require_openai_canary_key,
    run_openai_reasoning_tool_canary,
    skip_on_provider_environmental_error,
    unique_openai_direct_defaults,
)


def _http_error(status_code: int, body: str):
    class Response:
        pass

    response = Response()
    response.status_code = status_code
    response.text = body
    exc = RuntimeError(f"provider returned HTTP {status_code}")
    exc.response = response
    return exc


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (
            400,
            (
                '{"error":{"message":"Function tools with reasoning_effort are not '
                "supported for gpt-5.6-luna in /v1/chat/completions. To use function "
                'tools, use /v1/responses or set reasoning_effort to none."}}'
            ),
        ),
        (400, '{"error":{"message":"Unknown parameter: tools[0].custom"}}'),
        (401, '{"error":{"code":"invalid_api_key"}}'),
        (403, '{"message":"API key verification failed: key is expired"}'),
        (404, '{"error":{"code":"model_not_found"}}'),
        (422, '{"error":{"message":"reasoning_effort medium is unsupported"}}'),
    ],
)
def test_contract_and_auth_4xx_are_red(status_code, body):
    exc = _http_error(status_code, body)
    assert classify_provider_failure(
        "openai_direct",
        exc,
    ) == ProviderFailureClassification(
        ProviderFailureKind.RED,
        "provider_contract_or_unclassified",
        status_code,
    )
    assert skip_on_provider_environmental_error("openai_direct", exc) is None


@pytest.mark.parametrize(
    ("status_code", "body", "reason"),
    [
        (429, '{"error":{"code":"rate_limit_exceeded"}}', "rate_limit_429"),
        (503, '{"error":{"message":"upstream unavailable"}}', "provider_5xx"),
        (400, '{"error":{"code":"insufficient_quota"}}', "quota_or_billing"),
        (402, '{"error":{"message":"credit balance is too low"}}', "quota_or_billing"),
    ],
)
def test_only_explicit_environmental_outcomes_are_inconclusive(
    status_code,
    body,
    reason,
):
    classification = classify_provider_failure(
        "openai_direct",
        _http_error(status_code, body),
    )
    assert classification.kind is ProviderFailureKind.INCONCLUSIVE
    assert classification.reason == reason
    assert classification.status_code == status_code
    with pytest.raises(pytest.skip.Exception, match=reason):
        skip_on_provider_environmental_error(
            "openai_direct",
            _http_error(status_code, body),
        )


def test_timeout_is_inconclusive_but_http_400_with_timeout_cause_is_red():
    timeout = TimeoutError("timed out")
    assert classify_provider_failure(
        "openai_direct",
        timeout,
    ) == ProviderFailureClassification(
        ProviderFailureKind.INCONCLUSIVE,
        "transport_timeout",
    )
    with pytest.raises(pytest.skip.Exception, match="transport_timeout"):
        skip_on_provider_environmental_error("openai_direct", timeout)

    http_400 = _http_error(400, '{"error":{"message":"reasoning timeout invalid"}}')
    http_400.__cause__ = TimeoutError("socket timed out")
    assert (
        classify_provider_failure(
            "openai_direct",
            http_400,
        ).kind
        is ProviderFailureKind.RED
    )


def test_cloudru_disconnect_and_generic_connection_error_are_red():
    disconnect = RuntimeError("APIConnectionError: Connection error.")
    disconnect.__cause__ = RuntimeError("httpx.RemoteProtocolError: Server disconnected without sending a response.")
    assert (
        classify_provider_failure(
            "cloudru",
            disconnect,
        ).kind
        is ProviderFailureKind.RED
    )
    assert (
        classify_provider_failure(
            "cloudru",
            RuntimeError("APIConnectionError: Connection error."),
        ).kind
        is ProviderFailureKind.RED
    )


def test_provider_alarm_output_sanitizes_token_shaped_evidence(capsys):
    sentinel = "sk-proj-" + ("A" * 40)
    exc = _http_error(429, f'{{"error":{{"token":"{sentinel}"}}}}')
    with pytest.raises(pytest.skip.Exception) as caught:
        skip_on_provider_environmental_error("openai_direct", exc)

    assert sentinel not in capsys.readouterr().err
    assert sentinel not in str(caught.value)
    assert "***REDACTED***" in str(caught.value)

    with pytest.raises(pytest.skip.Exception) as caught_message:
        skip_on_provider_environmental_error(
            "openai_direct",
            TimeoutError(f"timed out with token {sentinel}"),
        )
    assert sentinel not in str(caught_message.value)
    assert "***REDACTED***" in str(caught_message.value)


def test_openai_key_is_required_only_on_official_github_job(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "razzant/ouroboros")
    with pytest.raises(pytest.fail.Exception, match="required"):
        require_openai_canary_key()

    monkeypatch.setenv("GITHUB_REPOSITORY", "fork/ouroboros")
    with pytest.raises(pytest.skip.Exception, match="not set"):
        require_openai_canary_key()

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    with pytest.raises(pytest.skip.Exception, match="not set"):
        require_openai_canary_key()


def test_openai_models_follow_default_ssot_without_allowlist(monkeypatch):
    expected = tuple(dict.fromkeys(model for model in OPENAI_DIRECT_DEFAULTS.values() if model))
    assert unique_openai_direct_defaults() == expected
    assert OPENAI_DIRECT_DEFAULTS["main"] in expected
    assert len(expected) == len(set(expected))
    assert all(model.startswith("openai::") for model in expected)

    future = "openai::future-direct-model"
    monkeypatch.setitem(OPENAI_DIRECT_DEFAULTS, "future_ci_role", future)
    monkeypatch.setitem(OPENAI_DIRECT_DEFAULTS, "future_ci_duplicate", future)
    assert unique_openai_direct_defaults()[-1] == future
    assert unique_openai_direct_defaults().count(future) == 1


def test_registry_canary_lookup_never_refreshes_mcp(monkeypatch, tmp_path):
    import ouroboros.mcp_client as mcp_client

    refreshes = []

    def record_refresh(*_args, **kwargs):
        refreshes.append(kwargs.get("refresh"))

    monkeypatch.setattr(
        mcp_client,
        "ensure_configured_from_settings",
        record_refresh,
    )
    tool = registry_openai_canary_tool(tmp_path)
    assert tool["function"]["name"] == OPENAI_CANARY_TOOL_NAME
    assert refreshes == []


def _bind_candidate(model, tool, messages, tool_choice):
    prefix, separator, resolved_model = model.partition("::")
    assert (prefix, separator) == ("openai", "::")
    source = {
        "model": resolved_model,
        "messages": copy.deepcopy(messages),
        "reasoning_effort": "medium",
        "max_completion_tokens": OPENAI_CANARY_MAX_TOKENS,
        "tools": [copy.deepcopy(tool)],
        "tool_choice": copy.deepcopy(tool_choice),
    }
    return bind_wire_candidate(
        target={
            "provider": "openai",
            "resolved_model": resolved_model,
            "usage_model": normalize_model_identity(model),
            "base_url": "https://api.openai.com/v1",
        },
        api_surface="chat.completions",
        source_payload=source,
        candidate_spec=WireCandidateSpec(
            "openai_chat_custom",
            "medium",
            "requested_wire_form",
        ),
        requested_effort="medium",
        ladder_ordinal=1,
    )


def test_shipped_defaults_bind_registry_strict_custom_medium_shape(tmp_path):
    tool = registry_openai_canary_tool(tmp_path)
    choice = {
        "type": "function",
        "function": {"name": OPENAI_CANARY_TOOL_NAME},
    }
    for model in unique_openai_direct_defaults():
        candidate = _bind_candidate(
            model,
            tool,
            [{"role": "user", "content": "Call read_file exactly once."}],
            choice,
        )
        physical = candidate.physical_payload()
        assert candidate.source_profile.function_strictness == "strict"
        assert candidate.source_profile.tool_dialect == "function"
        assert candidate.accepted_profile.tool_dialect == "openai_chat_custom"
        assert candidate.accepted_profile.reasoning_carrier == "reasoning_effort"
        assert candidate.candidate_spec.reason_code == "requested_wire_form"
        assert candidate.physical_model == normalize_model_identity(model)
        assert physical["model"] == model.split("::", 1)[-1]
        assert physical["reasoning_effort"] == "medium"
        assert physical["max_completion_tokens"] == OPENAI_CANARY_MAX_TOKENS
        assert "max_tokens" not in physical
        assert physical["tool_choice"] == {
            "type": "custom",
            "custom": {"name": OPENAI_CANARY_TOOL_NAME},
        }
        assert physical["tools"][0]["custom"]["format"]["type"] == "grammar"
        assert (
            candidate.custom_catalog.schema_binding(OPENAI_CANARY_TOOL_NAME).schema() == tool["function"]["parameters"]
        )


def test_custom_call_normalizes_and_replays_role_tool_continuation(tmp_path):
    from ouroboros.openai_chat_custom import normalize_openai_custom_tool_calls

    model = unique_openai_direct_defaults()[0]
    tool = registry_openai_canary_tool(tmp_path)
    user = {"role": "user", "content": "Call read_file exactly once."}
    first = _bind_candidate(
        model,
        tool,
        [user],
        {
            "type": "function",
            "function": {"name": OPENAI_CANARY_TOOL_NAME},
        },
    )
    raw_arguments = json.dumps(OPENAI_CANARY_ARGUMENTS, sort_keys=True)
    canonical_calls, receipts = normalize_openai_custom_tool_calls(
        [
            {
                "id": "call_phase2c",
                "type": "custom",
                "custom": {"name": OPENAI_CANARY_TOOL_NAME, "input": raw_arguments},
            }
        ],
        first,
    )
    assert receipts[0].allows_execution
    assert canonical_calls[0] == {
        "id": "call_phase2c",
        "type": "function",
        "function": {
            "name": OPENAI_CANARY_TOOL_NAME,
            "arguments": raw_arguments,
        },
    }

    nonce = "deterministic-phase2c-nonce"
    continuation = _bind_candidate(
        model,
        tool,
        [
            user,
            {"role": "assistant", "content": None, "tool_calls": canonical_calls},
            {
                "role": "tool",
                "tool_call_id": "call_phase2c",
                "content": json.dumps({"nonce": nonce}),
            },
        ],
        "none",
    )
    physical = continuation.physical_payload()
    assert physical["messages"][1]["tool_calls"][0]["type"] == "custom"
    assert physical["messages"][1]["tool_calls"][0]["custom"]["input"] == raw_arguments
    assert physical["messages"][2]["role"] == "tool"
    assert physical["messages"][2]["tool_call_id"] == "call_phase2c"
    assert nonce in physical["messages"][2]["content"]
    assert physical["tool_choice"] == "none"
    assert physical["reasoning_effort"] == "medium"


def _fake_usage(model, ordinal):
    return {
        "provider": "openai",
        "resolved_model": normalize_model_identity(model),
        "prompt_tokens": 12,
        "completion_tokens": 4,
        "request_wire": {
            "requested_effort": "medium",
            "applied_effort": "medium",
            "requested_tool_dialect": "function",
            "applied_tool_dialect": "openai_chat_custom",
            "reason_code": "requested_wire_form",
            "source_profile_fingerprint": "a" * 64,
            "accepted_profile_fingerprint": "b" * 64,
            "attempt_id": f"attempt-{ordinal}",
            "candidate_sha256": ("c" if ordinal == 1 else "d") * 64,
            "ladder_ordinal": 1,
            "applied_actions": [],
            "task_local": False,
        },
    }


def test_reasoning_integrity_canary_rejects_explicit_none_and_clamp():
    model = unique_openai_direct_defaults()[0]
    explicit_none = _fake_usage(model, 1)
    explicit_none["request_wire"]["applied_effort"] = "none"
    with pytest.raises(AssertionError):
        assert_openai_canary_usage(explicit_none, model)

    clamped = _fake_usage(model, 1)
    clamped["reasoning_effort_clamped"] = {
        "requested": "medium",
        "applied": "none",
        "reason": "task_local_availability_fallback",
    }
    with pytest.raises(AssertionError):
        assert_openai_canary_usage(clamped, model)

    neutral_repair = _fake_usage(model, 1)
    neutral_repair["request_wire"]["applied_actions"] = [{
        "source": "pending",
        "profile_fingerprint": "e" * 64,
        "action": {
            "kind": "drop_field",
            "fields": ["temperature"],
            "reason_code": "provider_unsupported_field",
        },
    }]
    assert_openai_canary_usage(neutral_repair, model)


def _canonical_canary_call(call_id):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": OPENAI_CANARY_TOOL_NAME,
            "arguments": json.dumps(OPENAI_CANARY_ARGUMENTS, sort_keys=True),
        },
    }


def test_canary_call_validation_rejects_duplicate_ids(tmp_path):
    tool = registry_openai_canary_tool(tmp_path)
    duplicate = _canonical_canary_call("call-duplicate")
    with pytest.raises(AssertionError):
        assert_normalized_canary_call(
            {
                "tool_calls": [
                    duplicate,
                    copy.deepcopy(duplicate),
                ]
            },
            tool,
        )


def test_production_custom_none_text_is_semantic_success(monkeypatch, tmp_path):
    model = unique_openai_direct_defaults()[0]
    tool = registry_openai_canary_tool(tmp_path)
    candidate = _bind_candidate(
        model,
        tool,
        [{"role": "user", "content": "Use the supplied tool result."}],
        "none",
    )
    resolved_model = model.split("::", 1)[-1]
    source = {
        "model": resolved_model,
        "messages": [{"role": "user", "content": "Use the supplied tool result."}],
        "reasoning_effort": "medium",
        "max_completion_tokens": OPENAI_CANARY_MAX_TOKENS,
        "tools": [copy.deepcopy(tool)],
        "tool_choice": "none",
    }
    target = {
        "provider": "openai",
        "resolved_model": resolved_model,
        "usage_model": normalize_model_identity(model),
        "base_url": "https://api.openai.com/v1",
    }
    assert candidate.forbids_tool_call is True
    assert candidate.applied_actions == ()
    foundation_observation = observe_wire_semantics(
        candidate=candidate,
        normalized_response={
            "role": "assistant",
            "content": "phase2c semantic text",
        },
        normalized_usage={},
    )
    assert foundation_observation.semantic_kind == "chat_message"

    import ouroboros.openai_chat_dispatch as dispatch
    import ouroboros.request_wire_recovery as recovery

    attempt_id = "phase2c-custom-none-text"
    capture = PhysicalAttemptCapture(
        attempt_id=attempt_id,
        model=candidate.physical_model,
        provider="openai",
        state="settled",
        candidate_measurement_kind="canonical_json_v1",
        candidate_raw_sha256=candidate.candidate_sha256,
        candidate_manifest_ref={
            "path": f"physical/{attempt_id}.json",
            "call_id": attempt_id,
            "sha256": canonical_sha256(attempt_id),
        },
        provider_status_code=200,
    )
    issued = []
    real_bind = recovery.bind_wire_compatibility_receipt

    def record_receipt(**kwargs):
        receipt = real_bind(**kwargs)
        issued.append(receipt)
        return receipt

    monkeypatch.setattr(recovery, "bind_wire_compatibility_receipt", record_receipt)
    with recovery.request_wire_call_scope():
        recovery.register_wire_candidate(
            candidate, source_payload=source, target=target,
        )
        recovery.note_wire_send_succeeded(capture)
        message, usage = dispatch.normalize_direct_openai_completion(
            {"role": "assistant", "content": "phase2c semantic text"},
            {
                "provider": "openai",
                "resolved_model": normalize_model_identity(model),
                "prompt_tokens": 12,
                "completion_tokens": 4,
            },
            None,
        )
        recovery.finalize_wire_response(message, usage)

    assert message == {"role": "assistant", "content": "phase2c semantic text"}
    assert dispatch.CUSTOM_RECEIPTS_USAGE_KEY not in usage
    assert len(issued) == 1
    assert issued[0].semantic_kind == "chat_message"
    assert_openai_canary_usage(usage, model)


def test_public_chat_contract_consumes_nonce_continuation(tmp_path):
    model = unique_openai_direct_defaults()[0]
    tool = registry_openai_canary_tool(tmp_path)
    nonce = "unit-public-chat-nonce"

    class FakeClient:
        def __init__(self):
            self.calls = []

        def chat(self, **kwargs):
            self.calls.append(copy.deepcopy(kwargs))
            ordinal = len(self.calls)
            if ordinal == 1:
                return (
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            _canonical_canary_call("call_public_contract_1"),
                            _canonical_canary_call("call_public_contract_2"),
                        ],
                    },
                    _fake_usage(model, ordinal),
                )
            return (
                {
                    "role": "assistant",
                    "content": f"PHASE2C_CONTINUED_{nonce}",
                },
                _fake_usage(model, ordinal),
            )

    client = FakeClient()
    run_openai_reasoning_tool_canary(
        client,
        model=model,
        tool=tool,
        nonce=nonce,
        continue_to_final=True,
    )

    assert len(client.calls) == 2
    first, second = client.calls
    for call in client.calls:
        assert call["model"] == model
        assert call["tools"] == [tool]
        assert call["reasoning_effort"] == "medium"
        assert call["max_tokens"] == OPENAI_CANARY_MAX_TOKENS
        assert call["no_proxy"] is True
        assert call["timeout"] == OPENAI_CANARY_TIMEOUT_SEC
    assert first["tool_choice"]["function"]["name"] == OPENAI_CANARY_TOOL_NAME
    assert second["tool_choice"] == "none"
    assert nonce not in first["messages"][0]["content"]
    assert [message["role"] for message in second["messages"]] == [
        "user",
        "assistant",
        "tool",
        "tool",
    ]
    assert [message["tool_call_id"] for message in second["messages"][2:]] == [
        "call_public_contract_1",
        "call_public_contract_2",
    ]
    assert all(nonce in message["content"] for message in second["messages"][2:])
