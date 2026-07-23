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

# The stub-ssh tests write a `#!/bin/sh` script and execute it directly; Windows
# CreateProcess cannot run a shebang script (WinError 193). These tests exercise
# POSIX transport mechanics only — the runtime code is portable (platform_layer).
_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX stub-ssh (shebang script)")


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
    assert argv[-1] == "cat ~/ouroboros-server/data/state/server_port"


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
@_POSIX_ONLY
def test_discover_remote_port_reads_stub_output(tmp_path):
    ssh = _write_stub_ssh(tmp_path, 'echo 8123\n')
    prof = rt.validate_profile({"id": "a", "ssh_target": "h"})
    assert rt.discover_remote_port(prof, ssh_path=ssh) == 8123


@pytest.mark.serial
@_POSIX_ONLY
def test_discover_remote_port_classifies_ssh_transport_failure(tmp_path):
    ssh = _write_stub_ssh(tmp_path, 'echo "Permission denied (publickey)." >&2\nexit 255\n')
    prof = rt.validate_profile({"id": "a", "ssh_target": "h"})
    with pytest.raises(rt.TunnelError) as err:
        rt.discover_remote_port(prof, ssh_path=ssh)
    assert err.value.state == "ssh_failed"
    assert "ssh h true" in err.value.hint


@pytest.mark.serial
@_POSIX_ONLY
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
        # process; _terminate_quietly then falls through to .kill()+.wait().
        self.pid = -1
        self.stderr = None
        self._dead = False

    def poll(self):
        return 1 if self._dead else None

    def kill(self):
        self._dead = True

    # terminate() (group TERM) does NOT kill this fake — it models an ssh child
    # that outlives TERM, so _terminate_quietly must escalate to kill()+wait().
    def terminate(self):
        pass

    def wait(self, timeout=None):
        if self._dead:
            return 1
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=timeout)


def _manager(tmp_path, **kwargs) -> rt.RemoteTunnelManager:
    return rt.RemoteTunnelManager(tmp_path, ssh_path="/usr/bin/ssh", **kwargs)


def _drain_events(mgr):
    """Block until the off-lock notifier thread has delivered every emitted
    state (the callback runs on a dedicated thread, never under the lock)."""
    mgr._emit_q.join()


def test_connect_success_and_replace_on_connect(tmp_path, monkeypatch):
    events = []
    mgr = _manager(tmp_path, on_state_change=lambda s: events.append(s["state"]))
    fake = _FakeProc()
    monkeypatch.setattr(rt, "discover_remote_port", lambda p, ssh_path, **_k: 8765)
    monkeypatch.setattr(
        rt.RemoteTunnelManager,
        "_spawn_and_wait",
        lambda self, gen, prof, lp, rp: rt._Live(gen, prof, lp, rp, fake),
    )
    # Neutralize the SUPERVISOR thread only (the notifier thread was already
    # started in __init__, before this patch, so events still flow).
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
    _drain_events(mgr)
    assert "connecting" in events and "connected" in events


def test_connect_failure_sets_typed_error_status(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)

    def _boom(profile, ssh_path, **_k):
        raise rt.TunnelError("server_inactive", "down", hint="start it")

    monkeypatch.setattr(rt, "discover_remote_port", _boom)
    with pytest.raises(rt.TunnelError):
        mgr.connect({"id": "a", "ssh_target": "u@h"})
    status = mgr.status()
    assert status["state"] == "error"
    assert status["error_state"] == "server_inactive"
    assert status["hint"] == "start it"


def _install_live(mgr, *, generation=0, local_port=50000, remote_port=8765, proc=None):
    live = rt._Live(
        generation,
        rt.validate_profile({"id": "a", "name": "prod", "ssh_target": "h"}),
        local_port,
        remote_port,
        proc or _FakeProc(),
    )
    with mgr._lock:
        mgr._generation = generation
        mgr._live = live
    return live


