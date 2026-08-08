"""D-Q5: host resolution of reviewer evidence_refs by EXACT membership against the
packet's enumerable exhibit keys. The resolution feeds ONLY the release-clean bit
(task_acceptance_is_clean) plus disclosure — parse validity, quorum participation,
and verdicts are untouched (the v6.71.1 starvation and v6.64.3 bare-veto classes
stay closed), and rows are absent on historical results (forward-only)."""

import json

from ouroboros.review_evidence import (
    acceptance_evidence_ref_vocabulary,
    annotate_criteria_evidence_resolution,
    resolve_criteria_evidence_refs,
)
from ouroboros.review_substrate import ReviewRunResult, task_acceptance_is_clean


_PACKET = {
    "task_contract": {
        "objective": "ship it",
        "acceptance_claims": [
            {"id": "claim_1", "claim": "game boots"},
            {"id": "claim_2", "claim": "score persists"},
        ],
    },
    # Host support table: claim_1 has a PASSING receipt behind it, claim_2 was
    # only declared. A reviewer may cite claim_1 as evidence; claim_2 names a
    # real claim but no host-attested support.
    "acceptance_support_refs": [
        {"criterion_id": "claim_1", "support_status": "supported",
         "support_refs": [{"ref": "verification_receipts[0]", "status": "pass"}]},
        {"criterion_id": "claim_2", "support_status": "declared_only",
         "support_refs": [{"ref": "verification_receipts[1]", "status": "declared"}]},
    ],
    "verification_summary": {"count": 2, "failed_count": 0},
    "acceptance_obligations": [{"id": "ob-12ab34cd", "item": "cover edge case"}],
    "artifacts": [{"name": "report/summary.md", "size": 10}],
    "repo_diff": "diff --git ...",
    "__provenance__": {"task_contract": "host_attested"},
}


def _actor(criteria, unresolved_rows=None):
    row = {
        "slot_id": "s0", "signal": "PASS",
        "parsed": {
            "verdict": "PASS", "outcome_tier": "solved", "criteria_used": criteria,
        },
    }
    if unresolved_rows is not None:
        row["criteria_refs_unresolved"] = unresolved_rows
    return row


def _result(actors):
    return ReviewRunResult(
        request={"surface": "task_acceptance", "policy": {"min_successful_slots": 1}},
        actors=actors, parsed_findings=[], aggregate_signal="PASS",
    )


def test_vocabulary_enumerates_packet_exhibits_exactly():
    vocab = acceptance_evidence_ref_vocabulary(_PACKET)
    # A claim id is evidence only through the HOST support table.
    assert vocab["claim_1"] == "claim_id"
    assert vocab["claim_2"] == "claim_id_unsupported"
    assert vocab["ob-12ab34cd"] == "obligation_id"
    assert vocab["report/summary.md"] == "artifact"
    assert vocab["verification_receipts[0]"] == "verification_receipt"
    assert vocab["verification_receipts[1]"] == "verification_receipt"
    assert "verification_receipts[2]" not in vocab
    assert vocab["repo_diff"] == "packet_section"
    assert vocab["verification_summary"] == "packet_section"
    assert "__provenance__" not in vocab
    # Robust to junk input.
    assert acceptance_evidence_ref_vocabulary(None) == {}
    assert acceptance_evidence_ref_vocabulary("junk") == {}


def test_resolution_is_exact_match_only_no_fuzzy():
    vocab = acceptance_evidence_ref_vocabulary(_PACKET)
    rows = resolve_criteria_evidence_refs(
        [{"criterion": "game boots", "status": "supported",
          "evidence_refs": ["verification_receipts", "the SERVE_OK receipt", "repo_dif"]}],
        vocab,
    )
    # Substrings / near-misses of real keys never resolve (v6.78 lossy-identity lesson).
    assert len(rows) == 1
    assert rows[0]["supported_evidence_resolves"] is False
    assert all(ref["resolved_as"] == "" for ref in rows[0]["refs"])


