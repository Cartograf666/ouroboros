"""Fixed-model benchmark profiles use the canonical Available-subagents wire."""

from __future__ import annotations

import json
import pathlib
import sys
import types

import pytest

from devtools.benchmarks.common.manifests import MODEL_SLOT_KEYS, model_slot_snapshot
from devtools.benchmarks.common.model_slots import (
    configured_subagents_snapshot,
    disabled_subagents_setting,
    pin_single_model,
    runtime_actor_snapshot,
    single_model_slot_snapshot,
    single_model_subagents_setting,
)
from devtools.benchmarks.common.server_runner import build_isolated_settings
from ouroboros.configured_subagents import (
    parse_configured_subagents,
    serialize_configured_subagents,
)


REPO = pathlib.Path(__file__).resolve().parents[1]
PROFILE_TARGETS = {
    "devtools/benchmarks/gaia/settings_base.json": "google/gemini-2.5-pro",
    "devtools/benchmarks/osworld/settings_base.json": "anthropic/claude-sonnet-4.6",
    "devtools/benchmarks/programbench/settings_base.json": "openai/gpt-5.5",
    "devtools/benchmarks/continual_learning/settings_base.json": "anthropic/claude-sonnet-4.6",
    "devtools/benchmarks/swe_bench_pro/e1v2/settings_base.json": "anthropic/claude-sonnet-4.5",
    "devtools/benchmarks/swe_bench_pro/e1v2/settings_sonnet46_probe.json":
        "anthropic/claude-sonnet-4.6",
    "devtools/benchmarks/swe_bench_pro/e1v2/_run_settings.example.json":
        "anthropic/claude-sonnet-4.5",
    "devtools/benchmarks/swe_bench_pro/e1v2/profiles/light_subagents_gpt55.json":
        "openai/gpt-5.5",
}

_PROVIDER_ROUTE_ENV_KEYS = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_COMPATIBLE_BASE_URL",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY",
    "GIGACHAT_CREDENTIALS",
    "GIGACHAT_USER",
    "GIGACHAT_PASSWORD",
)


def _only_target(raw: object) -> str:
    config = parse_configured_subagents(raw)
    assert config.enabled is True
    assert len(config.items) == 1
    row = config.items[0]
    assert row.subagent_id == "benchmark-model"
    assert row.route.kind == "api_model"
    assert row.route.credential_profile_id == ""
    return row.route.target_id


def _scrub_model_route_env(monkeypatch) -> None:
    from devtools.benchmarks.common.manifests import MODEL_SLOT_KEYS

    for key in (*_PROVIDER_ROUTE_ENV_KEYS, *MODEL_SLOT_KEYS):
        monkeypatch.delenv(key, raising=False)


def test_single_model_encoder_round_trips_one_exact_api_actor():
    raw = single_model_subagents_setting("openai::gpt-5.6-sol")
    assert _only_target(raw) == "openai::gpt-5.6-sol"
    assert serialize_configured_subagents(parse_configured_subagents(raw)) == raw


def test_pin_single_model_replaces_legacy_heavy_and_prior_actor_list():
    target = {
        "OUROBOROS_MODEL_HEAVY": "decoy/heavy",
        "USE_LOCAL_HEAVY": "true",
        "OUROBOROS_SUBAGENTS": single_model_subagents_setting("decoy/actor"),
    }
    pin_single_model("openai/gpt-5.5", target=target)
    assert "OUROBOROS_MODEL_HEAVY" not in target
    assert "USE_LOCAL_HEAVY" not in target
    assert _only_target(target["OUROBOROS_SUBAGENTS"]) == "openai/gpt-5.5"


def test_disabled_encoder_is_explicit_empty_off():
    config = parse_configured_subagents(disabled_subagents_setting())
    assert config.enabled is False
    assert config.items == ()


def test_disabled_encoder_can_retain_one_exact_measured_actor():
    config = parse_configured_subagents(disabled_subagents_setting("openai/gpt-5.5"))
    assert config.enabled is False
    assert len(config.items) == 1
    assert config.items[0].route.target_id == "openai/gpt-5.5"


