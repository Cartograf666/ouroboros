"""Thread branching, checkout diff and lifecycle routes (Gateway Boundary).

Thin transport only: every decision about branching lives in
:mod:`ouroboros.thread_branching`, every decision about the checkout registry in
:mod:`ouroboros.thread_worktrees`, every diff decision in
:mod:`ouroboros.gateway.task_diff`, and every registry mutation in
:mod:`ouroboros.project_threads_registry`. Nothing here decides anything.

These are OWNER surfaces reached through the gateway and deliberately NOT
LLM-callable tools (R10). Branching off, merging back, removing a checkout,
archiving and deleting are gestures with consequences for the owner's own folder
and history; putting them in the tool ABI would let the agent perform them on its
own reading of a conversation, and SYSTEM.md would grow a paragraph explaining
when not to.

They live in their own module rather than in ``gateway/projects.py`` because that
file is already near the module-size gate and because the thread lifecycle is a
coherent surface of its own — the split is the same one the registry took.

Refusal shape: the branching module answers with a TYPED reason and owner-facing
copy, so these routes pass it through with HTTP 409 (a precondition the owner can
resolve) or 404/400 where the request itself is wrong. A UI cannot branch on a
stack trace, and a 500 would tell the owner nothing about what to do next.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.gateway._helpers import json_exception, request_drive_root

log = logging.getLogger(__name__)

#: Reasons that mean "the request named something that does not exist".
_NOT_FOUND_REASONS = frozenset({"unknown_project", "unknown_thread"})
#: Reasons that mean "the request itself is malformed", as opposed to a state the
#: owner can resolve. Everything else is a PRECONDITION (409).
_BAD_REQUEST_REASONS = frozenset({"unknown_base"})


def _refusal_status(reason: str) -> int:
    if reason in _NOT_FOUND_REASONS:
        return 404
    if reason in _BAD_REQUEST_REASONS:
        return 400
    return 409


def _answer(outcome: Dict[str, Any]) -> JSONResponse:
    """Pass a typed outcome through, choosing the status from its reason."""
    if outcome.get("ok"):
        return JSONResponse(outcome)
    return JSONResponse(outcome, status_code=_refusal_status(str(outcome.get("reason") or "")))


async def _json_body(request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return body if isinstance(body, dict) else None


def _route_ids(request: Request) -> tuple:
    return (
        str(request.path_params.get("project_id") or "").strip(),
        str(request.path_params.get("thread_id") or "").strip(),
    )


# --------------------------------------------------------------------------- #
# BRANCH OFF / MERGE BACK / remove
# --------------------------------------------------------------------------- #

async def api_thread_branch_bases(request: Request) -> JSONResponse:
    """GET /api/projects/{project_id}/threads/{thread_id}/branch-bases.

    The OWNER's list of bases (A8): the current branch, every other branch, every
    tag, and the always-present "exactly as it is now" entry, which discloses
    whether choosing it would create a snapshot commit. Any commit-ish typed
    instead is accepted by the branch-off route — this list is an offer.
    """
    from ouroboros.projects_registry import get_thread
    from ouroboros.thread_branching import branch_off_bases, resolve_project_repo, thread_location

    try:
        project_id, thread_id = _route_ids(request)
        drive_root = request_drive_root(request)
        resolved = await asyncio.to_thread(resolve_project_repo, drive_root, project_id)
        if not resolved.get("ok"):
            return _answer(resolved)
        pid = str(resolved["project_id"])
        thread = get_thread(drive_root, pid, thread_id)
        if thread is None:
            return JSONResponse(
                {"ok": False, "reason": "unknown_thread", "message": f"unknown thread: {thread_id}"},
                status_code=404,
            )
        listed = await asyncio.to_thread(branch_off_bases, resolved["repo_dir"])
        return JSONResponse({
            "project_id": pid,
            "thread_id": int(thread["id"]),
            "location": thread_location(drive_root, pid, thread["id"]),
            **listed,
        })
    except Exception as exc:
        return json_exception(exc)


async def api_thread_branch_off(request: Request) -> JSONResponse:
    """POST /api/projects/{project_id}/threads/{thread_id}/branch-off.

    One of A7's two explicit operations. The thread's location is not stored: it
    becomes "worktree" because a worktree now exists.
    """
    from ouroboros.thread_branching import branch_off_thread

    try:
        project_id, thread_id = _route_ids(request)
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        drive_root = request_drive_root(request)
        outcome = await asyncio.to_thread(
            branch_off_thread,
            drive_root, project_id, thread_id,
            base_ref=str(body.get("base_ref") or "").strip(),
        )
        if outcome.get("ok"):
            _broadcast_thread_change(drive_root, str(outcome["project_id"]), outcome["thread_id"])
        return _answer(outcome)
    except Exception as exc:
        return json_exception(exc)


async def api_thread_merge_back(request: Request) -> JSONResponse:
    """POST /api/projects/{project_id}/threads/{thread_id}/merge-back.

    A9's preconditions are enforced in the branching module and reported as typed
    409s. A conflict comes back as ``merge_conflict`` WITH its paths: the merge is
    already aborted by then, so the owner's folder is untouched and the thread
    still holds every commit in its branch.
    """
    from ouroboros.thread_branching import merge_back_thread

    try:
        project_id, thread_id = _route_ids(request)
        drive_root = request_drive_root(request)
        outcome = await asyncio.to_thread(merge_back_thread, drive_root, project_id, thread_id)
        if outcome.get("ok"):
            _broadcast_thread_change(drive_root, str(outcome["project_id"]), outcome["thread_id"])
        return _answer(outcome)
    except Exception as exc:
        return json_exception(exc)


async def api_thread_worktree_inspect(request: Request) -> JSONResponse:
    """GET /api/projects/{project_id}/threads/{thread_id}/worktree.

    WHAT removing this checkout would destroy, so the prompt A10 requires can be
    shown BEFORE anything is removed: dirty files, commits the base never
    received, and an honest ``error`` when the checkout cannot be read at all
    (which counts as unsafe — "cannot tell" must never read as "nothing to lose").
    """
    from ouroboros.projects_registry import get_thread
    from ouroboros.thread_branching import thread_location
    from ouroboros.thread_worktrees import get_thread_worktree, inspect_thread_worktree

    try:
        project_id, thread_id = _route_ids(request)
        drive_root = request_drive_root(request)
        thread = get_thread(drive_root, project_id, thread_id)
        if thread is None:
            return JSONResponse(
                {"ok": False, "reason": "unknown_thread", "message": f"unknown thread: {thread_id}"},
                status_code=404,
            )
        tid = int(thread["id"])
        row = get_thread_worktree(drive_root, project_id, tid)
        if not row:
            return JSONResponse({
                "ok": True, "project_id": project_id, "thread_id": tid,
                "location": {"where": "project_folder"}, "inspection": {},
            })
        inspection = await asyncio.to_thread(inspect_thread_worktree, row)
        return JSONResponse({
            "ok": True,
            "project_id": project_id,
            "thread_id": tid,
            "location": thread_location(drive_root, project_id, tid),
            "inspection": inspection,
        })
    except Exception as exc:
        return json_exception(exc)


async def api_thread_worktree_remove(request: Request) -> JSONResponse:
    """POST /api/projects/{project_id}/threads/{thread_id}/worktree/remove.

    A10: never silent, never automatic, never on a timer. Unmerged work refuses
    with ``unmerged_work`` and the inspection attached; the owner acknowledges by
    re-sending with ``acknowledge_unmerged: true``, which is the whole consent
    mechanism — there is no other path into this function.
    """
    from ouroboros.projects_registry import get_thread
    from ouroboros.thread_branching import thread_location
    from ouroboros.thread_worktrees import remove_thread_worktree

    try:
        project_id, thread_id = _route_ids(request)
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        drive_root = request_drive_root(request)
        thread = get_thread(drive_root, project_id, thread_id)
        if thread is None:
            return JSONResponse(
                {"ok": False, "reason": "unknown_thread", "message": f"unknown thread: {thread_id}"},
                status_code=404,
            )
        tid = int(thread["id"])
        outcome = await asyncio.to_thread(
            remove_thread_worktree,
            data_dir=drive_root,
            project_id=project_id,
            thread_id=tid,
            acknowledge_unmerged=bool(body.get("acknowledge_unmerged")),
        )
        payload: Dict[str, Any] = {
            "ok": bool(outcome.get("removed")),
            "project_id": project_id,
            "thread_id": tid,
            "removed": bool(outcome.get("removed")),
            "reason": str(outcome.get("reason") or ""),
            "inspection": outcome.get("inspection") or {},
            "location": thread_location(drive_root, project_id, tid),
        }
        if payload["ok"]:
            _broadcast_thread_change(drive_root, project_id, tid)
            return JSONResponse(payload)
        payload["message"] = _removal_message(payload["reason"], payload["inspection"])
        return JSONResponse(payload, status_code=_refusal_status(payload["reason"]))
    except Exception as exc:
        return json_exception(exc)


def _removal_message(reason: str, inspection: Dict[str, Any]) -> str:
    """Owner-facing copy for a refused removal — what is at stake, in plain words."""
    if reason == "unmerged_work":
        parts = []
        commits = int(inspection.get("unmerged_commits") or 0)
        if commits:
            parts.append(f"{commits} commit{'s' if commits != 1 else ''} the project folder never received")
        if inspection.get("dirty_files"):
            parts.append(f"{len(inspection['dirty_files'])} uncommitted file changes")
        if inspection.get("error"):
            parts.append("a checkout that could not be read, so its contents are unknown")
        detail = " and ".join(parts) if parts else "work the project folder never received"
        return (
            f"This checkout still holds {detail}. Removing it deletes that work. "
            "Merge it back first, or confirm you want it gone."
        )
    if reason == "unknown":
        return "This thread has no checkout to remove."
    if reason == "path_outside_root":
        return (
            "The registry entry points outside the folder these checkouts live in, "
            "so nothing was deleted. This needs a look before it can be removed."
        )
    return "The checkout could not be removed."


# --------------------------------------------------------------------------- #
# The checkout's own diff (A13 / X9)
# --------------------------------------------------------------------------- #

async def api_thread_diff(request: Request) -> JSONResponse:
    """GET /api/projects/{project_id}/threads/{thread_id}/diff.

    A13: the Changes screen for a branched thread shows THAT checkout's diff.
    Thin route over ``task_diff.thread_checkout_diff_payload`` — same hardened git
    invocation, same patch envelope and the same process-wide admission gate as
    the task diff, so the two surfaces cannot drift apart or fan out git processes
    independently against the owner's repo.
    """
    from ouroboros.gateway.task_diff import diff_gate, thread_checkout_diff_payload
    from ouroboros.projects_registry import get_thread

    try:
        project_id, thread_id = _route_ids(request)
        drive_root = request_drive_root(request)
        thread = get_thread(drive_root, project_id, thread_id)
        if thread is None:
            return JSONResponse({"error": f"unknown thread: {thread_id}"}, status_code=404)
        tid = int(thread["id"])
    except Exception as exc:
        return json_exception(exc)
    try:
        async with diff_gate():
            payload = await asyncio.to_thread(
                thread_checkout_diff_payload, drive_root, project_id, tid,
            )
    except Exception as exc:
        return json_exception(exc, 503)
    return JSONResponse({"project_id": project_id, "thread_id": tid, **payload})


# --------------------------------------------------------------------------- #
# Broadcast
# --------------------------------------------------------------------------- #

def _broadcast_thread_change(drive_root: Any, project_id: str, thread_id: Any) -> None:
    """Ride ``projects_changed`` (X11): no new ABI event for a registry mutation
    the client already refreshes on. The affected thread's chat id travels with it
    so an open client's known-chat set is current before any live frame arrives.
    """
    try:
        from ouroboros.projects_registry import get_thread
        from supervisor.message_bus import get_bridge

        thread = get_thread(drive_root, project_id, thread_id) or {}
        get_bridge().broadcast({
            "type": "projects_changed",
            "project_id": str(project_id),
            "chat_id": thread.get("chat_id"),
        })
    except Exception:
        log.debug("projects_changed broadcast failed for %s#%s", project_id, thread_id, exc_info=True)


__all__ = [
    "api_thread_branch_bases",
    "api_thread_branch_off",
    "api_thread_diff",
    "api_thread_merge_back",
    "api_thread_worktree_inspect",
    "api_thread_worktree_remove",
]
