"""The C1 integration seam of the delegated-run tools (authority → snapshot → capture).

Extracted from ``ouroboros/tools/delegate.py`` when that module crossed its size gate:
this is one coherent concern — how a MUTATING delegated run is bound to the tree it may
change. The host derives the unified authority record (B5), provisions the private
execution snapshot the run edits instead of the shared tree (C1), validates the
byte-identical retry binding, and captures the terminal diff for explicit integration.
``tools.delegate`` re-exports every name here (same objects), so every existing
reference — sibling code, the tests, the convergence census — still finds them there.
"""

from __future__ import annotations

import json
import logging
import pathlib
import threading
from typing import TYPE_CHECKING, Any, Dict, NamedTuple, Optional, Tuple

from ouroboros import delegate_custody as custody
from ouroboros.delegate_custody import RunCustody as _RunCustody
# ONE refusal author for the whole delegate surface: the neutral leaf
# `delegate_shared` (phase B's facade split), never a local twin that could drift.
from ouroboros.delegate_shared import _fail
from ouroboros.tools.registry import ToolContext, active_repo_dir_for
from ouroboros.utils import resolve_path_allow_missing

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ouroboros.subagents import DelegatedRunShape

log = logging.getLogger(__name__)


def _resolved(path: Any) -> Optional[pathlib.Path]:
    """Resolve a path, or None when it cannot be resolved at all (null byte, symlink
    loop, unreadable parent). One predicate, so no call site re-enumerates the set."""
    try:
        return resolve_path_allow_missing(pathlib.Path(str(path)))
    except (OSError, ValueError, RuntimeError, TypeError):
        return None


# C1 capture mode: every mutating delegated run executes in a private snapshot and
# returns its diff for explicit integration. The value is recorded (not branched on)
# so the durable record names the regime a run was admitted under.
_CAPTURE_DELEGATED_SNAPSHOT = "delegated_snapshot"


def _mutation_authority(ctx: ToolContext, authority: "DelegatedRunShape") -> tuple[Dict[str, str], str]:
    """The UNIFIED host-derived authority record for one delegated run (B5).

    ``{"target_root", "source", "capture_mode"}`` — the tree the run's changes are
    destined for, WHERE that authority came from, and how the changes travel. Two
    sources exist, validated differently but recorded in one shape:

    - ``acting_constraint``: an acting child derives its target from its own
      ``task_constraint.write_root``, which must equal the genuinely ACTIVE workspace
      root (the original guard: `active_repo_dir_for` falls back to the LIVE Ouroboros
      repo whenever `is_workspace_mode()` is false, and a constraint naming that same
      directory would hand an external shell the live repository).
    - ``external_workspace_root``: the ROOT of an external-workspace task holds no
      acting constraint at all — the old seam answered ``write_root_missing`` here —
      so its authority derives from its own VALIDATED active workspace: workspace mode
      external, workspace root resolving to the active root. (Owner 2=A: the root
      already holds write+shell inside the project; the only prior gap was provenance,
      which the C1 snapshot + explicit apply now records per run.)

    Read-only runs return the ordinary active root with ``capture_mode: "none"``.
    Disagreement anywhere is a typed refusal, never a best-effort guess.
    """
    root = str(active_repo_dir_for(ctx))
    if authority.access != "workspace_write":
        return {"target_root": root, "source": "readonly", "capture_mode": "none"}, ""
    constraint = getattr(ctx, "task_constraint", None)
    mode = str(
        (constraint.get("mode") if isinstance(constraint, dict)
         else getattr(constraint, "mode", "")) or ""
    ).strip()
    granted = str(
        (constraint.get("write_root") if isinstance(constraint, dict)
         else getattr(constraint, "write_root", "")) or ""
    ).strip()
    # ONE predicate, the one the registry already owns. `workspace_mode_block_reason`
    # returns "" precisely WHEN `workspace_mode` is empty, so "no block reason" is
    # satisfied by the absence of a workspace — the condition was true in exactly the
    # case it was written to refuse. `is_workspace_mode()` is the question actually
    # being asked, and `active_repo_dir()` branches on that same call.
    workspace_active = bool(
        callable(getattr(ctx, "is_workspace_mode", None)) and ctx.is_workspace_mode())
    if mode == "acting_subagent" or granted:
        if not granted:
            return {}, _fail(
                "delegate_start", "write_root_missing",
                "This child is allowed to write, but its task constraint names no write_root, "
                "so there is no directory the host can honestly confine the run to.",
            )
        # AGREEMENT IS NOT ENOUGH: `active_repo_dir_for` falls back to `repo_dir` when
        # workspace mode is off, so a constraint whose write_root happens to name that
        # same directory made the comparison pass and handed a shell the live repository
        # — the very case this guard was written for. Require a genuinely ACTIVE
        # workspace.
        if not workspace_active:
            return {}, _fail(
                "delegate_start", "workspace_not_active",
                "A delegated run may only WRITE inside an ACTIVE workspace, and this task "
                "has none. Refusing rather than falling back to the repository root.",
            )
        # "Can this path be resolved at all" is ONE question, not an exception set to
        # re-enumerate: an embedded null raises ValueError and a symlink loop RuntimeError,
        # and either escaping here would abort delegate_start with a traceback instead of
        # the typed refusal this function exists to produce.
        resolved_root, resolved_grant = _resolved(root), _resolved(granted)
        if resolved_root is None or resolved_root != resolved_grant:
            return {}, _fail(
                "delegate_start", "write_root_mismatch",
                "The active root and the granted write_root disagree, so the run would write "
                "somewhere this task was never given. Refusing rather than guessing.",
                active_root=root, granted_write_root=granted,
            )
        return {"target_root": root, "source": "acting_constraint",
                "capture_mode": _CAPTURE_DELEGATED_SNAPSHOT}, ""
    # ROOT branch (B5): no acting constraint. The mutating shape can only have come
    # from the external-workspace-root profile, and the workspace contract is the
    # authority: workspace genuinely active, mode external, and the declared
    # workspace root must BE the active root.
    if not workspace_active:
        return {}, _fail(
            "delegate_start", "workspace_not_active",
            "A delegated run may only WRITE inside an ACTIVE workspace, and this task "
            "has none. Refusing rather than falling back to the repository root.",
        )
    ws_mode = str(getattr(ctx, "workspace_mode", "") or "").strip().lower()
    if ws_mode not in {"external", "external_workspace"}:
        return {}, _fail(
            "delegate_start", "write_root_missing",
            "This task holds a mutating shape but neither an acting write_root nor an "
            "external workspace contract names the tree it may write. Refusing rather "
            "than guessing a target.",
        )
    declared = _resolved(getattr(ctx, "workspace_root", None))
    resolved_root = _resolved(root)
    if resolved_root is None or declared is None or resolved_root != declared:
        return {}, _fail(
            "delegate_start", "write_root_mismatch",
            "The active root does not resolve to this task's declared external "
            "workspace, so the run would write somewhere this task was never given.",
            active_root=root, declared_workspace_root=str(getattr(ctx, "workspace_root", "") or ""),
        )
    return {"target_root": root, "source": "external_workspace_root",
            "capture_mode": _CAPTURE_DELEGATED_SNAPSHOT}, ""


def _retry_binding_refusal(record: Dict[str, Any], retry_token: str) -> str:
    """Refuse a MUTATING retry whose stored row carries no C1 isolation binding.

    A PRE-C1 row's recorded body scopes the run at the LIVE target tree, and the
    body is replayed byte-identically — so the binding cannot be minted
    retroactively and replaying would write straight into the shared tree, in the
    in-place regime C1 retired. Returns "" when the full binding is present.
    """
    snapshot_id = str(record.get("snapshot_id") or "")
    baseline_sha = str(record.get("baseline_sha") or "")
    target_root = str(record.get("target_root") or "")
    if snapshot_id and str(record.get("execution_root") or "") and baseline_sha and target_root:
        return ""
    return _fail(
        "delegate_start", "retry_binding_absent",
        "This retry replays a MUTATING invocation recorded BEFORE private "
        "execution snapshots (no snapshot/baseline binding), so replaying it "
        "would write directly into the shared tree. Start a new run with a "
        "plain delegate_start — it takes its own snapshot and returns its "
        "diff for explicit integration.",
        retry_of=retry_token, recorded_root=target_root,
        snapshot_id=snapshot_id, baseline_sha=baseline_sha)


