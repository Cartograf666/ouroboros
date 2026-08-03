"""Phase 5.8: the advisory review's delegated route and the four key sites.

The ANTHROPIC_API_KEY checks are ROUTE-DEPENDENT: an api route requires the key
byte-for-byte as before, and the delegated route runs without it — most
importantly, the constitutional pre-commit gate RUNS on the delegated route
instead of recording a routine-looking "auto-bypassed".

Offline fixtures throughout (owner test rule): the FakeGateway from the
agent-session route tests stands in for the Claudexor control plane.
"""

import json
import subprocess

import pytest

import ouroboros.tools.claude_advisory_review as advisory
from tests.test_review_agent_session_route import FakeGateway, _terminal_detail


@pytest.fixture()
def fake_route(monkeypatch):
    from ouroboros import delegate_custody as custody

    FakeGateway.reset()
    monkeypatch.setattr("ouroboros.gateways.claudexor.ClaudexorGateway", FakeGateway)
    monkeypatch.setenv("OUROBOROS_REVIEW_SESSION_ROUTE", "fake-review=fake-small:low")
    custody._CUSTODY.clear()
    return FakeGateway


def _ctx(tmp_path):
    from ouroboros.tools.registry import ToolContext

    repo = tmp_path / "repo"
    drive = tmp_path / "data"
    repo.mkdir(exist_ok=True)
    drive.mkdir(exist_ok=True)
    return ToolContext(repo_dir=repo, drive_root=drive)


_ADVISORY_ITEMS = json.dumps([
    {"item": "correctness", "verdict": "PASS", "severity": "advisory",
     "reason": "checked the change end to end"},
])


# ---------------------------------------------------------------------------
# Site 1 — the run_readonly entry check
# ---------------------------------------------------------------------------


