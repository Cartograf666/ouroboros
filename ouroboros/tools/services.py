"""Task-scoped long-running service manager."""

from __future__ import annotations

import json
import os
import pathlib
import re
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ouroboros.observability import redact_projection, write_blob
from ouroboros.platform_layer import (
    bootstrap_process_path,
    kill_process_group_id,
    kill_process_tree,
    process_group_id,
    process_group_is_alive,
)
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tool_access import resolve_shell_cwd
from ouroboros.utils import append_jsonl, utc_now_iso
from ouroboros.workspace_executor import executor_ref_from_ctx
from ouroboros.workspace_executor import kill_all_services as executor_kill_all_services
from ouroboros.workspace_executor import map_host_path as executor_map_host_path
from ouroboros.workspace_executor import service_logs as executor_service_logs
from ouroboros.workspace_executor import service_status as executor_service_status
from ouroboros.workspace_executor import start_service as executor_start_service
from ouroboros.workspace_executor import stop_service as executor_stop_service
from ouroboros.workspace_executor import stop_task_services as executor_stop_task_services


@dataclass
class ServiceRecord:
    name: str
    service_id: str
    task_id: str
    cmd: List[str]
    cwd: str
    log_path: pathlib.Path
    proc: subprocess.Popen
    pgid: int = 0
    started_at: float = field(default_factory=time.time)
    readiness: Dict[str, Any] = field(default_factory=dict)
    ready: bool = False
    outputs: List[str] = field(default_factory=list)
    cwd_root: str = ""
    before_outputs: Dict[str, tuple[bool, int, str]] = field(default_factory=dict)
    keep_alive: bool = False
    # The repository-writer admission lease this service holds while it runs inside the managed
    # checkout (see `_service_writes_managed_repo`). Empty for every service rooted elsewhere.
    repo_writer_lease: str = ""


_LOCK = threading.Lock()
_SERVICES: Dict[str, ServiceRecord] = {}
_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_MAX_SERVICE_LOG_BLOB_BYTES = 5_000_000
_MAX_SERVICE_LOG_TAIL_CHARS = 80_000
# Mirrors `process_custody._REPO_WRITER_KILL_GRACE_SEC`: how long a signalled process group is
# allowed to take to actually disappear before we call it a survivor.
_REPO_WRITER_GROUP_REAP_GRACE_SEC = 3.0


from ouroboros.workspace_executor import service_key as _service_key


def task_service_teardown(ctx: ToolContext) -> str:
    """Task-level service teardown policy: ``stop`` (default) or ``keep``.

    ``keep`` means services started by this task survive task finalization so
    an external party (benchmark verifier, the owner's own shell) can still
    reach them. They remain custody-ledgered and die with the server session.
    """
    meta = getattr(ctx, "task_metadata", None)
    if isinstance(meta, dict) and str(meta.get("service_teardown") or "").strip().lower() == "keep":
        return "keep"
    return "stop"


def _executor_can_run_cwd(ctx: ToolContext, workdir: pathlib.Path) -> bool:
    executor_ref = executor_ref_from_ctx(ctx)
    if executor_ref is None:
        return False
    try:
        executor_map_host_path(executor_ref, pathlib.Path(workdir).resolve(strict=False))
        return True
    except Exception:
        return False


def _tail(path: pathlib.Path, chars: int) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    limit = max(0, int(chars))
    with path.open("rb") as fh:
        fh.seek(max(0, size - limit))
        data = fh.read(limit)
    return data.decode("utf-8", errors="replace")


def _sanitize_service_name(name: str) -> tuple[str, str]:
    service_name = str(name or "service").strip() or "service"
    if not _SERVICE_NAME_RE.fullmatch(service_name):
        return "", "⚠️ TOOL_ARG_ERROR (start_service): name must match [A-Za-z0-9_.-]{1,80}."
    return service_name, ""


def _readiness_timeout(readiness: Dict[str, Any] | None) -> tuple[float, str]:
    raw = (readiness or {}).get("timeout_sec", 5)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0, "⚠️ TOOL_ARG_ERROR (start_service): readiness.timeout_sec must be numeric."
    if value < 0:
        return 0.0, "⚠️ TOOL_ARG_ERROR (start_service): readiness.timeout_sec must be non-negative."
    return min(value, 25.0), ""


def _service_env() -> Dict[str, str]:
    allowed_exact = {
        "PATH",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "NODE_PATH",
        "SystemRoot",
        "SYSTEMROOT",
        "WINDIR",
        "windir",
        "COMSPEC",
        "ComSpec",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
        "PROGRAMDATA",
        "ProgramData",
        "ProgramFiles",
        "PROGRAMFILES",
        "ProgramFiles(x86)",
        "PROGRAMFILES(X86)",
    }
    allowed_casefold = {key.casefold() for key in allowed_exact}
    env: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key.casefold() not in allowed_casefold and not key.startswith("LC_"):
            continue
        try:
            redacted = redact_projection(str(value))
            if redacted.records:
                continue
        except Exception:
            continue
        env[key] = str(value)
    return env


