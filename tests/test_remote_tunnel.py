"""Remote tunnel manager: profile contract, argv safety, state machine, boundary."""

from __future__ import annotations

import ast
import os
import pathlib
import stat
import subprocess
import time

import pytest

from ouroboros import remote_tunnel as rt

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


# --- profile validation -------------------------------------------------------

def test_validate_profile_minimal_and_alias():
    assert rt.validate_profile({"id": "a", "ssh_target": "user@host"})["name"] == "a"
    assert rt.validate_profile({"id": "b", "ssh_target": "prod-box"})["ssh_target"] == "prod-box"


@pytest.mark.parametrize(
    "target",
    [
        "-oProxyCommand=evil",  # option injection
        "user@host cat /etc/passwd",  # whitespace / trailing command
        "host\nevil",  # newline
        "",
        "пример",  # non-ascii
    ],
)
def test_validate_profile_rejects_hostile_targets(target):
    with pytest.raises(rt.ProfileError):
        rt.validate_profile({"id": "a", "ssh_target": target})


def test_validate_profile_rejects_unknown_fields_and_bad_port():
    with pytest.raises(rt.ProfileError):
        rt.validate_profile({"id": "a", "ssh_target": "h", "identity_file": "x"})
    for bad_port in ("nope", -1, 0.5, 70000):
        with pytest.raises(rt.ProfileError):
            rt.validate_profile({"id": "a", "ssh_target": "h", "remote_agent_port": bad_port})


@pytest.mark.parametrize("path", ["~/Ouroboros/data", "/srv/obo/data"])
def test_validate_profile_accepts_safe_remote_dirs(path):
    prof = rt.validate_profile({"id": "a", "ssh_target": "h", "remote_data_dir": path})
    assert prof["remote_data_dir"] == path


@pytest.mark.parametrize(
    "path",
    ["relative/dir", "~/x;rm -rf /", "/tmp/dir with space", "/tmp/$(evil)", "~"],
)
def test_validate_profile_rejects_unsafe_remote_dirs(path):
    with pytest.raises(rt.ProfileError):
        rt.validate_profile({"id": "a", "ssh_target": "h", "remote_data_dir": path})


def test_validate_profiles_rejects_duplicates_and_over_bound():
    with pytest.raises(rt.ProfileError):
        rt.validate_profiles(
            [{"id": "a", "ssh_target": "h"}, {"id": "a", "ssh_target": "h2"}]
        )
    from ouroboros.config import REMOTE_CONNECTIONS_MAX

    too_many = [{"id": f"p{i}", "ssh_target": "h"} for i in range(REMOTE_CONNECTIONS_MAX + 1)]
    with pytest.raises(rt.ProfileError):
        rt.validate_profiles(too_many)


# --- argv builders -------------------------------------------------------------

def test_discovery_argv_shape():
    prof = rt.validate_profile({"id": "a", "ssh_target": "user@host"})
    argv = rt.discovery_argv(prof, ssh_path="/usr/bin/ssh")
    assert argv[0] == "/usr/bin/ssh"
    assert "BatchMode=yes" in argv and "ControlMaster=no" in argv
    assert "--" in argv  # option/operand separator before the target
    assert argv[argv.index("--") + 1] == "user@host"
    assert argv[-1] == "cat ~/Ouroboros/data/state/server_port"


def test_tunnel_argv_binds_loopback_explicitly():
    prof = rt.validate_profile(
        {"id": "a", "ssh_target": "box", "remote_data_dir": "/srv/obo/data"}
    )
    argv = rt.tunnel_argv(prof, 50123, 8765)
    assert "-N" in argv and "-T" in argv
    assert "ExitOnForwardFailure=yes" in argv
    forward = argv[argv.index("-L") + 1]
    assert forward == "127.0.0.1:50123:127.0.0.1:8765"
    assert argv[-1] == "box"
    assert rt.remote_port_file_path(prof) == "/srv/obo/data/state/server_port"


@pytest.mark.parametrize(
    "text,expected",
    [("8765\n", 8765), (" 443 ", 443)],
)
def test_parse_discovered_port_ok(text, expected):
    assert rt.parse_discovered_port(text) == expected


@pytest.mark.parametrize("text", ["", "0", "99999", "8765 extra", "print('x')"])
def test_parse_discovered_port_rejects(text):
    with pytest.raises(rt.TunnelError):
        rt.parse_discovered_port(text)


def test_explicit_remote_agent_port_skips_ssh():
    prof = rt.validate_profile(
        {"id": "a", "ssh_target": "h", "remote_agent_port": 9000}
    )
    # ssh_path points nowhere: discovery must not spawn anything.
    assert rt.discover_remote_port(prof, ssh_path="/nonexistent/ssh") == 9000


# --- discovery via stub ssh (real subprocess → serial) --------------------------

def _write_stub_ssh(tmp_path: pathlib.Path, body: str) -> str:
    stub = tmp_path / "ssh"
    stub.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return str(stub)


@pytest.mark.serial
def test_discover_remote_port_reads_stub_output(tmp_path):
    ssh = _write_stub_ssh(tmp_path, 'echo 8123\n')
    prof = rt.validate_profile({"id": "a", "ssh_target": "h"})
    assert rt.discover_remote_port(prof, ssh_path=ssh) == 8123


@pytest.mark.serial
def test_discover_remote_port_classifies_ssh_transport_failure(tmp_path):
    ssh = _write_stub_ssh(tmp_path, 'echo "Permission denied (publickey)." >&2\nexit 255\n')
    prof = rt.validate_profile({"id": "a", "ssh_target": "h"})
    with pytest.raises(rt.TunnelError) as err:
        rt.discover_remote_port(prof, ssh_path=ssh)
    assert err.value.state == "ssh_failed"
    assert "ssh h true" in err.value.hint


