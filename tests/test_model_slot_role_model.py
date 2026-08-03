"""F1 (v6.39): model-slot role-model + 429-aware fallback chain + cooldown.

Covers the empty->Main accessors, the new comma-separated fallback chain
(dedup / drop-active / benchmark no-op / legacy-singular env), the stored-key
rename migration, the process-local cooldown, and the subagent lane resolver
(mutating-child -> heavy, read-only -> light, explicit honored, depth-cap note).
"""

from __future__ import annotations

import json
import pathlib

import pytest

import ouroboros.config as config
from ouroboros import fallback_cooldown as fcd
from ouroboros import subagents


# ---------------------------------------------------------------- accessors

def test_heavy_and_light_empty_fall_back_to_main(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main-x")
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "")
    assert config.get_heavy_model() == "provider::main-x"
    assert config.get_light_model() == "provider::main-x"


def test_heavy_and_light_explicit_values_are_honored(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main-x")
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "provider::strong")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "provider::cheap")
    assert config.get_heavy_model() == "provider::strong"
    assert config.get_light_model() == "provider::cheap"


# ----------------------------------------------------------- fallback chain

def test_fallback_chain_dedups_and_drops_active(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "a, b , a, c")
    monkeypatch.delenv("OUROBOROS_MODEL_FALLBACK", raising=False)
    assert config.get_fallback_models("b") == ["a", "c"]
    # No active model -> full deduped chain in order.
    assert config.get_fallback_models("") == ["a", "b", "c"]


def test_fallback_chain_benchmark_dedupes_to_no_op(monkeypatch):
    # Benchmark sets every slot to one model; the active model is dropped, so the
    # chain collapses to empty -> no cross-model fallback happens.
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "same::model")
    monkeypatch.delenv("OUROBOROS_MODEL_FALLBACK", raising=False)
    assert config.get_fallback_models("same::model") == []


def test_fallback_chain_reads_legacy_singular_env(monkeypatch):
    monkeypatch.delenv("OUROBOROS_MODEL_FALLBACKS", raising=False)
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACK", "legacy::single")
    assert config.get_fallback_models("primary") == ["legacy::single"]


def test_fallback_chain_empty_means_no_fallback(monkeypatch):
    # An explicitly empty/unset Fallbacks slot must NOT silently fall back to the shipped
    # Anthropic default (which would cross an OpenAI-compatible/local owner into an
    # unconfigured provider). The default reaches a default install via the env instead.
    monkeypatch.delenv("OUROBOROS_MODEL_FALLBACKS", raising=False)
    monkeypatch.delenv("OUROBOROS_MODEL_FALLBACK", raising=False)
    assert config.get_fallback_models("primary") == []


def test_advisory_fallback_model_uses_main_when_light_empty(monkeypatch):
    from ouroboros.tools.claude_advisory_review import _resolve_fallback_model
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main-x")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "")
    # Empty Light must resolve to Main, never "" (which would call chat with no model id).
    assert _resolve_fallback_model() == "provider::main-x"


def test_parse_fallback_chain_ssot(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "a, b , a")
    monkeypatch.delenv("OUROBOROS_MODEL_FALLBACK", raising=False)
    # Raw chain: parsed, whitespace-trimmed, NO dedup, NO active-drop (those belong to
    # get_fallback_models on top).
    assert config.parse_fallback_chain() == ["a", "b", "a"]
    monkeypatch.delenv("OUROBOROS_MODEL_FALLBACKS", raising=False)
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACK", "legacy")
    assert config.parse_fallback_chain() == ["legacy"]


def test_infer_model_category_recognizes_chain_link(monkeypatch):
    from ouroboros.pricing import infer_model_category
    monkeypatch.setenv("OUROBOROS_MODEL", "main/x")
    monkeypatch.delenv("OUROBOROS_MODEL_HEAVY", raising=False)
    monkeypatch.delenv("OUROBOROS_MODEL_LIGHT", raising=False)
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "fb/one, fb/two")
    monkeypatch.delenv("OUROBOROS_MODEL_FALLBACK", raising=False)
    # A model that is a LINK of the chain is categorized "fallback", not "other".
    assert infer_model_category("fb/two") == "fallback"
    assert infer_model_category("main/x") == "main"
    assert infer_model_category("unrelated/z") == "other"