# --- repository-writer admission for services (v6.88.1 r6) ------------------------------------
#
# A service is a PROCESS that outlives the tool call which started it (`keep_alive` outlives the
# whole task), and `start_service` runs an arbitrary command in the active workspace. So a service
# rooted in the managed checkout is a repository writer that the update fence could not see: it
# enumerates worker handles and in-process chat turns, and this is neither.
#
# Rather than a second registry with a second refusal path, such a service takes the SAME
# admission lease the in-process chat writers take. That makes the EXISTING fence prove it away:
# admission closed means no new one can start, `terminate_repo_rooted_services` stops the ones
# already running, and one we could not stop keeps its lease — so `drain_repo_writers` reports it
# and the fence refuses the update. Services rooted outside the repository (and executor-backed
# ones, which run against a mapped workspace in their own container) take no lease at all.
#
# The lease reaches only the process that took it. `start_service` also runs inside pooled
# multiprocessing WORKERS, which hold their own copies of this module AND of
# `supervisor.workers`, so a worker's lease and `_SERVICES` record are invisible to the
# supervisor's fence — and the service itself is spawned into its own session, so killing the
# worker does not kill it. The DURABLE side of the same fact is what covers that:
# `spawn_supervised(repo_writer=...)` marks the process in the custody ledger, which both
# processes read, and the fence sweeps it with
# `process_custody.terminate_repo_writer_processes`.


def _service_writes_managed_repo(ctx: ToolContext, workdir: pathlib.Path) -> bool:
    """Whether this service would run INSIDE the managed checkout.

    Fails CLOSED: a root we cannot resolve counts as the repository. That costs nothing on the
    normal path — admission is open, so the lease is simply granted — and it is the only answer
    that keeps an unplaceable service off a tree an update is about to hard-reset.
    """
    try:
        repo = pathlib.Path(ctx.repo_dir).resolve(strict=False)
        resolved = pathlib.Path(workdir).resolve(strict=False)
    except Exception:
        return True
    return resolved == repo or repo in resolved.parents


def _acquire_repo_writer_lease(kind: str) -> tuple[str, str]:
    """Take the admission lease for a repo-rooted service; returns ``(lease, refusal)``.

    A process with no writer-admission registry runs no update fence either (the fence and the
    registry are the same module state), so there is nothing a refusal could protect there and the
    service starts unleased — which is also what already happens for worker-started services, whose
    whole process tree is torn down with their worker.
    """
    try:
        from supervisor.workers import acquire_repo_writer_lease, repo_writer_admission_closed
    except Exception:
        return "", ""
    lease = acquire_repo_writer_lease(kind)
    if lease:
        return str(lease), ""
    return "", (
        "⚠️ SERVICE_REPO_LOCKED: the managed repository is closed to writers "
        f"({repo_writer_admission_closed() or 'update in progress'}); retry once it finishes."
    )


def _release_repo_writer_lease(lease: str) -> None:
    if not lease:
        return
    try:
        from supervisor.workers import release_repo_writer_lease

        release_repo_writer_lease(lease)
    except Exception:
        pass


def _retire_repo_writer_lease_if_dead(record: ServiceRecord) -> bool:
    """Release a repo-rooted service's lease, but ONLY once its process is PROVEN gone.

    Returns whether the record holds no lease any more. An unreadable exit status is not a proof
    of death, so it keeps the lease — and therefore fails the fence — rather than being written
    over by a hard reset. Neither is the LEADER's exit: the service was spawned into its own
    process group with an arbitrary command, so a child that inherited the checkout keeps writing
    to it after the launcher we hold a handle on is gone. Both must be proven gone.
    """
    if not record.repo_writer_lease:
        return True
    try:
        if record.proc.poll() is None:
            return False
    except Exception:
        return False
    if record.pgid > 0 and process_group_is_alive(record.pgid):
        return False
    _release_repo_writer_lease(record.repo_writer_lease)
    record.repo_writer_lease = ""
    return True


def _register_failed_repo_writer_start(
    *,
    service_name: str,
    key: str,
    task_id: str,
    cmd: List[Any],
    workdir: Any,
    log_path: pathlib.Path,
    proc: subprocess.Popen,
    cwd_root: str,
    repo_writer_lease: str,
) -> None:
    """Keep a half-started repo-rooted service reachable after `_start_service` gave up on it.

    A process DID start, so its lease may only go once that process is PROVEN gone — and the
    caller's teardown is best-effort and allowed to fail. A leaseholder we walked away from would
    be a repository writer with nothing pointing at it: invisible to the fence and unreleasable
    forever, which refuses every later managed update. `_SERVICES` is both the only place the fence
    can find it and the only place its lease can be retired from, so that is where it goes — unless
    it is already proven dead, in which case the lease is simply handed back here.
    """
    # Carry the group id so lease retirement applies the SAME group-outlives-leader proof to a
    # failed start as to a healthy record (the child is spawned as a session leader, so the pid is
    # the pgid when the probe cannot answer).
    try:
        failed_pgid = int(process_group_id(int(getattr(proc, "pid", 0) or 0)) or 0)
    except Exception:
        failed_pgid = 0
    failed = ServiceRecord(
        name=service_name, service_id=key, task_id=task_id,
        cmd=[str(part) for part in cmd], cwd=str(workdir), log_path=log_path,
        proc=proc, cwd_root=cwd_root, repo_writer_lease=repo_writer_lease,
        pgid=failed_pgid or int(getattr(proc, "pid", 0) or 0),
    )
    if not _retire_repo_writer_lease_if_dead(failed):
        with _LOCK:
            _SERVICES[key] = failed


