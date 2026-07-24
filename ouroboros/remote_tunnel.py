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
import os
import pathlib
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from ouroboros.platform_layer import (
    kill_pid_tree,
    kill_process_group_id,
    process_group_id,
    subprocess_new_group_kwargs,
    terminate_process_tree,
)

# --- profile contract -------------------------------------------------------

PROFILE_FIELDS = ("id", "name", "ssh_target", "remote_data_dir", "remote_agent_port")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Opaque ssh destination: user@host, bare host, or an ~/.ssh/config alias.
# Conservative charset; a leading "-" (option injection) is structurally impossible.
_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:\[\]%-]{0,255}$")
# Remote data dir is charset-whitelisted so it needs no remote-shell quoting.
_REMOTE_DIR_RE = re.compile(r"^(~/|/)[A-Za-z0-9_./-]{0,511}$")
_NAME_MAX = 80

# Matches the documented headless install layout (docs/DEPLOYMENT.md and the
# systemd unit's Environment=OUROBOROS_APP_ROOT=%h/ouroboros-server) — NOT the
# desktop app root (~/Ouroboros). A non-standard install sets the profile's
# remote_data_dir explicitly.
DEFAULT_REMOTE_DATA_DIR = "~/ouroboros-server/data"

# --- timing/policy constants -------------------------------------------------
# DELIBERATE EXCEPTION to the DEVELOPMENT.md "numeric timeouts live in config.py
# SETTINGS_DEFAULTS with a getter + env registration" rule (owner-approved).
# That rule governs AGENT-facing / cognitive waits the owner may tune via the
# settings surface. These are LAUNCHER-ONLY ssh-transport constants: this module
# is imported solely by launcher.py (an import-boundary test asserts the
# server/agent runtime never imports it), so they never belong in the
# agent-readable settings table. They are already CENTRALIZED here as named
# constants (the rule's actual target is *scattered* magic numbers at call
# sites, which this is not), and config.py sits at the P7 module-size gate —
# relocating them would breach that gate. If a deployment ever needs to tune
# these, promote them to config then.

SSH_CONNECT_TIMEOUT_SEC = 10
DISCOVERY_SUBPROCESS_TIMEOUT_SEC = 25
HEALTH_CONNECT_TIMEOUT_SEC = 20.0
HEALTH_POLL_INTERVAL_SEC = 5.0
HEALTH_FAIL_THRESHOLD = 3
RECONNECT_TOTAL_SEC = 120.0
RECONNECT_BACKOFF_SEC = (2.0, 5.0, 10.0, 15.0)
TERMINATE_WAIT_SEC = 5.0  # bounded wait for the ssh child to actually exit

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


CUSTODY_PURPOSE_PREFIX = "remote_ssh_tunnel:"


def reap_orphaned_tunnels(data_dir: pathlib.Path) -> int:
    """Reap any ssh tunnel left ledgered by a previously-crashed launcher.

    The tunnel is recorded daemon-scope (launcher-owned), which the ordinary
    generation reaper keeps — so an abrupt launcher SIGKILL would otherwise
    leave the loopback→remote forward alive indefinitely. The launcher calls
    this at startup; strict-fingerprint matching means a recycled pid is pruned,
    never killed. Returns the number of tunnels reaped.
    """
    try:
        from ouroboros.process_custody import reap_purpose_prefix

        return len(reap_purpose_prefix(pathlib.Path(data_dir), CUSTODY_PURPOSE_PREFIX))
    except Exception:
        return 0


def is_local_origin(current_url: str, local_port: int) -> bool:
    """True when ``current_url`` is the LOCAL Ouroboros server page.

    The launcher's bridge authority gate (D20): privileged owner methods are
    honored only while the window shows the local page — a remote server's SPA
    (another self-modifying being's code) receives the same bridge object and
    must be refused in Python, never merely hidden in JS.
    """
    import urllib.parse

    parsed = urllib.parse.urlparse(str(current_url or ""))
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost"}
        and parsed.port == int(local_port)
    )


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


