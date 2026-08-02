"""Control, update, and evolution HTTP endpoints."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, NamedTuple, Tuple

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros import get_version
from ouroboros.gateway._helpers import json_error, json_exception, request_drive_root, request_json_or, request_repo_dir
from ouroboros.gateway.ws import broadcast_ws_sync
from ouroboros.outcomes import public_task_result
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

_RECENT_VISIBLE_COMMANDS: Dict[str, float] = {}
_VISIBLE_COMMAND_DEDUPE_SEC = 5.0
_evo_cache: Dict[str, Any] = {}
_evo_task: asyncio.Task | None = None
# How long an interactive update apply may wait for the assisted resolver pool to say it is ready.
# Deliberately below `workers.WORKER_READY_TIMEOUT_SEC`: that wait runs synchronously inside the
# async apply handler, so it is event-loop time with the pool down and writer admission closed. The
# boot-recovery caller keeps the longer module default, where no request is waiting.
_APPLY_READY_TIMEOUT_SEC = 20.0


def _request_restart(request: Request) -> None:
    callback = getattr(getattr(request.app, "state", None), "request_restart", None)
    if callable(callback):
        callback()


def _runtime_branch_defaults(request: Request) -> tuple[str, str]:
    callback = getattr(getattr(request.app, "state", None), "runtime_branch_defaults", None)
    if callable(callback):
        return callback()
    return "ouroboros", "ouroboros-stable"


def _managed_update_payload(*, fetch: bool, include_tags: bool) -> dict[str, Any]:
    from supervisor.git_ops import compute_managed_update_status, git_capture

    status = compute_managed_update_status(fetch=fetch)
    # P2 2F check-on-restart cache: a passive read (fetch=False) bails before resolving the
    # official ref ("official_status_requires_check"), so overlay the boot/manual-check cache
    # — the badge shows availability after a restart without re-fetching on every poll. A real
    # fetch refreshes the cache. Fresh local `dirty` still gates `safe_to_apply` downward.
    try:
        from supervisor.state import load_state, update_state

        if fetch and status.get("latest_sha"):
            from ouroboros.utils import utc_now_iso

            _snapshot = {
                "available": bool(status.get("available")),
                "safe_to_apply": bool(status.get("safe_to_apply")),
                "latest_sha": status.get("latest_sha") or "",
                "latest_short_sha": status.get("latest_short_sha") or "",
                "latest_message": status.get("latest_message") or "",
                "behind": int(status.get("behind") or 0),
                "ahead": int(status.get("ahead") or 0),
                "checked_at": utc_now_iso(),
            }
            update_state(lambda s: s.__setitem__("managed_update_cache", _snapshot))
        elif not fetch and not status.get("available"):
            cache = (load_state() or {}).get("managed_update_cache") or {}
            cached_latest_sha = cache.get("latest_sha") or ""
            current_sha = status.get("current_sha") or ""
            cache_target_consumed = bool(cached_latest_sha and cached_latest_sha == current_sha)
            if cached_latest_sha and current_sha and not cache_target_consumed:
                rc, _out, _err = git_capture(["git", "merge-base", "--is-ancestor", cached_latest_sha, current_sha])
                cache_target_consumed = rc == 0
            if cache.get("available") and cached_latest_sha and not cache_target_consumed:
                status["available"] = True
                status["safe_to_apply"] = bool(cache.get("safe_to_apply")) and not status.get("dirty")
                status["latest_sha"] = cached_latest_sha
                status["latest_short_sha"] = cache.get("latest_short_sha") or ""
                status["latest_message"] = cache.get("latest_message") or ""
                status["behind"] = int(cache.get("behind") or 0)
                status["ahead"] = int(cache.get("ahead") or 0)
                status["from_cache"] = True
                status["checked_at"] = cache.get("checked_at") or ""
    except Exception:
        log.debug("managed update status cache overlay failed", exc_info=True)
    latest_version = ""
    target_ref = status.get("target_ref") or ""
    if target_ref and status.get("latest_sha"):
        rc, version_text, _ = git_capture(["git", "show", f"{target_ref}:VERSION"])
        if rc == 0:
            latest_version = version_text.strip()
    official_tags = []
    if include_tags:
        from supervisor.git_ops import list_official_update_tags

        official_tags = list_official_update_tags()
    return {
        "current_version": get_version(),
        "latest_version": latest_version,
        "official_tags": official_tags,
        **status,
    }


async def api_reset(request: Request) -> JSONResponse:
    """Reset all runtime data (state, memory, logs, settings) but keep repo."""
    import shutil

    data_dir = request_drive_root(request)
    try:
        deleted = []
        for subdir in ("state", "memory", "logs", "archive", "locks", "task_results", "uploads"):
            target = data_dir / subdir
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
                deleted.append(subdir)
        settings_file = data_dir / "settings.json"
        if settings_file.exists():
            settings_file.unlink()
            deleted.append("settings.json")
        _request_restart(request)
        return JSONResponse({"status": "ok", "deleted": deleted, "restarting": True})
    except Exception as exc:
        return json_exception(exc)


async def api_command(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        cmd = body.get("cmd", "")
        if cmd:
            from supervisor.message_bus import get_bridge, log_chat

            bridge = get_bridge()
            visible_text = str(body.get("visible_text") or "").strip()
            task_constraint = body.get("task_constraint") if isinstance(body.get("task_constraint"), dict) else None
            visible_task_id = str(body.get("visible_task_id") or "").strip()
            if visible_task_id:
                now = time.monotonic()
                expired = [
                    key for key, ts in _RECENT_VISIBLE_COMMANDS.items()
                    if now - ts > _VISIBLE_COMMAND_DEDUPE_SEC
                ]
                for key in expired:
                    _RECENT_VISIBLE_COMMANDS.pop(key, None)
                if visible_task_id in _RECENT_VISIBLE_COMMANDS:
                    return JSONResponse({"ok": True, "deduped": True, "task_id": visible_task_id})
            send_kwargs: dict[str, Any] = {"broadcast": False, "suppress_chat_log": bool(visible_text)}
            if task_constraint:
                send_kwargs["task_constraint"] = task_constraint
            bridge.ui_send(cmd, **send_kwargs)
            if visible_task_id:
                _RECENT_VISIBLE_COMMANDS[visible_task_id] = time.monotonic()
            if visible_text:
                task_id = visible_task_id or "skill_repair"
                ts = utc_now_iso()
                payload = {
                    "type": "chat",
                    "role": "system",
                    "content": visible_text,
                    "ts": ts,
                    "source": "skill_repair",
                    "system_type": "skill_repair",
                    "task_id": task_id,
                }
                broadcast_ws_sync(payload)
                log_chat(
                    "system",
                    0,
                    0,
                    visible_text,
                    ts=ts,
                    source="skill_repair",
                    task_id=task_id,
                )
        return JSONResponse({"status": "ok"})
    except Exception as exc:
        return json_exception(exc, 400)


async def api_git_log(_request: Request) -> JSONResponse:
    """Return recent commits, tags, and current branch/sha."""
    try:
        from supervisor.git_ops import git_capture, list_commits, list_versions

        commits = list_commits(max_count=30)
        tags = list_versions(max_count=20)
        rc, branch, _ = git_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        rc2, sha, _ = git_capture(["git", "rev-parse", "--short", "HEAD"])
        return JSONResponse({
            "commits": commits,
            "tags": tags,
            "branch": branch.strip() if rc == 0 else "unknown",
            "sha": sha.strip() if rc2 == 0 else "",
        })
    except Exception as exc:
        return json_exception(exc)


async def api_git_rollback(request: Request) -> JSONResponse:
    """Roll back to a specific commit or tag, then restart."""
    try:
        body = await request.json()
        target = body.get("target", "").strip()
        if not target:
            return json_error("missing target", 400)
        from supervisor.git_ops import rollback_to_version

        ok, msg = rollback_to_version(target, reason="ui_rollback")
        if not ok:
            return json_error(msg, 400)
        _request_restart(request)
        return JSONResponse({"status": "ok", "message": msg})
    except Exception as exc:
        return json_exception(exc)


async def api_git_promote(request: Request) -> JSONResponse:
    """Promote the current dev branch to the runtime's stable branch."""
    try:
        import subprocess as sp

        branch_dev, branch_stable = _runtime_branch_defaults(request)
        sp.run(
            ["git", "branch", "-f", branch_stable, branch_dev],
            cwd=str(request_repo_dir(request)),
            check=True,
            capture_output=True,
        )
        return JSONResponse({"status": "ok", "message": f"{branch_stable} updated to match {branch_dev}"})
    except Exception as exc:
        return json_exception(exc)


async def api_update_status(_request: Request) -> JSONResponse:
    """Return passive managed-update status without fetching."""
    try:
        return JSONResponse(_managed_update_payload(fetch=False, include_tags=False))
    except Exception as exc:
        return json_exception(exc)


async def api_update_check(_request: Request) -> JSONResponse:
    """Fetch the managed remote and return fresh update status."""
    try:
        return JSONResponse(_managed_update_payload(fetch=True, include_tags=True))
    except Exception as exc:
        return json_exception(exc)


def _open_repo_writer_admission() -> None:
    """Re-admit the IN-PROCESS repository writers the fence closed out (direct/ephemeral chat).

    Every path that gives the pool back owns this: the fence closes admission so a chat turn cannot
    write behind the update, and an abort that left it closed would silently refuse direct chat for
    the rest of the process' life. The restarting exits deliberately leave it closed — the restart
    is what re-opens it.
    """
    try:
        from supervisor.workers import open_repo_writer_admission

        open_repo_writer_admission()
    except Exception:
        log.warning("update_apply: could not re-open repository writer admission", exc_info=True)


