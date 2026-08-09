"""The owner settings WRITE seam: the lock is a precondition, and "saved" is a fact.

Two failure classes, both proved against a REAL settings file rather than a mock:

* the settings lock timing out used to be IGNORED — ``_acquire_settings_lock``
  answers ``None`` and the write ran anyway, so a contended save was the one save
  that skipped the precondition it advertises and raced another writer;
* a failure AFTER the bytes landed used to be reported as a failed save (``400``
  from the generic endpoint, ``saved=False`` from onboarding), sending the owner
  to re-do a save that is already on disk.

The onboarding endpoint's own coverage lives in
``test_onboarding_complete_endpoint.py``; these cover the SHARED seam and the
generic ``POST /api/settings`` that reaches it.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    from ouroboros import config as cfg

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    settings_path = data_dir / "settings.json"
    monkeypatch.setattr(cfg, "DATA_DIR", data_dir, raising=True)
    monkeypatch.setattr(cfg, "SETTINGS_PATH", settings_path, raising=True)
    cfg.reset_runtime_mode_baseline_for_tests()
    yield settings_path
    cfg.reset_runtime_mode_baseline_for_tests()


@contextlib.contextmanager
def _foreign_lock(settings_path: pathlib.Path):
    """Hold the settings lock the way another PROCESS would: a real O_EXCL fd,
    released exactly as ``_release_settings_lock`` does (close + unlink)."""
    lock_path = pathlib.Path(str(settings_path) + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        yield lock_path
    finally:
        os.close(fd)
        lock_path.unlink(missing_ok=True)


def _settings_app(monkeypatch, settings_path):
    from ouroboros.gateway import settings as settings_mod

    monkeypatch.setattr(settings_mod, "apply_runtime_provider_defaults", lambda s: (s, False, []))
    monkeypatch.setattr(settings_mod, "_start_supervisor_if_needed_for_request",
                        lambda *_a, **_k: False)
    monkeypatch.setattr(settings_mod, "_apply_settings_to_env", lambda *_a, **_k: None)
    monkeypatch.setattr(settings_mod, "_apply_settings_save_side_effects", lambda *_a, **_k: None)
    app = Starlette(routes=[
        Route("/api/settings", endpoint=settings_mod.api_settings_post, methods=["POST"])])
    app.state.drive_root = settings_path.parent
    app.state.repo_dir = settings_path.parent
    return app


def test_a_contended_lock_aborts_before_the_precondition_and_the_write(isolated_settings):
    from ouroboros.gateway.owner_settings import SettingsLockUnavailable, _owner_write_settings

    checked: list = []
    with _foreign_lock(isolated_settings) as lock_path:
        with pytest.raises(SettingsLockUnavailable):
            _owner_write_settings({"TOTAL_BUDGET": 10.0},
                                  precondition=lambda: checked.append("ran") or "")
        assert lock_path.exists(), "the holder's lock was taken or removed"

    assert checked == [], "the precondition ran without the lock it is supposed to hold"
    assert not isolated_settings.exists(), "a contended write still touched settings.json"


def test_the_commit_boundary_is_only_marked_by_a_real_write(isolated_settings):
    from ouroboros.gateway.owner_settings import (
        CommitBoundary,
        SettingsLockUnavailable,
        SettingsPreconditionFailed,
        _owner_write_settings,
    )

    refused = CommitBoundary()
    with pytest.raises(SettingsPreconditionFailed):
        _owner_write_settings({"TOTAL_BUDGET": 10.0}, precondition=lambda: "no",
                              boundary=refused)
    assert refused.committed is False

    locked = CommitBoundary()
    with _foreign_lock(isolated_settings):
        with pytest.raises(SettingsLockUnavailable):
            _owner_write_settings({"TOTAL_BUDGET": 10.0}, boundary=locked)
    assert locked.committed is False

    landed = CommitBoundary()
    _owner_write_settings({"TOTAL_BUDGET": 10.0}, boundary=landed)
    assert landed.committed is True
    assert json.loads(isolated_settings.read_text(encoding="utf-8"))["TOTAL_BUDGET"] == 10.0


def test_generic_settings_post_reports_a_contended_lock_as_unsaved(monkeypatch,
                                                                   isolated_settings):
    app = _settings_app(monkeypatch, isolated_settings)
    with _foreign_lock(isolated_settings):
        resp = TestClient(app).post("/api/settings", json={"TOTAL_BUDGET": "25"})

    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["code"] == "settings_locked"
    assert body["saved"] is False
    assert not isolated_settings.exists()


def test_generic_settings_post_says_saved_when_a_post_commit_step_fails(monkeypatch,
                                                                       isolated_settings):
    """FINDING 4 at the second site: the write lands at settings.py's commit,
    and every step after it (env projection, supervisor start, hot-reload) is
    post-commit. The broad ``except`` answered all three with ``400``."""
    from ouroboros.gateway import settings as settings_mod

    app = _settings_app(monkeypatch, isolated_settings)

    def _boom(*_a, **_k):
        raise RuntimeError("hot reload exploded")

    monkeypatch.setattr(settings_mod, "_apply_settings_save_side_effects", _boom)
    resp = TestClient(app).post("/api/settings", json={"TOTAL_BUDGET": "25"})

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["saved"] is True
    assert body["status"] == "saved_with_post_commit_error"
    assert body["post_commit_failed"] == "hot-reload"
    assert "hot reload exploded" in body["error"]
    # The claim is checked against the file, not against the handler's opinion.
    assert json.loads(isolated_settings.read_text(encoding="utf-8"))["TOTAL_BUDGET"] == 25.0


def test_a_pre_commit_failure_is_still_reported_as_unsaved(monkeypatch, isolated_settings):
    """The other side of the boundary must not drift: a failure BEFORE the write
    keeps its old, correct answer — and SAYS so.

    The earlier version of this test checked only that the file was absent, which
    is the one thing the CLIENT cannot see. Once a post-commit failure started
    answering ``saved=true``, an envelope that merely omits ``saved`` stopped
    being readable: nothing-was-written and an old/truncated response look
    identical. So the assertion is on the FIELD, not on the disk."""
    from ouroboros.gateway import settings as settings_mod

    app = _settings_app(monkeypatch, isolated_settings)
    monkeypatch.setattr(settings_mod, "_classify_settings_changes",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("before the write")))
    resp = TestClient(app).post("/api/settings", json={"TOTAL_BUDGET": "25"})

    assert resp.status_code == 400, resp.text
    assert "before the write" in resp.json()["error"]
    assert resp.json()["saved"] is False, resp.text
    assert not isolated_settings.exists()


@pytest.mark.parametrize("payload", [
    {"TOTAL_BUDGET": "25", "OUROBOROS_UPDATE_CHANNEL": "not-a-channel"},
    {"TOTAL_BUDGET": "25", "OUROBOROS_POST_TASK_EVOLUTION_CADENCE": "every_n:0"},
    {"TOTAL_BUDGET": "not a number"},
    {"MINIMAX_REGION": "atlantis"},
])
def test_every_generic_validation_refusal_says_saved_false(monkeypatch, isolated_settings,
                                                           payload):
    """Malformed input is a PRE-commit refusal like any other. Each of these
    answered a bare ``{"error": ...}``, so the wizard and any API client had to
    infer "nothing was saved" from the status code."""
    app = _settings_app(monkeypatch, isolated_settings)
    resp = TestClient(app).post("/api/settings", json=payload)

    assert resp.status_code == 400, resp.text
    assert resp.json()["saved"] is False, resp.text
    assert not isolated_settings.exists()


def test_a_malformed_body_says_saved_false(monkeypatch, isolated_settings):
    app = _settings_app(monkeypatch, isolated_settings)
    resp = TestClient(app).post("/api/settings", json=["not", "an", "object"])

    assert resp.status_code == 400, resp.text
    assert resp.json()["saved"] is False, resp.text


# The FIVE single-decision owner endpoints — membership is "calls
# `_owner_write_settings`", not "wears the decorator". Each entry is the route,
# the handler name and a payload its own validation accepts.
_OWNER_SETTINGS_WRITERS = [
    ("/api/owner/runtime-mode", "api_owner_runtime_mode", {"mode": "pro"}),
    ("/api/owner/auto-grant", "api_owner_auto_grant", {"enabled": False}),
    ("/api/owner/context-mode", "api_owner_context_mode", {"mode": "low"}),
    ("/api/owner/scope-review-floor", "api_owner_scope_review_floor", {"floor": "advisory"}),
    ("/api/owner/safety-mode", "api_owner_safety_mode", {"mode": "light"}),
]


def _owner_app(handler_name, route, isolated_settings):
    from ouroboros.gateway import settings as settings_mod

    app = Starlette(routes=[
        Route(route, endpoint=getattr(settings_mod, handler_name), methods=["POST"])])
    app.state.drive_root = isolated_settings.parent
    return app


@pytest.mark.parametrize("route,handler_name,payload", _OWNER_SETTINGS_WRITERS)
def test_owner_endpoints_map_a_contended_lock_to_a_typed_refusal(monkeypatch, isolated_settings,
                                                                 route, handler_name, payload):
    """Every single-decision owner endpoint shares the seam, and none of them had
    a handler at all: the refusal reached Starlette as an opaque 500 that said
    nothing about whether the file changed. Parametrised over ALL FIVE, so the
    guard cannot quietly go missing from four of them."""
    from ouroboros.gateway import settings as settings_mod

    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(settings_mod, "_has_running_agent_tasks", lambda: False, raising=False)
    app = _owner_app(handler_name, route, isolated_settings)

    with _foreign_lock(isolated_settings):
        resp = TestClient(app).post(route, json=payload)

    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "settings_locked"
    assert resp.json()["saved"] is False
    assert not isolated_settings.exists()


@pytest.mark.parametrize("route,handler_name,_payload", _OWNER_SETTINGS_WRITERS)
def test_owner_endpoint_validation_refusals_say_saved_false(monkeypatch, isolated_settings,
                                                            route, handler_name, _payload):
    """The same field on the same endpoints' PRE-commit validation path."""
    monkeypatch.setattr(os, "environ", dict(os.environ))
    app = _owner_app(handler_name, route, isolated_settings)
    resp = TestClient(app).post(route, json={"mode": "?", "enabled": "?", "floor": "?"})

    assert resp.status_code == 400, resp.text
    assert resp.json()["saved"] is False, resp.text
    assert not isolated_settings.exists()


