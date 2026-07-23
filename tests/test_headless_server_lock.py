"""Headless server data-dir lock (SERVER_PID_FILE) and its exit-code contract."""

from __future__ import annotations

import os

import pytest

from ouroboros import config, remote_support
from ouroboros.platform_layer import pid_flock_close, pid_flock_open


@pytest.fixture()
def _isolated_server_lock(tmp_path, monkeypatch):
    # SERVER_PID_FILE is read via config.SERVER_PID_FILE inside remote_support,
    # so patching config is what the acquire/release helpers observe.
    monkeypatch.setattr(config, "SERVER_PID_FILE", tmp_path / "state" / "server.pid")
    monkeypatch.setattr(remote_support, "_server_pid_lock_handle", None)
    yield
    config.release_server_pid_lock()


def test_pid_flock_open_is_exclusive_per_open_description(tmp_path):
    path = tmp_path / "server.pid"
    first = pid_flock_open(str(path))
    assert first is not None
    try:
        assert pid_flock_open(str(path)) is None
    finally:
        pid_flock_close(str(path), first)  # default remove=False → file persists
    # The lock FILE persists after release (race-free discipline); a fresh
    # handle re-opens and re-flocks the same inode.
    assert path.exists()
    again = pid_flock_open(str(path))
    assert again is not None
    pid_flock_close(str(path), again)
    assert path.exists()


def test_pid_flock_close_no_unlink_race_release_reacquire(tmp_path):
    # C4: release must NOT unlink — an unlink after unlock lets a second holder
    # take the same inode, then the first's unlink would delete the live lock.
    # With persistent file, release→reacquire keeps the SAME path exclusive.
    path = tmp_path / "server.pid"
    a = pid_flock_open(str(path))
    assert a is not None
    pid_flock_close(str(path), a)  # persistent
    b = pid_flock_open(str(path))
    assert b is not None
    assert pid_flock_open(str(path)) is None  # still exclusive on the same file
    pid_flock_close(str(path), b)


def test_acquire_server_pid_lock_creates_state_dir_and_is_idempotent(_isolated_server_lock):
    assert config.acquire_server_pid_lock() is True
    assert config.SERVER_PID_FILE.exists()
    # Same process re-acquire is a no-op success (held handle kept).
    assert config.acquire_server_pid_lock() is True
    config.release_server_pid_lock()
    # File persists after release (race-free discipline); the lock is free.
    assert config.SERVER_PID_FILE.exists()
    assert config.acquire_server_pid_lock() is True  # re-acquirable
    config.release_server_pid_lock()
    # Release when not held stays a safe no-op.
    config.release_server_pid_lock()


def test_acquire_server_pid_lock_refuses_a_held_lock(_isolated_server_lock):
    config.SERVER_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    foreign = pid_flock_open(str(config.SERVER_PID_FILE))
    assert foreign is not None
    try:
        assert config.acquire_server_pid_lock() is False
    finally:
        pid_flock_close(str(config.SERVER_PID_FILE), foreign)


@pytest.mark.serial
@pytest.mark.skipif(os.name == "nt", reason="POSIX fork semantics")
@pytest.mark.parametrize("drop_in_child,expect_free_after_child_dies", [(False, True), (True, True)])
def test_forked_child_inherited_lock_fd_semantics(tmp_path, monkeypatch, drop_in_child, expect_free_after_child_dies):
    """C3: a Linux fork inherits the flock's open file description, so a child
    that KEEPS its inherited fd holds the lock even after the parent's fd is
    gone; a child that calls close_inherited_server_pid_lock() does not. Proven
    by holding the lock, forking, releasing the PARENT's fd, and checking
    acquirability while the child is alive vs after it exits.
    """
    import time

    monkeypatch.setattr(config, "SERVER_PID_FILE", tmp_path / "state" / "server.pid")
    monkeypatch.setattr(remote_support, "_server_pid_lock_handle", None)
    assert config.acquire_server_pid_lock() is True

    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        try:
            os.close(r)
            if drop_in_child:
                remote_support.close_inherited_server_pid_lock()
            os.write(w, b"x")  # signal ready
            time.sleep(2.0)
        finally:
            os._exit(0)
    # parent
    os.close(w)
    os.read(r, 1)  # wait until child has (optionally) dropped its fd
    # Simulate parent DEATH: close the parent's fd WITHOUT flock LOCK_UN (a crash
    # closes fds but never unlocks). An explicit release_server_pid_lock() would
    # LOCK_UN the shared open file description and free it regardless of the
    # child — the opposite of what a crash does.
    _handle = remote_support._server_pid_lock_handle
    os.close(_handle.fileno())
    remote_support._server_pid_lock_handle = None
    # While the child is still alive: free only if the child dropped its copy.
    h1 = pid_flock_open(str(config.SERVER_PID_FILE))
    free_while_child_alive = h1 is not None
    if h1 is not None:
        pid_flock_close(str(config.SERVER_PID_FILE), h1)
    os.waitpid(pid, 0)  # child exits → its fd closes too
    h2 = pid_flock_open(str(config.SERVER_PID_FILE))
    free_after_child_dies = h2 is not None
    if h2 is not None:
        pid_flock_close(str(config.SERVER_PID_FILE), h2)
    os.close(r)
    # A child that dropped its fd frees the lock immediately; one that kept it
    # blocks until it dies — the property that keeps a crashed server from
    # trapping its replacement on exit 43.
    assert free_while_child_alive == drop_in_child
    assert free_after_child_dies == expect_free_after_child_dies


