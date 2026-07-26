"""Provenance contracts for benchmark artefacts (v6.81.0).

Two claims a benchmark artefact must never make falsely:

* FIX A — a container carries only the provider credentials the run DECLARED, and the
  manifest discloses which ones it got (by fingerprint, never by value);
* FIX B — a task the RUNTIME stopped for a reason other than finishing (the per-task USD
  reservation rail, a round cap) says so in the artefact, instead of being indistinguishable
  from an honest failure.
"""
from __future__ import annotations

import json

from devtools.benchmarks.common.manifests import provider_credential_disclosure
from devtools.benchmarks.common.result_index import (
    runtime_terminal_disclosure,
    task_result_row,
)
from devtools.benchmarks.common.secrets import (
    credential_disclosure,
    isolated_credential_grants,
)
from devtools.benchmarks.common.server_runner import build_isolated_settings
from ouroboros.provider_models import (
    PROVIDER_CREDENTIAL_GROUPS,
    PROVIDER_PREFIXES,
    credential_keys_for_providers,
    provider_credential_plan,
)

# A live settings file carrying EVERY provider credential the owner has configured. This is
# the realistic shape: the owner's file accumulates keys over time, and which of them a
# benchmark container could reach used to be a function of that accumulation.
_LIVE = {
    "OUROBOROS_MODEL": "anthropic/claude-sonnet-5",
    "OUROBOROS_MODEL_HEAVY": "claude-opus-4.8",
    "OUROBOROS_MODEL_LIGHT": "anthropic/claude-sonnet-4.6",
    "OUROBOROS_MODEL_FALLBACKS": "openai/gpt-5.5",
    "OUROBOROS_REVIEW_MODELS": "anthropic/claude-fable-5,openai/gpt-5.6-sol",
    "OPENROUTER_API_KEY": "or-value",
    "OPENAI_API_KEY": "oa-value",
    "OPENAI_BASE_URL": "https://compat.example/v1",
    "ANTHROPIC_API_KEY": "an-value",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY": "cr-value",
    "CLOUDRU_FOUNDATION_MODELS_BASE_URL": "https://cloudru.example/v1",
    "OPENAI_COMPATIBLE_API_KEY": "compat-value",
    "GIGACHAT_CREDENTIALS": "gc-value",
    "GIGACHAT_PASSWORD": "gp-value",
    "GITHUB_TOKEN": "gh-value",
    "OUROBOROS_NETWORK_PASSWORD": "np-value",
    "TELEGRAM_BOT_TOKEN": "tg-value",
    "TOTAL_BUDGET": 100.0,
}


# --------------------------------------------------------------------------- FIX A


def test_isolated_settings_grant_only_the_declared_providers_credentials():
    """A run pinned to OpenRouter must not receive the owner's DIRECT provider keys.

    Owner/control secrets were already excluded and still are. The defect was narrower: every
    provider credential in the live file was copied regardless of which providers the run
    declared, so a routing fallback could spend outside the declared bucket while the manifest
    said otherwise — and the reachable provider set was a function of whatever happened to be
    in the live file at launch, which makes two nominally identical runs differ invisibly.
    """
    out = build_isolated_settings(_LIVE, OUROBOROS_RUNTIME_MODE="advanced")

    assert out["OPENROUTER_API_KEY"] == "or-value"      # every declared slot routes here
    assert out["ANTHROPIC_API_KEY"] == "an-value"       # CLAUDE_CODE_MODEL's SDK transport
    for never in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_COMPATIBLE_API_KEY",
                  "CLOUDRU_FOUNDATION_MODELS_API_KEY", "CLOUDRU_FOUNDATION_MODELS_BASE_URL",
                  "GIGACHAT_CREDENTIALS", "GIGACHAT_PASSWORD"):
        assert never not in out, f"{never} was not declared by any model slot"
    # Unchanged: owner/control and transport secrets were never copied and must stay out.
    for owner_secret in ("GITHUB_TOKEN", "OUROBOROS_NETWORK_PASSWORD", "TELEGRAM_BOT_TOKEN"):
        assert owner_secret not in out