def test_the_capability_ack_is_not_a_settings_writer(monkeypatch, isolated_settings, tmp_path):
    """FINDING 3, decided the smaller way. `api_acknowledge_capability` wore
    `owner_write_guard` but never calls `_owner_write_settings`: it writes its own
    route-fingerprinted evidence file. The decorator translated exceptions that
    cannot be raised while implying the endpoint was lock-guarded — under a
    genuinely held lock the five writers above refuse 503 and this one records
    its acknowledgement and answers 200, which is CORRECT and now unclaimed.

    Widening the settings lock to cover an unrelated ledger would have made the
    decorator true at the price of coupling a capability ack to whether some
    settings save is in flight. The decorator went instead; this test pins both
    halves so the count of guarded settings writers stays five."""
    from ouroboros.gateway import settings as settings_mod

    assert not hasattr(settings_mod.api_acknowledge_capability, "__wrapped__"), (
        "the capability ack is wearing owner_write_guard again; it writes no settings"
    )

    app = Starlette(routes=[Route("/api/owner/capability-ack",
                                  endpoint=settings_mod.api_acknowledge_capability,
                                  methods=["POST"])])
    app.state.drive_root = tmp_path / "drive"
    with _foreign_lock(isolated_settings):
        resp = TestClient(app).post("/api/owner/capability-ack", json={
            "provider": "openai", "model": "gpt-5.6-luna", "window_tokens": 1_000_000})

    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert resp.json()["ack"]["window_tokens"] == 1_000_000
    assert not isolated_settings.exists(), "a capability ack touched settings.json"


