"""One compiler for configured-session work orders and host assignment context."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Mapping

_WORK_ORDER_CHARS = 40_000
# Historical import name retained for tests/callers which only need the public
# wire budget. It is no longer a per-field truncation limit.
_FIELD_CHARS = _WORK_ORDER_CHARS


class WorkOrderBudgetExceeded(ValueError):
    """A complete work order cannot fit the one explicit wire budget."""

    def __init__(self, *, chars: int, sha256_hex: str) -> None:
        super().__init__(f"complete work order is {chars} characters (budget {_WORK_ORDER_CHARS})")
        self.chars = int(chars)
        self.sha256 = str(sha256_hex)
        self.limit = _WORK_ORDER_CHARS


def _text(value: Any) -> str:
    if isinstance(value, list):
        value = "\n".join(f"- {item}" for item in value if str(item).strip())
    return str(value or "").strip()


def assignment_instructions(ctx: Any) -> str:
    """Host-authored complete normalized contract for every direct delegate start."""

    contract = getattr(ctx, "task_contract", None)
    if not isinstance(contract, dict) or not contract:
        meta = getattr(ctx, "task_metadata", {})
        raw = meta.get("task_contract") if isinstance(meta, dict) else None
        contract = raw if isinstance(raw, dict) else {}
    if contract:
        from ouroboros.contracts.task_contract import build_task_contract

        contract = build_task_contract({"task_contract": contract})
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
    if contract:
        parts.append(
            "HOST TASK CONTRACT AUTHORITY (complete normalized JSON; exact strings are authority):\n"
            + json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    return "\n\n".join(parts)


def _render_external_work_order(task: Mapping[str, Any]) -> str:
    contract = task.get("task_contract") if isinstance(task.get("task_contract"), dict) else {}
    assignment_context = "" if task.get("context") is None else str(task.get("context"))
    inherited_context = "" if contract.get("context") is None else str(contract.get("context"))
    context_sections = []
    if assignment_context:
        context_sections.append("DELEGATED ASSIGNMENT CONTEXT\n" + assignment_context)
    if inherited_context and inherited_context != assignment_context:
        context_sections.append("INHERITED CALLER AUTHORITY CONTEXT\n" + inherited_context)
    represented_keys = {
        "objective", "context", "expected_output", "constraints", "acceptance_claims",
    }
    remaining_contract = {
        key: value for key, value in contract.items() if key not in represented_keys
    }
    sections: list[tuple[str, Any]] = [
        ("OBJECTIVE", task.get("objective") or contract.get("objective") or task.get("description")),
        ("PARENT CONTEXT / REFERENCES", "\n\n".join(context_sections)),
        ("EXPECTED OUTPUT", task.get("expected_output") or contract.get("expected_output")),
        ("CONSTRAINTS / NON-GOALS", task.get("constraints") or contract.get("constraints")),
        ("ACCEPTANCE CLAIMS", contract.get("acceptance_claims")),
        ("TASK CONTRACT AUTHORITY", remaining_contract),
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
        "origin_message_ref": task.get("origin_message_ref") if isinstance(task.get("origin_message_ref"), dict) else {},
    }
    rendered = []
    for title, value in sections:
        body = (
            str(value) if title == "PARENT CONTEXT / REFERENCES" and isinstance(value, str)
            else _text(value)
        )
        if body:
            rendered.append(f"{title}\n{body}")
    rendered.append(
        "HOST AUTHORITY BINDING (facts, not instructions to widen)\n"
        + _text(json.dumps(authority, ensure_ascii=False, sort_keys=True))
    )
    return "\n\n".join(rendered)


def compile_external_work_order(task: Mapping[str, Any]) -> str:
    """Compile one complete brief or refuse instead of sending a false prefix."""

    rendered = _render_external_work_order(task)
    if len(rendered) > _WORK_ORDER_CHARS:
        raise WorkOrderBudgetExceeded(
            chars=len(rendered), sha256_hex=sha256(rendered.encode("utf-8")).hexdigest(),
        )
    return rendered


def start_binding_fingerprints(ctx: Any, prompt: str) -> tuple[str, str]:
    """Digest the exact brief and the existing normalized task authority."""

    from ouroboros.delegate_recovery import authority_fingerprint_from_context

    return (
        sha256(str(prompt).encode("utf-8")).hexdigest(),
        authority_fingerprint_from_context(ctx),
    )


def work_order_fingerprint(task: Mapping[str, Any]) -> str:
    """Digest the complete canonical brief, including an over-budget one."""

    return sha256(_render_external_work_order(task).encode("utf-8")).hexdigest()


__all__ = [
    "WorkOrderBudgetExceeded", "assignment_instructions", "compile_external_work_order",
    "start_binding_fingerprints", "work_order_fingerprint",
]