# -------------------------------------------------------- stored migration

def test_stored_slot_keys_migrate_on_load(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({
        "OUROBOROS_MODEL": "provider::main",
        "OUROBOROS_MODEL_CODE": "provider::legacy-code",
        "USE_LOCAL_CODE": True,
        "OUROBOROS_MODEL_FALLBACK": "provider::legacy-fb",
    }), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_PATH", pathlib.Path(settings_file))
    for key in ("OUROBOROS_MODEL_HEAVY", "USE_LOCAL_HEAVY", "OUROBOROS_MODEL_FALLBACKS",
                "OUROBOROS_MODEL_CODE", "USE_LOCAL_CODE", "OUROBOROS_MODEL_FALLBACK"):
        monkeypatch.delenv(key, raising=False)

    loaded = config.load_settings()

    assert loaded.get("OUROBOROS_MODEL_HEAVY") == "provider::legacy-code"
    assert loaded.get("USE_LOCAL_HEAVY") is True
    assert loaded.get("OUROBOROS_MODEL_FALLBACKS") == "provider::legacy-fb"
    # Legacy keys are dropped, not left to linger.
    assert "OUROBOROS_MODEL_CODE" not in loaded
    assert "USE_LOCAL_CODE" not in loaded
    assert "OUROBOROS_MODEL_FALLBACK" not in loaded


def test_migrate_legacy_slot_keys_ssot():
    # The shared SSOT helper preserves a stored value, drops the legacy key, and never
    # clobbers an already-set new key.
    s = {"OUROBOROS_MODEL_CODE": "x", "USE_LOCAL_CODE": True, "OUROBOROS_MODEL_FALLBACK": "y"}
    config.migrate_legacy_slot_keys(s)
    assert s == {"OUROBOROS_MODEL_HEAVY": "x", "USE_LOCAL_HEAVY": True, "OUROBOROS_MODEL_FALLBACKS": "y"}
    # An already-set new key wins; the legacy key is still dropped.
    s2 = {"OUROBOROS_MODEL_CODE": "old", "OUROBOROS_MODEL_HEAVY": "new"}
    config.migrate_legacy_slot_keys(s2)
    assert s2 == {"OUROBOROS_MODEL_HEAVY": "new"}


def test_colab_settings_migrate_legacy_drive_keys():
    # A Colab re-run with legacy Drive settings.json must keep the owner's prior
    # code/heavy + fallback customizations (not silently drop them).
    from ouroboros.colab_bootstrap import build_colab_settings
    existing = {
        "OUROBOROS_MODEL": "openai::gpt-5.5",
        "OUROBOROS_MODEL_CODE": "openai::gpt-5.5-custom-heavy",
        "OUROBOROS_MODEL_FALLBACK": "openai::gpt-5.5-mini",
        "OPENAI_API_KEY": "sk-openai-existing",
    }
    out = build_colab_settings({}, models=None, existing=existing)
    assert out.get("OUROBOROS_MODEL_HEAVY") == "openai::gpt-5.5-custom-heavy"
    assert out.get("OUROBOROS_MODEL_FALLBACKS") == "openai::gpt-5.5-mini"
    assert "OUROBOROS_MODEL_CODE" not in out
    assert "OUROBOROS_MODEL_FALLBACK" not in out


# ---------------------------------------------------------------- cooldown

def test_cooldown_marks_and_heals(monkeypatch):
    fcd.reset_for_tests()
    monkeypatch.delenv("OUROBOROS_FALLBACK_COOLDOWN_ENABLED", raising=False)
    monkeypatch.setenv("OUROBOROS_FALLBACK_COOLDOWN_SEC", "120")
    assert fcd.is_cooling_down("m1") is False
    fcd.mark_cooldown("m1")
    assert fcd.is_cooling_down("m1") is True
    # A zero-length window heals immediately on the next read (passive heal).
    monkeypatch.setenv("OUROBOROS_FALLBACK_COOLDOWN_SEC", "0")
    fcd.mark_cooldown("m2")
    assert fcd.is_cooling_down("m2") is False


def test_cooldown_disabled_is_noop(monkeypatch):
    fcd.reset_for_tests()
    monkeypatch.setenv("OUROBOROS_FALLBACK_COOLDOWN_ENABLED", "false")
    fcd.mark_cooldown("m1")
    assert fcd.is_cooling_down("m1") is False


def test_cooldown_local_and_remote_are_distinct(monkeypatch):
    fcd.reset_for_tests()
    monkeypatch.delenv("OUROBOROS_FALLBACK_COOLDOWN_ENABLED", raising=False)
    monkeypatch.setenv("OUROBOROS_FALLBACK_COOLDOWN_SEC", "120")
    fcd.mark_cooldown("m1", use_local=True)
    assert fcd.is_cooling_down("m1", use_local=True) is True
    assert fcd.is_cooling_down("m1", use_local=False) is False


def test_attempts_per_model_is_bounded(monkeypatch):
    monkeypatch.setenv("OUROBOROS_FALLBACK_ATTEMPTS_PER_MODEL", "9")
    assert fcd.attempts_per_model() == 2
    monkeypatch.setenv("OUROBOROS_FALLBACK_ATTEMPTS_PER_MODEL", "0")
    assert fcd.attempts_per_model() == 1
    monkeypatch.setenv("OUROBOROS_FALLBACK_ATTEMPTS_PER_MODEL", "nonsense")
    assert fcd.attempts_per_model() == 1


# ------------------------------------------------------------ lane resolver

def test_omitted_lane_routes_to_light_whatever_the_child_may_do(monkeypatch):
    """Omission is a decision: an unspecified lane is Light, and the child's write
    authority does not enter the choice. This is the v6.87.7 decoupling — before it,
    `auto` read `write_surface OR may_mutate` and handed a read-only child the
    expensive model because of a permission about its grandchildren."""
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main")
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "provider::strong")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "provider::cheap")
    res = subagents.resolve_subagent_lane("auto", depth=1)
    assert res.effective_lane == "light"
    assert res.model == "provider::cheap"