def test_the_environment_cannot_author_an_install_time_fact_through_a_generic_save(
        monkeypatch, isolated_settings):
    """FINDING 1 at the generic endpoint. The merge skip-list blocks the request
    BODY, so the reviewer's probe never sent one: it put the preset marker in the
    ENVIRONMENT. `load_settings` overlaid it, the save carried it through, and an
    environment-only marker landed on disk as a durable install-time fact."""
    from ouroboros import config as cfg

    monkeypatch.setenv("OUROBOROS_SUBSCRIPTION_PRESET_VERSION", "1")
    monkeypatch.setenv("OUROBOROS_ONBOARDING_COMPLETED_AT", "2020-01-01T00:00:00Z")
    assert cfg.load_settings()["OUROBOROS_SUBSCRIPTION_PRESET_VERSION"] == "", (
        "the loader read an install-time fact out of the environment")

    app = _settings_app(monkeypatch, isolated_settings)
    resp = TestClient(app).post("/api/settings", json={"TOTAL_BUDGET": "25"})

    assert resp.status_code == 200, resp.text
    saved = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert not saved.get("OUROBOROS_SUBSCRIPTION_PRESET_VERSION")
    assert not saved.get("OUROBOROS_ONBOARDING_COMPLETED_AT")


def test_install_time_facts_are_never_projected_back_into_the_environment(monkeypatch,
                                                                          isolated_settings):
    """The other direction of the same set: exported, they would be read back by
    the next process that loads settings from a bare environment."""
    from ouroboros import config as cfg

    for key in cfg.ENDPOINT_AUTHORED_SETTINGS:
        monkeypatch.delenv(key, raising=False)
        assert key not in cfg.settings_env_keys()

    cfg.apply_settings_to_env({
        "OUROBOROS_SUBSCRIPTION_PRESET_VERSION": "1",
        "OUROBOROS_ONBOARDING_COMPLETED_AT": "2026-08-09T00:00:00Z",
    })

    for key in cfg.ENDPOINT_AUTHORED_SETTINGS:
        assert key not in os.environ, f"{key} was projected into the environment"


def test_a_lock_held_read_does_not_wait_for_the_lock_it_holds(isolated_settings):
    """FINDING 5, at the config seam: ``load_settings`` takes the lock, so a
    precondition running INSIDE it must use the lock-held read or spend the full
    2s timeout re-taking a lock it already owns."""
    import time

    from ouroboros import config as cfg

    isolated_settings.write_text(json.dumps({"TOTAL_BUDGET": 42.0}), encoding="utf-8")
    with _foreign_lock(isolated_settings):
        started = time.monotonic()
        settings = cfg.load_settings_lock_held()
        elapsed = time.monotonic() - started

    assert settings["TOTAL_BUDGET"] == 42.0
    assert elapsed < 0.5, f"the lock-held read waited {elapsed:.2f}s for a lock it holds"