def test_declaring_a_direct_provider_slot_grants_exactly_that_provider():
    """The mirror: a run that DOES declare a direct lane must still be able to authenticate.

    Fail-closed in the wrong direction is worse than a spare key — a benchmark that dies on a
    missing credential at hour six burns the whole schedule.
    """
    cloudru = build_isolated_settings(_LIVE, OUROBOROS_MODEL="cloudru::zai-org/GLM-4.7")
    assert cloudru["CLOUDRU_FOUNDATION_MODELS_API_KEY"] == "cr-value"
    assert "GIGACHAT_CREDENTIALS" not in cloudru

    compat = build_isolated_settings(_LIVE, OUROBOROS_MODEL="openai-compatible::local-llm")
    assert compat["OPENAI_COMPATIBLE_API_KEY"] == "compat-value"
    # The openai-compatible lane legitimately falls back to the legacy OPENAI_* pair.
    assert compat["OPENAI_API_KEY"] == "oa-value"
    assert compat["OPENAI_BASE_URL"] == "https://compat.example/v1"


def test_paired_credentials_travel_together_or_not_at_all():
    """GigaChat needs CREDENTIALS *or* USER+PASSWORD plus its endpoint; Cloud.ru needs its
    base_url. A key without the fields it is useless without is a broken grant, and the
    `GIGACHAT_` blanket prefix used to smuggle exactly half of one in unconditionally."""
    from devtools.benchmarks.common.server_runner import _ISO_SETTINGS_ALLOW_PREFIX

    assert "GIGACHAT_" not in _ISO_SETTINGS_ALLOW_PREFIX, \
        "the GigaChat family must be gated on the declared slots, not copied by prefix"

    giga = build_isolated_settings(_LIVE, OUROBOROS_MODEL="gigachat::GigaChat-3-Ultra")
    assert giga["GIGACHAT_CREDENTIALS"] == "gc-value"
    assert giga["GIGACHAT_PASSWORD"] == "gp-value"

    without = build_isolated_settings(_LIVE)
    assert "GIGACHAT_CREDENTIALS" not in without and "GIGACHAT_PASSWORD" not in without


def test_credential_groups_cover_every_routable_provider():
    """Drift guard. `provider_for_model` can only return a provider from PROVIDER_PREFIXES;
    a new one without a credential group would silently grant nothing."""
    for _prefix, provider in PROVIDER_PREFIXES:
        assert provider in PROVIDER_CREDENTIAL_GROUPS, provider
    assert credential_keys_for_providers(["openrouter"]) == ("OPENROUTER_API_KEY",)


def test_a_settings_mapping_with_no_slots_fails_OPEN_and_discloses_it(monkeypatch):
    """No resolvable slot at all must not mean "no credentials" — that kills a run outright.

    Ambiguity resolves toward carrying a spare, never toward removing one, and the escape is
    taken OPENLY: `fail_open` rides in the record so an auditor is not left reading a full
    credential list as if the slots had asked for it.
    """
    import ouroboros.provider_models as pm

    # Realistic case: SETTINGS_DEFAULTS fill the empty slots, so this still resolves narrowly.
    plan = provider_credential_plan({"OUROBOROS_MODEL": "", "CLAUDE_CODE_MODEL": ""})
    assert plan["fail_open"] is False and plan["planned_keys"]

    # Degenerate case: nothing resolvable at all.
    monkeypatch.setattr(pm, "declared_model_settings", lambda _settings: {})
    degenerate = provider_credential_plan({})
    assert degenerate["fail_open"] is True
    assert degenerate["planned_keys"] == sorted(pm.ALL_PROVIDER_CREDENTIAL_KEYS)


def test_manifest_discloses_granted_credentials_by_fingerprint_never_by_value(tmp_path):
    """Prevention without evidence is half a fix: the artefact must let an auditor see what
    the run could reach — and must never carry the value itself."""
    settings_path = tmp_path / "settings.json"
    out = build_isolated_settings(_LIVE)
    settings_path.write_text(json.dumps(out), encoding="utf-8")

    disclosure = provider_credential_disclosure(settings_path)
    assert disclosure["available"] is True
    assert sorted(disclosure["granted"]) == ["ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"]
    assert disclosure["granted"]["OPENROUTER_API_KEY"]["present"] is True
    assert disclosure["granted"]["OPENROUTER_API_KEY"]["fingerprint"].startswith("sha256:")
    assert disclosure["fail_open"] is False
    assert "openrouter" in disclosure["providers"]

    blob = json.dumps(disclosure)
    for value in ("or-value", "an-value", "oa-value", "gh-value"):
        assert value not in blob, "a disclosure must never carry a credential value"

    # The same key fingerprints identically across runs — that IS the audit question.
    assert (credential_disclosure({"OPENROUTER_API_KEY": "or-value"})["OPENROUTER_API_KEY"]
            == disclosure["granted"]["OPENROUTER_API_KEY"])
    # An absent settings path is a STATED gap, never a silently empty grant list.
    assert provider_credential_disclosure(tmp_path / "nope.json") == {
        "available": False, "reason": "settings_path_absent"}