def _validated_invocation(drive: Any, retry_token: str, task_id: str,
                          text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """The stored invocation a retry may replay, or the typed refusal that stops it.

    Six ways a token is not replayable, each answered by name: no record, another
    task's, already bound, definitely refused (its id is retired — replaying wedges a
    permanent 409), no canonical body, prompt disagrees. One author for all six.
    """
    record = custody.invocation_record(drive, retry_token)
    if record is None:
        return None, _fail("delegate_start", "unknown_invocation",
                           "retry_of names an invocation with no durable record on this "
                           "drive. Start a new run with a plain delegate_start.",
                           retry_of=retry_token)
    if record["task_id"] != task_id:
        return None, _fail("delegate_start", "invocation_not_owned",
                           "retry_of names another task's invocation. A delegated start "
                           "may only be retried by the task that requested it.",
                           retry_of=retry_token)
    if record["state"] == "started":
        return None, _fail("delegate_start", "invocation_already_started",
                           "That invocation already bound a run — do not re-post it. "
                           "Wait on the existing run instead.",
                           retry_of=retry_token, run_id=record["run_id"])
    if record["state"] == "failed_definite":
        return None, _fail("delegate_start", "invocation_definitely_refused",
                           "That invocation was definitively refused by the daemon; its "
                           "id is retired. Start a new run with a plain delegate_start.",
                           retry_of=retry_token)
    body = record["request"]
    if not isinstance(body, dict) or not body:
        return None, _fail("delegate_start", "invocation_request_unrecorded",
                           "That invocation's durable row carries no canonical request "
                           "body, so it cannot be replayed byte-identically. Start a "
                           "new run with a plain delegate_start.",
                           retry_of=retry_token)
    if str(body.get("prompt") or "") != text:
        return None, _fail("delegate_start", "retry_prompt_mismatch",
                           "retry_of replays the RECORDED invocation, but the prompt "
                           "you passed differs from the one it sent. Pass the original "
                           "prompt to retry, or drop retry_of to start a new run.",
                           retry_of=retry_token)
    return record, ""


class _RetryBinding(NamedTuple):
    """Every fact of a replayed invocation, read off its ONE durable record."""

    request_body: Dict[str, Any]
    route: Any          # DelegationRoute
    authority: Any      # DelegatedRunShape
    root: str
    key: str
    project_id: str
    owned_project_id: str
    seconds: int
    snapshot_id: str
    target_root: str
    baseline_sha: str
    authority_source: str
    resource_ref: Dict[str, Any]


def _resolve_retry_invocation(ctx: ToolContext, drive: pathlib.Path, retry_token: str,
                              text: str) -> Tuple[Optional[_RetryBinding], str]:
    """Rebuild a retried start from its stored invocation, or refuse it typed.

    The stored invocation is the SINGLE SOURCE of EVERY fact about a retry — the
    health-checked route, the shape, the root, the project, the lookup key — not
    only of the wire bytes: re-deriving any of them POSTed the recorded body while
    the record and the parent's result described a configuration the run never had.
    Validated BEFORE any daemon call, so a refused token registers nothing.

    A MUTATING replay additionally re-proves its C1 binding: the row must carry the
    snapshot/baseline binding at all (pre-C1 rows are refused), the task's PRESENT
    workspace must still resolve to the recorded authority target, and the recorded
    snapshot must still exist on disk — a GC-collected baseline cannot be re-minted.
    """
    from ouroboros.subagents import DelegatedRunShape, DelegationRoute

    record, refusal = _validated_invocation(
        drive, retry_token, str(getattr(ctx, "task_id", "") or ""), text)
    if refusal:
        return None, refusal
    request_body = record["request"]
    execution = (request_body.get("execution")
                 if isinstance(request_body.get("execution"), dict) else {})
    scope = request_body.get("scope") if isinstance(request_body.get("scope"), dict) else {}
    route = DelegationRoute(route_id=str(request_body.get("primaryHarness") or ""),
                            model=str(request_body.get("model") or ""),
                            effort=str(request_body.get("effort") or ""))
    authority = DelegatedRunShape(access=str(request_body.get("access") or ""),
                                  mode=str(request_body.get("mode") or ""),
                                  isolation=str(execution.get("isolation") or ""),
                                  delegated=bool(execution.get("delegated")))
    root = str(scope.get("root") or "")
    project_id = str(record.get("project_id") or "")
    # The C1 isolation binding recorded at the original attempt. Pre-C1 rows
    # carry none; their scope.root IS the authority target (in-place regime).
    snapshot_id = str(record.get("snapshot_id") or "")
    target_root = str(record.get("target_root") or "") or root
    if authority.access == "workspace_write":
        binding_refusal = _retry_binding_refusal(record, retry_token)
        if binding_refusal:
            return None, binding_refusal
        # The replay will WRITE for the recorded AUTHORITY TARGET, so authority
        # is re-asked against the task's PRESENT context — and the answer must
        # be the very target the invocation recorded. A target that moved
        # between the attempts makes the replay a write destined for a tree this
        # task no longer holds, which is a refusal, never a re-derivation. A
        # payload run re-resolves its SEMANTIC reference (bucket + skill name)
        # through a fresh exact binding rather than the workspace contract.
        if str(record.get("authority_source") or "") == "skill_payload":
            _target, _binding, rebind_refusal = _rebind_payload_reference(
                ctx, record.get("resource_ref") or {}, target_root,
                tool="delegate_start", context=f"retry_of={retry_token}")
            if rebind_refusal:
                return None, rebind_refusal
        else:
            record_auth, root_error = _mutation_authority(ctx, authority)
            if root_error:
                return None, root_error
            resolved_current = _resolved(record_auth.get("target_root"))
            resolved_recorded = _resolved(target_root)
            if resolved_current is None or resolved_current != resolved_recorded:
                return None, _fail(
                    "delegate_start", "retry_root_divergence",
                    "This retry replays a MUTATING invocation recorded against a root "
                    "this task no longer holds: the active write root has moved since "
                    "the original attempt. Start a new run for the current root.",
                    retry_of=retry_token, recorded_root=target_root,
                    active_root=record_auth.get("target_root"))
        if snapshot_id:
            # The retry reproduces the EXACT binding — same execution root, same
            # baseline. A snapshot the GC already collected cannot be re-minted
            # (the baseline commit is gone with its ref): typed refusal.
            from ouroboros.subagent_worktrees import find_execution_snapshot
            snap_entry = find_execution_snapshot(snapshot_id)
            if snap_entry is None or not pathlib.Path(str(snap_entry.get("path") or "")).exists():
                return None, _fail(
                    "delegate_start", "execution_snapshot_missing",
                    "This retry replays a MUTATING invocation whose private "
                    "execution snapshot no longer exists on disk, so the recorded "
                    "binding cannot be reproduced. Start a new run with a plain "
                    "delegate_start (it will take a fresh snapshot).",
                    retry_of=retry_token, snapshot_id=snapshot_id)
    return _RetryBinding(
        request_body=request_body,
        route=route,
        authority=authority,
        root=root,
        key=str(record.get("idempotency_key") or ""),
        project_id=project_id,
        owned_project_id=(project_id if record.get("project_owned") else ""),
        seconds=int(request_body.get("maxSeconds") or 0),
        snapshot_id=snapshot_id,
        target_root=target_root,
        baseline_sha=str(record.get("baseline_sha") or ""),
        authority_source=str(record.get("authority_source") or ""),
        resource_ref=(record.get("resource_ref")
                      if isinstance(record.get("resource_ref"), dict) else {}),
    ), ""


def _provision_snapshot(ctx: ToolContext, drive: pathlib.Path, target_root: str,
                        invocation_id: str) -> Tuple[Optional[Any], str]:
    """Provision the C1 private execution snapshot for one mutating run.

    Registered durably (worktree registry) and described durably (baseline manifest
    artifact) BEFORE the caller records any start intent, so a worker death at any
    later point leaves a nameable, GC-reconcilable root — never an orphan directory.
    Returns ``(handle, "")`` or ``(None, typed_refusal)``.
    """
    from ouroboros.subagent_worktrees import provision_execution_snapshot

    task_id = str(getattr(ctx, "task_id", "") or "")
    try:
        handle = provision_execution_snapshot(
            target_root=target_root, task_id=task_id, snapshot_id=invocation_id)
    except Exception as exc:
        return None, _fail(
            "delegate_start", "execution_snapshot_failed",
            "A private execution snapshot of the write root could not be provisioned "
            f"({type(exc).__name__}: {exc}). The run was NOT started: a mutating "
            "delegated run executes only in its own snapshot, never in the shared tree.",
            target_root=target_root)
    _record_baseline_manifest(drive, task_id, invocation_id, handle)
    return handle, ""


def _record_baseline_manifest(drive: pathlib.Path, task_id: str, invocation_id: str,
                              handle: Any, **extra: Any) -> None:
    """The forensic baseline record beside the capture-to-be. Fail-soft: the BINDING
    itself is durable on the custody request row, and the baseline tree stays
    reconstructable for as long as the snapshot lives. One author for the Git and
    the standalone-payload snapshot paths."""
    try:
        from ouroboros.utils import atomic_write_json, utc_now_iso

        cap_dir = custody.delegated_capture_dir(drive, task_id, invocation_id)
        cap_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(cap_dir / "baseline_manifest.json", {
            "schema_version": 1,
            "created_at": utc_now_iso(),
            "snapshot_id": handle.snapshot_id,
            "baseline_id": handle.baseline_sha,
            "baseline_tree": handle.baseline_tree,
            "manifest_digest": handle.manifest_digest,
            "entry_count": handle.entry_count,
            "target_root": handle.target_root,
            "target_head": handle.target_head,
            "execution_root": handle.path,
            "excluded_untracked": list(handle.excluded_untracked),
            **extra,
        }, trailing_newline=True)
    except Exception:
        log.warning("Failed to write delegated baseline manifest for %s", invocation_id,
                    exc_info=True)


def _capture_block(entry: _RunCustody, cap_dir: pathlib.Path,
                   manifest: Dict[str, Any]) -> Dict[str, Any]:
    """The terminal-payload projection of one captured run patch (C1)."""
    from ouroboros.headless import ARTIFACT_STATUS_READY_WITH_CHANGES

    status = str(manifest.get("status") or "")
    patch_path = cap_dir / "workspace.patch"
    has_patch = status == ARTIFACT_STATUS_READY_WITH_CHANGES and patch_path.exists()
    # The tool-surface read handle (CR1-2): the capture lives on the CANONICAL
    # drive, which a split-drive child cannot reach through a raw absolute path —
    # read_file(root='artifact_store', path=...) resolves this prefix against the
    # canonical root for the owning task, so THIS pair is what the agent reads.
    read_prefix = f"delegated_runs/{cap_dir.name}"
    block: Dict[str, Any] = {
        "status": status or "missing",
        "baseline_id": entry.baseline_sha,
        "execution_root": entry.execution_root,
        "authority_target_root": entry.target_root,
        "patch_artifact": str(patch_path) if has_patch else None,
        "manifest_artifact": str(cap_dir / "workspace_patch.json"),
        "patch_read": ({"root": "artifact_store", "path": f"{read_prefix}/workspace.patch"}
                       if has_patch else None),
        "manifest_read": {"root": "artifact_store", "path": f"{read_prefix}/workspace_patch.json"},
        "sha256": str(manifest.get("sha256") or ""),
        "diffstat": str(manifest.get("diffstat") or ""),
        "note": (
            "NOT APPLIED: the run edited its private execution snapshot only. Nothing "
            "reaches the shared tree until you explicitly call "
            "integrate_delegated_patch(run_id=...) with decision='apply' or 'reject'. "
            "The snapshot and this patch persist until that disposition. Read the "
            "captured diff via read_file(root='artifact_store', path=patch_read.path)."
        ),
    }
    current_head = str(manifest.get("current_head") or "")
    if current_head and entry.baseline_sha and current_head != entry.baseline_sha:
        # Single-writer snapshot: a moved HEAD can only mean the run itself committed.
        # The diff is still measured from the baseline (committed work is captured),
        # but the violation of the no-commit instruction is disclosed, not hidden.
        block["head_moved"] = {
            "baseline": entry.baseline_sha, "current": current_head,
            "note": "the run COMMITTED inside its snapshot despite instructions; its "
                    "committed work is still in the captured diff",
        }
    return block


def _capture_terminal_patch(ctx: ToolContext, entry: Optional[_RunCustody]) -> Optional[Dict[str, Any]]:
    """Capture a terminal mutating run's diff from its execution snapshot, durably.

    Idempotent: the durable ``patch_captured`` flag (replayed) plus the manifest on
    disk mean a re-wait re-reads the existing capture instead of re-diffing. Runs
    only for C1-isolated runs (``execution_root`` recorded); read-only and legacy
    in-place runs return None. Capture failure is disclosed in the block, never
    raised — the settlement and delivery around it must not die on a diff error.
    """
    if entry is None or not entry.execution_root:
        return None
    return capture_terminal_patch_for_drive(custody.custody_root(ctx), entry)


def capture_terminal_patch_for_drive(drive: Any, entry: _RunCustody) -> Optional[Dict[str, Any]]:
    """The drive-rooted core of the terminal capture — same contract, no ToolContext.

    The RECONCILIATION path (owner task gone: orphan sweep, kill-path reconcile,
    pending-invocation recovery) settles mutating runs from a bare drive root, and
    a run settled there with no capture strands the child's work in the snapshot
    with no apply/reject material. One capture author for both paths, so the nanny
    flow and the sweep cannot drift in what a "captured patch" is. Capture only —
    the apply/reject DECISION stays with a live owner, never taken here.

    ``patch_captured`` MEANS "a usable patch artifact exists" (C1-R3): the custody
    row is minted only over a manifest whose own status is ready. A manifest that
    reports its own failure is returned as the failed block but leaves the row
    uncaptured, so every retry point (re-wait, sweep, disposition) stays open —
    and a durable row minted by pre-R3 code over a failed manifest is not trusted
    either: the replay falls through to a fresh capture instead of serving the
    failed manifest forever.
    """
    if entry is None or not entry.execution_root:
        return None
    from ouroboros.headless import (
        ARTIFACT_STATUS_READY_NO_CHANGES,
        ARTIFACT_STATUS_READY_WITH_CHANGES,
    )

    ready = {ARTIFACT_STATUS_READY_WITH_CHANGES, ARTIFACT_STATUS_READY_NO_CHANGES}
    cap_dir = custody.delegated_capture_dir(drive, entry.task_id, entry.snapshot_id or entry.run_id)
    manifest_path = cap_dir / "workspace_patch.json"
    if entry.patch_captured and manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            manifest = {}
        manifest = manifest if isinstance(manifest, dict) else {}
        if str(manifest.get("status") or "") in ready:
            return _capture_block(entry, cap_dir, manifest)
    exec_root = pathlib.Path(entry.execution_root)
    if not exec_root.exists():
        return {
            "status": "missing",
            "baseline_id": entry.baseline_sha,
            "execution_root": entry.execution_root,
            "authority_target_root": entry.target_root,
            "patch_artifact": None,
            "manifest_artifact": None,
            "note": "the private execution snapshot no longer exists on disk, so the "
                    "run's changes cannot be captured. Nothing was applied anywhere.",
        }
    try:
        from ouroboros.headless import write_workspace_patch_artifacts

        cap_dir.mkdir(parents=True, exist_ok=True)
        if entry.authority_source == "skill_payload":
            # The payload-specific adapter over the skill-loader inventory (R1
            # items 5/6): the generic workspace capture's junk/lockfile filters
            # would silently omit legitimate loader-visible files.
            manifest = _write_payload_patch_artifacts(exec_root, cap_dir, entry)
        else:
            # The preflight-head shape pins the capture BASE to the snapshot's baseline
            # commit (never a moved HEAD), reusing the one existing capture primitive —
            # sensitive veto, binary handling, sha256 manifest and all.
            _, manifest = write_workspace_patch_artifacts(
                exec_root, cap_dir,
                task={"metadata": {"workspace_preflight": {"git": {"head": entry.baseline_sha}}}},
            )
    except Exception as exc:
        log.warning("Delegated run patch capture failed for %s", entry.run_id, exc_info=True)
        return {
            "status": "failed",
            "baseline_id": entry.baseline_sha,
            "execution_root": entry.execution_root,
            "authority_target_root": entry.target_root,
            "patch_artifact": None,
            "manifest_artifact": None,
            "note": f"patch capture failed ({type(exc).__name__}: {exc}); the execution "
                    "snapshot is preserved — inspect it directly.",
        }
    if str(manifest.get("status") or "") in ready:
        custody.record_patch_captured(
            drive, entry,
            status=str(manifest.get("status") or ""),
            sha256=str(manifest.get("sha256") or ""),
            patch_size=manifest.get("patch_size"),
            capture_dir=str(cap_dir),
        )
    return _capture_block(entry, cap_dir, manifest)


# -- exact skill-payload delegation (R1) ---------------------------------------
#
# The restored delegated coding target class: an ordinary top-level task selects
# ONE exact non-native skill payload through the existing ResolvedResourceBinding
# vocabulary (root=skill_payload, bucket, skill_name), Claudexor edits a PRIVATE
# standalone Git snapshot of it, and the parent explicitly applies the captured
# harness-authored diff under a whole-payload content-hash CAS. No fourth write
# surface, no live-payload harness writes, no acting+repair composition.

# The profiles allowed to hold this authority. Mirrors the top-level principal
# set of the tool-access policy: child/acting/repair/ephemeral contexts keep
# their current gates and never acquire delegated payload mutation.
_PAYLOAD_PRINCIPAL_PROFILES = frozenset({
    "workspace_task", "external_workspace_task", "self_modification"})


def payload_content_hash(payload_root: Any) -> str:
    """The whole-payload CAS hash — the existing skill-loader inventory hash.

    One definition for snapshot baseline, capture result, and apply-time CAS, so
    the three comparisons are always about the same byte surface. Raises
    ``SkillPayloadUnreadable`` (fail-closed) on unreadable or credential-shaped
    payload files, exactly like the loader itself.
    """
    from ouroboros.skill_loader import compute_content_hash

    return compute_content_hash(pathlib.Path(str(payload_root)))


def _reserved_payload_rel_path(rel: str) -> bool:
    """Name-rule half of reserved-path detection (R1 item 3): lifecycle/control
    filenames and directories from the frozen skill-payload policy, plus an
    explicit ``.git`` rule (a live payload must never receive VCS internals).
    The live-target half (`is_skill_control_plane_path` / owner-state aliases)
    runs at apply time against the real destination paths."""
    from ouroboros.contracts.skill_payload_policy import (
        SKILL_PAYLOAD_CONTROL_DIRNAMES,
        SKILL_PAYLOAD_CONTROL_FILENAMES,
    )

    parts = [part.lower() for part in pathlib.PurePosixPath(str(rel or "")).parts]
    if not parts:
        return False
    if ".git" in parts:
        return True
    if any(part in SKILL_PAYLOAD_CONTROL_DIRNAMES for part in parts):
        return True
    return parts[-1] in SKILL_PAYLOAD_CONTROL_FILENAMES


def _payload_delegation_busy(drive: pathlib.Path, target: pathlib.Path) -> str:
    """A run/invocation that still holds THIS payload open, or "" (R1 item 9).

    Cheap and before any snapshot or gateway work: two concurrent delegations
    against one payload would race the same CAS baseline, so the second is
    refused while the first has undisposed custody (unsettled run, undisposed
    patch, or a pending invocation naming the same target).

    Single-pass (Sol delta, fix 5a): both projections replay ONE pre-read row
    snapshot. Two separate log reads let the holder's REQUESTED→STARTED
    transition land between them — invisible to the first pass as a run and to
    the second as a pending invocation — so a second start slipped through the
    claim lock. Against one snapshot the holder is in exactly one of the two
    states and cannot be missed.
    """
    resolved = _resolved(target)
    rows = list(custody._iter_rows(custody.event_log_path(drive)))
    for run in custody.replay(drive, rows=rows).values():
        if (run.authority_source == "skill_payload"
                and _resolved(run.target_root) == resolved
                and not (run.settled and run.patch_disposed)):
            return run.run_id or run.invocation_id
    for record in custody.pending_invocations(drive, rows=rows):
        if (str(record.get("authority_source") or "") == "skill_payload"
                and _resolved(record.get("target_root")) == resolved):
            return str(record.get("invocation_id") or "")
    return ""


def _payload_selector_refusal(selector_root: str, retry_of: Any, bucket: Any,
                              skill_name: Any) -> str:
    """Argument-shape validation for delegate_start's exact-resource selector."""
    if selector_root and selector_root != "skill_payload":
        return _fail("delegate_start", "unsupported_root",
                     "root supports only 'skill_payload' (an installed non-native "
                     "skill payload). Ordinary workspace delegation takes no root.")
    if selector_root and str(retry_of or "").strip():
        return _fail("delegate_start", "selector_on_retry",
                     "retry_of replays the RECORDED invocation, whose exact resource "
                     "binding is re-resolved from its durable record — a retry call "
                     "carries no selector. Drop root/bucket/skill_name to retry.")
    if selector_root and (not str(bucket or "").strip() or not str(skill_name or "").strip()):
        return _fail("delegate_start", "payload_selector_incomplete",
                     "root='skill_payload' requires bucket and skill_name.")
    if not selector_root and (str(bucket or "").strip() or str(skill_name or "").strip()):
        return _fail("delegate_start", "payload_selector_incomplete",
                     "bucket/skill_name select a skill payload only together with "
                     "root='skill_payload'.")
    return ""


def payload_host_instructions(text: str, skill_name: str) -> str:
    """The payload-run variant of the host instructions (gate fix 3).

    The generic text bans touching the host's "skills" — for a payload run that
    blanket ban would contradict the assignment itself, so it is narrowed and an
    explicit, truthful permission block is appended.
    """
    adjusted = text.replace(
        "do not touch the host's runtime controls, skills, or memory",
        "do not touch the host's runtime controls or memory")
    return adjusted + (
        "\nPAYLOAD ASSIGNMENT: this working tree is a PRIVATE standalone copy of "
        f"the installed skill payload '{skill_name}'. Editing its user-authored "
        "files IS your assignment — the prohibition above does not cover this "
        "copy. Lifecycle/control files (manifest sidecars, review/grants/enabled "
        "state, .git internals) remain off-limits and any change to them is "
        "refused whole at apply. Your host reviews the resulting diff and "
        "explicitly applies it to the live payload afterwards; the skill's "
        "review then re-runs before the new content is relied on.")


# In-process half of the atomic payload start claim; the cross-process half is
# the O_EXCL lockfile below. Both exist because two parallel delegate_start
# calls (same process or two workers) must produce exactly ONE started run.
_PAYLOAD_CLAIM_LOCK = threading.Lock()


def claimed_start_request(
    drive: pathlib.Path, *, claim_target: str, **request_row: Any,
) -> Tuple[bool, str]:
    """Write the START_REQUESTED row, atomically fused with the payload busy check.

    Gate fix 5: an unlocked busy read followed by a later request write let two
    synchronized starts both pass the check and both start. For a payload run
    (``claim_target`` non-empty) the busy check and the durable request write
    happen under ONE claim lock, so exactly one caller wins; the loser gets the
    holder id back and refuses typed. Non-payload rows pass straight through.
    Returns ``(requested, busy_holder)``.
    """
    if not claim_target:
        return custody.record_start_requested(drive, **request_row), ""
    from ouroboros.platform_layer import (
        acquire_exclusive_file_lock,
        release_exclusive_file_lock,
    )

    lock_path = pathlib.Path(drive) / "state" / ".payload_delegation_claim.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _PAYLOAD_CLAIM_LOCK:
        fd = acquire_exclusive_file_lock(lock_path, timeout_sec=20.0, stale_sec=120.0)
        if fd is None:
            # Fail CLOSED: without the cross-process half the claim would be a
            # plain unlocked read again — the exact race this fix removes.
            return False, "(payload claim lock unavailable — another start holds it)"
        try:
            holder = _payload_delegation_busy(drive, pathlib.Path(claim_target))
            if holder:
                return False, holder
            return custody.record_start_requested(drive, **request_row), ""
        finally:
            if fd is not None:
                release_exclusive_file_lock(lock_path, fd)


def _payload_mutation_authority(
    ctx: ToolContext, drive: pathlib.Path, bucket: str, skill_name: str,
    binding: Any,
) -> Tuple[Optional[Any], Optional[Dict[str, Any]], str]:
    """The payload counterpart of ``_mutation_authority`` (R1 item 1).

    Returns ``(run_shape, authority_record, refusal)``. The authority is a FRESH
    ``ResolvedResourceBinding`` for ``skill_payload.write`` — the registry builds
    it privately for a selector-carrying ``delegate_start``; a direct caller
    (tests, in-process reuse) rebuilds it here through the same one authorizer.
    The record carries the host-minted semantic ``resource_ref`` that custody
    stores durably and retry/apply later re-resolve.
    """
    from ouroboros.subagents import delegated_run_shape
    from ouroboros.tool_access import active_tool_profile, build_resolved_resource_binding

    b, s = str(bucket or "").strip(), str(skill_name or "").strip()
    if binding is None:
        # Policy BEFORE lookup (Fable F4): a caller whose profile cannot hold
        # payload-write authority gets an authority denial, not a target-
        # resolution flavor from the binding builder's ValueError.
        profile = str(active_tool_profile(ctx))
        if profile not in _PAYLOAD_PRINCIPAL_PROFILES:
            return None, None, _fail(
                "delegate_start", "payload_delegation_forbidden",
                "This is an AUTHORITY denial, not a lookup failure: only an "
                "ordinary top-level task may delegate skill-payload work. Child, "
                "acting, repair and ephemeral contexts keep their existing "
                "narrower gates.",
                profile=profile)
        try:
            binding = build_resolved_resource_binding(
                ctx, root="skill_payload", operation="write", path=".",
                bucket=b, skill_name=s)
        except ValueError as exc:
            return None, None, _fail(
                "delegate_start", "payload_target_unresolved",
                f"The selected skill payload could not be bound: {exc}. Delegation "
                "targets one EXISTING exact non-native payload. For a NEW skill, "
                "create its manifest first (write_file root='skill_payload', "
                "bucket='external', path='SKILL.md'), then delegate the "
                "now-existing payload.",
                bucket=b, skill_name=s)
    if getattr(binding, "root", "") != "skill_payload" or getattr(binding, "operation", "") != "write":
        return None, None, _fail(
            "delegate_start", "payload_binding_mismatch",
            "delegate_start(root='skill_payload') requires a skill_payload.write "
            "binding; got "
            f"{getattr(binding, 'root', '')!r}/{getattr(binding, 'operation', '')!r}.")
    if getattr(binding, "profile", "") not in _PAYLOAD_PRINCIPAL_PROFILES:
        return None, None, _fail(
            "delegate_start", "payload_delegation_forbidden",
            "Only an ordinary top-level task may delegate skill-payload work. "
            "Child, acting, repair and ephemeral contexts keep their existing "
            "narrower gates.",
            profile=str(getattr(binding, "profile", "")))
    target = pathlib.Path(binding.base_path)
    if not target.is_dir():
        return None, None, _fail(
            "delegate_start", "payload_target_missing",
            "The selected skill payload does not exist. Create its manifest first "
            "(write_file root='skill_payload', bucket='external', path='SKILL.md'), "
            "then delegate the now-existing payload.",
            bucket=b, skill_name=s)
    busy = _payload_delegation_busy(drive, target)
    if busy:
        return None, None, _fail(
            "delegate_start", "payload_delegation_busy",
            "Another delegated run already holds this exact payload open (its "
            "custody is not yet settled AND disposed). Finish that run — "
            "delegate_wait it and integrate_delegated_patch its capture — before "
            "starting another delegation against the same skill.",
            holder=busy, target_root=str(target))
    record = {
        "target_root": str(target),
        "source": "skill_payload",
        "capture_mode": _CAPTURE_DELEGATED_SNAPSHOT,
        "state_drive_root": str(binding.state_drive_root),
        # The host-minted semantic reference: replayed by retry, consumed by the
        # owned apply rebind. ``payload_hash`` is filled by snapshot provisioning
        # (the pre-copy loader hash is the CAS baseline).
        "resource_ref": {
            "root": "skill_payload",
            "source": str(binding.source or ""),
            "skill_name": str(binding.skill_name or ""),
            "target_root": str(target),
            "payload_hash": "",
        },
    }
    return delegated_run_shape(True), record, ""


def _provision_payload_snapshot(
    ctx: ToolContext, drive: pathlib.Path, record: Dict[str, Any], invocation_id: str,
) -> Tuple[Optional[Any], str]:
    """Provision the standalone private Git snapshot for one payload run (R1 §9.2).

    Same durability contract as ``_provision_snapshot``: registered in the
    worktree registry and described by the baseline manifest BEFORE any start
    intent is recorded. On success the record's ``resource_ref.payload_hash``
    is filled with the pre-copy loader hash (the CAS baseline).
    """
    from ouroboros.subagent_worktrees import provision_payload_snapshot

    task_id = str(getattr(ctx, "task_id", "") or "")
    try:
        handle = provision_payload_snapshot(
            target_root=record["target_root"], task_id=task_id,
            snapshot_id=invocation_id)
    except Exception as exc:
        return None, _fail(
            "delegate_start", "execution_snapshot_failed",
            "A private standalone snapshot of the skill payload could not be "
            f"provisioned ({type(exc).__name__}: {exc}). The run was NOT started: "
            "a mutating delegated run executes only in its own snapshot, never "
            "in the live payload.",
            target_root=record["target_root"])
    record["resource_ref"]["payload_hash"] = handle.payload_hash
    _record_baseline_manifest(drive, task_id, invocation_id, handle,
                              payload_hash=handle.payload_hash,
                              capture_kind="skill_payload")
    return handle, ""


def _rebind_payload_reference(
    ctx: ToolContext, resource_ref: Dict[str, Any], recorded_target: str, *,
    tool: str, context: str,
) -> Tuple[Optional[pathlib.Path], Optional[Any], str]:
    """Re-resolve a recorded semantic payload reference against CURRENT authority.

    Consumed by retry and by the owned apply (R1 item 3): the fresh binding path
    must equal the recorded target, so a moved/collided/removed/renamed skill is
    a typed refusal, never a write through a stale physical path. Returns
    ``(live_target, fresh_binding, refusal)``.
    """
    from ouroboros.tool_access import build_resolved_resource_binding

    ref = resource_ref if isinstance(resource_ref, dict) else {}
    source = str(ref.get("source") or "")
    skill = str(ref.get("skill_name") or "")
    recorded = _resolved(recorded_target or ref.get("target_root"))
    if not source or not skill or recorded is None:
        return None, None, _fail(
            tool, "payload_reference_incomplete",
            "This run's durable record carries no complete semantic payload "
            "reference, so its target cannot be re-authorized. Start a new "
            "delegation for the current payload.", context=context)
    try:
        binding = build_resolved_resource_binding(
            ctx, root="skill_payload", operation="write", path=".",
            bucket=source, skill_name=skill)
    except ValueError as exc:
        return None, None, _fail(
            tool, "payload_target_unresolved",
            "The recorded skill payload could not be re-bound under current "
            f"authority ({exc}). The skill may have been removed, renamed, or "
            "collided with another location since the run was recorded.",
            source=source, skill_name=skill, context=context)
    fresh = _resolved(binding.base_path)
    if fresh is None or fresh != recorded:
        return None, None, _fail(
            tool, "payload_target_moved",
            "The recorded skill payload no longer resolves to the same physical "
            "target under current authority. Refusing to act through a stale "
            "physical path.",
            recorded_target=str(recorded), current_target=str(fresh or ""),
            context=context)
    return fresh, binding, ""


def _write_payload_patch_artifacts(
    exec_root: pathlib.Path, cap_dir: pathlib.Path, entry: _RunCustody,
) -> Dict[str, Any]:
    """The payload-specific terminal capture (R1 items 5/6), same artifact contract.

    Stages exactly the UNION of baseline and final loader-visible paths in the
    snapshot's private index and emits ``workspace.patch`` (``git diff --binary``
    so git's binary heuristic cannot veto UTF-8-with-NUL content) plus the
    ``workspace_patch.json`` manifest against the synthetic baseline. Junk the
    loader excludes never enters the candidate. A candidate add/modify of a
    genuinely non-UTF-8 file is a typed capture FAILURE (the permanent text-only
    execution contract); deletions and untouched baseline files are unaffected.
    Reserved lifecycle/control paths never block capture: they are reported as
    ``blocked_reserved_paths`` and refused whole at apply (the candidate is
    always preserved for the parent's decision).
    """
    import hashlib
    import subprocess

    from ouroboros.headless import (
        ARTIFACT_STATUS_FAILED,
        ARTIFACT_STATUS_READY_NO_CHANGES,
        ARTIFACT_STATUS_READY_WITH_CHANGES,
    )
    from ouroboros.skill_loader import _iter_payload_files
    from ouroboros.subagent_worktrees import isolated_git_env, reset_snapshot_git_config
    from ouroboros.utils import atomic_write_json, utc_now_iso

    git_env = isolated_git_env()
    diff_isolation = ("--no-ext-diff", "--no-textconv", "--no-renames")

    def _git(*args: str, input_bytes: bytes = b"") -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=str(exec_root), env=git_env,
                              capture_output=True, input=input_bytes or None)

    def _manifest(status: str, **extra: Any) -> Dict[str, Any]:
        payload = {
            "schema_version": 1, "created_at": utc_now_iso(), "status": status,
            "capture_kind": "skill_payload",
            "baseline_payload_hash": str((entry.resource_ref or {}).get("payload_hash") or ""),
            **extra,
        }
        atomic_write_json(cap_dir / "workspace_patch.json", payload, trailing_newline=True)
        return payload

    # The child held a shell inside this snapshot: neutralize the snapshot-local
    # .git/config (a child-written `diff.<driver>.command` executed in the PARENT
    # process — reproduced by review) and run every parent-side git command with
    # system and global config disabled. Diff commands additionally pin
    # --no-ext-diff / --no-textconv / --no-renames so no config or attribute can
    # substitute content, and renames-off keeps the candidate parser total.
    try:
        reset_snapshot_git_config(exec_root)
    except OSError as exc:
        return _manifest(ARTIFACT_STATUS_FAILED,
                         note=f"snapshot git config could not be reset: {exc}")
    baseline = str(entry.baseline_sha or "")
    resolved_root = exec_root.resolve()
    # Final loader-visible inventory; raises SkillPayloadUnreadable on
    # credential-shaped files (the existing refusal — caller discloses it typed).
    final_rel = sorted(
        path.relative_to(resolved_root).as_posix()
        for path in _iter_payload_files(resolved_root)
    )
    listed = _git("ls-tree", "-r", "--name-only", "-z", baseline)
    if listed.returncode != 0:
        detail = (listed.stderr or listed.stdout or b"").decode("utf-8", errors="replace")
        return _manifest(ARTIFACT_STATUS_FAILED, note=f"baseline unreadable: {detail.strip()[:300]}")
    baseline_rel = [
        chunk.decode("utf-8", errors="surrogateescape")
        for chunk in (listed.stdout or b"").split(b"\0") if chunk
    ]
    def _z(paths: list) -> bytes:
        return b"\0".join(
            p.encode("utf-8", errors="surrogateescape") for p in paths) + b"\0"

    # Only the FINAL loader-visible inventory rides as content. A baseline path
    # absent from the final inventory is staged as a DELETION even when something
    # still sits on disk there (e.g. a file replaced by an escaping symlink,
    # which the inventory drops) — --add --remove over such a path would stage
    # the on-disk escape artifact itself into the candidate (reproduced).
    dropped = sorted(set(baseline_rel) - set(final_rel))
    staged = _git("update-index", "-z", "--add", "--remove", "--stdin",
                  input_bytes=_z(sorted(final_rel)))
    if staged.returncode == 0 and dropped:
        staged = _git("update-index", "-z", "--force-remove", "--stdin",
                      input_bytes=_z(dropped))
    if staged.returncode != 0:
        detail = (staged.stderr or staged.stdout or b"").decode("utf-8", errors="replace")
        return _manifest(ARTIFACT_STATUS_FAILED, note=f"staging failed: {detail.strip()[:300]}")
    named = _git("diff", *diff_isolation, "--cached", "--name-only", "-z", baseline)
    if named.returncode != 0:
        detail = (named.stderr or named.stdout or b"").decode("utf-8", errors="replace")
        return _manifest(ARTIFACT_STATUS_FAILED, note=f"diff failed: {detail.strip()[:300]}")
    changed = sorted(
        chunk.decode("utf-8", errors="surrogateescape")
        for chunk in (named.stdout or b"").split(b"\0") if chunk
    )
    result_hash = payload_content_hash(resolved_root)
    head = _git("rev-parse", "HEAD")
    current_head = (head.stdout or b"").decode("utf-8", errors="replace").strip()
    if not changed:
        return _manifest(ARTIFACT_STATUS_READY_NO_CHANGES, sha256="", diffstat="",
                         tracked_changed=[], untracked_included=[],
                         blocked_reserved_paths=[], result_content_hash=result_hash,
                         current_head=current_head)
    non_utf8 = []
    for rel in changed:
        if rel in dropped:
            continue  # rides as a deletion; on-disk leftovers are not content
        candidate = resolved_root / rel
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            candidate.read_bytes().decode("utf-8", "strict")
        except (OSError, UnicodeDecodeError):
            non_utf8.append(rel)
    if non_utf8:
        return _manifest(
            ARTIFACT_STATUS_FAILED, non_utf8_paths=non_utf8,
            note="the candidate adds/modifies non-UTF-8 payload content, which the "
                 "permanent text-only skill contract refuses "
                 f"({', '.join(non_utf8[:5])}); the snapshot is preserved")
    diff = _git("diff", *diff_isolation, "--cached", "--binary", baseline)
    if diff.returncode != 0:
        detail = (diff.stderr or diff.stdout or b"").decode("utf-8", errors="replace")
        return _manifest(ARTIFACT_STATUS_FAILED, note=f"patch emit failed: {detail.strip()[:300]}")
    patch_bytes = diff.stdout or b""
    (cap_dir / "workspace.patch").write_bytes(patch_bytes)
    stat = _git("diff", *diff_isolation, "--cached", "--shortstat", baseline)
    return _manifest(
        ARTIFACT_STATUS_READY_WITH_CHANGES,
        sha256=hashlib.sha256(patch_bytes).hexdigest(),
        patch_size=len(patch_bytes),
        diffstat=(stat.stdout or b"").decode("utf-8", errors="replace").strip(),
        tracked_changed=changed,
        untracked_included=[],
        blocked_reserved_paths=[p for p in changed if _reserved_payload_rel_path(p)],
        result_content_hash=result_hash,
        current_head=current_head,
    )


