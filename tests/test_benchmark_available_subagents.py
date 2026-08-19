"""Fixed-model benchmark profiles use the canonical Available-subagents wire."""

from __future__ import annotations

import json
import pathlib

import pytest

from devtools.benchmarks.common.manifests import model_slot_snapshot
from devtools.benchmarks.common.model_slots import (
    configured_subagents_snapshot,
    disabled_subagents_setting,
    pin_single_model,
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


def test_legacy_heavy_run_manifest_reader_keeps_historical_meaning(tmp_path):
    old = tmp_path / "old-settings.json"
    old.write_text(json.dumps({"OUROBOROS_MODEL_HEAVY": "legacy/measured"}), encoding="utf-8")
    assert (
        model_slot_snapshot(old, env_overrides=False)["OUROBOROS_MODEL_HEAVY"]
        == "legacy/measured"
    )


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


def test_editbench_seed_explicitly_disables_available_subagents(tmp_path, monkeypatch):
    from devtools.benchmarks.editbench import run_editbench

    fake_home = tmp_path / "home"
    (fake_home / "Ouroboros" / "data").mkdir(parents=True)
    monkeypatch.setattr(run_editbench.pathlib.Path, "home", lambda: fake_home)
    settings_path = run_editbench._seed_settings(tmp_path / "data", model="openai/gpt-5.5")
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    config = parse_configured_subagents(payload["OUROBOROS_SUBAGENTS"])
    assert config.enabled is False
    assert config.items == ()
    assert "OUROBOROS_MODEL_HEAVY" not in payload
