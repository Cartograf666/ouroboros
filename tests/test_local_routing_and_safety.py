import pytest


def test_prepare_messages_for_local_context_preserves_core_and_compacts_non_core():
    from ouroboros.llm import LLMClient

    client = LLMClient()
    messages = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "SYSTEM PROMPT\n\n"
                        "## BIBLE.md\n\nBIBLE TEXT\n\n"
                        "## ARCHITECTURE.md\n\n" + ("A" * 4000)
                    ),
                },
                {
                    "type": "text",
                    "text": (
                        "## Identity\n\nIDENTITY\n\n"
                        "## Knowledge base\n\nKB\n\n"
                        "## Last Deep Self-Review\n\nDEEP\n\n"
                        "## Known error patterns (Pattern Register)\n\nPATTERNS"
                    ),
                },
                {
                    "type": "text",
                    "text": (
                        "## Scratchpad\n\nSCRATCHPAD\n\n"
                        "## Dialogue History\n\n" + ("D" * 4000) + "\n\n"
                        "## Memory Registry\n\nREGISTRY\n\n"
                        "## Drive state\n\n{}\n\n"
                        "## Runtime context\n\nruntime\n\n"
                        "## Recent tools\n\n" + ("T" * 4000)
                    ),
                },
            ],
        },
        {"role": "user", "content": "hello"},
    ]

    compacted = client._prepare_messages_for_local_context(messages, ctx_len=2600, max_tokens=500)
    system_blocks = compacted[0]["content"]

    assert "## BIBLE.md" in system_blocks[0]["text"]
    assert "ARCHITECTURE.md" in system_blocks[0]["text"]
    assert "[Compacted for local-model context" in system_blocks[0]["text"]
    assert "## Identity" in system_blocks[1]["text"]
    assert "## Knowledge base" in system_blocks[1]["text"]
    assert "## Last Deep Self-Review" in system_blocks[1]["text"]
    assert "## Scratchpad" not in system_blocks[1]["text"]
    assert "[Compacted for local-model context" in system_blocks[1]["text"]
    assert "## Dialogue History" in system_blocks[2]["text"]
    assert "## Memory Registry" in system_blocks[2]["text"]
    assert "## Drive state" in system_blocks[2]["text"]
    assert "## Runtime context" in system_blocks[2]["text"]
    assert "[Compacted for local-model context" in system_blocks[2]["text"]



def test_prepare_messages_for_local_context_raises_when_core_still_too_large():
    from ouroboros.llm import LLMClient, LocalContextTooLargeError

    client = LLMClient()
    huge_core = "X" * 12000
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": f"SYSTEM\n\n## BIBLE.md\n\n{huge_core}"},
                {"type": "text", "text": f"## Scratchpad\n\n{huge_core}\n\n## Identity\n\n{huge_core}"},
                {"type": "text", "text": "## Drive state\n\n{}"},
            ],
        },
        {"role": "user", "content": "hello"},
    ]

    with pytest.raises(LocalContextTooLargeError):
        client._prepare_messages_for_local_context(messages, ctx_len=1000, max_tokens=400)



def test_build_openrouter_kwargs_for_anthropic_keeps_require_parameters_only():
    from ouroboros.llm import LLMClient

    client = LLMClient()
    target = client._resolve_remote_target("anthropic/claude-opus-4.6")
    kwargs = client._build_remote_kwargs(
        target,
        [{"role": "user", "content": "hi"}],
        "medium",
        1000,
        "auto",
        None,
        None,
    )

    assert kwargs["extra_body"]["provider"] == {"require_parameters": True}
    assert "order" not in kwargs["extra_body"]["provider"]
    assert "allow_fallbacks" not in kwargs["extra_body"]["provider"]



def test_build_openrouter_kwargs_for_non_anthropic_has_no_provider_block():
    from ouroboros.llm import LLMClient

    client = LLMClient()
    target = client._resolve_remote_target("openai/gpt-4.1")
    kwargs = client._build_remote_kwargs(
        target,
        [{"role": "user", "content": "hi"}],
        "medium",
        1000,
        "auto",
        None,
        None,
    )

    assert "provider" not in kwargs["extra_body"]



