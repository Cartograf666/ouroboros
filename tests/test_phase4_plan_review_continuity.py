from __future__ import annotations

import json

import httpx
import pytest

from tests import test_plan_review_engine as plan_review_engine


plan_review_harness_fixture = pytest.fixture(name="_harness")(
    plan_review_engine.harness.__wrapped__
)


def _finding(index: int, klass: str = "note") -> dict:
    return {
        "id": f"f{index}",
        "class": klass,
        "breaks": "goal" if klass == "blocking" else "",
        "locator": "",
        "summary": f"finding {index}",
        "recommendation": "repair it",
    }


def test_33rd_blocking_finding_is_aggregated() -> None:
    from ouroboros.tools.plan_spec import aggregate, validate_findings

    raw = [_finding(i) for i in range(1, 33)] + [_finding(33, "blocking")]
    findings, disclosures, _seen = validate_findings(
        raw, spec_ids={"goal"}, seen_locators=(), slot="slot_a",
    )
    result = aggregate([
        {"slot": "slot_a", "model": "m/a", "ok": True, "findings": findings},
    ], quorum=1)

    assert disclosures == []
    assert len(findings) == 33
    assert result["aggregate"] == "REVISE_PLAN"
    assert result["counts"]["blocking"] == 1
    assert result["findings"][-1]["finding_id"] == "slot_a:f33"


def test_exact_evidence_selectors_return_the_requested_slice(tmp_path) -> None:
    from ouroboros.tools.plan_evidence import resolve_evidence

    source = tmp_path / "evidence.txt"
    source.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    manifest = resolve_evidence(
        ["evidence.txt::lines=3-4", "evidence.txt::tail=5"],
        active_root=tmp_path,
        allowed_roots=[tmp_path],
    )

    assert manifest["omissions"] == []
    assert manifest["attached"][0]["text"] == "three\nfour\n"
    assert manifest["attached"][0]["selector"] == {
        "kind": "line_range", "start": 3, "end": 4,
    }
    assert manifest["attached"][1]["text"] == "five\n"
    assert manifest["attached"][1]["selector"] == {"kind": "tail", "bytes": 5}


def test_symbol_selector_uses_the_qualified_definition(tmp_path) -> None:
    from ouroboros.tools.plan_evidence import resolve_evidence

    source = tmp_path / "subject.py"
    source.write_text(
        "class First:\n    def decide(self):\n        return 'wrong'\n\n"
        "class Second:\n    def decide(self):\n        return 'exact'\n",
        encoding="utf-8",
    )
    manifest = resolve_evidence(
        ["subject.py::symbol=Second.decide"], active_root=tmp_path,
        allowed_roots=[tmp_path],
    )

    assert manifest["omissions"] == []
    assert "return 'exact'" in manifest["attached"][0]["text"]
    assert "return 'wrong'" not in manifest["attached"][0]["text"]


def test_requested_tail_preempts_a_full_120k_declared_pack(_harness) -> None:
    from tests.test_plan_review_engine import CLEAN, DECK_SPEC, _call, _finding, _user_text

    for index, char in enumerate("abc", start=1):
        (_harness.workspace / f"bulk-{index}.txt").write_text(char * 40_000, encoding="utf-8")
    decisive = _harness.workspace / "decisive.txt"
    decisive.write_text("x" * 50_000 + "DECISIVE_TAIL\n", encoding="utf-8")
    spec = {**DECK_SPEC, "evidence": [f"bulk-{index}.txt" for index in range(1, 4)]}
    ask = json.dumps([
        _finding(
            "tail", "need_evidence", breaks="goal", locator="decisive.txt::tail=64",
            summary="need the decisive tail",
        )
    ])
    _harness.install({"s1": ask, "s2": CLEAN, "s3": CLEAN})
    _call(_harness.make_ctx(), spec=spec)

    substrate = _harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    _call(_harness.make_ctx(), spec=spec)

    current_user = _user_text(substrate.calls[0]["request"].slot_messages["s1"][-1]["content"])
    assert "DECISIVE_TAIL" in current_user


def test_missing_requested_evidence_cannot_close_clean(_harness) -> None:
    from tests.test_plan_review_engine import CLEAN, _call, _control, _finding

    ask = json.dumps([
        _finding(
            "f1", "need_evidence", breaks="goal", locator="missing.md::lines=1-2",
            summary="read the exact lines",
        )
    ])
    _harness.install({"s1": ask, "s2": CLEAN, "s3": CLEAN})
    assert _control(_call(_harness.make_ctx())) == {
        "outcome": "REVIEW_REQUIRED", "closed": False,
    }

    substrate = _harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    out = _call(_harness.make_ctx())

    assert "cannot_verify" in out
    assert _control(out)["closed"] is False
    assert substrate.calls == []


