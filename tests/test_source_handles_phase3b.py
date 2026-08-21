"""Production-shaped source-handle regressions for continuity Phase 3B."""

from __future__ import annotations

import json
from types import SimpleNamespace

from ouroboros import context_compaction as compaction
from ouroboros.artifacts import collect_task_artifact_records
from ouroboros.context_budget import ContextReclaimRequest
from ouroboros.consolidator import consolidate_scratchpad
from ouroboros.loop_tool_execution import process_tool_results
from ouroboros.memory import Memory
from ouroboros.review_evidence import build_task_acceptance_evidence
from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request
from ouroboros.tools.core import _read_file
from ouroboros.tools.registry import ToolContext


def _tool_ctx(tmp_path, *, task_id: str = "source-handles") -> ToolContext:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return ToolContext(repo_dir=repo, drive_root=tmp_path, task_id=task_id)


def _source_ref_from_visible_result(text: str) -> dict:
    prefix = "FULL_RESULT_SOURCE_JSON="
    line = next(line for line in text.splitlines() if line.startswith(prefix))
    return json.loads(line[len(prefix):])


def _read_source(ctx: ToolContext, ref: dict, *, start_char: int = 0) -> str:
    read = ref["read"]
    assert read["tool"] == "read_file"
    args = dict(read["arguments"])
    args["start_char"] = start_char
    return _read_file(ctx, **args)


def test_fifo_eviction_keeps_exact_block_and_current_scratchpad_names_reader(tmp_path):
    memory = Memory(tmp_path)
    contents = [f"scratch-source-{index}" for index in range(11)]
    for content in contents:
        memory.append_scratchpad_block(content, source="phase3b-test")

    current = memory.load_scratchpad()
    assert "read_file(root='runtime_data', path='memory/scratchpad_journal.jsonl'" in current

    journal = _read_file(
        _tool_ctx(tmp_path),
        root="runtime_data",
        path="memory/scratchpad_journal.jsonl",
    )
    rows = [json.loads(line) for line in journal.splitlines()[1:] if line.startswith("{")]
    evicted = [row for row in rows if row.get("type") == "block_evicted"]
    assert [row["evicted_block_content"] for row in evicted] == [contents[0]]


class _ConsolidationLLM:
    def chat(self, **_kwargs):
        return {
            "content": json.dumps({
                "knowledge_entries": [],
                "compressed_block": "compressed working memory",
            })
        }, {"prompt_tokens": 10, "completion_tokens": 5}


def test_scratchpad_consolidation_journals_exact_replaced_blocks_and_ref(tmp_path):
    memory = Memory(tmp_path)
    original = []
    for index in range(4):
        content = f"block-{index}-" + (chr(97 + index) * 8_000)
        original.append(memory.append_scratchpad_block(content, source=f"source-{index}"))

    usage = consolidate_scratchpad(
        memory,
        tmp_path / "memory" / "knowledge",
        _ConsolidationLLM(),
    )
    assert usage == {"prompt_tokens": 10, "completion_tokens": 5}

    blocks = memory.load_scratchpad_blocks()
    consolidated = blocks[0]
    assert consolidated["source"] == "consolidation"
    source_ref = consolidated["metadata"]["source_ref"]
    assert source_ref["read"] == {
        "tool": "read_file",
        "arguments": {
            "root": "runtime_data",
            "path": "memory/scratchpad_journal.jsonl",
            "start_line": 1,
        },
    }
    assert source_ref["entry_id"]

    journal = _read_file(
        _tool_ctx(tmp_path),
        root="runtime_data",
        path="memory/scratchpad_journal.jsonl",
    )
    rows = [json.loads(line) for line in journal.splitlines()[1:] if line.startswith("{")]
    entry = next(row for row in rows if row.get("entry_id") == source_ref["entry_id"])
    assert entry["type"] == "blocks_consolidated"
    assert entry["source_blocks"] == original[:2]
    assert source_ref["entry_id"] in memory.load_scratchpad()


def _project_large_result(tmp_path, *, tool_name: str, call_id: str, result: str):
    ctx = _tool_ctx(tmp_path, task_id="large-result")
    messages: list[dict] = []
    trace = {"tool_calls": []}
    tools = SimpleNamespace(_ctx=ctx)
    process_tool_results(
        [{
            "fn_name": tool_name,
            "tool_call_id": call_id,
            "result": result,
            "is_error": False,
            "tool_args": {"cmd": "non-idempotent-operation"},
            "args_for_log": {"cmd": "non-idempotent-operation"},
            "trace_ref": {"manifest_ref": {"path": "private-only"}},
            "result_meta": {"status": "ok"},
        }],
        messages,
        trace,
        emit_progress=lambda _message: None,
        tools=tools,
    )
    return ctx, messages[0]["content"], trace["tool_calls"][0]


def test_100k_non_idempotent_command_has_exact_actor_read_handle(tmp_path):
    decisive_suffix = "\nDECISIVE_SUFFIX: transaction committed but verification FAILED"
    full = "command-issued-once\n" + ("x" * 100_000) + decisive_suffix
    ctx, visible, trace_row = _project_large_result(
        tmp_path,
        tool_name="run_command",
        call_id="call-non-idempotent",
        result=full,
    )

    assert decisive_suffix not in visible
    assert "Do not rerun this tool to recover omitted output." in visible
    ref = _source_ref_from_visible_result(visible)
    assert trace_row["result_partial"] is True
    assert trace_row["result_source_ref"] == ref
    assert ref["root"] == "artifact_store"
    assert ref["size"] == len(full.encode("utf-8"))
    assert decisive_suffix in _read_source(ctx, ref, start_char=95_000)


