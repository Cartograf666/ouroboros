"""W2 claims wiring: the ONE pure seam ``effective_acceptance_claims`` and its two
consumers — the acceptance-evidence packet (ingress-contract claims first, the
CLOSED plan wave's frozen claims only when ingress is empty) and the child-contract
builder (claims re-stated AFTER the **parent_contract spread). The running task
contract is NEVER mutated: plan-frozen claims live in plan_review_state and are
resolved at read time."""

import tempfile
import types as _t
from pathlib import Path

from ouroboros.contracts.task_contract import effective_acceptance_claims
from ouroboros.review_evidence import build_task_acceptance_evidence


def _close_green_wave(root: Path, task_id: str, fingerprint: str, claims: list) -> None:
    from ouroboros.task_results import (
        STATUS_RUNNING,
        record_plan_review_attempt,
        record_plan_review_collection,
        record_plan_review_result,
        reserve_plan_review_wave,
        write_task_result,
    )

    write_task_result(root, task_id, STATUS_RUNNING, result="running")
    record_plan_review_attempt(root, task_id, fingerprint=fingerprint)
    reserve_plan_review_wave(
        root, task_id, fingerprint=fingerprint, plan_text_hash="d" * 64,
        scout_roles=[], cutoff_at="2026-08-08T00:00:00+00:00",
        acceptance_claims=claims,
    )
    record_plan_review_collection(
        root, task_id, fingerprint=fingerprint, included_task_ids=[], omissions=[],
        stop_reason="complete",
    )
    record_plan_review_result(
        root, task_id, fingerprint=fingerprint,
        review={
            "schema_version": 1,
            "request_fingerprint": fingerprint,
            "plan_text_hash": "d" * 64,
            "aggregate_signal": "GREEN",
            "findings": [],
            "closed": True,
            "reviewer_slots_degraded": False,
        },
        reviewed_result_hashes={},
    )


def test_effective_acceptance_claims_ingress_wins():
    wave = {"acceptance_claims": ["plan claim"]}
    claims, source = effective_acceptance_claims(
        {"acceptance_claims": [{"id": "c1", "claim": "ingress claim"}]}, wave,
    )
    assert source == "ingress_contract"
    assert [c["claim"] for c in claims] == ["ingress claim"]


def test_effective_acceptance_claims_plan_fallback_and_empty():
    claims, source = effective_acceptance_claims(
        {"acceptance_claims": []}, {"acceptance_claims": ["game boots", "score persists"]},
    )
    assert source == "plan_review"
    assert [c["claim"] for c in claims] == ["game boots", "score persists"]
    assert [c["id"] for c in claims] == ["claim_1", "claim_2"]
    assert effective_acceptance_claims({}, None) == ([], "")
    assert effective_acceptance_claims(None, {}) == ([], "")


def test_effective_acceptance_claims_accepts_task_like_mapping():
    task = {"task_contract": {"acceptance_claims": ["from contract"]}}
    claims, source = effective_acceptance_claims(task)
    assert source == "ingress_contract"
    assert claims[0]["claim"] == "from contract"


def test_packet_uses_plan_frozen_claims_when_ingress_empty():
    dr = Path(tempfile.mkdtemp())
    _close_green_wave(dr, "acc", "a" * 64, ["game boots", "score persists"])
    ctx = _t.SimpleNamespace(
        task_contract={"requirements": "do X", "expected_output": "42"},
        task_metadata={}, drive_root=str(dr), task_id="acc", repo_dir=str(dr),
    )

    ev = build_task_acceptance_evidence(ctx, llm_trace={"tool_calls": []}, drive_root=dr, task_id="acc")

    packet_claims = ev["task_contract"]["acceptance_claims"]
    assert [c["claim"] for c in packet_claims] == ["game boots", "score persists"]
    assert ev["acceptance_claims_source"] == "plan_review"
    assert ev["__provenance__"]["acceptance_claims_source"] == "host_attested"


def test_packet_prefers_ingress_claims_over_plan_wave():
    dr = Path(tempfile.mkdtemp())
    _close_green_wave(dr, "acc", "a" * 64, ["plan claim"])
    ctx = _t.SimpleNamespace(
        task_contract={"acceptance_claims": [{"id": "adapter_1", "claim": "adapter claim"}]},
        task_metadata={}, drive_root=str(dr), task_id="acc", repo_dir=str(dr),
    )

    ev = build_task_acceptance_evidence(ctx, llm_trace={"tool_calls": []}, drive_root=dr, task_id="acc")

    packet_claims = ev["task_contract"]["acceptance_claims"]
    assert [c["claim"] for c in packet_claims] == ["adapter claim"]
    assert ev["acceptance_claims_source"] == "ingress_contract"


def test_packet_claims_lookup_is_fail_soft():
    dr = Path(tempfile.mkdtemp())
    # Malformed task-result JSON: the plan-state lookup raises inside, packet survives.
    results = dr / "task_results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "acc.json").write_text("{not json", encoding="utf-8")
    ctx = _t.SimpleNamespace(
        task_contract={"requirements": "do X"},
        task_metadata={}, drive_root=str(dr), task_id="acc", repo_dir=str(dr),
    )

    ev = build_task_acceptance_evidence(ctx, llm_trace={"tool_calls": []}, drive_root=dr, task_id="acc")

    assert "acceptance_claims_source" not in ev
    assert not ev["task_contract"].get("acceptance_claims")


def test_packet_open_plan_wave_binds_no_claims():
    from ouroboros.task_results import (
        STATUS_RUNNING,
        record_plan_review_attempt,
        reserve_plan_review_wave,
        write_task_result,
    )

    dr = Path(tempfile.mkdtemp())
    write_task_result(dr, "acc", STATUS_RUNNING, result="running")
    record_plan_review_attempt(dr, "acc", fingerprint="a" * 64)
    reserve_plan_review_wave(
        dr, "acc", fingerprint="a" * 64, plan_text_hash="d" * 64,
        scout_roles=[], cutoff_at="2026-08-08T00:00:00+00:00",
        acceptance_claims=["unreviewed claim"],
    )
    ctx = _t.SimpleNamespace(
        task_contract={"requirements": "do X"},
        task_metadata={}, drive_root=str(dr), task_id="acc", repo_dir=str(dr),
    )

    ev = build_task_acceptance_evidence(ctx, llm_trace={"tool_calls": []}, drive_root=dr, task_id="acc")

    # An OPEN (unclosed) wave's claims never bind acceptance.
    assert "acceptance_claims_source" not in ev
    assert not ev["task_contract"].get("acceptance_claims")