def test_resolution_discloses_deciding_basis_and_counts_one_resolving_ref():
    vocab = acceptance_evidence_ref_vocabulary(_PACKET)
    rows = resolve_criteria_evidence_refs(
        [{"criterion": "game boots", "status": "supported",
          "evidence_refs": ["verification_receipts[1]", "made-up-ref"]}],
        vocab,
    )
    assert len(rows) == 1
    assert rows[0]["supported_evidence_resolves"] is True  # >=1 ref resolves -> counts
    bases = {ref["ref"]: ref["resolved_as"] for ref in rows[0]["refs"]}
    assert bases["verification_receipts[1]"] == "verification_receipt"
    assert bases["made-up-ref"] == ""
    # Fully-resolving criteria produce NO row (common clean path is unannotated),
    # and non-supported criteria are never resolution's business.
    assert resolve_criteria_evidence_refs(
        [{"criterion": "c", "status": "supported", "evidence_refs": ["claim_1"]},
         {"criterion": "d", "status": "missing", "evidence_refs": ["nonsense"]}],
        vocab,
    ) == []


def test_bare_claim_id_without_a_host_receipt_never_resolves():
    """The fabricated-evidence hole D-Q5 exists to close: a reviewer citing a
    claim the task itself declared, with no passing receipt behind it, must not
    buy a release-clean PASS. The disclosure NAMES the claim (it is a real packet
    entry) instead of pretending the ref was nonsense."""
    vocab = acceptance_evidence_ref_vocabulary(_PACKET)
    rows = resolve_criteria_evidence_refs(
        [{"criterion": "score persists", "status": "supported", "evidence_refs": ["claim_2"]}],
        vocab,
    )
    assert rows[0]["supported_evidence_resolves"] is False
    assert rows[0]["refs"][0]["resolved_as"] == "claim_id_unsupported"
    # ...and it demotes only the clean bit, on the existing rail.
    actor = _actor([{"criterion": "score persists", "status": "supported",
                     "evidence_refs": ["claim_2"]}])
    annotate_criteria_evidence_resolution([actor], _PACKET)
    result = _result([actor])
    assert task_acceptance_is_clean(result) is False
    assert result.aggregate_signal == "PASS" and actor["parsed"]["verdict"] == "PASS"
    # One REAL exhibit alongside the unsupported claim is still enough.
    assert resolve_criteria_evidence_refs(
        [{"criterion": "score persists", "status": "supported",
          "evidence_refs": ["claim_2", "verification_receipts[1]"]}],
        vocab,
    )[0]["supported_evidence_resolves"] is True


def test_claims_without_a_host_support_table_are_never_self_supporting():
    """No `acceptance_support_refs` in the packet means the host attested no
    support at all — claim ids fail CLOSED rather than resolving by default."""
    packet = {"task_contract": {"acceptance_claims": [{"id": "claim_1", "claim": "boots"}]}}
    vocab = acceptance_evidence_ref_vocabulary(packet)
    assert vocab["claim_1"] == "claim_id_unsupported"
    assert resolve_criteria_evidence_refs(
        [{"criterion": "c", "status": "supported", "evidence_refs": ["claim_1"]}], vocab,
    )[0]["supported_evidence_resolves"] is False


def test_annotation_marks_only_actors_with_unresolved_refs():
    actors = [
        _actor([{"criterion": "c", "status": "supported", "evidence_refs": ["claim_1"]}]),
        _actor([{"criterion": "c", "status": "supported", "evidence_refs": ["hallucinated"]}]),
    ]
    annotate_criteria_evidence_resolution(actors, _PACKET)
    assert "criteria_refs_unresolved" not in actors[0]
    rows = actors[1]["criteria_refs_unresolved"]
    assert rows[0]["supported_evidence_resolves"] is False


def test_clean_gate_demotes_only_the_clean_bit_never_the_verdict():
    criteria = [{"criterion": "c", "status": "supported", "evidence_refs": ["hallucinated"]}]
    annotated = _actor(criteria, unresolved_rows=[
        {"criterion": "c", "refs": [{"ref": "hallucinated", "resolved_as": ""}],
         "supported_evidence_resolves": False},
    ])
    result = _result([annotated])
    assert task_acceptance_is_clean(result) is False
    # The verdict, signal and aggregate are untouched: the demotion lands on the
    # EXISTING non-clean rails (finalized_unaccepted / bounded capsule), never
    # FAIL/veto/malformed — this cannot create the v6.71.1 unpassable-loop class.
    assert result.aggregate_signal == "PASS"
    assert annotated["signal"] == "PASS"
    assert annotated["parsed"]["verdict"] == "PASS"


