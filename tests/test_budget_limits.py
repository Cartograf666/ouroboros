"""Tests for _check_budget_limits (global budget guard + tree-fed in-task ceiling)
and the cost axis (typed v6.91 ceiling states + latched v6.56.0 milestones)."""
import os
import queue
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ouroboros import task_pacing
from ouroboros.contracts.task_contract import normalize_budget_profile
from ouroboros.loop import _RoundLimitContext, _check_budget_limits


def _make_args(**overrides):
    """Build default kwargs for _check_budget_limits.

    ``cost_ceiling`` defaults to the typed resolution with no root cap, so the
    legacy guard tests keep exercising the historical 50%-of-global semantics
    the runtime gets from ``task_pacing.resolve_cost_ceiling`` with an absent
    profile.
    """
    llm = MagicMock()
    llm.chat.return_value = (
        {"role": "assistant", "content": ""},
        {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0},
    )
    defaults = dict(
        budget_remaining_usd=100.0,
        accumulated_usage={"cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0},
        round_idx=0,
        messages=[],
        llm=llm,
        active_model="test-model",
        active_effort="high",
        max_retries=1,
        drive_logs=None,
        task_id="test-task",
        event_queue=queue.Queue(),
        llm_trace={},
        task_type="task",
        use_local=False,
    )
    defaults.update(overrides)
    if "cost_ceiling" not in defaults:
        defaults["cost_ceiling"] = task_pacing.resolve_cost_ceiling(
            defaults["budget_remaining_usd"], normalize_budget_profile(None),
        )
    budget_remaining_usd = defaults.pop("budget_remaining_usd")
    cost_ceiling = defaults.pop("cost_ceiling")
    ctx = _RoundLimitContext(
        messages=defaults["messages"],
        llm=defaults["llm"],
        active_model=defaults["active_model"],
        active_effort=defaults["active_effort"],
        max_retries=defaults["max_retries"],
        drive_logs=defaults["drive_logs"],
        task_id=defaults["task_id"],
        round_idx=defaults["round_idx"],
        event_queue=defaults["event_queue"],
        accumulated_usage=defaults["accumulated_usage"],
        task_type=defaults["task_type"],
        active_use_local=defaults["use_local"],
        max_rounds=100,
        deadline_ts=defaults.get("deadline_ts"),
        llm_trace=defaults["llm_trace"],
    )
    return {
        "ctx": ctx,
        "budget_remaining_usd": budget_remaining_usd,
        "cost_ceiling": cost_ceiling,
    }


# --- The retired per-task soft reminder (v6.91) ---

class TestPerTaskSoftNoteRetired:
    """The pre-v6.91 own-cost "[COST NOTE]" keyed to OUROBOROS_PER_TASK_COST_USD
    is gone: since v6.64.0 the same key hard-fences the whole tree at the
    ledger, so the note could never fire before the fence (proven live)."""

    def test_no_soft_note_at_or_above_key_value(self, tmp_path):
        messages = []
        args = _make_args(
            accumulated_usage={"cost": 6.0},
            round_idx=10,
            messages=messages,
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "5.0"}):
            result = _check_budget_limits(**args)
        assert result is None
        assert not any("[COST NOTE]" in m.get("content", "") for m in messages)

    def test_no_stop_from_the_key_alone(self, tmp_path):
        """The key does not stop the loop here; the ledger fence and the typed
        ceiling own that axis."""
        args = _make_args(accumulated_usage={"cost": 20.0}, round_idx=10, drive_logs=tmp_path)
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "5.0"}):
            result = _check_budget_limits(**args)
        assert result is None


# --- Global budget guard ---