def terminate_repo_rooted_services(*, wait: bool = True) -> List[str]:
    """Stop every service still holding a managed-repository writer lease.

    Returns the leases that could NOT be retired because their process was not proven gone. The
    caller does not have to act on that list: those leases stay in the admission registry, so the
    fence's own `drain_repo_writers` reports them and refuses the update on exactly the evidence
    it uses for every other in-process writer.

    Services that already exited are reconciled here too, which is what keeps a lease from leaking
    for the life of the process: the registry is only ever read by the fence, and the fence runs
    this first.
    """
    with _LOCK:
        records = [record for record in _SERVICES.values() if record.repo_writer_lease]
    undrained: List[str] = []
    for record in records:
        if _retire_repo_writer_lease_if_dead(record):
            continue
        try:
            _stop_record(record, wait=wait)
        except Exception:
            pass
        _retire_repo_writer_lease_if_dead(record)
        if record.repo_writer_lease:
            undrained.append(record.repo_writer_lease)
    return undrained


def _stop_record(record: ServiceRecord, *, wait: bool = True) -> None:
    if record.pgid:
        kill_process_group_id(record.pgid)
    elif record.proc.poll() is None:
        kill_process_tree(record.proc)
    if not wait:
        return
    try:
        record.proc.wait(timeout=5)
    except Exception:
        pass
    # Waiting on the LEADER does not wait for the group we just signalled, and the lease is now only
    # retired once the group is gone too. Without a grace window here, a group that was killed but
    # not yet reaped would hold its lease for the whole fence and refuse a legitimate update on
    # scheduling latency alone. Bounded, so a group that genuinely outlived the kill still refuses.
    # Gated on the LEASE, not just on having a pgid: every locally spawned service has a pgid, and a
    # record with no lease has no decision riding on this answer — it would only make ordinary
    # task/shutdown teardown pay the poll for nothing.
    if record.repo_writer_lease and record.pgid > 0:
        deadline = time.monotonic() + _REPO_WRITER_GROUP_REAP_GRACE_SEC
        while process_group_is_alive(record.pgid):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
    _retire_repo_writer_lease_if_dead(record)


def _finalize_service_log_for_drive(drive_root: pathlib.Path, record: ServiceRecord) -> Dict[str, Any]:
    result: Dict[str, Any] = {"deleted_live_log": False, "full_log_ref": {}, "tail": "", "errors": []}
    log_path = record.log_path
    try:
        size = log_path.stat().st_size if log_path.exists() else 0
        result["tail"] = str(redact_projection(_tail(log_path, _MAX_SERVICE_LOG_TAIL_CHARS)).value)
        if size <= _MAX_SERVICE_LOG_BLOB_BYTES:
            text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            result["full_log_ref"] = write_blob(pathlib.Path(drive_root), text, kind="txt")
        else:
            result["full_log_omitted"] = f"log exceeds {_MAX_SERVICE_LOG_BLOB_BYTES} byte blob cap"
    except Exception as exc:
        result["errors"].append(f"capture: {type(exc).__name__}: {exc}")
    should_delete = bool((result.get("full_log_ref") or {}).get("sha256")) or not log_path.exists()
    if should_delete:
        try:
            log_path.unlink(missing_ok=True)
            result["deleted_live_log"] = True
            parent = log_path.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except Exception as exc:
            result["errors"].append(f"delete: {type(exc).__name__}: {exc}")
    elif log_path.exists():
        result["retained_live_log_path"] = str(log_path)
    return result


def _archive_stale_service_log(
    drive_root: pathlib.Path,
    log_path: pathlib.Path,
    *,
    event_type: str = "service_log_pruned",
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"archived": False, "deleted_live_log": False, "full_log_ref": {}, "errors": []}
    try:
        size = log_path.lstat().st_size
        if size > _MAX_SERVICE_LOG_BLOB_BYTES:
            result["retained_live_log_path"] = str(log_path)
            result["full_log_omitted"] = f"log exceeds {_MAX_SERVICE_LOG_BLOB_BYTES} byte blob cap"
            return result
        text = log_path.read_text(encoding="utf-8", errors="replace")
        result["full_log_ref"] = write_blob(pathlib.Path(drive_root), text, kind="txt")
        result["tail_chars"] = len(str(redact_projection(_tail(log_path, _MAX_SERVICE_LOG_TAIL_CHARS)).value))
        append_jsonl(pathlib.Path(drive_root) / "logs" / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": event_type,
            "task_id": log_path.parent.name,
            "name": log_path.stem,
            "full_log_ref": result["full_log_ref"],
            "tail_chars": result["tail_chars"],
        })
        result["archived"] = True
        log_path.unlink(missing_ok=True)
        result["deleted_live_log"] = True
    except Exception as exc:
        result["errors"].append(f"{log_path}: {type(exc).__name__}: {exc}")
    return result


