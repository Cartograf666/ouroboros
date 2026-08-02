"""Cross-platform process, locking, path, and runtime helpers."""

from __future__ import annotations

import errno
import logging
import os
import pathlib
import platform
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

log = logging.getLogger(__name__)

# Platform flags.
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

PATH_SEP = ";" if IS_WINDOWS else ":"
_SUBPROCESS_NO_WINDOW = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if IS_WINDOWS else 0
)
_PATH_BOOTSTRAPPED = False


def local_zoneinfo():
    """Best-effort DST-aware local timezone.

    ``astimezone().tzinfo`` is a *fixed* offset that drifts across DST; resolve the IANA
    zone (``TZ`` or ``/etc/localtime``), falling back to the fixed offset.
    """
    import datetime
    from zoneinfo import ZoneInfo

    tz_env = os.environ.get("TZ", "").strip()
    if tz_env:
        try:
            return ZoneInfo(tz_env)
        except Exception:
            log.debug("Invalid TZ env %r for local timezone", tz_env)
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return ZoneInfo(link.split("zoneinfo/", 1)[1])
    except (OSError, ValueError):
        pass
    return datetime.datetime.now().astimezone().tzinfo or datetime.timezone.utc


def is_container_env() -> bool:
    """Return whether explicit env or Docker sentinel indicates a container."""
    if os.environ.get("OUROBOROS_CONTAINER") == "1":
        return True
    # /.dockerenv is Docker's Linux sentinel.
    if IS_LINUX and pathlib.Path("/.dockerenv").exists():
        return True
    return False


def bootstrap_process_path() -> list[str]:
    """Add existing common user tool directories to this process PATH once."""

    global _PATH_BOOTSTRAPPED
    if _PATH_BOOTSTRAPPED:
        return []
    _PATH_BOOTSTRAPPED = True

    candidates: list[pathlib.Path] = []
    home = pathlib.Path.home()
    if IS_MACOS or IS_LINUX:
        candidates.extend([
            pathlib.Path("/opt/homebrew/bin"),
            pathlib.Path("/opt/homebrew/sbin"),
            pathlib.Path("/usr/local/bin"),
            pathlib.Path("/usr/local/sbin"),
            pathlib.Path("/opt/local/bin"),
            home / ".local" / "bin",
            home / ".cargo" / "bin",
            home / ".npm-global" / "bin",
            home / "go" / "bin",
        ])
    if IS_WINDOWS:
        def _env_path(name: str, default: str = "") -> pathlib.Path | None:
            text = os.environ.get(name, default)
            if not text:
                return None
            path = pathlib.Path(text)
            return path if path.is_absolute() else None

        program_files = _env_path("ProgramFiles", r"C:\Program Files")
        local_app_data = _env_path("LOCALAPPDATA")
        app_data = _env_path("APPDATA")
        user_profile = _env_path("USERPROFILE")
        if program_files:
            candidates.extend([program_files / "Git" / "cmd", program_files / "nodejs"])
        if local_app_data:
            candidates.append(local_app_data / "Programs" / "Git" / "cmd")
        if app_data:
            candidates.append(app_data / "npm")
        if user_profile:
            candidates.append(user_profile / ".cargo" / "bin")

    existing = [part for part in os.environ.get("PATH", "").split(PATH_SEP) if part]
    existing_norm = {str(pathlib.Path(part)).lower() if IS_WINDOWS else str(pathlib.Path(part)) for part in existing}
    added: list[str] = []
    for candidate in candidates:
        try:
            if not candidate.is_dir():
                continue
            text = str(candidate)
            norm = text.lower() if IS_WINDOWS else text
            if norm in existing_norm:
                continue
            existing_norm.add(norm)
            added.append(text)
        except OSError:
            continue
    if added:
        os.environ["PATH"] = PATH_SEP.join([*added, *existing])
    return added


def scrub_repo_from_pythonpath(env: dict[str, str], repo_dir: "str | pathlib.Path | None") -> dict[str, str]:
    """Return a copy of *env* with any ``PYTHONPATH`` entry resolving to the Ouroboros
    system repo dir removed.

    An EXTERNAL-workspace command inherits the worker's ``PYTHONPATH`` repo entry, which
    makes the target's ``import web``/``server``/``ouroboros`` resolve to OUROBOROS's modules.
    Dropping ONLY the repo entry isolates the target; no-op without one."""
    out = dict(env)
    raw = out.get("PYTHONPATH", "")
    if not raw or not repo_dir:
        return out
    try:
        repo_resolved = pathlib.Path(repo_dir).resolve(strict=False)
    except Exception:
        return out
    kept: list[str] = []
    for part in raw.split(os.pathsep):
        if not part:
            continue
        try:
            if pathlib.Path(part).resolve(strict=False) == repo_resolved:
                continue
        except Exception:
            pass
        kept.append(part)
    if kept:
        out["PYTHONPATH"] = os.pathsep.join(kept)
    else:
        out.pop("PYTHONPATH", None)
    return out


def acquire_exclusive_file_lock(
    lock_path: pathlib.Path,
    *,
    timeout_sec: float = 4.0,
    stale_sec: float = 90.0,
    metadata: str = "",
    poll_sec: float = 0.05,
) -> Optional[int]:
    """Acquire a portable lockfile using O_EXCL and return its file descriptor."""
    lock_path = pathlib.Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    while (time.time() - started) < timeout_sec:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                text = metadata or f"pid={os.getpid()} ts={time.time()}\n"
                os.write(fd, text.encode("utf-8"))
            except Exception:
                log.debug("Failed to write lock metadata to %s", lock_path, exc_info=True)
            return fd
        except (FileExistsError, PermissionError):
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > stale_sec:
                    lock_path.unlink()
                    continue
            except Exception:
                log.debug("Failed to inspect/remove stale lock %s", lock_path, exc_info=True)
            time.sleep(poll_sec)
        except Exception:
            log.warning("Failed to acquire lock at %s", lock_path, exc_info=True)
            break
    return None


def release_exclusive_file_lock(lock_path: pathlib.Path, lock_fd: Optional[int]) -> None:
    """Release a lock acquired by :func:`acquire_exclusive_file_lock`."""
    lock_path = pathlib.Path(lock_path)
    if lock_fd is None:
        return
    try:
        os.close(lock_fd)
    except Exception:
        log.debug("Failed to close lock fd %s for %s", lock_fd, lock_path, exc_info=True)
    try:
        if lock_path.exists():
            lock_path.unlink()
    except Exception:
        log.debug("Failed to unlink lock file %s", lock_path, exc_info=True)