def test_reconnect_once_gives_up_and_reports(tmp_path, monkeypatch):
    events = []
    mgr = _manager(tmp_path, on_state_change=lambda s: events.append(dict(s)))
    fake = _FakeProc()
    monkeypatch.setattr(rt, "RECONNECT_TOTAL_SEC", 0.05)
    monkeypatch.setattr(rt, "RECONNECT_BACKOFF_SEC", (0.01,))
    monkeypatch.setattr(
        rt, "discover_remote_port",
        lambda profile, ssh_path, **_k: (_ for _ in ()).throw(rt.TunnelError("ssh_failed", "unreachable")),
    )
    _install_live(mgr, proc=fake)
    assert mgr._reconnect_once(0) is False  # give-up returns False → supervisor stops
    status = mgr.status()
    assert status["state"] == "gave_up"
    assert status["error_state"] == "ssh_failed"
    assert fake._dead is True
    _drain_events(mgr)
    assert any(e["state"] == "reconnecting" for e in events)


def test_reconnect_once_succeeds_and_reuses_stable_local_port(tmp_path, monkeypatch):
    """The whole point of D21: a drop reconnects on the SAME local port so the
    browser origin / sessionStorage survives, and returns True to keep watching."""
    events = []
    mgr = _manager(tmp_path, on_state_change=lambda s: events.append(dict(s)))
    monkeypatch.setattr(rt, "discover_remote_port", lambda profile, ssh_path, **_k: 8765)
    new_proc = _FakeProc()
    monkeypatch.setattr(
        rt.RemoteTunnelManager, "_spawn_and_wait",
        lambda self, gen, prof, lp, rp: rt._Live(gen, prof, lp, rp, new_proc),
    )
    # pick_local_port must NOT be called on the happy path (stable port reused).
    monkeypatch.setattr(rt, "pick_local_port", lambda: (_ for _ in ()).throw(AssertionError("re-picked")))
    _install_live(mgr, local_port=50123)
    assert mgr._reconnect_once(0) is True  # success → supervisor keeps watching
    status = mgr.status()
    assert status["state"] == "connected"
    assert status["reconnected"] is True
    assert status["local_port"] == 50123
    assert mgr._live.proc is new_proc


def test_reconnect_once_repicks_local_port_once_on_bind_conflict(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)
    monkeypatch.setattr(rt, "discover_remote_port", lambda profile, ssh_path, **_k: 8765)
    picks = iter([50999])
    monkeypatch.setattr(rt, "pick_local_port", lambda: next(picks))
    calls = {"n": 0}

    def _spawn(self, gen, prof, lp, rp):
        calls["n"] += 1
        if calls["n"] == 1:
            assert lp == 50000  # first tries the stable port
            raise rt.TunnelError("bind_conflict", "listener busy")
        assert lp == 50999  # second uses the re-picked port
        return rt._Live(gen, prof, lp, rp, _FakeProc())

    monkeypatch.setattr(rt.RemoteTunnelManager, "_spawn_and_wait", _spawn)
    _install_live(mgr, local_port=50000)
    assert mgr._reconnect_once(0) is True
    assert calls["n"] == 2
    assert mgr.status()["local_port"] == 50999


def test_watch_returns_true_on_sustained_health_failure(tmp_path, monkeypatch):
    """ssh-alive != destination-alive: a live ssh proc whose /api/health fails
    past the threshold must trigger reconnect (the ExitOnForwardFailure gap)."""
    mgr = _manager(tmp_path)
    monkeypatch.setattr(rt, "HEALTH_POLL_INTERVAL_SEC", 0.001)
    monkeypatch.setattr(rt, "HEALTH_FAIL_THRESHOLD", 3)
    monkeypatch.setattr(rt, "check_health", lambda port, **k: False)
    _install_live(mgr, proc=_FakeProc())  # proc.poll() is None (alive)
    assert mgr._watch_until_unhealthy(0) is True  # unhealthy → reconnect


