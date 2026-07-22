"""Owner-only OUROBOROS_REMOTE_CONNECTIONS storage contract.

The profiles are launcher-owned owner state: shape-coerced like MCP_SERVERS,
omitted from GET /api/settings entirely, merge-skipped on generic POST, and
written only through the locked read-modify-write helper.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros import config
from ouroboros.config import _coerce_setting_value, update_remote_connections
from ouroboros.gateway.settings import _merge_settings_payload

_KEY = "OUROBOROS_REMOTE_CONNECTIONS"


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


def test_update_remote_connections_locked_rmw_preserves_other_keys(tmp_path, monkeypatch):
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


def test_update_remote_connections_preserves_0600_permissions(tmp_path, monkeypatch):
    import os
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


def test_update_remote_connections_refuses_to_clobber_corrupt_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_SETTINGS_LOCK", tmp_path / "settings.json.lock")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # An existing but unparseable file must NOT be silently overwritten with a
    # single-key doc — that would destroy every other key the owner could fix.
    (tmp_path / "settings.json").write_text("{ this is not json", encoding="utf-8")
    import pytest

    with pytest.raises(RuntimeError, match="not readable/parseable"):
        update_remote_connections([{"id": "x", "ssh_target": "u@h"}])
    # The corrupt file is left intact for the owner to repair.
    assert (tmp_path / "settings.json").read_text(encoding="utf-8") == "{ this is not json"
    assert not (tmp_path / "settings.json.lock").exists()


def test_update_remote_connections_creates_settings_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "_SETTINGS_LOCK", tmp_path / "settings.json.lock")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    written = update_remote_connections([{"id": "x", "ssh_target": "u@h"}])
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))[_KEY] == written


def test_env_allowlist_never_propagates_remote_connections(monkeypatch):
    import os

    monkeypatch.delenv(_KEY, raising=False)
    config.apply_settings_to_env({_KEY: [{"id": "a"}], "OUROBOROS_MODEL": "m"})
    assert _KEY not in os.environ