def unlink_lockfile(lock_path: pathlib.Path) -> None:
    """Best-effort cleanup for path-only locks whose fd was closed after acquire."""
    lock_path = pathlib.Path(lock_path)
    try:
        if lock_path.exists():
            lock_path.unlink()
    except Exception:
        log.debug("Failed to unlink lock file %s", lock_path, exc_info=True)


def open_path_external(path: pathlib.Path) -> None:
    """Open a local path with the platform default application."""

    target = pathlib.Path(path)
    if IS_MACOS:
        subprocess.Popen(["open", str(target)])
    elif IS_WINDOWS:
        os.startfile(str(target))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(target)])


def is_unstable_macos_app_path(path: pathlib.Path) -> bool:
    """Return whether a macOS app path is likely a DMG/AppTranslocation mount."""
    raw = str(path).replace("\\", "/")
    resolved = str(path.resolve()).replace("\\", "/")
    return (
        "AppTranslocation" in raw
        or "AppTranslocation" in resolved
        or raw.startswith("/Volumes/")
        or resolved.startswith("/Volumes/")
    )


def ensure_windows_user_path(path: pathlib.Path) -> None:
    """Add a directory to the current Windows user's PATH and notify shells."""
    if not IS_WINDOWS:
        return
    import winreg  # type: ignore[import-not-found]

    path_text = str(path)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            current, value_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, value_type = "", winreg.REG_EXPAND_SZ
        parts = [p for p in str(current).split(";") if p]
        if any(p.lower() == path_text.lower() for p in parts):
            return
        updated = ";".join(parts + [path_text])
        winreg.SetValueEx(key, "Path", 0, value_type, updated)
    _broadcast_windows_environment_change()


def _broadcast_windows_environment_change() -> None:
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF,  # HWND_BROADCAST
            0x001A,  # WM_SETTINGCHANGE
            0,
            "Environment",
            0x0002,  # SMTO_ABORTIFHUNG
            5000,
            ctypes.byref(result),
        )
    except Exception:
        pass


def _hidden_run(command: list[str], **kwargs):
    if _SUBPROCESS_NO_WINDOW:
        kwargs = dict(kwargs)
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _SUBPROCESS_NO_WINDOW
    return subprocess.run(command, **kwargs)


# PID file locking.
_lock_fd: Any = None


def pid_lock_acquire(path: str) -> bool:
    """Acquire an exclusive PID lock, closing the fd on lock failure."""
    global _lock_fd
    fd_obj = None
    try:
        fd_obj = open(path, "w")
        if IS_WINDOWS:
            _win32_lock(fd_obj.fileno(), exclusive=True, blocking=False)
        else:
            import fcntl
            fcntl.flock(fd_obj, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd_obj.write(str(os.getpid()))
        fd_obj.flush()
        # Promote to global only after lock and PID write both succeed.
        _lock_fd = fd_obj
        return True
    except (IOError, OSError):
        if fd_obj is not None:
            try:
                fd_obj.close()
            except Exception:
                pass
        return False


def pid_lock_release(path: str) -> None:
    """Release the PID lock."""
    global _lock_fd
    if _lock_fd is not None:
        if IS_WINDOWS:
            try:
                _win32_unlock(_lock_fd.fileno())
            except Exception:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            _lock_fd.close()
        except Exception:
            pass
        _lock_fd = None
    try:
        os.unlink(path)
    except Exception:
        pass


# File locking.

def file_lock_exclusive(fd: int) -> None:
    """Acquire an exclusive (write) lock on a file descriptor. Blocks."""
    if IS_WINDOWS:
        _win32_lock(fd, exclusive=True, blocking=True)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)


def file_lock_shared(fd: int) -> None:
    """Acquire a shared (read) lock on a file descriptor. Blocks."""
    if IS_WINDOWS:
        _win32_lock(fd, exclusive=False, blocking=True)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_SH)


def file_lock_exclusive_nb(fd: int) -> None:
    """Try to acquire an exclusive lock, non-blocking. Raises OSError on failure."""
    if IS_WINDOWS:
        _win32_lock(fd, exclusive=True, blocking=False)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def file_unlock(fd: int) -> None:
    """Release a file lock."""
    if IS_WINDOWS:
        _win32_unlock(fd)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def pid_is_alive(pid: int) -> bool:
    """Return whether a PID appears alive without exposing os.kill to callers."""

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# Windows file locking via LockFileEx/UnlockFileEx; unlike msvcrt.locking(),
# this works on empty files by locking a range beyond current size.

# Per-fd OVERLAPPED storage for unlock.
_win32_overlapped: dict = {}


_OVERLAPPED_CLS = None  # cached once per process


def _win32_overlapped_class():
    """Return cached portable OVERLAPPED; ctypes requires one class identity."""
    global _OVERLAPPED_CLS
    if _OVERLAPPED_CLS is not None:
        return _OVERLAPPED_CLS

    import ctypes
    from ctypes import wintypes

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _OVERLAPPED_CLS = OVERLAPPED
    return OVERLAPPED


def _win32_lock(fd: int, *, exclusive: bool = True, blocking: bool = True) -> None:
    """Lock a file descriptor using Win32 LockFileEx. Works on empty files."""
    import ctypes
    from ctypes import wintypes
    import msvcrt as _msvcrt

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002

    OVERLAPPED = _win32_overlapped_class()

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED),
    ]
    kernel32.LockFileEx.restype = wintypes.BOOL

    hfile = _msvcrt.get_osfhandle(fd)
    flags = 0
    if exclusive:
        flags |= _LOCKFILE_EXCLUSIVE_LOCK
    if not blocking:
        flags |= _LOCKFILE_FAIL_IMMEDIATELY

    ov = OVERLAPPED()
    # Win32 whole-file lock pattern: huge range from offset 0.
    if not kernel32.LockFileEx(hfile, flags, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(ov)):
        err = ctypes.get_last_error()
        raise OSError(f"LockFileEx failed (error {err})")

    _win32_overlapped[fd] = (hfile, ov)


