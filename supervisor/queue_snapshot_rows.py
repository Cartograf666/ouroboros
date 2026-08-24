"""What a PENDING queue row keeps across a restart.

A pending child has not run yet, so this projection is the ONLY carrier of its
scheduling intent: every field the scheduler decided but the child has not yet
acted on. A key missing here is silently re-decided by the install-wide policy
after a restart — which is why `SUBAGENT_INTENT_FIELDS` is pinned against this
projection by test, rather than trusted to stay in sync by review.

It lives beside the queue rather than inside it because it is a RECORD SHAPE,
not queue behaviour: the durable contract deserves one obvious home.
"""

from __future__ import annotations

from typing import Any, Dict


def pending_snapshot_row(t: Dict[str, Any]) -> Dict[str, Any]:
    """One pending queue row, exactly as it is written to the snapshot."""
    return {
        "id": t.get("id"), "type": t.get("type"), "priority": t.get("priority"),
        "attempt": t.get("_attempt"), "queued_at": t.get("queued_at"),
        "queue_seq": t.get("_queue_seq"),
        "task": {
            "id": t.get("id"), "type": t.get("type"), "chat_id": t.get("chat_id"),
            "text": t.get("text"), "priority": t.get("priority"),
            "depth": t.get("depth"), "description": t.get("description"),
            "objective": t.get("objective"), "title": t.get("title"),
            "expected_output": t.get("expected_output"),
            "constraints": t.get("constraints"), "role": t.get("role"),
            "context": t.get("context"), "parent_task_id": t.get("parent_task_id"),
            "root_task_id": t.get("root_task_id"), "session_id": t.get("session_id"),
            "actor_id": t.get("actor_id"), "delegation_role": t.get("delegation_role"),
            "workspace_root": t.get("workspace_root"), "workspace_mode": t.get("workspace_mode"),
            "project_id": t.get("project_id"),
            "allowed_resources": t.get("allowed_resources"), "deadline_at": t.get("deadline_at"),
            "task_contract": t.get("task_contract"),
            # Scheduling INTENT survives a restart and is all a PENDING child has;
            # `parent_model_lane` and the F9 admission fact `required_model_lane`
            # above all (R2-3). Pinned to SUBAGENT_INTENT_FIELDS by test_model_slot.
            "model_lane": t.get("model_lane"), "parent_model_lane": t.get("parent_model_lane"),
            "requested_model_lane": t.get("requested_model_lane"),
            "required_model_lane": t.get("required_model_lane"), "requested_executor": t.get("requested_executor"),
            # The owner's approved allocation for this child. A PENDING child
            # has nothing else naming its destination, so dropping it here
            # would silently re-route the work to the install-wide policy
            # across a restart — after the owner had already approved it.
            "routing_plan_item": t.get("routing_plan_item"),
            "routing_plan_item_unresolved": t.get("routing_plan_item_unresolved"),
            "effective_model_lane": t.get("effective_model_lane"),
            "model": t.get("model"), "use_local_model": t.get("use_local_model"),
            "effective_executor": t.get("effective_executor"), "tool_profile": t.get("tool_profile"),
            "executor_route": t.get("executor_route"), "reasoning_effort": t.get("reasoning_effort"),
            "capability_delta": t.get("capability_delta"),
            "task_group_id": t.get("task_group_id"),
            "task_group": t.get("task_group"),
            "subagent_envelope": t.get("subagent_envelope"), "configured_subagent": t.get("configured_subagent"),
            "memory_mode": t.get("memory_mode"), "drive_root": t.get("drive_root"), "parent_cognitive_route": t.get("parent_cognitive_route"), "subagent_availability": t.get("subagent_availability"),
            "child_drive_root": t.get("child_drive_root"),
            "budget_drive_root": t.get("budget_drive_root"),
            "task_constraint": t.get("task_constraint"),
            "metadata": t.get("metadata"), "origin_message_ref": t.get("origin_message_ref"),
            "origin_message_text": t.get("origin_message_text"), "_attempt": t.get("_attempt"),
            "review_reason": t.get("review_reason"), "review_source_task_id": t.get("review_source_task_id"),
            "_budget_pause": t.get("_budget_pause"),
            "budget_resumed_at": t.get("budget_resumed_at"),
        },
    }
