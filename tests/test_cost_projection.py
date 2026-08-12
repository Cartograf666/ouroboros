"""C2 (owner 10=B): the SSOT cost projection — honest names, honest nulls.

`accounted_upper_bound_usd` is the additive honest name for `cost_usd` (same
value, deprecated alias retained — frozen ABI). Null is null: unknown cost never
renders as $0.00 and finality is never fabricated.
"""

from __future__ import annotations

from ouroboros.cost_projection import (
    cost_display,
    cost_projection,
    with_cost_aliases,
)


class TestAliases:
    def test_alias_pairs_mirror_both_directions(self):
        assert with_cost_aliases({"cost_usd": 1.5})["accounted_upper_bound_usd"] == 1.5
        assert with_cost_aliases({"accounted_upper_bound_usd": 2.5})["cost_usd"] == 2.5
        out = with_cost_aliases({"cost_usd_with_children": 3.0})
        assert out["accounted_upper_bound_usd_with_children"] == 3.0

    def test_deprecated_name_wins_a_diverged_pair(self):
        # Legacy mutators between two seam crossings edit cost_usd; re-aliasing
        # at the outer seam re-converges instead of shipping a stale honest name.
        out = with_cost_aliases({"cost_usd": None, "accounted_upper_bound_usd": 9.0})
        assert out["accounted_upper_bound_usd"] is None and out["cost_usd"] is None

    def test_aliasing_never_invents_a_field(self):
        assert "cost_usd" not in with_cost_aliases({"total_rounds": 3})
        assert with_cost_aliases(None) == {}

    def test_explicit_none_mirrors_as_none(self):
        out = with_cost_aliases({"cost_usd": None})
        assert out["accounted_upper_bound_usd"] is None


class TestProjection:
    def test_unknown_cost_is_null_on_both_names_and_never_final(self):
        out = cost_projection({"status": "completed"})
        assert out["cost_usd"] is None
        assert out["accounted_upper_bound_usd"] is None
        assert out["cost_known"] is False
        assert out["cost_final"] is False

    def test_missing_key_default_never_fabricates_zero(self):
        # The exact $0-fabrication class: data.get("cost_usd", 0) at five sites.
        assert cost_projection({})["cost_usd"] is None
        assert cost_projection(None)["cost_usd"] is None

    def test_finality_requires_a_known_amount(self):
        assert cost_projection({"cost_final": True})["cost_final"] is False
        assert cost_projection({"cost_final": True, "cost_usd": 0.0})["cost_final"] is True

    def test_known_amount_rides_both_names(self):
        out = cost_projection({"cost_usd": 0.42, "cost_final": True})
        assert out["accounted_upper_bound_usd"] == 0.42
        assert out["cost_usd"] == 0.42 and out["cost_known"] is True

    def test_with_children_pair_projects_only_when_present(self):
        assert "cost_usd_with_children" not in cost_projection({"cost_usd": 1.0})
        out = cost_projection({"cost_usd_with_children": 2.0})
        assert out["accounted_upper_bound_usd_with_children"] == 2.0

    def test_openness_flags_are_carried_not_dropped(self):
        out = cost_projection({"cost_usd": 1.0, "unknown_unmetered": 2, "non_final_rows": 1,
                               "cost_accounting_status": "available"})
        assert out["unknown_unmetered"] == 2 and out["non_final_rows"] == 1
        assert out["cost_accounting_status"] == "available"

    def test_boolean_is_not_an_amount(self):
        assert cost_projection({"cost_usd": True})["cost_usd"] is None


class TestDisplay:
    def test_null_never_renders_as_zero_dollars(self):
        text = cost_display({"status": "failed"})
        assert "$0.00" not in text and "unknown" in text

    def test_final_amount_is_plain_and_open_amount_is_labelled(self):
        assert cost_display({"cost_usd": 1.234, "cost_final": True}) == "$1.23"
        assert cost_display({"cost_usd": 1.234}) == "$1.23 (upper bound, not final)"
        assert cost_display({"cost_usd": 0.0, "cost_final": True}) == "$0.00"


class TestProducers:
    def test_reconstruct_task_cost_fields_carry_both_names(self, tmp_path):
        from supervisor.state import reconstruct_task_cost

        fields = reconstruct_task_cost("some-task", fields=True, drive_root=tmp_path)
        assert fields["accounted_upper_bound_usd"] == fields["cost_usd"]

    def test_handoff_block_has_no_fabricated_zero(self):
        import json

        from ouroboros.task_status import format_handoff_message

        message = format_handoff_message([{"task_id": "c1", "status": "completed"}])
        payload = json.loads(message.split("[SUBAGENT_HANDOFF_STATUS]\n", 1)[1]
                             .rsplit("\n[/SUBAGENT_HANDOFF_STATUS]", 1)[0])
        assert payload[0]["cost_usd"] is None
        assert payload[0]["accounted_upper_bound_usd"] is None
        assert payload[0]["cost_final"] is False

    def test_subagent_absorption_renders_unknown_not_zero(self):
        from ouroboros.task_status import format_subagent_absorption_message

        text = format_subagent_absorption_message([
            {"task_id": "c1", "status": "completed", "child_status": "completed",
             "result": "done", "role": "researcher", "parent_task_id": "p1"},
        ], parent_task_id="p1")
        assert "$0.0000" not in text
        assert "unknown" in text

    def test_task_cost_breakdown_carries_the_honest_total(self):
        from typing import get_type_hints

        from ouroboros.gateway.contracts import TaskCostBreakdown

        assert "accounted_upper_bound_usd" in get_type_hints(TaskCostBreakdown)

    def test_meta_fields_include_the_additive_names(self):
        from ouroboros.task_results import TASK_COST_META_FIELDS

        assert "accounted_upper_bound_usd" in TASK_COST_META_FIELDS
        assert "accounted_upper_bound_usd_with_children" in TASK_COST_META_FIELDS


