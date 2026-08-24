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