class TestGlobalBudgetGuard:
    """Existing global budget percentage checks."""

    def test_none_budget_returns_none(self, tmp_path):
        """No budget → no checks."""
        args = _make_args(budget_remaining_usd=None, accumulated_usage={"cost": 100.0}, drive_logs=tmp_path)
        result = _check_budget_limits(**args)
        assert result is None

    def test_budget_exhausted(self, tmp_path):
        """Remaining ≤ 0 → immediate stop."""
        args = _make_args(budget_remaining_usd=0.0, accumulated_usage={"cost": 0.01}, drive_logs=tmp_path)
        with (
            patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "999"}),
            patch("ouroboros.loop.call_llm_with_retry") as model_call,
        ):
            result = _check_budget_limits(**args)
        assert result is not None
        text, _, _ = result
        assert "budget exhausted" in text.lower()
        model_call.assert_not_called()

    def test_under_50pct_passes(self, tmp_path):
        """Task cost < 50% of remaining → no stop."""
        args = _make_args(
            budget_remaining_usd=10.0,
            accumulated_usage={"cost": 4.9},  # 49% < 50%
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "10.0"}):
            result = _check_budget_limits(**args)
        assert result is None

    def test_over_50pct_triggers(self, tmp_path):
        """Task cost > 50% of remaining budget → stops."""
        llm = MagicMock()
        llm.chat.return_value = ({"content": "done"}, {"prompt_tokens": 10, "completion_tokens": 5})
        args = _make_args(
            budget_remaining_usd=8.0,
            accumulated_usage={"cost": 4.5},  # 4.5/8 = 56% > 50%
            llm=llm,
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "10.0"}):
            result = _check_budget_limits(**args)
        assert result is not None

    def test_legacy_info_nudge_removed(self, tmp_path):
        """The old round-gated '[INFO] ... wrap up' nudge is gone (v6.56.0):
        cost awareness now comes from the latched task_pacing milestones."""
        messages = []
        args = _make_args(
            budget_remaining_usd=10.0,
            accumulated_usage={"cost": 3.5},  # 35% — would have nudged before
            round_idx=20,
            messages=messages,
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "10.0"}):
            result = _check_budget_limits(**args)
        assert result is None
        assert not any("[INFO]" in m.get("content", "") for m in messages)


# --- use_local propagation ---

class TestUseLocalPropagation:
    """Ensure use_local is passed to _call_llm_with_retry on global budget stop."""

    @patch("ouroboros.loop.call_llm_with_retry")
    def test_global_stop_passes_use_local(self, mock_retry, tmp_path):
        mock_retry.return_value = ({"content": "done"}, {"prompt_tokens": 10, "completion_tokens": 5})
        args = _make_args(
            budget_remaining_usd=6.0,
            accumulated_usage={"cost": 4.0},  # 67% > 50%
            use_local=True,
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "10.0"}):
            _check_budget_limits(**args)
        mock_retry.assert_called_once()
        _, kwargs = mock_retry.call_args
        assert kwargs.get("use_local") is True


# --- v6.91 typed cost ceiling resolution ---

