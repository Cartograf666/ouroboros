"""Remote/headless support state: owner-only connection profiles + server lock.

Extracted from ``config.py`` (P7: config crossed its size gate) but kept as a
thin sibling — every function reuses config's settings-lock/paths internals, so
config stays the single settings authority and re-exports these names for a
stable API. config is imported lazily inside the functions to avoid an import
cycle (config re-imports these at module load).
"""

from __future__ import annotations

import json
import os
from typing import Any

# Desktop remote-connection profiles (owner-only launcher state).
REMOTE_CONNECTIONS_MAX = 32


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

    normalized = config._coerce_setting_value("OUROBOROS_REMOTE_CONNECTIONS", profiles)
    config._guard_live_settings_write()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = config._acquire_settings_lock()
    try:
        raw: dict = {}
        if config.SETTINGS_PATH.exists():
            try:
                parsed = json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    raw = parsed
            except Exception:
                pass
        raw["OUROBOROS_REMOTE_CONNECTIONS"] = normalized
        try:
            tmp = config.SETTINGS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(config.SETTINGS_PATH))
        except OSError:
            config.SETTINGS_PATH.write_text(json.dumps(raw, indent=2), encoding="utf-8")
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
