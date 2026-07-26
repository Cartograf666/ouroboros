"""Regression tests: verify raised max_tokens / max_turns constants."""


def test_review_query_model_max_tokens():
    """review.py _query_model defaults to a 65536-token response reservation.
    v6.61.1: the literal moved into _review_output_budget (the
    OUROBOROS_REVIEW_MAX_TOKENS operator knob may LOWER it for mega-diff packs,
    floor 8192, never raise) — the un-overridden default must stay 65536."""
    import os
    from unittest import mock

    from ouroboros.tools.review import _review_output_budget

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OUROBOROS_REVIEW_MAX_TOKENS", None)
        assert _review_output_budget() == 65536


def test_project_naming_max_tokens_pinned():
    """v6.40 drift-guard (DEVELOPMENT #13): the LIGHT project-naming one-shot stays a TINY
    budget (256) — pinned so it can't silently drift up and matches the ARCHITECTURE
    'LLM output token budgets' table row."""
    import ast
    from pathlib import Path

    src = Path("ouroboros/project_naming.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = [
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword)
        and node.arg == "max_tokens"
        and isinstance(node.value, ast.Constant)
    ]
    assert 256 in found, f"Expected max_tokens=256 in project_naming.py, got {found}"


def test_scope_review_max_tokens():
    """scope_review.py _SCOPE_MAX_TOKENS must be ≥100000."""
    from ouroboros.tools.scope_review import _SCOPE_MAX_TOKENS
    assert _SCOPE_MAX_TOKENS >= 100_000


def test_llm_client_default_max_tokens():
    """Main remote chat defaults must leave enough output room for long tool plans."""
    import inspect
    from ouroboros.llm import LLMClient

    assert inspect.signature(LLMClient.chat).parameters["max_tokens"].default >= 65_536
    assert inspect.signature(LLMClient.chat_async).parameters["max_tokens"].default >= 65_536


def test_main_loop_explicit_max_tokens():
    """The task loop must pin the same 64K output budget even if client defaults move."""
    from ouroboros.loop_llm_call import MAIN_LOOP_MAX_TOKENS

    assert MAIN_LOOP_MAX_TOKENS >= 65_536


def test_vision_query_default_max_tokens():
    """VLM tools inherit the shared vision_query output budget."""
    import inspect
    from ouroboros.llm import LLMClient

    assert inspect.signature(LLMClient.vision_query).parameters["max_tokens"].default >= 32_768


def test_summary_and_background_token_budgets():
    """Summary/reflection/background paths must stay above the raised floors."""
    from pathlib import Path

    expectations = {
        "ouroboros/tools/review_synthesis.py": "max_tokens=16384",
        "ouroboros/consolidator.py": "max_tokens=16384",
        "ouroboros/reflection.py": "max_tokens=16384",
        "ouroboros/agent_task_pipeline.py": "max_tokens=16384",
        "ouroboros/context_compaction.py": "max_tokens=32768",
        "ouroboros/tools/skill_publish.py": "max_tokens=8192",
        "ouroboros/consciousness.py": "max_tokens=65536",
    }
    for path, needle in expectations.items():
        src = Path(path).read_text(encoding="utf-8").replace(" ", "")
        assert needle in src, f"{path} must contain {needle}"