def test_force_disconnect_does_not_graceful_wait(tmp_path, monkeypatch):
    # C2 / Emergency Stop: the panic teardown must kill immediately, never take
    # the graceful bounded-wait path (_terminate_quietly).
    mgr = _manager(tmp_path)
    fake = _FakeProc()
    _install_live(mgr, proc=fake)
    calls = {"graceful": 0, "now": 0}
    monkeypatch.setattr(rt, "_terminate_quietly", lambda p: calls.__setitem__("graceful", calls["graceful"] + 1))
    monkeypatch.setattr(rt, "_kill_tree_now", lambda p: (calls.__setitem__("now", calls["now"] + 1), p.kill()))
    mgr.force_disconnect()
    assert calls["now"] == 1 and calls["graceful"] == 0
    assert fake._dead is True
    assert mgr.status()["state"] == "disconnected"


def test_force_disconnect_kills_inflight_tunnel_before_live_assigned(tmp_path, monkeypatch):
    # C1 (round 4 / Emergency Stop): during connect() the tunnel ssh is spawned
    # and health-waited BEFORE self._live is assigned. A panic in that window
    # must still group-kill it — an untracked ssh -N would outlive os._exit.
    mgr = _manager(tmp_path)
    fake = _FakeProc()
    killed = []
    monkeypatch.setattr(rt, "_kill_tree_now", lambda p: (killed.append(p), p.kill()))
    # Simulate _spawn_and_wait mid-flight: proc registered in custody, _live
    # still None (exactly the connect() health-wait window).
    with mgr._lock:
        mgr._inflight.add(fake)
    assert mgr._live is None
    mgr.force_disconnect()
    assert fake in killed and fake._dead is True
    assert mgr._inflight == set()  # custody cleared
    assert mgr.status()["state"] == "disconnected"


def test_shutdown_latch_refuses_spawn_after_force_disconnect(tmp_path):
    # CR2 (round 6 / Emergency Stop): once force_disconnect fires (panic), the
    # terminal latch must make any spawn racing it refuse BEFORE Popen — so a
    # thread that was about to start ssh never leaves an orphan the just-run
    # force_disconnect could not see. Proven without a real ssh: the latch check
    # precedes Popen in both spawn paths.
    mgr = _manager(tmp_path)
    mgr.force_disconnect()
    assert mgr._shutdown is True
    prof = rt.validate_profile({"id": "a", "name": "prod", "ssh_target": "u@h"})
    with pytest.raises(rt.TunnelError):
        mgr._spawn_and_wait(mgr._generation, prof, 50000, 8765)
    with pytest.raises(rt.TunnelError):
        mgr._run_ssh_tracked(["/usr/bin/true"], timeout=5)
    assert mgr._inflight == set()  # nothing spawned → nothing leaked


def test_spawn_and_wait_registers_then_transfers_custody(tmp_path, monkeypatch):
    # C1: the connect() success path must leave the proc owned by _live and NOT
    # lingering in _inflight (else force_disconnect would kill it twice / a
    # stale handle would be group-killed on a later panic).
    mgr = _manager(tmp_path)
    fake = _FakeProc()
    monkeypatch.setattr(rt, "discover_remote_port", lambda p, ssh_path, **_k: 8765)

    def _spawn(self, gen, prof, lp, rp):
        # Model the real method's custody registration without a real ssh.
        self._register_inflight(fake)
        return rt._Live(gen, prof, lp, rp, fake)

    monkeypatch.setattr(rt.RemoteTunnelManager, "_spawn_and_wait", _spawn)
    monkeypatch.setattr(rt.threading.Thread, "start", lambda self: None)
    mgr.connect({"id": "a", "name": "prod", "ssh_target": "u@h"})
    assert mgr._live is not None and mgr._live.proc is fake
    assert mgr._inflight == set()  # ownership transferred out of _inflight


def test_run_ssh_tracked_rejects_stale_generation(tmp_path):
    # R11C1(a): a discovery sub-spawn bound to a stale generation must refuse
    # BEFORE Popen — so a multi-step discovery (port cat → unit_active fallback)
    # cannot launch its 2nd ssh after a disconnect bumped the generation and
    # cleared _inflight, which would leak past a graceful window close.
    import shutil
    true_bin = shutil.which("true") or "/usr/bin/true"
    mgr = _manager(tmp_path)
    with mgr._lock:
        mgr._generation = 5
    with pytest.raises(rt.TunnelError):
        mgr._run_ssh_tracked([true_bin], timeout=5, generation=4)  # stale → refuse
    assert mgr._inflight == set()  # nothing spawned, nothing to leak
    # The current generation still runs (and the generation-bound runner factory
    # produces a runner that carries it).
    runner = mgr._generation_runner(5)
    result = runner([true_bin], timeout=5)
    assert result.returncode == 0
    assert mgr._inflight == set()