def _payload_reserved_paths(
    ordered: list, target: pathlib.Path, state_root: pathlib.Path,
) -> Tuple[list, str]:
    """Reserved/escaping destinations among the patch's touched paths (R1 item 3).

    Name rules plus the LIVE-target predicates of the frozen skill-payload
    policy (control-plane paths and owner-state hardlink aliases), judged
    against the real destination each path lands on. Returns
    ``(reserved_paths, escape_refusal)``.
    """
    from ouroboros.contracts.skill_payload_policy import (
        is_skill_control_plane_path,
        is_skill_owner_state_alias,
    )

    resolved_target = target.resolve()
    reserved = []
    for rel in ordered:
        if pathlib.PurePosixPath(rel).is_absolute() or _reserved_payload_rel_path(rel):
            reserved.append(rel)
            continue
        live = _resolved(target / rel)
        if live is None:
            return [], f"touched path {rel!r} cannot be resolved"
        try:
            live.relative_to(resolved_target)
        except ValueError:
            return [], f"touched path {rel!r} escapes the payload root"
        if is_skill_control_plane_path(live, state_root) or is_skill_owner_state_alias(live, state_root):
            reserved.append(rel)
    return sorted(set(reserved)), ""


def _candidate_symlink_escapes(
    patch_path: pathlib.Path, target: pathlib.Path,
) -> Tuple[list, str]:
    """Symlink-introducing patch entries whose target would escape the LIVE payload.

    Containment is judged on the CANDIDATE, not the live preimage (gate fix 1):
    a hunk that lands a mode-120000 entry gets its link target resolved as it
    would land under the live payload root; an escaping resolution is refused
    exactly like a ``../`` path escape. Capture pins ``--no-renames``, so every
    symlink introduction appears as a full new-file/new-mode hunk and the parse
    is total; any unparseable symlink entry fails CLOSED as a parse refusal.
    Returns ``(escaping_rel_paths, parse_refusal)``.
    """
    import os

    try:
        raw = patch_path.read_bytes()
    except OSError as exc:
        return [], f"candidate patch unreadable: {exc}"
    resolved_target = target.resolve()
    escapes: list = []
    path, is_link, link_target, in_hunk = "", False, None, False

    def _flush() -> str:
        nonlocal path, is_link, link_target, in_hunk
        if is_link:
            if not path or link_target is None:
                return "a symlink-introducing patch entry could not be parsed"
            dest = resolved_target / pathlib.PurePosixPath(path)
            cand = (pathlib.Path(link_target) if os.path.isabs(link_target)
                    else dest.parent / link_target)
            landed = _resolved(cand)
            if landed is None or not (
                    landed == resolved_target or resolved_target in landed.parents):
                escapes.append(path)
        path, is_link, link_target, in_hunk = "", False, None, False
        return ""

    for line in raw.split(b"\n"):
        if line.startswith(b"diff --git "):
            err = _flush()
            if err:
                return [], err
        elif line in (b"new file mode 120000", b"new mode 120000"):
            is_link = True
        elif line.startswith(b"+++ "):
            name = line[4:].split(b"\t")[0].decode("utf-8", errors="surrogateescape")
            if name.startswith("b/"):
                path = name[2:]
            elif name.startswith('"'):
                # git-quoted (control/non-ASCII bytes in the name): fail closed
                # rather than guess the octal unescaping for a symlink entry.
                path = ""
        elif line.startswith(b"@@"):
            in_hunk = True
        elif is_link and in_hunk and line.startswith(b"+") and link_target is None:
            link_target = line[1:].decode("utf-8", errors="surrogateescape")
    err = _flush()
    if err:
        return [], err
    return sorted(set(escapes)), ""


