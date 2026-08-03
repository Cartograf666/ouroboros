"""Regressions for packaged-runtime resolution and terminal-path symmetry.

Covers the v6.87.10 fixes: bundled payloads reachable from the server process,
pip kept out of the signed bundle (and its exit code honored), the Windows
python download checksum, the merge-aware Update Now action, the
finalization-grace latch, the settings->env export derivation, and the
cancellation path's partial-result rescue.
"""

import pathlib
import subprocess
import sys
import types

import pytest

import ouroboros.launcher_bootstrap as bootstrap_module
from ouroboros import platform_layer


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _log_stub(sink):
    def _record(level):
        def _log(msg, *args, **kwargs):
            sink.append((level, msg % args if args else msg))
        return _log

    return types.SimpleNamespace(
        info=_record("info"), warning=_record("warning"), error=_record("error"),
        debug=_record("debug"),
    )


# --------------------------------------------------------------------------
# F1: bundled node/ripgrep must be visible to the SERVER process, which runs
# out of the managed repo with no sys._MEIPASS.
# --------------------------------------------------------------------------

def _make_bundled_rg(base: pathlib.Path) -> pathlib.Path:
    candidate = platform_layer.embedded_ripgrep_candidates(base)[0]
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("#!/bin/sh\n", encoding="utf-8")
    return candidate


def test_bundled_payload_resolves_through_bundle_dir_env(tmp_path, monkeypatch):
    """A process with no _MEIPASS and a repo-root elsewhere still finds the payload."""
    bundle = tmp_path / "bundle"
    expected = _make_bundled_rg(bundle)
    monkeypatch.delenv(platform_layer.BUNDLE_DIR_ENV, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    assert platform_layer.resolve_bundled_ripgrep() is None  # the pre-fix behaviour

    monkeypatch.setenv(platform_layer.BUNDLE_DIR_ENV, str(bundle))
    assert platform_layer.resolve_bundled_ripgrep() == str(expected)


def test_bundled_node_uses_the_same_bases(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    candidate = platform_layer.embedded_node_candidates(bundle)[0]
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv(platform_layer.BUNDLE_DIR_ENV, str(bundle))
    assert platform_layer.resolve_bundled_node() == str(candidate)


def test_bundle_dir_env_is_exported_to_the_server_and_cli():
    """The two spawn seams must hand the bundle root down; nothing else can."""
    launcher_src = (REPO_ROOT / "launcher.py").read_text(encoding="utf-8")
    start_agent = launcher_src.split("def start_agent", 1)[1].split("\ndef ", 1)[0]
    assert "BUNDLE_DIR_ENV" in start_agent

    cli_src = (REPO_ROOT / "ouroboros" / "packaged_cli.py").read_text(encoding="utf-8")
    inner_env = cli_src.split("def _inner_cli_env", 1)[1].split("\ndef ", 1)[0]
    assert "BUNDLE_DIR_ENV: str(runtime.bundle_root)" in inner_env


# --------------------------------------------------------------------------
# F4 / F3: pip must not write inside the signed bundle, and its exit code must
# not be swallowed.
# --------------------------------------------------------------------------

def test_embedded_python_env_redirects_both_bundle_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONNOUSERSITE", "1")
    env = bootstrap_module.embedded_python_env(tmp_path)
    state = tmp_path / "state"
    assert env["PYTHONPYCACHEPREFIX"] == str(state / "pycache")
    assert env["PYTHONUSERBASE"] == str(state / "python-userbase")
    # An inherited no-user-site would install to the user site then refuse to import it.
    assert "PYTHONNOUSERSITE" not in env


def test_pip_install_target_args_only_for_the_embedded_interpreter(tmp_path):
    embedded = tmp_path / "python-standalone" / "bin" / "python3"
    embedded.parent.mkdir(parents=True)
    embedded.write_text("", encoding="utf-8")
    assert platform_layer.pip_install_target_args(str(embedded)) == ["--user"]
    # A dev venv refuses --user; a blanket flag would break it.
    assert platform_layer.pip_install_target_args(sys.executable) == []


def _install_deps_context(tmp_path, interpreter, returncode, sink):
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "requirements.txt").write_text("anyio\n", encoding="utf-8")
    calls = []

    def _run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout=b"", stderr=b"boom")

    context = bootstrap_module.BootstrapContext(
        bundle_dir=tmp_path, repo_dir=tmp_path / "repo", data_dir=tmp_path / "data",
        settings_path=tmp_path / "settings.json", embedded_python=str(interpreter),
        app_version="6.87.10", hidden_run=_run, save_settings=lambda s: None,
        log=_log_stub(sink),
    )
    return context, calls