def _respawn_workers_after_failed_update() -> None:
    """Revive workers when an update aborts after they were stopped (no restart follows).

    Only ever called once the abort has established that NOTHING from the prior generation is still
    running and the repository is back in an unmutated state: starting a replacement pool beside a
    surviving writer, or on top of a half-applied merge, is worse than staying down.
    """
    _open_repo_writer_admission()
    try:
        from supervisor.workers import ensure_worker_pool_started

        ensure_worker_pool_started(allow_disabled_restart=True)
    except Exception:
        log.warning("update_apply: failed to respawn workers after aborted update", exc_info=True)


async def api_update_preflight(_request: Request) -> JSONResponse:
    """Plan the managed update as a REAL 3-way merge (P2). Does NOT touch the live
    worktree/branch/index (it fetches + merges in an isolated temp worktree), so the UI
    can present the right staged choice (auto / assisted / manual)."""
    try:
        from supervisor.update_merge import plan_managed_update_merge

        plan = plan_managed_update_merge(fetch=True)
        # Evaluate the SAME protected-path authority the apply gate enforces, for the strategy the
        # dialog would actually offer, so the UI never proposes an action the backend will refuse.
        offered = "auto_merge" if _supervisor_authored_clean_merge(plan, "auto_merge") else "assisted"
        blocked, reason = (
            _managed_update_protected_block(plan, offered) if plan.get("available") else ([], "")
        )
        return JSONResponse({
            "merge_plan": plan,
            "protected_route": {
                "offered_strategy": offered,
                # Derived from the REASON, never from list truthiness: an unverifiable delta routes
                # to manual with an EMPTY path list (we could not read what changed).
                "will_route_manual": bool(reason),
                "reason": reason,
                # Serialized like every apply envelope: paths are BLOCKED paths, so they are only
                # published under `protected_paths`. The authority deliberately keeps the plan's own
                # protected conflict paths when the diff fails, but those are not a statement about
                # what the release touches — forwarding them here contradicted the frozen contract
                # (and this endpoint's own comment) that an unverifiable delta carries an empty list.
                "protected_paths": blocked if reason == "protected_paths" else [],
            },
        })
    except Exception as exc:
        return json_exception(exc)


def _is_protected_for_managed_update(path: str) -> bool:
    """A managed-update path that must NOT be auto-resolved by the agent (BIBLE/CHECKLISTS/
    SAFETY + release/managed invariants) — routed to MANUAL (owner) instead."""
    from ouroboros.runtime_mode_policy import is_protected_runtime_path
    from supervisor.update_merge_policy import is_protected_doc

    return bool(is_protected_doc(path) or is_protected_runtime_path(path))


def _official_protected_hits(plan: dict) -> tuple[list, bool]:
    """PROTECTED paths the official update would touch (conflicting OR clean delta), plus whether
    that delta could be VERIFIED. Computed from the plan with a read-only `git diff base..target`
    (no mutation) so the apply path can route to MANUAL BEFORE stopping workers / staging anything.

    Returns ``(paths, verified)``. A plan without both SHAs, or a diff that fails, yields
    ``verified=False`` — the caller then knows the returned list is only the plan's own conflict
    paths and MUST NOT read an empty list as "this release touches nothing protected".
    """
    from supervisor.git_ops import git_capture

    base = str(plan.get("base_sha") or "").strip()
    target = str(plan.get("target_sha") or "").strip()
    paths = set(plan.get("protected_conflict_paths") or [])
    verified = False
    if not base or not target:
        log.warning("update gate: merge plan carries no base/target sha; protected delta unverified")
    else:
        # --no-renames is load-bearing, not tidiness: with rename detection ON git reports a
        # detected rename as its DESTINATION name only, so a release that renames BIBLE.md or
        # ouroboros/safety.py to an unprotected path would show no protected hit at all and sail
        # through this gate. Disabling detection reports the deletion of the source AND the
        # addition of the destination, so renaming a protected path away still blocks.
        rc, delta, err = git_capture(["git", "diff", "--no-renames", "--name-only", base, target])
        if rc == 0:
            paths |= {p for p in delta.splitlines() if p.strip()}
            verified = True
        else:
            log.warning(
                "update gate: could not diff %s..%s (rc=%s): %s", base[:12], target[:12], rc, err
            )
    return sorted(p for p in paths if _is_protected_for_managed_update(p)), verified


# Protected tiers a SUPERVISOR-authored clean merge may carry without owner diff review. Any
# other tier — notably `safety-critical`, and any path this categorizer does not recognize —
# stays blocking, so the exemption is fail-closed by construction.
_AUTO_MERGE_EXEMPT_PROTECTED_CATEGORIES = frozenset({"frozen-contract", "release-invariant"})


def _non_exempt_protected_hits(paths: list) -> list:
    """The already-protected paths that are NOT in one of the two agent-authorship tiers: i.e.
    `safety-critical` (constitution/safety surfaces) plus any tier this categorizer does not
    recognize. THE single expression of the exemption rule — both gates that need it call this,
    so the staged auto_merge exemption and the replace-family disclosure cannot drift apart
    again by editing one copy of the comprehension.

    Selecting by EXCLUSION is what makes both callers fail-closed by construction: future drift
    between the protected-path predicate and the category table keeps blocking (or disclosing)
    instead of silently exempting itself.
    """
    from ouroboros.runtime_mode_policy import protected_path_category

    return [
        p for p in paths
        if protected_path_category(p) not in _AUTO_MERGE_EXEMPT_PROTECTED_CATEGORIES
    ]


def _plan_worktree_is_clean(plan: dict) -> bool:
    """Whether the plan PROVES an empty working tree. Fail-closed: only a real, non-boolean
    integer zero counts, so a missing, `None`, `-1`, `True`/`False`, `"0"` or float count is
    treated as dirty. Every producer-emitted plan carries a real int (`plan_managed_update_merge`),
    so any other shape is a degraded or forged plan and must not unlock the unreviewed fast path.
    """
    dirty = plan.get("local_dirty_count")
    return isinstance(dirty, int) and not isinstance(dirty, bool) and dirty == 0


def _supervisor_authored_clean_merge(plan: dict, strategy: str) -> bool:
    """Whether this request takes the ZERO-AGENT-AUTHORSHIP branch: a clean 3-way merge of
    already-reviewed local COMMITTED history with the already-reviewed official release, landed
    by the supervisor (no conflict markers for the agent to resolve, no uncommitted content).

    Fail-closed: an unreadable dirty count, or a plan claiming `kind == "clean"` while still
    carrying conflict paths (which `classify_conflicts` defines as impossible), is not trusted.
    """
    if strategy != "auto_merge" or str(plan.get("kind") or "") != "clean":
        return False
    if not _plan_worktree_is_clean(plan):
        return False
    return not any(
        plan.get(key)
        for key in ("protected_conflict_paths", "code_conflict_paths", "doc_conflict_paths")
    )


def _managed_update_protected_block(plan: dict, strategy: str) -> tuple[list, str]:
    """PROTECTED paths that must route THIS strategy to MANUAL, plus the typed REASON — the single
    authority shared by the preflight (so the offered action is honest) and the apply gate (so it
    is enforced). The reason is ``""`` (proceed), ``"protected_paths"`` or
    ``"protected_delta_unverifiable"``.

    The protected-path policy exists to keep the AGENT from authoring changes to protected
    surfaces. So the gate is scoped to where that authorship can actually happen:
      - `safety-critical` (BIBLE.md, prompts/SAFETY.md, safety.py, runtime_mode_policy.py,
        tools/registry.py, tools/extension_dispatch.py) blocks on every staged strategy — the owner
        sees every constitutional/safety change before it lands, whoever authored it;
      - `frozen-contract` / `release-invariant` block wherever the agent resolves or commits the
        merge (assisted, doc_reconcile, or an auto_merge degrading into assisted), but NOT in the
        supervisor-authored clean branch, where no agent edit occurs at all. They are the ONLY
        exempt tiers, so an unrecognized tier blocks like `safety-critical` does.

    The replace/stash/force family gates on every tier OTHER than those two exempt ones —
    safety-critical or unrecognized — but there an explicit bound, audit-logged owner
    acknowledgement may override it (see `_replace_family_protected_gate`).

    When the official delta could not be verified the exemption is disabled entirely: we do not
    know what the release touches, so no strategy may proceed and no acknowledgement can be
    informed.
    """
    hits, verified = _official_protected_hits(plan)
    if not verified:
        return hits, "protected_delta_unverifiable"
    if not hits:
        return [], ""
    if not _supervisor_authored_clean_merge(plan, strategy):
        return hits, "protected_paths"
    blocking = _non_exempt_protected_hits(hits)
    return blocking, ("protected_paths" if blocking else "")