def test_claude_code_edit_sdk_max_turns():
    """Edit and advisory paths must share the same default Claude Code turn budget (50)."""
    import ast
    from pathlib import Path

    # Verify the constant value via AST (works without claude_agent_sdk installed)
    gw_src = Path("ouroboros/gateways/claude_code.py").read_text(encoding="utf-8")
    tree = ast.parse(gw_src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "DEFAULT_CLAUDE_CODE_MAX_TURNS":
                    assert isinstance(node.value, ast.Constant) and node.value.value == 50, (
                        f"DEFAULT_CLAUDE_CODE_MAX_TURNS should be 50, got {getattr(node.value, 'value', '?')}"
                    )
                    found = True
    assert found, "DEFAULT_CLAUDE_CODE_MAX_TURNS not found in claude_code.py"

    # Verify callers reference the shared constant
    shell_src = Path("ouroboros/tools/shell.py").read_text(encoding="utf-8")
    advisory_src = Path("ouroboros/tools/claude_advisory_review.py").read_text(encoding="utf-8")
    assert "DEFAULT_CLAUDE_CODE_MAX_TURNS" in shell_src
    assert "DEFAULT_CLAUDE_CODE_MAX_TURNS" in advisory_src
    assert "max_turns=25" not in shell_src
    assert "max_turns=8" not in advisory_src


def test_claude_code_sdk_only_no_cli_fallback():
    """shell.py must not contain legacy CLI subprocess fallback."""
    src = open("ouroboros/tools/shell.py", encoding="utf-8").read()
    assert "_run_claude_cli" not in src, "CLI fallback function should be gone"
    assert "ensure_claude_cli" not in src, "CLI install function should be gone"


def test_review_prompt_token_budget_is_ssot():
    """``review_helpers.REVIEW_PROMPT_TOKEN_BUDGET`` is the single source of
    truth for the unified scope/plan/deep-review input gate (920K). Bumping
    the constant must move all three call sites in lockstep so the skip
    threshold cannot silently desync between modules.

    Note: Claude Opus 4.6 has a 1M context window SHARED between input and
    output. ``estimate_tokens`` (chars/4) is approximate, so the 920K gate
    intentionally leaves limited output headroom and remains best-effort.
    """
    from ouroboros.tools.review_helpers import REVIEW_PROMPT_TOKEN_BUDGET
    from ouroboros.tools.scope_review import _SCOPE_BUDGET_TOKEN_LIMIT

    assert REVIEW_PROMPT_TOKEN_BUDGET == 920_000, (
        f"REVIEW_PROMPT_TOKEN_BUDGET drifted to {REVIEW_PROMPT_TOKEN_BUDGET}; "
        "see review_helpers.py docstring before changing — call sites do "
        "not silently re-pin to an old budget."
    )
    assert _SCOPE_BUDGET_TOKEN_LIMIT == REVIEW_PROMPT_TOKEN_BUDGET, (
        f"_SCOPE_BUDGET_TOKEN_LIMIT ({_SCOPE_BUDGET_TOKEN_LIMIT}) must equal "
        f"the SSOT REVIEW_PROMPT_TOKEN_BUDGET ({REVIEW_PROMPT_TOKEN_BUDGET})."
    )
    # Plan review reserves output headroom inside the reviewer window (same class of
    # fix as scope review), but v6.80.0 replaced its module constants with PER-SLOT
    # calibrated caps: every slot cap must still stay inside the shared SSOT.
    from ouroboros.tools.plan_review import _PLAN_REVIEW_MAX_TOKENS
    from ouroboros.tools.review_synthesis import per_slot_input_token_limits

    limits = per_slot_input_token_limits(
        ["anthropic/claude-fable-5", "openai/gpt-5.6-sol"],
        context_window=1_000_000,
        output_reserve=_PLAN_REVIEW_MAX_TOKENS,
        tokenizer_margin=155_000,
    )
    assert limits and all(v <= REVIEW_PROMPT_TOKEN_BUDGET for v in limits.values())
    assert all(v > 0 for v in limits.values())
    assert all(v + _PLAN_REVIEW_MAX_TOKENS <= 1_000_000 for v in limits.values()), (
        "a per-slot plan input cap + reserved output exceeds the reviewer window; "
        "the provider would hard-400."
    )


def test_scope_input_budget_reserves_output_within_window():
    """Scope input cap + reserved output must fit the reviewer context window.

    Regression guard for the deterministic provider 400 where the 920K input gate
    plus the 100K output reservation exceeded the 1M window and fail-closed-blocked
    every commit. The assembled INPUT prompt is gated on ``_SCOPE_INPUT_TOKEN_LIMIT``,
    which must leave room for ``_SCOPE_MAX_TOKENS`` output inside
    ``_SCOPE_MODEL_CONTEXT_WINDOW`` while never exceeding the shared 920K SSOT.
    """
    from ouroboros.tools.scope_review import (
        _SCOPE_BUDGET_TOKEN_LIMIT,
        _SCOPE_INPUT_TOKEN_LIMIT,
        _SCOPE_MAX_TOKENS,
        _SCOPE_MODEL_CONTEXT_WINDOW,
        _SCOPE_OUTPUT_MARGIN_TOKENS,
    )

    assert _SCOPE_INPUT_TOKEN_LIMIT == 745_000
    assert _SCOPE_INPUT_TOKEN_LIMIT + _SCOPE_MAX_TOKENS <= _SCOPE_MODEL_CONTEXT_WINDOW, (
        f"scope input cap ({_SCOPE_INPUT_TOKEN_LIMIT}) + reserved output "
        f"({_SCOPE_MAX_TOKENS}) exceeds the {_SCOPE_MODEL_CONTEXT_WINDOW}-token "
        "reviewer window; the provider would hard-400 and fail closed."
    )
    assert _SCOPE_INPUT_TOKEN_LIMIT + _SCOPE_MAX_TOKENS + _SCOPE_OUTPUT_MARGIN_TOKENS <= _SCOPE_MODEL_CONTEXT_WINDOW, (
        "scope input cap must leave both output reservation and tokenizer-underestimate headroom."
    )
    assert _SCOPE_OUTPUT_MARGIN_TOKENS >= 150_000, (
        "scope review needs a large tokenizer headroom margin for atlas-heavy prompts."
    )
    assert _SCOPE_INPUT_TOKEN_LIMIT <= _SCOPE_BUDGET_TOKEN_LIMIT, (
        "scope input cap must not exceed the shared prompt-size SSOT."
    )


def test_scope_input_limit_is_density_calibrated(monkeypatch, tmp_path):
    """Scope reviewers get a MEASURED-density-calibrated input cap (v6.80.0).

    Regression guard for the deterministic 400 where a 739,508-estimated-token scope
    pack measured 1,166,914 REAL tokens on the Claude tokenizer (~1.58x the chars/4
    estimate on code) and was rejected by every fable-5 upstream as "prompt is too
    long: > 1,000,000 maximum". The hand-set family constant is gone; the cap must
    still keep estimated*density + output inside the 1M window, must never exceed the
    shared SSOT, and must never be LOOSER than the historical absolute-margin cap.
    """
    from ouroboros.capability_evidence import COLD_START_TOKEN_DENSITY
    from ouroboros.tools.scope_review import (
        _SCOPE_BUDGET_TOKEN_LIMIT,
        _SCOPE_INPUT_TOKEN_LIMIT,
        _SCOPE_MAX_TOKENS,
        _SCOPE_MODEL_CONTEXT_WINDOW,
        _effective_scope_input_limit,
    )

    # This test verifies the CALIBRATION at a 1M window, not the window-resolution
    # policy: since the v6.46.0 false-1M fix an off-default model with no Capability
    # Evidence fail-closes to the sub-floor.
    monkeypatch.setattr("ouroboros.tools.scope_review._scope_reviewer_window", lambda m: 1_000_000)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))

    assert COLD_START_TOKEN_DENSITY >= 1.58, (
        "the conservative cold-start density must cover the measured 1.58x Claude code density"
    )
    cap = _effective_scope_input_limit(scope_model="anthropic/claude-fable-5")
    assert int(cap * COLD_START_TOKEN_DENSITY) + _SCOPE_MAX_TOKENS <= _SCOPE_MODEL_CONTEXT_WINDOW, (
        "calibrated cap * real-token density + output reserve must fit the 1M window"
    )
    assert cap <= _SCOPE_BUDGET_TOKEN_LIMIT
    # Every spelling of the shipped reviewer resolves to the same calibrated cap, and
    # the historical absolute-margin cap remains an upper bound for ANY model.
    for spelling in ("anthropic::claude-fable-5", "fable-5", "~mythos-5", "openai/gpt-5.5"):
        assert _effective_scope_input_limit(scope_model=spelling) <= _SCOPE_INPUT_TOKEN_LIMIT


