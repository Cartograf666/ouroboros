"""Headless server data-dir lock (SERVER_PID_FILE) and its exit-code contract."""

from __future__ import annotations

import argparse

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
        pid_flock_close(str(path), first)
    # Released — a fresh handle acquires again.
    again = pid_flock_open(str(path))
    assert again is not None
    pid_flock_close(str(path), again)


def test_acquire_server_pid_lock_creates_state_dir_and_is_idempotent(_isolated_server_lock):
    assert config.acquire_server_pid_lock() is True
    assert config.SERVER_PID_FILE.exists()
    # Same process re-acquire is a no-op success (held handle kept).
    assert config.acquire_server_pid_lock() is True
    config.release_server_pid_lock()
    assert not config.SERVER_PID_FILE.exists()
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


def test_server_command_exits_already_running_when_lock_held(monkeypatch, capsys):
    from ouroboros import cli

    monkeypatch.setattr(config, "acquire_server_pid_lock", lambda: False)
    args = argparse.Namespace(host="", port=0)
    code = cli._server_command(args)
    assert code == config.SERVER_ALREADY_RUNNING_EXIT_CODE
    err = capsys.readouterr().err
    assert "another instance already holds" in err


def test_already_running_exit_code_is_distinct():
    assert config.SERVER_ALREADY_RUNNING_EXIT_CODE not in {
        config.RESTART_EXIT_CODE,
        config.PANIC_EXIT_CODE,
        0,
    }


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