class TestCostCeilingResolution:
    """task_pacing.resolve_cost_ceiling: typed states, global pct component,
    root-cap-minus-margin component."""

    def test_absent_profile_means_historical_50pct(self):
        profile = normalize_budget_profile(None)
        ceiling = task_pacing.resolve_cost_ceiling(10.0, profile)
        assert ceiling.state == task_pacing.COST_CEILING_ACTIVE
        assert ceiling.ceiling_usd == 5.0
        assert ceiling.root_cap_usd is None

    def test_zero_pct_means_disabled_never_zero_dollars(self):
        profile = normalize_budget_profile({"cost_hard_stop_pct": 0})
        ceiling = task_pacing.resolve_cost_ceiling(10.0, profile)
        assert ceiling.state == task_pacing.COST_CEILING_DISABLED
        assert ceiling.ceiling_usd is None

    def test_zero_pct_disabled_even_with_tiny_root_cap(self):
        """The bench contract (SWE-Pro: pct=0 + a small root cap) keeps the
        in-task stop fully off — the ledger fence is the only stop."""
        profile = normalize_budget_profile({"cost_hard_stop_pct": 0})
        ceiling = task_pacing.resolve_cost_ceiling(10.0, profile, root_cap_usd=0.5)
        assert ceiling.state == task_pacing.COST_CEILING_DISABLED
        assert ceiling.ceiling_usd is None

    def test_custom_pct(self):
        profile = normalize_budget_profile({"cost_hard_stop_pct": 25})
        ceiling = task_pacing.resolve_cost_ceiling(10.0, profile)
        assert ceiling.state == task_pacing.COST_CEILING_ACTIVE
        assert ceiling.ceiling_usd == 2.5

    def test_no_finite_budget_means_disabled_axis(self):
        profile = normalize_budget_profile(None)
        assert task_pacing.resolve_cost_ceiling(None, profile).state == task_pacing.COST_CEILING_DISABLED
        assert task_pacing.resolve_cost_ceiling(0.0, profile).state == task_pacing.COST_CEILING_DISABLED

    def test_root_cap_component_binds_when_smaller(self):
        """min(pct-of-global, cap − margin): the live wave1/2 shape — a huge
        global remaining must not hide a $100 tree cap."""
        profile = normalize_budget_profile(None)
        ceiling = task_pacing.resolve_cost_ceiling(1900.0, profile, root_cap_usd=100.0)
        assert ceiling.state == task_pacing.COST_CEILING_ACTIVE
        assert ceiling.root_cap_usd == 100.0
        assert ceiling.ceiling_usd == 100.0 - task_pacing.COST_PLANNING_MARGIN_USD
        assert ceiling.planning_margin_usd == task_pacing.COST_PLANNING_MARGIN_USD

    def test_global_pct_component_binds_when_smaller(self):
        profile = normalize_budget_profile(None)
        ceiling = task_pacing.resolve_cost_ceiling(10.0, profile, root_cap_usd=100.0)
        assert ceiling.state == task_pacing.COST_CEILING_ACTIVE
        assert ceiling.ceiling_usd == 5.0

    def test_root_cap_only_no_finite_global(self):
        """A per-task cap with an unbounded global still yields an active stop."""
        profile = normalize_budget_profile(None)
        ceiling = task_pacing.resolve_cost_ceiling(None, profile, root_cap_usd=50.0)
        assert ceiling.state == task_pacing.COST_CEILING_ACTIVE
        assert ceiling.ceiling_usd == 50.0 - task_pacing.COST_PLANNING_MARGIN_USD

    def test_cap_at_or_below_margin_soft_lands_never_uncapped(self):
        """A root cap at/below the planning margin must resolve to the typed
        soft-land state — the pre-typed shape returned the same None as
        'unlimited' (a $0.50 bench cap would have run uncapped)."""
        profile = normalize_budget_profile(None)
        for cap in (0.5, task_pacing.COST_PLANNING_MARGIN_USD):
            ceiling = task_pacing.resolve_cost_ceiling(100.0, profile, root_cap_usd=cap)
            assert ceiling.state == task_pacing.COST_CEILING_EXHAUSTED_SOFT_LAND, cap
            assert ceiling.ceiling_usd is None
            assert ceiling.root_cap_usd == cap

    def test_ceiling_is_never_computed_zero(self):
        profile = normalize_budget_profile(None)
        just_above = task_pacing.COST_PLANNING_MARGIN_USD + 0.01
        ceiling = task_pacing.resolve_cost_ceiling(1000.0, profile, root_cap_usd=just_above)
        assert ceiling.state == task_pacing.COST_CEILING_ACTIVE
        assert ceiling.ceiling_usd is not None and ceiling.ceiling_usd > 0

    def test_planning_margin_is_absolute_not_pct(self):
        """The margin must not scale with the cap (a pct reserve amputated the
        tail of long tasks — v6.54.4 r1; the money-axis analogue is pinned)."""
        profile = normalize_budget_profile(None)
        small = task_pacing.resolve_cost_ceiling(None, profile, root_cap_usd=10.0)
        large = task_pacing.resolve_cost_ceiling(None, profile, root_cap_usd=1000.0)
        assert small.ceiling_usd == 10.0 - task_pacing.COST_PLANNING_MARGIN_USD
        assert large.ceiling_usd == 1000.0 - task_pacing.COST_PLANNING_MARGIN_USD

    def test_malformed_pct_fails_safe_to_default_not_zero(self):
        """A garbage cost_hard_stop_pct must NOT silently become 0 (= no in-task
        stop, the most permissive setting): negative / non-numeric / a 0<v<1
        fraction map to None (the 50% default), while an explicit 0 is honored."""
        for bad in (-5, -0.1, 0.5, "0.5", "abc", [1]):
            profile = normalize_budget_profile({"cost_hard_stop_pct": bad})
            assert profile["cost_hard_stop_pct"] is None, bad
            ceiling = task_pacing.resolve_cost_ceiling(10.0, profile)
            assert ceiling.state == task_pacing.COST_CEILING_ACTIVE, bad
            assert ceiling.ceiling_usd == 5.0, bad
        # explicit 0 (and "0") stays a deliberate no-stop; whole percents clamp.
        assert normalize_budget_profile({"cost_hard_stop_pct": 0})["cost_hard_stop_pct"] == 0
        assert normalize_budget_profile({"cost_hard_stop_pct": "0"})["cost_hard_stop_pct"] == 0
        assert normalize_budget_profile({"cost_hard_stop_pct": 250})["cost_hard_stop_pct"] == 100


