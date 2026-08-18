"""Launcher-owned reaping of same-install stray server processes.

No test in this file signals a real process: `kill_pid_tree` is always spied and
process enumeration is always faked.
"""

import inspect
import logging
import os
import sys
import types

import pytest

from ouroboros import launcher_server_reaper as reaper
from ouroboros import process_containment as containment


REPO = "/opt/Ouroboros/repo"
DATA = "/opt/Ouroboros/data"
OURS = f"/opt/Ouroboros/python/bin/python3 {REPO}/server.py"


def _install_fakes(monkeypatch, pids, commands, env_states, groups=None):
    """Fake the three live-state readers the finder uses.

    ``env_states`` maps pid -> {(key, value): state}; a missing entry answers
    ABSENT, which (like UNREADABLE) is not a proof.
    """
    monkeypatch.setattr(
        reaper, "_candidate_pids", lambda: list(pids),
    )
    monkeypatch.setattr(
        "ouroboros.platform_layer.process_command",
        lambda pid: commands.get(pid, ""),
    )
    monkeypatch.setattr(
        "ouroboros.platform_layer.process_group_id",
        lambda pid: (groups or {}).get(pid, pid),
    )
    monkeypatch.setattr(
        reaper, "pid_environment_assignment_state",
        lambda pid, key, value: env_states.get(pid, {}).get(
            (key, value), containment.ENV_ASSIGNMENT_ABSENT
        ),
    )
    monkeypatch.setattr("ouroboros.platform_layer.IS_WINDOWS", False)
    # Pin self/parent identity: the finder folds the REAL getpid/getppid into
    # its exclusion set, and a fake pid colliding with the test runner's own
    # pid would silently vanish from the candidate list.
    monkeypatch.setattr(reaper.os, "getpid", lambda: 999900001)
    monkeypatch.setattr(reaper.os, "getppid", lambda: 999900002)


def _proof(data_dir=DATA):
    return {
        (reaper.MANAGED_MARKER_ENV, reaper.MANAGED_MARKER_VALUE): (
            containment.ENV_ASSIGNMENT_PRESENT
        ),
        (reaper.DATA_DIR_ENV, data_dir): containment.ENV_ASSIGNMENT_PRESENT,
    }


# ---------------------------------------------------------------------------
# Finder: what counts as proof
# ---------------------------------------------------------------------------

def test_both_env_assignments_are_required_before_a_pid_is_proven(monkeypatch):
    """The marker alone, or the data dir alone, is not a licence to kill: a
    direct run of this checkout that exported our data dir is still not managed."""
    _install_fakes(
        monkeypatch,
        pids=[101, 102, 103],
        commands={101: OURS, 102: OURS, 103: OURS},
        env_states={
            101: _proof(),
            102: {(reaper.DATA_DIR_ENV, DATA): containment.ENV_ASSIGNMENT_PRESENT},
            103: {
                (reaper.MANAGED_MARKER_ENV, reaper.MANAGED_MARKER_VALUE): (
                    containment.ENV_ASSIGNMENT_PRESENT
                )
            },
        },
    )
    proven, unproven = reaper.find_same_install_server_pids(REPO, DATA)
    assert proven == [101]
    assert sorted(unproven) == [102, 103]


def test_an_unreadable_environment_is_spared_not_killed(monkeypatch):
    """macOS `ps -E` omits an environment it may not read, and /proc returns
    EACCES for a nondumpable process. Unanswered is never answered-yes."""
    _install_fakes(
        monkeypatch,
        pids=[201],
        commands={201: OURS},
        env_states={
            201: {
                (reaper.MANAGED_MARKER_ENV, reaper.MANAGED_MARKER_VALUE): (
                    containment.ENV_ASSIGNMENT_UNREADABLE
                ),
                (reaper.DATA_DIR_ENV, DATA): containment.ENV_ASSIGNMENT_UNREADABLE,
            }
        },
    )
    proven, unproven = reaper.find_same_install_server_pids(REPO, DATA)
    assert proven == [] and unproven == [201]


def test_a_different_data_directory_is_not_our_generation(monkeypatch):
    _install_fakes(
        monkeypatch,
        pids=[301],
        commands={301: OURS},
        env_states={301: _proof(data_dir="/opt/Ouroboros/other-data")},
    )
    proven, unproven = reaper.find_same_install_server_pids(REPO, DATA)
    assert proven == [] and unproven == [301]