@pytest.mark.serial
def test_discover_remote_port_detects_inactive_unit(tmp_path):
    ssh = _write_stub_ssh(
        tmp_path,
        'case "$*" in\n*is-active*) echo inactive; exit 3;;\n*) exit 1;;\nesac\n',
    )
    prof = rt.validate_profile({"id": "a", "ssh_target": "h"})
    with pytest.raises(rt.TunnelError) as err:
        rt.discover_remote_port(prof, ssh_path=ssh)
    assert err.value.state == "server_inactive"
    assert "systemctl --user start ouroboros" in err.value.hint


# --- manager state machine (no real ssh) ----------------------------------------

class _FakeProc:
    def __init__(self):
        # Invalid pid so platform group-kill helpers can never touch a real
        # process; _terminate_quietly then falls through to .kill().
        self.pid = -1
        self.stderr = None
        self._dead = False

    def poll(self):
        return 1 if self._dead else None

    def kill(self):
        self._dead = True

    terminate = kill


def _manager(tmp_path, **kwargs) -> rt.RemoteTunnelManager:
    return rt.RemoteTunnelManager(tmp_path, ssh_path="/usr/bin/ssh", **kwargs)


def test_connect_success_and_replace_on_connect(tmp_path, monkeypatch):
    events = []
    mgr = _manager(tmp_path, on_state_change=lambda s: events.append(s["state"]))
    fake = _FakeProc()
    monkeypatch.setattr(rt, "discover_remote_port", lambda p, ssh_path: 8765)
    monkeypatch.setattr(
        rt.RemoteTunnelManager,
        "_spawn_and_wait",
        lambda self, gen, prof, lp, rp: rt._Live(gen, prof, lp, rp, fake),
    )
    monkeypatch.setattr(rt.threading.Thread, "start", lambda self: None)
    status = mgr.connect({"id": "a", "name": "prod", "ssh_target": "u@h"})
    assert status["state"] == "connected"
    assert status["remote_port"] == 8765
    # Replace-on-connect (D27): a second connect supersedes and kills the first.
    fake2 = _FakeProc()
    monkeypatch.setattr(
        rt.RemoteTunnelManager,
        "_spawn_and_wait",
        lambda self, gen, prof, lp, rp: rt._Live(gen, prof, lp, rp, fake2),
    )
    status2 = mgr.connect({"id": "b", "ssh_target": "u@h2"})
    assert status2["profile_id"] == "b"
    assert fake._dead is True
    mgr.disconnect()
    assert fake2._dead is True
    assert mgr.status()["state"] == "disconnected"
    assert "connecting" in events and "connected" in events


def test_connect_failure_sets_typed_error_status(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)

    def _boom(profile, ssh_path):
        raise rt.TunnelError("server_inactive", "down", hint="start it")

    monkeypatch.setattr(rt, "discover_remote_port", _boom)
    with pytest.raises(rt.TunnelError):
        mgr.connect({"id": "a", "ssh_target": "u@h"})
    status = mgr.status()
    assert status["state"] == "error"
    assert status["error_state"] == "server_inactive"
    assert status["hint"] == "start it"


def test_reconnect_gives_up_and_reports(tmp_path, monkeypatch):
    events = []
    mgr = _manager(tmp_path, on_state_change=lambda s: events.append(dict(s)))
    fake = _FakeProc()
    monkeypatch.setattr(rt, "RECONNECT_TOTAL_SEC", 0.05)
    monkeypatch.setattr(rt, "RECONNECT_BACKOFF_SEC", (0.01,))

    def _still_down(profile, ssh_path):
        raise rt.TunnelError("ssh_failed", "unreachable")

    monkeypatch.setattr(rt, "discover_remote_port", _still_down)
    live = rt._Live(0, rt.validate_profile({"id": "a", "ssh_target": "h"}), 50000, 8765, fake)
    with mgr._lock:
        mgr._generation = 0
        mgr._live = live
    mgr._reconnect(0, live)
    status = mgr.status()
    assert status["state"] == "gave_up"
    assert status["error_state"] == "ssh_failed"
    assert fake._dead is True
    assert any(e["state"] == "reconnecting" for e in events)


def test_monitor_exits_on_generation_change(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)
    monkeypatch.setattr(rt, "HEALTH_POLL_INTERVAL_SEC", 0.01)
    with mgr._lock:
        mgr._generation = 7  # monitor was started for an older generation
    started = time.time()
    mgr._monitor(3)
    assert time.time() - started < 1.0  # returned immediately, no reconnect attempt


# --- import boundary -------------------------------------------------------------

def test_remote_tunnel_is_never_imported_by_server_or_agent_code():
    """remote_tunnel is launcher support; server/agent code must not import it."""
    offenders = []
    roots = [REPO_ROOT / "ouroboros", REPO_ROOT / "supervisor", REPO_ROOT / "server.py"]
    for root in roots:
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in files:
            if path.name == "remote_tunnel.py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(a.name.startswith("ouroboros.remote_tunnel") for a in node.names):
                        offenders.append(str(path))
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module.startswith("ouroboros.remote_tunnel") or (
                        module == "ouroboros"
                        and any(a.name == "remote_tunnel" for a in node.names)
                    ):
                        offenders.append(str(path))
    assert offenders == []


def test_ssh_available_returns_path_or_none():
    result = rt.ssh_available()
    assert result is None or os.path.isabs(result)


def test_stub_subprocess_helpers_do_not_leak(tmp_path):
    # _run_ssh must not hang on a stub that ignores stdin and exits.
    ssh = _write_stub_ssh(tmp_path, "exit 0\n")
    proc = rt._run_ssh([ssh, "arg"], timeout=10)
    assert isinstance(proc, subprocess.CompletedProcess)