def test_resolver_takes_no_authority_argument():
    """The resolver must not regrow an authority input. If a future change adds one,
    this fails and the reviewer sees the coupling coming back."""
    import inspect

    params = set(inspect.signature(subagents.resolve_subagent_lane).parameters)
    assert "mutating" not in params
    assert not (params & {"mutating", "may_mutate", "write_surface", "surface"})
    assert "requested_lane" in params
    # Deliberately NOT an equality assertion. The remaining slot_index/slot_count/depth
    # parameters feed a multi-slot fan-out no lane can trigger any more; pinning the exact
    # signature here would mean a future cleanup has to delete a test in order to delete
    # dead code. What this test is FOR is the absence of an authority input.


def test_explicit_lane_is_honored_at_any_depth(monkeypatch):
    """Depth bounds how DEEP delegation goes, never how strong a descendant is."""
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main")
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "provider::strong")
    for depth in (1, 2, 5):
        res = subagents.resolve_subagent_lane("heavy", depth=depth)
        assert res.effective_lane == "heavy", depth
        assert res.model == "provider::strong", depth
        assert res.downgrade_note == "", depth


def test_explicit_main_honored(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main")
    res = subagents.resolve_subagent_lane("main", depth=1)
    assert res.effective_lane == "main"
    assert res.downgrade_note == ""


def test_code_lane_is_rejected_no_legacy_alias():
    with pytest.raises(ValueError):
        subagents.normalize_subagent_model_lane("code")


def test_build_envelope_tolerates_legacy_stored_lane():
    # The PUBLIC schema rejects "code", but an envelope built from an already-ran task's
    # durable record (which may carry a pre-v6.39 "code" lane) must NOT crash — it coerces
    # the unknown stored lane to a safe default (not a "code"->"heavy" alias).
    env = subagents.build_subagent_envelope(
        task_id="t1", parent_task_id="p1", root_task_id="r1", task_group_id="",
        depth=1, role="builder", requested_lane="code", effective_lane="code",
        model="m", status="completed", usage={},
    )
    assert env["requested_lane"] == "auto"
    assert env["effective_lane"] == "light"


def test_string_false_may_mutate_stays_falsey(monkeypatch):
    # A tool-call payload may carry may_mutate as the STRING "false"; the SSOT
    # normalize_bool must treat it as falsey (regression: bool("false") was truthy).
    # Since v6.87.7 may_mutate governs AUTHORITY only — it no longer reaches the lane
    # resolver at all — so this pins the primitive rather than a routing side effect.
    from ouroboros.contracts.task_contract import normalize_bool
    assert normalize_bool("false") is False
    assert normalize_bool("true") is True


def test_use_local_empty_heavy_follows_main_flag(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main")
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "")
    monkeypatch.setenv("USE_LOCAL_MAIN", "true")
    monkeypatch.delenv("USE_LOCAL_HEAVY", raising=False)
    res = subagents.resolve_subagent_lane("heavy", depth=1)
    assert res.effective_lane == "heavy"
    assert res.model == "provider::main"
    # Empty Heavy -> Main, so the Main local flag governs (not silently ignored).
    assert res.use_local_model is True


# ------------------------------------------------- cooldown trigger SSOT (C1)

def test_cooldown_error_kinds_include_rate_limit_but_not_in_retry_kinds():
    from ouroboros.loop_llm_call import _COOLDOWN_ERROR_KINDS, _TRANSIENT_RETRY_KINDS
    # A body-error 429 is classified "rate_limit" -> it MUST trigger cooldown.
    assert "rate_limit" in _COOLDOWN_ERROR_KINDS
    assert _TRANSIENT_RETRY_KINDS <= _COOLDOWN_ERROR_KINDS
    # ...but the same-model transient-retry budget must NOT be widened by it.
    assert "rate_limit" not in _TRANSIENT_RETRY_KINDS


# ------------------------------------ credentialed-model resolver parses chain (C2)

def test_resolve_credentialed_model_parses_fallbacks_chain(monkeypatch):
    from ouroboros.provider_models import resolve_credentialed_model
    # Only OpenRouter is credentialed in this environment.
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    for k in ("GIGACHAT_CREDENTIALS", "GIGACHAT_USER", "GIGACHAT_PASSWORD",
              "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLOUDRU_FOUNDATION_MODELS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OUROBOROS_MODEL", "gigachat::GigaChat")  # uncredentialed
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "")
    # First chain entry uncredentialed (gigachat), second routes via OpenRouter.
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "gigachat::nocreds, anthropic/claude-sonnet-4.6")
    # The resolver must parse the chain and return the credentialed SECOND entry — not
    # test the raw comma-string as one (broken) model id, nor skip past it.
    assert resolve_credentialed_model("gigachat::GigaChat") == "anthropic/claude-sonnet-4.6"