def _plan_error_response(plan: dict, message: str, status_code: int = 409) -> JSONResponse:
    """An error response that CARRIES a merge plan without letting the plan speak for it.

    Splat order is load-bearing here. `plan_managed_update_merge` emits its own low-level
    ``error`` on every degraded/unavailable result ("could not resolve target/HEAD",
    "status failed: ...", "add -A failed: ..."), so the historical
    ``{"error": <contextual>, **plan}`` literal resolved to the GIT text: the later ``**plan``
    wins in a dict display, and every caller here silently lost the explanation it meant to
    send. The plan therefore goes FIRST and the caller's message last, while the producer's
    diagnostic is preserved under ``plan_error`` so nothing is thrown away.
    """
    payload = {**plan, "error": message}
    producer_error = plan.get("error")
    if producer_error:
        payload["plan_error"] = producer_error
    return JSONResponse(payload, status_code=status_code)


def _post_stop_plan_drift(gated: dict, fresh: dict, strategy: str) -> JSONResponse | None:
    """Whether the post-worker-stop re-plan still describes the release the gate approved.

    The protected-path authority runs on the PRE-stop plan (so a rejection never interrupts live
    tasks), but the plan the staged flows then MATERIALIZE or COMMIT is resolved again once the
    workers are down. A fetch racing that window — another apply, or the boot update-check thread —
    can move the managed tracking ref from the approved target to a safety-changing one, which the
    fresh plan would apply without the owner ever seeing it. So the fresh plan must pin to the SAME
    base and target the gate saw AND pass the complete protected authority again before anything is
    rescued, staged or committed. Returns the blocking response, or ``None`` when it is the
    approved release. The caller has already stopped workers, so it must respawn them.
    """
    for label, key in (("base", "base_sha"), ("target", "target_sha")):
        was, now = str(gated.get(key) or ""), str(fresh.get(key) or "")
        if not now or now != was:
            return JSONResponse({
                **fresh,
                "error": (
                    f"managed update {label} moved from {was[:12] or 'unknown'} to "
                    f"{now[:12] or 'unknown'} after stopping workers; rerun preflight"
                ),
                "reason": "release_moved",
            }, status_code=409)
    hits, reason = _managed_update_protected_block(fresh, strategy)
    if reason:
        return JSONResponse({
            "status": "manual",
            "reason": reason,
            "protected_paths": hits if reason == "protected_paths" else [],
            "merge_plan": fresh,
        })
    return None


def _protected_ack_is_bound(body: dict, base_sha: str, target_sha: str, paths: list) -> bool:
    """Whether the request carries an acknowledgement BOUND to exactly what was disclosed: the same
    base/target SHA and the same path list. A stale or partial echo is not consent to THIS update,
    and requiring the SHAs closes the window between the disclosure fetch and the apply fetch."""
    if body.get("acknowledge_protected") is not True:
        return False
    if str(body.get("acknowledged_base_sha") or "") != base_sha:
        return False
    if str(body.get("acknowledged_target_sha") or "") != target_sha:
        return False
    acknowledged = body.get("acknowledged_protected_paths")
    if not isinstance(acknowledged, list):
        return False
    return [str(p) for p in acknowledged] == list(paths)


def _audit_protected_acknowledgement(
    strategy: str, base_sha: str, target_sha: str, paths: list
) -> bool:
    """Record the owner's bound override in the supervisor audit log BEFORE anything is prepared.
    Returns False when the record could not be written — an unlogged override is refused."""
    try:
        from ouroboros.utils import append_jsonl
        from supervisor.git_ops import DRIVE_ROOT

        return bool(append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
            "ts": utc_now_iso(),
            "type": "ui_update_protected_acknowledged",
            "strategy": strategy,
            "base_sha": base_sha,
            "target_sha": target_sha,
            "protected_paths": list(paths),
        }))
    except Exception:
        log.warning("update_apply: protected acknowledgement audit failed", exc_info=True)
        return False


def _replace_family_protected_gate(
    body: dict, strategy: str
) -> tuple[JSONResponse | None, str, str, list]:
    """Gate the REPLACE family (replace / stash / force and any unknown strategy) on the same
    protected authority the staged merge paths use, scoped to `safety-critical` or unrecognized
    protected tiers (v6.88.1).

    `replace` hard-resets the checkout to the official release, so no agent authors that delta and
    the frozen-contract / release-invariant tiers — whose whole point is agent authorship — do not
    apply; gating them here would only make the escape hatch unusable. Everything else — a
    constitutional/safety change, or a protected path of an unrecognized tier — must still reach
    the owner, so it is DISCLOSED and proceeds only on an acknowledgement bound to the exact SHAs
    and paths disclosed. An unverifiable delta is never acknowledgeable: consent about an unknown
    list is not informed consent.

    The disclosure set comes from `_non_exempt_protected_hits`, the SAME helper the staged
    auto_merge exemption uses, so the two gates share one exemption rule by construction rather
    than by inspection.

    PURE gate: it plans (fetching) and decides, but writes nothing — no audit record, no
    preparation. `_apply_replace_family` runs it TWICE (once as the pre-lock disclosure, once
    under the worker fence) and only the fenced verdict is allowed to act.

    Returns ``(blocking_response_or_None, base_sha, target_sha, disclosed_paths)``; the SHAs pin
    the subsequent preparation to the release the owner was actually shown.
    """
    from supervisor.update_merge import plan_managed_update_merge

    plan = plan_managed_update_merge(fetch=True, build=False)
    if not plan.get("available"):
        return _plan_error_response(plan, "no managed update available"), "", "", []
    hits, reason = _managed_update_protected_block(plan, strategy)
    if reason == "protected_delta_unverifiable":
        return JSONResponse({
            "status": "manual", "reason": reason, "protected_paths": [], "merge_plan": plan,
        }), "", "", []
    base_sha = str(plan.get("base_sha") or "")
    target_sha = str(plan.get("target_sha") or "")
    disclose = _non_exempt_protected_hits(hits)
    if not disclose:
        return None, base_sha, target_sha, disclose
    if not _protected_ack_is_bound(body, base_sha, target_sha, disclose):
        return JSONResponse({
            "status": "manual",
            "reason": "protected_paths",
            "requires_acknowledgement": True,
            "protected_paths": disclose,
            "base_sha": base_sha,
            "target_sha": target_sha,
            "merge_plan": plan,
        }), base_sha, target_sha, disclose
    return None, base_sha, target_sha, disclose


class _FenceResult(NamedTuple):
    """Outcome of the mandatory writer fence.

    `survivors` carries the worker HANDLES we could not prove dead. That distinction is the whole
    point of returning a structure rather than a bool: "the stop failed and nothing from the prior
    generation is running" is recoverable by starting a replacement pool, while "the stop failed and
    a writer is still alive" is NOT — `kill_workers` has already removed that survivor from
    ``WORKERS``, so `ensure_worker_pool_started` would see an empty pool and cheerfully run a fresh
    generation alongside it.

    `blocked` carries the same verdict for the non-worker writer PROCESSES — the repository-rooted
    services in the custody ledger — as descriptions rather than handles, because they belong to no
    pool and there is no handle to latch. They need the same treatment for the same reason: an
    arbitrary command still running inside the checkout is a writer, so neither the update nor a
    replacement pool may go ahead. The latch is the ledger itself: the entry stays live, so the
    next apply re-derives the same refusal without needing to remember anything.
    """

    ok: bool
    survivors: List[Any]
    blocked: Tuple[str, ...] = ()


def _unproven_dead_workers(fenced: List[Any]) -> List[Any]:
    """The fenced handles we cannot PROVE are dead — live ones, plus any we could not read at all,
    because an unreadable handle is not a worker we are entitled to call stopped."""
    survivors: List[Any] = []
    for worker in fenced:
        try:
            if worker.proc.is_alive():
                survivors.append(worker)
        except Exception:
            survivors.append(worker)
    return survivors


def _terminate_ledgered_repo_writers() -> List[str]:
    """Stop every repository-writing process in the DURABLE custody ledger; return the unproven ones.

    Imported inside the function for the same reason the worker module is: a fence that could not be
    established has to come back as a typed refusal, and an import error at module scope would take
    the whole gateway down instead. Raises are the caller's to convert — an unreadable ledger is not
    evidence of an empty one.
    """
    from ouroboros.process_custody import terminate_repo_writer_processes
    from supervisor.git_ops import DRIVE_ROOT

    return terminate_repo_writer_processes(DRIVE_ROOT)


def _verified_pool_teardown(result_reason: str, *, terminal_status: str = "interrupted") -> _FenceResult:
    """Stop the worker pool and answer with EVIDENCE — the one teardown every update path uses.

    The teardown hands back a RECEIPT — the pre-kill handles plus any teardown error — so the
    verification below runs on the failing path with exactly the evidence it runs on for the
    succeeding one. Letting a teardown exception through instead would arrive here with no handles,
    and an empty handle list is indistinguishable from "the pool was already empty", which is the
    reading that authorizes a respawn. `kill_workers` clears ``WORKERS`` before its remaining
    bookkeeping, so that is precisely the state a late raise leaves behind.

    Non-empty ``survivors`` means a prior-generation writer may still be running and has been
    LATCHED: no replacement pool may be started and the checkout must not move. ``ok`` false with no
    survivors means every captured handle is proven dead but the teardown itself did not finish — a
    respawn is safe there, the update is not.
    """
    try:
        from supervisor import workers as _workers

        teardown = _workers.snapshot_and_kill_workers(
            result_reason=result_reason, terminal_status=terminal_status
        )
    except Exception:
        log.warning("update_apply: mandatory worker fence failed", exc_info=True)
        return _FenceResult(False, [])
    if teardown.error:
        log.warning("update_apply: worker teardown reported %s", teardown.error)
    survivors = _unproven_dead_workers(teardown.fenced)
    if survivors:
        try:
            survivors = _workers.terminate_worker_survivors(survivors)
        except Exception:
            log.warning("update_apply: survivor termination retry failed", exc_info=True)
    if survivors:
        log.error(
            "update_apply: worker fence left %d worker(s) that could not be proven dead", len(survivors)
        )
        try:
            _workers.latch_worker_survivors(survivors)
        except Exception:
            log.warning("update_apply: could not latch the unproven survivors", exc_info=True)
        return _FenceResult(False, survivors)
    return _FenceResult(not teardown.error, [])