def test_isolated_credential_grants_reports_the_file_not_the_intent():
    """`planned_keys` is the derivation; `granted` is the truth about the file. An explicit
    override the slots never asked for must be VISIBLE, not inferred away."""
    out = build_isolated_settings(_LIVE, ANTHROPIC_API_KEY="", OPENAI_API_KEY="forced")
    grants = isolated_credential_grants(out)
    assert "OPENAI_API_KEY" not in grants["planned_keys"]
    assert grants["granted"]["OPENAI_API_KEY"]["present"] is True


# --------------------------------------------------------------------------- FIX B

# The shape `GET /api/tasks/<id>` returns for a task the per-task USD reservation rail
# stopped: `usage_accounting.reserve_attempt` refuses, `loop._handle_budget_exceeded` stamps
# the reason and the resource-limit block, `task_results.write_task_result` persists both.
_BUDGET_TRUNCATED = {
    "status": "failed",
    "reason_code": "budget_exhausted",
    "total_rounds": 13,
    "loop_outcome": {
        "reason_code": "budget_exhausted",
        "resource_limit": {"status": "resource_limited", "scope": "root",
                           "resume_policy": "increase_or_reset_budget_then_retry"},
    },
    "outcome_axes": {"execution": {"status": "failed", "reason_code": "budget_exhausted"}},
}


def test_runtime_terminal_disclosure_names_a_cost_truncated_run():
    disclosed = runtime_terminal_disclosure(_BUDGET_TRUNCATED)
    assert disclosed["available"] is True
    assert disclosed["reason_code"] == "budget_exhausted"
    assert disclosed["truncated"] is True
    assert disclosed["resource_limit"]["scope"] == "root"
    assert disclosed["execution_reason_code"] == "budget_exhausted"


def test_runtime_terminal_disclosure_states_the_gap_instead_of_inventing_one():
    """A writer with no runtime result must say so — never a fabricated reason, never a
    silent absence a reader would mistake for "nothing to report"."""
    assert runtime_terminal_disclosure(None) == {"available": False}
    assert runtime_terminal_disclosure({}) == {"available": False}
    ok = runtime_terminal_disclosure({"status": "completed", "reason_code": "final_answer"})
    assert ok["available"] is True and ok["truncated"] is False


def test_task_result_row_publishes_the_runtime_reason_alongside_the_adapter_stage():
    """The two vocabularies are independent facts and BOTH must reach the ledger.

    An adapter honestly reports `completed`/`official_evaluate` — the evaluation really did
    run — while the runtime reports `budget_exhausted`. Publishing only the former is how an
    aggregator records 2/3 with no indication that a third of the run was cost-truncated.
    """
    row = task_result_row(
        benchmark="osworld", instance_id="chrome/abc", status="completed",
        reason_code="official_evaluate", official_eval_status="completed",
        runtime_result=_BUDGET_TRUNCATED, details={"reward": 0.0},
    )
    assert row["status"] == "completed"                      # unchanged: not demoted
    assert row["official_eval_status"] == "completed"        # unchanged: the eval DID run
    assert row["reason_code"] == "official_evaluate"         # unchanged: adapter stage
    assert row["runtime_outcome"]["reason_code"] == "budget_exhausted"
    assert row["runtime_outcome"]["truncated"] is True

    # Every row carries the field, so an auditor never has to guess whether it was omitted
    # because nothing happened or because the writer forgot.
    assert task_result_row(benchmark="gaia", instance_id="x",
                           status="failed")["runtime_outcome"] == {"available": False}
