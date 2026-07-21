"""Launcher-owned SSH tunnel manager for the desktop Remote connection.

LAUNCHER SUPPORT MODULE. This file is part of the frozen outer shell's review
boundary (like ``launcher.py``): it runs inside the desktop launcher process
only and is never imported by server/agent code — enforced by the
import-boundary test in ``tests/test_remote_tunnel.py``.

Transport contract (owner decisions D5/D9/D10/D21/D22):
- system ``ssh`` only, key/agent auth (``BatchMode=yes`` — never a prompt);
- the remote agent port is discovered by reading ``<data>/state/server_port``
  over ssh on every (re)connect, so server-side port drift self-heals;
- the tunnel binds ``127.0.0.1`` on both ends explicitly;
- liveness is health-based: ``ExitOnForwardFailure`` only covers listener
  setup, ssh stays alive when the forwarded destination later dies, so the
  monitor polls ``/api/health`` through the tunnel and reconnects (bounded,
  stable local port) before giving up.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from ouroboros.platform_layer import subprocess_new_group_kwargs, terminate_process_tree

# --- profile contract -------------------------------------------------------

PROFILE_FIELDS = ("id", "name", "ssh_target", "remote_data_dir", "remote_agent_port")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Opaque ssh destination: user@host, bare host, or an ~/.ssh/config alias.
# Conservative charset; a leading "-" (option injection) is structurally impossible.
_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:\[\]%-]{0,255}$")
# Remote data dir is charset-whitelisted so it needs no remote-shell quoting.
_REMOTE_DIR_RE = re.compile(r"^(~/|/)[A-Za-z0-9_./-]{0,511}$")
_NAME_MAX = 80

DEFAULT_REMOTE_DATA_DIR = "~/Ouroboros/data"

# --- timing/policy constants (module-local; launcher-only surface) ----------

SSH_CONNECT_TIMEOUT_SEC = 10
DISCOVERY_SUBPROCESS_TIMEOUT_SEC = 25
HEALTH_CONNECT_TIMEOUT_SEC = 20.0
HEALTH_POLL_INTERVAL_SEC = 5.0
HEALTH_FAIL_THRESHOLD = 3
RECONNECT_TOTAL_SEC = 120.0
RECONNECT_BACKOFF_SEC = (2.0, 5.0, 10.0, 15.0)

_BASE_SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SEC}",
    # Never let multiplexing/config detach forwarding custody from our child.
    "-o", "ControlMaster=no",
    "-o", "ControlPath=none",
]


class ProfileError(ValueError):
    """A remote-connection profile failed validation."""


class TunnelError(RuntimeError):
    """A connect/discovery/health step failed, with a typed state + owner hint."""

    def __init__(self, state: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.state = state
        self.hint = hint


def ssh_available() -> Optional[str]:
    """Absolute path of the system ssh, or None (feature disabled in UI)."""
    return shutil.which("ssh")


def validate_profile(raw: Any) -> Dict[str, Any]:
    """Normalize one profile dict; raise ProfileError with a readable reason."""
    if not isinstance(raw, dict):
        raise ProfileError("profile must be an object")
    unknown = set(raw) - set(PROFILE_FIELDS)
    if unknown:
        raise ProfileError(f"unknown profile fields: {sorted(unknown)}")
    profile_id = str(raw.get("id") or "").strip()
    if not _ID_RE.match(profile_id):
        raise ProfileError("profile id must be 1-64 chars of [A-Za-z0-9_-]")
    name = str(raw.get("name") or "").strip() or profile_id
    if len(name) > _NAME_MAX or any(ord(ch) < 32 for ch in name):
        raise ProfileError(f"profile name must be printable and at most {_NAME_MAX} chars")
    target = str(raw.get("ssh_target") or "").strip()
    if not _TARGET_RE.match(target):
        raise ProfileError(
            "ssh_target must be user@host, a host, or an ~/.ssh/config alias "
            "(printable, no spaces, must not start with '-')"
        )
    normalized: Dict[str, Any] = {"id": profile_id, "name": name, "ssh_target": target}
    remote_dir = str(raw.get("remote_data_dir") or "").strip()
    if remote_dir:
        if not _REMOTE_DIR_RE.match(remote_dir):
            raise ProfileError(
                "remote_data_dir must start with '/' or '~/' and use only "
                "[A-Za-z0-9_./-] (it is passed to the remote shell unquoted)"
            )
        normalized["remote_data_dir"] = remote_dir
    port = raw.get("remote_agent_port")
    if port not in (None, "", 0):
        try:
            port_int = int(port)
        except (TypeError, ValueError):
            raise ProfileError("remote_agent_port must be an integer") from None
        if not 1 <= port_int <= 65535:
            raise ProfileError("remote_agent_port must be in 1..65535")
        normalized["remote_agent_port"] = port_int
    return normalized


def validate_profiles(raw: Any) -> List[Dict[str, Any]]:
    """Normalize a profile list; ids must be unique."""
    if not isinstance(raw, list):
        raise ProfileError("profiles must be a list")
    from ouroboros.config import REMOTE_CONNECTIONS_MAX

    if len(raw) > REMOTE_CONNECTIONS_MAX:
        raise ProfileError(f"at most {REMOTE_CONNECTIONS_MAX} profiles are supported")
    seen: set = set()
    result: List[Dict[str, Any]] = []
    for item in raw:
        profile = validate_profile(item)
        if profile["id"] in seen:
            raise ProfileError(f"duplicate profile id: {profile['id']}")
        seen.add(profile["id"])
        result.append(profile)
    return result


# --- pure argv builders ------------------------------------------------------

def remote_port_file_path(profile: Dict[str, Any]) -> str:
    base = str(profile.get("remote_data_dir") or DEFAULT_REMOTE_DATA_DIR)
    return f"{base.rstrip('/')}/state/server_port"


def discovery_argv(profile: Dict[str, Any], *, ssh_path: str = "ssh") -> List[str]:
    return [
        ssh_path, "-n", "-T", *_BASE_SSH_OPTS,
        "--", profile["ssh_target"],
        f"cat {remote_port_file_path(profile)}",
    ]


def unit_active_argv(profile: Dict[str, Any], *, ssh_path: str = "ssh") -> List[str]:
    return [
        ssh_path, "-n", "-T", *_BASE_SSH_OPTS,
        "--", profile["ssh_target"],
        "systemctl --user is-active ouroboros",
    ]


def tunnel_argv(
    profile: Dict[str, Any], local_port: int, remote_port: int, *, ssh_path: str = "ssh"
) -> List[str]:
    return [
        ssh_path, "-N", "-T", *_BASE_SSH_OPTS,
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-L", f"127.0.0.1:{int(local_port)}:127.0.0.1:{int(remote_port)}",
        "--", profile["ssh_target"],
    ]


def parse_discovered_port(stdout: str) -> int:
    text = (stdout or "").strip()
    if not re.fullmatch(r"[0-9]{1,5}", text):
        raise TunnelError(
            "invalid_port",
            f"remote port file did not contain a port (got {text[:32]!r})",
        )
    port = int(text)
    if not 1 <= port <= 65535:
        raise TunnelError("invalid_port", f"remote port {port} is out of range")
    return port


# --- impure helpers ----------------------------------------------------------

def pick_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def check_health(local_port: int, *, timeout: float = 3.0) -> bool:
    url = f"http://127.0.0.1:{int(local_port)}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def fetch_remote_state(local_port: int, *, timeout: float = 5.0) -> Dict[str, Any]:
    """GET /api/state through the tunnel (launcher compatibility handshake)."""
    import json

    url = f"http://127.0.0.1:{int(local_port)}/api/state"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
            return payload if isinstance(payload, dict) else {}
    except (urllib.error.URLError, OSError, ValueError):
        return {}


def _run_ssh(argv: List[str], *, timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        **subprocess_new_group_kwargs(),
    )


def discover_remote_port(profile: Dict[str, Any], *, ssh_path: str = "ssh") -> int:
    """Read the remote server_port file over ssh; raise typed TunnelError."""
    explicit = profile.get("remote_agent_port")
    if explicit:
        return int(explicit)
    try:
        proc = _run_ssh(
            discovery_argv(profile, ssh_path=ssh_path),
            timeout=DISCOVERY_SUBPROCESS_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        raise TunnelError(
            "ssh_failed",
            f"ssh to {profile['ssh_target']} timed out during port discovery",
        ) from None
    if proc.returncode == 0:
        return parse_discovered_port(proc.stdout)
    if proc.returncode == 255:
        # ssh transport/auth failure (openssh reports both as 255; best-effort text).
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        raise TunnelError(
            "ssh_failed",
            f"ssh to {profile['ssh_target']} failed: {tail[0][:200]}",
            hint=(
                "check the target, keys/agent, and run "
                f"`ssh {profile['ssh_target']} true` once interactively "
                "(BatchMode never prompts for passwords or host keys)"
            ),
        )
    # Remote command ran but cat failed: the port file is absent.
    state, hint = "port_file_missing", "start it: systemctl --user start ouroboros"
    try:
        active = _run_ssh(
            unit_active_argv(profile, ssh_path=ssh_path),
            timeout=DISCOVERY_SUBPROCESS_TIMEOUT_SEC,
        )
        if active.returncode != 0 and "inactive" in (active.stdout or ""):
            state = "server_inactive"
    except (subprocess.TimeoutExpired, OSError):
        pass
    raise TunnelError(
        state,
        f"Ouroboros server on {profile['ssh_target']} is not running "
        f"(no {remote_port_file_path(profile)})",
        hint=hint,
    )


# --- connection manager ------------------------------------------------------

@dataclasses.dataclass
class _Live:
    generation: int
    profile: Dict[str, Any]
    local_port: int
    remote_port: int
    proc: "subprocess.Popen[bytes]"


class RemoteTunnelManager:
    """One active ssh tunnel with health-based monitoring and reconnect.

    Owned by the launcher. ``on_state_change(status_dict)`` fires on every
    transition; the launcher uses ``connected``/``gave_up`` transitions to
    move the webview between local and tunnel URLs.
    """

    def __init__(
        self,
        data_dir: pathlib.Path,
        *,
        on_state_change: Optional[Callable[[Dict[str, Any]], None]] = None,
        ssh_path: str = "",
    ) -> None:
        self._data_dir = pathlib.Path(data_dir)
        self._on_state_change = on_state_change
        self._ssh_path = ssh_path or ssh_available() or "ssh"
        self._lock = threading.RLock()
        self._generation = 0
        self._live: Optional[_Live] = None
        self._status: Dict[str, Any] = {"state": "disconnected"}

    # -- public API -----------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def connect(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        """Open a tunnel to ``profile`` (replacing any current connection)."""
        profile = validate_profile(profile)
        self.disconnect()
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._set_status(
                state="connecting",
                profile_id=profile["id"],
                profile_name=profile["name"],
            )
        try:
            remote_port = discover_remote_port(profile, ssh_path=self._ssh_path)
            local_port = pick_local_port()
            live = self._spawn_and_wait(generation, profile, local_port, remote_port)
        except TunnelError as exc:
            with self._lock:
                if generation == self._generation:
                    self._set_status(
                        state="error",
                        profile_id=profile["id"],
                        profile_name=profile["name"],
                        error=str(exc),
                        error_state=exc.state,
                        hint=exc.hint,
                    )
            raise
        with self._lock:
            if generation != self._generation:
                _terminate_quietly(live.proc)
                raise TunnelError("ssh_failed", "connection superseded")
            self._live = live
            self._set_status(
                state="connected",
                profile_id=profile["id"],
                profile_name=profile["name"],
                local_port=live.local_port,
                remote_port=live.remote_port,
            )
        threading.Thread(
            target=self._monitor, args=(generation,), daemon=True,
            name=f"remote-tunnel-monitor-{generation}",
        ).start()
        return self.status()

    def disconnect(self) -> None:
        with self._lock:
            self._generation += 1
            live, self._live = self._live, None
            self._set_status(state="disconnected")
        if live is not None:
            _terminate_quietly(live.proc)

    # -- internals -------------------------------------------------------------

    def _set_status(self, **status: Any) -> None:
        self._status = status
        callback = self._on_state_change
        if callback is not None:
            try:
                callback(dict(status))
            except Exception:
                pass

    def _spawn_and_wait(
        self,
        generation: int,
        profile: Dict[str, Any],
        local_port: int,
        remote_port: int,
    ) -> _Live:
        argv = tunnel_argv(profile, local_port, remote_port, ssh_path=self._ssh_path)
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                **subprocess_new_group_kwargs(),
            )
        except OSError as exc:
            raise TunnelError("ssh_unavailable", f"could not start ssh: {exc}") from exc
        try:
            from ouroboros.process_custody import record_process

            record_process(
                self._data_dir,
                pid=proc.pid,
                cmd=argv,
                purpose=f"remote_ssh_tunnel:{profile['id']}",
                scope="daemon",
            )
        except Exception:
            pass  # custody is best-effort forensics; teardown is launcher-owned
        deadline = time.time() + HEALTH_CONNECT_TIMEOUT_SEC
        while time.time() < deadline:
            if proc.poll() is not None:
                stderr_tail = ""
                try:
                    stderr_tail = (proc.stderr.read() or b"").decode(
                        "utf-8", errors="replace"
                    ).strip().splitlines()[-1:][0] if proc.stderr else ""
                except Exception:
                    pass
                state = "bind_conflict" if "forwarding" in stderr_tail.lower() else "ssh_failed"
                raise TunnelError(
                    state,
                    f"ssh tunnel exited (code {proc.returncode}): {stderr_tail[:200]}",
                    hint=(
                        "run `ssh %s true` once interactively if keys/host "
                        "verification are not set up yet" % profile["ssh_target"]
                    ),
                )
            if check_health(local_port):
                return _Live(generation, profile, local_port, remote_port, proc)
            with self._lock:
                if generation != self._generation:
                    _terminate_quietly(proc)
                    raise TunnelError("ssh_failed", "connection superseded")
            time.sleep(0.5)
        _terminate_quietly(proc)
        raise TunnelError(
            "health_timeout",
            f"tunnel to {profile['ssh_target']} established no healthy "
            f"/api/health response within {int(HEALTH_CONNECT_TIMEOUT_SEC)}s",
        )

    def _monitor(self, generation: int) -> None:
        failures = 0
        while True:
            time.sleep(HEALTH_POLL_INTERVAL_SEC)
            with self._lock:
                if generation != self._generation or self._live is None:
                    return
                live = self._live
            if live.proc.poll() is None and check_health(live.local_port):
                failures = 0
                continue
            failures += 1
            if live.proc.poll() is None and failures < HEALTH_FAIL_THRESHOLD:
                continue
            self._reconnect(generation, live)
            return

    def _reconnect(self, generation: int, live: _Live) -> None:
        profile = live.profile
        with self._lock:
            if generation != self._generation:
                return
            self._set_status(
                state="reconnecting",
                profile_id=profile["id"],
                profile_name=profile["name"],
                local_port=live.local_port,
            )
        _terminate_quietly(live.proc)
        deadline = time.time() + RECONNECT_TOTAL_SEC
        attempt = 0
        last_error: Optional[TunnelError] = None
        while time.time() < deadline:
            with self._lock:
                if generation != self._generation:
                    return
            try:
                remote_port = discover_remote_port(profile, ssh_path=self._ssh_path)
                new_live = self._spawn_and_wait(
                    generation, profile, live.local_port, remote_port
                )
            except TunnelError as exc:
                last_error = exc
                if exc.state == "bind_conflict":
                    # The stable local port was stolen while we were down —
                    # pick a fresh one exactly once per reconnect cycle.
                    try:
                        live = dataclasses.replace(live, local_port=pick_local_port())
                        continue
                    except OSError:
                        pass
                delay = RECONNECT_BACKOFF_SEC[min(attempt, len(RECONNECT_BACKOFF_SEC) - 1)]
                attempt += 1
                time.sleep(min(delay, max(0.0, deadline - time.time())))
                continue
            with self._lock:
                if generation != self._generation:
                    _terminate_quietly(new_live.proc)
                    return
                self._live = new_live
                self._set_status(
                    state="connected",
                    profile_id=profile["id"],
                    profile_name=profile["name"],
                    local_port=new_live.local_port,
                    remote_port=new_live.remote_port,
                    reconnected=True,
                )
            self._monitor(generation)
            return
        with self._lock:
            if generation != self._generation:
                return
            self._live = None
            self._set_status(
                state="gave_up",
                profile_id=profile["id"],
                profile_name=profile["name"],
                error=str(last_error) if last_error else "reconnect window exhausted",
                error_state=getattr(last_error, "state", "health_timeout"),
                hint=getattr(last_error, "hint", ""),
            )


def _terminate_quietly(proc: "subprocess.Popen[Any]") -> None:
    try:
        terminate_process_tree(proc)
    except Exception:
        pass
    # Group SIGTERM is best-effort (a reparented/foreign-group child may miss
    # it); make sure the direct child itself is down before we return.
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
