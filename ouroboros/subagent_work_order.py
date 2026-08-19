"""One compiler for configured-session work orders and host assignment context."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Mapping

from ouroboros.utils import truncate_within_limit

_FIELD_CHARS = 4_000


def _text(value: Any) -> str:
    if isinstance(value, list):
        value = "\n".join(f"- {item}" for item in value if str(item).strip())
    return truncate_within_limit(str(value or "").strip(), _FIELD_CHARS)


def assignment_instructions(ctx: Any) -> str:
    """Host-authored immutable objective/output block for every delegate start."""

    contract = getattr(ctx, "task_contract", None)
    if not isinstance(contract, dict) or not contract:
        meta = getattr(ctx, "task_metadata", {})
        raw = meta.get("task_contract") if isinstance(meta, dict) else None
        contract = raw if isinstance(raw, dict) else {}
    parts: list[str] = []
    objective = _text(contract.get("objective"))
    expected = _text(contract.get("expected_output"))
    if objective:
        parts.append(
            "HOST TASK OBJECTIVE (immutable contract; the prompt is one assignment inside it): "
            + objective
        )
    if expected:
        parts.append("HOST EXPECTED OUTPUT: " + expected)
    return "\n\n".join(parts)


def compile_external_work_order(task: Mapping[str, Any]) -> str:
    """Compile the scheduled child brief into the exact external leaf prompt."""

    contract = task.get("task_contract") if isinstance(task.get("task_contract"), dict) else {}
    sections: list[tuple[str, Any]] = [
        ("OBJECTIVE", task.get("objective") or contract.get("objective") or task.get("description")),
        ("PARENT CONTEXT / REFERENCES", task.get("context")),
        ("EXPECTED OUTPUT", task.get("expected_output") or contract.get("expected_output")),
        ("CONSTRAINTS / NON-GOALS", task.get("constraints") or contract.get("constraints")),
        ("ACCEPTANCE CLAIMS", contract.get("acceptance_claims")),
    ]
    authority = {
        "task_id": str(task.get("id") or ""),
        "parent_task_id": str(task.get("parent_task_id") or ""),
        "root_task_id": str(task.get("root_task_id") or ""),
        "workspace_root": str(task.get("workspace_root") or ""),
        "workspace_mode": str(task.get("workspace_mode") or ""),
        "task_constraint": task.get("task_constraint") if isinstance(task.get("task_constraint"), dict) else {},
        "allowed_resources": contract.get("allowed_resources") if isinstance(contract.get("allowed_resources"), dict) else {},
        "deadline_at": str(contract.get("deadline_at") or ""),
    }
    rendered = [
        f"{title}\n{body}"
        for title, value in sections
        if (body := _text(value))
    ]
    rendered.append(
        "HOST AUTHORITY BINDING (facts, not instructions to widen)\n"
        + _text(json.dumps(authority, ensure_ascii=False, sort_keys=True))
    )
    return "\n\n".join(rendered)


def start_binding_fingerprints(ctx: Any, prompt: str) -> tuple[str, str]:
    """Digest the exact brief and the existing normalized task authority."""

    from ouroboros.delegate_recovery import authority_fingerprint_from_context

    return (
        sha256(str(prompt).encode("utf-8")).hexdigest(),
        authority_fingerprint_from_context(ctx),
    )


def work_order_fingerprint(task: Mapping[str, Any]) -> str:
    """Digest the one canonical scheduled-child brief."""

    return sha256(compile_external_work_order(task).encode("utf-8")).hexdigest()


__all__ = [
    "assignment_instructions", "compile_external_work_order", "start_binding_fingerprints",
    "work_order_fingerprint",
]