def test_sibling_installs_and_dev_clones_are_never_enumerated(monkeypatch):
    """A second packaged install and a development checkout run a DIFFERENT
    server.py path, so they are not candidates however their env reads."""
    sibling = "/Users/o/Ouroboros/python/bin/python3 /Users/o/Ouroboros/repo/server.py"
    dev = "/usr/bin/python3 /home/dev/src/ouroboros/server.py"
    _install_fakes(
        monkeypatch,
        pids=[401, 402],
        commands={401: sibling, 402: dev},
        env_states={401: _proof(), 402: _proof()},
    )
    proven, unproven = reaper.find_same_install_server_pids(REPO, DATA)
    assert proven == [] and unproven == []


def test_self_parent_caller_exclusions_and_known_groups_are_skipped(monkeypatch):
    """The launcher, its parent, an explicitly excluded pid and anything sharing
    a known process group are part of a tree we already account for."""
    # The stubbed identities _install_fakes pins (not this test runner's own).
    me, parent = 999900001, 999900002
    _install_fakes(
        monkeypatch,
        pids=[me, parent, 501, 502, 503],
        commands={pid: OURS for pid in (me, parent, 501, 502, 503)},
        env_states={pid: _proof() for pid in (me, parent, 501, 502, 503)},
        # 502 shares the caller-excluded pid's group; 503 stands alone.
        groups={me: me, parent: parent, 501: 501, 502: 501, 503: 503},
    )
    proven, unproven = reaper.find_same_install_server_pids(
        REPO, DATA, exclude_pids=[501],
    )
    assert proven == [503] and unproven == []