def _win32_unlock(fd: int) -> None:
    """Unlock a file descriptor previously locked by _win32_lock."""
    import ctypes
    from ctypes import wintypes

    entry = _win32_overlapped.pop(fd, None)
    if entry is None:
        return

    OVERLAPPED = _win32_overlapped_class()

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED),
    ]
    kernel32.UnlockFileEx.restype = wintypes.BOOL

    hfile, ov = entry
    try:
        kernel32.UnlockFileEx(hfile, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(ov))
    except OSError:
        pass


# Process management.

def kill_process_tree(proc: subprocess.Popen) -> None:
    """Force-kill a subprocess and its entire process tree.

    On POSIX the immediate process group is SIGKILLed first, then descendants that
    escaped into their own session/group are swept by PID — without that sweep a
    cancelled child which spawned grandchildren in new groups leaks orphans.
    Descendants are collected BEFORE the kill: once the parent dies its children are
    reparented and the ppid links disappear.
    """
    pid = proc.pid
    if IS_WINDOWS:
        try:
            _hidden_run(["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True, timeout=10)
        except Exception:
            pass
        return
    descendants: list[int] = []
    try:
        _collect_descendants(pid, descendants)
    except Exception:
        descendants = []
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    for dpid in reversed(descendants):
        try:
            os.kill(dpid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def terminate_process_tree(proc: subprocess.Popen) -> None:
    """Gracefully terminate a subprocess and its process tree."""
    if IS_WINDOWS:
        proc.terminate()
    else:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def terminate_process_group_id(pgid: int) -> None:
    """Gracefully terminate a Unix process group by id."""
    if IS_WINDOWS:
        return
    try:
        os.killpg(int(pgid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError, ValueError):
        pass


def kill_process_group_id(pgid: int) -> None:
    """Force-kill a Unix process group by id."""
    if IS_WINDOWS:
        return
    try:
        os.killpg(int(pgid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, ValueError):
        pass


# `ProcessContainer` token prefix. Deliberately NOT in the scrubbed `OUROBOROS_*` namespace: a
# nested container must keep the OUTER token so an outer reap sees the whole tree; uuids compose.
CONTAINMENT_ENV_PREFIX = "OURO_PROC_CONTAINER_"


# Tri-state membership: UNREADABLE is deliberately NOT a "no" — reading a nondumpable member as a
# non-member is how a live descendant would leave containment without exiting.
MARKER_MEMBER = "member"
MARKER_ABSENT = "absent"
MARKER_UNREADABLE = "unreadable"


def pid_marker_state(pid: int, marker: str) -> str:
    """Tri-state membership for ONE pid from live kernel state: ABSENT means ANSWERED-not-a-member;
    UNREADABLE means unanswerable — ``reap`` treats it as a leak. Windows: ABSENT (job = membership)."""
    if IS_WINDOWS or not marker or int(pid) <= 0:
        return MARKER_ABSENT
    if os.path.isdir("/proc"):
        try:
            with open(f"/proc/{int(pid)}/environ", "rb") as handle:
                data = handle.read()
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ESRCH, errno.ENOTDIR):
                return MARKER_ABSENT  # the pid is gone
            return MARKER_UNREADABLE  # nondumpable, or another user's process
        return MARKER_MEMBER if marker.encode("utf-8", "replace") in data else MARKER_ABSENT
    try:
        out = subprocess.run(["ps", "-E", "-ww", "-p", str(int(pid)), "-o", "command="],
                             capture_output=True, text=True, timeout=10)
    except Exception:
        # No usable `ps` on a system with no /proc: unanswered, not answered "no".
        return MARKER_UNREADABLE
    if out.returncode != 0:
        # `ps -p` fails only when there is no such process, so this is the liveness probe too.
        return MARKER_ABSENT
    if marker in (out.stdout or ""):
        return MARKER_MEMBER
    # ALIVE, and `ps` showed no token. Unlike /proc's EACCES, `ps -E` reports a process whose
    # environment it may not read by OMITTING it — identical to one that never carried the token.
    # Unanswered, then, and unanswered is a leak: it stops a nondumpable member leaving quietly.
    return MARKER_UNREADABLE


def pid_is_zombie(pid: int) -> bool:
    """Whether ``pid`` is an already-exited process still holding a table slot. A SIGKILLed child of
    THIS process keeps its pid, pgid and ``ps`` row until someone ``wait()``s it, and the preflight
    reaps before waiting pytest; a corpse can execute nothing, so counting it only burns time."""
    if IS_WINDOWS or int(pid) <= 0:
        return False
    try:
        if os.path.isdir("/proc"):
            # comm is parenthesised and may contain ')', so state is the field after the LAST.
            with open(f"/proc/{int(pid)}/stat", "rb") as handle:
                fields = handle.read().rpartition(b")")[2].split()
            return bool(fields) and fields[0] == b"Z"
        out = subprocess.run(["ps", "-o", "state=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=10)
        return out.returncode == 0 and (out.stdout or "").strip().startswith("Z")
    except Exception:
        return False


def _proc_start_ticks(pid: int) -> int:
    """Boot-relative start time (``/proc/<pid>/stat`` field 22), or 0 when it cannot be read."""
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as handle:
            fields = handle.read().rpartition(b")")[2].split()
        return int(fields[19]) if len(fields) >= 20 else 0  # rpartition dropped fields 1-2
    except (OSError, ValueError):
        return 0


def _could_be_hidden_member(pid: int, since_ticks: int) -> bool:
    """Whether a pid whose ENVIRONMENT cannot be read could still be a container member.

    Dropping such a pid is how a live descendant leaves containment without exiting — ``setsid()``
    sheds the group and nondumpability sheds the token — so it is enumerated on PLAUSIBILITY and
    reported undetermined, which blocks. What the kernel publishes however it hid (``stat`` and
    ``status`` stay world-readable) rules out two things a member cannot be: started BEFORE the
    container root, which a member is forked FROM, or running under another EFFECTIVE uid, which
    needs privilege we do not have and would be unsignallable anyway. All else is kept, unanswered
    reads included: an exited pid re-probes as absent and a stranger costs a clearable false block,
    while dropping a live member is the leak itself."""
    started = _proc_start_ticks(pid)
    if since_ticks > 0 and 0 < started < since_ticks:
        return False
    try:
        with open(f"/proc/{int(pid)}/status", "rb") as handle:
            for line in handle:
                if line.startswith(b"Uid:"):  # real, EFFECTIVE, saved, fs
                    return int(line.split()[2]) == os.geteuid()
    except (OSError, ValueError, IndexError):
        return True
    return True


def pids_with_env_marker(marker: str, pgid: int = 0, since_ticks: int = 0) -> "Optional[List[int]]":
    """Pids that belong to a container, read from live kernel state; ``None`` when the process
    table could NOT be read at all (conflating that with "empty" reports a clean reap).

    THREE signals, each covering the others' blind spots: the kernel-copied ENVIRONMENT token
    (survives setsid/fd-closing/reparenting — but is claimed by READING the process, so a member
    turning nondumpable drops out silently), the kernel-held PROCESS GROUP (names nondumpable and
    env-replaced members), and on Linux the undetermined-unreadable candidates
    ``_could_be_hidden_member`` cannot rule out (dated by ``since_ticks``), kept fail-closed.
    The group is an ENUMERATION input only — ``reap`` never signals by pgid."""
    if IS_WINDOWS or not marker:
        return []
    found: List[int] = []
    if os.path.isdir("/proc"):
        try:
            entries = os.listdir("/proc")
        except OSError:
            return None
        for name in entries:
            if not name.isdigit():
                continue
            state = pid_marker_state(int(name), marker)
            if (state == MARKER_MEMBER
                    or (pgid and process_group_id(int(name)) == pgid)
                    or (state == MARKER_UNREADABLE
                        and _could_be_hidden_member(int(name), since_ticks))):
                found.append(int(name))
        return found
    try:
        out = subprocess.run(["ps", "-E", "-ww", "-Ao", "pid=,pgid=,command="],
                             capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    for line in (out.stdout or "").splitlines():
        head = line.split(maxsplit=2)
        if len(head) < 3 or not head[0].isdigit():
            continue
        if marker in line or (pgid and head[1] == str(int(pgid))):
            found.append(int(head[0]))
    return found


# A member can fork between scans and its child inherits the token, so one quiet scan proves little:
# `reap` needs `_REAP_QUIET_SCANS` scans in a row with nothing live and nothing undeterminable, and
# FAILS if that has not happened by `_REAP_DEADLINE_SEC`.
_REAP_QUIET_SCANS = 2
_REAP_DEADLINE_SEC = 10.0
_REAP_SETTLE_SEC = 0.05


def _containment_leak_reason(alive: List[int], undetermined: List[int]) -> str:
    """The failure text ``reap`` returns. Both lists are leaks; they differ only in remediation."""
    parts = []
    if alive:
        parts.append("still alive after a best-effort kill: "
                     + ", ".join(str(pid) for pid in alive))
    if undetermined:
        parts.append("liveness could not be determined (the process environment could not be "
                     "read): " + ", ".join(str(pid) for pid in undetermined))
    detail = "; ".join(parts) or ("no member was visible in the last scan, but two consecutive "
                                  "quiet scans were never reached, so nothing is proven gone")
    return (f"the contained tree could not be proven gone within {_REAP_DEADLINE_SEC:.0f}s — "
            f"{detail}")


class ProcessContainer:
    """DETECTION for a spawned tree that outlives its root — not a teardown guarantee.

    POSIX offers no guaranteed teardown of a detached descendant (pids are reusable names,
    membership is not kernel-held, a signal can be refused or land on a recycled stranger), so this
    container claims an honest ANSWER instead: ``reap`` resolves membership
    (``pids_with_env_marker``) from the LIVE table at teardown — never from mid-run samples — then
    attempts one bounded best-effort kill sweep and reports every member still alive or
    undeterminable to the caller, which hard-blocks. Residual limit on Linux: a descendant that
    sheds the token, leaves the group AND changes euid; without ``/proc`` (macOS/BSD) one that
    sheds the token and leaves the group. Windows membership is the Job Object (kernel-enforced
    teardown — but only when the API confirms it). Prefer ``spawn`` over ``Popen`` + ``adopt``:
    POSIX ``adopt`` can neither plant the token nor vouch for a pid or group."""

    def __init__(self) -> None:
        self._job = None
        # The pid `spawn` started and the group it leads: known WITHOUT having to be read.
        self._root = 0
        self._pgid = 0
        # The root's start time: nothing the container spawned can predate it.
        self._start_ticks = 0
        self._suspended = False
        # Non-empty when containment was never ESTABLISHED: else it reads as "everything reaped".
        self._setup_error = ""
        # Unique per instance: a nested preflight and its outer run never claim each other's.
        self._token = f"{CONTAINMENT_ENV_PREFIX}{uuid.uuid4().hex}"

    def containment_env(self) -> "dict[str, str]":
        """Environment entries that make a process and its descendants members. ``spawn`` applies
        these; a caller building its own env for ``Popen`` + ``adopt`` must merge them in."""
        return {self._token: "1"}

    def spawn(self, argv: List[str], **popen_kwargs) -> subprocess.Popen:
        """``Popen`` the process already inside the container, with no gap. The token is merged into
        the caller's ``env`` (defaulting to the inherited one) BEFORE the process exists, so no
        descendant can start outside the membership. On Windows the root is created SUSPENDED too:
        a descendant preceding the job assignment would survive terminate/close. ``adopt`` resumes."""
        kwargs = dict(popen_kwargs)
        group_kwargs = dict(subprocess_new_group_kwargs())
        flags = int(kwargs.pop("creationflags", 0)) | int(group_kwargs.pop("creationflags", 0))
        kwargs.update(group_kwargs)
        env = kwargs.get("env")
        kwargs["env"] = {**(os.environ if env is None else env), **self.containment_env()}
        if IS_WINDOWS:
            flags |= getattr(subprocess, "CREATE_SUSPENDED", 0x4)
            self._suspended = True
        if flags:
            kwargs["creationflags"] = flags
        proc = subprocess.Popen(argv, **kwargs)
        # Knowledge no scan can re-derive: enumeration claims members by READING them, so a root
        # turning nondumpable before the first scan is in no list. `start_new_session` makes it its
        # own group LEADER (pgid == pid); enumerating any OTHER pgid would report bystanders.
        self._root = int(getattr(proc, "pid", 0) or 0)
        pgid = process_group_id(self._root)
        self._pgid = pgid if pgid and pgid == self._root else 0
        self._start_ticks = _proc_start_ticks(self._root)
        self.adopt(proc)
        return proc

    def adopt(self, proc: subprocess.Popen) -> None:
        """Take custody of a just-spawned process; call right after ``Popen``. A no-op on POSIX: the
        token can only be planted before the process exists, and only ``spawn`` knows the pid and
        the group it planted it into. A ``Popen`` + ``adopt`` caller must merge
        ``containment_env()`` into the env it passes, or nothing is contained."""
        if IS_WINDOWS:
            self._job = self._adopt_windows(proc)
            if self._job is None:
                self._setup_error = (
                    "the Windows Job Object could not be created, or the pytest root could not "
                    "be assigned to it, so nothing the run spawned would be kernel-held; the "
                    "still-suspended root was terminated rather than resumed uncontained"
                )

    def _adopt_windows(self, proc: subprocess.Popen):
        """Put ``proc`` in a kill-on-close Job Object; return the handle or None. It is created
        suspended so nothing escapes before assignment, never LEFT suspended (a caller waiting on it
        would deadlock), and never resumed uncontained — an unheld root starts descendants that
        survive terminate/close. A failed create/assign kills the root."""
        pid = int(getattr(proc, "pid", 0) or 0)
        job = create_kill_on_close_job()
        if job is not None and not assign_pid_to_job(job, pid):
            close_job(job)
            job = None
        if job is None:
            self._suspended = False
            force_kill_pid(pid)
            return None
        if self._suspended:
            self._suspended = False
            # If even the resume fails the process is unusable, so tear it down.
            if not resume_process(pid):
                terminate_job(job)
                close_job(job)
                force_kill_pid(pid)
                return None
        return job

    def _scan(self, token: str, pgid: int, known: "set[int]", kill: bool, since: int = 0
              ) -> "tuple[List[int], List[int], str]":
        """ONE membership scan: ``(still alive, undeterminable, enumeration error)``. ``kill`` is
        true for the single sweep only. ``known`` is carried across scans and only ever GROWS: once
        a pid has been seen as a member, dropping out of a later enumeration proves nothing — that
        is what a member turning unreadable looks like too."""
        members = pids_with_env_marker(token, pgid, since)
        if members is None:
            return [], [], ("the live process table could not be enumerated, so the container "
                            "cannot say whether the tree it spawned is still running")
        me = os.getpid()
        known.update(pid for pid in members if pid != me)
        alive: List[int] = []
        undetermined: List[int] = []
        for pid in sorted(known):
            if pid == me or pid_is_zombie(pid):
                continue  # exited; only its parent's `wait()` frees the table slot
            state = pid_marker_state(pid, token)
            if state == MARKER_MEMBER:
                alive.append(pid)
                if kill:
                    # NOTHING stands between the revalidation above and this signal: any lookup
                    # in that gap is a window for the pid to be recycled onto a stranger.
                    force_kill_pid(pid)
            elif pgid and process_group_id(pid) == pgid:
                # The kernel still places it in the container's own group: alive, and a member
                # however its environment reads. Reported, never signalled — a pgid is a borrowed
                # name, so this pid is not proven ours the way a token-bearer is.
                alive.append(pid)
            elif state == MARKER_UNREADABLE:
                undetermined.append(pid)
        return alive, undetermined, ""

    def reap(self) -> str:
        """SCAN the container; return "" only when nothing of it is left, else why not. Detection,
        not guaranteed teardown. ONE bounded best-effort kill sweep runs first; after it nothing is
        ever signalled again, so a member is targeted at most once and the unavoidable
        exit-then-reuse race is entered once rather than every 50ms for ten seconds. The rest is
        rescanning. A member still alive, or one whose liveness could not be DETERMINED (unreadable
        environment, unenumerable table, a Windows job that will not confirm its own termination),
        fails naming the pids — "cannot tell" is not "gone". Handles are consumed once."""
        if IS_WINDOWS:
            if self._setup_error:
                return self._setup_error
            job, self._job = self._job, None
            if job is None:
                return ""
            # Teardown happens HERE, where a failure can still reach the verdict. Terminate AND
            # close — kill-on-close backstops a termination that did not take.
            return "; ".join(text for text in (terminate_job(job), close_job(job)) if text)
        token, self._token = self._token, ""
        root, self._root = self._root, 0
        pgid, self._pgid = self._pgid, 0
        since, self._start_ticks = self._start_ticks, 0
        if self._setup_error or not token:
            return self._setup_error
        # Seeded with the spawned root: a member by construction, not by having been read.
        known: "set[int]" = {root} if root > 0 else set()
        deadline = time.monotonic() + _REAP_DEADLINE_SEC
        alive, undetermined, error = self._scan(token, pgid, known, True, since)  # the ONE sweep
        last, quiet = (alive, undetermined), 0
        while not error:
            if alive or undetermined:
                quiet, last = 0, (alive, undetermined)
            else:
                quiet += 1
            if quiet >= _REAP_QUIET_SCANS:
                return ""
            if time.monotonic() >= deadline:
                # The last NON-EMPTY scan, not the current one: a scan coming back empty just as the
                # deadline passes would name no pid, under a remediation that says to kill them.
                return _containment_leak_reason(*last)
            time.sleep(_REAP_SETTLE_SEC)
            alive, undetermined, error = self._scan(token, pgid, known, False, since)
        return error

    def close(self) -> None:
        """Release the container handle. Inert after ``reap``, which closes it itself."""
        if IS_WINDOWS and self._job is not None:
            close_job(self._job)
            self._job = None


def process_group_is_alive(pgid: int) -> bool:
    """Whether ANY member of Unix group ``pgid`` remains (signal-0 probe). For callers VERIFYING
    a kill, so it fails CLOSED: an unanswerable probe (Windows, refused signal, bad id) reports
    alive — "we could not tell" is not "nothing is left"."""
    if IS_WINDOWS:
        return True
    try:
        os.killpg(int(pgid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, ValueError):
        return True
    return True


def process_group_id(pid: int) -> int:
    """Return the Unix process group id for ``pid`` or 0 when unavailable."""
    if IS_WINDOWS:
        return 0
    try:
        return int(os.getpgid(int(pid)))
    except (ProcessLookupError, PermissionError, OSError, ValueError):
        return 0


def current_process_group_id() -> int:
    """Return the current Unix process group id or 0 when unavailable."""
    if IS_WINDOWS:
        return 0
    try:
        return int(os.getpgrp())
    except (PermissionError, OSError, ValueError):
        return 0


def process_start_time(pid: int) -> str:
    """Best-effort stable start-time token for (pid, start_time) fingerprints.

    A bare pid is not an identity — the kernel reuses it. ``(pid, start_time)`` is, which is what
    lets a caller refuse to signal a pid it merely used to own. POSIX uses ``ps -o lstart=``,
    falling back to the same /proc field the containment scan dates candidates by, so the
    fingerprint stays real on an image with no usable ``ps``. Windows returns "" (callers degrade
    to pid liveness), as does a pid that is already gone."""
    if pid <= 0:
        return ""
    if os.name == "nt":
        return ""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        text = (out.stdout or "").strip()
        if out.returncode == 0 and text:
            return text
    except Exception:
        pass
    ticks = _proc_start_ticks(pid)
    return str(ticks) if ticks else ""


def process_command(pid: int) -> str:
    """Return a best-effort command line for a Unix process."""
    if IS_WINDOWS:
        return ""
    try:
        result = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="],
                                capture_output=True, text=True, timeout=3)
        return result.stdout.strip()
    except Exception:
        return ""


def force_kill_pid(pid: int) -> None:
    """Force-kill a single process by PID."""
    if IS_WINDOWS:
        try:
            _hidden_run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
        except Exception:
            pass
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def kill_pid_tree(pid: int, exclude_pids: "set[int] | None" = None) -> None:
    """Force-kill a PID tree recursively.

    ``exclude_pids`` are spared along with their own descendants, keeping
    ``service_teardown=keep`` services reachable for a verifier when a worker is
    force-killed; spared children reparent to init and fall to the custody reaper.
    """
    exclude = {int(p) for p in (exclude_pids or set())}
    if IS_WINDOWS:
        # exclude_pids is a POSIX-only nicety: descendant enumeration relies on
        # `pgrep -P`, which Windows lacks, so honouring exclusions here would
        # enumerate nothing and LEAK the worker's whole tree (only the root would
        # die). taskkill /T always tree-kills; sparing is unsupported on Windows.
        try:
            _hidden_run(["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True, timeout=10)
        except Exception:
            pass
        return

    descendants: list[int] = []
    _collect_descendants(pid, descendants)
    spared: set[int] = set()
    for ep in exclude:
        spared.add(ep)
        sub: list[int] = []
        _collect_descendants(ep, sub)
        spared.update(sub)
    for dpid in reversed(descendants):
        if dpid in spared:
            continue
        try:
            os.kill(dpid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if pid in spared:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _collect_descendants(pid: int, result: list[int]) -> None:
    """Recursively collect all descendant PIDs via pgrep."""
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)],
                             capture_output=True, text=True, timeout=3)
        for line in out.stdout.strip().splitlines():
            line = line.strip()
            if line:
                child_pid = int(line)
                _collect_descendants(child_pid, result)
                result.append(child_pid)
    except Exception:
        pass


def collect_descendant_pids(pid: int) -> List[int]:
    """Public: all descendant PIDs of ``pid`` (depth-first, children last).

    Keeps tree discovery in the platform layer, off the private recursive helper."""
    result: List[int] = []
    try:
        _collect_descendants(int(pid), result)
    except (TypeError, ValueError):
        pass
    return result


def kill_processes_referencing(marker: str) -> None:
    """Force-kill any process whose command line references ``marker``.

    Sweeps children that double-forked to init, escaping both ``killpg`` and the
    ``pgrep -P`` walk. ``marker`` is matched literally (regex specials escaped) so a
    temp path containing ``.``/``+`` cannot over-match unrelated command lines."""
    if IS_WINDOWS or not marker:
        return
    try:
        out = subprocess.run(
            ["pgrep", "-f", re.escape(marker)], capture_output=True, text=True, timeout=3
        )
    except Exception:
        return
    my_pid = os.getpid()
    for line in (out.stdout or "").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid == my_pid:
            continue
        force_kill_pid(pid)


def kill_process_on_port(port: int) -> None:
    """Kill any process listening on the given TCP port."""
    try:
        if IS_WINDOWS:
            res = _hidden_run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=5,
            )
            for line in res.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    if parts:
                        try:
                            pid = int(parts[-1])
                            if pid != os.getpid():
                                _hidden_run(
                                    ["taskkill", "/F", "/PID", str(pid)],
                                    capture_output=True,
                                )
                        except (ValueError, ProcessLookupError, PermissionError):
                            pass
        else:
            res = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"],
                capture_output=True, text=True, timeout=5,
            )
            for pid_str in res.stdout.strip().split():
                try:
                    pid = int(pid_str)
                    if pid != os.getpid():
                        os.kill(pid, 9)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
    except Exception:
        pass


# Embedded Python paths.

def embedded_python_candidates(base_dir: pathlib.Path) -> List[pathlib.Path]:
    """Return candidate embedded python-build-standalone paths."""
    if IS_WINDOWS:
        return [
            base_dir / "python-standalone" / "python.exe",
            base_dir / "python-standalone" / "python3.exe",
        ]
    return [
        base_dir / "python-standalone" / "bin" / "python3",
        base_dir / "python-standalone" / "bin" / "python",
    ]


def project_venv_python(project_root: pathlib.Path) -> str:
    """Return the executable for a valid project ``.venv`` on this platform.

    Keep the lexical venv path (rather than resolving its symlink) so Python
    discovers the adjacent ``pyvenv.cfg`` and activates the environment.
    """
    env_root = pathlib.Path(project_root) / ".venv"
    if not (env_root / "pyvenv.cfg").is_file():
        return ""
    candidates = (
        (env_root / "Scripts" / "python.exe",)
        if IS_WINDOWS
        else (env_root / "bin" / "python", env_root / "bin" / "python3")
    )
    for candidate in candidates:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return os.path.abspath(os.fspath(candidate))
        except OSError:
            continue
    return ""


def embedded_node_candidates(base_dir: pathlib.Path) -> List[pathlib.Path]:
    """Return candidate bundled Node.js runtime paths."""
    if IS_WINDOWS:
        return [base_dir / "node-standalone" / "node.exe"]
    return [base_dir / "node-standalone" / "bin" / "node"]


def embedded_ripgrep_candidates(base_dir: pathlib.Path) -> List[pathlib.Path]:
    """Return candidate bundled ripgrep paths."""
    if IS_WINDOWS:
        return [base_dir / "ripgrep-standalone" / "rg.exe"]
    return [base_dir / "ripgrep-standalone" / "bin" / "rg"]


def _resolve_bundled(candidates_for: Callable[[pathlib.Path], List[pathlib.Path]]) -> Optional[str]:
    """First existing path from ``candidates_for(base)`` over the frozen and source roots."""
    bases: List[pathlib.Path] = []
    frozen_base = getattr(sys, "_MEIPASS", None)
    if frozen_base:
        bases.append(pathlib.Path(frozen_base))
    # Dev/source layout: the standalone dirs sit at the repo root, two levels up.
    bases.append(pathlib.Path(__file__).resolve().parent.parent)
    for base in bases:
        for candidate in candidates_for(base):
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    return None


def resolve_bundled_node() -> Optional[str]:
    """Return the path to the bundled, signed Node.js runtime if present.

    The packaged app ships an official notarized node under ``node-standalone``, preferred
    over a PATH (e.g. Homebrew) node that macOS code-signing enforcement can SIGKILL when
    launched from the packaged process tree.
    """
    return _resolve_bundled(embedded_node_candidates)


def resolve_bundled_ripgrep() -> Optional[str]:
    """Return the bundled rg path if present."""
    return _resolve_bundled(embedded_ripgrep_candidates)


# Claude runtime resolution.

@dataclass
class ClaudeRuntimeState:
    """Structured Claude SDK/CLI availability snapshot."""
    # App-managed runtime: bundled SDK and CLI.
    app_managed: bool = False
    sdk_version: str = ""
    sdk_path: str = ""
    cli_path: str = ""
    cli_version: str = ""
    interpreter_path: str = ""
    # Legacy user-site runtime.
    legacy_detected: bool = False
    legacy_sdk_path: str = ""
    legacy_sdk_version: str = ""
    # Operational state.
    ready: bool = False
    api_key_set: bool = False
    error: str = ""
    last_stderr: str = ""

    def status_label(self) -> str:
        if not self.sdk_version:
            return "missing"
        # Version errors must not be shadowed by a missing API key.
        if self.error:
            return "error"
        if not self.api_key_set:
            return "no_api_key"
        if not self.ready:
            return "degraded"
        return "ready"


def _find_sdk_package_path() -> Optional[str]:
    """Return the filesystem path to the installed claude_agent_sdk package."""
    try:
        import claude_agent_sdk
        pkg_file = getattr(claude_agent_sdk, "__file__", None)
        if pkg_file:
            return str(pathlib.Path(pkg_file).parent)
    except ImportError:
        pass
    return None


def _find_bundled_cli(sdk_path: str) -> Optional[str]:
    """Locate the bundled CLI binary inside the SDK package."""
    cli_name = "claude.exe" if IS_WINDOWS else "claude"
    bundled = pathlib.Path(sdk_path) / "_bundled" / cli_name
    return str(bundled) if bundled.is_file() else None


def _probe_cli_version(cli_path: str) -> str:
    """Run ``claude -v`` and return the version string, or empty on failure."""
    try:
        result = subprocess.run(
            [cli_path, "-v"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            m = re.match(r"([0-9]+\.[0-9]+\.[0-9]+)", result.stdout.strip())
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


def _detect_legacy_user_site_sdk() -> tuple[bool, str, str]:
    """Detect an SDK installed outside the app-managed python-standalone."""
    sdk_path = _find_sdk_package_path()
    if not sdk_path:
        return False, "", ""
    if "python-standalone" in [p.lower() for p in pathlib.Path(sdk_path).resolve().parts]:
        return False, "", ""
    try:
        import importlib.metadata
        ver = importlib.metadata.version("claude-agent-sdk")
    except Exception:
        ver = ""
    return True, sdk_path, ver


def resolve_claude_runtime() -> ClaudeRuntimeState:
    """Build a deterministic, non-persistent Claude runtime snapshot."""
    state = ClaudeRuntimeState()
    state.interpreter_path = sys.executable

    # SDK availability.
    try:
        import importlib.metadata
        state.sdk_version = importlib.metadata.version("claude-agent-sdk")
    except Exception:
        pass

    sdk_path = _find_sdk_package_path()
    if sdk_path:
        state.sdk_path = sdk_path
        # App-managed SDK lives inside python-standalone.
        parts_lower = [p.lower() for p in pathlib.Path(sdk_path).resolve().parts]
        state.app_managed = "python-standalone" in parts_lower
        cli = _find_bundled_cli(sdk_path)
        if cli:
            state.cli_path = cli
            state.cli_version = _probe_cli_version(cli)

    # Legacy detection.
    legacy_detected, legacy_path, legacy_ver = _detect_legacy_user_site_sdk()
    state.legacy_detected = legacy_detected
    state.legacy_sdk_path = legacy_path
    state.legacy_sdk_version = legacy_ver

    # API key.
    state.api_key_set = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    # Baseline gate avoids false-ready older SDKs with bundled CLI.
    sdk_version_ok = False
    if state.sdk_version:
        try:
            from ouroboros.launcher_bootstrap import _CLAUDE_SDK_MIN_VERSION, _version_tuple
            sdk_version_ok = _version_tuple(state.sdk_version) >= _version_tuple(_CLAUDE_SDK_MIN_VERSION)
        except Exception:
            # Unknown baseline means not-ready so UI offers Repair.
            sdk_version_ok = False
    state.ready = bool(
        state.sdk_version and sdk_version_ok and state.cli_path and state.api_key_set
    )
    if state.sdk_version and not sdk_version_ok and not state.error:
        try:
            from ouroboros.launcher_bootstrap import _CLAUDE_SDK_MIN_VERSION
            state.error = (
                f"Claude SDK {state.sdk_version} is below baseline {_CLAUDE_SDK_MIN_VERSION}. "
                "Run Repair to upgrade."
            )
        except Exception:
            state.error = f"Claude SDK {state.sdk_version} is below the required baseline."

    return state


# System profiling helpers.

def get_system_memory() -> str:
    """Return total system memory as a human-readable string."""
    os_name = platform.system()
    try:
        if os_name == "Darwin":
            mem_bytes = int(subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"],
            ).strip())
            return f"{mem_bytes / (1024**3):.1f} GB"
        elif os_name == "Linux":
            out = subprocess.check_output(
                ["awk", '/MemTotal/ {print $2/1024/1024 " GB"}', "/proc/meminfo"],
            ).strip().decode()
            return out
        elif os_name == "Windows":
            out = _hidden_run(
                ["wmic", "ComputerSystem", "get", "TotalPhysicalMemory", "/value"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout.strip()
            for line in out.splitlines():
                if "=" in line:
                    mem_bytes = int(line.split("=")[1])
                    return f"{mem_bytes / (1024**3):.1f} GB"
    except Exception:
        pass
    return "Unknown"


def get_cpu_info() -> str:
    """Return CPU model string."""
    os_name = platform.system()
    try:
        if os_name == "Darwin":
            return subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
            ).strip().decode()
        elif os_name == "Windows":
            out = _hidden_run(
                ["wmic", "cpu", "get", "Name", "/value"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout.strip()
            for line in out.splitlines():
                if "=" in line:
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return platform.processor()


# Process session isolation.

def create_new_session() -> None:
    """Create a new process session (Unix: setsid). No-op on Windows."""
    if not IS_WINDOWS:
        os.setsid()


def subprocess_new_group_kwargs() -> dict:
    """Return subprocess kwargs for killable process-group/session isolation."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def subprocess_hidden_kwargs() -> dict:
    """Return kwargs to suppress Windows console windows."""
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def merge_hidden_kwargs(kwargs: dict) -> dict:
    """Merge Windows hidden-window flags without dropping caller flags."""
    hidden = subprocess_hidden_kwargs()
    if not hidden:
        return dict(kwargs)
    result = dict(kwargs)
    result["creationflags"] = result.get("creationflags", 0) | hidden.get("creationflags", 0)
    return result


# Git installation hint.

def git_install_hint() -> str:
    """Return platform-appropriate instructions for installing Git."""
    if IS_MACOS:
        return "Install Git via Xcode CLI Tools: xcode-select --install"
    elif IS_WINDOWS:
        return "Download Git from https://git-scm.com/download/win or run: winget install Git.Git"
    else:
        return "Install Git via your package manager, e.g.: sudo apt install git"


# Windows Job Object helpers.

if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes

    # `use_last_error=True` so `ctypes.get_last_error()` reads the code the CALL set: without it
    # ctypes does not snapshot the thread's last error, and the failure text below would quote
    # whatever ctypes' own bookkeeping left behind. Same pattern as the file-lock helpers above.
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    _INVALID_HANDLE_VALUE = ctypes.wintypes.HANDLE(-1)
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOBOBJECTINFOCLASS_EXTENDED = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SUSPEND_RESUME = 0x0800
    _CREATE_SUSPENDED = 0x4

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.wintypes.DWORD),
            ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
            ("PriorityClass", ctypes.wintypes.DWORD),
            ("SchedulingClass", ctypes.wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _ExtendedLimitInfo(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


def create_kill_on_close_job() -> Optional[Any]:
    """Create a Windows kill-on-close Job Object, or None."""
    if not IS_WINDOWS:
        return None
    try:
        handle = _kernel32.CreateJobObjectW(None, None)
        if handle in (0, _INVALID_HANDLE_VALUE):
            log.warning("CreateJobObjectW failed")
            return None
        info = _ExtendedLimitInfo()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            handle,
            _JOBOBJECTINFOCLASS_EXTENDED,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            log.warning("SetInformationJobObject failed")
            _kernel32.CloseHandle(handle)
            return None
        return handle
    except Exception as exc:
        log.warning("Job Object creation failed: %s", exc)
        return None


def assign_pid_to_job(job_handle: Any, pid: int) -> bool:
    """Assign a running process (by PID) to a Job Object. Windows only."""
    if not IS_WINDOWS or job_handle is None:
        return False
    try:
        proc_handle = _kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid,
        )
        if not proc_handle:
            log.warning("OpenProcess(%d) failed for Job Object assignment", pid)
            return False
        ok = _kernel32.AssignProcessToJobObject(job_handle, proc_handle)
        _kernel32.CloseHandle(proc_handle)
        if not ok:
            log.warning("AssignProcessToJobObject failed for pid %d", pid)
            return False
        return True
    except Exception as exc:
        log.warning("Job Object assign failed: %s", exc)
        return False


def terminate_job(job_handle: Any, exit_code: int = 1) -> str:
    """Terminate all processes in a Job Object; "" on success, else the reason it is unproven.

    A FALSE Win32 BOOL is a failure exactly like a raised call, and swallowing either let
    ``ProcessContainer.reap`` report a clean teardown while job members were still running."""
    if not IS_WINDOWS or job_handle is None:
        return ""
    try:
        if not _kernel32.TerminateJobObject(job_handle, exit_code):
            return (f"TerminateJobObject returned false (Win32 error {ctypes.get_last_error()}), "
                    "so the processes held by the job cannot be assumed dead")
    except Exception as exc:
        return f"TerminateJobObject failed ({exc}), so the job's processes are unaccounted for"
    return ""


def close_job(job_handle: Any) -> str:
    """Close a Job Object handle (triggers kill-on-close if set); "" on success, else the reason.

    The handle is the last thing holding kill-on-close, so a close that did not happen leaves
    survivors AND leaks the handle; the caller reports it rather than discarding it."""
    if not IS_WINDOWS or job_handle is None:
        return ""
    try:
        if not _kernel32.CloseHandle(job_handle):
            return (f"CloseHandle on the Job Object returned false (Win32 error "
                    f"{ctypes.get_last_error()}), so kill-on-close never fired")
    except Exception as exc:
        return f"CloseHandle on the Job Object failed ({exc}), so kill-on-close never fired"
    return ""


def resume_process(pid: int) -> bool:
    """Resume all threads of a suspended process. Windows only."""
    if not IS_WINDOWS:
        return False
    try:
        _ntdll = ctypes.windll.ntdll  # type: ignore[attr-defined]
        handle = _kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, pid)
        if not handle:
            log.warning("OpenProcess(%d) failed for resume", pid)
            return False
        status = _ntdll.NtResumeProcess(handle)
        _kernel32.CloseHandle(handle)
        if status != 0:
            log.warning("NtResumeProcess(%d) returned NTSTATUS 0x%08x", pid, status)
            return False
        return True
    except Exception as exc:
        log.warning("resume_process failed: %s", exc)
        return False