def test_api_route_without_key_errors_exactly_as_before(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv(advisory.ADVISORY_REVIEW_ROUTE_ENV, raising=False)
    ctx = _ctx(tmp_path)
    items, raw, model, chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert items == [] and model == "" and chars == 0
    assert raw.startswith("⚠️ ADVISORY_ERROR: ANTHROPIC_API_KEY not set")


def test_delegated_route_runs_without_the_key(tmp_path, monkeypatch, fake_route):
    """The whole free-route walk: no key anywhere, route=agent_session, and the
    advisory runs as a delegated session whose checklist comes back parsed."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv(advisory.ADVISORY_REVIEW_ROUTE_ENV, "agent_session")
    fake_route.catalog_entry["json_schema_output"] = False
    fake_route.detail = _terminal_detail(_ADVISORY_ITEMS)
    ctx = _ctx(tmp_path)
    items, raw, model, chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert not raw.startswith("⚠️ ADVISORY_ERROR")
    assert [i["item"] for i in items] == ["correctness"]
    assert model  # the effective session model/route is reported
    start = fake_route.instances[0].start_requests[0]
    assert start["authPreference"] == "subscription"
    assert start["access"] == "readonly"


def test_unknown_route_token_is_a_loud_error_not_a_transport_pick(tmp_path, monkeypatch):
    monkeypatch.setenv(advisory.ADVISORY_REVIEW_ROUTE_ENV, "codex")
    ctx = _ctx(tmp_path)
    items, raw, _model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert items == []
    assert raw.startswith("⚠️ ADVISORY_ERROR") and "codex" in raw


# ---------------------------------------------------------------------------
# Site 3 — the constitutional pre-commit gate's auto-bypass
# ---------------------------------------------------------------------------


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def test_missing_key_auto_bypasses_only_on_the_api_route(tmp_path, monkeypatch):
    """THE dangerous site: on the api route a missing key still auto-bypasses
    with the audited record, byte-compatible with today; on the delegated route
    the gate RUNS — the advisory is actually invoked and no bypass is recorded."""
    from ouroboros.tools.registry import ToolContext

    repo = _git_repo(tmp_path)
    (repo / "README.md").write_text("hello\nchanged\n", encoding="utf-8")  # a real diff to review
    drive = tmp_path / "data"
    drive.mkdir(exist_ok=True)
    ctx = ToolContext(repo_dir=repo, drive_root=drive)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # API route: auto-bypass, exactly as today (with the route named).
    monkeypatch.delenv(advisory.ADVISORY_REVIEW_ROUTE_ENV, raising=False)
    payload = json.loads(advisory._handle_advisory_pre_review(
        ctx, commit_message="m", skip_tests=True,
    ))
    assert payload["status"] == "bypassed"
    assert "auto-bypassed" in payload["bypass_reason"]
    assert "agent_session" in payload["message"]  # the keyless path is named

    # Delegated route: the gate RUNS instead of bypassing. The downstream
    # deterministic pre-SDK gate (P9 metadata preflight, test preflight) and
    # the transport are not under test here and are stubbed; the site under
    # test — the auto-bypass — sits UPSTREAM of both.
    called = {}

    def _capture(repo_dir, commit_message, ctx_, goal="", scope="", paths=None, options=None):
        called["ran"] = True
        return [], "⚠️ ADVISORY_ERROR: sentinel — transport not under test here", "", 0

    monkeypatch.setattr(advisory, "_run_claude_advisory", _capture)
    monkeypatch.setattr(advisory, "_advisory_pre_sdk_gate",
                        lambda **_kwargs: ([], "README.md", None))
    monkeypatch.setenv(advisory.ADVISORY_REVIEW_ROUTE_ENV, "agent_session")
    payload = json.loads(advisory._handle_advisory_pre_review(
        ctx, commit_message="m", skip_tests=True,
    ))
    assert called.get("ran") is True
    assert payload["status"] != "bypassed"
    # No auto-bypass row was recorded for the delegated attempt.
    events = (drive / "logs" / "events.jsonl")
    if events.exists():
        rows = [json.loads(line) for line in events.read_text().splitlines() if line.strip()]
        assert not any(
            r.get("type") == "advisory_review_bypassed"
            and "auto-bypassed" in str(r.get("bypass_reason"))
            and "route=api" not in str(r.get("bypass_reason"))
            for r in rows
        )
        api_bypasses = [r for r in rows if r.get("type") == "advisory_review_bypassed"]
        assert len(api_bypasses) == 1  # only the api-route attempt above


def test_explicit_skip_still_bypasses_on_the_delegated_route(tmp_path, monkeypatch):
    """The owner's explicit audited bypass is route-independent and untouched."""
    from ouroboros.tools.registry import ToolContext

    repo = _git_repo(tmp_path)
    drive = tmp_path / "data"
    drive.mkdir(exist_ok=True)
    ctx = ToolContext(repo_dir=repo, drive_root=drive)
    monkeypatch.setenv(advisory.ADVISORY_REVIEW_ROUTE_ENV, "agent_session")
    payload = json.loads(advisory._handle_advisory_pre_review(
        ctx, commit_message="m", skip_advisory_review=True, skip_tests=True,
    ))
    assert payload["status"] == "bypassed"
    assert payload["bypass_reason"] == "explicit skip_advisory_review=True"


# ---------------------------------------------------------------------------
# The route reader itself
# ---------------------------------------------------------------------------


def test_advisory_route_reader_vocabulary(monkeypatch):
    monkeypatch.delenv(advisory.ADVISORY_REVIEW_ROUTE_ENV, raising=False)
    assert advisory.advisory_review_route() == "api"
    assert advisory.advisory_route_requires_api_key() is True
    monkeypatch.setenv(advisory.ADVISORY_REVIEW_ROUTE_ENV, "agent_session")
    assert advisory.advisory_review_route() == "agent_session"
    assert advisory.advisory_route_requires_api_key() is False
    monkeypatch.setenv(advisory.ADVISORY_REVIEW_ROUTE_ENV, "cursor")
    with pytest.raises(ValueError):
        advisory.advisory_review_route()
