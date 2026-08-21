from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_contributor_flow_is_agent_first_and_route_neutral():
    guide = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "against lowercase `ouroboros`, not `main` or `ouroboros-stable`" in guide
    assert "Coding agents and people must read [CONTRIBUTING.md]" in readme
    for path in (
        "BIBLE.md", "docs/ARCHITECTURE.md", "docs/DEVELOPMENT.md",
        "docs/CHECKLISTS.md",
    ):
        assert path in guide
    assert "read these files **in full**" in guide
    assert "separate agent context" in guide
    assert "Reviewing in the authoring conversation does not count" in guide
    assert "Mark the review `NOT_RUN`" in guide
    assert "--contributor" in guide
    assert "--base-ref upstream/ouroboros" in guide
    assert "--head-ref HEAD" in guide
    assert "review-packet.zip" in guide
    assert "evidence, not a promise to merge" in guide
    assert "OpenRouter" not in guide


def test_pull_request_template_has_one_universal_agent_review_block():
    template = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(
        encoding="utf-8"
    )

    assert "The PR base branch is `ouroboros`" in template
    assert "I did **not** bump `VERSION`" in template
    assert template.count("## Review evidence") == 1
    assert "Authoring agent/context" in template
    assert "Separate review agent/context" in template
    assert "Reviewer model and effort (when exposed)" in template
    assert "Reviewed base SHA" in template
    assert "Reviewed head SHA" in template
    assert "Findings and disposition" in template
    assert "coverage limitations" in template
    assert "PASS`, `NEEDS_CHANGES`, `INCOMPLETE`, or `NOT_RUN" in template
    assert "If not run, reason" in template
    assert "self-review in the\nauthoring conversation does not" in template
    assert "Agent assistance (optional)" not in template
    assert "Human verification" not in template
    assert "Triad verdict" not in template
    assert "Scope verdict" not in template
    assert "OpenRouter" not in template


def test_missing_session_model_is_labelled_absent_in_evidence():
    from scripts.contributor_review_evidence import _session_evidence

    receipt = {"model_verification": "not_requested"}
    _session_evidence(
        surface="triad", slot_id="slot-1",
        route={"target_id": "codex=gpt-5.6-sol"}, status="responded",
        observed_model="", usage={"delegated_route": "codex"}, transcript="",
        deltas=[], receipt=receipt, mismatches=[],
    )

    assert receipt["model_verification"] == "absent"


def test_pull_request_ci_is_fork_safe_and_does_not_enable_provider_jobs():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    quick_job = workflow.partition("\n  quick-test:\n")[2].partition(
        "\n  # ──────────────────────────────────────────────────────────────────"
    )[0]

    assert "pull_request:\n    branches: [ouroboros]" in workflow
    assert "\n  pull_request_target:" not in workflow
    assert "\n  schedule:" not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "github.event_name == 'pull_request' && github.base_ref == 'ouroboros'" in workflow
    assert "secrets." not in quick_job
    assert "release:\n" in workflow
    assert "      contents: write" in workflow


def test_trusted_provider_ci_wires_full_secret_policy_and_release_dependency():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    integration_job = workflow.partition("\n  integration-test:\n")[2].partition(
        "\n  # ──────────────────────────────────────────────────────────────────"
    )[0]
    release_preflight = workflow.partition("\n  release-preflight:\n")[2].partition(
        "\n  build:\n"
    )[0]

    assert integration_job
    assert "github.event_name == 'pull_request'" not in integration_job
    assert "github.event_name == 'workflow_dispatch'" in integration_job
    for ref in (
        "refs/heads/main",
        "refs/heads/ouroboros",
        "refs/heads/ouroboros-stable",
        "refs/tags/v",
    ):
        assert ref in integration_job

    for secret in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "MINIMAX_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "GIGACHAT_CREDENTIALS",
    ):
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in integration_job

    assert " -rs " in integration_job
    assert "needs: [full-test, integration-test]" in release_preflight


def test_repository_has_explicit_mit_license_holder():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 Anton Razzhigaev" in license_text
    assert "Andrew Kaznacheev" not in license_text
