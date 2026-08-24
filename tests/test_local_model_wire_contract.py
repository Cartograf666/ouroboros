"""The local llama-cpp-python server is STRICTER than the OpenAI wire.

A hosted provider returns `content: null` on a tool-calling assistant turn, and
every hosted provider accepts it back. llama-cpp-python's request model types
assistant content as a bare `str`, so that null matches none of its five message
variants and the ENTIRE request is rejected — the loop sees a 500 and can only
read it as the provider being down, which is how a working local model looked
like an outage.

These tests run the server's OWN schema rather than a restatement of it, so a
future llama-cpp-python that tightens or relaxes a rule is caught here instead
of mid-task.
"""

from __future__ import annotations

import pytest

pytest.importorskip("llama_cpp", reason="local-model extra not installed")


def _validate(messages):
    from llama_cpp.server.types import CreateChatCompletionRequest

    CreateChatCompletionRequest(model="local", messages=messages)


def _tool_call_turn(content):
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }],
    }


def test_the_server_really_does_reject_a_null_assistant_content():
    """The premise of the fix. If this ever passes, the workaround is obsolete
    and should be removed rather than left as unexplained ceremony."""
    with pytest.raises(Exception):
        _validate([{"role": "user", "content": "hi"}, _tool_call_turn(None)])


def test_the_empty_string_the_fix_substitutes_is_accepted():
    _validate([{"role": "user", "content": "hi"}, _tool_call_turn("")])


def test_a_tool_result_needs_its_call_id():
    _validate([
        {"role": "user", "content": "hi"},
        _tool_call_turn(""),
        {"role": "tool", "content": "done", "tool_call_id": "call_1"},
    ])


def test_the_local_path_normalizes_null_content_before_it_is_sent():
    """The seam itself: whatever the loop hands over, nothing with a null content
    reaches the server. Scheduling around a 500 is not a recovery — the request
    was never malformed in a way the model could answer differently."""
    import inspect

    from ouroboros.llm import LLMClient

    source = inspect.getsource(LLMClient._chat_local)
    assert "elif content is None:" in source
    assert 'msg["content"] = ""' in source


# --- the same rejection reached us through the OTHER lane -----------------------
#
# `USE_LOCAL_FALLBACK` points the fallback at a local model, but the fallback ROW
# itself can name an `openai-compatible::` model, and that lane does not go through
# `_chat_local` at all. So the identical null-content request went out a second way
# — to whatever server the owner's compatible base URL names, which is very often
# another llama-cpp-python. Fixing only the local path left the failure looking
# unchanged: same 500, same "provider outage", one round later.

def _remote_target(provider):
    return {
        "provider": provider, "model": "m", "resolved_model": "m",
        "base_url": "https://example.invalid/v1", "api_key": "x",
        "supports_openrouter_extensions": provider == "openrouter",
        "supports_generation_cost": False, "default_headers": {}, "verify_ssl_certs": True,
    }


def _built_messages(provider):
    from ouroboros.llm import LLMClient

    client = LLMClient.__new__(LLMClient)
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }]},
    ]
    return client._build_remote_kwargs(
        _remote_target(provider), messages, "medium", 512, "auto", None, None,
        skip_capability_fetch=True,
    )["messages"]


def test_the_compatible_lane_sends_no_null_content():
    built = _built_messages("openai-compatible")
    assert [m for m in built if m.get("content") is None] == []
    assert built[2]["content"] == ""


def test_the_tool_calls_survive_the_rewrite():
    """The empty string replaces the content, never the message: which tools the
    assistant asked for is the only thing that turn carries."""
    built = _built_messages("openai-compatible")
    assert built[2]["tool_calls"][0]["function"]["name"] == "read_file"


def test_a_named_lane_still_sends_the_null():
    """OpenRouter and the first-party providers accept `content: null` correctly.
    Rewriting what we send them buys nothing and would hide a real difference."""
    assert _built_messages("openrouter")[2]["content"] is None


def test_what_the_compatible_lane_now_sends_passes_the_strict_schema():
    """End to end against the real validator: the exact payload shape that used to
    come back as `7 validation errors`."""
    _validate(_built_messages("openai-compatible"))
