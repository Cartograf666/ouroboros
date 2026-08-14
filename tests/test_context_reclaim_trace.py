"""Focused trace-correlation coverage for reclaim provenance."""

from ouroboros import context_compaction as cc
from ouroboros.loop_tool_execution import process_tool_results


def test_tool_trace_row_carries_exact_tool_call_id_beside_trace_ref():
    trace_ref = {
        "manifest_ref": {"path": "calls/tool.json", "sha256": "a" * 64},
    }
    llm_trace = {"tool_calls": []}
    messages = []

    errors = process_tool_results(
        [{
            "fn_name": "read_file",
            "tool_call_id": "call-correlated",
            "result": "complete actor-visible result",
            "is_error": False,
            "args_for_log": {"path": "README.md"},
            "tool_args": {"path": "README.md"},
            "trace_ref": trace_ref,
            "result_meta": {"status": "ok"},
        }],
        messages,
        llm_trace,
        emit_progress=lambda _message: None,
    )

    assert errors == 0
    assert messages[0]["tool_call_id"] == "call-correlated"
    row = llm_trace["tool_calls"][0]
    assert row["tool_call_id"] == "call-correlated"
    assert row["trace_ref"] is trace_ref


def test_materializer_resolves_only_matching_tool_call_trace_refs():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call-a", "function": {"name": "a", "arguments": "x" * 1_000}},
                {"id": "call-b", "function": {"name": "b", "arguments": "y" * 1_000}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-a", "content": "a" * 1_000},
        {"role": "tool", "tool_call_id": "call-b", "content": "b" * 1_000},
    ]
    ref_a = {"path": "calls/a.json", "sha256": "a" * 64}
    ref_b = {"path": "calls/b.json", "sha256": "b" * 64}
    unrelated = {"path": "calls/other.json", "sha256": "f" * 64}

    unit = cc._atomic_units(
        messages,
        trace_refs_by_tool_call_id={
            "call-a": ref_a,
            "call-b": ref_b,
            "call-other": unrelated,
        },
    )[0]

    assert unit.source_refs == (ref_a, ref_b)
    assert unrelated not in unit.source_refs