def test_clean_gate_accepts_partial_resolution_and_historical_rows():
    criteria = [{"criterion": "c", "status": "supported",
                 "evidence_refs": ["verification_receipts[0]", "made-up"]}]
    # >=1 ref resolved -> the disclosure row exists but the criterion still counts.
    partially = _actor(criteria, unresolved_rows=[
        {"criterion": "c",
         "refs": [{"ref": "verification_receipts[0]", "resolved_as": "verification_receipt"},
                  {"ref": "made-up", "resolved_as": ""}],
         "supported_evidence_resolves": True},
    ])
    assert task_acceptance_is_clean(_result([partially])) is True
    # Historical rows carry no annotation (forward-only): the gate must not
    # invent a demotion for them.
    historical = _actor([{"criterion": "c", "status": "supported",
                          "evidence_refs": ["free-form old ref"]}])
    assert task_acceptance_is_clean(_result([historical])) is True


def test_require_criterion_evidence_knob_is_deleted():
    import pathlib

    for rel in ("ouroboros/loop.py", "ouroboros/tools/review.py"):
        source = pathlib.Path(rel).read_text(encoding="utf-8")
        assert '"require_criterion_evidence"' not in source, rel
    # The evidence condition is unconditional: a solved PASS without criteria is
    # not clean regardless of any policy content.
    knobless = _actor(None)
    knobless["parsed"].pop("criteria_used")
    assert task_acceptance_is_clean(_result([knobless])) is False


def test_reviewer_prompt_names_the_ref_vocabulary_once():
    from ouroboros.review_execution import _render_prompt_parts
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot

    request = ReviewRequest(
        surface="task_acceptance", goal="g", subject="s",
        policy={"classify_outcome_tier": True, "min_successful_slots": 1},
    )
    stable, _task, _dynamic = _render_prompt_parts(request, ReviewSlot("s0", "m"))
    assert "EXACT match" in stable
    assert "verification_receipts[i]" in stable
    assert "task_contract.acceptance_claims" in stable
    # Non-acceptance surfaces carry no vocabulary line.
    generic = ReviewRequest(surface="commit_review", goal="g", subject="s")
    g_stable, _t, _d = _render_prompt_parts(generic, ReviewSlot("s0", "m"))
    assert "verification_receipts[i]" not in g_stable


def test_annotation_rides_the_panel_and_never_blocks_it(tmp_path):
    """End-to-end through run_review_request: the annotation lands on actor rows
    while quorum participation and the aggregate stay untouched."""
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request

    class _PassWithBadRefsLLM:
        def chat(self, **_kwargs):
            return {"content": json.dumps({
                "verdict": "PASS", "outcome_tier": "solved",
                "completion_coach": "ship",
                "criteria_used": [{"criterion": "c", "status": "supported",
                                   "evidence_refs": ["hallucinated-ref"]}],
                "findings": [], "summary": "ready",
            })}, {}

    result = run_review_request(
        ReviewRequest(
            surface="task_acceptance", goal="g", subject="candidate",
            evidence=dict(_PACKET),
            policy={"classify_outcome_tier": True, "min_successful_slots": 1},
            task_id="dq5-e2e",
        ),
        slots=[ReviewSlot("s0", "m-0")],
        drive_root=tmp_path,
        llm=_PassWithBadRefsLLM(),
    )

    assert result.aggregate_signal == "PASS"          # verdict untouched
    actor = result.actors[0]
    assert actor["quorum_contribution"] is True        # quorum untouched
    rows = actor["criteria_refs_unresolved"]
    assert rows[0]["supported_evidence_resolves"] is False
    assert task_acceptance_is_clean(result) is False   # only the clean bit demotes
