"""One onboarding host, served by a live gateway (D-8).

These tests pin the startup REORDERING and the single wizard host:

* the managed gateway is healthy BEFORE first-run onboarding is presented, and
  every pre-server safety step still precedes the server;
* the wizard is a real page (`GET /onboarding`) reachable with no provider
  configured and no supervisor running;
* neither the launcher's nor the server's boot normalization may CREATE
  settings.json, because the fresh-install proofs are gated on its absence;
* the desktop host has NO save path of its own: completion is the single atomic
  `POST /api/onboarding/complete` on every host, and that endpoint authors the
  fresh-install `OUROBOROS_SAFETY_MODE=light` while the generic save path still
  cannot lower safety;
* closing the setup window without saving stays non-fatal.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import sys
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# Fake pywebview: the real one is absent in the quick-test lane (same
# precedent as test_launcher_headless_fallback.py).
# --------------------------------------------------------------------------


class _FakeWindow:
    def __init__(self) -> None:
        self.destroyed = False

    def destroy(self) -> None:
        self.destroyed = True


def _install_fake_webview(monkeypatch, on_start=None):
    fake = types.ModuleType("webview")
    fake.windows = []
    created: dict = {}

    def create_window(title, url=None, js_api=None, **kwargs):
        created.update({"title": title, "url": url, "js_api": js_api, **kwargs})
        window = _FakeWindow()
        fake.windows.append(window)
        return window

    def start(*_args, **_kwargs):
        created["started"] = True
        if on_start is not None:
            on_start(created)

    fake.create_window = create_window
    fake.start = start
    monkeypatch.setitem(sys.modules, "webview", fake)
    return created, fake


def _valid_onboarding_payload() -> dict:
    return {
        "OPENAI_API_KEY": "sk-openai-1234567890",
        "OPENROUTER_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "MINIMAX_API_KEY": "",
        "MINIMAX_REGION": "",
        "OPENAI_COMPATIBLE_BASE_URL": "",
        "OPENAI_COMPATIBLE_API_KEY": "",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY": "",
        "TOTAL_BUDGET": 10,
        "OUROBOROS_PER_TASK_COST_USD": 20,
        "OUROBOROS_REVIEW_ENFORCEMENT": "advisory",
        # `light` is the LOWEST runtime rank, so this payload can never trip the
        # elevation ratchet on a process whose baseline another test pinned.
        "OUROBOROS_RUNTIME_MODE": "light",
        "OUROBOROS_SKILLS_REPO_PATH": "",
        "LOCAL_MODEL_SOURCE": "",
        "LOCAL_MODEL_FILENAME": "",
        "LOCAL_MODEL_CONTEXT_LENGTH": 16384,
        "LOCAL_MODEL_N_GPU_LAYERS": -1,
        "LOCAL_MODEL_CHAT_FORMAT": "",
        "LOCAL_ROUTING_MODE": "cloud",
        "OUROBOROS_MODEL": "openai::gpt-5.6-terra",
        "OUROBOROS_MODEL_HEAVY": "",
        "OUROBOROS_MODEL_LIGHT": "",
        "OUROBOROS_MODEL_FALLBACKS": "",
    }


# --------------------------------------------------------------------------
# 1. Startup ordering: gateway first, onboarding after
# --------------------------------------------------------------------------


def test_first_run_onboarding_is_presented_only_after_the_gateway_is_healthy():
    """THE reordering (D-8). The old sequence rendered the wizard before
    server.py existed, so it could not reach /api/* at all — which is why
    connecting an agent subscription during first-run was impossible on desktop.
    Every pre-server safety step is still pre-server: they are preconditions of
    the server itself."""
    import launcher

    src = inspect.getsource(launcher.main)
    order = [
        "acquire_pid_lock()",
        "check_git()",
        "bootstrap_repo()",
        "_prepare_first_run_settings()",
        '_cleanup_recorded_server_process("preflight")',
        "lifecycle_thread.start()",
        "_await_server_ready(port, _abort)",
        "_present_first_run_onboarding(",
    ]
    positions = [src.index(marker) for marker in order]
    assert positions == sorted(positions), f"startup order drifted: {order}"

    # Presented against the AUTHORITATIVE bound port, and only once healthy.
    call_at = src.index("_present_first_run_onboarding(")
    call = src[call_at:call_at + 160]
    assert "onboarding_settings, actual_port" in call
    assert "headless=_headless" in call
    assert "if server_ready and onboarding_required" in src
    # The pre-server wizard window is gone for good.
    assert "_run_first_run_wizard" not in src


def test_launcher_restart_request_is_not_charged_to_the_crash_fuse():
    """A launcher-requested recycle (adopting first-run configuration) must be
    handled like the agent's own code-42 restart: no crash accounting, no crash
    backoff — otherwise four first-run saves would trip the five-crash fuse."""
    import launcher

    loop_src = inspect.getsource(launcher.agent_lifecycle_loop)
    restart_at = loop_src.index("_agent_restart_requested.is_set()")
    crash_at = loop_src.index("crash_times.append(now)")
    fuse_at = loop_src.index("len(crash_times) >= MAX_CRASH_RESTARTS")
    assert restart_at < crash_at < fuse_at
    assert "_agent_restart_requested.clear()" in loop_src


def test_request_agent_restart_flags_intent_then_stops_the_child(monkeypatch):
    import launcher

    stopped = []
    monkeypatch.setattr(launcher, "stop_agent", lambda: stopped.append(True))
    launcher._agent_restart_requested.clear()
    try:
        # No live agent: the flag must NOT be left armed, or the next ordinary
        # agent exit would masquerade as a requested restart and skip the fuse.
        monkeypatch.setattr(launcher, "_agent_proc", None)
        launcher._request_agent_restart()
        assert launcher._agent_restart_requested.is_set() is False
        assert stopped == []

        monkeypatch.setattr(launcher, "_agent_proc", object())
        launcher._request_agent_restart()
        assert launcher._agent_restart_requested.is_set() is True
        assert stopped == [True]
    finally:
        launcher._agent_restart_requested.clear()


# --------------------------------------------------------------------------
# 2. The setup window loads the live page
# --------------------------------------------------------------------------


def test_setup_window_loads_the_live_onboarding_page(monkeypatch):
    from ouroboros import launcher_onboarding

    created, _fake = _install_fake_webview(monkeypatch)

    outcome = launcher_onboarding.present_first_run_onboarding({}, 8899)

    assert created["url"] == "http://127.0.0.1:8899/onboarding"
    assert created.get("html") is None
    assert created["started"] is True
    # Window LIFECYCLE ONLY. The bridge is not a settings authority any more:
    # completion is the same atomic endpoint a browser owner posts to, so a
    # bridge method able to write settings.json would be a live authority with
    # no caller and no audit.
    api = created["js_api"]
    assert callable(getattr(api, "onboarding_finished", None))
    assert not hasattr(api, "save_wizard")
    assert not hasattr(api, "claude_code_status")
    assert not hasattr(api, "install_claude_code")
    assert not hasattr(api, "fetch_compatible_models")
    # Nothing was saved: a window that merely opened is not a completion.
    assert outcome == {"saved": False, "restart_required": False}


def test_closing_the_setup_window_without_saving_is_non_fatal(monkeypatch):
    """Startup continues and the blocking overlay remains the owner's surface."""
    import launcher
    from ouroboros import launcher_onboarding

    _install_fake_webview(monkeypatch)  # start() returns without any bridge call

    assert launcher_onboarding.present_first_run_onboarding({}, 8765)["saved"] is False

    src = inspect.getsource(launcher.main)
    cancel_at = src.index('if not onboarding["saved"]')
    next_at = src.index('if onboarding["restart_required"]')
    # The cancel branch only logs; it never aborts startup or skips the UI.
    cancel_branch = src[cancel_at:next_at]
    assert "sys.exit" not in cancel_branch
    assert "return" not in cancel_branch
    assert "Launching anyway" in cancel_branch


def test_completion_reporting_restart_required_recycles_the_managed_server(monkeypatch):
    import launcher
    from ouroboros import launcher_onboarding

    def drive(created):
        created["js_api"].onboarding_finished({"ok": True, "restart_required": True})

    _created, fake = _install_fake_webview(monkeypatch, on_start=drive)

    outcome = launcher_onboarding.present_first_run_onboarding({}, 8765)

    assert outcome == {"saved": True, "restart_required": True}
    assert all(window.destroyed for window in fake.windows)

    src = inspect.getsource(launcher.main)
    restart_at = src.index('if onboarding["restart_required"]')
    assert "_request_agent_restart()" in src[restart_at:restart_at + 800]
    assert "_await_server_ready(port, _abort)" in src[restart_at:restart_at + 800]


# --------------------------------------------------------------------------
# 3. Fresh-install proofs
# --------------------------------------------------------------------------


def test_the_desktop_setup_window_cannot_write_settings_at_all(monkeypatch, tmp_path):
    """D-8 closed: the desktop host has NO save path of its own.

    The one deliberately preserved exception was authoring the fresh-install
    `light` safety coverage. `POST /api/onboarding/complete` now authors it on
    its own server-side freshness proof (see
    `test_a_desktop_shaped_completion_still_authors_light_on_a_fresh_install`
    in tests/test_onboarding_complete_endpoint.py), so the exception is gone and
    the full->light ratchet is the ONLY rule an ordinary save sees — before and
    after a settings file exists."""
    from ouroboros import config as cfg
    from ouroboros import launcher_onboarding

    monkeypatch.setattr(cfg, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.delenv("OUROBOROS_SAFETY_MODE", raising=False)

    # Generic (non-owner) save of that lowering: refused on a fresh install.
    with pytest.raises(PermissionError, match="OUROBOROS_SAFETY_MODE lowering refused"):
        cfg.save_settings({"OUROBOROS_SAFETY_MODE": "light", "TOTAL_BUDGET": 10})
    assert not (tmp_path / "settings.json").exists()

    seen = {}

    def drive(created):
        api = created["js_api"]
        seen["methods"] = [n for n in dir(api) if not n.startswith("_")]
        api.onboarding_finished({"ok": True, "restart_required": True})

    _created, _fake = _install_fake_webview(monkeypatch, on_start=drive)
    outcome = launcher_onboarding.present_first_run_onboarding({}, 8765)

    assert seen["methods"] == ["onboarding_finished"]
    assert outcome == {"saved": True, "restart_required": True}
    # The window reported completion, and the LAUNCHER still wrote nothing: the
    # bytes on disk (if any) came from the endpoint the page posted to.
    assert not (tmp_path / "settings.json").exists()

    # With a settings file present, the ratchet still refuses the same lowering.
    (tmp_path / "settings.json").write_text(json.dumps({"TOTAL_BUDGET": 10}), encoding="utf-8")
    with pytest.raises(PermissionError, match="OUROBOROS_SAFETY_MODE lowering refused"):
        cfg.save_settings({"OUROBOROS_SAFETY_MODE": "off", "TOTAL_BUDGET": 10})


def test_the_launcher_never_authors_settings_during_first_run(monkeypatch, tmp_path):
    """The invariant the old bridge-validation test guarded, at its new home.

    Payload validation and the refusal-writes-nothing rule now belong to the
    completion endpoint (tests/test_onboarding_complete_endpoint.py:
    `test_subscription_alone_does_not_satisfy_the_launch_gate`). What remains
    true HERE is stronger and simpler: presenting first-run onboarding writes
    nothing, whatever the page does."""
    from ouroboros import config as cfg
    from ouroboros import launcher_onboarding

    monkeypatch.setattr(cfg, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)

    def drive(created):
        # Nothing on the bridge can persist, so a page that tries finds no door.
        assert not hasattr(created["js_api"], "save_wizard")

    _install_fake_webview(monkeypatch, on_start=drive)
    outcome = launcher_onboarding.present_first_run_onboarding({}, 8765)

    assert outcome["saved"] is False
    assert not (tmp_path / "settings.json").exists()


def test_onboarding_page_route_is_declared_on_the_gateway_boundary():
    from ouroboros.gateway.contracts import HTTP_ENDPOINTS

    assert "GET /onboarding" in HTTP_ENDPOINTS
    # The page and the atomic completion it posts to are both on the boundary:
    # every host now finishes through the same one transaction.
    assert "POST /api/onboarding/complete" in HTTP_ENDPOINTS


# --------------------------------------------------------------------------
# 5. Completion seam
# --------------------------------------------------------------------------


def test_completion_is_the_single_atomic_endpoint_on_every_host():
    """D-8 closed. The page posts ONE transaction and has no second way to
    finish: not the `POST /api/settings` + `POST /api/owner/runtime-mode` pair
    whose failure between the two writes left providers saved and runtime mode
    not, and not the desktop `save_wizard` bridge, whose only reason to exist
    (authoring the fresh-install `light` safety default) the endpoint now
    discharges itself.

    CHANGED EXPECTATION: this test previously REQUIRED both fallbacks, because
    the endpoint did not exist in the branch that wrote it. It does now, so the
    fallbacks were the last thing keeping a first run able to take a
    non-atomic path."""
    source = (REPO / "web/modules/onboarding_wizard.js").read_text(encoding="utf-8")
    # The absence assertions read CODE, not prose: the module documents the two
    # removed paths by name, and a comment must not be able to fail the gate
    # (nor to satisfy it).
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("//"))

    assert "const ONBOARDING_COMPLETE_ENDPOINT = '/api/onboarding/complete';" in code
    assert "const result = await completeOnboardingAtomically(payload);" in code
    assert "saveWizardThroughDesktopBridge" not in code
    assert "saveWizardThroughSettingsPair" not in code
    assert "save_wizard" not in code
    assert "/api/owner/runtime-mode" not in code
    assert "'/api/settings'" not in code
    # A missing route is now a real error, not a signal to take another path.
    assert "response.status === 404" not in code
    # One completion announcer for all three shells (that part is unchanged).
    assert "function announceCompletion(result)" in source
    assert "ouroboros:onboarding-complete" in source
    assert "window.pywebview.api.onboarding_finished" in source


# --------------------------------------------------------------------------
# 6. D-1: the startup gate is untouched
# --------------------------------------------------------------------------


def test_a_subscription_can_never_satisfy_the_startup_provider_gate():
    """D-1: `has_startup_ready_provider` stays a structural API-key/local-routing
    predicate. Agent subscriptions live in the Claudexor daemon and contribute
    nothing to it — connecting one during onboarding must not unlock startup."""
    from ouroboros.server_runtime import has_startup_ready_provider

    assert has_startup_ready_provider({}) is False
    assert has_startup_ready_provider({"OUROBOROS_SUBAGENT_HARNESS": "claude=claude-opus-5"}) is False
    assert has_startup_ready_provider({"OUROBOROS_REVIEWER_SLOTS": '{"triad": []}'}) is False
    assert has_startup_ready_provider({"LOCAL_MODEL_SOURCE": "Qwen/Qwen2.5-7B"}) is False
    assert has_startup_ready_provider({"OPENROUTER_API_KEY": "sk-or-v1-x"}) is True
    assert has_startup_ready_provider({"USE_LOCAL_MAIN": True}) is True