def test_format_messages_for_safety_marks_omission():
    from ouroboros.safety import _format_messages_for_safety

    text = "X" * 700
    output = _format_messages_for_safety([
        {"role": "user", "content": text},
    ])

    assert "chars omitted" in output



def test_repo_commit_policy_is_skip():
    """Trusted reviewed-mutative built-ins must be marked skip, not recheck."""
    from ouroboros.safety import TOOL_POLICY, POLICY_SKIP

    assert TOOL_POLICY["commit_reviewed"] == POLICY_SKIP



def test_python_m_pytest_has_safe_shell_subject():
    from ouroboros.safety import _normalize_safe_shell_subject

    assert _normalize_safe_shell_subject(
        ["python3", "-m", "pytest", "tests/test_scope_review.py", "-q"]
    ) == "pytest"



def test_string_python_m_pytest_has_safe_shell_subject():
    from ouroboros.safety import _normalize_safe_shell_subject

    assert _normalize_safe_shell_subject(
        "python3 -m pytest tests/test_scope_review.py -q"
    ) == "pytest"



def test_json_array_string_python_m_pytest_has_safe_shell_subject():
    from ouroboros.safety import _normalize_safe_shell_subject

    assert _normalize_safe_shell_subject(
        '["python3", "-m", "pytest", "tests/test_scope_review.py", "-q"]'
    ) == "pytest"



def test_python_literal_list_string_pytest_has_safe_shell_subject():
    from ouroboros.safety import _normalize_safe_shell_subject

    assert _normalize_safe_shell_subject(
        "['python3', '-m', 'pytest', 'tests/test_scope_review.py', '-q']"
    ) == "pytest"



def test_python_inline_code_has_no_safe_shell_subject():
    from ouroboros.safety import _normalize_safe_shell_subject

    assert _normalize_safe_shell_subject(["python3", "-c", "print('hello')"]) == ""



def test_python_non_pytest_module_has_no_safe_shell_subject():
    from ouroboros.safety import _normalize_safe_shell_subject

    assert _normalize_safe_shell_subject(["python3", "-m", "pip", "list"]) == ""


@pytest.mark.parametrize(
    "cmd",
    [
        ["/tmp/git", "status"],
        ["/tmp/pytest", "-q"],
        ["./rg", "needle", "."],
    ],
)
def test_path_spoofed_safe_basenames_have_no_safe_shell_subject(cmd):
    from ouroboros.safety import _normalize_safe_shell_subject

    assert _normalize_safe_shell_subject(cmd) == ""



def test_python_named_wrapper_has_no_safe_shell_subject():
    from ouroboros.safety import _normalize_safe_shell_subject

    assert _normalize_safe_shell_subject(
        "/tmp/python-malicious -m pytest tests/test_scope_review.py -q"
    ) == ""


def test_prefill_target_never_refuses_a_prompt_the_window_can_hold():
    """The 12k prefill target is a SPEED preference, not a capacity limit.

    Live failure on a 128k-window local model, for the message "ты работаешь?":
    "local context window cannot hold the preserved core: 16980 chars > 12000
    budget (ctx_len=131072, max_tokens=32768)". That window holds ~147k chars, so
    12k is 8% of it.

    16,980 is not one huge section — it is the SUM OF FLOORS. Each preserved
    section stops shrinking at _LOCAL_SECTION_BODY_FLOOR, so once a prompt carries
    enough of them their floors alone exceed a 12k target while remaining far
    inside the real window. Compaction may still aim at the small target; only the
    window may refuse.
    """
    from ouroboros.llm import LLMClient, LocalContextTooLargeError

    client = LLMClient()
    # Enough preserved sections that their floors alone clear 12k.
    dynamic = "\n\n".join(
        f"## Runtime context {index}\n\n" + "Z" * 900 for index in range(100)
    )
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "SYSTEM\n\n## BIBLE.md\n\n" + "B" * 5000},
                {"type": "text", "text": "## Identity\n\n" + "I" * 5000},
                {"type": "text", "text": dynamic},
            ],
        },
        {"role": "user", "content": "ты работаешь?"},
    ]

    # The exact configuration that failed in production.
    compacted = client._prepare_messages_for_local_context(
        messages, ctx_len=131072, max_tokens=32768
    )
    sent = sum(
        len(block["text"])
        for message in compacted
        if message["role"] == "system"
        for block in message["content"]
    )
    assert sent > 12000, (
        "the fixture no longer reproduces the failure: it compacts under the prefill "
        f"target ({sent} chars), so it cannot prove the target stopped refusing"
    )

    # A window that genuinely cannot hold the floor-trimmed core still refuses, so
    # the typed signal the recovery path depends on is not weakened.
    with pytest.raises(LocalContextTooLargeError) as excinfo:
        client._prepare_messages_for_local_context(messages, ctx_len=100, max_tokens=50)
    assert "window" in str(excinfo.value)