def _blocked_fence(labels: List[str]) -> _FenceResult:
    """A BLOCKED refusal, latched durably.

    The 409 this produces promises task processing stays disabled until a restart, and the latch is
    what enforces it. Writer admission is closed at this point, but the admission reconciler re-opens
    it on its next timer pass unless something says the checkout is still shared — and the ledger
    entry is not that something: the unkillable service may exit (or its record be pruned) later,
    which would let admission re-open behind an update fence that was never actually established.
    """
    try:
        from supervisor import workers as _workers

        _workers.latch_repo_writer_blockers(list(labels))
    except Exception:
        log.warning("update_apply: could not latch the repository-writer blockers", exc_info=True)
    return _FenceResult(False, [], tuple(labels))


def _clear_or_refuse_latched_blockers() -> _FenceResult | None:
    """Re-probe the blockers a previous fence latched; ``None`` means the latch is clear.

    Killing is retried here, exactly as the survivor latch retries its terminations, because a
    service that has since exited must not disable updates forever. Only an empty sweep clears it.
    """
    from supervisor import workers as _workers

    if not _workers.latched_repo_writer_blockers():
        return None
    try:
        still_blocked = _terminate_ledgered_repo_writers()
    except Exception:
        log.warning("update_apply: could not re-probe the latched repository writers", exc_info=True)
        return _FenceResult(False, [], ("repo_service_ledger_unreadable",))
    if still_blocked:
        log.error(
            "update_apply: repository-writing service(s) from an earlier fence are still alive: %s",
            still_blocked,
        )
        return _blocked_fence(still_blocked)
    _workers.clear_repo_writer_blockers()
    return None


def _fence_workers_for_update(reason: str) -> _FenceResult:
    """Exclude every repository writer and VERIFY it — the exclusion every update pin rests on.

    A best-effort stop is not a fence: a surviving writer can commit and advance HEAD between the
    re-resolved base and the hard reset, or dirty the tree between the post-stop re-plan and the
    fast commit, so the transition that lands is not the one the owner acknowledged.

    There are THREE kinds of writer and they need different treatment:

    - The multiprocessing worker pool. `kill_workers` alone cannot answer whether it went away: it
      joins each worker with a timeout, makes a best-effort second force-kill pass whose outcome it
      never checks, and then clears ``WORKERS`` unconditionally — so a process tree that outlives
      both joins raises nothing and is indistinguishable from success. The handles are therefore
      snapshotted BEFORE the call and re-read after it, and anything still alive gets ONE verified
      process-tree termination retry before the fence gives up and hands the survivors back.
    - The in-process chat writers. The direct-chat agent is a THREAD in this process holding the
      full operator-control tool profile (and the ephemeral turn runs the same route), so stopping
      the pool excludes nothing there. A thread cannot be killed, so admission is closed first —
      no new turn may start — and the turns already inside are DRAINED. A turn that will not drain
      fails the fence rather than being written over. Each turn takes its admission as a LEASE,
      atomically with the closed-state read and inside its own serialization boundary, so "already
      inside" is a registered fact rather than an inference from a flag the run sets later.
    - Services rooted in the checkout. `start_service` runs an arbitrary command in the active
      workspace and the process it leaves behind outlives the tool call — a `keep_alive` one
      outlives the whole task — so it is a writer that is neither a pool worker nor a chat turn.
      Those started by THIS process take the SAME admission lease as the in-process writers, so
      they need no separate refusal: closed admission stops new ones,
      `terminate_repo_rooted_services` stops the running ones, and one that will not die keeps its
      lease and is reported by the drain below.

      Those started by a pooled WORKER cannot use any of that. The worker holds its own copies of
      `supervisor.workers` and `ouroboros.tools.services`, so its lease and its service record
      live in that process and are invisible here — and a service is spawned into its own session
      with an arbitrary command, so it does not die with the worker either. The DURABLE custody
      ledger is the registry both processes share, so it carries the repo-writer marker and
      `_terminate_ledgered_repo_writers` sweeps it once the pool is proven dead, which is the
      earliest point at which no worker can add another entry behind the sweep.

    Snapshot and kill are taken as ONE step (`workers.snapshot_and_kill_workers`) rather than
    read here: ``WORKERS`` is mutated under the worker lifecycle lock, so a respawn slipping
    between an unlocked read and the kill would produce a worker this verification pass never
    inspects — and the fence would then report success on a stop it never proved.
    """
    try:
        # Imported INSIDE the guard, not above it: this helper's contract is that a fence it could
        # not establish comes back as a typed refusal. An exception escaping here would instead
        # unwind past every caller's `finally` into the blanket 500 handler, which is the one answer
        # the callers cannot act on.
        from supervisor import workers as _workers

        # Survivors from an EARLIER fence first, before anything is snapshotted or killed. That
        # refusal promises task processing stays down until a restart, and this is what enforces
        # it: the survivor is long gone from ``WORKERS``, so a fresh snapshot would find an empty
        # pool, report no survivors, and manufacture a clean proof beside a writer that never died.
        # Only a verified termination of every latched handle clears it.
        latched = _workers.reap_latched_worker_survivors()
    except Exception:
        log.warning("update_apply: could not reach the worker module for the fence", exc_info=True)
        return _FenceResult(False, [])
    if latched:
        log.error(
            "update_apply: %d worker(s) from an earlier fence are still not proven dead", len(latched)
        )
        return _FenceResult(False, latched)
    # ...and the repository-rooted services an earlier fence could not clear, for the same reason:
    # a blocked refusal leaves no handle behind, so this latch is the only thing that survives it.
    blocked_latch = _clear_or_refuse_latched_blockers()
    if blocked_latch is not None:
        return blocked_latch
    try:
        _workers.close_repo_writer_admission(f"managed_update: {reason}")
    except Exception:
        log.warning("update_apply: could not close repository writer admission", exc_info=True)
        return _FenceResult(False, [])
    pool = _verified_pool_teardown(reason)
    if not pool.ok:
        # Survivors (latched) or a teardown that did not finish; both refuse the update, and the
        # caller tells them apart the same way it always did — by `survivors`.
        return pool
    try:
        # The third writer class: services this process started inside the checkout. They are
        # separate processes that outlive the tool call (a `keep_alive` one outlives its task), so
        # neither the pool teardown nor the chat drain reaches them. They hold the SAME admission
        # lease as the in-process writers, which is why nothing new is needed here: stop them, and
        # let the drain below report any lease that could not be retired.
        from ouroboros.tools.services import terminate_repo_rooted_services

        terminate_repo_rooted_services()
    except Exception:
        log.warning("update_apply: could not terminate repository-rooted services", exc_info=True)
    # ...and the ones a POOLED WORKER started, which the call above cannot see: its `_SERVICES` map
    # lives in the worker's own address space. This runs AFTER the pool is proven dead, which is what
    # makes the sweep exhaustive rather than racy — no worker is left to append an entry behind it.
    # It also runs after `terminate_repo_rooted_services`, which waits on the processes it owns, so
    # a supervisor-started service is already reaped and cannot be miscounted as a survivor.
    try:
        unstopped = _terminate_ledgered_repo_writers()
    except Exception:
        log.warning("update_apply: could not sweep ledgered repository-writing services", exc_info=True)
        return _blocked_fence(["repo_service_ledger_unreadable"])
    if unstopped:
        log.error(
            "update_apply: %d repository-writing service(s) could not be proven dead: %s",
            len(unstopped),
            unstopped,
        )
        return _blocked_fence(unstopped)
    try:
        undrained = _workers.drain_repo_writers()
    except Exception:
        log.warning("update_apply: could not drain in-process repository writers", exc_info=True)
        return _FenceResult(False, [])
    if undrained:
        log.error("update_apply: in-process repository writer(s) would not drain: %s", undrained)
        return _FenceResult(False, [])
    return _FenceResult(True, [])