class TestOnePrecedence:
    """F7: the read seam and the write seam must pick the SAME winner."""

    def test_read_and_write_agree_on_a_diverged_pair(self):
        diverged = {"cost_usd": 1.0, "accounted_upper_bound_usd": 9.0}
        written = with_cost_aliases(diverged)
        read = cost_projection(diverged)
        assert written["cost_usd"] == written["accounted_upper_bound_usd"] == 1.0
        assert read["cost_usd"] == read["accounted_upper_bound_usd"] == 1.0

    def test_resolver_is_the_one_answer_for_both(self):
        from ouroboros.cost_projection import COST_ALIAS_PAIRS, resolve_cost_pair

        assert resolve_cost_pair({"cost_usd": 1.0, "accounted_upper_bound_usd": 9.0},
                                 *COST_ALIAS_PAIRS[0]) == (True, 1.0)
        assert resolve_cost_pair({"accounted_upper_bound_usd": 9.0},
                                 *COST_ALIAS_PAIRS[0]) == (True, 9.0)
        assert resolve_cost_pair({}, *COST_ALIAS_PAIRS[0]) == (False, None)

    def test_persisted_result_leaves_the_pair_converged(self, tmp_path):
        # A producer that mutates the DEPRECATED name after aliasing used to
        # persist a diverged pair; aliasing is now the last step there.
        from ouroboros.task_results import load_task_result, write_task_result

        write_task_result(tmp_path, "t1", "completed",
                          **with_cost_aliases({"cost_usd": 2.0, "cost_final": True}))
        stored = load_task_result(tmp_path, "t1")
        assert stored["cost_usd"] == stored["accounted_upper_bound_usd"] == 2.0
        assert cost_projection(stored)["accounted_upper_bound_usd"] == 2.0


class TestOpennessCarry:
    """F12: openness/integrity markers travel with the amount, from ONE list."""

    def test_carry_includes_reserved_unresolved_and_integrity(self):
        from ouroboros.cost_projection import carry_cost_meta

        carried = carry_cost_meta({
            "cost_usd": 3.0, "reserved_usd": 0.5,
            "unresolved_upper_bound_usd": 1.25, "ledger_integrity_degraded": True,
            "cost_final": False, "irrelevant": "x",
        })
        assert carried["accounted_upper_bound_usd"] == 3.0
        assert carried["reserved_usd"] == 0.5
        assert carried["unresolved_upper_bound_usd"] == 1.25
        assert carried["ledger_integrity_degraded"] is True
        assert "irrelevant" not in carried

    def test_durable_meta_fields_derive_from_the_ssot(self):
        from ouroboros.cost_projection import COST_ALIAS_PAIRS, COST_OPENNESS_FIELDS
        from ouroboros.task_results import TASK_COST_META_FIELDS

        expected = {name for pair in COST_ALIAS_PAIRS for name in pair}
        expected |= set(COST_OPENNESS_FIELDS)
        assert set(TASK_COST_META_FIELDS) == expected
        assert "ledger_integrity_degraded" in TASK_COST_META_FIELDS

    def test_the_subagent_terminal_frame_covers_every_ssot_field(self):
        # The frame's KEYS must stay literal (a ChatOutbound key set has to be
        # statically checkable — tests/test_contracts.py), so this is the check
        # that keeps the literal honest: a marker added to the SSOT must appear
        # on the frame instead of being silently dropped like the three before it.
        import inspect

        from ouroboros.cost_projection import COST_ALIAS_PAIRS, COST_OPENNESS_FIELDS
        from supervisor.events import _finish_task_done_dispatch

        source = inspect.getsource(_finish_task_done_dispatch)
        for field in (*COST_OPENNESS_FIELDS, *COST_ALIAS_PAIRS[0]):
            assert f'"{field}"' in source, f"the terminal subagent frame drops {field}"

    def test_integrity_marker_is_declared_on_both_contract_mirrors(self):
        from typing import get_type_hints

        from ouroboros.gateway.contracts import ChatOutbound

        assert "ledger_integrity_degraded" in get_type_hints(ChatOutbound, include_extras=True)


class TestRestartRecoverySynthesis:
    """F11: an unknown cost must not become a confident $0.00 on recovery."""

    def test_recovered_usage_keeps_null(self):
        from ouroboros.agent_task_pipeline import _synthesis_cost_text

        unknown = {"cost": cost_projection({"status": "running"})["accounted_upper_bound_usd"]}
        assert unknown["cost"] is None
        assert "$0.00" not in _synthesis_cost_text(unknown)
        assert "unknown" in _synthesis_cost_text(unknown)
        # A REAL zero still reads as a zero.
        assert _synthesis_cost_text({"cost": 0.0}) == "$0.00"
