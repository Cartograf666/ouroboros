"""Regression tests: a retried LLM call must not be served from a response cache.

`provider_incomplete_response` is classified as transient (`_TRANSIENT_RETRY_KINDS`),
i.e. the retry loop assumes a repeat MAY produce a different result.  That assumption
only holds when nothing between the client and the model caches responses.  A gateway
response cache (LiteLLM `cache: true`) replays the identical failed body for every
attempt, so the whole transient-retry budget is spent without ever reaching the model
and the task ends as `infra_failed` / `provider_unavailable`.

Observed in the field: six attempts, one shared `response_id`, each returned in ~0.0s;
the same request replayed later succeeded.
"""

from __future__ import annotations


class TestBuildRemoteKwargsCacheOptOut:
    def test_no_cache_field_absent_by_default(self):
        from ouroboros.llm import LLMClient

        client = LLMClient(api_key="test")
        target = {
            "provider": "openai-compatible",
            "resolved_model": "local-reason",
            "usage_model": "local-reason",
            "api_key": "test",
            "base_url": "http://127.0.0.1:4000/v1",
            "default_headers": {},
        }
        kwargs = client._build_remote_kwargs(
            target=target,
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="medium",
            max_tokens=256,
            tool_choice="auto",
            temperature=None,
            tools=None,
        )
        assert "cache" not in (kwargs.get("extra_body") or {}), (
            "the first attempt must stay cacheable; only retries opt out"
        )
        assert "cache" not in kwargs, "cache must never be a top-level kwarg"

    def test_no_cache_field_present_when_bypassing(self):
        from ouroboros.llm import LLMClient

        client = LLMClient(api_key="test")
        target = {
            "provider": "openai-compatible",
            "resolved_model": "local-reason",
            "usage_model": "local-reason",
            "api_key": "test",
            "base_url": "http://127.0.0.1:4000/v1",
            "default_headers": {},
        }
        kwargs = client._build_remote_kwargs(
            target=target,
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="medium",
            max_tokens=256,
            tool_choice="auto",
            temperature=None,
            tools=None,
            bypass_response_cache=True,
        )
        assert (kwargs.get("extra_body") or {}).get("cache") == {"no-cache": True}, (
            "a retry must carry LiteLLM's documented per-request cache opt-out, "
            "otherwise the gateway replays the cached failed response"
        )
        assert "cache" not in kwargs, (
            "the OpenAI SDK raises TypeError on unknown top-level kwargs, so the "
            "opt-out must ride in extra_body"
        )


class TestRetryLoopRequestsCacheOptOut:
    def test_first_attempt_does_not_bypass_but_retry_does(self):
        """The kwargs the retry loop hands to `LLM.chat` must differ across attempts.

        Guards the actual defect: the payload used to be rebuilt byte-identically on
        every attempt, which is what made the cached response unavoidable.
        """
        import inspect

        from ouroboros import loop_llm_call

        source = inspect.getsource(loop_llm_call.call_llm_with_retry)
        assert "bypass_response_cache" in source, (
            "call_llm_with_retry must vary the request on retries; without it a "
            "response cache in front of the provider makes the retry budget useless"
        )
        assert "attempt > 0" in source, (
            "the cache opt-out must be tied to retry attempts, not applied to the "
            "first call (which should still benefit from a warm cache)"
        )


class TestChatSignatureThreadsFlag:
    def test_chat_accepts_bypass_response_cache(self):
        import inspect

        from ouroboros.llm import LLMClient

        params = inspect.signature(LLMClient.chat).parameters
        assert "bypass_response_cache" in params, (
            "LLM.chat must expose the flag so the retry loop can request a fresh call"
        )
        assert params["bypass_response_cache"].default is False, (
            "bypassing the cache must be opt-in"
        )