def test_calibrated_input_limit_shared_helper(tmp_path, monkeypatch):
    """The shared helper is the calibration SSOT for scope, triad, plan AND deep review.

    A MEASURED observation must actually reach it (the old import-time constant froze
    the pre-measurement value for the whole process), and seeding an observation that
    reproduces the legacy effective density must reproduce the legacy limit BYTE-FOR-BYTE.
    """
    import inspect

    from ouroboros import deep_self_review
    from ouroboros.capability_evidence import (
        COLD_START_TOKEN_DENSITY,
        MEASURED_DENSITY_SAFETY_FACTOR,
        record_token_density,
    )
    from ouroboros.tools.review_helpers import calibrated_input_token_limit

    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))

    def limit(model):
        return calibrated_input_token_limit(
            model, context_window=1_000_000, output_reserve=100_000,
            tokenizer_margin=155_000, drive_root=tmp_path,
        )

    # Cold start: the conservative density applies to EVERY model, so the cap is at or
    # below the historical GPT-family 745K and never above it.
    assert limit("openai/gpt-5.5") == int(900_000 / COLD_START_TOKEN_DENSITY)
    assert limit("anthropic/claude-fable-5") == int(900_000 / COLD_START_TOKEN_DENSITY)
    assert limit("openai/gpt-5.5") <= 745_000

    # Seed a measurement reproducing the LEGACY effective density (1.65) and the
    # legacy Claude limit comes back byte-identical.
    legacy_density = 1.65 / MEASURED_DENSITY_SAFETY_FACTOR
    chars = 4_000_000
    record_token_density(
        tmp_path, "anthropic/claude-fable-5",
        prompt_chars=chars, prompt_tokens=int(legacy_density * chars / 4),
    )
    assert limit("anthropic/claude-fable-5") == int(900_000 / 1.65)

    # A model with a genuinely LIGHTER tokenizer keeps its OWN measured density: the
    # cold-start constant is a Claude-derived cold-path value, not a global floor that
    # permanently shrinks every other model's pack. The historical absolute-margin form
    # still bounds the result, so the cap never exceeds the pre-measurement one.
    record_token_density(
        tmp_path, "openai/gpt-5.5", prompt_chars=chars, prompt_tokens=int(1.0 * chars / 4),
    )
    assert limit("openai/gpt-5.5") == 1_000_000 - 100_000 - 155_000 == 745_000

    # Deep self-review consumes the same helper for its model-aware gate.
    assert "calibrated_input_token_limit" in inspect.getsource(deep_self_review.run_deep_self_review)
    # ...as does the triad, whose pack size moves with the same formula.
    from ouroboros.tools import review as triad
    assert "calibrated_input_token_limit" in inspect.getsource(triad)