def test_empty_light_slot_inherits_main_routing_even_when_models_match(monkeypatch):
    """ENV PRESENCE decides the inherit-from-Main case, not string equality. A
    local-only install whose Main happens to equal the shipped Light default must
    still route the Light lane locally — it is running Main, not the Light slot."""
    from ouroboros.config import SETTINGS_DEFAULTS
    from ouroboros.subagents import _use_local_for_lane

    shared = SETTINGS_DEFAULTS["OUROBOROS_MODEL_LIGHT"]
    monkeypatch.setenv("OUROBOROS_MODEL", shared)
    monkeypatch.delenv("OUROBOROS_MODEL_LIGHT", raising=False)
    monkeypatch.setenv("USE_LOCAL_MAIN", "true")
    monkeypatch.delenv("USE_LOCAL_LIGHT", raising=False)

    assert _use_local_for_lane("light", shared) is True
    # A slot the owner really configured still governs itself.
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", shared)
    assert _use_local_for_lane("light", shared) is False


def _scheduling_ctx(tmp_path, *, parent_deadline: str = ""):
    import queue

    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "parent1"
    ctx.task_depth = 0
    ctx.current_chat_id = 1
    ctx.event_queue = queue.Queue()
    ctx.task_metadata = {"root_task_id": "root1", "session_id": "sess1"}
    if parent_deadline:
        ctx.task_metadata["task_contract"] = {"deadline_at": parent_deadline}
    return ctx