class TestCostCeilingStop:
    """_check_budget_limits consumes the typed pre-resolved ceiling."""

    def test_no_active_ceiling_means_no_in_task_stop(self, tmp_path):
        """disabled state → even a huge task spend does not stop here."""
        messages = []
        disabled = task_pacing.resolve_cost_ceiling(
            100.0, normalize_budget_profile({"cost_hard_stop_pct": 0}),
        )
        args = _make_args(
            budget_remaining_usd=100.0,
            accumulated_usage={"cost": 90.0},
            cost_ceiling=disabled,
            messages=messages,
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "999"}):
            result = _check_budget_limits(**args)
        assert result is None
        assert messages == []

    def test_none_ceiling_object_means_no_in_task_stop(self, tmp_path):
        args = _make_args(
            budget_remaining_usd=100.0,
            accumulated_usage={"cost": 90.0},
            cost_ceiling=None,
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "999"}):
            result = _check_budget_limits(**args)
        assert result is None

    def test_custom_ceiling_stops_when_exceeded(self, tmp_path):
        llm = MagicMock()
        llm.chat.return_value = ({"content": "done"}, {"prompt_tokens": 1, "completion_tokens": 1})
        args = _make_args(
            budget_remaining_usd=100.0,
            accumulated_usage={"cost": 26.0},
            cost_ceiling=task_pacing.resolve_cost_ceiling(
                50.0, normalize_budget_profile(None),
            ),
            llm=llm,
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "999"}):
            result = _check_budget_limits(**args)
        assert result is not None

    def test_cost_equal_to_ceiling_does_not_stop(self, tmp_path):
        """Strict > preserves the historical edge (budget_pct > 0.5)."""
        args = _make_args(
            budget_remaining_usd=100.0,
            accumulated_usage={"cost": 25.0},
            cost_ceiling=task_pacing.resolve_cost_ceiling(
                50.0, normalize_budget_profile(None),
            ),
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "999"}):
            result = _check_budget_limits(**args)
        assert result is None


# --- v6.91 tree-fed deciding value ---

class TestTreeFedDecidingValue:
    """Under a root cap the deciding spend is the root subtree's ledger-accounted
    number from the reserve-time scope telemetry — own cost stays a diagnostic.
    The waves died at tree $84-94 while own cost showed $41-49 and no warning
    ever fired; these pin the closed class."""

    def _scoped(self, root_id, root_limit):
        from ouroboros.usage_accounting import UsageScope, usage_scope

        return usage_scope(UsageScope(
            drive_root=None, task_id=root_id, root_task_id=root_id,
            root_limit_usd=root_limit,
        ))

    def test_tree_spend_over_ceiling_stops_even_when_own_is_low(self, tmp_path):
        from ouroboros import usage_accounting

        llm = MagicMock()
        llm.chat.return_value = ({"content": "done"}, {"prompt_tokens": 1, "completion_tokens": 1})
        ceiling = task_pacing.resolve_cost_ceiling(
            1900.0, normalize_budget_profile(None), root_cap_usd=100.0,
        )
        assert ceiling.state == task_pacing.COST_CEILING_ACTIVE
        args = _make_args(
            budget_remaining_usd=1900.0,
            accumulated_usage={"cost": 41.0},  # own: far below the ceiling
            cost_ceiling=ceiling,
            llm=llm,
            drive_logs=tmp_path,
        )
        with self._scoped("root-tree-1", 100.0):
            usage_accounting._stash_root_accounting("root-tree-1", 98.5, 100.0)
            result = _check_budget_limits(**args)
        assert result is not None
        text, usage, _ = result
        assert usage.get("reason_code") == "budget_exhausted"

    def test_tree_spend_under_ceiling_does_not_stop(self, tmp_path):
        from ouroboros import usage_accounting

        ceiling = task_pacing.resolve_cost_ceiling(
            1900.0, normalize_budget_profile(None), root_cap_usd=100.0,
        )
        args = _make_args(
            budget_remaining_usd=1900.0,
            accumulated_usage={"cost": 41.0},
            cost_ceiling=ceiling,
            drive_logs=tmp_path,
        )
        with self._scoped("root-tree-2", 100.0):
            usage_accounting._stash_root_accounting("root-tree-2", 60.0, 100.0)
            result = _check_budget_limits(**args)
        assert result is None

    def test_unknown_tree_falls_back_to_own_cost_never_zero(self, tmp_path):
        """No telemetry for this tree → the deciding value falls back to own
        cost (a real number), never a coerced $0 that would disable the stop."""
        llm = MagicMock()
        llm.chat.return_value = ({"content": "done"}, {"prompt_tokens": 1, "completion_tokens": 1})
        ceiling = task_pacing.resolve_cost_ceiling(
            10.0, normalize_budget_profile(None),
        )
        args = _make_args(
            budget_remaining_usd=10.0,
            accumulated_usage={"cost": 6.0},  # own over the $5 ceiling
            cost_ceiling=ceiling,
            llm=llm,
            drive_logs=tmp_path,
        )
        result = _check_budget_limits(**args)
        assert result is not None


