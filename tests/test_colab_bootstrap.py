from __future__ import annotations
import subprocess

def test_build_colab_settings_defaults_auto_grant_and_runtime():
    from ouroboros.colab_bootstrap import build_colab_settings, masked_secret_status
    settings = build_colab_settings({"OPENROUTER_API_KEY": "or-key", "TELEGRAM_BOT_TOKEN": "tg-token", "GITHUB_TOKEN": "gh-token"}, github_repo="anton/ouroboros", total_budget=25, runtime_mode="pro", max_workers=2)
    assert settings["GITHUB_REPO"] == "anton/ouroboros"
    assert settings["OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS"] == "true"
    assert masked_secret_status(settings)["TELEGRAM_BOT_TOKEN"] is True

def test_build_colab_settings_merges_existing_owner_choices():
    # A Colab re-run must preserve prior owner choices not set by the launch knobs
    # (pinned chat, tweaked model) and drop private sentinel keys.
    from ouroboros.colab_bootstrap import build_colab_settings
    existing = {"TELEGRAM_CHAT_ID": "12345", "OUROBOROS_MODEL": "custom/model", "_settings_file_exists": True}
    out = build_colab_settings({"OPENROUTER_API_KEY": "k"}, existing=existing)
    assert out["TELEGRAM_CHAT_ID"] == "12345"
    assert out["OUROBOROS_MODEL"] == "custom/model"
    assert "_settings_file_exists" not in out
    assert out["OPENROUTER_API_KEY"] == "k"


def test_build_colab_settings_accepts_vision_model_override():
    from ouroboros.colab_bootstrap import build_colab_settings

    out = build_colab_settings(
        {"OPENROUTER_API_KEY": "k"},
        models={"OUROBOROS_MODEL_VISION": "google/gemini-2.5-pro"},
    )
    assert out["OUROBOROS_MODEL_VISION"] == "google/gemini-2.5-pro"

def test_quickstart_uses_clone_or_update_repo_helper():
    import pathlib
    source = pathlib.Path(__file__).resolve().parents[1].joinpath("notebooks", "colab_quickstart.py").read_text(encoding="utf-8")
    assert "clone_or_update_repo" in source
    assert source.index("clone_or_update_repo(REPO_DIR)") < source.index("pip", source.index("clone_or_update_repo(REPO_DIR)"))

def test_clone_or_update_repo_fast_forwards_existing_checkout(tmp_path):
    from ouroboros.colab_bootstrap import clone_or_update_repo
    upstream = tmp_path / "upstream"; upstream.mkdir()
    subprocess.run(["git", "init"], cwd=upstream, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=upstream, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=upstream, check=True)
    subprocess.run(["git", "checkout", "-b", "ouroboros"], cwd=upstream, check=True, capture_output=True)
    (upstream / "marker.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "marker.txt"], cwd=upstream, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v1"], cwd=upstream, check=True, capture_output=True)
    checkout = tmp_path / "checkout"
    clone_or_update_repo(checkout, source_url=str(upstream), branch="ouroboros")
    (upstream / "marker.txt").write_text("v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "marker.txt"], cwd=upstream, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v2"], cwd=upstream, check=True, capture_output=True)
    clone_or_update_repo(checkout, source_url=str(upstream), branch="ouroboros")
    assert (checkout / "marker.txt").read_text(encoding="utf-8") == "v2\n"

def test_get_colab_secret_optional_returns_empty_without_prompt(monkeypatch):
    from ouroboros.colab_bootstrap import get_colab_secret
    monkeypatch.delenv("OUROBOROS_TEST_ABSENT_KEY", raising=False)
    # required=False must never block on getpass when the secret is absent.
    assert get_colab_secret("OUROBOROS_TEST_ABSENT_KEY", required=False) == ""

def _native_telegram_index(*, missing=None, conflict=None):
    return {
        "skills": [{
            "name": "telegram",
            "source": "native",
            "review_profile": "native_seed",
            "executable_review": True,
            "review_stale": False,
            "conflict": conflict,
            "grants": {
                "missing_keys": list(missing or []),
                "missing_permissions": [],
            },
        }]
    }