def test_status_payloads_carry_generation_for_navigation_guard(tmp_path, monkeypatch):
    # R11C1(b): every emitted status carries its generation so the launcher can
    # reject a superseded navigation; current_generation tracks connect/disconnect.
    seen = []
    mgr = _manager(tmp_path, on_state_change=lambda s: seen.append(s.get("_generation")))
    g0 = mgr.current_generation
    mgr.disconnect()
    _drain_events(mgr)
    assert mgr.current_generation == g0 + 1
    assert seen and seen[-1] == mgr.current_generation  # payload stamped current
    # status() itself never leaks the internal token.
    assert "_generation" not in mgr.status()


def test_run_ssh_tracked_registers_and_clears_inflight(tmp_path):
    # C1: the discovery runner registers its short-lived ssh under custody for
    # the duration of the call, then clears it. A `true` stand-in proves the
    # set is empty after a normal completion.
    import shutil
    true_bin = shutil.which("true") or "/usr/bin/true"
    mgr = _manager(tmp_path)
    result = mgr._run_ssh_tracked([true_bin], timeout=10)
    assert result.returncode == 0
    assert mgr._inflight == set()


def test_terminate_quietly_reaps_child_before_returning(monkeypatch):
    # C5: teardown must not return until the ssh child is terminal, else a
    # zombie lingers and the stable forwarded port can be reused mid-exit.
    proc = _FakeProc()  # terminate() does NOT kill; only kill()+wait() reaps
    rt._terminate_quietly(proc)
    assert proc.poll() is not None  # provably reaped (kill escalated + waited)


def test_terminate_quietly_escalates_to_group_kill_not_direct_child(tmp_path, monkeypatch):
    # R10C2: a TERM-resistant ProxyCommand/ProxyJump descendant must be reaped by
    # a group/tree SIGKILL, NOT a bare proc.kill() on the direct ssh child. Pin
    # that the graceful teardown escalates through _kill_tree_now after the TERM
    # timeout (the same tree-kill the panic path uses).
    calls = {"tree": 0, "direct": 0}
    proc = _FakeProc()
    monkeypatch.setattr(rt, "TERMINATE_WAIT_SEC", 0.01)
    monkeypatch.setattr(rt, "_kill_tree_now", lambda p: (calls.__setitem__("tree", calls["tree"] + 1), p.kill()))
    orig_kill = proc.kill
    proc.kill = lambda: (calls.__setitem__("direct", calls["direct"] + 1), orig_kill())
    rt._terminate_quietly(proc)
    assert calls["tree"] == 1, "must escalate via the group/tree SIGKILL"
    assert proc.poll() is not None


def test_watch_exits_on_generation_change(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)
    monkeypatch.setattr(rt, "HEALTH_POLL_INTERVAL_SEC", 0.01)
    with mgr._lock:
        mgr._generation = 7  # supervisor was started for an older generation
    started = time.time()
    assert mgr._watch_until_unhealthy(3) is False  # stale → stop, no reconnect
    assert time.time() - started < 1.0