def test_executor_is_a_third_axis_independent_of_lane_and_surface(tmp_path):
    """WHO runs a child is its own axis. It is a closed enum of intents — never a harness
    name — so that adding a harness never touches this contract."""
    from ouroboros.subagents import SUBAGENT_EXECUTORS
    from ouroboros.tools.control import _schedule_task

    assert SUBAGENT_EXECUTORS == ("auto", "harness", "native")

    for executor in SUBAGENT_EXECUTORS:
        ctx = _scheduling_ctx(tmp_path / executor)
        out = _schedule_task(ctx, objective="o", expected_output="e", executor=executor)
        assert "TOOL_ARG_ERROR" not in out, executor
        assert ctx.event_queue.get_nowait()["requested_executor"] == executor

    ctx = _scheduling_ctx(tmp_path / "omitted")
    _schedule_task(ctx, objective="o", expected_output="e")
    assert ctx.event_queue.get_nowait()["requested_executor"] == "auto"

    ctx = _scheduling_ctx(tmp_path / "bad")
    out = _schedule_task(ctx, objective="o", expected_output="e", executor="codex")
    assert "TOOL_ARG_ERROR" in out and "executor must be one of" in out
    assert ctx.event_queue.empty()


def test_effort_is_optional_and_validated_against_the_scale(tmp_path):
    """A parent may name one child's effort; omission inherits the configured effort for the
    task type, which stays the normal case. Nothing clamps effort by depth."""
    from ouroboros.tools.control import _schedule_task

    ctx = _scheduling_ctx(tmp_path / "named")
    out = _schedule_task(ctx, objective="o", expected_output="e", effort="xhigh")
    assert "TOOL_ARG_ERROR" not in out
    assert ctx.event_queue.get_nowait()["reasoning_effort"] == "xhigh"

    ctx = _scheduling_ctx(tmp_path / "omitted")
    _schedule_task(ctx, objective="o", expected_output="e")
    assert ctx.event_queue.get_nowait()["reasoning_effort"] == ""

    ctx = _scheduling_ctx(tmp_path / "bad")
    out = _schedule_task(ctx, objective="o", expected_output="e", effort="ludicrous")
    assert "TOOL_ARG_ERROR" in out and "effort must be one of" in out
    assert ctx.event_queue.empty()


def _enqueue_through_supervisor(tmp_path, monkeypatch, **schedule_kwargs):
    """Drive the REAL path: tool call -> event -> supervisor -> the task a worker is handed."""
    from types import SimpleNamespace

    from supervisor import events as ev_module
    from ouroboros.tools.control import _schedule_task

    ctx = _scheduling_ctx(tmp_path)
    out = _schedule_task(ctx, objective="o", expected_output="e", **schedule_kwargs)
    assert "TOOL_ARG_ERROR" not in out, out
    event = ctx.event_queue.get_nowait()
    event["type"] = "schedule_subagent"
    event["depth"] = 0
    event["delegation_role"] = ""

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *a, **k: None)
    enqueued = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        PENDING = []
        RUNNING = {}
        WORKERS = {0: SimpleNamespace(busy_task_id=None)}

        def load_state(self):
            return {"owner_chat_id": 1}

        def send_with_budget(self, chat_id, text, **kwargs):
            pass

        def enqueue_task(self, task):
            enqueued.append(task)

        def persist_queue_snapshot(self, reason=""):
            pass

    ev_module._handle_schedule_task(event, FakeCtx())
    assert enqueued, "supervisor did not enqueue the task"
    return enqueued[0]