def _abort_after_failed_fence(fence: _FenceResult, what: str) -> JSONResponse:
    """The shared abort for a fence that could not be established.

    A fence can fail three ways and they are NOT the same recovery. `kill_workers` tears each worker
    down before it clears the pool, so a raise (or a stop we simply could not confirm with nothing
    left alive) may have killed part of the pool with no restart coming — that has to respawn, or a
    refused update silently ends task processing until the process is restarted by hand. But a fence
    that reports SURVIVORS means a prior-generation writer is still running while ``WORKERS`` is
    already empty; starting a replacement pool there would run two generations against one checkout.
    That case stays down, with writer admission still closed, until the server is restarted.

    "Stays down" is not a wish here: the fence LATCHED those handles in the worker module, and it
    re-reads that latch before it snapshots anything, so a retried apply is refused the same way
    until every latched handle has been proven dead.

    A BLOCKED fence is the same verdict for a different writer: a repository-rooted service — very
    likely one a pooled worker started — is running an arbitrary command inside the checkout and
    could not be proven dead. It is not a worker, so there is nothing to respawn around; the fence
    latches the blocker LABELS instead, which both keeps the admission reconciler from re-opening
    writer admission on its next timer pass and makes the next apply re-derive the same refusal
    until a re-probe proves the service gone. Starting a replacement pool here would put a fresh
    generation of writers next to it, so this arm, like the survivor arm, deliberately does not
    respawn.
    """
    if fence.blocked:
        return JSONResponse(
            {
                "error": (
                    f"could not stop {len(fence.blocked)} repository-rooted service(s) for the "
                    "update and they could not be proven dead; task processing stays disabled "
                    f"until the server is restarted ({what})"
                ),
                "reason": "repo_service_fence_blocked",
            },
            status_code=409,
        )
    if fence.survivors:
        return JSONResponse(
            {
                "error": (
                    f"could not stop {len(fence.survivors)} worker(s) for the update and they "
                    "could not be proven dead; task processing stays disabled until the server "
                    f"is restarted ({what})"
                ),
                "reason": "worker_fence_survivors",
            },
            status_code=409,
        )
    _respawn_workers_after_failed_update()
    return JSONResponse(
        {"error": f"could not stop workers for the update; {what}"}, status_code=409
    )


def _repository_is_recovered() -> bool:
    """Whether the checkout is PROVEN to carry no leftover managed-update mutation.

    Delegates to the update authority rather than re-deriving the predicate: the orphaned-resolution
    watchdog gates re-admitting writers on the SAME question, and two definitions of "recovered"
    drifting apart is how one of them ends up letting writers onto a tree the other would refuse.
    Fail-closed by construction there — a check that could not run counts as unproven.
    """
    from supervisor.update_merge import managed_update_repository_is_recovered

    return managed_update_repository_is_recovered()


def _recovery_locked_down_response(reason: str, detail: str, rollback: str) -> JSONResponse:
    """The single 409 for "the checkout could not be proven safe, so nothing gets it back".

    Every caller of this has already decided that neither a replacement pool nor in-process writer
    admission may be handed out until a human restarts the server, so they all say it the same way
    and answer the same machine-readable ``update_recovery_failed``.
    """
    return JSONResponse(
        {
            "error": (
                f"{reason}: {detail}, so task processing and in-process chat stay disabled "
                "until the server is restarted"
            ),
            "reason": "update_recovery_failed",
            "rollback": rollback,
        },
        status_code=409,
    )


def _rollback_and_respawn(reason: str, failure: Dict[str, Any]) -> JSONResponse:
    """Undo a mutation that already landed, PROVE the undo, and only then give the writers back.

    Every abort past the fence that runs AFTER the checkout was mutated shares this shape, and they
    all used to discard the answer: `rollback_managed_update` returns False when it could not
    restore the pre-update checkout (no `pre_update_sha`, a failed `checkout -B`), yet the branches
    called it for its side effect and then unconditionally ran
    `_respawn_workers_after_failed_update` — which re-opens in-process writer admission and starts a
    fresh pool. On a failed rollback that put direct chat and a new worker generation onto a live
    transaction, a leftover ``MERGE_HEAD`` with conflict markers, or the newly applied merge HEAD:
    exactly the precondition the respawn helper's own docstring says it must never violate.

    So the boolean gates everything below it, and it is not trusted alone — `_repository_is_recovered`
    re-derives the state from the checkout itself, because a rollback can report success and still
    leave a marker or a half-finished merge behind. An undo we cannot prove keeps admission closed
    and the pool down and answers `update_recovery_failed`: that state needs the restart / boot
    recovery path, not more writers.

    `failure` is the envelope the branch would have returned on a PROVEN rollback; the unproven
    answer is built here instead, because the branch's own wording ("rolled back to the prior
    version") is a claim this path could not establish.
    """
    from supervisor.update_merge import rollback_managed_update

    try:
        rolled_back, rollback_msg = rollback_managed_update(reason)
    except Exception as rollback_exc:
        log.warning("update_apply: rollback raised during %s recovery", reason, exc_info=True)
        rolled_back, rollback_msg = False, str(rollback_exc)
    if rolled_back and _repository_is_recovered():
        _respawn_workers_after_failed_update()
        return JSONResponse({**failure, "rollback": rollback_msg}, status_code=409)
    log.error(
        "update_apply: the %s rollback could not be proven (%s); writers stay locked out",
        reason, rollback_msg,
    )
    return _recovery_locked_down_response(
        reason, f"the rollback could not be proven ({rollback_msg})", rollback_msg
    )


def _recover_after_post_fence_exception(exc: Exception, *, context: str) -> JSONResponse:
    """PHASE-AWARE recovery for an exception raised past the fence.

    Respawning is only a safe answer while nothing has been mutated. Both staged flows write their
    transaction and then mutate the live tree — auto_merge hard-applies the merge commit before
    running a smoke that can raise, and assisted materializes ``MERGE_HEAD`` plus conflict markers
    before its later phase writes and enqueue can raise — so a handler that only respawned left the
    new HEAD, the staged merge and the live transaction in place and revived the general pool on top
    of them.

    So the transaction marker decides: no transaction means nothing was staged and the pool can come
    straight back; a transaction means the mutation has to be undone through `_rollback_and_respawn`,
    the same proven-rollback gate the explicit abort branches take.
    """
    from supervisor.update_merge import active_update_tx

    try:
        tx = active_update_tx() or {}
    except Exception:
        # A marker we cannot even READ is a mutation we are not entitled to rule out.
        log.warning("update_apply: could not read the update tx during recovery", exc_info=True)
        tx = {"phase": "unreadable"}
    if not tx:
        _respawn_workers_after_failed_update()
        return JSONResponse(
            {"error": f"update aborted after stopping workers: {exc}"}, status_code=409
        )
    return _rollback_and_respawn(
        f"{context}_post_fence_exception",
        {
            "error": f"update aborted after stopping workers and rolled back: {exc}",
            "reason": "rolled_back",
        },
    )


async def _apply_replace_family(
    request: Request, body: dict, strategy: str, base_sha: str, target_sha: str, disclosed: list
) -> JSONResponse:
    """Apply a REPLACE-family update ATOMICALLY with the acknowledgement it was gated on (v6.88.1).

    Resolving the pin before the writers are stopped is not enough: the base was read before the
    gate's network fetch, and live workers kept running through it and through preparation, so a
    worker commit (or a racing fetch) could move HEAD or the target between the disclosure and the
    hard reset — the applied transition would then differ from the acknowledged one while every
    equality check still passed.

    So everything past the preliminary disclosure happens under the FAIL-CLOSED update lock WITH a
    mandatory worker fence: stop the writers first, THEN re-run the whole gate against a freshly
    fetched plan (re-resolving both SHAs and re-deriving the disclosed set), and only then audit,
    prepare and reset. Any drift respawns the workers and hands back a fresh disclosure rather
    than applying a release nobody reviewed.

    Stopping the writers before validation used to be the bug (a plain "no update available" 409
    killed every worker with no restart to revive them); that is why EVERY exit after the fence
    respawns the pool — including an EXCEPTIONAL one, which is what this function's try/except
    around `_apply_replace_family_fenced` is for — and why the preliminary gate still answers
    before the fence is taken.
    """
    from supervisor.update_merge import acquire_update_lock, active_update_tx, release_update_lock

    try:
        lock_fh = acquire_update_lock()
    except RuntimeError as exc:
        return JSONResponse(
            {"error": str(exc), "reason": "update_lock_held"}, status_code=409
        )
    try:
        if active_update_tx():  # TOCTOU: re-check UNDER the lock, before any mutation
            return JSONResponse({"error": "a managed update is already in progress"}, status_code=409)
        fence = _fence_workers_for_update(
            "Task interrupted by an owner-requested managed update (restart follows)."
        )
        if not fence.ok:
            # A fence can fail HALF-WAY (some workers already gone, no restart coming) or with
            # SURVIVORS still running; `_abort_after_failed_fence` tells the two apart, because only
            # the first may be answered by starting a replacement pool.
            return _abort_after_failed_fence(fence, "nothing was applied")
        # PAST THE FENCE the pool is down, so no exit may leave it there. The gate and the
        # preparation below both shell out to git (`_official_protected_hits`,
        # `compute_managed_update_status`) and touch the filesystem (rescue snapshot), so any of
        # them can RAISE — and an exception escaping to `api_update_apply`'s `json_exception`
        # handler would answer a 500 with every worker dead until the process restarts. Convert
        # instead: respawn, then report.
        try:
            return await _apply_replace_family_fenced(
                request, body, strategy, base_sha, target_sha, disclosed
            )
        except Exception as exc:
            log.warning("update_apply: replace-family apply failed after the fence", exc_info=True)
            return _recover_after_post_fence_exception(exc, context="replace")
    finally:
        release_update_lock(lock_fh)


