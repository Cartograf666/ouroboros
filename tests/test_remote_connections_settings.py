"""Owner-only OUROBOROS_REMOTE_CONNECTIONS storage contract.

The profiles are launcher-owned owner state: shape-coerced like MCP_SERVERS,
omitted from GET /api/settings entirely, merge-skipped on generic POST, and
written only through the locked read-modify-write helper.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros import config
from ouroboros.config import _coerce_setting_value, update_remote_connections
from ouroboros.gateway.settings import _merge_settings_payload

_KEY = "OUROBOROS_REMOTE_CONNECTIONS"


@pytest.fixture()
def _launcher_identity(tmp_path, monkeypatch):
    """Grant this test process the launcher's OS-anchored write authority.

    update_remote_connections refuses outside the desktop launcher process
    (CR3/R12C1: authority = holding the exclusive PID lock ON THE CANONICAL
    config.PID_FILE, platform_layer.pid_lock_held(PID_FILE)). Tests simulate the
    launcher the REAL way — point PID_FILE at an isolated path and acquire the
    global lock on THAT exact path — then release it afterwards.
    """
    from ouroboros import config, platform_layer

    pid_file = tmp_path / "ouroboros.pid"
    monkeypatch.setattr(config, "PID_FILE", pid_file)
    assert platform_layer.pid_lock_acquire(str(pid_file))
    try:
        yield
    finally:
        platform_layer.pid_lock_release(str(pid_file))


def test_coercion_accepts_list_of_dicts_and_json_string():
    profiles = [{"id": "a", "name": "prod", "ssh_target": "user@host"}]
    assert _coerce_setting_value(_KEY, profiles) == profiles
    assert _coerce_setting_value(_KEY, json.dumps(profiles)) == profiles


def test_coercion_rejects_garbage_and_bounds_count():
    assert _coerce_setting_value(_KEY, "not json") == []
    assert _coerce_setting_value(_KEY, {"id": "a"}) == []
    assert _coerce_setting_value(_KEY, None) == []
    assert _coerce_setting_value(_KEY, ["str", 42, {"id": "ok"}]) == [{"id": "ok"}]
    over = [{"id": str(i)} for i in range(config.REMOTE_CONNECTIONS_MAX + 10)]
    assert len(_coerce_setting_value(_KEY, over)) == config.REMOTE_CONNECTIONS_MAX


def test_generic_settings_merge_skips_remote_connections():
    current = {_KEY: [{"id": "mine", "ssh_target": "me@box"}], "TOTAL_BUDGET": 10.0}
    body = {_KEY: [{"id": "evil", "ssh_target": "attacker@evil"}], "TOTAL_BUDGET": 20.0}
    merged = _merge_settings_payload(current, body)
    assert merged[_KEY] == current[_KEY]
    assert merged["TOTAL_BUDGET"] == 20.0


def test_settings_get_omits_remote_connections(tmp_path, monkeypatch):
    import server as srv

    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_SETTINGS_LOCK", tmp_path / "settings.json.lock")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "settings.json").write_text(
        json.dumps({_KEY: [{"id": "a", "ssh_target": "user@host"}], "TOTAL_BUDGET": 5}),
        encoding="utf-8",
    )
    with patch(
        "ouroboros.gateway.settings.apply_runtime_provider_defaults",
        lambda s: (s, False, []),
    ):
        app = Starlette(routes=[
            Route("/api/settings", endpoint=srv.api_settings_get, methods=["GET"]),
        ])
        app.state.drive_root = tmp_path
        app.state.repo_dir = tmp_path
        payload = TestClient(app).get("/api/settings").json()
    assert _KEY not in payload


def test_update_remote_connections_locked_rmw_preserves_other_keys(tmp_path, monkeypatch, _launcher_identity):
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_SETTINGS_LOCK", tmp_path / "settings.json.lock")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "settings.json").write_text(
        json.dumps({"TOTAL_BUDGET": 33.0, "OPENROUTER_API_KEY": "sk-keep"}),
        encoding="utf-8",
    )
    written = update_remote_connections([
        {"id": "a", "name": "prod", "ssh_target": "user@host"},
        "garbage-entry",
    ])
    assert written == [{"id": "a", "name": "prod", "ssh_target": "user@host"}]
    on_disk = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert on_disk[_KEY] == written
    assert on_disk["TOTAL_BUDGET"] == 33.0
    assert on_disk["OPENROUTER_API_KEY"] == "sk-keep"
    # Lockfile must not linger.
    assert not (tmp_path / "settings.json.lock").exists()


def test_update_remote_connections_preserves_0600_permissions(tmp_path, monkeypatch, _launcher_identity):
    import stat

    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_SETTINGS_LOCK", tmp_path / "settings.json.lock")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"OPENROUTER_API_KEY": "sk-secret"}), encoding="utf-8")
    os.chmod(p, 0o600)
    update_remote_connections([{"id": "a", "ssh_target": "u@h"}])
    mode = stat.S_IMODE(os.stat(p).st_mode)
    # The secret-bearing settings file must stay 0600 (not relaxed to 0644).
    assert mode == 0o600, oct(mode)
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["OPENROUTER_API_KEY"] == "sk-secret"
    assert on_disk[_KEY] == [{"id": "a", "ssh_target": "u@h"}]


def test_update_remote_connections_refuses_to_clobber_corrupt_settings(tmp_path, monkeypatch, _launcher_identity):
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_SETTINGS_LOCK", tmp_path / "settings.json.lock")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # An existing but unparseable file must NOT be silently overwritten with a
    # single-key doc — that would destroy every other key the owner could fix.
    (tmp_path / "settings.json").write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not readable/parseable"):
        update_remote_connections([{"id": "x", "ssh_target": "u@h"}])
    # The corrupt file is left intact for the owner to repair.
    assert (tmp_path / "settings.json").read_text(encoding="utf-8") == "{ this is not json"
    assert not (tmp_path / "settings.json.lock").exists()


def test_update_remote_connections_creates_settings_file(tmp_path, monkeypatch, _launcher_identity):
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_SETTINGS_LOCK", tmp_path / "settings.json.lock")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    written = update_remote_connections([{"id": "x", "ssh_target": "u@h"}])
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))[_KEY] == written


def test_update_remote_connections_refuses_outside_launcher_process(tmp_path, monkeypatch):
    """CR3 (D13 structural): the writer must refuse in any process that does NOT
    hold the launcher's exclusive PID lock — an agent run_command/run_script
    interpreter calling it directly (however the function name was reached)
    hits this wall, not just the shell-text detector. Forging is out of reach:
    while the launcher lives its flock is exclusive, and pid-file CONTENT is
    deliberately not consulted (an advisory lock does not stop writes)."""
    from ouroboros import platform_layer

    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_SETTINGS_LOCK", tmp_path / "settings.json.lock")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # This test process does not hold the global pid lock (agent topology).
    monkeypatch.setattr(platform_layer, "_lock_fd", None)
    monkeypatch.setattr(platform_layer, "_lock_path", "")
    pid_file = tmp_path / "ouroboros.pid"
    monkeypatch.setattr(config, "PID_FILE", pid_file)
    with pytest.raises(RuntimeError, match="owner-only"):
        update_remote_connections([{"id": "x", "ssh_target": "u@h"}])
    # Forged pid-file content must NOT grant authority (the lock, not the file
    # content, is the identity).
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    with pytest.raises(RuntimeError, match="owner-only"):
        update_remote_connections([{"id": "x", "ssh_target": "u@h"}])
    # R12C1: holding the lock on an ARBITRARY OTHER path must NOT grant authority
    # — a worker could pid_lock_acquire('/tmp/x') otherwise. Authority requires
    # the lock be held on the CANONICAL PID_FILE (which the launcher holds
    # exclusively, so a child can never acquire it).
    other = tmp_path / "not-the-pid-file.lock"
    assert platform_layer.pid_lock_acquire(str(other))
    try:
        with pytest.raises(RuntimeError, match="owner-only"):
            update_remote_connections([{"id": "x", "ssh_target": "u@h"}])
    finally:
        platform_layer.pid_lock_release(str(other))
    # Nothing was written in any refusal path.
    assert not (tmp_path / "settings.json").exists()


def test_generic_saves_never_clobber_concurrent_profile_write(tmp_path, monkeypatch):
    """R7 scope advisory: a generic settings save carries a profile list loaded
    BEFORE the settings lock; if the launcher writes profiles in between, the
    save must carry the DISK value (re-read under the lock), not the stale one.
    Covers both writers: gateway._owner_write_settings and config.save_settings."""
    from ouroboros.gateway.settings import _owner_write_settings

    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_SETTINGS_LOCK", tmp_path / "settings.json.lock")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    fresh = [{"id": "fresh", "ssh_target": "owner@new"}]
    stale = [{"id": "stale", "ssh_target": "old@old"}]
    # Disk state = the launcher's just-landed write.
    (tmp_path / "settings.json").write_text(
        json.dumps({_KEY: fresh, "TOTAL_BUDGET": 1.0}), encoding="utf-8"
    )
    # A generic save arrives carrying the pre-lock (stale) list.
    _owner_write_settings({_KEY: stale, "TOTAL_BUDGET": 2.0})
    on_disk = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert on_disk[_KEY] == fresh  # launcher write preserved
    assert on_disk["TOTAL_BUDGET"] == 2.0  # the generic save itself landed
    # Same guarantee through config.save_settings.
    (tmp_path / "settings.json").write_text(
        json.dumps({_KEY: fresh, "TOTAL_BUDGET": 3.0}), encoding="utf-8"
    )
    config.save_settings({_KEY: stale, "TOTAL_BUDGET": 4.0})
    on_disk = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert on_disk[_KEY] == fresh
    assert on_disk["TOTAL_BUDGET"] == 4.0


def test_generic_save_cannot_seed_profiles_when_disk_lacks_key(tmp_path, monkeypatch):
    """R12C2: on a fresh/upgraded install whose settings lack the profile key, a
    non-launcher generic save must NOT be able to seed profiles — the caller's
    value is discarded and the empty default written, so update_remote_connections
    (launcher-gated) stays the ONLY writer that can set profiles."""
    from ouroboros.gateway.settings import _owner_write_settings

    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_SETTINGS_LOCK", tmp_path / "settings.json.lock")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # Disk has NO remote-connections key (fresh install); NOT the launcher.
    (tmp_path / "settings.json").write_text(json.dumps({"TOTAL_BUDGET": 1.0}), encoding="utf-8")
    evil = [{"id": "evil", "ssh_target": "attacker@evil"}]
    _owner_write_settings({_KEY: evil, "TOTAL_BUDGET": 2.0})
    on_disk = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert on_disk[_KEY] == []  # seeded value discarded → empty default
    assert on_disk["TOTAL_BUDGET"] == 2.0
    # Same guarantee through config.save_settings.
    config.save_settings({_KEY: evil, "TOTAL_BUDGET": 3.0})
    on_disk = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert on_disk[_KEY] == []


def test_env_allowlist_never_propagates_remote_connections(monkeypatch):
    monkeypatch.delenv(_KEY, raising=False)
    config.apply_settings_to_env({_KEY: [{"id": "a"}], "OUROBOROS_MODEL": "m"})
    assert _KEY not in os.environ