def test_install_deps_targets_the_user_site_for_a_bundled_interpreter(tmp_path):
    embedded = tmp_path / "python-standalone" / "bin" / "python3"
    embedded.parent.mkdir(parents=True)
    embedded.write_text("", encoding="utf-8")
    sink = []
    context, calls = _install_deps_context(tmp_path, embedded, 0, sink)
    assert bootstrap_module.install_deps(context) is True
    assert "--user" in calls[0]


def test_install_deps_reports_a_failing_pip(tmp_path):
    sink = []
    context, _calls = _install_deps_context(tmp_path, sys.executable, 1, sink)
    assert bootstrap_module.install_deps(context) is False
    errors = [msg for level, msg in sink if level == "error"]
    assert any("pip exited 1" in msg and "boom" in msg for msg in errors)


# --------------------------------------------------------------------------
# F2: no unverified download may become the packaged runtime.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "script",
    sorted(p.name for p in (REPO_ROOT / "scripts").glob("download_*_standalone.*")),
)
def test_every_standalone_download_verifies_a_checksum(script):
    text = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8").lower()
    assert "sha256" in text, f"{script} downloads a runtime without verifying it"
    assert any(token in text for token in ("mismatch", "verified")), (
        f"{script} computes a hash but never refuses on mismatch"
    )


def test_windows_python_download_pins_the_release_checksum():
    text = (REPO_ROOT / "scripts" / "download_python_standalone.ps1").read_text(encoding="utf-8")
    assert "Get-FileHash -Algorithm SHA256" in text
    assert "throw" in text.split("Get-FileHash", 1)[1]


# --------------------------------------------------------------------------
# F5: Update Now must go through the merge plan, not the hard-reset hatch.
# --------------------------------------------------------------------------

def test_update_now_posts_the_merge_aware_strategy():
    text = (REPO_ROOT / "web" / "modules" / "updates.js").read_text(encoding="utf-8")
    apply_fn = text.split("async function applyUpdate", 1)[1].split("\n    }", 1)[0]
    code = "\n".join(
        line for line in apply_fn.splitlines() if not line.strip().startswith("//")
    )
    assert "'auto_merge'" in code
    for legacy in ("'replace'", "'stash'"):
        assert legacy not in code, f"Update Now still reaches for the legacy {legacy} path"
    assert "assisted_started" in code


# --------------------------------------------------------------------------
# G2: a settings key that never reaches the environment is a silent no-op.
# --------------------------------------------------------------------------

def test_every_settings_key_is_exported_unless_named():
    from ouroboros import config

    exported = set(config.settings_env_keys())
    missing = set(config.SETTINGS_DEFAULTS) - exported - config.SETTINGS_KEYS_NOT_EXPORTED_TO_ENV
    assert not missing, f"settings keys accepted but never exported to env: {sorted(missing)}"
    assert config.SETTINGS_KEYS_NOT_EXPORTED_TO_ENV <= set(config.SETTINGS_DEFAULTS)


def test_skill_lifecycle_timeout_setting_reaches_the_queue(monkeypatch):
    from ouroboros import config
    from ouroboros import skill_lifecycle_queue

    monkeypatch.delenv("OUROBOROS_SKILL_LIFECYCLE_TIMEOUT_SEC", raising=False)
    assert skill_lifecycle_queue._lifecycle_deadline_sec() == float(
        config.SETTINGS_DEFAULTS["OUROBOROS_SKILL_LIFECYCLE_TIMEOUT_SEC"]
    )
    monkeypatch.setattr(config, "_DISK_AUTHORED_SETTINGS", ())
    config.apply_settings_to_env({"OUROBOROS_SKILL_LIFECYCLE_TIMEOUT_SEC": 42})
    assert skill_lifecycle_queue._lifecycle_deadline_sec() == 42.0


# --------------------------------------------------------------------------
# E1: the finalization-grace latch belongs to one episode, not to the task.
# --------------------------------------------------------------------------