def test_large_extension_result_uses_same_exact_actor_read_handle(tmp_path):
    decisive_suffix = "\nEXTENSION_DECISION=DENY"
    token = "sk-" + ("secret" * 8)
    full = json.dumps({
        "payload": "y" * 20_000, "api_key": token, "decision": decisive_suffix,
    })
    ctx, visible, trace_row = _project_large_result(
        tmp_path,
        tool_name="ext_demo_large_result",
        call_id="call-extension",
        result=full,
    )

    assert decisive_suffix not in visible
    ref = _source_ref_from_visible_result(visible)
    assert trace_row["result_partial"] is True
    assert trace_row["result_source_ref"] == ref
    recovered = _read_source(ctx, ref)
    assert full in recovered
    evidence = build_task_acceptance_evidence(
        ctx, llm_trace={"tool_calls": [trace_row]},
        drive_root=tmp_path, task_id="large-result",
    )
    assert token not in json.dumps(evidence, ensure_ascii=False)
    assert "***REDACTED***" in evidence["tool_trajectory"][0]["result"]
    assert collect_task_artifact_records(tmp_path, "large-result") == []


def _unit(call_id: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": f"reasoning-{call_id}",
            "tool_calls": [{
                "id": call_id,
                "function": {
                    "name": "run_command",
                    "arguments": json.dumps({"cmd": "one-shot"}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": "z" * 6_000 + f"-exact-result-tail-{call_id}",
        },
    ]


def test_context_capsule_checkpoint_is_actor_readable_and_dangling_is_explicit(
    monkeypatch, tmp_path,
):
    messages = _unit("checkpointed")
    request = ContextReclaimRequest(
        route_fp="main-route",
        round_id="round-1",
        transcript_sha256=compaction.context_reclaim_transcript_sha256(messages),
        measurement_basis="cold_estimate",
        measurement_density=1.0,
        reclaim_goal_tokens=100,
        allow_partial_shrink=True,
    )
    monkeypatch.setattr(compaction, "_summarizer_spec", lambda: {
        "model": "summary-model",
        "resolved_model": "summary-model",
        "provider": "test",
        "route_fp": "summary-route",
        "effort": "low",
        "output_budget": 32_768,
        "use_local": False,
    })
    monkeypatch.setattr(
        compaction,
        "_call_summarizer",
        lambda parts, **_kwargs: {
            part.source_id: f"summary {part.sha256}" for part in parts
        },
    )

    rebuilt, receipt, _usage = compaction.compact_tool_history_llm(
        messages,
        request=request,
        drive_root=tmp_path,
        task_id="checkpoint-task",
        keep_recent=0,
        negative_memo=set(),
    )
    assert receipt.status == "applied"
    capsule = rebuilt[0]["content"][0]["_context_capsule"]
    ref = capsule["checkpoint_ref"]
    assert ref["root"] == "artifact_store"
    ctx = _tool_ctx(tmp_path, task_id="checkpoint-task")
    recovered = _read_source(ctx, ref)
    checkpoint = json.loads(recovered.split("\n", 1)[1])
    assert checkpoint["messages"] == messages

    source_path = tmp_path / "task_results" / "artifacts" / "checkpoint-task" / ref["path"]
    source_path.unlink()
    assert "NOT_FOUND" in _read_source(ctx, ref)


class _MustNotReviewPartial:
    def __init__(self):
        self.calls = 0

    def chat(self, **_kwargs):
        self.calls += 1
        raise AssertionError("an unresolved partial source must not reach a clean reviewer")


def test_task_acceptance_abstains_before_review_on_unresolved_partial_source(tmp_path):
    full = "decision-input\n" + ("p" * 20_000) + "\nDECISIVE_ACCEPTANCE_SUFFIX=FAIL"
    ctx, _visible, trace_row = _project_large_result(
        tmp_path, tool_name="ext_acceptance_probe", call_id="acceptance-source", result=full,
    )
    trace = {"tool_calls": [trace_row]}
    complete_evidence = build_task_acceptance_evidence(
        ctx, llm_trace=trace, drive_root=tmp_path, task_id="large-result",
    )
    assert complete_evidence["tool_trajectory"][0]["result_complete"] is True
    assert "DECISIVE_ACCEPTANCE_SUFFIX=FAIL" in complete_evidence["tool_trajectory"][0]["result"]

    source_ref = trace_row["result_source_ref"]
    source_path = tmp_path / "task_results" / "artifacts" / "large-result" / source_ref["path"]
    source_path.unlink()
    evidence = build_task_acceptance_evidence(
        ctx, llm_trace=trace, drive_root=tmp_path, task_id="large-result",
    )
    assert evidence["__unresolved_partial_artifacts__"][0]["status"] == "source_unavailable"

    llm = _MustNotReviewPartial()
    result = run_review_request(
        ReviewRequest(
            surface="task_acceptance",
            goal="decide from evidence",
            subject="candidate",
            evidence=evidence,
            policy={"min_successful_slots": 1},
            task_id="acceptance-partial",
        ),
        slots=[ReviewSlot(slot_id="slot", model="review-model")],
        drive_root=tmp_path,
        llm=llm,
    )

    assert llm.calls == 0
    assert result.aggregate_signal == "DEGRADED"
    assert result.degraded is True
    assert result.actors[0]["status"] == "ok"
    assert result.actors[0]["signal"] == "DEGRADED"