def test_measured_density_never_loosens_a_models_own_review_pack_cap(tmp_path, monkeypatch):
    """Measurement may only ever TIGHTEN a given model's cap — PER MODEL IDENTITY.

    One normalized identity accumulates observations from every surface (the shipped
    `claude-fable-5` is both the scope reviewer and a triad slot), so a run of doc-only
    commits whose prose-dominated packs measure ~1.1 must not pull a code-heavy model's
    stored density back down and hand the NEXT code-heavy scope pack a bigger cap — the
    deterministic 400 this calibration prevents. The guarantee lives in the STORE (a
    running per-model maximum), not in a global Claude-derived floor: flooring every
    model at 1.65 permanently shrank the pack of every lighter tokenizer instead.
    """
    from ouroboros.capability_evidence import (
        _DENSITY_MEMO,
        record_token_density,
        resolve_token_density,
    )
    from ouroboros.tools.review_helpers import calibrated_input_token_limit

    _DENSITY_MEMO.clear()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))

    def limit(model):
        return calibrated_input_token_limit(
            model, context_window=1_000_000, output_reserve=100_000,
            tokenizer_margin=155_000, drive_root=tmp_path,
        )

    cold = limit("anthropic/claude-fable-5")
    chars = 4_000_000

    # A real code-heavy pack measures 1.58 on this identity: the cap TIGHTENS.
    record_token_density(
        tmp_path, "anthropic/claude-fable-5",
        prompt_chars=chars, prompt_tokens=int(1.58 * chars / 4),
    )
    density, source = resolve_token_density(tmp_path, "anthropic/claude-fable-5")
    assert source == "measured"
    tightened = limit("anthropic/claude-fable-5")
    assert tightened <= cold

    # A later run of prose-dominated packs measures ~1.1 on the SAME identity: the
    # stored density is a running maximum, so the cap does NOT loosen back.
    _DENSITY_MEMO.clear()
    record_token_density(
        tmp_path, "anthropic/claude-fable-5",
        prompt_chars=chars, prompt_tokens=int(1.1 * chars / 4),
    )
    assert resolve_token_density(tmp_path, "anthropic/claude-fable-5")[0] == density
    assert limit("anthropic/claude-fable-5") == tightened

    # A measured DENSER value still tightens further (the direction that prevents 400s).
    _DENSITY_MEMO.clear()
    record_token_density(
        tmp_path, "anthropic/claude-fable-5",
        prompt_chars=chars, prompt_tokens=int(2.0 * chars / 4),
    )
    assert limit("anthropic/claude-fable-5") < tightened