async def _apply_replace_family_fenced(
    request: Request, body: dict, strategy: str, base_sha: str, target_sha: str, disclosed: list
) -> JSONResponse:
    """The mutating half of the replace-family apply, run with the update lock held AND the worker
    pool already fenced (see `_apply_replace_family`, which owns both and guarantees the pool comes
    back on every exit including an exception).

    Re-runs the WHOLE gate against a freshly fetched plan — re-resolving both SHAs and re-deriving
    the disclosed set — because the caller's pin was computed before the writers were stopped. Only
    a re-resolution that matches the acknowledged transition exactly is allowed to audit, prepare
    and reset; anything else hands the owner a fresh disclosure.
    """
    import uuid

    from supervisor.git_ops import (
        BRANCH_DEV,
        _clear_update_intent,
        checkout_and_reset,
        prepare_managed_update,
    )
    from supervisor.update_merge import clear_update_tx, write_update_tx

    blocked, fenced_base, fenced_target, fenced_disclosed = _replace_family_protected_gate(
        body, strategy
    )
    if blocked is not None:
        _respawn_workers_after_failed_update()
        return blocked
    if (fenced_base, fenced_target) != (base_sha, target_sha) or fenced_disclosed != disclosed:
        # The release moved while we were taking the fence. The acknowledgement described the
        # OLD transition, so this one goes back to the owner as a fresh disclosure.
        _respawn_workers_after_failed_update()
        return JSONResponse({
            "status": "manual",
            "reason": "release_moved",
            "protected_paths": fenced_disclosed,
            "base_sha": fenced_base,
            "target_sha": fenced_target,
        })
    if disclosed and not _audit_protected_acknowledgement(
        strategy, fenced_base, fenced_target, disclosed
    ):
        # Fail closed: an override we cannot record is an override that never happened.
        _respawn_workers_after_failed_update()
        return JSONResponse({"error": "protected_ack_audit_failed"}, status_code=409)
    # The replace family MUTATES the live checkout (a hard reset onto the target), so from here on
    # a failure has something to UNDO — and the only durable record of what to undo to is this
    # transaction. Without one, every post-mutation recovery read "no transaction is active", took
    # that as proof nothing was staged, and handed the checkout back to a fresh worker generation
    # and to in-process chat over whatever the reset had already produced.
    #
    # It is written BEFORE `prepare_managed_update`, because prepare ends by publishing the update
    # INTENT — a marker the next boot consumes to hard-reset onto the target. Written afterwards, a
    # crash in that window persisted an intent with no transaction, which the bootstrap reads as an
    # ordinary checkout and applies with normal reset recovery: an unsupervised update with no
    # rollback point.
    #
    # The phase is the EXISTING `pending_boot_smoke` rather than a new one. It already means "HEAD
    # must carry `merge_commit`, otherwise roll back to `pre_update_sha`", and for a replace the
    # commit HEAD must carry simply IS the target — so the boot finalizer, `_RESTART_PENDING_PHASES`
    # and the admission reconcile all handle this marker unchanged. A phase of its own would have
    # left the marker stranded on the finalizer's `unhandled phase` branch.
    write_update_tx(
        {
            "pre_update_sha": fenced_base,
            "pre_update_branch": BRANCH_DEV,
            "target_sha": fenced_target,
            "merge_commit": fenced_target,
            "phase": "pending_boot_smoke",
            "strategy": strategy,
            "rollback_attempted": False,
            "attempt_id": uuid.uuid4().hex[:12],
        }
    )
    try:
        ok, payload = prepare_managed_update(
            strategy, expected_base_sha=fenced_base, expected_target_sha=fenced_target
        )
    except Exception:
        # Prepare raised with the tree unmutated, so the transaction pre-written above has to come
        # back off — intent first, same rule as below — and the caller's post-fence handler then
        # does the bare respawn it has always done for this case. A transaction left behind would
        # turn a failed PREPARATION into a rollback attempt against a checkout nothing touched;
        # an intent we cannot prove gone keeps the transaction, and that handler locks down.
        # A transaction whose own removal cannot be proven is LEFT for the same handler: it reads
        # the marker and routes to the proven-rollback gate, which is the fail-closed answer here.
        if _clear_update_intent():
            clear_update_tx()
        raise
    if not ok:
        # Prepare failed with the tree still unmutated, so this stays the bare respawn it always
        # was — but the transaction written above has to come back off, and in the ONLY safe order:
        # the intent (which prepare may have published just before failing) is proven gone FIRST,
        # because it is the restart-consumed marker. If it cannot be proven gone, the transaction
        # stays: an intent with a transaction is a recoverable state the boot path handles, an
        # intent alone is an unsupervised reset onto the target.
        if not _clear_update_intent():
            return _recovery_locked_down_response(
                "replace_prepare_failed",
                "the update intent marker could not be removed",
                "not attempted",
            )
        # And the transaction's own removal is proven before the respawn, for the same reason: this
        # branch is about to re-admit in-process writers and start a fresh pool, and a marker still
        # on disk means `active_update_tx()` goes on demanding recovery for an apply that never
        # mutated anything — the next boot would "recover" a checkout by rolling it back.
        if not clear_update_tx():
            return _recovery_locked_down_response(
                "replace_prepare_failed",
                "the update transaction marker could not be removed",
                "not attempted",
            )
        _respawn_workers_after_failed_update()
        return JSONResponse(payload, status_code=409)
    try:
        checkout_ok, checkout_msg = checkout_and_reset(
            BRANCH_DEV,
            reason="ui_update_apply",
            unsynced_policy="rescue_and_reset",
        )
    except Exception as checkout_exc:
        checkout_ok, checkout_msg = False, f"{checkout_exc}"
    if not checkout_ok:
        # A checkout that failed may still have MOVED the live tree before it gave up, so this is
        # not a bare respawn any more: undo to the fenced pre-update SHA, prove the undo, and only
        # then give the writers back — the same gate every other post-mutation abort takes. It also
        # owns the markers, clearing the transaction and the update intent on the proven path and
        # deliberately keeping them on the unproven one, where they are the only record of where
        # this checkout was supposed to return to.
        #
        # `**payload` FIRST for the same reason as `_plan_error_response`: the prepared payload
        # carries its own `status` key (the full git status dict), so splatting it last replaced
        # the outcome this response reports.
        return _rollback_and_respawn(
            "replace_checkout_failed",
            {**payload, "error": f"Prepared update but checkout failed: {checkout_msg}"},
        )
    try:
        _request_restart(request)
    except Exception:
        # The checkout SUCCEEDED, so the update is COMMITTED: the tree already carries the target
        # and the transaction above will be finalized on the next boot. Only the restart request
        # failed. Letting this raise sent it to `_recover_after_post_fence_exception`, which would
        # now roll a landed update back — and, before the transaction existed, found none, called
        # the update "aborted after stopping workers", and respawned the pool onto the freshly
        # reset tree. So it is reported as what it is: applied, not restarted.
        log.warning("update_apply: the restart request failed after the update landed", exc_info=True)
        return JSONResponse({
            **payload,
            "status": "ok",
            "restarting": False,
            "warning": "restart request failed; restart manually",
        })
    return JSONResponse({**payload, "status": "ok", "restarting": True})


def _start_assisted_merge(plan: dict) -> JSONResponse:
    """Orchestrate the AUTOMATED assisted managed-update merge (P2/SC2). Under the FAIL-CLOSED
    update lock with workers stopped: re-plan, route official PROTECTED-path changes to MANUAL
    (never the agent), durably rescue local work, stage a REAL `git merge --no-commit` into the
    LIVE worktree (MERGE_HEAD + conflict markers) via the supervisor, and enqueue the single
    authorized resolution task. The agent resolves the markers with normal file tools and the
    UNMODIFIED commit_reviewed lands a reviewed 2-parent merge commit (Q11) — no blocked git,
    no parallel trust path. The merge state + tx marker survive a restart (resumable recovery).

    Owns the lock and the worker fence; `_start_assisted_merge_fenced` does the staging. Every
    exit that does not hand the pool to `enqueue_assisted_resolution_task` brings it back —
    including an EXCEPTIONAL one, which is what the try/except around the fenced half is for."""
    from supervisor.git_ops import BRANCH_DEV
    from supervisor.update_merge import acquire_update_lock, active_update_tx, release_update_lock

    branch = BRANCH_DEV
    try:
        lock_fh = acquire_update_lock()
    except RuntimeError as exc:
        # Typed so the UI can say "an update check is in progress, try again" instead of a
        # generic failure: the boot check-on-restart thread holds this same lock across its
        # fetch, so a held lock is routinely transient rather than an error the owner caused.
        return JSONResponse({"error": str(exc), "reason": "update_lock_held"}, status_code=409)
    try:
        if active_update_tx():  # TOCTOU: re-check UNDER the lock
            return JSONResponse({"error": "a managed update is already in progress"}, status_code=409)
        # MANDATORY, not best-effort. The `_post_stop_plan_drift` recheck below is only worth
        # anything if the writers are actually down: a surviving worker can advance HEAD (or dirty
        # the tree) after `plan2` clears that recheck and before the merge is materialized into the
        # LIVE worktree, which is exactly the unacknowledged-transition race the recheck exists to
        # close. A stop we could not confirm therefore aborts before any planning or staging.
        fence = _fence_workers_for_update(
            "Task interrupted by an owner-requested assisted merge update."
        )
        if not fence.ok:
            return _abort_after_failed_fence(fence, "nothing was staged")
        # PAST THE FENCE the pool is down and no restart follows, so no exit may leave it there.
        # Re-planning shells out to git, the rescue snapshot and the durable local ref touch the
        # filesystem, and the tx marker writes to disk — every one of them can RAISE. An exception
        # escaping here would unwind through the `finally` below, releasing only the lock and
        # leaving task processing dead until the process restarts. Convert instead: respawn, report.
        try:
            return _start_assisted_merge_fenced(plan, branch)
        except Exception as exc:
            log.warning("assisted merge: staging failed after the fence", exc_info=True)
            return _recover_after_post_fence_exception(exc, context="assisted")
    finally:
        release_update_lock(lock_fh)