def test_runtime_actor_snapshot_compares_main_and_canonical_actor():
    actor = single_model_subagents_setting("openai/gpt-5.5")
    exact = runtime_actor_snapshot(
        {"OUROBOROS_MODEL": "openai/gpt-5.5", "OUROBOROS_SUBAGENTS": actor},
        expected_model="openai/gpt-5.5",
    )
    assert exact["mismatches"] == []
    assert _only_target(json.dumps(exact["available_subagents"])) == "openai/gpt-5.5"

    drifted = runtime_actor_snapshot(
        {
            "OUROBOROS_MODEL": "anthropic/claude-fable-5",
            "OUROBOROS_SUBAGENTS": single_model_subagents_setting(
                "anthropic/claude-fable-5"
            ),
        },
        expected_model="openai/gpt-5.5",
    )
    assert len(drifted["mismatches"]) == 2
    assert _only_target(json.dumps(drifted["available_subagents"])) == (
        "anthropic/claude-fable-5"
    )


def test_single_model_slot_snapshot_is_cli_derived_and_has_no_heavy():
    slots = single_model_slot_snapshot("openai/gpt-5.6-sol", review_slots=2)
    assert slots["OUROBOROS_MODEL"] == "openai/gpt-5.6-sol"
    assert slots["OUROBOROS_REVIEW_MODELS"] == (
        "openai/gpt-5.6-sol,openai/gpt-5.6-sol"
    )
    assert "OUROBOROS_MODEL_HEAVY" not in slots


@pytest.mark.parametrize("relative,expected", PROFILE_TARGETS.items())
def test_committed_single_model_profiles_use_one_canonical_actor(relative: str, expected: str):
    payload = json.loads((REPO / relative).read_text(encoding="utf-8"))
    raw = payload["OUROBOROS_SUBAGENTS"]
    assert _only_target(raw) == expected == payload["OUROBOROS_MODEL"]
    assert serialize_configured_subagents(parse_configured_subagents(raw)) == raw
    assert "OUROBOROS_MODEL_HEAVY" not in payload
    assert "USE_LOCAL_HEAVY" not in payload


def test_benchmark_snapshot_records_canonical_actor_and_refuses_malformed(tmp_path):
    settings = tmp_path / "settings.json"
    raw = single_model_subagents_setting("anthropic/claude-fable-5")
    settings.write_text(json.dumps({"OUROBOROS_SUBAGENTS": raw}), encoding="utf-8")
    snapshot = configured_subagents_snapshot(settings, env_overrides=False)
    assert [row["route"]["target_id"] for row in snapshot["items"]] == [
        "anthropic/claude-fable-5"
    ]

    settings.write_text(json.dumps({"OUROBOROS_SUBAGENTS": "not-json"}), encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        configured_subagents_snapshot(settings, env_overrides=False)


def test_isolated_settings_copy_active_actor_but_not_legacy_heavy():
    raw = single_model_subagents_setting("openai/gpt-5.5")
    isolated = build_isolated_settings({
        "OUROBOROS_MODEL": "openai/gpt-5.5",
        "OUROBOROS_MODEL_HEAVY": "decoy/heavy",
        "OUROBOROS_SUBAGENTS": raw,
    })
    assert isolated["OUROBOROS_SUBAGENTS"] == raw
    assert "OUROBOROS_MODEL_HEAVY" not in isolated


def test_legacy_heavy_read_vocabulary_does_not_leak_into_new_projection(
    tmp_path, monkeypatch
):
    old = tmp_path / "old-settings.json"
    old.write_text(json.dumps({"OUROBOROS_MODEL_HEAVY": "legacy/measured"}), encoding="utf-8")
    assert "OUROBOROS_MODEL_HEAVY" in MODEL_SLOT_KEYS
    old_manifest = {"model_slots": {"OUROBOROS_MODEL_HEAVY": "legacy/measured"}}
    assert old_manifest["model_slots"]["OUROBOROS_MODEL_HEAVY"] == "legacy/measured"
    assert "OUROBOROS_MODEL_HEAVY" not in model_slot_snapshot(old, env_overrides=False)
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "ambient/heavy")
    assert "OUROBOROS_MODEL_HEAVY" not in model_slot_snapshot(old)