def remote_ui_compatible(local_port: int) -> bool:
    """Shared post-connect admission (R21C1): the remote must advertise
    ``remote_ui`` via /api/state through the tunnel before the launcher navigates
    the window to it. Enforced IDENTICALLY on initial connect AND on reconnect —
    a remote that restarts/downgrades to an incompatible version must not slip
    through the reconnect navigation. Fail closed on any error."""
    try:
        return fetch_remote_state(int(local_port)).get("remote_ui") is True
    except Exception:
        return False


def _run_ssh(argv: List[str], *, timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        **subprocess_new_group_kwargs(),
    )


def discover_remote_port(
    profile: Dict[str, Any],
    *,
    ssh_path: str = "ssh",
    runner: Optional[Callable[..., "subprocess.CompletedProcess[str]"]] = None,
) -> int:
    """Read the remote server_port file over ssh; raise typed TunnelError.

    ``runner`` runs one ssh command and returns its CompletedProcess; it
    defaults to the module ``_run_ssh``. The manager passes its OWN tracked
    runner so the short-lived discovery ssh is registered under manager custody
    and can be group-killed by ``force_disconnect`` mid-flight (Emergency Stop:
    no ssh subprocess may outlive a panic, even one spawned before ``_live``).
    """
    run = runner or _run_ssh
    explicit = profile.get("remote_agent_port")
    if explicit:
        return int(explicit)
    try:
        proc = run(
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
        active = run(
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
        # Every ssh subprocess that is live but NOT yet owned by self._live:
        # the in-flight discovery `cat` and the tunnel `ssh -N` during its
        # connect/reconnect health-wait window. force_disconnect() group-kills
        # this set together with self._live so a panic in that window can never
        # leave an orphan ssh (Emergency Stop Invariant). Ownership transfers to
        # self._live atomically under self._lock (see connect/_reconnect_once).
        self._inflight: "set[subprocess.Popen[Any]]" = set()
        # Procs currently in GRACEFUL teardown (_terminate_quietly can wait
        # seconds). They are removed from _live/_inflight before that wait, so a
        # concurrent panic would otherwise miss them — force_disconnect also
        # group-kills this set so no terminating ssh survives os._exit (R19C1).
        self._terminating: "set[subprocess.Popen[Any]]" = set()
        # Terminal panic latch: set ONCE by force_disconnect (never cleared —
        # panic is followed by os._exit). Every spawn publishes its Popen to
        # _inflight UNDER self._lock and refuses if the latch is set, so a spawn
        # racing a panic either (a) publishes first and is then group-killed by
        # force_disconnect, or (b) sees the latch and kills nothing because it
        # never spawned. There is no window where a live ssh is invisible to
        # force_disconnect (Emergency Stop Invariant).
        self._shutdown = False
        self._status: Dict[str, Any] = {"state": "disconnected"}
        # State-change callbacks run on a dedicated single-consumer thread,
        # NEVER under self._lock: the launcher callback calls webview.load_url,
        # and status() (polled by the connection pill every few seconds) also
        # takes the lock — invoking load_url while holding it risks a UI-thread
        # stall / deadlock. The queue serializes callbacks in transition order.
        self._emit_q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        if on_state_change is not None:
            threading.Thread(
                target=self._notify_loop, daemon=True, name="remote-tunnel-notify",
            ).start()
        # No tunnel is live at construction (launcher start): clear any stale
        # active-tunnel-port registry a crashed predecessor may have left, so the
        # subagent control-plane deny boundary never over-blocks a random port.
        self._publish_active_tunnel_port(None)

    def _active_tunnel_port_file(self) -> pathlib.Path:
        return self._data_dir / "state" / "active_tunnel_port"

    def _publish_active_tunnel_port(self, port: Optional[int]) -> bool:
        """Publish (or clear) the live tunnel's local port for the subagent
        control-plane deny boundary (R18C1). Returns True on success. A PUBLISH
        (port set) that fails must be treated as fail-CLOSED by the caller — a
        live forward whose port the deny set cannot cover must not be presented
        as connected (R20C3). A CLEAR is best-effort (removing a deny entry never
        creates exposure). Written atomically; the marker is cleared only after
        the forward is confirmed dead (see connect/disconnect)."""
        path = self._active_tunnel_port_file()
        try:
            if port is None:
                path.unlink(missing_ok=True)
                return True
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
            tmp.write_text(str(int(port)), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except OSError:
            return False

    def _notify_loop(self) -> None:
        while True:
            payload = self._emit_q.get()
            try:
                # Every transition is delivered (the pill shows them all); the
                # payload carries its `_generation` so a NAVIGATION-causing
                # callback (gave_up → local, reconnected → new port) can be
                # ignored by the launcher when a newer connect/disconnect has
                # superseded it (R11C1 — a stale callback must never navigate).
                self._on_state_change(payload)  # type: ignore[misc]
            except Exception:
                pass
            finally:
                self._emit_q.task_done()

    @property
    def current_generation(self) -> int:
        with self._lock:
            return self._generation

    # -- in-flight subprocess custody -----------------------------------------

    def _register_inflight(self, proc: "subprocess.Popen[Any]") -> None:
        with self._lock:
            self._inflight.add(proc)

    def _unregister_inflight(self, proc: "subprocess.Popen[Any]") -> None:
        with self._lock:
            self._inflight.discard(proc)

    def _generation_runner(self, generation: int) -> Callable[..., "subprocess.CompletedProcess[str]"]:
        """A discovery runner bound to ``generation`` (every ssh it spawns is
        generation-checked before Popen)."""
        def _run(argv: List[str], *, timeout: float) -> "subprocess.CompletedProcess[str]":
            return self._run_ssh_tracked(argv, timeout=timeout, generation=generation)
        return _run

    def _run_ssh_tracked(
        self, argv: List[str], *, timeout: float, generation: Optional[int] = None
    ) -> "subprocess.CompletedProcess[str]":
        """Run one ssh command under manager custody (see self._inflight).

        Mirrors ``_run_ssh`` semantics (DEVNULL stdin, captured text output,
        TimeoutExpired on overrun) but via Popen so the child is registered
        before it runs and force_disconnect can group-kill it mid-flight.

        ``generation`` binds the spawn to a connection generation: a stale
        generation (a disconnect/newer connect happened) refuses BEFORE Popen,
        so a multi-step discovery (port cat → unit_active fallback) cannot spawn
        its 2nd ssh after disconnect() already returned and cleared _inflight —
        which would otherwise leak past a graceful window close (R11C1).
        """
        # Spawn and publish to _inflight atomically under the force_disconnect
        # lock (CR2): a panic cannot land between Popen and registration.
        with self._lock:
            if self._shutdown:
                raise TunnelError("ssh_failed", "tunnel manager shut down")
            if generation is not None and generation != self._generation:
                raise TunnelError("ssh_failed", "connection superseded")
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **subprocess_new_group_kwargs(),
            )
            self._inflight.add(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
            return subprocess.CompletedProcess(argv, proc.returncode, out, err)
        except BaseException:
            # ANY failure (TimeoutExpired or an unexpected communicate error):
            # group-kill and reap before dropping custody, so no live ssh escapes
            # force_disconnect and no zombie lingers (R18C2).
            _kill_tree_now(proc)
            try:
                proc.communicate(timeout=2)
            except Exception:
                pass
            raise
        finally:
            self._unregister_inflight(proc)

    # -- public API -----------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        with self._lock:
            out = dict(self._status)
        out.pop("_generation", None)  # internal ordering token, not public state
        return out

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
            remote_port = discover_remote_port(
                profile, ssh_path=self._ssh_path,
                runner=self._generation_runner(generation),
            )
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
        superseded = False
        marker_ok = True
        with self._lock:
            if generation != self._generation:
                # Superseded: hand the proc to _terminating under the lock (stays
                # visible to a concurrent panic); the graceful wait runs OUTSIDE
                # the lock (R20C1 — never hold force_disconnect's lock for ~10s).
                self._inflight.discard(live.proc)
                self._terminating.add(live.proc)
                superseded = True
            else:
                # Ownership transfer + deny-boundary marker + connected status,
                # ALL under one lock: a fast atomic marker write (NOT a graceful
                # wait) so the marker-present ⟺ forward-live invariant has no gap
                # (R20C3), while a panic waits only microseconds here.
                marker_ok = self._publish_active_tunnel_port(live.local_port)
                if marker_ok:
                    self._live = live
                    self._inflight.discard(live.proc)
                    self._set_status(
                        state="connected",
                        profile_id=profile["id"],
                        profile_name=profile["name"],
                        local_port=live.local_port,
                        remote_port=live.remote_port,
                    )
                else:
                    self._inflight.discard(live.proc)
                    self._terminating.add(live.proc)
        if superseded:
            self._finish_terminating(live.proc)
            raise TunnelError("ssh_failed", "connection superseded")
        if not marker_ok:
            # Fail CLOSED (R20C3): a live forward whose port the subagent deny set
            # cannot cover must never be presented as connected.
            self._finish_terminating(live.proc)
            raise TunnelError(
                "custody_failed",
                "could not record the active tunnel port for the subagent "
                "control-plane deny boundary; refusing to connect",
            )
        threading.Thread(
            target=self._supervise, args=(generation,), daemon=True,
            name=f"remote-tunnel-supervisor-{generation}",
        ).start()
        return self.status()

    def disconnect(self) -> None:
        with self._lock:
            self._generation += 1
            live, self._live = self._live, None
            inflight = list(self._inflight)
            self._inflight.clear()
            # Move to _terminating UNDER the same lock, before releasing it, so
            # there is no window where these procs are in no tracked set.
            self._terminating.update(p for p in _dedupe_procs(live, inflight))
            self._set_status(state="disconnected")
        # Bumping the generation makes any in-flight connect abandon its result,
        # but an ssh already spawned would linger until its own timeout — tear
        # the current live tunnel AND every in-flight ssh down now (graceful).
        for proc in _dedupe_procs(live, inflight):
            try:
                _terminate_quietly(proc)
            finally:
                with self._lock:
                    self._terminating.discard(proc)
        # Clear the deny-boundary marker ONLY NOW, after the forward is confirmed
        # dead (R20C3): clearing earlier would drop the deny entry while the old
        # forward was still live for the graceful-wait window.
        self._publish_active_tunnel_port(None)

    def force_disconnect(self) -> None:
        """Panic path: kill the ssh process TREE immediately, no graceful wait.

        The Emergency Stop Invariant forbids any delay in panic teardown, so
        this must NOT use the graceful bounded-wait path (`_terminate_quietly`).
        It group-SIGKILLs the whole ssh tree (catching ProxyJump/ProxyCommand
        descendants) and returns at once. Kills the live tunnel, every in-flight
        ssh (discovery + a tunnel in its connect/reconnect health-wait, before
        _live is assigned), AND every proc mid graceful teardown (_terminating —
        a concurrent disconnect that already removed them from _live/_inflight
        but is still waiting on TERM, R19C1) — otherwise a panic could leave an
        orphan ssh. Safe to call from the launcher's exit-99 branch before
        os._exit.
        """
        with self._lock:
            self._shutdown = True  # terminal: no further spawn may proceed
            self._generation += 1
            live, self._live = self._live, None
            inflight = list(self._inflight)
            self._inflight.clear()
            terminating = list(self._terminating)
            self._terminating.clear()
            self._status = {"state": "disconnected"}
        # force_disconnect bypasses _set_status, so clear the port registry here
        # too — the forward is being killed (R18C1).
        self._publish_active_tunnel_port(None)
        for proc in _dedupe_procs(live, inflight + terminating):
            _kill_tree_now(proc)

    # -- internals -------------------------------------------------------------

    def _set_status(self, **status: Any) -> None:
        # Callers hold self._lock; only the assignment happens here. The
        # callback is dispatched off-lock via the notifier thread (see __init__).
        # Stamp the current generation so the notifier can drop a payload that a
        # newer disconnect/connect has superseded before it reaches the launcher.
        status["_generation"] = self._generation
        self._status = status
        # NOTE: the active_tunnel_port marker is NOT driven from here — it is a
        # forward-liveness invariant (marker present ⟺ a forward is live on that
        # port), published on a CONFIRMED connect and cleared only after the
        # forward is CONFIRMED dead (R20C3). Driving it off status transitions
        # would clear it before the old forward actually terminated.
        if self._on_state_change is not None:
            self._emit_q.put(dict(status))

    def _finish_terminating(self, proc: "subprocess.Popen[Any]") -> None:
        """Graceful kill+reap OUTSIDE self._lock, then drop from _terminating.

        Callers first move proc into _terminating UNDER the lock (so a concurrent
        panic still sees it), then release the lock and call this — the multi-
        second graceful wait must NEVER run while holding the lock force_disconnect
        needs (R20C1 Emergency Stop)."""
        try:
            _terminate_quietly(proc)
        finally:
            with self._lock:
                self._terminating.discard(proc)

    def _spawn_and_wait(
        self,
        generation: int,
        profile: Dict[str, Any],
        local_port: int,
        remote_port: int,
    ) -> _Live:
        argv = tunnel_argv(profile, local_port, remote_port, ssh_path=self._ssh_path)
        # Spawn and publish to _inflight ATOMICALLY under the force_disconnect
        # lock (CR2): the ssh child is visible to a concurrent panic the instant
        # it exists — there is no Popen→register gap in which force_disconnect
        # could observe neither _live nor _inflight and os._exit past a live ssh.
        try:
            with self._lock:
                if self._shutdown:
                    raise TunnelError("ssh_failed", "tunnel manager shut down")
                if generation != self._generation:
                    raise TunnelError("ssh_failed", "connection superseded")
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    **subprocess_new_group_kwargs(),
                )
                self._inflight.add(proc)
        except OSError as exc:
            raise TunnelError("ssh_unavailable", f"could not start ssh: {exc}") from exc
        # From here proc is under manager custody in _inflight, so a panic during
        # the (slower) ledger write or the health-wait group-kills it. On any
        # failure below we terminate AND drop it from _inflight; on success we
        # keep it registered so the caller transfers ownership to _live under a
        # single lock hold (no window where a panic could miss it).
        try:
            # Custody registration is part of a SUCCESSFUL spawn (Process Custody
            # Rule): an unledgered long-lived ssh child is invisible to the
            # startup reaper, so a launcher crash would leak it. If recording
            # fails, tear the child down and fail the connect rather than run it
            # unledgered.
            try:
                from ouroboros.process_custody import record_process

                record_process(
                    self._data_dir,
                    pid=proc.pid,
                    cmd=argv,
                    purpose=f"{CUSTODY_PURPOSE_PREFIX}{profile['id']}",
                    scope="daemon",
                )
            except Exception as exc:
                # Teardown+reap is centralized in the except BaseException below
                # (R18C2), so a custody-record failure no longer leaves a zombie.
                raise TunnelError(
                    "custody_failed",
                    f"could not record the ssh tunnel in the process ledger: {exc}",
                ) from exc
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
                    if self._shutdown or generation != self._generation:
                        # Raise UNDER the lock (fast) — the graceful wait runs in
                        # the except BaseException below, OUTSIDE the lock, so a
                        # concurrent panic is never delayed (R20C1).
                        raise TunnelError("ssh_failed", "connection superseded")
                time.sleep(0.5)
            _terminate_quietly(proc)
            raise TunnelError(
                "health_timeout",
                f"tunnel to {profile['ssh_target']} established no healthy "
                f"/api/health response within {int(HEALTH_CONNECT_TIMEOUT_SEC)}s",
            )
        except BaseException:
            # ANY failure path (expected supersede/timeout, an UNEXPECTED
            # health/HTTP exception, or custody-record failure): guarantee the
            # child is terminal AND reaped before dropping custody, so no live
            # ssh escapes force_disconnect and no zombie lingers (R18C2). Success
            # returns above without entering here, keeping the proc registered
            # for the ownership transfer. _terminate_quietly is idempotent on an
            # already-dead child (fast no-op wait), so paths that already
            # terminated pay nothing.
            _terminate_quietly(proc)
            self._unregister_inflight(proc)
            raise

    def _supervise(self, generation: int) -> None:
        """Iterative watch→reconnect driver (one daemon thread per connection).

        A loop, NOT mutual recursion: watch until the tunnel goes unhealthy,
        attempt a bounded reconnect, and on success loop back to watching. Any
        stale generation (disconnect / a newer connect) or an exhausted
        reconnect window ends the thread. Reconnect cycles therefore never nest
        stack frames (a long session with intermittent flaps stays flat)."""
        while self._watch_until_unhealthy(generation):
            if not self._reconnect_once(generation):
                return

    def _watch_until_unhealthy(self, generation: int) -> bool:
        """Poll health until the tunnel needs reconnecting.

        Returns True if the current live tunnel became unhealthy (caller should
        reconnect); False if this generation is stale/disconnected (stop)."""
        failures = 0
        while True:
            time.sleep(HEALTH_POLL_INTERVAL_SEC)
            with self._lock:
                if generation != self._generation or self._live is None:
                    return False
                live = self._live
            if live.proc.poll() is None and check_health(live.local_port):
                failures = 0
                continue
            failures += 1
            if live.proc.poll() is None and failures < HEALTH_FAIL_THRESHOLD:
                continue
            return True

    def _reconnect_once(self, generation: int) -> bool:
        """One bounded reconnect cycle for the current live tunnel.

        Returns True on a successful reconnect (caller resumes watching), False
        when the generation went stale or the reconnect window was exhausted
        (`gave_up`) — in both cases the supervisor thread should stop."""
        with self._lock:
            if generation != self._generation or self._live is None:
                return False
            live = self._live
            profile = live.profile
            self._set_status(
                state="reconnecting",
                profile_id=profile["id"],
                profile_name=profile["name"],
                local_port=live.local_port,
            )
        _terminate_quietly(live.proc)
        deadline = time.time() + RECONNECT_TOTAL_SEC
        attempt = 0
        repicked = False
        last_error: Optional[TunnelError] = None
        while time.time() < deadline:
            with self._lock:
                if generation != self._generation:
                    return False
            try:
                remote_port = discover_remote_port(
                    profile, ssh_path=self._ssh_path,
                    runner=self._generation_runner(generation),
                )
                new_live = self._spawn_and_wait(
                    generation, profile, live.local_port, remote_port
                )
            except TunnelError as exc:
                last_error = exc
                if exc.state == "bind_conflict" and not repicked:
                    # The stable local port was stolen while we were down —
                    # pick a fresh one ONCE per reconnect cycle, then fall
                    # through to normal backoff so a pathological repeated
                    # steal cannot become an un-backed-off respawn loop.
                    try:
                        live = dataclasses.replace(live, local_port=pick_local_port())
                        repicked = True
                        continue
                    except OSError:
                        pass
                delay = RECONNECT_BACKOFF_SEC[min(attempt, len(RECONNECT_BACKOFF_SEC) - 1)]
                attempt += 1
                time.sleep(min(delay, max(0.0, deadline - time.time())))
                continue
            reconnect_superseded = False
            marker_failed = False
            with self._lock:
                if generation != self._generation:
                    # Hand to _terminating under the lock; graceful wait OUTSIDE
                    # (R20C1).
                    self._inflight.discard(new_live.proc)
                    self._terminating.add(new_live.proc)
                    reconnect_superseded = True
                elif not self._publish_active_tunnel_port(new_live.local_port):
                    # Fail CLOSED, same admission invariant as initial connect
                    # (R21C1): if the deny-boundary marker can't be persisted for
                    # the (possibly re-picked) port, do NOT go connected — tear
                    # this attempt down and keep retrying/backing off.
                    self._inflight.discard(new_live.proc)
                    self._terminating.add(new_live.proc)
                    marker_failed = True
                else:
                    # Marker republished (reconnect may have re-picked the local
                    # port after a bind steal) atomically with going live — a
                    # fast write, not a graceful wait (R20C3).
                    self._live = new_live
                    self._inflight.discard(new_live.proc)
                    self._set_status(
                        state="connected",
                        profile_id=profile["id"],
                        profile_name=profile["name"],
                        local_port=new_live.local_port,
                        remote_port=new_live.remote_port,
                        reconnected=True,
                    )
            if reconnect_superseded:
                self._finish_terminating(new_live.proc)
                return False
            if marker_failed:
                self._finish_terminating(new_live.proc)
                last_error = TunnelError(
                    "custody_failed",
                    "could not record the active tunnel port for the subagent "
                    "control-plane deny boundary",
                )
                delay = RECONNECT_BACKOFF_SEC[min(attempt, len(RECONNECT_BACKOFF_SEC) - 1)]
                attempt += 1
                time.sleep(min(delay, max(0.0, deadline - time.time())))
                continue
            return True
        with self._lock:
            if generation != self._generation:
                return False
            self._live = None
            # The old forward was terminated at the top of this cycle and no new
            # one came up — clear the deny-boundary marker (forward is dead).
            self._publish_active_tunnel_port(None)
            self._set_status(
                state="gave_up",
                profile_id=profile["id"],
                profile_name=profile["name"],
                error=str(last_error) if last_error else "reconnect window exhausted",
                error_state=getattr(last_error, "state", "health_timeout"),
                hint=getattr(last_error, "hint", ""),
            )
        return False


def _dedupe_procs(
    live: "Optional[_Live]", inflight: "List[subprocess.Popen[Any]]"
) -> "List[subprocess.Popen[Any]]":
    """Live tunnel proc + in-flight procs, de-duplicated by identity.

    Ownership transfer removes a proc from _inflight as it becomes _live, so
    overlap is not expected — but a teardown must never kill the same proc
    twice, so dedupe defensively (identity, since Popen is unhashable-safe as a
    set member but we keep it explicit)."""
    procs: List["subprocess.Popen[Any]"] = []
    seen: set = set()
    for proc in ([live.proc] if live is not None else []) + list(inflight):
        if id(proc) in seen:
            continue
        seen.add(id(proc))
        procs.append(proc)
    return procs


def _kill_tree_now(proc: "subprocess.Popen[Any]") -> None:
    """Immediate SIGKILL of the ssh process tree — no graceful wait (panic)."""
    try:
        pgid = process_group_id(proc.pid) if proc.pid and proc.pid > 0 else 0
    except Exception:
        pgid = 0
    try:
        if pgid and pgid > 0:
            kill_process_group_id(pgid)  # whole group incl ProxyJump/Command kids
        elif proc.pid and proc.pid > 0:
            kill_pid_tree(proc.pid)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _terminate_quietly(proc: "subprocess.Popen[Any]") -> None:
    """Terminate the ssh child and WAIT until it is actually reaped.

    Returning before the child exits would leave a zombie and race the reuse of
    the (stable) forwarded local port on the next connect/reconnect. So: group
    TERM, bounded wait, then kill the process tree and wait again.
    """
    try:
        terminate_process_tree(proc)  # group SIGTERM (best-effort)
    except Exception:
        pass
    try:
        proc.wait(timeout=TERMINATE_WAIT_SEC)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        return
    # Still alive after group TERM — escalate to a group/tree SIGKILL, NOT just
    # proc.kill() on the direct ssh child: a ProxyCommand/ProxyJump descendant
    # that ignores TERM would otherwise survive disconnect/reconnect/window-close
    # and orphan the forward (R10C2 — zero-orphans shutdown invariant).
    _kill_tree_now(proc)
    try:
        proc.wait(timeout=TERMINATE_WAIT_SEC)
    except Exception:
        pass
