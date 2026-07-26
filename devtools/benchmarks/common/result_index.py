"""Result indexing utilities shared by benchmark adapters."""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any


def append_result_index(run_dir: pathlib.Path, row: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "result_index.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# Reason codes on which the RUNTIME stopped a task for a reason that is not "the task is
# finished". A truncated run and an honest failure are otherwise indistinguishable in a
# benchmark artefact, which is how an aggregator records `2/3` with no indication that a
# third of the run was cost-truncated. NOT an exhaustive failure taxonomy — only the class
# an auditor must never mistake for a capability result. SSOT for the vocabulary:
# `ouroboros.outcomes.BEST_EFFORT_REASON_CODES` + `loop._handle_budget_exceeded`.
RUNTIME_TRUNCATION_REASON_CODES = frozenset({
    "budget_exhausted", "max_rounds_exceeded", "task_timeout", "cancelled",
    "context_exhausted", "provider_unavailable", "llm_api_error", "rate_limited",
})


def runtime_terminal_disclosure(task_result: Any) -> dict[str, Any]:
    """Project the RUNTIME's OWN terminal reason out of a task-result payload.

    ``GET /api/tasks/<id>`` and the CLI's ``--result-json-out`` both carry ``reason_code``,
    ``outcome_axes`` and ``loop_outcome.resource_limit`` (``ouroboros.outcomes``,
    ``task_results.write_task_result``); adapters historically read them only on their OWN
    failure branch and hard-coded an adapter-stage literal on the success branch. This makes
    the runtime reason a first-class, always-present field.

    ``{"available": False}`` when the writer genuinely has no runtime result — a STATED gap.
    It never invents a reason, never touches reward, and never demotes an eval status: the
    evaluation really did run, so this is disclosure ADDED, not fact subtracted."""
    if not isinstance(task_result, dict) or not task_result:
        return {"available": False}
    loop_outcome = task_result.get("loop_outcome")
    loop_outcome = loop_outcome if isinstance(loop_outcome, dict) else {}
    resource_limit = task_result.get("resource_limit")
    if not isinstance(resource_limit, dict):
        candidate = loop_outcome.get("resource_limit")
        resource_limit = candidate if isinstance(candidate, dict) else {}
    axes = task_result.get("outcome_axes")
    axes = axes if isinstance(axes, dict) else {}
    execution = axes.get("execution") if isinstance(axes.get("execution"), dict) else {}
    reason_code = str(task_result.get("reason_code") or "")
    return {
        "available": True,
        "status": str(task_result.get("status") or ""),
        "reason_code": reason_code,
        "truncated": reason_code in RUNTIME_TRUNCATION_REASON_CODES,
        "degraded": bool(task_result.get("degraded") or loop_outcome.get("degraded")),
        "degraded_reason": str(
            task_result.get("degraded_reason") or loop_outcome.get("degraded_reason") or ""
        ),
        "execution_status": str(execution.get("status") or ""),
        "execution_reason_code": str(execution.get("reason_code") or ""),
        "resource_limit": resource_limit,
    }


def task_result_row(
    *,
    benchmark: str,
    instance_id: str,
    status: str,
    metadata: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Create a denominator-preserving per-task result row.

    Pass ``runtime_result=<task result payload>`` (metadata or keyword) wherever the adapter
    holds one: ``runtime_outcome`` then discloses why the RUNTIME stopped, independently of
    the adapter-stage ``reason_code`` this row's ``status`` describes."""
    meta = dict(metadata or {})
    for key, value in overrides.items():
        if value is not None:
            meta[key] = value
    return {
        "schema": "ouroboros.benchmark.task_result.v1",
        "ts_unix": time.time(),
        "benchmark": benchmark,
        "instance_id": str(instance_id),
        "status": status,
        "reason_code": str(meta.get("reason_code") or ""),
        "runtime_outcome": runtime_terminal_disclosure(meta.get("runtime_result")),
        "prediction_written": bool(meta.get("prediction_written")),
        "official_eval_status": str(meta.get("official_eval_status") or "not_run"),
        "output_paths": meta.get("output_paths") or {},
        "error": str(meta.get("error") or ""),
        "details": meta.get("details") or {},
    }


def write_result_index(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