def _finalize_payload_apply(
    ctx: ToolContext, *, rid: str, reason: str, target: pathlib.Path,
    touched: list, ordered: list, manifest: Dict[str, Any],
    state_root: pathlib.Path, skill_name: str, dispose: Any, already: bool,
) -> str:
    """The ONE post-apply finalizer (gate fix 4): advisory invalidation →
    extension reconcile → verdict artifact → disposal, in this order, for BOTH
    the fresh-apply and the already-applied/idempotent outcomes — an earlier
    attempt may have died after mutating but before invalidation/reconcile.
    A reconcile queue-write failure degrades the receipt honestly instead of
    claiming the extension was reconciled off.
    """
    from ouroboros.tools.subagent_integration import (
        _unwritten_disposition_text,
        _write_verdict,
    )

    try:
        from ouroboros.review_state import invalidate_advisory_after_mutation

        invalidate_advisory_after_mutation(
            pathlib.Path(getattr(ctx, "drive_root", ".")), mutation_root=target,
            changed_paths=ordered, source_tool="integrate_delegated_patch")
    except Exception:
        pass
    reconcile_err = ""
    try:
        # A stale ENABLED extension must stop being live until re-review (R1
        # item 10); enablement/grants state itself is untouched by delegation.
        from ouroboros.extension_reconcile_queue import request_extension_reconcile

        request_extension_reconcile(state_root, skill_name,
                                    reason="delegated_payload_apply", source="worker")
    except Exception as exc:
        log.warning("extension reconcile request failed after payload apply %s",
                    rid, exc_info=True)
        reconcile_err = f"{type(exc).__name__}: {exc}"
    verdict_path = _write_verdict(
        ctx, f"run_{rid}", outcome="applied",
        reason=reason or ("already applied" if already else ""),
        files=touched, manifest=manifest, applied=True, conflicts=[], protected=[],
        target=str(target))
    recorded, note = dispose("applied", True)
    if not recorded:
        return _unwritten_disposition_text(rid, str(target), "applied", True)
    staleness = (
        "The payload CONTENT CHANGED, so any prior skill review is now STALE for "
        "the new content hash: run skill_preflight and skill_review before "
        "relying on this skill. Enablement and grants were not changed by this "
        "apply; "
        + ("a stale enabled extension is reconciled off until re-review."
           if not reconcile_err else
           f"WARNING: the extension reconcile could NOT be queued ({reconcile_err})"
           " — the review staleness above still holds, but a stale enabled "
           "extension may remain live until the next restart or a manual "
           "reconcile."))
    if already:
        return (f"OK: the live payload ALREADY carries run {rid}'s captured result "
                f"(content hash match) — recorded as applied, nothing re-applied. "
                f"Verdict: {verdict_path or '(unwritten)'}.\n{staleness}{note}")
    return (
        f"✅ Integrated delegated run {rid}'s patch into the live skill payload "
        f"{target} ({len(ordered)} file(s)). No .git, index, or staging was created "
        f"there.\n{str(manifest.get('diffstat') or '').strip()}\n"
        f"Verdict: {verdict_path or '(unwritten)'}. The standalone snapshot is "
        f"released.\n{staleness}{note}")