def test_requested_effort_and_executor_survive_the_whole_scheduling_path(tmp_path, monkeypatch):
    """The parent's `effort` and `executor` must reach the task the WORKER is handed.

    This asserts on the task the supervisor actually enqueues, not on the event and not on
    a re-implementation of the agent's fallback. An earlier version of this test built the
    payload itself from the event, which meant it supplied the very keys under test and
    could not fail — and a version before THAT re-implemented the agent's three lines in
    the test body. Both passed while the supervisor was silently dropping the keys on the
    floor. The loss is destructive, not merely inert: the worker writes its own view back
    over the durable record, so a drop here also erases the evidence of what was asked."""
    task = _enqueue_through_supervisor(tmp_path, monkeypatch, effort="xhigh", executor="harness")
    assert task["reasoning_effort"] == "xhigh"
    assert task["requested_executor"] == "harness"
    assert task["metadata"]["reasoning_effort"] == "xhigh"
    assert task["metadata"]["requested_executor"] == "harness"


def test_omitted_effort_leaves_the_task_type_default_in_charge(tmp_path, monkeypatch):
    """Omission stays omission all the way down: an empty stored effort must not be
    mistaken for a request, or the child would pin an effort nobody asked for."""
    task = _enqueue_through_supervisor(tmp_path, monkeypatch)
    assert task["reasoning_effort"] == ""
    assert task["requested_executor"] == "auto"