@pytest.mark.serial
@_POSIX_ONLY
def test_reap_orphaned_tunnels_kills_ledgered_leak(tmp_path):
    # C2: a tunnel left ledgered by a crashed launcher (daemon custody, kept by
    # the generation reaper) must be reaped at the next launcher startup by
    # strict fingerprint.
    import subprocess as sp

    from ouroboros import process_custody
    from ouroboros.platform_layer import subprocess_new_group_kwargs

    # C4 (round 4): one command list for BOTH the spawn and the custody record,
    # so the recorded fingerprint is the argv actually launched. record_process
    # anchors on the LIVE cmdline when available (so this test passed before on
    # POSIX), but on a platform where the live cmdline is unreadable the fallback
    # hashes the passed argv — a divergent list there would record a mismatched
    # fingerprint and skip the kill. Keep them identical.
    leak_cmd = ["python3", "-c", "import time; time.sleep(30)"]
    proc = sp.Popen(
        leak_cmd,
        stdin=sp.DEVNULL, stdout=sp.DEVNULL, stderr=sp.DEVNULL,
        **subprocess_new_group_kwargs(),
    )
    try:
        process_custody.record_process(
            tmp_path, pid=proc.pid, cmd=leak_cmd,
            purpose="remote_ssh_tunnel:leaked", scope="daemon",
        )
        # An unrelated daemon entry must survive the purpose-scoped reap.
        process_custody.record_process(
            tmp_path, pid=os.getpid(), cmd=["x"], purpose="companion:x:y", scope="daemon",
        )
        reaped = rt.reap_orphaned_tunnels(tmp_path)
        assert reaped == 1
        # The child is killed via its process group; reap the zombie so the
        # returncode is observable (pid_is_alive alone sees an unreaped zombie).
        proc.wait(timeout=5)
        assert proc.returncode is not None and proc.returncode != 0
        # ledger keeps the unrelated entry, drops the tunnel entry.
        purposes = [e.get("purpose") for e in process_custody._read_ledger(tmp_path)]
        assert "companion:x:y" in purposes
        assert not any(str(p).startswith("remote_ssh_tunnel:") for p in purposes)
    finally:
        try:
            proc.kill()
        except Exception:
            pass


@pytest.mark.serial
@_POSIX_ONLY
def test_reap_purpose_prefix_holds_ledger_lock_during_transaction(tmp_path, monkeypatch):
    # CR4 (round 6): the reap must hold the ledger's append lock across the whole
    # read→match→kill→rewrite, so a concurrent record_process append cannot slip
    # in and be erased. Probe: while the reap is mid-kill, an outside attempt to
    # take the same lock must FAIL (None) — proving appenders are serialized
    # behind the reap and their records survive the rewrite.
    import subprocess as sp

    from ouroboros import process_custody as pc
    from ouroboros.platform_layer import (
        acquire_exclusive_file_lock,
        release_exclusive_file_lock,
        subprocess_new_group_kwargs,
    )
    from ouroboros.utils import jsonl_append_lock_path

    leak_cmd = ["python3", "-c", "import time; time.sleep(30)"]
    proc = sp.Popen(
        leak_cmd, stdin=sp.DEVNULL, stdout=sp.DEVNULL, stderr=sp.DEVNULL,
        **subprocess_new_group_kwargs(),
    )
    try:
        pc.record_process(
            tmp_path, pid=proc.pid, cmd=leak_cmd,
            purpose="remote_ssh_tunnel:leaked", scope="daemon",
        )
        lock_path = jsonl_append_lock_path(pc.ledger_path(tmp_path))
        held = {}
        real_kill = pc.kill_process_group_id

        def _probe_kill(pgid):
            # Non-blocking attempt from "another appender": must fail because the
            # reap holds the ledger lock for the full transaction.
            fd = acquire_exclusive_file_lock(lock_path, timeout_sec=0.1)
            held["locked_during_kill"] = fd is None
            if fd is not None:
                release_exclusive_file_lock(lock_path, fd)
            return real_kill(pgid)

        monkeypatch.setattr(pc, "kill_process_group_id", _probe_kill)
        reaped = pc.reap_purpose_prefix(tmp_path, "remote_ssh_tunnel:")
        proc.wait(timeout=5)
        assert reaped == [proc.pid]
        assert held.get("locked_during_kill") is True
    finally:
        try:
            proc.kill()
        except Exception:
            pass


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


@pytest.mark.serial
@_POSIX_ONLY
def test_stub_subprocess_helpers_do_not_leak(tmp_path):
    # _run_ssh must not hang on a stub that ignores stdin and exits.
    ssh = _write_stub_ssh(tmp_path, "exit 0\n")
    proc = rt._run_ssh([ssh, "arg"], timeout=10)
    assert isinstance(proc, subprocess.CompletedProcess)