def test_scope_normalize_defaults_pass_severity_only():
    """PASS rows may omit severity (semantically void there); FAIL rows must
    carry an explicit valid severity because it decides blocking (fail-closed)."""
    from ouroboros.tools.scope_review import _SCOPE_REQUIRED_ITEMS, _normalize_scope_items

    items = []
    required = sorted(_SCOPE_REQUIRED_ITEMS)
    for idx, item_id in enumerate(required):
        if idx == 0:
            items.append({"item": item_id, "verdict": "FAIL", "severity": "advisory",
                          "reason": "a concrete finding with enough words"})
        else:
            # PASS rows WITHOUT severity — the fable-5 output shape.
            items.append({"item": item_id, "verdict": "PASS",
                          "reason": "verified against the staged diff and context"})
    normalized, err = _normalize_scope_items(items)
    assert err == "", f"PASS rows without severity must normalize cleanly: {err}"
    assert len(normalized) == len(required)

    # A FAIL row without severity stays invalid (fail-closed).
    bad = list(items)
    bad[0] = {"item": required[0], "verdict": "FAIL", "reason": "a concrete finding with enough words"}
    _normalized, err = _normalize_scope_items(bad)
    assert "missing or invalid severity" in err


def test_scope_actor_record_surfaces_error_text():
    """A non-responded scope actor record must carry the failure text so a
    provider 400 is visible in the verdict without observability digging."""
    from ouroboros.tools.review_helpers import build_scope_actor_record
    from ouroboros.tools.scope_review import ScopeReviewResult

    failed = ScopeReviewResult(
        blocked=True,
        block_message="SCOPE_REVIEW_BLOCKED: Error code: 400 - prompt is too long",
        model_id="anthropic/claude-fable-5",
        status="error",
    )
    record = build_scope_actor_record(failed, slot_id="scope_slot_1")
    assert "400" in record["error"]
    ok = ScopeReviewResult(model_id="m", status="responded", raw_text="[]")
    assert build_scope_actor_record(ok, slot_id="s")["error"] == ""


def test_deep_self_review_budget_uses_ssot():
    """``deep_self_review`` must gate the FULL assembled prompt (system + user)
    on an input limit derived from the SSOT constant WITH output reservation
    (min(SSOT, window − output − margin)) — matching scope_review/plan_review —
    using the shared ``estimate_tokens(chars/4)`` helper.
    """
    import pathlib
    src = pathlib.Path("ouroboros/deep_self_review.py").read_text(encoding="utf-8")
    assert "REVIEW_PROMPT_TOKEN_BUDGET" in src, (
        "deep_self_review must derive its gate from the SSOT constant"
    )
    assert "estimated_tokens > input_limit" in src, (
        "deep_self_review must gate on the model-calibrated output-reserving input limit"
    )
    assert "calibrated_input_token_limit(" in src, (
        "deep_self_review must resolve its gate through the shared model-family calibration helper"
    )
    assert "estimate_tokens(_SYSTEM_PROMPT + pack_text)" in src, (
        "deep_self_review must gate on the FULL assembled prompt "
        "(system + user) using the shared estimate_tokens(chars/4) helper."
    )
    # Old hardcoded literals must not survive — drift would silently desync.
    assert "estimated_tokens > 850_000" not in src, (
        "deep_self_review still has the old hardcoded literal; switch to the SSOT constant"
    )
    assert "estimated_tokens > 920_000" not in src, (
        "deep_self_review hardcodes the current budget; use the SSOT constant instead"
    )
    assert "int(stats[\"total_chars\"] / 3.5)" not in src, (
        "deep_self_review must not use its old chars/3.5 estimator"
    )

    from ouroboros.deep_self_review import (
        _DEEP_INPUT_TOKEN_LIMIT,
        _DEEP_MAX_OUTPUT_TOKENS,
        _DEEP_MODEL_CONTEXT_WINDOW,
        _DEEP_OUTPUT_MARGIN_TOKENS,
    )
    from ouroboros.tools.review_helpers import REVIEW_PROMPT_TOKEN_BUDGET

    assert _DEEP_INPUT_TOKEN_LIMIT == min(
        REVIEW_PROMPT_TOKEN_BUDGET,
        _DEEP_MODEL_CONTEXT_WINDOW - _DEEP_MAX_OUTPUT_TOKENS - _DEEP_OUTPUT_MARGIN_TOKENS,
    )
    assert _DEEP_INPUT_TOKEN_LIMIT + _DEEP_MAX_OUTPUT_TOKENS <= _DEEP_MODEL_CONTEXT_WINDOW, (
        "deep review input cap + reserved output exceeds the reviewer window; "
        "the provider would hard-400."
    )


