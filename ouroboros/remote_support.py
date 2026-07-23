"""Remote/headless support state: owner-only connection profiles + server lock.

Extracted from ``config.py`` (P7: config crossed its size gate) but kept as a
thin sibling — every function reuses config's settings-lock/paths internals, so
config stays the single settings authority and re-exports these names for a
stable API. config is imported lazily inside the functions to avoid an import
cycle (config re-imports these at module load).
"""

from __future__ import annotations

import json
from typing import Any

# Desktop remote-connection profiles (owner-only launcher state).
REMOTE_CONNECTIONS_MAX = 32


def _launcher_process_holds_authority() -> bool:
    """True only inside the desktop launcher process (OS-anchored identity).

    Authority = THIS process holds the launcher's exclusive ``PID_FILE`` flock
    (``platform_layer.pid_lock_held``; only launcher.py::main acquires it).
    That identity cannot be minted from agent code: while the launcher lives
    the OS lock is exclusive, so a ``run_command``/``run_script`` interpreter
    (a separate child process) can neither acquire it nor inherit it (children
    are fresh subprocesses, not forks of the launcher). This makes D13
    structural — a direct in-process call to the writer refuses regardless of
    how the function name was reached; the string detector in tools/registry
    stays as defense-in-depth. Deliberately NOT a pid-file CONTENT check: the
    advisory lock does not prevent writes, so file content is forgeable — and
    on Windows re-reading a LockFileEx-held file from a second handle fails.
    Residual limit (documented in ARCHITECTURE): same-user arbitrary code can
    still bypass Python and write settings.json raw; closing that class needs
    OS-level user separation, out of v1 scope.
    """
    from ouroboros import platform_layer

    return platform_layer.pid_lock_held()


def preserve_disk_remote_connections(settings: dict) -> dict:
    """Carry the CURRENT on-disk profile list into a generic settings write.

    Caller MUST hold the settings lock. Every generic save writes a full dict
    loaded BEFORE the lock, so a launcher profile write landing in between
    would be clobbered by the stale list riding along. The server never
    legitimately changes OUROBOROS_REMOTE_CONNECTIONS (merge-skipped, no HTTP
    surface), so at write time the disk value is authoritative. Unreadable
    file → leave the dict as-is (the generic save overwrites wholesale anyway,
    matching its pre-existing corrupt-file behavior).
    """
    from ouroboros import config

    key = "OUROBOROS_REMOTE_CONNECTIONS"
    try:
        parsed = json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(parsed, dict) and key in parsed:
            settings[key] = parsed[key]
    except (OSError, ValueError):
        pass
    return settings


def get_remote_connections() -> list:
    from ouroboros import config

    return list(
        config._coerce_setting_value(
            "OUROBOROS_REMOTE_CONNECTIONS",
            config.load_settings().get("OUROBOROS_REMOTE_CONNECTIONS"),
        )
    )


def update_remote_connections(profiles: list) -> list:
    """Owner-only replacement write of OUROBOROS_REMOTE_CONNECTIONS.

    Holds the settings lock across the whole read-modify-write (a separate
    load()+save() pair would race the server's own saves and clobber them).
    Writes exactly this one key and preserves every other on-disk key verbatim,
    so the save_settings mode ratchets are structurally out of reach. Callers
    (the launcher bridge) validate profile semantics via
    ouroboros/remote_tunnel.py before calling; this layer enforces shape only.
    """
    from ouroboros import config

    if not _launcher_process_holds_authority():
        raise RuntimeError(
            "OUROBOROS_REMOTE_CONNECTIONS is owner-only: update_remote_connections "
            "runs only inside the desktop launcher process (whose pid holds "
            f"{config.PID_FILE}). Agent, server and worker processes must not "
            "modify remote connection profiles — the owner manages them in "
            "Settings → Remote."
        )
    normalized = config._coerce_setting_value("OUROBOROS_REMOTE_CONNECTIONS", profiles)
    config._guard_live_settings_write()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = config._acquire_settings_lock()
    try:
        raw: dict = {}
        if config.SETTINGS_PATH.exists():
            # Refuse rather than clobber: if the file exists but is unreadable/
            # unparseable, writing only this one key would DESTROY every other
            # on-disk key (the RMW promises to preserve them). A hand-fixable
            # partial corruption must survive for the owner to repair.
            try:
                parsed = json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(
                    f"refusing to write remote connections: {config.SETTINGS_PATH} "
                    f"exists but is not readable/parseable ({exc}); fix it first"
                ) from exc
            if isinstance(parsed, dict):
                raw = parsed
        raw["OUROBOROS_REMOTE_CONNECTIONS"] = normalized
        # settings.json is 0600 and holds API keys — use the shared atomic
        # writer, which PRESERVES the existing permission bits (a bare
        # write_text on a fresh temp file would create it 0644 and os.replace
        # would relax the secret file to world-readable).
        from ouroboros.utils import write_text_atomic

        write_text_atomic(config.SETTINGS_PATH, json.dumps(raw, indent=2))
        return list(normalized)
    finally:
        config._release_settings_lock(fd)


# Headless server lock (SERVER_PID_FILE): held by `ouroboros server` for the
# lifetime of the process so two source-mode servers cannot share one data dir.
# Handle-based (not the platform_layer process-global slot) so it coexists with
# the launcher lock inside one test process. OS-released on death; the fd is
# close-on-exec, so an execvpe self-restart hands the lock to the replacement
# image, which re-acquires it on its own `_server_command` startup path.
_server_pid_lock_handle: Any = None


def acquire_server_pid_lock() -> bool:
    global _server_pid_lock_handle
    if _server_pid_lock_handle is not None:
        return True
    from ouroboros import config
    from ouroboros.platform_layer import pid_flock_open

    config.SERVER_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = pid_flock_open(str(config.SERVER_PID_FILE))
    if handle is None:
        return False
    _server_pid_lock_handle = handle
    return True


def release_server_pid_lock() -> None:
    """Release the headless server lock; safe no-op when not held.

    Also called by the restart spawn-fallback (`server_control.py`) so the
    replacement process it spawns can acquire the lock before this dying
    process exits.
    """
    global _server_pid_lock_handle
    if _server_pid_lock_handle is None:
        return
    from ouroboros import config
    from ouroboros.platform_layer import pid_flock_close

    pid_flock_close(str(config.SERVER_PID_FILE), _server_pid_lock_handle)
    _server_pid_lock_handle = None


def close_inherited_server_pid_lock() -> None:
    """Close (WITHOUT unlocking) a server-lock fd inherited across fork().

    On Linux, multiprocessing workers are FORKED (not exec'd), so the CLOEXEC
    fd does not auto-close and the worker inherits a duplicate referring to the
    SAME open file description. An flock is held while ANY fd to that description
    is open, so a surviving worker would keep the lock alive after the server
    parent dies — the replacement server then hits exit 43 and
    RestartPreventExitStatus=43 blocks recovery. Each worker calls this at
    startup to drop its inherited copy (a bare close, never LOCK_UN: the parent
    keeps the lock while alive; the worker's copy simply must not outlive it).
    Only touches this (forked) process's module global; the parent is unaffected.
    """
    global _server_pid_lock_handle
    fd_obj = _server_pid_lock_handle
    _server_pid_lock_handle = None
    if fd_obj is not None:
        try:
            fd_obj.close()
        except Exception:
            pass
