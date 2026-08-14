"""Context overflow classification and recovery disclosure."""

import json

from ouroboros.llm import LocalContextTooLargeError
from ouroboros.loop import _provider_recovery_hint
from ouroboros.loop_llm_call import (
    _LlmErrorContext,
    _is_context_overflow_error,
    _record_llm_call_error,
    classify_llm_exception,
)


class _TypedOverflow(RuntimeError):
    status_code = 400
    body = {"error": {"code": "context_length_exceeded", "type": "invalid_request_error"}}


def test_untyped_context_scan_excludes_rate_and_output_size_errors():
    assert _is_context_overflow_error(LocalContextTooLargeError("too big"), "")
    assert _is_context_overflow_error(Exception(), "prompt is too long for context window")
    assert not _is_context_overflow_error(Exception(), "429 rate limit exceeded")
    assert not _is_context_overflow_error(Exception(), "Rate limit: too many tokens per minute")

    for text in (
        "max_tokens 65536 exceeds maximum context length 32768",
        "maximum output tokens exceed the context window",
        "request body too large",
    ):
        assert classify_llm_exception(RuntimeError(text), text).kind == "request_too_large"


def test_structured_context_code_wins_over_output_wording():
    result = classify_llm_exception(
        _TypedOverflow("max_tokens exceeds maximum context length"),
        "max_tokens exceeds maximum context length",
    )
    assert result.kind == "context_overflow"
    assert result.retry_same_request is False


def test_recovery_hint_uses_typed_kind_without_suggesting_owner_mode_change():
    hint = _provider_recovery_hint({"_last_llm_error_kind": "context_overflow"})
    assert "context overflowed" in hint.lower()
    assert "low context mode" not in hint.lower()


def test_remote_context_overflow_is_not_logged_as_local_or_global_mode_hint(tmp_path):
    usage = {
        "_context_profile": "owner_low",
        "_context_target_miss": True,
        "_context_automatic_pass_used": True,
    }
    ctx = _LlmErrorContext(
        task_id="task-ctx",
        task_type="task",
        execution_id="exec-1",
        round_id="round-1",
        llm_call_id="call-1",
        round_idx=1,
        attempt=0,
        model="provider/model",
        request_ref=None,
        drive_logs=tmp_path,
        event_queue=None,
        accumulated_usage=usage,
        context_fit_event_fields={
            "context_profile": "owner_low",
            "context_target_miss": True,
            "context_automatic_pass_used": True,
        },
    )

    stop_retry = _record_llm_call_error(_TypedOverflow("provider rejected request"), ctx)
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]

    assert stop_retry is True
    assert any(row["type"] == "remote_context_overflow" for row in rows)
    assert not any(row["type"] == "local_context_overflow" for row in rows)
    assert usage["_last_llm_error_kind"] == "context_overflow"
    assert "context_overflow_suggest_low" not in usage
    api_error = next(row for row in rows if row["type"] == "llm_api_error")
    assert api_error["context_profile"] == "owner_low"
    assert api_error["context_target_miss"] is True
    assert api_error["context_automatic_pass_used"] is True