def test_deadline_at_narrows_but_never_extends(tmp_path):
    """`deadline_at` is public as of v6.87.7, and narrowing-only: a child may be bound
    tighter than its parent, never looser."""
    from ouroboros.tools.control import _INTERNAL_SCHEDULE_OPTIONS, _schedule_task

    assert _INTERNAL_SCHEDULE_OPTIONS == frozenset()

    # Relative to now, not hardcoded: `deadline_at` must be a FUTURE instant, so fixed
    # calendar dates in this test would silently turn into rejections as time passes.
    from datetime import timedelta

    from ouroboros.deadline_utils import utc_now

    def stamp(hours):
        return (utc_now() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    parent, tighter, looser = stamp(12), stamp(9), stamp(23)

    ctx = _scheduling_ctx(tmp_path / "tighter", parent_deadline=parent)
    _schedule_task(ctx, objective="o", expected_output="e", deadline_at=tighter)
    evt = ctx.event_queue.get_nowait()
    assert evt["task_contract"]["deadline_at"] == tighter

    ctx = _scheduling_ctx(tmp_path / "looser", parent_deadline=parent)
    _schedule_task(ctx, objective="o", expected_output="e", deadline_at=looser)
    evt = ctx.event_queue.get_nowait()
    assert evt["task_contract"]["deadline_at"] == parent

    # A model-authored deadline is validated, because both failures are otherwise silent.
    ctx = _scheduling_ctx(tmp_path / "garbage")
    out = _schedule_task(ctx, objective="o", expected_output="e", deadline_at="in 2 hours")
    assert "TOOL_ARG_ERROR" in out and "ISO-8601" in out
    assert ctx.event_queue.empty()

    ctx = _scheduling_ctx(tmp_path / "past")
    out = _schedule_task(ctx, objective="o", expected_output="e", deadline_at=stamp(-1))
    assert "TOOL_ARG_ERROR" in out and "already in the past" in out
    assert ctx.event_queue.empty()


def test_the_envelope_carries_the_axes_the_child_was_scheduled_on(tmp_path, monkeypatch):
    """The envelope is the subagent's public description, and the TZ requires effort to
    reach it. Empty stays empty: substituting the resolved default would report a decision
    the parent never made, and the envelope is read as evidence of what WAS asked."""
    from ouroboros.tools.control import _schedule_task

    ctx = _scheduling_ctx(tmp_path / "asked")
    _schedule_task(ctx, objective="o", expected_output="e", effort="xhigh", executor="harness")
    envelope = ctx.event_queue.get_nowait()["subagent_envelope"]
    assert envelope["reasoning_effort"] == "xhigh"
    assert envelope["executor"] == "harness"

    ctx = _scheduling_ctx(tmp_path / "omitted")
    _schedule_task(ctx, objective="o", expected_output="e")
    envelope = ctx.event_queue.get_nowait()["subagent_envelope"]
    assert envelope["reasoning_effort"] == ""
    assert envelope["executor"] == "auto"


def test_the_envelope_states_what_actually_ran_not_only_what_was_asked(tmp_path, monkeypatch):
    """P34P1 (D4's p4-owned half): the durable envelope and the parent-facing terminal
    result were rebuilt from `task["requested_executor"]`, so an `auto` child that
    actually fell back to metered NATIVE spend reported `auto` in both — the dispatch
    resolution reached the event log and the child's own prompt and stopped there.
    The resolution is now persisted onto the task record at dispatch and carried into
    the envelope, with the divergence stated rather than left for a reader to infer.

    NOTE ON SCOPE: this is NOT D4's `capability_delta` chain, which lives on
    `cxi/p2-axes` (subagents.capability_delta_notice / agent.capability_delta_prompt_block
    / control.disclosable_capability_delta) and is deliberately absent here — the
    branch's own coherence guard in test_convergence_invariants pins that absence. This
    fixes the p4-owned half in p4's own vocabulary so the two compose at synthesis
    instead of becoming two implementations of one decision."""
    from ouroboros.subagents import build_subagent_envelope

    # An `auto` request that resolved to native: both facts, and the divergence.
    envelope = build_subagent_envelope(
        task_id="t-child", requested_lane="auto", effective_lane="light",
        executor="auto", resolved_executor="native",
        executor_reason="harness_not_configured", status="done")
    assert envelope["executor"] == "auto", "what was ASKED is preserved"
    assert envelope["resolved_executor"] == "native", "and what RAN is stated"
    assert envelope["executor_reason"] == "harness_not_configured"
    assert envelope["executor_diverged"] is True

    # A request that was honored is not a divergence.
    honored = build_subagent_envelope(
        task_id="t2", executor="harness", resolved_executor="harness",
        executor_reason="harness_ready", status="done")
    assert honored["executor_diverged"] is False

    # A task that never reached dispatch has no resolved value, and the request is NOT
    # substituted for one — that would be the same lie one field over.
    unreached = build_subagent_envelope(task_id="t3", executor="auto", status="queued")
    assert unreached["resolved_executor"] == "" and unreached["executor_diverged"] is False

    # The real seam: the loop persists the resolution onto the task record, and the
    # result pipeline reads it from there.
    import ouroboros.agent as agent

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "")     # harness not configured
    task = {"id": "t-child", "delegation_role": "subagent",
            "requested_executor": "auto", "model_lane": "light"}
    decision = agent.resolve_dispatch_executor(task)
    assert decision is not None and decision.executor == "native"
    task["resolved_executor"] = decision.executor
    task["executor_reason"] = decision.reason
    carried = build_subagent_envelope(
        task_id=str(task["id"]), executor=str(task.get("requested_executor") or ""),
        resolved_executor=str(task.get("resolved_executor") or ""),
        executor_reason=str(task.get("executor_reason") or ""), status="done")
    assert carried["resolved_executor"] == "native" and carried["executor_diverged"] is True


def test_the_scheduling_axes_survive_a_queue_snapshot(tmp_path, monkeypatch):
    """A pending child that waits through a restart must resume on the effort its parent
    asked for. The axes live at the task TOP LEVEL because that is where the agent loop
    reads them — restoring only the copies nested in `metadata` would leave the resumed
    child running the task-type default while its own record still claimed the request."""
    import supervisor.queue as q

    task = _enqueue_through_supervisor(tmp_path, monkeypatch, effort="xhigh", executor="harness")

    import json as _json

    captured = {}
    monkeypatch.setattr(q, "atomic_write_text",
                        lambda path, text: captured.update(_json.loads(text)))
    monkeypatch.setattr(q, "PENDING", [task], raising=False)
    monkeypatch.setattr(q, "RUNNING", {}, raising=False)
    assert q.persist_queue_snapshot(reason="test") is True

    rows = captured.get("pending") or []
    assert rows, captured
    restored = rows[0]["task"]
    assert restored["reasoning_effort"] == "xhigh"
    assert restored["requested_executor"] == "harness"