def test_finalization_latch_clears_when_the_task_resumes_progress(monkeypatch):
    from supervisor import queue as queue_mod

    task_id = "t-latch"
    meta = {
        "task": {"id": task_id, "chat_id": 0},
        "started_at": 1000.0,
        "last_progress_at": 1000.0,
        "worker_id": 0,
        "finalization_requested_at": 1100.0,
        "finalization_reason": "idle_timeout",
    }
    monkeypatch.setattr(queue_mod, "RUNNING", {task_id: meta})
    monkeypatch.setattr(queue_mod, "PENDING", [])
    monkeypatch.setattr(queue_mod, "get_task_idle_timeout_sec", lambda: 900)
    monkeypatch.setattr(queue_mod, "get_per_call_timeout_ceiling_sec", lambda: 60)
    monkeypatch.setattr(queue_mod, "get_task_abs_ceiling_sec", lambda: 10_000_000)

    # now is only 10s past the last progress: the task is alive again.
    queue_mod._enforce_task_timeouts_locked(
        types.SimpleNamespace(WORKERS={}), 1010.0, 0, {},
    )
    assert "finalization_requested_at" not in meta
    assert "finalization_reason" not in meta
    assert task_id in queue_mod.RUNNING  # still running, not killed


# --------------------------------------------------------------------------
# B5: cancellation must rescue the partial result the way a timeout does.
# --------------------------------------------------------------------------

def test_cancel_and_timeout_paths_share_one_salvage_helper():
    lifecycle = (REPO_ROOT / "supervisor" / "task_lifecycle.py").read_text(encoding="utf-8")
    reaper = (REPO_ROOT / "supervisor" / "task_reaper.py").read_text(encoding="utf-8")
    assert "salvaged_output_note" in reaper
    running = lifecycle.split("def _finish_captured_running", 1)[1].split("\ndef ", 1)[0]
    assert "salvaged_output_note" in running
    # The rescue must precede the write, which precedes the drive removal.
    assert running.index("salvaged_output_note") < running.index("write_task_result(")


def test_cancelled_result_carries_the_salvaged_output(tmp_path, monkeypatch):
    from ouroboros import observability

    drive = tmp_path / "drive"
    monkeypatch.setattr(
        observability, "latest_llm_response_text", lambda root, tid: "partial finding X",
    )
    note = observability.salvaged_output_note(drive, "task-1")
    assert "partial finding X" in note
    assert "salvaged best-effort" in note


def test_salvage_note_is_empty_without_evidence(tmp_path):
    from ouroboros import observability

    assert observability.salvaged_output_note(tmp_path / "missing", "task-1") == ""


def test_cancelling_a_subagent_rescues_its_partial_result(monkeypatch, tmp_path):
    """End to end: the drive is deleted by publication, so the note must already be
    in the terminal result — the asymmetry with the timeout path was exactly this."""
    from ouroboros import observability
    from ouroboros.task_results import load_task_result
    import supervisor.queue as q
    from supervisor import task_lifecycle, workers

    monkeypatch.setattr(q, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(q, "PENDING", [])
    monkeypatch.setattr(q, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr(task_lifecycle, "CANCELLED_ROOT_FENCES", {}, raising=False)
    monkeypatch.setattr(task_lifecycle, "_ACTIVE_CASCADE_FENCES", {}, raising=False)

    state = {"alive": True}
    proc = types.SimpleNamespace(
        pid=4242, is_alive=lambda: state["alive"],
        join=lambda timeout=None: None, terminate=lambda: state.__setitem__("alive", False),
    )
    worker = types.SimpleNamespace(wid=0, proc=proc, busy_task_id="live-salvage", reaping=False)
    monkeypatch.setattr(workers, "WORKERS", {0: worker}, raising=False)
    monkeypatch.setattr(workers, "respawn_worker", lambda wid: None, raising=False)
    monkeypatch.setattr(
        "ouroboros.platform_layer.kill_pid_tree",
        lambda *a, **k: state.__setitem__("alive", False),
    )
    monkeypatch.setattr(q, "RUNNING", {
        "live-salvage": {
            "task": {"id": "live-salvage", "delegation_role": "subagent"}, "worker_id": 0,
        },
    }, raising=False)
    monkeypatch.setattr(q, "_emit_cancel_task_done", lambda *a, **k: None)
    monkeypatch.setattr(
        observability, "latest_llm_response_text", lambda root, tid: "half-written answer",
    )

    assert q.cancel_task_custody("live-salvage") == q.CANCEL_CANCELLED
    result = load_task_result(tmp_path, "live-salvage")
    assert result["status"] == "cancelled"
    assert "half-written answer" in result["result"]