def integrate_payload_patch(
    ctx: ToolContext, *, drive: pathlib.Path, entry: _RunCustody, rid: str,
    decision: str, reason: str, cap_dir: pathlib.Path,
    manifest: Dict[str, Any], patch_path: pathlib.Path,
) -> str:
    """Apply or reject ONE payload run's captured patch into the LIVE payload (R1 item 3).

    The payload counterpart of the Git apply branch, entered by
    ``integrate_delegated_patch`` after its owned custody lookup and shared
    early refusals. Differences from the Git branch, each deliberate: the
    target is the live NON-Git payload (no active-root comparison, no ``.git``
    requirement, no staging); target authority is a FRESH exact binding that
    must equal the recorded target; drift is the whole-payload loader
    content-hash CAS (already-applied content disposes as applied,
    idempotently); reserved lifecycle/control destinations refuse the WHOLE
    apply with the candidate preserved; ``git apply`` runs with the live
    payload as its cwd (index-free apply writes relative to cwd — probed);
    a successful apply queues the existing extension reconcile so a stale
    enabled extension stops being live until re-review.
    """
    import subprocess

    from ouroboros.headless import (
        ARTIFACT_STATUS_READY_NO_CHANGES,
        ARTIFACT_STATUS_READY_WITH_CHANGES,
    )
    from ouroboros.tools.subagent_integration import (
        _READY_CAPTURE_STATUSES,
        _capture_failed_refusal,
        _dispose_delegated,
        _patch_touched_paths,
        _sha256_file,
        _unwritten_disposition_text,
        _write_verdict,
    )

    status = str(manifest.get("status") or "")
    touched = [str(p) for p in (manifest.get("tracked_changed") or [])]
    snapshot_key = entry.snapshot_id or entry.run_id

    def _dispose(disposition: str, cleanup: bool) -> Tuple[bool, str]:
        return _dispose_delegated(drive, entry, snapshot_key, reason, disposition, cleanup)

    if decision == "reject":
        # A reject RELEASES the snapshot (the child's only copy): ready-only. It
        # deliberately needs NO fresh target authority — the owner can release
        # retained material even after the skill was deleted or revoked.
        if status not in _READY_CAPTURE_STATUSES:
            return _capture_failed_refusal(
                rid, status, "a reject would release the snapshot over it")
        verdict_path = _write_verdict(
            ctx, f"run_{rid}", outcome="rejected", reason=reason, files=touched,
            manifest=manifest, applied=False, conflicts=[], protected=[],
            target=str(entry.target_root))
        recorded, note = _dispose("rejected", cleanup=True)
        if not recorded:
            return _unwritten_disposition_text(rid, str(entry.target_root), "rejected", False)
        return (
            f"🚫 Rejected delegated run {rid}'s captured payload patch ({len(touched)} "
            f"file(s) not applied); the live skill payload is unchanged and the "
            f"standalone snapshot is released. Verdict: {verdict_path or '(unwritten)'}. "
            f"Reason: {reason or '(none)'}.{note}")

    if status == ARTIFACT_STATUS_READY_NO_CHANGES:
        recorded, note = _dispose("applied", cleanup=True)
        if not recorded:
            return _unwritten_disposition_text(rid, str(entry.target_root), "applied", False)
        return (f"OK: delegated run {rid} changed NOTHING in its payload snapshot; "
                f"there is no patch to apply and the snapshot is released.{note}")
    if status != ARTIFACT_STATUS_READY_WITH_CHANGES:
        return (
            f"⚠️ INTEGRATE_DELEGATED_NO_CAPTURE: run {rid}'s payload capture status is "
            f"{status or 'missing'!r} — no applicable patch "
            f"({str(manifest.get('note') or '')[:300]}). A failed capture keeps the "
            "snapshot for direct inspection; fix the cause, then retry.")
    if not patch_path.exists():
        return f"⚠️ INTEGRATE_PATCH_MISSING: captured patch not found at {patch_path}."
    expected_digest = str(manifest.get("sha256") or "")
    if expected_digest and _sha256_file(patch_path) != expected_digest:
        return (f"⚠️ INTEGRATE_PATCH_CORRUPT: sha256 mismatch for run {rid}; "
                "refusing to apply.")

    target, binding, rebind_refusal = _rebind_payload_reference(
        ctx, entry.resource_ref, entry.target_root,
        tool="integrate_delegated_patch", context=f"run_id={rid}")
    if rebind_refusal:
        return rebind_refusal
    patch_touched, parse_error = _patch_touched_paths(patch_path, target)
    if parse_error:
        return (f"⚠️ INTEGRATE_PATCH_UNREADABLE: cannot parse run {rid}'s captured "
                f"patch (git apply --numstat failed): {parse_error[:300]}")
    ordered = sorted(patch_touched)
    state_root = pathlib.Path(binding.state_drive_root)
    reserved, escape = _payload_reserved_paths(ordered, target, state_root)
    if not escape:
        # The CANDIDATE is judged too: a patch that lands an escaping symlink is
        # refused whole, exactly like a ../ path escape (gate fix 1).
        link_escapes, escape = _candidate_symlink_escapes(patch_path, target)
        reserved = sorted(set(reserved) | set(link_escapes))
    if escape:
        return (f"⚠️ INTEGRATE_DELEGATED_PATH_ESCAPE: run {rid}'s patch was NOT "
                f"applied — {escape}. The snapshot and the patch are preserved.")
    if reserved:
        verdict_path = _write_verdict(
            ctx, f"run_{rid}", outcome="blocked_reserved_paths", reason=reason,
            files=touched, manifest=manifest, applied=False, conflicts=reserved,
            protected=reserved, target=str(target))
        return (
            f"⚠️ INTEGRATE_DELEGATED_RESERVED_PATHS: run {rid}'s patch touches "
            f"{len(reserved)} reserved lifecycle/control or escaping-symlink "
            f"path(s) ({', '.join(reserved[:5])}{' …' if len(reserved) > 5 else ''}), "
            "so the WHOLE apply is refused — nothing was partially filtered or "
            "applied. The exact patch and the snapshot are preserved: read the "
            "patch, have the change redone without those paths, or "
            "integrate_delegated_patch(decision='reject') to discard. "
            f"Verdict: {verdict_path or '(unwritten)'}.")

    baseline_hash = str((entry.resource_ref or {}).get("payload_hash")
                        or manifest.get("baseline_payload_hash") or "")
    result_hash = str(manifest.get("result_content_hash") or "")
    skill_name = str((entry.resource_ref or {}).get("skill_name") or "")
    if not baseline_hash:
        return (f"⚠️ INTEGRATE_DELEGATED_BASELINE_UNVERIFIABLE: run {rid} carries no "
                "recorded baseline payload hash, so drift cannot be judged. Nothing "
                "was changed; the snapshot and the patch are preserved.")
    try:
        live_hash = payload_content_hash(target)
    except Exception as exc:
        return (f"⚠️ INTEGRATE_DELEGATED_BASELINE_UNVERIFIABLE: the live payload "
                f"could not be hashed ({type(exc).__name__}: {exc}). Nothing was "
                "changed; the snapshot and the patch are preserved.")
    if live_hash != baseline_hash:
        if result_hash and live_hash == result_hash:
            # Already applied (a crashed prior attempt landed the patch before its
            # disposition row): dispose as applied instead of a false CAS conflict,
            # through the SAME finalizer — the prior attempt may have died before
            # its advisory invalidation and extension reconcile (gate fix 4).
            return _finalize_payload_apply(
                ctx, rid=rid, reason=reason, target=target, touched=touched,
                ordered=ordered, manifest=manifest, state_root=state_root,
                skill_name=skill_name, dispose=_dispose, already=True)
        verdict_path = _write_verdict(
            ctx, f"run_{rid}", outcome="baseline_drift", reason=reason, files=touched,
            manifest=manifest, applied=False, conflicts=[f"live={live_hash[:12]}",
            f"baseline={baseline_hash[:12]}"], protected=[], target=str(target))
        return (
            f"⚠️ INTEGRATE_CONFLICT: the live payload {target} CHANGED since run "
            f"{rid}'s snapshot was taken (whole-payload content hash differs), so its "
            "patch was NOT applied. YOU own this conflict: the snapshot and the patch "
            "are preserved — reconcile the payload with the captured diff, then retry, "
            "or integrate_delegated_patch(decision='reject') to discard. "
            f"Verdict: {verdict_path or '(unwritten)'}.")

    if not custody.record_patch_apply_started(drive, entry, target_root=str(target)):
        return (f"⚠️ INTEGRATE_INTENT_UNWRITTEN: the durable apply-intent row for run "
                f"{rid} could not be written. Refusing to mutate; fix the drive/event "
                "log and retry. Nothing was changed.")
    # Index-free apply with cwd = the LIVE payload (R1 item 3, probed): no .git,
    # no index, no staging is created in the live payload. Atomic on failure.
    # Config-isolated like every parent-side git invocation of this surface.
    from ouroboros.subagent_worktrees import isolated_git_env

    proc = subprocess.run(["git", "apply", str(patch_path)], cwd=str(target),
                          capture_output=True, text=True, env=isolated_git_env())
    if proc.returncode != 0:
        custody.record_patch_apply_resolved(drive, entry, reason="apply_failed")
        stderr = (proc.stderr or proc.stdout or "").strip()
        verdict_path = _write_verdict(
            ctx, f"run_{rid}", outcome="conflict", reason=reason, files=touched,
            manifest=manifest, applied=False, conflicts=[stderr[:500]], protected=[],
            target=str(target))
        return (
            f"⚠️ INTEGRATE_CONFLICT: applying run {rid}'s patch into {target} did not "
            f"apply cleanly (git apply is atomic — the payload is unchanged). git "
            f"said: {stderr[:600]}\nThe snapshot and the patch are preserved; "
            "reconcile and retry, or integrate_delegated_patch(decision='reject'). "
            f"Verdict: {verdict_path or '(unwritten)'}.")
    return _finalize_payload_apply(
        ctx, rid=rid, reason=reason, target=target, touched=touched,
        ordered=ordered, manifest=manifest, state_root=state_root,
        skill_name=skill_name, dispose=_dispose, already=False)


__all__ = [
    "_CAPTURE_DELEGATED_SNAPSHOT",
    "_RetryBinding",
    "_capture_block",
    "_capture_terminal_patch",
    "_fail",
    "_mutation_authority",
    "_provision_snapshot",
    "_resolve_retry_invocation",
    "_resolved",
    "_retry_binding_refusal",
    "_validated_invocation",
    "capture_terminal_patch_for_drive",
    "claimed_start_request",
    "integrate_payload_patch",
    "payload_content_hash",
    "payload_host_instructions",
]