def archive_task_service_logs(
    drive_root: pathlib.Path,
    task_id: str,
    task: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Archive and remove leftover live service logs for a terminal task."""

    task_text = str(task_id or "").strip()
    if isinstance(task, dict):
        seen: set[str] = set()
        roots: List[pathlib.Path] = []
        for candidate in (
            drive_root,
            task.get("drive_root"),
            task.get("child_drive_root"),
            task.get("headless_child_drive_root"),
        ):
            if not candidate:
                continue
            root = pathlib.Path(candidate).resolve(strict=False)
            key = str(root)
            if key in seen:
                continue
            seen.add(key)
            roots.append(root)
        reports = [archive_task_service_logs(root, task_text) for root in roots]
        return {
            "task_id": task_text,
            "roots": [str(root) for root in roots],
            "archived_files": sum(int(report.get("archived_files") or 0) for report in reports),
            "deleted_files": sum(int(report.get("deleted_files") or 0) for report in reports),
            "deleted_dirs": sum(int(report.get("deleted_dirs") or 0) for report in reports),
            "retained_files": sum(int(report.get("retained_files") or 0) for report in reports),
            "errors": [err for report in reports for err in (report.get("errors") or [])],
        }
    report = {
        "task_id": task_text,
        "archived_files": 0,
        "deleted_files": 0,
        "deleted_dirs": 0,
        "retained_files": 0,
        "errors": [],
    }
    if not task_text or pathlib.Path(task_text).name != task_text:
        report["errors"].append("invalid task_id")
        return report
    task_dir = pathlib.Path(drive_root) / "services" / task_text
    try:
        task_stat = task_dir.lstat()
    except OSError:
        return report
    if not stat.S_ISDIR(task_stat.st_mode):
        return report
    try:
        for child in task_dir.glob("*.log"):
            try:
                child_stat = child.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(child_stat.st_mode):
                continue
            archive_result = _archive_stale_service_log(
                pathlib.Path(drive_root),
                child,
                event_type="service_log_archived",
            )
            if archive_result.get("archived"):
                report["archived_files"] += 1
            if archive_result.get("deleted_live_log"):
                report["deleted_files"] += 1
            if archive_result.get("retained_live_log_path"):
                report["retained_files"] += 1
            report["errors"].extend(archive_result.get("errors") or [])
        if task_dir.exists() and not any(task_dir.iterdir()):
            task_dir.rmdir()
            report["deleted_dirs"] += 1
    except Exception as exc:
        report["errors"].append(f"{task_dir}: {type(exc).__name__}: {exc}")
    return report


def _refresh_ready(record: ServiceRecord) -> bool:
    if record.proc.poll() is not None:
        record.ready = False
        return False
    readiness = record.readiness or {}
    contains = str(readiness.get("stdout_contains") or readiness.get("log_contains") or "").strip()
    if not contains:
        record.ready = True
        return True
    record.ready = contains in _tail(record.log_path, 20_000)
    return record.ready


def _start_service(
    ctx: ToolContext,
    cmd: List[str],
    name: str = "service",
    cwd: str = "",
    readiness: Dict[str, Any] | None = None,
    outputs: List[str] | None = None,
    keep_alive: bool = False,
) -> str:
    if not isinstance(cmd, list) or not cmd or not all(str(x).strip() for x in cmd):
        return "⚠️ TOOL_ARG_ERROR (start_service): cmd must be a non-empty array of strings."
    service_name, name_error = _sanitize_service_name(name)
    if name_error:
        return name_error
    readiness_timeout, readiness_error = _readiness_timeout(readiness)
    if readiness_error:
        return readiness_error
    key = _service_key(ctx, service_name)
    with _LOCK:
        existing = _SERVICES.get(key)
        if existing and existing.proc.poll() is None:
            return f"⚠️ SERVICE_ALREADY_RUNNING: {service_name} pid={existing.proc.pid}"
    if existing is not None and not _retire_repo_writer_lease_if_dead(existing):
        # A success below OVERWRITES `_SERVICES[key]`, after which nothing can reach this record
        # again — so its lease is retired here, while it still can be. A writer lease no code can
        # release refuses every later managed update for the life of the process, and `_SERVICES` is
        # also the fence's only handle on the group.
        #
        # The exited LEADER is no longer proof that the retirement succeeds: it now also requires
        # the recorded GROUP to be gone, and a leader-exited-but-group-alive record is exactly the
        # case that check exists for. So the answer is acted on rather than discarded — stop the
        # group, and if the lease still cannot be handed back, refuse the start instead of dropping
        # the only record that can ever release it.
        try:
            _stop_record(existing)
        except Exception:
            pass
        if existing.repo_writer_lease:
            return (
                f"⚠️ SERVICE_REPO_LOCKED: {service_name} was started before and its process group "
                f"(pgid={existing.pgid}) is still alive, so its managed-repository writer lease "
                "cannot be released; retry once it exits."
            )
    try:
        workdir, cwd_root, _allowed_roots = resolve_shell_cwd(ctx, cwd, operation="service")
        workdir = pathlib.Path(workdir).resolve(strict=False)
    except Exception as exc:
        return f"⚠️ SERVICE_CWD_ERROR: {type(exc).__name__}: {exc}"
    try:
        from ouroboros.protected_artifacts import shell_block_reason

        protected_block = shell_block_reason(ctx, cmd, cwd=str(workdir), default_cwd=workdir)
        if protected_block:
            return protected_block
    except Exception:
        pass
    declared_outputs = [str(item) for item in (outputs or []) if str(item or "").strip()]
    try:
        from ouroboros.tools.shell import _snapshot_declared_outputs

        before_outputs = _snapshot_declared_outputs(ctx, declared_outputs, workdir, cwd_root=cwd_root)
    except Exception:
        before_outputs = {}
    # Resolve the effective keep BEFORE choosing a backend: a task-level
    # service_teardown='keep' must reach the executor path too, else an
    # executor-backed service is recorded task-scoped and the custody reaper
    # kills it once the task ends — breaking the keep contract for a service a
    # verifier still needs (triad review r1, gpt-5.5 critical).
    keep_alive = bool(keep_alive) or task_service_teardown(ctx) == "keep"
    if _executor_can_run_cwd(ctx, workdir):
        try:
            payload = executor_start_service(
                ctx,
                name=service_name,
                cmd=[str(part) for part in cmd],
                host_cwd=workdir,
                cwd_root=cwd_root,
                readiness=dict(readiness or {}),
                outputs=declared_outputs,
                before_outputs=before_outputs,
                keep_alive=keep_alive,
            )
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as exc:
            return f"⚠️ SERVICE_START_ERROR: executor backend failed: {type(exc).__name__}: {exc}"
    task_id = str(getattr(ctx, "task_id", "") or "manual")
    log_dir = pathlib.Path(ctx.drive_root) / "services" / task_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{service_name}.log"
    log_fh = log_path.open("ab")
    proc = None
    repo_writer_lease = ""
    try:
        bootstrap_process_path()
        # Take the writer lease BEFORE the process exists. The lease is what an update fence
        # enumerates, so a service spawned first and registered afterwards would be invisible to a
        # fence that closed in between — left running against the checkout it is about to reset.
        writes_managed_repo = _service_writes_managed_repo(ctx, workdir)
        if writes_managed_repo:
            repo_writer_lease, lease_refusal = _acquire_repo_writer_lease(f"service:{service_name}")
            if lease_refusal:
                log_fh.close()
                return lease_refusal
        # Supervised spawn: the durable custody record means a SIGKILLed worker
        # can no longer orphan this service invisibly — the reaper finds it in
        # the ledger on the next server generation. keep_alive services use
        # session scope: they outlive their task on purpose, but a later server
        # generation still reaps them (no permanent orphans).
        from ouroboros.process_custody import spawn_supervised

        proc = spawn_supervised(
            [str(part) for part in cmd],
            drive_root=pathlib.Path(ctx.drive_root),
            purpose=f"service:{service_name}",
            scope="session" if keep_alive else "task",
            owner_task_id=task_id,
            # The lease above is process-local, so it is invisible to an update fence running in
            # the SUPERVISOR when this call is executing inside a pooled worker. The ledger is the
            # registry both processes share, so the fence finds this service there instead.
            repo_writer=writes_managed_repo,
            cwd=str(workdir),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=_service_env(),
        )
        pgid = process_group_id(proc.pid)
        log_fh.close()
    except Exception as exc:
        log_fh.close()
        from ouroboros.process_custody import RepoWriterCustodyError

        if proc is None and isinstance(exc, RepoWriterCustodyError):
            # The raise came from INSIDE `spawn_supervised`, before this frame was handed the
            # object, so the local `proc` is None even though a repository writer is running. The
            # error carries the handle for exactly this reason: recovered BEFORE the teardown below,
            # it turns "nothing was ever spawned" back into the two things that can still be done
            # with a survivor — one more bounded termination attempt here, and a record that keeps
            # it reachable afterwards.
            proc = getattr(exc, "proc", None)
        if proc is not None:
            try:
                kill_process_tree(proc)
                proc.wait(timeout=5)
            except Exception:
                pass
        if repo_writer_lease and isinstance(exc, RepoWriterCustodyError) and proc is None:
            # No handle came back with the error, so the teardown above had nothing to act on. This
            # error means custody could neither prove the writer dead nor make it visible to the
            # fence, and handing the lease back would remove the last thing standing between it and
            # a hard reset. It is retained instead: this process refuses managed updates for as long
            # as it lives, which is the fail-closed answer to a writer nothing can see. Residual,
            # named rather than papered over: a pooled worker holds no lease (it runs no fence
            # either), so there the survivor is left to the next generation's ledger reaper — the
            # bounded kill rounds inside the repudiation are all that path gets.
            pass
        elif repo_writer_lease and proc is None:
            _release_repo_writer_lease(repo_writer_lease)  # nothing was ever spawned to hold it
        elif repo_writer_lease:
            _register_failed_repo_writer_start(
                service_name=service_name, key=key, task_id=task_id, cmd=cmd, workdir=workdir,
                log_path=log_path, proc=proc, cwd_root=cwd_root,
                repo_writer_lease=repo_writer_lease,
            )
        return f"⚠️ SERVICE_START_ERROR: {type(exc).__name__}: {exc}"
    record = ServiceRecord(
        name=service_name,
        service_id=key,
        task_id=task_id,
        cmd=[str(part) for part in cmd],
        cwd=str(workdir),
        log_path=log_path,
        proc=proc,
        pgid=pgid,
        readiness=dict(readiness or {}),
        outputs=declared_outputs,
        cwd_root=cwd_root,
        before_outputs=before_outputs,
        keep_alive=keep_alive,
        repo_writer_lease=repo_writer_lease,
    )
    with _LOCK:
        _SERVICES[key] = record
    try:
        if cwd_root == "active_workspace" and not bool(getattr(ctx, "is_workspace_mode", lambda: False)()):
            from ouroboros.tools.commit_gate import _invalidate_advisory

            _invalidate_advisory(
                ctx,
                changed_paths=[f"<service:{service_name}>"],
                mutation_root=workdir,
                source_tool="start_service",
            )
    except Exception:
        pass
    deadline = time.time() + readiness_timeout
    while time.time() < deadline:
        if _refresh_ready(record):
            break
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    return json.dumps(_status_payload(record), ensure_ascii=False, indent=2)


def _status_payload(record: ServiceRecord) -> Dict[str, Any]:
    _refresh_ready(record)
    rc = record.proc.poll()
    state = "running" if rc is None else "exited"
    return {
        "service_id": record.service_id,
        "name": record.name,
        "task_id": record.task_id,
        "pid": record.proc.pid,
        "pgid": record.pgid,
        "state": state,
        "ready": bool(record.ready),
        "returncode": rc,
        "uptime_sec": round(max(0.0, time.time() - record.started_at), 3),
        "cwd": record.cwd,
        "cwd_root": record.cwd_root,
        "cmd": record.cmd,
        "outputs": list(record.outputs),
        "keep_alive": bool(record.keep_alive),
        "log_path": str(record.log_path),
        "ts": utc_now_iso(),
    }


def _service_status(ctx: ToolContext, name: str = "service") -> str:
    service_name, name_error = _sanitize_service_name(name)
    if name_error:
        return name_error
    key = _service_key(ctx, service_name)
    with _LOCK:
        record = _SERVICES.get(key)
    if record:
        return json.dumps(_status_payload(record), ensure_ascii=False, indent=2)
    if executor_ref_from_ctx(ctx) is not None:
        payload = executor_service_status(ctx, service_name)
        if payload is None:
            return f"⚠️ SERVICE_NOT_FOUND: {name}"
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return f"⚠️ SERVICE_NOT_FOUND: {name}"


def _service_logs(ctx: ToolContext, name: str = "service", tail: int = 8000) -> str:
    service_name, name_error = _sanitize_service_name(name)
    if name_error:
        return name_error
    key = _service_key(ctx, service_name)
    with _LOCK:
        record = _SERVICES.get(key)
    if record:
        try:
            tail_chars = int(tail or 8000)
        except (TypeError, ValueError):
            return "⚠️ TOOL_ARG_ERROR (service_logs): tail must be an integer."
        tail_chars = min(max(1, tail_chars), _MAX_SERVICE_LOG_TAIL_CHARS)
        text = str(redact_projection(_tail(record.log_path, tail_chars)).value)
        ref = {}
        omitted_reason = ""
        try:
            size = record.log_path.stat().st_size if record.log_path.exists() else 0
            if size <= _MAX_SERVICE_LOG_BLOB_BYTES:
                full = record.log_path.read_text(encoding="utf-8", errors="replace") if record.log_path.exists() else ""
                ref = write_blob(pathlib.Path(ctx.drive_root), full, kind="txt")
            else:
                omitted_reason = f"log exceeds {_MAX_SERVICE_LOG_BLOB_BYTES} byte blob cap"
        except Exception:
            ref = {}
        return json.dumps({
            "service_id": record.service_id,
            "name": record.name,
            "tail": text,
            "full_log_ref": ref,
            "full_log_omitted": omitted_reason,
        }, ensure_ascii=False, indent=2)
    if executor_ref_from_ctx(ctx) is not None:
        try:
            tail_chars = int(tail or 8000)
        except (TypeError, ValueError):
            return "⚠️ TOOL_ARG_ERROR (service_logs): tail must be an integer."
        payload = executor_service_logs(ctx, service_name, tail_chars)
        if payload is None:
            return f"⚠️ SERVICE_NOT_FOUND: {name}"
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return f"⚠️ SERVICE_NOT_FOUND: {name}"


def _stop_service(ctx: ToolContext, name: str = "service") -> str:
    service_name, name_error = _sanitize_service_name(name)
    if name_error:
        return name_error
    key = _service_key(ctx, service_name)
    with _LOCK:
        record = _SERVICES.get(key)
    if record:
        # Stop FIRST, and only then forget it. Popping ahead of the stop meant a service that
        # survived `kill_process_tree` plus the bounded wait kept its repository-writer lease with
        # its record already gone — unfindable by the fence and unreleasable by anything, which
        # refuses every later update. A process that will not die stays in `_SERVICES` instead, so
        # the fence keeps seeing it and `terminate_repo_rooted_services` keeps retrying it.
        _stop_record(record)  # retires the lease if (and only if) the process is proven gone
        if not record.repo_writer_lease:
            with _LOCK:
                if _SERVICES.get(key) is record:
                    del _SERVICES[key]
        payload = _status_payload(record)
        payload["log_finalization"] = _finalize_service_log_for_drive(pathlib.Path(ctx.drive_root), record)
        artifact_note = ""
        artifact_failed = False
        if record.outputs:
            try:
                from ouroboros.tools.shell import _register_process_outputs

                artifact_note, artifact_failed = _register_process_outputs(
                    ctx,
                    record.outputs,
                    pathlib.Path(record.cwd),
                    cwd_root=record.cwd_root,
                    before_outputs=record.before_outputs,
                )
            except Exception as exc:
                artifact_note = f"\n\n⚠️ ARTIFACT_OUTPUT_ERROR:\n- service output finalization failed: {type(exc).__name__}: {exc}"
                artifact_failed = True
        elif record.cwd_root == "user_files":
            payload["artifact_audit_gap"] = (
                "⚠️ ARTIFACT_AUDIT_GAP: service ran in user_files cwd without outputs=[...]. "
                "If it created a deliverable, rerun/register the file with outputs or "
                "write_file(root=artifact_store) before claiming it."
            )
        if artifact_note:
            payload["artifact_outputs"] = artifact_note.strip()
        payload["artifact_output_failed"] = bool(artifact_failed)
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        if artifact_failed:
            return "⚠️ ARTIFACT_OUTPUT_ERROR (stop_service): declared service outputs were not finalized.\n\n" + rendered
        return rendered
    if executor_ref_from_ctx(ctx) is not None:
        payload = executor_stop_service(ctx, service_name)
        if payload is None:
            return f"⚠️ SERVICE_NOT_FOUND: {name}"
        if payload.get("stop_failed"):
            return "⚠️ SERVICE_STOP_ERROR (stop_service): executor backend did not confirm service termination.\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)
        artifact_note = ""
        artifact_failed = False
        before_outputs = payload.pop("_before_outputs", {})
        if payload.get("outputs"):
            try:
                from ouroboros.tools.shell import _register_process_outputs

                artifact_note, artifact_failed = _register_process_outputs(
                    ctx,
                    [str(item) for item in (payload.get("outputs") or [])],
                    pathlib.Path(str(payload.get("host_cwd") or ".")),
                    cwd_root=str(payload.get("cwd_root") or ""),
                    before_outputs=before_outputs if isinstance(before_outputs, dict) else None,
                )
            except Exception as exc:
                artifact_note = f"\n\n⚠️ ARTIFACT_OUTPUT_ERROR:\n- executor service output finalization failed: {type(exc).__name__}: {exc}"
                artifact_failed = True
        if artifact_note:
            payload["artifact_outputs"] = artifact_note.strip()
        payload["artifact_output_failed"] = bool(artifact_failed)
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        if artifact_failed:
            return "⚠️ ARTIFACT_OUTPUT_ERROR (stop_service): declared executor service outputs were not finalized.\n\n" + rendered
        return rendered
    return f"⚠️ SERVICE_NOT_FOUND: {name}"


def stop_task_services(ctx: ToolContext) -> List[Dict[str, Any]]:
    """Finalize this task's services; returns payloads tagged with ``lifecycle``.

    Services marked ``keep_alive`` (per-service flag or task-level
    ``service_teardown=keep``) are NOT stopped: they are reported with
    ``lifecycle="kept"`` so the caller can surface pid/port metadata to
    whoever inherits responsibility for them (benchmark verifier, the owner).
    """
    task_id = str(getattr(ctx, "task_id", "") or "manual")
    keep_all = task_service_teardown(ctx) == "keep"
    results: List[Dict[str, Any]] = []
    with _LOCK:
        keys = [
            key for key, record in _SERVICES.items()
            if record.task_id == task_id
        ]
    for key in keys:
        name = key.split(":", 1)[1]
        with _LOCK:
            record = _SERVICES.get(key)
        if record is not None and (keep_all or record.keep_alive):
            payload = _status_payload(record)
            payload["lifecycle"] = "kept"
            results.append(payload)
            continue
        try:
            raw = _stop_service(ctx, name=name)
            if raw.startswith("⚠️ ARTIFACT_OUTPUT_ERROR") and "\n\n{" in raw:
                raw = raw.split("\n\n", 1)[1]
            payload = json.loads(raw)
            payload["lifecycle"] = "stopped"
            results.append(payload)
        except Exception:
            pass
    try:
        if keep_all:
            # Executor-backed services have no detach path of their own; under a
            # task-level keep they are simply left running (their container is
            # the harness's cleanup responsibility).
            from ouroboros.workspace_executor import _services_snapshot as _executor_snapshot

            for record in _executor_snapshot():
                if str(getattr(record, "task_id", "")) == task_id:
                    results.append({
                        "service_id": getattr(record, "service_id", ""),
                        "name": getattr(record, "name", ""),
                        "task_id": task_id,
                        "lifecycle": "kept",
                        "backend": "executor",
                    })
        else:
            for payload in executor_stop_task_services(ctx):
                payload = dict(payload)
                payload.setdefault("lifecycle", "stopped")
                results.append(payload)
    except Exception:
        pass
    return results


def kill_all_services(
    drive_root: pathlib.Path | None = None,
    *,
    wait: bool = True,
    include_keep_alive: bool = True,
) -> List[Dict[str, Any]]:
    """Stop every tracked service process group for panic/shutdown paths.

    ``include_keep_alive=False`` (graceful shutdown/restart) leaves keep_alive
    services running: they are session-scoped in the process custody ledger,
    so the next server generation's reaper still collects them. Panic and
    emergency cleanup keep the default and kill everything.
    """

    with _LOCK:
        if include_keep_alive:
            records = list(_SERVICES.values())
        else:
            records = [r for r in _SERVICES.values() if not r.keep_alive]
    stopped: List[Dict[str, Any]] = []
    for record in records:
        _stop_record(record, wait=wait)
        # Removed AFTER the stop, and only if it is no longer holding a repository-writer lease —
        # same rule as `_stop_service`. Clearing the registry first left a service that outlived
        # the kill holding a lease no code could reach, which refuses every later managed update.
        if not record.repo_writer_lease:
            with _LOCK:
                if _SERVICES.get(record.service_id) is record:
                    del _SERVICES[record.service_id]
        payload = _status_payload(record)
        if wait and drive_root is not None:
            payload["log_finalization"] = _finalize_service_log_for_drive(pathlib.Path(drive_root), record)
        stopped.append(payload)
    try:
        stopped.extend(executor_kill_all_services(drive_root, wait=wait))
    except Exception:
        pass
    if wait and drive_root is not None and stopped:
        def _compact(payload: Dict[str, Any]) -> Dict[str, Any]:
            item = dict(payload)
            finalization = dict(item.get("log_finalization") or {})
            tail = finalization.pop("tail", "")
            if tail:
                finalization["tail_chars"] = len(str(tail))
            item["log_finalization"] = finalization
            return item

        try:
            append_jsonl(pathlib.Path(drive_root) / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "services_shutdown_cleanup",
                "services": [_compact(payload) for payload in stopped],
            })
        except Exception:
            pass
    return stopped


def prune_service_logs(
    drive_root: pathlib.Path,
    retention_days: int | None = None,
    *,
    now: float | None = None,
) -> Dict[str, Any]:
    from ouroboros.retention import age_cutoff, get_gc_retention_days

    # Explicit overrides are honored as-is (age_cutoff floors at 0 => explicit 0
    # prunes everything before `now`), uniform with the worktree/task prunes; only
    # the default (None) path reads the clamped owner knob.
    if retention_days is None:
        retention_days = get_gc_retention_days()
    cutoff = age_cutoff(retention_days, now)
    services_root = pathlib.Path(drive_root) / "services"
    report = {
        "enabled": True,
        "retention_days": retention_days,
        "deleted_dirs": 0,
        "deleted_files": 0,
        "archived_files": 0,
        "retained_files": 0,
        "errors": [],
    }
    if not services_root.exists():
        return report
    for task_dir in list(services_root.iterdir()):
        try:
            task_stat = task_dir.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(task_stat.st_mode):
            continue
        try:
            for child in task_dir.glob("*.log"):
                try:
                    child_stat = child.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(child_stat.st_mode) and child_stat.st_mtime < cutoff:
                    archive_result = _archive_stale_service_log(pathlib.Path(drive_root), child)
                    if archive_result.get("archived"):
                        report["archived_files"] += 1
                    if archive_result.get("deleted_live_log"):
                        report["deleted_files"] += 1
                    if archive_result.get("retained_live_log_path"):
                        report["retained_files"] += 1
                    report["errors"].extend(archive_result.get("errors") or [])
            if not any(task_dir.iterdir()):
                task_dir.rmdir()
                report["deleted_dirs"] += 1
        except Exception as exc:
            report["errors"].append(f"{task_dir}: {type(exc).__name__}: {exc}")
    return report


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("start_service", {
            "name": "start_service",
            "description": "Start a task-scoped long-running service and return pid/readiness/state.",
            "parameters": {"type": "object", "properties": {
                "cmd": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string", "default": ""},
                "name": {"type": "string", "default": "service"},
                "readiness": {"type": "object", "default": {}, "description": "Optional {log_contains|stdout_contains, timeout_sec} readiness probe."},
                "outputs": {"type": "array", "items": {"type": "string"}, "default": [], "description": "Files generated by the service to copy into the task artifact store when the service stops."},
                "keep_alive": {"type": "boolean", "default": False, "description": "Leave this service running after the task ends (e.g. a dev server the user or an external verifier still needs). It stays custody-ledgered and dies with the server session or panic."},
            }, "required": ["cmd"]},
        }, _start_service, is_code_tool=True, timeout_sec=30, mutates_worktree=True),
        ToolEntry("service_status", {
            "name": "service_status",
            "description": "Return pid/state/readiness/uptime for a task-scoped service.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "default": "service"},
            }, "required": []},
        }, _service_status),
        ToolEntry("service_logs", {
            "name": "service_logs",
            "description": "Return bounded service log tail plus a private full-log blob ref.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "default": "service"},
                "tail": {"type": "integer", "default": 8000},
            }, "required": []},
        }, _service_logs),
        ToolEntry("stop_service", {
            "name": "stop_service",
            "description": "Stop a task-scoped service process group.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "default": "service"},
            }, "required": []},
        }, _stop_service, is_code_tool=True, timeout_sec=30, mutates_worktree=True),
    ]