def test_clb_operator_adapters_transport_canonical_actor_without_active_heavy():
    patch_root = REPO / "devtools/benchmarks/continual_learning/operator_patches"
    host_patch = (patch_root / "_launcher.v6560.patch").read_text(encoding="utf-8")
    docker_patch = (patch_root / "clb_env_campaign_overrides.v6745.patch").read_text(
        encoding="utf-8"
    )
    official_patch = (patch_root / "adapter_official_submission.v681.patch").read_text(
        encoding="utf-8"
    )
    assert "Canonical benchmark actor bytes are authored by run_clb.py" in host_patch
    assert '"OUROBOROS_SUBAGENTS"):' in docker_patch
    assert "OUROBOROS_SUBAGENTS=os.environ.get" in official_patch
    assert "OUROBOROS_MODEL_HEAVY" not in official_patch
    assert "OUROBOROS_MODEL_CODE" not in official_patch


def test_programbench_preflight_requires_exact_benchmark_actor(tmp_path, monkeypatch):
    from devtools.benchmarks.programbench.run_programbench_e2e import preflight_model_slots

    _scrub_model_route_env(monkeypatch)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "OPENROUTER_API_KEY": "test-key",
        "OUROBOROS_MODEL": "openai/gpt-5.5",
    }), encoding="utf-8")
    with pytest.raises(SystemExit, match="OUROBOROS_SUBAGENTS"):
        preflight_model_slots(settings, solve_model="openai/gpt-5.5")


def test_programbench_preflight_requires_a_declared_measured_model(tmp_path, monkeypatch):
    from devtools.benchmarks.programbench.run_programbench_e2e import preflight_model_slots

    _scrub_model_route_env(monkeypatch)
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="measured model must be declared"):
        preflight_model_slots(settings)


def test_programbench_binds_manifest_to_target_actor_and_refuses_before_discovery(
    tmp_path, monkeypatch
):
    from devtools.benchmarks.programbench import run_programbench_e2e as e2e

    measured = "openai/gpt-5.5"
    actual = "anthropic/claude-fable-5"
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({
            "OUROBOROS_MODEL": measured,
            "OUROBOROS_SUBAGENTS": single_model_subagents_setting(measured),
        }),
        encoding="utf-8",
    )
    out = tmp_path / "run"

    def fake_admit(path, **_kwargs):
        manifest = {"harness": {}, "extra": {}, "output_paths": {}}
        e2e.write_json(path, manifest)
        return manifest

    monkeypatch.setattr(e2e, "run_root", lambda *_a, **_k: out)
    monkeypatch.setattr(e2e, "assert_outside_repo", lambda path, _repo: path)
    monkeypatch.setattr(e2e, "admit_benchmark_run", fake_admit)
    monkeypatch.setattr(
        e2e,
        "preflight_model_slots",
        lambda *_a, **_k: {"OUROBOROS_MODEL": measured},
    )
    monkeypatch.setattr(
        e2e,
        "ouroboros_api_request",
        lambda *_a, **_k: {
            "OUROBOROS_MODEL": actual,
            "OUROBOROS_SUBAGENTS": single_model_subagents_setting(actual),
        },
    )
    monkeypatch.setattr(
        e2e,
        "_load_instances",
        lambda **_k: (_ for _ in ()).throw(AssertionError("paid discovery ran")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_programbench_e2e.py",
            "--repo-dir",
            str(tmp_path / "repo"),
            "--settings-path",
            str(settings),
            "--solve-model",
            measured,
        ],
    )
    assert e2e.main() == 2
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert _only_target(json.dumps(manifest["available_subagents"])) == actual
    assert manifest["extra"]["refusal"]["reason"] == "target_actor_mismatch"


