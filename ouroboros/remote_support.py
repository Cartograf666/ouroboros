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


def build_server_conflict_html() -> str:
    """Owner-visible exit-43 page (another live server owns this data dir/port).
    Rendered by the desktop launcher when its server child exits 43; the copy
    names the exact recovery commands. Lives here (the headless-server-lock
    domain) so the launcher stays within its size budget."""
    return (
        "<html><body style='background:#1a1a2e;color:white;font-family:system-ui;display:flex;"
        "align-items:center;justify-content:center;height:100vh;margin:0'><div style='max-width:560px;text-align:center'>"
        "<h2>Another Ouroboros server owns this data directory</h2>"
        "<p>The desktop server exited (code 43) because a different live server — usually a headless "
        "<code>ouroboros server</code> under systemd — already holds this data dir's lock. Restarting cannot "
        "win that race, so the launcher stopped instead of fighting it.</p>"
        "<p style='text-align:left'>To use the desktop app here:<br>1. Stop the other server: "
        "<code>systemctl --user stop ouroboros</code><br>2. Relaunch Ouroboros.</p>"
        "<p>To keep the headless server, connect to it via Settings&nbsp;→&nbsp;Remote from a desktop on "
        "another data dir.</p></div></body></html>"
    )


def _generic_startup_failed_html() -> str:
    """Fallback page when the local server did not become ready for a reason
    OTHER than a data-dir conflict (crash, port bind failure, dependency error)."""
    return (
        "<html><body style='background:#1a1a2e;color:white;font-family:system-ui;"
        "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
        "<div style='text-align:center;max-width:460px;padding:24px'>"
        "<h2>Ouroboros failed to start</h2>"
        "<p>The local agent server did not become ready.</p>"
        "<p style='color:#94a3b8;font-size:13px;margin-top:10px'>"
        "Check launcher.log and agent_stdout.log in the Ouroboros data directory "
        "for details.</p>"
        "</div></body></html>"
    )


def startup_failed_window_spec(conflict_active: bool) -> dict:
    """Choose the startup-failure window (title/html/size) the launcher shows.

    R35C1: on the PRIMARY conflict path a headless server already owns the data
    dir, so the desktop server child exits 43 and the lifecycle loop latches
    ``_server_conflict_active`` BEFORE any window exists — the live
    ``_present_server_conflict_page`` call then no-op'd (window is None). The
    launcher's ``not server_ready`` branch consults that persisted flag through
    this helper so a data-dir conflict shows the ACTIONABLE recovery page
    (``build_server_conflict_html`` — names the exact ``systemctl`` command)
    rather than the generic ``failed to start`` surface. Pure + here (not the
    at-budget launcher) so it is unit-testable without a webview.
    """
    if conflict_active:
        return {
            "title": "Ouroboros — Server Already Running",
            "html": build_server_conflict_html(),
            "width": 560,
            "height": 380,
        }
    return {
        "title": "Ouroboros — Startup Failed",
        "html": _generic_startup_failed_html(),
        "width": 520,
        "height": 260,
    }


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
    from ouroboros import config, platform_layer

    # Require the lock be held on the CANONICAL PID_FILE — not any writable path
    # a worker could lock to forge the identity (R12C1).
    return platform_layer.pid_lock_held(str(config.PID_FILE))


def preserve_disk_remote_connections(settings: dict) -> dict:
    """Force the profile key in a generic settings write to the on-disk value
    (or the empty default when absent) — NEVER the caller's value.

    Caller MUST hold the settings lock. Two guarantees:
      * a launcher profile write landing between a generic caller's load and its
        write is never clobbered by the stale list riding along; and
      * a generic writer can never SEED profiles — R12C1/R12C2: if the caller
        supplied a value and the disk lacks the key (fresh/upgraded install),
        keeping the caller's value would let any save_settings/_owner_write
        caller bypass update_remote_connections's launcher-only gate. So the
        caller's value is ALWAYS discarded; only update_remote_connections (the
        launcher-gated writer, which does not pass through here) sets profiles.
    Unreadable/corrupt disk → empty default (fail toward no-injection).
    """
    from ouroboros import config

    key = "OUROBOROS_REMOTE_CONNECTIONS"
    on_disk: list = []
    try:
        parsed = json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(parsed, dict) and key in parsed:
            on_disk = config._coerce_setting_value(key, parsed[key])
    except (OSError, ValueError):
        pass
    settings[key] = on_disk
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

    def _write_once() -> None:
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
        finally:
            config._release_settings_lock(fd)

    def _persisted() -> bool:
        fd = config._acquire_settings_lock()
        try:
            parsed = json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))
            return isinstance(parsed, dict) and config._coerce_setting_value(
                "OUROBOROS_REMOTE_CONNECTIONS", parsed.get("OUROBOROS_REMOTE_CONNECTIONS"),
            ) == list(normalized)
        except Exception:
            return False
        finally:
            config._release_settings_lock(fd)

    # R29C2: the server's process-tool owner-state restore snapshots the WHOLE
    # settings.json before a run_command/run_script and rewrites that snapshot
    # after — which would silently revert a legitimate launcher profile write
    # landing in that window. Write, then VERIFY it persisted; if a concurrent
    # restore reverted it, retry a few times, and fail LOUDLY if it will not
    # stick so the UI asks the owner to retry instead of falsely reporting
    # success. (Full cross-process interval locking is deferred, D-followup.)
    import time as _time

    for _attempt in range(6):
        _write_once()
        if _persisted():
            return list(normalized)
        _time.sleep(0.25)
    raise RuntimeError(
        "could not persist remote connection profiles: a background agent task "
        "is reverting owner settings right now; try again in a moment"
    )


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