def _start_assisted_merge_fenced(plan: dict, branch: str) -> JSONResponse:
    """The staging half of the assisted merge, run with the update lock held AND the worker pool
    already fenced (see `_start_assisted_merge`, which owns both and guarantees the pool comes back
    on every exit including an exception)."""
    import uuid as _uuid

    from ouroboros.utils import utc_now_iso
    from supervisor.git_ops import _create_rescue_snapshot, git_capture
    from supervisor.state import load_state
    from supervisor.update_merge import (
        create_rescue_local_ref,
        enqueue_assisted_resolution_task,
        materialize_assisted_merge_live,
        plan_managed_update_merge,
        write_update_tx,
    )

    plan2 = plan_managed_update_merge(fetch=False, build=False)
    if not plan2.get("available"):
        _respawn_workers_after_failed_update()
        return _plan_error_response(plan2, "no managed update available")
    # The upstream gate ran on `plan` BEFORE kill_workers; this is the plan that actually gets
    # materialized, so it must be the SAME release and must pass the protected authority again
    # (evaluated as `assisted` — the agent authors and commits this merge, so no tier is
    # exempt). A drifted target is refused here rather than staged into the live worktree.
    drift = _post_stop_plan_drift(plan, plan2, "assisted")
    if drift is not None:
        _respawn_workers_after_failed_update()
        return drift
    base_sha = str(plan2.get("base_sha") or "")
    target_sha = str(plan2.get("target_sha") or "")
    local_snapshot = str(plan2.get("local_snapshot") or "")
    if not local_snapshot or not target_sha:
        _respawn_workers_after_failed_update()
        return _plan_error_response(plan2, "could not build local snapshot / target")

    rc_b, cur_branch, _be = git_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    rc_s, status_txt, _se = git_capture(["git", "status", "--porcelain"])
    _create_rescue_snapshot(branch, "ui_update_assisted_merge", {
        "current_branch": cur_branch if rc_b == 0 else "",
        "dirty_lines": [ln for ln in status_txt.splitlines() if ln.strip()] if rc_s == 0 else [],
        "unpushed_lines": [], "warnings": [],
    })
    create_rescue_local_ref(local_snapshot)  # durable ref: local work survives any rollback/gc

    st = load_state() or {}
    try:
        owner_chat_id = int(st.get("owner_chat_id") or 0)
    except (TypeError, ValueError):
        owner_chat_id = 0
    task_id = "update_assisted_merge_" + _uuid.uuid4().hex[:8]
    tx = {
        "phase": "materializing_assisted",
        "pre_update_sha": base_sha,
        "pre_update_branch": branch,
        "base_sha": base_sha,
        "target_sha": target_sha,
        "local_snapshot": local_snapshot,
        "conflict_paths": (list(plan2.get("code_conflict_paths") or [])
                           + list(plan2.get("doc_conflict_paths") or [])),
        "task_id": task_id,
        "owner_chat_id": owner_chat_id,
        "resolution_attempts": 0,
        "requested_at": utc_now_iso(),
    }
    write_update_tx(tx)  # BEFORE destructive materialization (crash-safe recovery)
    ok, msg = materialize_assisted_merge_live(branch, local_snapshot, target_sha, base_sha)
    if not ok:
        return _rollback_and_respawn(
            "assisted_materialize_failed", {"error": f"could not stage the merge: {msg}"}
        )
    tx["phase"] = "assisted_resolution"
    write_update_tx(tx)
    # The WORKER POOL comes back HERE rather than through the respawn helper: the enqueued
    # resolution task spawns the worker that will do the merge. In-process writer admission does
    # NOT come back with it. That worker is the one authorized writer for a checkout that now holds
    # MERGE_HEAD and conflict markers, and admission re-opening here would put direct-chat turns
    # beside it — and, on the failure branch below, beside a rollback that hard-resets the
    # worktree. It re-opens where the resolution ENDS instead: a committed merge restarts the
    # process, and a resolver task that ends without committing is caught by
    # `update_merge.abort_orphaned_assisted_tx`, which rolls back and re-admits there.
    try:
        # Enqueues the authorized task + spawns a worker. The ready handshake is bounded SHORTER
        # here than at boot: this runs synchronously inside the async apply handler, so every second
        # of it is the gateway event loop blocked with the pool down and admission closed. The
        # rollback that follows a "no" is the same either way, so only the ceiling differs.
        enqueue_assisted_resolution_task(tx, ready_timeout=_APPLY_READY_TIMEOUT_SEC)
    except Exception as exc:
        # `assisted_started` is a promise that something is now resolving the merge. Without a live
        # worker nothing is: the staged merge and its transaction would sit in the worktree with no
        # resolver, and the health loop only respawns slots already in ``WORKERS``, so an empty pool
        # never self-recovers. Undo the staging instead of reporting a start that did not happen.
        log.warning("assisted merge: no worker could run the resolution task", exc_info=True)
        # Stop the half-started pool HERE, where the matching respawn is. `enqueue_assisted_
        # resolution_task` deliberately does not: boot recovery calls it too and is non-destructive,
        # so a teardown inside the callee would empty ``WORKERS`` at boot, drive every restored
        # pending task to a terminal failure through the kill's drain, and leave a pool the health
        # loop cannot refill. On THIS path the teardown is required — the rollback below hard-resets
        # the worktree, so processes that never answered ready must be gone before the tree moves
        # under them, and `_rollback_and_respawn` rebuilds the pool from the restored checkout.
        #
        # It goes through the SAME verified teardown the fence uses, for the same reason: a resolver
        # that never answered ready is exactly the process most likely to survive `kill_workers`,
        # and a hard reset run beside a live one is the coexistence the fence exists to prevent. A
        # survivor here is latched, so the rollback, the respawn and every later apply stay refused.
        if _verified_pool_teardown(
            "Managed update resolver pool never became ready. Task was not completed."
        ).survivors:
            log.error("assisted merge: the unready resolver pool could not be proven dead")
            return _recovery_locked_down_response(
                "assisted_resolver_unavailable",
                "the unready resolver pool could not be proven dead",
                "not attempted: a resolver process may still be running",
            )
        return _rollback_and_respawn(
            "assisted_resolver_unavailable",
            {
                "error": f"could not start the assisted resolver: {exc}",
                "reason": "assisted_resolver_unavailable",
            },
        )
    return JSONResponse({"status": "assisted_started", "task_id": task_id, "merge_plan": plan2})


async def _apply_managed_merge(request: Request, strategy: str) -> JSONResponse:
    """Staged merge-aware update apply (P2). auto_merge lands a CLEAN 3-way merge behind a
    FAIL-CLOSED lock with a pre-restart smoke + transactional rollback (local work is
    preserved in the merge's local-snapshot parent + a rescue snapshot). assisted/
    doc_reconcile route to the agent-assisted flow; manual returns the plan without
    mutating."""
    from supervisor.git_ops import BRANCH_DEV
    from supervisor.update_merge import (
        acquire_update_lock,
        plan_managed_update_merge,
        release_update_lock,
    )

    branch = BRANCH_DEV
    plan = plan_managed_update_merge(fetch=True, build=False)
    if not plan.get("available"):
        return _plan_error_response(plan, "no managed update available")
    kind = str(plan.get("kind") or "")

    if strategy == "manual":
        # No mutation: hand the UI the plan; recovery artifacts are created only on apply.
        return JSONResponse({"status": "manual", "merge_plan": plan})

    # Official changes to PROTECTED paths route to MANUAL, scoped by category and strategy (see
    # _managed_update_protected_block): safety-critical always, the other tiers wherever the agent
    # would author or commit the merge. Checked on the initial plan BEFORE any kill_workers /
    # rescue / materialization, so a read-only handoff never interrupts active tasks. The official
    # delta (base..target) does not change when workers stop, so this pre-kill check is sufficient.
    protected_hits, protected_reason = _managed_update_protected_block(plan, strategy)
    if protected_reason:
        return JSONResponse(
            {"status": "manual", "reason": protected_reason,
             "protected_paths": protected_hits if protected_reason == "protected_paths" else [],
             "merge_plan": plan}
        )

    if (
        strategy in ("assisted", "doc_reconcile")
        or (strategy == "auto_merge" and kind != "clean")
        # P3/P9 (triad): auto_merge fast-commits the local-snapshot parent into history
        # WITHOUT the commit_reviewed gate. That is only acceptable when the local work is
        # already-reviewed COMMITTED history; UNCOMMITTED dirty/untracked content must NOT
        # land unreviewed. So a dirty working tree — or a plan that cannot PROVE a clean one —
        # routes to the REVIEWED assisted task (which still PRESERVES the local changes — Q2),
        # never a silent auto-commit.
        or (strategy == "auto_merge" and not _plan_worktree_is_clean(plan))
    ):
        # Conflicts (code/doc) OR uncommitted local work: hand the merge to Ouroboros as an
        # AUTOMATED, REVIEWED task. The supervisor stages a real merge into the live worktree
        # and the agent resolves the markers; the resulting commit flows through the standard
        # triad/scope immune review (Q11) before it lands — no blocked git, no parallel trust
        # path. The owner watches progress in chat (no manual git).
        return _start_assisted_merge(plan)

    # ---- auto_merge (clean) ----
    try:
        lock_fh = acquire_update_lock()
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc), "reason": "update_lock_held"}, status_code=409)
    try:
        from supervisor.update_merge import active_update_tx

        if active_update_tx():  # TOCTOU: re-check UNDER the lock before any mutation
            return JSONResponse({"error": "a managed update is already in progress"}, status_code=409)
        # MANDATORY, not best-effort — same reason as the assisted path: every guarantee the
        # post-stop re-plan makes (clean tree, same base/target, unchanged protected set) is a
        # guarantee only for as long as nothing else can write. A surviving worker could advance
        # HEAD between `plan2` passing and `apply_managed_merge_update` landing the fast commit,
        # so a stop we could not confirm aborts before the re-plan is even taken.
        fence = _fence_workers_for_update(
            "Task interrupted by an owner-requested managed merge update (restart follows)."
        )
        if not fence.ok:
            return _abort_after_failed_fence(fence, "nothing was applied")
        # PAST THE FENCE the pool is down, so no non-restarting exit may leave it there. The
        # re-plan, the rescue snapshot, the tx marker, the apply and the smoke all shell out to git
        # or touch the filesystem and can RAISE; an exception unwinding through the `finally` below
        # would release the lock and nothing else. Convert instead: respawn, then report.
        try:
            return _apply_auto_merge_fenced(request, plan, branch)
        except Exception as exc:
            log.warning("update_apply(merge): apply failed after the fence", exc_info=True)
            return _recover_after_post_fence_exception(exc, context="auto_merge")
    finally:
        release_update_lock(lock_fh)