class TestRootAccountingTelemetry:
    def test_stash_roundtrip_and_age(self):
        from ouroboros import usage_accounting

        usage_accounting._stash_root_accounting("root-t-1", 12.5, 100.0)
        entry = usage_accounting.last_root_accounting("root-t-1")
        assert entry is not None
        assert entry["accounted_usd"] == 12.5
        assert entry["root_limit_usd"] == 100.0
        assert entry["age_sec"] >= 0.0

    def test_unknown_root_is_none(self):
        from ouroboros import usage_accounting

        assert usage_accounting.last_root_accounting("no-such-root") is None
        assert usage_accounting.last_root_accounting("") is None

    def test_refresh_reads_ledger_and_updates_stash(self, tmp_path, monkeypatch):
        from ouroboros import usage_accounting

        monkeypatch.setattr(
            usage_accounting, "usage_projection",
            lambda *a, **k: {"accounted_usd": 7.25, "limit_usd": 25.0},
        )
        entry = usage_accounting.refresh_root_accounting(tmp_path, "root-t-2")
        assert entry is not None and entry["accounted_usd"] == 7.25
        assert usage_accounting.last_root_accounting("root-t-2")["root_limit_usd"] == 25.0

    def test_refresh_failure_returns_stale_stash_not_zero(self, tmp_path, monkeypatch):
        from ouroboros import usage_accounting

        usage_accounting._stash_root_accounting("root-t-3", 3.0, 10.0)

        def _boom(*a, **k):
            raise RuntimeError("ledger unavailable")

        monkeypatch.setattr(usage_accounting, "usage_projection", _boom)
        entry = usage_accounting.refresh_root_accounting(tmp_path, "root-t-3")
        assert entry is not None and entry["accounted_usd"] == 3.0

    def test_reserve_attempt_piggybacks_tree_sum(self, tmp_path):
        """The stash is a byproduct of the existing in-lock computation — no new
        ledger read path (the e4a87344 starvation constraint)."""
        from ouroboros import usage_accounting
        from ouroboros.usage_accounting import AttemptRequest, UsageScope, usage_scope

        scope = UsageScope(
            drive_root=tmp_path, task_id="rroot", root_task_id="rroot",
            global_limit_usd=100.0, root_limit_usd=50.0,
        )
        with usage_scope(scope):
            usage_accounting.reserve_attempt(AttemptRequest(
                model="test/model", provider="local", drive_root=tmp_path,
            ))
        entry = usage_accounting.last_root_accounting("rroot")
        assert entry is not None
        assert entry["accounted_usd"] == 0.0  # measured pre-attempt sum, fresh tree
        assert entry["root_limit_usd"] == 50.0


# --- v6.56.0 cost axis: latched milestones + wrap-up (task_pacing content) ---