def test_osworld_allowed_target_mismatch_records_the_actual_actor():
    from devtools.benchmarks.osworld import run_step_agent as rsa

    actual = single_model_subagents_setting("anthropic/claude-fable-5")
    manifest = {"harness": {}}
    preflight = {
        "details": {
            "scaffold_mismatch_allowed": ["actor drift"],
            "target_runtime_actor": runtime_actor_snapshot(
                {
                    "OUROBOROS_MODEL": "anthropic/claude-fable-5",
                    "OUROBOROS_SUBAGENTS": actual,
                },
                expected_model="openai/gpt-5.5",
            ),
        }
    }
    rsa._bind_target_actor(manifest, preflight)
    assert _only_target(json.dumps(manifest["available_subagents"])) == (
        "anthropic/claude-fable-5"
    )
    assert manifest["harness"]["target_runtime_actor"]["mismatches"]


def test_osworld_cu_bridge_validates_declared_and_target_actor(tmp_path, monkeypatch):
    from devtools.benchmarks.osworld import run_cu_bridge_agent as cu

    model = "openai/gpt-5.5"
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({
            "OUROBOROS_MODEL": model,
            "OUROBOROS_SUBAGENTS": single_model_subagents_setting(model),
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cu,
        "_api",
        lambda *_a, **_k: {
            "OUROBOROS_MODEL": model,
            "OUROBOROS_SUBAGENTS": single_model_subagents_setting(model),
        },
    )
    assert cu._cu_actor_preflight(settings, "http://target")["ok"] is True

    other = "anthropic/claude-fable-5"
    monkeypatch.setattr(
        cu,
        "_api",
        lambda *_a, **_k: {
            "OUROBOROS_MODEL": other,
            "OUROBOROS_SUBAGENTS": single_model_subagents_setting(other),
        },
    )
    drifted = cu._cu_actor_preflight(settings, "http://target")
    assert drifted["ok"] is False
    assert _only_target(json.dumps(drifted["target"]["available_subagents"])) == other
    manifest = {"harness": {}}
    cu._bind_cu_actor(manifest, drifted)
    assert _only_target(json.dumps(manifest["available_subagents"])) == other
    assert manifest["harness"]["actor_preflight"] is drifted


def test_harness_and_harbor_manifests_use_exact_cli_model(tmp_path, monkeypatch):
    from devtools.benchmarks.harness_bench_fast import run_harness_bench_fast as hbf
    from devtools.benchmarks.terminal_bench import run_harbor_smoke as smoke

    measured = "openai/gpt-5.6-sol"
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({
            "OUROBOROS_MODEL": "decoy/template",
            "OUROBOROS_MODEL_HEAVY": "decoy/heavy",
            "OUROBOROS_SUBAGENTS": single_model_subagents_setting("decoy/actor"),
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_MODEL", "decoy/ambient")
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "decoy/ambient-heavy")

    hbf_root = tmp_path / "hbf"
    monkeypatch.setattr(hbf, "_read_task_ids", lambda *_a, **_k: ["task_1"])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_harness_bench_fast.py",
            "--repo-dir",
            str(REPO),
            "--bench-root",
            str(tmp_path / "bench"),
            "--run-root",
            str(hbf_root),
            "--settings-path",
            str(settings),
            "--model",
            measured,
            "--allow-dirty-seed",
            "--dry-run",
        ],
    )
    assert hbf.main() == 0
    hbf_manifest = json.loads((hbf_root / "run_manifest.json").read_text(encoding="utf-8"))
    assert set(hbf_manifest["model_slots"].values()) == {measured}
    assert "OUROBOROS_MODEL_HEAVY" not in hbf_manifest["model_slots"]
    assert _only_target(json.dumps(hbf_manifest["available_subagents"])) == measured

    smoke_root = tmp_path / "smoke"
    monkeypatch.setattr(smoke, "repo_root_from_devtools", lambda: REPO)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_harbor_smoke.py",
            "--run-root",
            str(smoke_root),
            "--settings-path",
            str(settings),
            "--model",
            measured,
            "--allow-dirty-seed",
        ],
    )
    assert smoke.main() == 0
    smoke_manifest = json.loads(
        (smoke_root / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert set(smoke_manifest["model_slots"].values()) == {measured}
    assert "OUROBOROS_MODEL_HEAVY" not in smoke_manifest["model_slots"]
    assert _only_target(json.dumps(smoke_manifest["available_subagents"])) == measured


def test_swe_pro_derived_profile_overrides_actor_without_heavy(tmp_path):
    from devtools.benchmarks.swe_bench_pro.e1v2.run_pro import derive_run_settings

    template = tmp_path / "template.json"
    template.write_text(json.dumps({
        "OUROBOROS_MODEL": "decoy/main",
        "OUROBOROS_MODEL_HEAVY": "decoy/heavy",
        "OUROBOROS_SUBAGENTS": single_model_subagents_setting("decoy/actor"),
    }), encoding="utf-8")
    out = tmp_path / "run"
    out.mkdir()
    derived = derive_run_settings(str(template), out, "openai/gpt-5.6-sol", 10.0, 5.0)
    payload = json.loads(derived.read_text(encoding="utf-8"))
    assert _only_target(payload["OUROBOROS_SUBAGENTS"]) == "openai/gpt-5.6-sol"
    assert "OUROBOROS_MODEL_HEAVY" not in payload


def test_swe_pro_all_resume_manifest_keeps_exact_actor_and_slots(tmp_path, monkeypatch):
    from devtools.benchmarks.swe_bench_pro.e1v2 import run_pro

    out = tmp_path / "run"
    ids = ["inst__a", "inst__b"]
    for cid in ids:
        task_dir = out / cid
        task_dir.mkdir(parents=True)
        (task_dir / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({
            "OUROBOROS_MODEL": "decoy/template",
            "OUROBOROS_MODEL_HEAVY": "decoy/heavy",
        }),
        encoding="utf-8",
    )
    args = types.SimpleNamespace(
        full_set=True,
        csv="",
        start=1,
        limit=2,
        allow_dirty_seed=True,
        solve_timeout=0,
        settings=str(settings),
        solve_model="openai/gpt-5.6-sol",
        self_improve=False,
        cadence="off",
        reset_state=False,
        model_name="ouroboros-test",
        review_slots=1,
        review_effort="",
        runtime_mode="",
        image_input_mode="",
        total_budget=100.0,
        per_task_cost=10.0,
        pretask_evolution=False,
        pause_on_api_err=-1,
    )
    row = {"dockerhub_tag": "unused"}
    monkeypatch.setattr(run_pro, "read_full_order", lambda: ids)
    monkeypatch.setattr(run_pro, "load_pro_rows", lambda selected: {i: row for i in selected})
    monkeypatch.setattr(run_pro, "assert_seed_is_git_directory", lambda _path: None)
    monkeypatch.setattr(run_pro, "ensure_util_image", lambda: None)
    monkeypatch.setattr(
        run_pro,
        "derive_run_settings",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("resume derived settings")),
    )
    monkeypatch.setattr(
        run_pro.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    assert run_pro._run_schedule(args, out, "", "key") == 0
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["model_slots"]["OUROBOROS_MODEL"] == "openai/gpt-5.6-sol"
    assert "OUROBOROS_MODEL_HEAVY" not in manifest["model_slots"]
    assert _only_target(json.dumps(manifest["available_subagents"])) == (
        "openai/gpt-5.6-sol"
    )


def test_editbench_seed_disables_but_records_effective_main_actor(tmp_path, monkeypatch):
    from devtools.benchmarks.editbench import run_editbench

    fake_home = tmp_path / "home"
    (fake_home / "Ouroboros" / "data").mkdir(parents=True)
    (fake_home / "Ouroboros" / "data" / "settings.json").write_text(
        json.dumps({"OUROBOROS_MODEL": "anthropic/claude-fable-5"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(run_editbench.pathlib.Path, "home", lambda: fake_home)
    settings_path = run_editbench._seed_settings(tmp_path / "data")
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    config = parse_configured_subagents(payload["OUROBOROS_SUBAGENTS"])
    assert config.enabled is False
    assert len(config.items) == 1
    assert config.items[0].route.target_id == payload["OUROBOROS_MODEL"]
    assert payload["OUROBOROS_MODEL"] == "anthropic/claude-fable-5"
    assert "OUROBOROS_MODEL_HEAVY" not in payload