def test_tool_timeout_uses_max_of_settings_and_per_tool():
    """_get_tool_timeout must return max(settings, per_tool) not just settings."""
    from unittest.mock import patch
    import ouroboros.loop_tool_execution as mod

    class FakeTools:
        def get_timeout(self, name):
            return 1200  # per-tool declares 1200s

    # settings says 600, per-tool says 1200 → should return 1200
    with patch.object(mod, "load_settings", return_value={"OUROBOROS_TOOL_TIMEOUT_SEC": 600}):
        result = mod._get_tool_timeout(FakeTools(), "claude_code_edit")
    assert result == 1200, f"Expected 1200 (per-tool), got {result}"


def test_tool_timeout_settings_wins_when_higher():
    """_get_tool_timeout: if settings > per_tool, settings wins."""
    from unittest.mock import patch
    import ouroboros.loop_tool_execution as mod

    class FakeTools:
        def get_timeout(self, name):
            return 360  # default per-tool

    with patch.object(mod, "load_settings", return_value={"OUROBOROS_TOOL_TIMEOUT_SEC": 900}):
        result = mod._get_tool_timeout(FakeTools(), "run_command")
    assert result == 900, f"Expected 900 (settings), got {result}"


def test_review_evidence_no_truncation_by_default():
    """format_review_evidence_for_prompt must NOT truncate by default (max_chars=0)."""
    from ouroboros.review_evidence import format_review_evidence_for_prompt
    big = {"has_evidence": True, "data": "x" * 10000}
    result = format_review_evidence_for_prompt(big)
    assert "truncated" not in result.lower()
    assert len(result) > 10000


def test_review_evidence_bounded_with_omission_note():
    """format_review_evidence_for_prompt truncates with explicit omission note when max_chars>0."""
    from ouroboros.review_evidence import format_review_evidence_for_prompt
    big = {"has_evidence": True, "data": "x" * 10000}
    result = format_review_evidence_for_prompt(big, max_chars=500)
    assert "OMISSION NOTE" in result
    assert "truncated at 500 chars" in result


def test_review_evidence_no_obligation_cap():
    """collect_review_evidence default max_obligations must be None (no cap)."""
    import inspect
    from ouroboros.review_evidence import collect_review_evidence
    sig = inspect.signature(collect_review_evidence)
    default = sig.parameters["max_obligations"].default
    assert default is None, f"Expected None, got {default}"


def test_run_script_timeout_360():
    """run_script ToolEntry must stay foreground-bounded like run_command."""
    from ouroboros.tools.shell import get_tools
    entries = get_tools()
    rs = [e for e in entries if e.name == "run_script"]
    assert rs, "run_script not found in shell.get_tools()"
    assert rs[0].timeout_sec == 360


def test_advisory_pre_review_timeout_1200():
    """advisory_pre_review ToolEntry must declare timeout_sec=1200."""
    from ouroboros.tools.claude_advisory_review import get_tools
    entries = get_tools()
    apr = [e for e in entries if e.name == "advisory_review"]
    assert apr, "advisory_pre_review not found"
    assert apr[0].timeout_sec == 1200


def test_full_repo_pack_excludes_junk_dirs():
    """build_full_repo_pack must skip broad non-core directories."""
    from ouroboros.tools.review_helpers import _FULL_REPO_SKIP_DIR_PREFIXES
    for prefix in ("assets/", "tests/", "devtools/"):
        assert prefix in _FULL_REPO_SKIP_DIR_PREFIXES, f"{prefix} not in skip list"


def test_summary_and_reflection_callers_use_bounded_evidence():
    """Summary and reflection prompt builders must call format_review_evidence_for_prompt with max_chars."""
    from pathlib import Path

    for filename in ("ouroboros/agent_task_pipeline.py", "ouroboros/reflection.py"):
        src = Path(filename).read_text(encoding="utf-8")
        assert "format_review_evidence_for_prompt(" in src
        # Must pass max_chars argument (not rely on default 0)
        assert "max_chars=" in src, f"{filename} must call format_review_evidence_for_prompt with max_chars"


def test_obligation_context_shows_all():
    """build_review_context must not slice open_obligations."""
    src = open("ouroboros/agent_task_pipeline.py", encoding="utf-8").read()
    assert "open_obs[:4]" not in src, "open_obs[:4] cap should be removed"
    assert "obligation_ids[:4]" not in src, "obligation_ids[:4] cap should be removed"