def test_ensure_native_telegram_grants_enables_and_sets_poc_modes(monkeypatch):
    from ouroboros.colab_bootstrap import ensure_native_telegram_live
    calls = []
    index_calls = 0
    monkeypatch.setattr("ouroboros.colab_bootstrap.time.sleep", lambda _seconds: None)
    def fake_request(method, path, body=None, timeout=None):
        nonlocal index_calls
        calls.append((method, path, body, timeout))
        if path == "/api/health":
            return 200, {"ok": True}
        if path == "/api/extensions":
            index_calls += 1
            payload = _native_telegram_index(missing=["TELEGRAM_BOT_TOKEN"])
            payload["skills"][0]["grants"]["missing_permissions"] = ["subscribe_event:chat.outbound"]
            if index_calls == 1:
                payload["skills"][0]["executable_review"] = False
                payload["skills"][0]["review_stale"] = True
            return 200, payload
        if path.endswith("/grants"):
            assert index_calls == 2
        return 200, {"ok": True}
    status = ensure_native_telegram_live(
        settings={
            "TELEGRAM_BOT_TOKEN": "x",
            "OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS": "true",
        },
        request=fake_request,
        timeout=5,
    )
    assert status["ok"] is True and status["settings_ok"] is True
    assert status["steps"] == ["ready", "discovered", "granted", "enabled", "settings_saved"]
    triples = [(m, p, b) for (m, p, b, t) in calls]
    assert ("POST", "/api/skills/telegram/grants", {"items": ["TELEGRAM_BOT_TOKEN", "subscribe_event:chat.outbound"]}) in triples
    assert ("POST", "/api/skills/telegram/toggle", {"enabled": True}) in triples
    assert (
        "POST",
        "/api/extensions/telegram/settings/save",
        {
            "TELEGRAM_COMMAND_MODE": "full_access",
            "TELEGRAM_MIRROR_MODE": "all",
            "TELEGRAM_MINIAPP_ENABLED": "on",
        },
    ) in triples
    assert all("marketplace" not in path and not path.endswith("/review") for _, path, _ in triples)
    assert all("chat.document" not in str(body) for _, _, body in triples)


def test_ensure_native_telegram_respects_disabled_auto_grant():
    from ouroboros.colab_bootstrap import ensure_native_telegram_live
    calls = []
    def fake_request(method, path, body=None, timeout=None):
        calls.append((method, path, body))
        if path == "/api/health":
            return 200, {}
        if path == "/api/extensions":
            return 200, _native_telegram_index(missing=["TELEGRAM_BOT_TOKEN"])
        return 200, {}
    status = ensure_native_telegram_live(
        settings={
            "TELEGRAM_BOT_TOKEN": "x",
            "OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS": "false",
        },
        request=fake_request,
        timeout=5,
    )
    assert status["ok"] is False
    assert "automatic grants are disabled" in status["error"]
    assert all(not path.endswith(("/grants", "/toggle")) for _, path, _ in calls)


def test_ensure_native_telegram_settings_failure_is_not_silent():
    from ouroboros.colab_bootstrap import ensure_native_telegram_live
    def fake_request(method, path, body=None, timeout=None):
        if path == "/api/health":
            return 200, {}
        if path == "/api/extensions":
            return 200, _native_telegram_index()
        if path.endswith("/settings/save"):
            return 404, {"error": "route not found"}
        return 200, {}
    status = ensure_native_telegram_live(settings={"TELEGRAM_BOT_TOKEN": "x"}, request=fake_request, timeout=5)
    assert status["ok"] is True
    assert status.get("settings_ok") is False
    assert status.get("warning")
    assert "settings_saved" not in status["steps"]


def test_ensure_native_telegram_reports_conflict_without_disabling_peer():
    from ouroboros.colab_bootstrap import ensure_native_telegram_live
    calls = []
    def fake_request(method, path, body=None, timeout=None):
        calls.append((method, path, body))
        if path == "/api/health":
            return 200, {}
        if path == "/api/extensions":
            return 200, _native_telegram_index(conflict={
                "code": "skill_conflict",
                "skills": ["telegram-bridge"],
                "omitted": 0,
            })
        return 200, {}
    status = ensure_native_telegram_live(
        settings={"TELEGRAM_BOT_TOKEN": "x"},
        request=fake_request,
        timeout=5,
    )
    assert status["ok"] is False
    assert "telegram-bridge" in status["error"]
    assert all(not path.endswith("/toggle") for _, path, _ in calls)


def test_ensure_native_telegram_reports_server_not_ready():
    from ouroboros.colab_bootstrap import ensure_native_telegram_live
    status = ensure_native_telegram_live(request=lambda *a, **k: (503, {}), timeout=0.2)
    assert status["ok"] is False and "ready" in status["error"]


def test_ensure_native_telegram_stops_on_enable_error():
    from ouroboros.colab_bootstrap import ensure_native_telegram_live
    def fake_request(method, path, body=None, timeout=None):
        if path == "/api/health":
            return 200, {}
        if path == "/api/extensions":
            return 200, _native_telegram_index()
        if path.endswith("/toggle"):
            return 409, {"error": "cannot enable until requested key and permission grants are approved"}
        return 200, {}
    status = ensure_native_telegram_live(settings={"TELEGRAM_BOT_TOKEN": "x"}, request=fake_request, timeout=5)
    assert status["ok"] is False and "enable failed" in status["error"]
