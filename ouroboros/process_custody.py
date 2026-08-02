"""Process custody: supervised spawning plus a durable orphan ledger.

Every long-lived OS process Ouroboros spawns should go through
``spawn_supervised`` so its identity lands in ``data/state/process_ledger.jsonl``
BEFORE it can be orphaned. The reaper (server startup + periodic tick) then
kills ledger entries whose owning generation is gone, matching processes by a
STRICT (pid, start_time, cmd_sha256) fingerprint — never by command-line
class, so a dev instance can never reap a packaged instance's processes (or
vice versa).

Scopes:
  - ``task``:    dies with its owning task; reapable as soon as the task is no
                 longer running (and always across server generations).
  - ``session``: dies with the server generation (session_id mismatch → reap).
  - ``daemon``:  genuine launcher-managed processes (e.g. ``server_restart_fallback``)
                 outlive generations — never killed. Skill COMPANIONS also record
                 daemon scope but are the exception: ``reap_orphaned_processes``
                 reaps them on owner-uninstall or a foreign generation.

This module deliberately lives OUTSIDE platform_layer (primitives-only) and
adds no policy to the panic layers it complements (``_active_subprocesses``,
port sweeps, Windows Job Objects all stay).
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional

from ouroboros.platform_layer import (
    kill_process_group_id,
    pid_is_alive,
    process_command,
    process_group_id,
    process_group_is_alive,
    process_start_time,
    subprocess_new_group_kwargs,
)
from ouroboros.utils import append_jsonl, utc_now_iso

log = logging.getLogger(__name__)

LEDGER_FILENAME = "process_ledger.jsonl"
# One ledger generation per server process; recorded into every entry so the
# reaper can tell "mine" from "previous generation" without guessing.
_SESSION_ID = uuid.uuid4().hex
_VALID_SCOPES = ("task", "session", "daemon")


def current_custody_session_id() -> str:
    return _SESSION_ID


def adopt_session_id(value: str) -> None:
    """Adopt a parent process's custody session id.

    Workers started with the 'spawn' multiprocessing method (the default on
    macOS/Windows, and forced on Linux by the Terminal-Bench harness) re-import
    this module and would otherwise generate a fresh ``_SESSION_ID``. Every
    process such a worker records (its task/session-scoped services, executor
    children, local model server) would then look like a *foreign generation*
    to the server's periodic reaper — which kills task- and session-scoped
    foreign entries — so a still-running task's services get SIGKILLed at the
    next reap tick. The worker entrypoint calls this with the server's id.

    The id is passed as a spawn ARGUMENT, never via ambient env: an env var
    would survive ``server_control.restart_current_process`` (which re-execs
    with ``os.environ.copy()``), making a freshly restarted server adopt the
    dead generation's id and treat leftover processes as same-session
    survivors — the inverse leak. A spawn arg cannot survive an exec.
    """
    global _SESSION_ID
    v = str(value or "").strip()
    if v:
        _SESSION_ID = v


def ledger_path(drive_root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(drive_root) / "state" / LEDGER_FILENAME


def _cmd_sha256(cmd: Any) -> str:
    try:
        if isinstance(cmd, (list, tuple)):
            text = "\0".join(str(part) for part in cmd)
        else:
            text = str(cmd or "")
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    except Exception:
        return ""


def _live_cmd_sha256(pid: int) -> str:
    command = process_command(pid)
    if not command:
        return ""
    return hashlib.sha256(command.encode("utf-8", errors="replace")).hexdigest()


def record_process(
    drive_root: pathlib.Path,
    *,
    pid: int,
    cmd: Any,
    purpose: str,
    scope: str,
    owner_task_id: str = "",
    reap_process_group: bool = True,
    repo_writer: bool = False,
) -> Dict[str, Any]:
    """Append a custody record for an already-spawned process.

    ``repo_writer`` marks a process that runs INSIDE the managed checkout and may therefore write
    to it. The marker is written only when true, so every other record keeps its exact shape.
    """
    if scope not in _VALID_SCOPES:
        raise ValueError(f"process custody scope must be one of {_VALID_SCOPES}, got {scope!r}")
    try:
        pgid = process_group_id(pid)
    except Exception:
        pgid = 0
    entry = {
        "ts": utc_now_iso(),
        "pid": int(pid),
        "pgid": int(pgid or 0) if reap_process_group else 0,
        "fingerprint": {
            "start_time": process_start_time(pid),
            # The LIVE command line (what the OS reports) is the reap-time
            # comparison anchor; the argv we passed may differ cosmetically.
            "cmd_sha256": _live_cmd_sha256(pid) or _cmd_sha256(cmd),
        },
        "purpose": str(purpose or "")[:200],
        "scope": scope,
        "owner_task": str(owner_task_id or ""),
        "session_id": _SESSION_ID,
    }
    if repo_writer:
        entry["repo_writer"] = True
    # `append_jsonl` REPORTS its failure instead of raising it (it exhausts its retries and answers
    # False), so an unchecked call turned "the ledger has no record of this process" into a silent
    # success. For a repository writer that is the whole custody guarantee: `spawn_supervised` only
    # handles exceptions, so it would hand back a running repo-rooted service the update fence has
    # no durable handle on. Raise instead, and let the repudiation path in `spawn_supervised` own it.
    #
    # Scoped to `repo_writer` deliberately. For every OTHER scope this ledger is advisory — the
    # reaper's registry, whose degradation is "an orphan is reaped a generation later" — and the
    # direct callers (`supervisor.workers`, `workspace_executor`, `local_model`,
    # `extension_companion`) were written against a `record_process` that could not fail. Raising at
    # them would convert a filesystem fault into new failure paths in code this fix is not about,
    # for no custody guarantee that depends on it.
    if append_jsonl(ledger_path(drive_root), entry) is not True:
        if repo_writer:
            raise OSError(f"custody ledger append failed for pid {pid} ({purpose})")
        log.warning("custody ledger append failed for pid %s (%s); entry not durable", pid, purpose)
    return entry


def spawn_supervised(
    cmd: Any,
    *,
    drive_root: pathlib.Path,
    purpose: str,
    scope: str,
    owner_task_id: str = "",
    new_process_group: bool = True,
    repo_writer: bool = False,
    **popen_kwargs: Any,
) -> subprocess.Popen:
    """Popen + durable custody record (the single supervised chokepoint).

    The record is written immediately after spawn, so even a SIGKILL of the
    spawning worker cannot orphan the child invisibly — the reaper finds it
    in the ledger on the next generation.

    ``repo_writer=True`` additionally marks the child as running inside the
    managed checkout, which is what lets the supervisor's update fence find it
    from a DIFFERENT process than the one that spawned it (see
    ``terminate_repo_writer_processes``) — and makes the record MANDATORY: an
    unrecordable writer is killed and the start failure propagates, because a
    running writer the fence cannot see is worse than a failed service.
    """
    if new_process_group:
        merged = dict(subprocess_new_group_kwargs())
        merged.update(popen_kwargs)
        popen_kwargs = merged
    proc = subprocess.Popen(cmd, **popen_kwargs)  # noqa: S603 — callers pass vetted argv lists
    try:
        record_process(
            drive_root,
            pid=proc.pid,
            cmd=cmd,
            purpose=purpose,
            scope=scope,
            owner_task_id=owner_task_id,
            repo_writer=repo_writer,
        )
    except Exception as record_exc:
        log.warning("process custody record failed for pid %s (%s)", proc.pid, purpose, exc_info=True)
        if repo_writer:
            # Mandatory for repository writers: an unrecorded one is invisible to the update fence
            # that runs in a DIFFERENT process, so we refuse to hand back a running one. This raises
            # `RepoWriterCustodyError` — deliberately NOT the RuntimeError below — when the writer
            # is neither proven dead nor latched, so a caller holding its lease can tell "nothing is
            # running" apart from "something is, and the fence cannot see it".
            _repudiate_unregistered_repo_writer(
                drive_root, proc, purpose=purpose, scope=scope, owner_task_id=owner_task_id
            )
            raise RuntimeError(
                f"custody registration failed for repository writer {purpose!r}: {record_exc}"
            ) from record_exc
    return proc


def _fingerprint_matches(entry: Dict[str, Any]) -> bool:
    """STRICT identity: the live process must still BE the recorded one.

    pid alive + same start_time (when we have one) + same command hash (when
    we have one). A recycled pid fails this and is left alone. We never match
    by command-line class.
    """
    pid = int(entry.get("pid") or 0)
    if pid <= 0 or not pid_is_alive(pid):
        return False
    fp = entry.get("fingerprint") if isinstance(entry.get("fingerprint"), dict) else {}
    recorded_start = str(fp.get("start_time") or "")
    if recorded_start:
        live_start = process_start_time(pid)
        if live_start and live_start != recorded_start:
            return False
    recorded_cmd = str(fp.get("cmd_sha256") or "")
    if recorded_cmd:
        live_cmd = _live_cmd_sha256(pid)
        if live_cmd and live_cmd != recorded_cmd:
            return False
        if not live_cmd and not recorded_start:
            # Windows: no command line and no start time — liveness is all we
            # have (same degradation as workspace_executor records).
            return True
    return True


def _read_ledger(drive_root: pathlib.Path) -> List[Dict[str, Any]]:
    path = ledger_path(drive_root)
    if not path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    try:
        import json

        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("pid"):
                entries.append(obj)
    except OSError:
        return []
    # Last record per pid wins (a pid may be re-registered by a newer spawn).
    by_pid: Dict[int, Dict[str, Any]] = {}
    for entry in entries:
        try:
            by_pid[int(entry.get("pid") or 0)] = entry
        except (TypeError, ValueError):
            continue
    by_pid.pop(0, None)
    return list(by_pid.values())


def _rewrite_ledger(drive_root: pathlib.Path, entries: List[Dict[str, Any]]) -> None:
    import json

    path = ledger_path(drive_root)
    try:
        from ouroboros.utils import jsonl_append_lock_path
        from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock

        lock_path = jsonl_append_lock_path(path)
        lock_fd = acquire_exclusive_file_lock(lock_path, timeout_sec=2.0, stale_sec=10.0)
        try:
            tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
            tmp.write_text(
                "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        finally:
            release_exclusive_file_lock(lock_path, lock_fd)
    except Exception:
        log.debug("process ledger rewrite failed", exc_info=True)


def start_parent_lifeline(*, poll_sec: float = 5.0, label: str = "") -> None:
    """Daemon watchdog: group-suicide when the parent process dies (POSIX).

    For OUR python entrypoints only (workers, extension runner, claude child):
    when the parent dies, the child is reparented to init (ppid==1) and would
    otherwise keep burning CPU/budget invisibly. Arbitrary-argv services and
    skills cannot get a watchdog injected — they are covered by the ledger +
    reaper instead.
    """
    if os.name == "nt":
        return  # Windows children are covered by Job Objects

    import threading
    import time as _time

    def _suicide() -> None:
        log.warning("parent process died — lifeline group-suicide (%s)", label or "child")
        try:
            from ouroboros.platform_layer import current_process_group_id

            pgid = current_process_group_id()
            # Kill the whole group ONLY when we lead it (worker/runner
            # entrypoints setsid/new-group first). A non-leader sharing the
            # spawner's group must not take unrelated siblings down with it.
            if pgid == os.getpid():
                kill_process_group_id(pgid)
        except Exception:
            pass
        os._exit(1)

    initial_ppid = os.getppid()
    if initial_ppid <= 1:
        # The parent died before we even got here (import-delay race after an
        # abrupt supervisor kill). These entrypoints are always spawned by a
        # live Ouroboros parent, so an orphan at startup is already a leak.
        _suicide()
        return

    def _watch() -> None:
        while True:
            _time.sleep(poll_sec)
            # Any reparenting means the original parent died (orphans go to
            # init/launchd or the nearest subreaper).
            if os.getppid() != initial_ppid:
                _suicide()

    threading.Thread(target=_watch, daemon=True, name=f"parent-lifeline-{label or 'child'}").start()


def live_kept_service_pids(drive_root: pathlib.Path) -> "set[int]":
    """PIDs of still-alive, deliberately-kept (session-scope) services.

    Used by the cancel/hard-timeout worker kill to spare ``service_teardown=keep``
    services that are direct children of the worker: tree-killing the worker
    would otherwise destroy services a verifier still needs, even though the
    keep contract says they outlive the task. Only live, fingerprint-matching
    session-scoped service entries are returned.
    """
    pids: set[int] = set()
    try:
        for entry in _read_ledger(pathlib.Path(drive_root)):
            if str(entry.get("scope") or "") != "session":
                continue
            # Both the in-process service path (purpose "service:<name>") and the
            # local-executor path (purpose "workspace_service:<name>") record
            # deliberately-kept services; spare both.
            if not str(entry.get("purpose") or "").startswith(("service:", "workspace_service:")):
                continue
            if not _fingerprint_matches(entry):
                continue
            pid = int(entry.get("pid") or 0)
            if pid > 0:
                pids.add(pid)
    except Exception:
        return pids
    return pids


# --- repository writers the update fence has to reach across PROCESSES (v6.88.1 r7) -----------
#
# `ouroboros.tools.services` keeps its records — and the writer-admission lease they hold — in
# MODULE GLOBALS. That is enough while the tool call runs in the supervisor, and nothing else,
# because the fence reads those same globals. It is not enough for a `start_service` executed
# inside a pooled multiprocessing WORKER: the worker imports its own copy of the module, so the
# lease lands in the worker's registry and the supervisor's fence never sees it.
#
# Killing the worker does not settle the question either. A service is spawned into its own
# session/process group with an ARBITRARY command, so it is not killed with the worker, and a
# command free to daemonize or reparent can outlive it and keep writing to the checkout the update
# is about to hard-reset.
#
# The custody ledger is the registry both processes already share, so the marker goes there rather
# than into a new one. Residual window, named rather than papered over: the record is appended
# AFTER `Popen` returns (a pid cannot be known before the fork) and `record_process` fingerprints
# via `ps`, so a service whose spawning worker is SIGKILLed inside that window is ledgered nowhere
# and this sweep cannot see it. It is the same window the reaper has always had, and the next
# server generation reaps it; closing it would need a spawn primitive that publishes identity
# atomically, which POSIX does not offer.

_REPO_WRITER_KILL_GRACE_SEC = 3.0
# How many kill/latch rounds `_repudiate_unregistered_repo_writer` runs before it gives up and
# hands the survivor back to its caller. Three, for the same reason `append_jsonl` retries three
# times: enough to ride out a transient, short enough that a spawn call is not held indefinitely.
_REPO_WRITER_REPUDIATION_ROUNDS = 3


def _repo_writer_group_outlives_leader(entry: Dict[str, Any]) -> bool:
    """Whether a repo-writer record's recorded PROCESS GROUP is still alive after its leader exited.

    The leader is only the launcher. A service is spawned into its OWN group with an arbitrary
    command, so a child that inherited the checkout (and its open file handles) keeps writing after
    the launcher exits — and the strict pid fingerprint, which is all the fence used to consult,
    reports that record as gone. The group is the stronger claim, so it decides here.

    Residual, named rather than papered over: nothing re-verifies the IDENTITY of that group. POSIX
    frees a pgid once the group empties, so an old record can name a group a new unrelated leader
    has since taken, and ``process_group_is_alive`` fails closed on PermissionError — which is what
    another user's recycled group produces. ``terminate_repo_writer_processes`` signals such a
    record anyway (see the reasoning there): an unsignalled one is a blocker nothing can clear, and
    reaching this branch already requires the recorded leader pid to be dead, which is a much
    narrower coincidence than plain pid reuse. Windows never reaches here: it has no process groups,
    so ``process_group_id`` records 0 there and the ``pgid > 0`` guard is false.
    """
    if not entry.get("repo_writer"):
        return False
    pid = int(entry.get("pid") or 0)
    if pid > 0 and pid_is_alive(pid):
        return False  # the leader is still there; the strict fingerprint owns that case
    pgid = int(entry.get("pgid") or 0)
    return pgid > 0 and process_group_is_alive(pgid)


def live_repo_writer_processes(drive_root: pathlib.Path) -> List[Dict[str, Any]]:
    """Ledger entries for repository-writing processes that are still the process we recorded — or
    whose recorded process GROUP outlived that process."""
    root = pathlib.Path(drive_root)
    path = ledger_path(root)
    if path.exists():
        # Readability probe: `_read_ledger` swallows OSError and answers [], and "no writers" is
        # exactly the answer a fence must never be handed by a file it could not read. Raising
        # instead lets the caller refuse the update. Bounded to ONE byte: the ledger is append-only,
        # so reading it whole here would cost more on every fence and every 60s admission reconcile
        # for the life of an installation, and a single read proves readability just as well.
        with path.open("rb") as probe_fh:
            probe_fh.read(1)
    return [
        entry for entry in _read_ledger(root)
        if entry.get("repo_writer")
        and (_fingerprint_matches(entry) or _repo_writer_group_outlives_leader(entry))
    ]


def _repo_writer_survives(entry: Dict[str, Any]) -> bool:
    """Whether anything from this ledgered writer is still running after we killed it.

    Cheap probes only (no `ps`): this is polled. Identity is not re-checked here — a pid recycled
    within the grace window would be counted as a survivor, which is the fail-closed direction.
    """
    pid = int(entry.get("pid") or 0)
    pgid = int(entry.get("pgid") or 0)
    if pid > 0 and pid_is_alive(pid):
        return True
    # The group is the stronger claim where we have one: the service's own children inherited the
    # checkout and its file handles, so an empty-leader group is not an empty group.
    return pgid > 0 and process_group_is_alive(pgid)


def _kill_and_prove_dead(pid: int, pgid: int, *, grace_sec: float) -> bool:
    """Kill ONE repository writer — by GROUP where we have one — and answer whether it is PROVEN
    gone within ``grace_sec``.

    ``False`` means "still there, OR we could not tell": both are the fail-closed answer, and both
    callers (the fence sweep and the failed-registration repudiation below) treat them alike.
    """
    try:
        if pgid > 0:
            kill_process_group_id(pgid)
        else:
            from ouroboros.platform_layer import kill_pid_tree

            kill_pid_tree(pid)
    except Exception:
        log.warning("Failed to kill repository writer %s", pid, exc_info=True)
    probe = {"pid": pid, "pgid": pgid}
    deadline = time.monotonic() + max(0.0, float(grace_sec))
    while _repo_writer_survives(probe):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


class RepoWriterCustodyError(RuntimeError):
    """A repository writer was left running with neither proof of death nor a fence-visible record.

    Distinct from the ordinary ``RuntimeError`` ``spawn_supervised`` raises for a merely failed
    registration: that one means "no writer is running", this one means "one is, and nothing can
    see it". Callers that can still reach the survivor must not treat the two alike.

    ``proc`` CARRIES that survivor. The raise happens inside ``spawn_supervised``, before the
    ``Popen`` has been returned to anybody, so an error that named the situation without handing
    over the handle described a process the caller then had no way to reach: ``_start_service`` saw
    its own ``proc`` still ``None`` and could neither register nor re-tear-down what was running.
    """

    def __init__(self, message: str, *, proc: Optional[subprocess.Popen] = None):
        super().__init__(message)
        self.proc = proc


def _repudiate_unregistered_repo_writer(
    drive_root: pathlib.Path,
    proc: subprocess.Popen,
    *,
    purpose: str,
    scope: str,
    owner_task_id: str,
) -> None:
    """Undo a repository-writer spawn whose custody record could not be written.

    The ledger is the fence's ONLY registry for a writer started inside a pooled worker, so an
    unrecorded one would survive the fence invisibly. Returning from this function ASSERTS one of
    exactly two postconditions, and nothing else:

      1. the process is proven dead, or
      2. a MINIMAL ledger entry (no ``ps`` fingerprint — the very thing that may have just failed)
         is durably written, so ``live_repo_writer_processes`` sees a survivor and the next fence
         refuses the update. A fingerprint-free entry matches on liveness alone: fail-closed.

    Neither is established on a single try. The two postconditions are attempted in ALTERNATION,
    ``_REPO_WRITER_REPUDIATION_ROUNDS`` times: each kill re-signals and re-proves (a group that was
    merely slow to be reaped is proven gone on the next round, since the previous signal has had the
    whole failing-append window to land), and each latch re-attempts the publication (``append_jsonl``
    has its own retries, but a transient ENOSPC/EROFS outlives them). Bounded, because this runs on
    a spawn path a caller is waiting on.

    When the rounds are exhausted this RAISES ``RepoWriterCustodyError`` rather than returning.
    Logging was the terminal outcome before, which let a live, unlatchable writer leave through the
    ordinary spawn-failure path as though the spawn had simply not happened.

    Residual, named rather than papered over: a ledger this process cannot append to is a ledger no
    supervisor-visible blocker can reach either, so the raise is the last signal available here.
    What it buys is that the signal is DISTINGUISHABLE and CARRIES THE HANDLE — the caller gets the
    live ``Popen`` back and can keep controlling it (and keep its admission lease) instead of
    discarding both as a start that never happened.
    """
    pid = int(getattr(proc, "pid", 0) or 0)
    try:
        pgid = process_group_id(pid)
    except Exception:
        pgid = 0
    entry = {
        "pid": pid,
        "pgid": int(pgid or 0),
        "purpose": str(purpose or "")[:200],
        "scope": scope if scope in _VALID_SCOPES else "session",
        "owner_task": str(owner_task_id or ""),
        "session_id": _SESSION_ID,
        "repo_writer": True,
    }
    for _round in range(_REPO_WRITER_REPUDIATION_ROUNDS):
        if _kill_and_prove_dead(pid, int(pgid or 0), grace_sec=_REPO_WRITER_KILL_GRACE_SEC):
            return
        try:
            # Same reason as in `record_process`: a False here is a failed append, not a written
            # one, and this is the LAST place the fence could have been told about this process.
            if append_jsonl(
                ledger_path(pathlib.Path(drive_root)), {"ts": utc_now_iso(), **entry}
            ) is True:
                return
        except Exception:
            log.warning(
                "repository writer latch append raised for pid %s (%s)", pid, purpose, exc_info=True
            )
    log.error(
        "unregisterable repository writer %s (%s) could not be killed OR latched in %d rounds",
        pid,
        purpose,
        _REPO_WRITER_REPUDIATION_ROUNDS,
    )
    raise RepoWriterCustodyError(
        f"repository writer {pid} ({purpose!r}) is running with no proof of death and no "
        "fence-visible custody record",
        proc=proc,
    )


def terminate_repo_writer_processes(
    drive_root: pathlib.Path, *, grace_sec: float = _REPO_WRITER_KILL_GRACE_SEC
) -> List[str]:
    """Kill every ledgered repository-writing process; return the ones NOT proven gone.

    An EMPTY list is the only proof the caller may act on. Raises when the ledger cannot be read,
    because an unreadable registry is not evidence of an empty one.
    """
    # A record admitted by `_repo_writer_group_outlives_leader` alone is killed here like any other.
    # Retaining it UNSIGNALLED was the alternative, on the argument that the pgid probe re-verifies
    # no identity — but nothing else ever signals such a record either, and nothing prunes it while
    # its group answers, so the fence would refuse every update and every repo-rooted service start
    # until the orphan happened to exit on its own. That is an unbounded lockout with no operator
    # escape, which is a worse failure than the collateral this kill risks, and the collateral is
    # narrower than it first looks: the branch requires the recorded LEADER PID to be DEAD, so a
    # misdirected kill needs that pid to have been recycled by a process that then made itself a
    # group leader AND exited leaving children — not the ordinary pid-reuse case. Killing by group
    # is also what an operator asked for by starting an update, which is the only caller here.
    remaining: List[str] = []
    for entry in live_repo_writer_processes(drive_root):
        pid = int(entry.get("pid") or 0)
        if not _kill_and_prove_dead(pid, int(entry.get("pgid") or 0), grace_sec=grace_sec):
            # Still refused, and now bounded: `_kill_and_prove_dead` returning False for a group we
            # may not signal at all (another user's recycled pgid → EPERM → `process_group_is_alive`
            # fails closed) is the one case that stays a standing blocker, and it is one no action
            # available to this process could clear.
            remaining.append(f"{str(entry.get('purpose') or 'process')}#{pid}")
    return remaining


def reap_orphaned_processes(
    drive_root: pathlib.Path,
    *,
    running_task_ids: Optional[set] = None,
    live_owner_skills: Optional[set] = None,
    enforce_companion_reap: bool = False,
) -> List[int]:
    """Kill ledgered processes whose owning generation/task is gone.

    Rules:
      - dead pid / fingerprint mismatch → prune the entry, never kill;
      - current session's entries → keep (their owners are alive);
        EXCEPT task-scoped entries whose owner task is no longer running;
      - previous generations: task/session scopes → kill group + reap event;
        daemon scope → keep (genuine launcher-managed lifecycles, e.g.
        ``server_restart_fallback``).
      - skill COMPANIONS (purpose ``companion:<skill>:<name>``, recorded under
        daemon scope) are the exception to daemon-keep: reap when the owner skill
        is UNINSTALLED (not in ``live_owner_skills``) OR the entry is from a
        FOREIGN generation (``CompanionSupervisor.start()`` always re-spawns a
        fresh pid, so a fingerprint-matching companion from another generation is
        a stale duplicate that also blocks the re-spawn on a port conflict).
        Same-session companions are killed only when their owner is uninstalled.
        ``live_owner_skills`` (installed skill names, disk-derived) is passed in
        to keep this module policy-free; if None **or empty** the companion
        clause keeps everything (fail-safe — never mass-kill on missing info; an
        explicitly empty set is normalized to None below, so no caller can
        trigger a companion mass-reap by passing an empty install set). Transient
        not-live states (disabled / review / deps) are ``stop_skill``'s job, not
        the reaper's. ``enforce_companion_reap=False`` (default) is LOG-ONLY:
        records a ``process_would_reap`` event instead of killing — a safe first
        rollout; flip to True to enforce.
    """
    drive_root = pathlib.Path(drive_root)
    # Fail-safe (defense-in-depth): an explicitly EMPTY live_owner_skills means
    # UNKNOWN (keep-all), NOT "every skill uninstalled". The only production
    # producer (server._installed_skill_names) already coalesces an empty/failed
    # discovery to None; normalizing here too guarantees no caller can trigger a
    # companion mass-reap by handing in an empty set.
    if live_owner_skills is not None and not live_owner_skills:
        live_owner_skills = None
    entries = _read_ledger(drive_root)
    if not entries:
        return []
    reaped: List[int] = []
    survivors: List[Dict[str, Any]] = []
    for entry in entries:
        pid = int(entry.get("pid") or 0)
        scope = str(entry.get("scope") or "task")
        same_session = str(entry.get("session_id") or "") == _SESSION_ID
        if not _fingerprint_matches(entry):
            if _repo_writer_group_outlives_leader(entry):
                # The launcher exited but its group did not, so this record is still the update
                # fence's ONLY handle on whatever is left writing the checkout. Pruning it here
                # would delete that handle; keep it until the group is gone too.
                survivors.append(entry)
            continue  # dead or recycled pid: prune silently
        owner_task = str(entry.get("owner_task") or "")
        purpose = str(entry.get("purpose") or "")
        task_owner_gone = (
            scope == "task"
            and running_task_ids is not None
            and owner_task
            and owner_task not in running_task_ids
        )
        # Skill companions (purpose "companion:<skill>:<name>", daemon scope):
        # reap when the owner skill is uninstalled OR the entry is from a foreign
        # generation (start() always re-spawns → another generation's companion
        # is a stale duplicate). Fail-safe: keep when the live set is unknown or
        # the purpose can't be parsed. Same-session companions are killed only
        # when their owner is uninstalled; foreign-generation ones always are.
        if purpose.startswith("companion:"):
            parts = purpose.split(":", 2)
            owner_skill = parts[1] if (len(parts) >= 3 and parts[0] == "companion") else ""
            owner_uninstalled = (
                bool(owner_skill)
                and live_owner_skills is not None
                and owner_skill not in live_owner_skills
            )
            reapable = (
                live_owner_skills is not None
                and bool(owner_skill)
                and (owner_uninstalled or not same_session)
            )
            if not reapable:
                survivors.append(entry)
                continue
            if not enforce_companion_reap:
                # First-rollout safety: record the intent, do NOT kill yet.
                append_jsonl(drive_root / "logs" / "supervisor.jsonl", {
                    "ts": utc_now_iso(),
                    "type": "process_would_reap",
                    "pid": pid,
                    "pgid": int(entry.get("pgid") or 0),
                    "purpose": purpose,
                    "owner_skill": owner_skill,
                    "reason": "owner_uninstalled" if owner_uninstalled else "foreign_generation",
                    "stale_session": str(entry.get("session_id") or ""),
                })
                survivors.append(entry)
                continue
            # enforce → fall through to the kill block below
        elif scope == "daemon" or (same_session and not task_owner_gone):
            survivors.append(entry)
            continue
        try:
            pgid = int(entry.get("pgid") or 0)
            if pgid > 0:
                kill_process_group_id(pgid)
            else:
                from ouroboros.platform_layer import kill_pid_tree

                kill_pid_tree(pid)
            reaped.append(pid)
            append_jsonl(drive_root / "logs" / "supervisor.jsonl", {
                "ts": utc_now_iso(),
                "type": "process_reaped",
                "pid": pid,
                "pgid": int(entry.get("pgid") or 0),
                "purpose": entry.get("purpose"),
                "scope": scope,
                "owner_task": owner_task,
                "stale_session": str(entry.get("session_id") or ""),
            })
        except Exception:
            log.warning("Failed to reap ledgered process %s", pid, exc_info=True)
            survivors.append(entry)
    _rewrite_ledger(drive_root, survivors)
    return reaped