def test_wave_9_and_65_remain_exactly_readable_after_hot_trimming(tmp_path) -> None:
    from ouroboros.task_results import load_plan_review_state, record_plan_review_wave
    from ouroboros.tools.plan_review_runtime import (
        persist_plan_review_wave_artifact,
        read_plan_review_wave_artifact,
    )

    task_id = "task-review"
    refs = {}
    for index in range(1, 66):
        fingerprint = f"{index:064x}"
        exact = {
            "schema_version": 1,
            "cycle_index": index,
            "request_fingerprint": fingerprint,
            "findings": [{"id": f"tail-{index}", "summary": "x" * 5000}],
            "reviewer_outputs": [{"slot_id": "s1", "text": f"exact-wave-{index}"}],
        }
        ref = persist_plan_review_wave_artifact(tmp_path, task_id, exact)
        refs[index] = ref
        record_plan_review_wave(tmp_path, task_id, {
            "schema_version": 2,
            "cycle_index": index,
            "request_fingerprint": fingerprint,
            "spec": {"goal": "g"},
            "findings": exact["findings"],
            "aggregate": "GREEN",
            "closed": True,
            "paid": True,
            "dispositions": [],
            "wave_artifact": ref,
        })

    state = load_plan_review_state(tmp_path, task_id)
    assert len(state["waves"]) == 64
    assert sum(1 for wave in state["waves"] if not wave.get("compact")) == 8
    wave9 = next(w for w in state["waves"] if w["cycle_index"] == 9)
    assert wave9["compact"] is True
    assert wave9["wave_artifact"] == refs[9]
    assert read_plan_review_wave_artifact(tmp_path, task_id, refs[9])["reviewer_outputs"][0]["text"] == "exact-wave-9"
    assert read_plan_review_wave_artifact(tmp_path, task_id, refs[65])["reviewer_outputs"][0]["text"] == "exact-wave-65"


def test_api_chat_continuation_uses_exact_slot_transcript() -> None:
    from ouroboros.review_execution import ApiChatReviewExecutor, ReviewAssignment
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot

    prior = [
        {"role": "system", "content": "system-v1"},
        {"role": "user", "content": "plan-v1"},
        {"role": "assistant", "content": "need exact tail"},
        {"role": "user", "content": "plan-v1 + exact tail"},
    ]
    request = ReviewRequest(
        surface="plan_review",
        goal="review",
        task_id="task-1",
        messages=[{"role": "user", "content": "wrong common transcript"}],
        slot_messages={"slot_a": prior},
    )
    slot = ReviewSlot(slot_id="slot_a", model="m/a")
    executor = ApiChatReviewExecutor(ReviewAssignment(request=request, slot=slot))

    assert executor.messages == prior
    assert executor._kwargs()["model"] == "m/a"