class TestCostMilestones:
    def test_milestones_latch_once_and_sequence(self):
        ctx = SimpleNamespace()
        kw = dict(start_remaining_usd=20.0, cost_ceiling_usd=10.0)
        # 50% remaining of the $10 ceiling crossed.
        note = task_pacing.build_cost_budget_note(ctx, task_cost=5.1, **kw)
        assert note is not None and "50% remaining" in note.text
        assert note.checkpoint["checkpoint_kind"] == "cost_budget_milestone"
        assert note.checkpoint["hard_stop"] is True
        # Same spend again → latched, silent.
        assert task_pacing.build_cost_budget_note(ctx, task_cost=5.1, **kw) is None
        # 25% remaining crossed.
        note = task_pacing.build_cost_budget_note(ctx, task_cost=7.6, **kw)
        assert note is not None and "25% remaining" in note.text
        # ~80% spent → one-shot wrap-up.
        note = task_pacing.build_cost_budget_note(ctx, task_cost=8.1, **kw)
        assert note is not None and note.checkpoint["checkpoint_kind"] == "cost_budget_wrapup"
        assert task_pacing.build_cost_budget_note(ctx, task_cost=8.2, **kw) is None
        # 10% remaining crossed (wrap-up already latched, no duplicate).
        note = task_pacing.build_cost_budget_note(ctx, task_cost=9.1, **kw)
        assert note is not None and "10% remaining" in note.text
        assert task_pacing.build_cost_budget_note(ctx, task_cost=9.9, **kw) is None

    def test_jump_past_wrapup_with_milestone_suppresses_duplicate_wrapup(self):
        """A single jump deep past 80% spent fires the tightest milestone and
        latches wrap-up, so the next round does not double-note."""
        ctx = SimpleNamespace()
        kw = dict(start_remaining_usd=20.0, cost_ceiling_usd=10.0)
        note = task_pacing.build_cost_budget_note(ctx, task_cost=9.5, **kw)
        assert note is not None and "10% remaining" in note.text
        assert task_pacing.build_cost_budget_note(ctx, task_cost=9.6, **kw) is None

    def test_no_finite_budget_axis_is_silent(self):
        ctx = SimpleNamespace()
        assert task_pacing.build_cost_budget_note(
            ctx, start_remaining_usd=None, cost_ceiling_usd=None, task_cost=999.0,
        ) is None

    def test_uncapped_run_uses_start_snapshot_informationally(self):
        """cost_hard_stop_pct=0: milestones fire against the start snapshot,
        disclose there is no in-task stop, and clamp remaining at 0%."""
        ctx = SimpleNamespace()
        kw = dict(start_remaining_usd=10.0, cost_ceiling_usd=None)
        note = task_pacing.build_cost_budget_note(ctx, task_cost=5.5, **kw)
        assert note is not None and "no in-task cost stop" in note.text
        assert note.checkpoint["hard_stop"] is False
        # Spend past the whole snapshot: clamped, still just the tightest milestone.
        note = task_pacing.build_cost_budget_note(ctx, task_cost=25.0, **kw)
        assert note is not None and "10% remaining" in note.text
        assert "Remaining: ~$0.00" in note.text

    def test_tree_cost_is_the_deciding_value_and_is_labeled(self):
        """v6.91: the tree-accounted number decides the crossing and is labeled
        honestly (incl. in-flight holds); own cost rides as the diagnostic."""
        ctx = SimpleNamespace()
        note = task_pacing.build_cost_budget_note(
            ctx, start_remaining_usd=200.0, cost_ceiling_usd=97.0,
            task_cost=41.0, tree_cost_usd=50.0,
        )
        assert note is not None and "50% remaining" in note.text
        assert "in-flight holds" in note.text
        assert "own calls ~$41.00" in note.text
        assert note.checkpoint["spend_basis"] == "tree_accounted"
        assert note.checkpoint["task_cost_usd"] == 50.0
        assert note.checkpoint["own_cost_usd"] == 41.0

    def test_own_cost_alone_would_not_have_crossed(self):
        """The wave1/2 blindness pin: own $41 of a $97 ceiling fires nothing,
        tree $50 fires the 50% milestone."""
        silent_ctx = SimpleNamespace()
        assert task_pacing.build_cost_budget_note(
            silent_ctx, start_remaining_usd=200.0, cost_ceiling_usd=97.0,
            task_cost=41.0,
        ) is None

    def test_unknown_tree_cost_falls_back_to_own(self):
        ctx = SimpleNamespace()
        note = task_pacing.build_cost_budget_note(
            ctx, start_remaining_usd=20.0, cost_ceiling_usd=10.0,
            task_cost=5.1, tree_cost_usd=None,
        )
        assert note is not None and "Spent this task: ~$5.10" in note.text
        assert "spend_basis" not in note.checkpoint