def test_health_probe_does_not_erase_the_launched_context_length(monkeypatch):
    """The launch value is authoritative; a probe that learned nothing must not erase it.

    Live symptom: the server ran with `--n_ctx 65536`, but /api/local-model/status
    reported context_length 0, so llm.py fell back to a hardcoded 131072 and
    reserved 131072//4 = 32768 tokens for output — half the REAL window — while
    sizing the input budget against a window twice the true size.

    Cause: health_check reads `n_ctx_train` (the model's TRAINING window), which
    llama-cpp-python does not always expose. It returned 0, and the readiness path
    assigned that 0 over the value start() had just recorded. get_context_length()
    then never cached, because it only caches a positive value.

    This drives the REAL readiness path with a stubbed probe, not a copy of its logic.
    """
    from ouroboros.local_model import LocalModelManager

    manager = LocalModelManager.__new__(LocalModelManager)
    manager._context_length = 65536          # what start() recorded from --n_ctx
    manager._model_name = ""
    manager._status = "starting"
    manager._error = None
    manager._stderr_buf = b""
    manager._proc = type("P", (), {"poll": staticmethod(lambda: None)})()

    monkeypatch.setattr(
        manager, "health_check",
        lambda: {"ok": True, "context_length": 0, "model_name": "local.gguf"},
        raising=False,
    )
    manager._wait_for_healthy(timeout=5.0)
    assert manager._status == "ready"
    assert manager._context_length == 65536, "a silent probe erased the launched n_ctx"
    assert manager.get_context_length() == 65536

    # A probe that DID determine the window still wins.
    manager._status = "starting"
    monkeypatch.setattr(
        manager, "health_check",
        lambda: {"ok": True, "context_length": 32768, "model_name": "local.gguf"},
        raising=False,
    )
    manager._wait_for_healthy(timeout=5.0)
    assert manager._context_length == 32768


def test_dangling_reasoning_close_tag_does_not_leak_into_the_answer():
    """A closing </think> whose opener never arrived is still reasoning, not answer.

    Live output from a local GGUF model:

        Пользователь спрашивает: ... Я должен ответить тем же языком ...
        </think>Я вижу, что вы спрашиваете на русском языке — я отвечу по-русски.

    _extract only matched a PAIRED <think>...</think>, so an unmatched closer left
    both the tag and the whole reasoning block in the user-visible answer.
    """
    from ouroboros.llm import LLMClient

    raw = (
        "Пользователь спрашивает про проект.\n\n"
        "Я должен ответить по-русски.</think>"
        "Вот ответ."
    )
    body, reasoning = LLMClient._strip_reasoning_wrappers(raw)
    assert body == "Вот ответ."
    assert "</think>" not in body
    assert "Я должен ответить" in reasoning

    # A properly paired block keeps working, and prose after it survives.
    paired = "<think>скрытое рассуждение</think>Видимый ответ."
    body, reasoning = LLMClient._strip_reasoning_wrappers(paired)
    assert body == "Видимый ответ."
    assert reasoning == "скрытое рассуждение"

    # Text with no reasoning markup is untouched.
    plain = "Просто ответ без тегов."
    body, reasoning = LLMClient._strip_reasoning_wrappers(plain)
    assert body == plain and not reasoning

    # A tool-call payload is never touched: the split happens before it.
    with_tool = 'думаю вслух</think><tool_call>{"name": "read_file"}</tool_call>'
    body, _ = LLMClient._strip_reasoning_wrappers(with_tool)
    assert body.startswith("<tool_call>") and '"read_file"' in body