def test_server_main_exits_already_running_when_lock_held(monkeypatch):
    # The one-server lock now lives at the universal boundary server.main(), so a
    # held lock returns exit 43 there (before binding a port), covering every
    # entry path (headless CLI, launcher-managed server.py, direct run).
    import server
    from types import SimpleNamespace

    monkeypatch.setattr(server, "load_settings", lambda: {"OUROBOROS_SERVER_HOST": "127.0.0.1"})
    monkeypatch.setattr(server, "parse_server_args", lambda *_a, **_k: SimpleNamespace(host="127.0.0.1", port=8765))
    monkeypatch.setattr(server, "get_network_auth_startup_warning", lambda _host: "")
    monkeypatch.setattr(server, "validate_network_auth_configuration", lambda _host: "")
    monkeypatch.setattr(server, "acquire_server_pid_lock", lambda: False)  # lock held
    # find_free_port must NOT be reached; make it loud if it is.
    monkeypatch.setattr(server, "find_free_port", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("reached bind")))
    assert server.main() == server.SERVER_ALREADY_RUNNING_EXIT_CODE


def test_already_running_exit_code_is_distinct():
    assert config.SERVER_ALREADY_RUNNING_EXIT_CODE not in {
        config.RESTART_EXIT_CODE,
        config.PANIC_EXIT_CODE,
        0,
    }


def test_launcher_lifecycle_loop_stops_on_already_running_exit():
    # A2 (R3 round-5): server.main() now acquires the one-server lock at the
    # universal boundary, so a launcher-managed server can return exit 43 when a
    # headless server already owns the data dir. The launcher must stop cleanly
    # on 43 (break) rather than burn the crash budget (5 respawns) on a race it
    # cannot win — pinned by source so a refactor can't silently drop the branch.
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / "launcher.py").read_text(encoding="utf-8")
    loop_start = src.index("def agent_lifecycle_loop")
    loop = src[loop_start:]
    branch = loop.index("if exit_code == SERVER_ALREADY_RUNNING_EXIT_CODE:")
    # The 43 branch must appear BEFORE the crash-counter give-up, and break.
    crash_giveup = loop.index('log.error("Agent crashed %d times')
    assert branch < crash_giveup, "exit-43 branch must precede the crash-count give-up"
    # The branch runs up to the restart section's `time.sleep(2)`.
    body = loop[branch : loop.index("time.sleep(2)", branch)]
    assert "break" in body and "Not restarting" in body
    # CR1: exit 43 means another LIVE server legitimately owns this data dir —
    # the branch must NOT sweep the port (that would kill the valid owner).
    assert "_kill_stale_runtime_ports" not in body
    # R7C2: the branch must show the owner an actionable page, not strand the
    # window on a dead 'Connecting…' view.
    assert "_present_server_conflict_page()" in body


def test_server_conflict_page_is_shown_and_actionable(monkeypatch):
    # R7C2 behavioral: the conflict presenter replaces the window content with
    # recovery instructions (load_html), and degrades safely with no window.
    import launcher

    shown = []

    class _FakeWindow:
        def load_html(self, html):
            shown.append(html)

    monkeypatch.setattr(launcher, "_webview_window", _FakeWindow())
    assert launcher._present_server_conflict_page() is True
    assert len(shown) == 1
    page = shown[0]
    # Actionable content: names the conflict, the stop command, and the remote path.
    assert "owns this data" in page
    assert "systemctl --user stop ouroboros" in page
    assert "Remote" in page
    # No window (headless/early startup): a safe no-op, not a crash.
    monkeypatch.setattr(launcher, "_webview_window", None)
    assert launcher._present_server_conflict_page() is False

    class _Boom:
        def load_html(self, html):
            raise RuntimeError("nope")

    monkeypatch.setattr(launcher, "_webview_window", _Boom())
    assert launcher._present_server_conflict_page() is False  # swallowed, logged


def test_systemd_unit_prevents_panic_and_conflict_restarts():
    import pathlib

    here = pathlib.Path(__file__).resolve().parents[1]
    unit = here / "packaging" / "systemd" / "ouroboros.service"
    text = unit.read_text(encoding="utf-8")
    prevent_line = next(
        line for line in text.splitlines() if line.startswith("RestartPreventExitStatus=")
    )
    codes = set(prevent_line.split("=", 1)[1].split())
    assert str(config.PANIC_EXIT_CODE) in codes
    assert str(config.SERVER_ALREADY_RUNNING_EXIT_CODE) in codes
    assert "Restart=on-failure" in text
    assert "--host 127.0.0.1" in text
    # R7C1: without an explicit app root the server would import code from
    # ~/ouroboros-server/repo while config paths (data, self-repo git and
    # self-modification flows) defaulted to ~/Ouroboros/* — a different,
    # possibly desktop, checkout. The unit must pin the app root to the
    # documented layout.
    assert "Environment=OUROBOROS_APP_ROOT=%h/ouroboros-server" in text