def test_candidate_enumeration_scopes_pgrep_to_the_current_user(monkeypatch):
    """-U keeps another account's legitimate install out of the sweep; -fi keeps
    the capital-O packaged path in it."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return types.SimpleNamespace(stdout="777\nnot-a-pid\n-1\n", returncode=0)

    monkeypatch.setattr(reaper.subprocess, "run", fake_run)
    assert reaper._candidate_pids() == [777]
    assert seen["cmd"] == ["pgrep", "-U", str(os.getuid()), "-fi", "ouroboros"]


def test_a_missing_pgrep_enumerates_nothing(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr(reaper.subprocess, "run", fake_run)
    assert reaper._candidate_pids() == []


def test_the_finder_never_reads_the_custody_ledger():
    """Missing ledger entries are the defect being repaired, so consulting the
    ledger would spare exactly the strays that matter."""
    source = inspect.getsource(reaper)
    assert "process_ledger" not in source
    assert "process_custody" not in source


# ---------------------------------------------------------------------------
# Reap: kill mechanics
# ---------------------------------------------------------------------------

def _spy_kill(monkeypatch):
    killed = []
    monkeypatch.setattr(
        "ouroboros.platform_layer.kill_pid_tree", lambda pid, **kw: killed.append(pid),
    )
    # Confirmed-dead follows the signal: liveness is answered from the spy so
    # no test outcome depends on which real pids exist on this machine.
    monkeypatch.setattr(
        "ouroboros.platform_layer.pid_is_alive", lambda pid: pid not in killed,
    )
    monkeypatch.setattr(reaper, "_SETTLE_SEC", 0)
    monkeypatch.setattr(reaper, "_CONFIRM_DEADLINE_SEC", 0)
    return killed


def test_proven_strays_are_tree_killed_and_unproven_ones_are_spared(monkeypatch, caplog):
    killed = _spy_kill(monkeypatch)
    _install_fakes(
        monkeypatch,
        pids=[601, 602],
        commands={601: OURS, 602: OURS},
        env_states={601: _proof()},
    )
    with caplog.at_level(logging.WARNING, logger=reaper.log.name):
        survivors = reaper.reap_same_install_strays(REPO, DATA, "startup")
    # Three bounded passes, each re-proving the pid that never dies in this fake.
    assert set(killed) == {601}
    assert killed.count(601) == reaper.REAP_PASSES
    assert survivors == [601]
    assert 602 not in killed
    assert any("602" in record.getMessage() for record in caplog.records)


def test_a_pid_that_dies_between_proof_and_signal_is_not_killed(monkeypatch):
    """Revalidation happens immediately before the signal; a pid whose command
    line no longer matches may have been recycled onto a stranger."""
    killed = _spy_kill(monkeypatch)
    commands = {701: OURS}
    _install_fakes(
        monkeypatch, pids=[701], commands=commands, env_states={701: _proof()},
    )

    real_revalidate = reaper._revalidate_and_kill

    def vanishing(pid, server_paths, data_dir_values):
        commands[pid] = ""  # exited between the scan and the signal
        return real_revalidate(pid, server_paths, data_dir_values)

    monkeypatch.setattr(reaper, "_revalidate_and_kill", vanishing)
    assert reaper.reap_same_install_strays(REPO, DATA) == []
    assert killed == []


def test_a_fork_between_passes_is_caught_by_the_rescan(monkeypatch):
    """A stray forking mid-sweep hands its child the same cmdline and the same
    inherited environment, so the child is proven on the next pass."""
    killed = _spy_kill(monkeypatch)
    live = {801: OURS}
    monkeypatch.setattr(reaper, "_candidate_pids", lambda: sorted(live))
    monkeypatch.setattr(
        "ouroboros.platform_layer.process_command", lambda pid: live.get(pid, ""),
    )
    monkeypatch.setattr("ouroboros.platform_layer.process_group_id", lambda pid: pid)
    monkeypatch.setattr(
        reaper, "pid_environment_assignment_state",
        lambda pid, key, value: (
            containment.ENV_ASSIGNMENT_PRESENT if pid in live
            else containment.ENV_ASSIGNMENT_ABSENT
        ),
    )
    monkeypatch.setattr("ouroboros.platform_layer.IS_WINDOWS", False)

    real_kill = reaper._pl.kill_pid_tree

    def forking_kill(pid, **kwargs):
        real_kill(pid)
        live.pop(pid, None)
        if pid == 801:
            live[802] = OURS  # the fork inherits everything

    monkeypatch.setattr("ouroboros.platform_layer.kill_pid_tree", forking_kill)
    survivors = reaper.reap_same_install_strays(REPO, DATA)
    assert killed == [801, 802]
    assert survivors == []


def test_the_sweep_is_bounded_and_reports_survivors(monkeypatch):
    """A pid that refuses to die must not become an unbounded kill loop."""
    killed = _spy_kill(monkeypatch)
    _install_fakes(
        monkeypatch, pids=[901], commands={901: OURS}, env_states={901: _proof()},
    )
    assert reaper.reap_same_install_strays(REPO, DATA) == [901]
    assert len(killed) == reaper.REAP_PASSES


def test_windows_sweeps_nothing(monkeypatch):
    """The kill-on-close Job Object already reaps orphans there."""
    killed = _spy_kill(monkeypatch)
    monkeypatch.setattr("ouroboros.platform_layer.IS_WINDOWS", True)
    assert reaper.reap_same_install_strays(REPO, DATA) == []
    assert reaper.find_same_install_server_pids(REPO, DATA) == ([], [])
    assert killed == []


# ---------------------------------------------------------------------------
# Launcher wiring
# ---------------------------------------------------------------------------

def test_the_sweep_runs_per_generation_between_record_cleanup_and_the_port_sweep(monkeypatch):
    """Behavioural: the per-generation helper really wires the three phases in order
    and hands the sweep's survivors through to the caller."""
    import launcher

    calls: list = []
    monkeypatch.setattr(
        launcher, "_cleanup_recorded_server_process", lambda reason: calls.append(("record", reason))
    )
    monkeypatch.setattr(
        launcher, "_reap_same_install_strays",
        lambda reason: calls.append(("sweep", reason)) or [4242],
    )
    monkeypatch.setattr(
        launcher, "_kill_stale_runtime_ports", lambda port: calls.append(("ports", port))
    )

    survivors = launcher._pre_generation_cleanup(8765)

    assert calls == [("record", "startup"), ("sweep", "startup"), ("ports", 8765)]
    assert survivors == [4242]
    # The lifecycle loop consumes exactly this helper before any start.
    loop_src = inspect.getsource(launcher.agent_lifecycle_loop)
    assert loop_src.index("_pre_generation_cleanup(port)") < loop_src.index("proc = start_agent(port)")


def test_a_proven_survivor_skips_start_agent_for_that_generation(monkeypatch):
    """Behavioural: with a proven survivor reported, the generation must not start —
    the loop logs, waits, and comes back around instead of calling start_agent."""
    import threading

    import launcher

    started: list = []
    shutdown = threading.Event()

    def fake_cleanup(port):
        # First generation reports a survivor; ending the loop here keeps the
        # test to exactly one iteration.
        shutdown.set()
        return [4242]

    monkeypatch.setattr(launcher, "_shutdown_event", shutdown)
    monkeypatch.setattr(launcher, "_pre_generation_cleanup", fake_cleanup)
    monkeypatch.setattr(launcher, "start_agent", lambda port: started.append(port))
    monkeypatch.setattr(launcher.time, "sleep", lambda seconds: None)

    launcher.agent_lifecycle_loop(8765)

    assert started == [], "a proven surviving stray must suppress start_agent"