def _apply_auto_merge_fenced(request: Request, plan: dict, branch: str) -> JSONResponse:
    """The mutating half of the clean auto_merge apply, run with the update lock held AND the
    worker pool already fenced (see `_apply_managed_merge`, which owns both and guarantees the pool
    comes back on every exit that is not followed by a restart, including an exception)."""
    import uuid

    from supervisor.git_ops import _create_rescue_snapshot, git_capture
    from supervisor.update_merge import (
        apply_managed_merge_update,
        plan_managed_update_merge,
        update_restart_smoke,
        write_update_tx,
    )

    # Re-plan + build AFTER stopping writers; never trust the pre-kill plan. Re-check
    # BOTH clean-merge AND a clean working tree here: a worker (or any in-process path)
    # may have dirtied/untracked files between the pre-kill plan and now, and auto_merge
    # must NEVER fast-commit uncommitted local content unreviewed (P3/P9) — a dirty
    # post-kill plan aborts back to the reviewed assisted/manual path.
    plan2 = plan_managed_update_merge(fetch=False, build=True)
    merge_commit = str(plan2.get("merge_commit") or "")
    if plan2.get("kind") != "clean" or not merge_commit or not _plan_worktree_is_clean(plan2):
        _respawn_workers_after_failed_update()
        return _plan_error_response(
            plan2, "update is no longer a clean auto-merge after stopping workers"
        )
    # The upstream gate ran on `plan`, BEFORE kill_workers; `plan2` is what actually lands, so
    # it must pin to the same base/target and pass the protected authority again — a fetch
    # racing this window could otherwise swap in a safety-changing target unreviewed.
    drift = _post_stop_plan_drift(plan, plan2, "auto_merge")
    if drift is not None:
        _respawn_workers_after_failed_update()
        return drift

    rc_b, cur_branch, _be = git_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    rc_s, status_txt, _se = git_capture(["git", "status", "--porcelain"])
    _create_rescue_snapshot(
        branch,
        "ui_update_apply_merge",
        {
            "current_branch": cur_branch if rc_b == 0 else "",
            "dirty_lines": [ln for ln in status_txt.splitlines() if ln.strip()] if rc_s == 0 else [],
            "unpushed_lines": [],
            "warnings": [],
        },
    )
    write_update_tx(
        {
            "pre_update_sha": str(plan2.get("base_sha") or ""),
            "pre_update_branch": branch,
            "target_sha": str(plan2.get("target_sha") or ""),
            "merge_commit": merge_commit,
            "phase": "pending_boot_smoke",
            "rollback_attempted": False,
            "attempt_id": uuid.uuid4().hex[:12],
        }
    )

    ok, msg = apply_managed_merge_update(branch, merge_commit)
    if not ok:
        return _rollback_and_respawn(
            "merge_apply_failed", {"error": f"merge apply failed (rolled back): {msg}"}
        )

    smoke = update_restart_smoke()
    if not smoke.get("ok"):
        return _rollback_and_respawn(
            "pre_restart_smoke_failed",
            {"error": "pre-restart smoke failed; rolled back to the prior version", "smoke": smoke},
        )

    try:
        _request_restart(request)  # the restart brings the pool back; no respawn on this exit
    except Exception:
        # The merge is APPLIED and the pre-restart smoke has PASSED, so the update is committed and
        # the transaction above will be finalized on the next boot. Letting this raise reached
        # `_recover_after_post_fence_exception` over a live `pending_boot_smoke` transaction, which
        # takes the transaction branch and hard-resets a successfully applied, smoke-verified update
        # back to `pre_update_sha` — reporting a landed update as an abort and undoing it. Only the
        # restart failed, so that is what is reported. (Same treatment as the replace family.)
        log.warning("update_apply: the restart request failed after the merge landed", exc_info=True)
        return JSONResponse({
            "status": "ok",
            "restarting": False,
            "strategy": "auto_merge",
            "merge_plan": plan2,
            "warning": "restart request failed; restart manually",
        })
    return JSONResponse(
        {"status": "ok", "restarting": True, "strategy": "auto_merge", "merge_plan": plan2}
    )


async def api_update_apply(request: Request) -> JSONResponse:
    """Apply a managed update. Default is the merge-aware auto_merge (P2); auto_merge/
    assisted/doc_reconcile/manual route to the staged merge flow, while the legacy
    'replace' (advanced escape hatch) hard-resets to the remote behind the protected gate for
    safety-critical or unrecognized tiers + a bound owner acknowledgement."""
    body = await request_json_or(request, {}, exceptions=(Exception,))
    # Normalize once: the replace family is selected by ELIMINATION below, so an unrecognized or
    # oddly-cased strategy must land on the SAME gated path a plain 'replace' takes.
    strategy = str(body.get("strategy") or "auto_merge").strip().lower()
    # Reject ANY mutating apply while a managed-update tx is already in flight (the legacy
    # 'replace' path would otherwise kill_workers + hard-reset over an in-progress assisted
    # resolution). 'manual' is read-only and always allowed. The merge paths re-check the tx
    # under the lock (TOCTOU); this is the cheap front gate.
    if strategy != "manual":
        from supervisor.update_merge import active_update_tx

        if active_update_tx():
            return JSONResponse({"error": "a managed update is already in progress"}, status_code=409)
    if strategy in ("auto_merge", "assisted", "doc_reconcile", "manual"):
        return await _apply_managed_merge(request, strategy)
    try:
        # Preliminary (read-only) disclosure: a request that routes to MANUAL must not stop a
        # single worker, so the gate answers BEFORE the lock and the fence are taken. Everything
        # that mutates then happens under both, against a re-resolved plan — see
        # `_apply_replace_family`.
        blocked, base_sha, target_sha, disclosed = _replace_family_protected_gate(body, strategy)
        if blocked is not None:
            return blocked
        return await _apply_replace_family(
            request, body, strategy, base_sha, target_sha, disclosed
        )
    except Exception as exc:
        return json_exception(exc)


async def api_evolution_data(request: Request) -> JSONResponse:
    """Collect evolution metrics for each git tag."""
    from ouroboros.utils import collect_evolution_metrics

    global _evo_task
    now = time.time()
    force_refresh = str(request.query_params.get("force") or "").strip().lower() in {"1", "true", "yes"}
    if not force_refresh and _evo_cache.get("ts") and now - _evo_cache["ts"] < 60:
        return JSONResponse({
            "points": _evo_cache["points"],
            "checkpoints": _evo_cache.get("checkpoints", []),
            "generated_at": _evo_cache.get("generated_at", ""),
            "cached": True,
        })
    if _evo_task is None or _evo_task.done():
        _evo_task = asyncio.create_task(
            collect_evolution_metrics(
                str(request_repo_dir(request)),
                data_dir=str(request_drive_root(request)),
            )
        )
    data_points = await _evo_task
    try:
        from ouroboros.evolution_checkpoints import CHECKPOINTS_REL
        from ouroboros.utils import iter_jsonl_objects

        checkpoints = []
        rows = [
            row for row in iter_jsonl_objects(request_drive_root(request) / CHECKPOINTS_REL)
            # cycle_outcome rows are solve-capability digest fodder (different
            # schema: no git_sha/identity hashes); the Dashboard checkpoints
            # view renders absorb checkpoints only.
            if isinstance(row, dict) and row.get("kind") != "cycle_outcome"
        ]
        for row in rows[-100:]:
            checkpoints.append(public_task_result(row))
    except Exception:
        checkpoints = []
    _evo_cache["ts"] = time.time()
    _evo_cache["points"] = data_points
    _evo_cache["checkpoints"] = checkpoints
    _evo_cache["generated_at"] = utc_now_iso()
    return JSONResponse({
        "points": data_points,
        "checkpoints": checkpoints,
        "generated_at": _evo_cache["generated_at"],
        "cached": False,
    })


__all__ = [
    "api_command",
    "api_evolution_data",
    "api_git_log",
    "api_git_promote",
    "api_git_rollback",
    "api_reset",
    "api_update_apply",
    "api_update_check",
    "api_update_preflight",
    "api_update_status",
]