def test_claudexor_gateway_thread_turn_contract(monkeypatch) -> None:
    from ouroboros.gateways.claudexor import ClaudexorGateway, DaemonEndpoint

    seen: list[tuple[str, str, dict, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        seen.append((request.method, request.url.path, body, request.headers.get("Idempotency-Key", "")))
        if request.url.path == "/v2/threads":
            return httpx.Response(200, json={"id": "thread-1"})
        if request.url.path == "/v2/threads/thread-1/turns":
            return httpx.Response(200, json={
                "jobId": "job-2", "runId": "run-2", "runDir": "/tmp/run-2",
                "threadId": "thread-1", "turnId": "turn-2",
            })
        if request.url.path == "/v2/threads/thread-1":
            return httpx.Response(200, json={
                "thread": {"id": "thread-1", "headRunId": "run-2"},
                "sessions": [{
                    "id": "session-1", "threadId": "thread-1", "harnessId": "claude",
                    "profileId": "profile-b", "state": "live",
                }],
                "turns": [{
                    "id": "turn-2", "threadId": "thread-1", "runId": "run-2",
                    "continuity": {
                        "kind": "packet", "packetTurns": 1, "summarized": False,
                        "laneSwitchedFrom": {"harness": "claude", "profileId": "profile-a"},
                    },
                }],
            })
        return httpx.Response(404, json={"code": "not_found", "message": "no"})

    gateway = ClaudexorGateway(DaemonEndpoint(host="127.0.0.1", port=1, token="token"))
    gateway._client.close()
    gateway._client = httpx.Client(
        base_url="http://127.0.0.1:1", transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer token"},
    )
    try:
        thread = gateway.create_thread({
            "scope": {"kind": "project", "root": "/repo"},
            "mode": "ask", "authPreference": "subscription",
            "primaryHarness": "claude", "eligibleHarnesses": ["claude"],
            "credentialProfileId": "profile-a", "access": "readonly",
        }, idempotency_key="thread-key")
        turn = gateway.start_thread_turn(
            thread["id"], {"prompt": "exact evidence", "mode": "ask"},
            idempotency_key="turn-key",
        )
        detail = gateway.get_thread(thread["id"])
    finally:
        gateway.close()

    assert turn["threadId"] == "thread-1" and turn["runId"] == "run-2"
    assert detail["turns"][0]["continuity"]["kind"] == "packet"
    assert detail["sessions"][0]["profileId"] == "profile-b"
    assert seen[0][3] == "thread-key" and seen[1][3] == "turn-key"


def test_continued_thread_does_not_repin_the_exhausted_profile() -> None:
    from ouroboros.review_thread_continuity import start_review_thread_turn

    captured = {}

    class Gateway:
        def start_thread_turn(self, thread_id, request, *, idempotency_key):
            captured.update({
                "thread_id": thread_id, "request": request,
                "idempotency_key": idempotency_key,
            })
            return {"runId": "run-2", "threadId": thread_id, "turnId": "turn-2"}

    start_review_thread_turn(Gateway(), "thread-1", {
        "prompt": "continue", "model": "fable", "harnesses": ["claude"],
        "credentialProfileId": "profile-a", "_thread_id": "thread-1",
        "scope": {"kind": "project", "root": "/repo"},
    }, idempotency_key="turn-key")

    assert captured["request"]["model"] == "fable"
    assert captured["request"]["harnesses"] == ["claude"]
    assert "credentialProfileId" not in captured["request"]


def test_agent_session_continuation_passes_the_real_thread_id(monkeypatch, tmp_path) -> None:
    from ouroboros.review_execution import AgentSessionReviewExecutor, ReviewAssignment
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot

    captured = {}

    def fake_run(*, prompt, root, custody_drive, invocation):
        captured.update({"prompt": prompt, "root": root, "invocation": invocation})
        return {
            "run_id": "run-2", "thread_id": "thread-1", "turn_id": "turn-2",
            "thread_receipt": {"continuity": {"kind": "native_resume"}},
            "text": "[]\nNO_FINDINGS", "conformance": "passed", "schema_asked": True,
            "custody_durable": True, "settlement": "settled", "route_id": "claude",
            "effective_route_ids": ["claude"], "model": "fable", "spend": 0.0,
            "spend_estimated": False, "applied_profile": "profile-a", "applied_access": "readonly",
            "auth_route_receipt": {
                "requested": "subscription", "effective": "subscription",
                "reason": "quota_exhausted", "profileId": "profile-a",
            },
        }

    monkeypatch.setattr("ouroboros.review_execution.run_delegated_review_session", fake_run)
    request = ReviewRequest(
        surface="plan_review", goal="review", task_id="task-1",
        session_root=str(tmp_path), session_task="review exact evidence",
        session_threads={"slot_a": "thread-1"},
        policy={"output_contract": "return findings"},
    )
    slot = ReviewSlot(
        slot_id="slot_a", model="fable", route="agent_session",
        session_target="claude=fable", session_profile="profile-a",
    )
    result = AgentSessionReviewExecutor(
        ReviewAssignment(request=request, slot=slot, custody_root=tmp_path)
    ).execute()

    assert captured["invocation"].thread_id == "thread-1"
    assert captured["invocation"].use_thread is True
    assert result.usage["review_thread_id"] == "thread-1"
    assert result.usage["review_thread_receipt"]["continuity"]["kind"] == "native_resume"
    assert result.usage["auth_route_receipt"]["reason"] == "quota_exhausted"


def test_disposition_supersedes_the_exact_wave_before_hot_state(_harness) -> None:
    from ouroboros.task_results import load_plan_review_state
    from ouroboros.tools import plan_review as pr
    from ouroboros.tools.plan_review_artifacts import read_wave
    from tests.test_plan_review_engine import CLEAN, _call, _finding

    note = json.dumps([_finding("n1", "note")])
    _harness.install({"s1": note, "s2": CLEAN, "s3": CLEAN})
    _call(_harness.make_ctx())
    prior = load_plan_review_state(_harness.drive, "task-1")["waves"][-1]
    prior_ref = prior["wave_artifact"]

    pr._handle_plan_task(_harness.make_ctx(), review_disposition={
        "review_fingerprint": prior["request_fingerprint"],
        "items": [{"finding_id": "s1:n1", "decision": "accept", "rationale": "will do"}],
    })

    stored = load_plan_review_state(_harness.drive, "task-1")["waves"][-1]
    assert stored["wave_artifact"] != prior_ref
    exact = read_wave(_harness.drive, "task-1", stored["wave_artifact"])
    assert exact["dispositions"][0]["finding_id"] == "s1:n1"
    assert exact["supersedes_wave_artifact"] == prior_ref
    assert exact["artifact_meta"]["retention_owner"] == "task_artifact_store"


def test_33rd_blocker_is_dispositionable(_harness) -> None:
    from ouroboros.task_results import load_plan_review_state
    from ouroboros.tools import plan_review as pr
    from tests.test_plan_review_engine import CLEAN, _call

    findings = json.dumps([_finding(index) for index in range(1, 33)] + [_finding(33, "blocking")])
    _harness.install({"s1": findings, "s2": CLEAN, "s3": CLEAN})
    _call(_harness.make_ctx())
    wave = load_plan_review_state(_harness.drive, "task-1")["waves"][-1]

    out = pr._handle_plan_task(_harness.make_ctx(), review_disposition={
        "review_fingerprint": wave["request_fingerprint"],
        "items": [{"finding_id": "s1:f33", "decision": "reject", "rationale": "not valid"}],
    })

    assert "unknown finding ids" not in out
    assert "blocking_finding_below_quorum_stays_open" in out