def test_the_preflight_sweep_follows_the_recorded_process_cleanup():
    import launcher

    src = inspect.getsource(launcher.main)
    order = [
        "acquire_pid_lock()",
        '_cleanup_recorded_server_process("preflight")',
        '_reap_same_install_strays("preflight")',
        "_kill_stale_runtime_ports(port)",
    ]
    positions = [src.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_the_panic_and_window_close_path_performs_no_stray_sweep():
    """BIBLE Emergency Stop: the panic path tears down what it owns and gains no
    new killing."""
    import launcher

    assert "_reap_same_install_strays" not in inspect.getsource(
        launcher._kill_orphaned_children
    )


# ---------------------------------------------------------------------------
# Env-assignment primitive
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX /proc and ps -E semantics")
def test_env_assignment_state_answers_for_our_own_pid():
    """Smoke test against live kernel state: an assignment this process cannot be
    carrying must never come back PRESENT, on either the /proc or the `ps -E` path."""
    state = containment.pid_environment_assignment_state(
        os.getpid(), "OUROBOROS_REAPER_PROBE_UNSET", "unit-test-placeholder",
    )
    assert state in (
        containment.ENV_ASSIGNMENT_ABSENT, containment.ENV_ASSIGNMENT_UNREADABLE,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX /proc and ps -E semantics")
def test_env_assignment_state_never_answers_present_for_an_impossible_pid():
    assert containment.pid_environment_assignment_state(
        -1, "OUROBOROS_REAPER_PROBE_UNSET", "unit-test-placeholder",
    ) != containment.ENV_ASSIGNMENT_PRESENT


def test_env_assignment_state_is_unreadable_on_windows(monkeypatch):
    monkeypatch.setattr("ouroboros.platform_layer.IS_WINDOWS", True)
    assert containment.pid_environment_assignment_state(
        os.getpid(), "PATH", os.environ.get("PATH", ""),
    ) == containment.ENV_ASSIGNMENT_UNREADABLE


def test_a_command_merely_mentioning_the_server_path_is_not_a_candidate(monkeypatch):
    """Substring hits are not identity: an editor or log tool naming the path, or
    a longer filename sharing the prefix, is neither killable nor spared-listed —
    only the launcher's `<python> <repo>/server.py` spawn shape is this
    install's server."""
    _install_fakes(
        monkeypatch,
        pids=[621, 622, 623],
        commands={
            621: f"vim {REPO}/server.py",
            622: f"/opt/Ouroboros/python/bin/python3 {REPO}/server.py.bak",
            623: f"tail -f {REPO}/server.py.log",
        },
        env_states={pid: _proof() for pid in (621, 622, 623)},
    )
    assert reaper.find_same_install_server_pids(REPO, DATA) == ([], [])


def test_a_sweep_aborted_mid_work_reports_survivors_not_clean(monkeypatch, caplog):
    """An exception after a pid was proven must not read as swept-clean: the
    caller would start the exact colliding generation the sweep exists to
    prevent."""
    killed = _spy_kill(monkeypatch)
    _install_fakes(
        monkeypatch, pids=[631], commands={631: OURS}, env_states={631: _proof()},
    )

    def exploding(pid, server_paths, data_dir_values):
        raise RuntimeError("mid-sweep failure")

    monkeypatch.setattr(reaper, "_revalidate_and_kill", exploding)
    with caplog.at_level(logging.WARNING, logger=reaper.log.name):
        assert reaper.reap_same_install_strays(REPO, DATA) == [631]
    assert killed == []
    assert any("aborted mid-work" in r.getMessage() for r in caplog.records)


def test_a_signalled_pid_still_alive_is_a_survivor_not_a_kill(monkeypatch, caplog):
    """kill_pid_tree swallows per-pid errors, so only a liveness read can say
    what the signal achieved; a pid logged as reaped while it survived would
    contradict the survivor report from the same generation."""
    signalled = []
    monkeypatch.setattr(
        "ouroboros.platform_layer.kill_pid_tree", lambda pid, **kw: signalled.append(pid),
    )
    monkeypatch.setattr("ouroboros.platform_layer.pid_is_alive", lambda pid: True)
    monkeypatch.setattr(reaper, "_SETTLE_SEC", 0)
    monkeypatch.setattr(reaper, "_CONFIRM_DEADLINE_SEC", 0)
    _install_fakes(
        monkeypatch, pids=[641], commands={641: OURS}, env_states={641: _proof()},
    )
    with caplog.at_level(logging.INFO, logger=reaper.log.name):
        assert reaper.reap_same_install_strays(REPO, DATA) == [641]
    assert signalled and all(pid == 641 for pid in signalled)
    assert not any("Reaped" in r.getMessage() for r in caplog.records)
