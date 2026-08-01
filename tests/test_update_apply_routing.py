"""Managed-update protected-path routing (v6.88.0).

The gate exists to keep the AGENT from authoring changes to protected surfaces. Before this
release it also blocked the SUPERVISOR-authored clean merge, which made autoupdate unreachable:
measured over this repo's tag history, 39/39 real update spans routed to `manual` because almost
every release touches a frozen-contract or release-invariant file. These tests pin the scoped
rule and the fail-closed edges.
"""

import asyncio

import pytest

from ouroboros.gateway import control


def _plan(**over):
    plan = {
        "available": True,
        "kind": "clean",
        "local_dirty_count": 0,
        "base_sha": "aaaa",
        "target_sha": "bbbb",
        "protected_conflict_paths": [],
        "code_conflict_paths": [],
        "doc_conflict_paths": [],
    }
    plan.update(over)
    return plan


@pytest.fixture
def official_delta(monkeypatch):
    """Drive `_official_protected_hits` by faking the read-only `git diff base..target`."""
    def _set(paths):
        import supervisor.git_ops as git_ops

        monkeypatch.setattr(
            git_ops, "git_capture", lambda *_a, **_k: (0, "\n".join(paths), ""), raising=True
        )
    return _set


# --- the supervisor-authored predicate -------------------------------------------------------

def test_pure_clean_auto_merge_is_supervisor_authored():
    assert control._supervisor_authored_clean_merge(_plan(), "auto_merge") is True


@pytest.mark.parametrize("plan_over, strategy", [
    ({}, "assisted"),                                   # agent resolves + commits
    ({}, "doc_reconcile"),                              # agent resolves + commits
    ({"kind": "conflicting"}, "auto_merge"),            # degrades into assisted
    ({"local_dirty_count": 3}, "auto_merge"),           # uncommitted work -> reviewed path
])
def test_agent_authored_branches_are_not_supervisor_authored(plan_over, strategy):
    assert control._supervisor_authored_clean_merge(_plan(**plan_over), strategy) is False


@pytest.mark.parametrize("plan_over", [
    {"local_dirty_count": "not-a-number"},                       # unreadable count
    {"protected_conflict_paths": ["BIBLE.md"]},                  # contradicts kind=="clean"
    {"code_conflict_paths": ["ouroboros/loop.py"]},
    {"doc_conflict_paths": ["README.md"]},
])
def test_inconsistent_clean_plan_fails_closed(plan_over):
    """`classify_conflicts` defines kind=="clean" as having NO unmerged paths, so a plan that
    claims both is not trusted into the exempt branch."""
    assert control._supervisor_authored_clean_merge(_plan(**plan_over), "auto_merge") is False


# --- the category-scoped block ---------------------------------------------------------------

def test_clean_delta_release_invariant_does_not_block_auto_merge(official_delta):
    """The owner's real case: v6.87.1 -> v6.87.3, whose only protected hit is the release
    invariant supervisor/update_merge.py in a clean delta."""
    official_delta(["supervisor/update_merge.py", "ouroboros/loop.py"])
    assert control._managed_update_protected_block(_plan(), "auto_merge") == []


def test_clean_delta_frozen_contract_does_not_block_auto_merge(official_delta):
    official_delta(["ouroboros/gateway/contracts.py", "docs/CHECKLISTS.md"])
    assert control._managed_update_protected_block(_plan(), "auto_merge") == []


@pytest.mark.parametrize("path", [
    "BIBLE.md",
    "prompts/SAFETY.md",
    "ouroboros/safety.py",
    "ouroboros/runtime_mode_policy.py",
    "ouroboros/tools/registry.py",
])
def test_safety_critical_always_blocks(official_delta, path):
    """Safety-critical changes reach the owner's eyes on EVERY strategy, whoever authored them."""
    official_delta([path, "ouroboros/loop.py"])
    for strategy in ("auto_merge", "assisted", "doc_reconcile"):
        assert control._managed_update_protected_block(_plan(), strategy) == [path], strategy


def test_safety_critical_blocks_even_beside_exempt_tiers(official_delta):
    official_delta(["ouroboros/safety.py", "supervisor/update_merge.py"])
    assert control._managed_update_protected_block(_plan(), "auto_merge") == ["ouroboros/safety.py"]


def test_exempt_tiers_still_block_the_agent_authored_paths(official_delta):
    """Unchanged behavior where the agent resolves markers and lands its OWN commit."""
    official_delta(["supervisor/update_merge.py"])
    for strategy in ("assisted", "doc_reconcile"):
        assert control._managed_update_protected_block(_plan(), strategy) == [
            "supervisor/update_merge.py"
        ], strategy
    dirty = _plan(local_dirty_count=2)
    assert control._managed_update_protected_block(dirty, "auto_merge") == [
        "supervisor/update_merge.py"
    ]
    conflicting = _plan(kind="conflicting", code_conflict_paths=["ouroboros/loop.py"])
    assert control._managed_update_protected_block(conflicting, "auto_merge") == [
        "supervisor/update_merge.py"
    ]


def test_unrecognized_protected_path_is_not_exempted(official_delta, monkeypatch):
    """Fail-closed: a path the categorizer does not place in a known tier keeps blocking."""
    official_delta(["some/odd/protected_doc.md"])
    monkeypatch.setattr(control, "_is_protected_for_managed_update", lambda _p: True)
    import ouroboros.runtime_mode_policy as policy

    monkeypatch.setattr(policy, "protected_path_category", lambda _p: "")
    assert control._managed_update_protected_block(_plan(), "auto_merge") == [
        "some/odd/protected_doc.md"
    ]


# --- the no-side-effect boundary -------------------------------------------------------------

def test_protected_manual_routing_has_no_side_effects(official_delta, monkeypatch):
    """A protected rejection must happen BEFORE workers are stopped or anything is staged, so a
    read-only handoff never interrupts active tasks or mutates the worktree."""
    official_delta(["ouroboros/safety.py"])
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge", lambda **_k: _plan(), raising=True
    )
    called = []
    for mod, name in (
        (workers, "kill_workers"),
        (update_merge, "acquire_update_lock"),
        (update_merge, "apply_managed_merge_update"),
        (update_merge, "materialize_assisted_merge_live"),
        (update_merge, "write_update_tx"),
    ):
        monkeypatch.setattr(
            mod, name, lambda *_a, _n=name, **_k: called.append(_n), raising=True
        )

    resp = asyncio.run(control._apply_managed_merge(None, "auto_merge"))

    assert resp.status_code == 200
    import json

    body = json.loads(resp.body)
    assert body["status"] == "manual"
    assert body["reason"] == "protected_paths"
    assert body["protected_paths"] == ["ouroboros/safety.py"]
    assert "merge_plan" in body  # response shape consumed by web/modules/update_status.js
    assert called == [], f"protected routing must not mutate anything, called: {called}"


def test_preflight_reports_the_route_the_apply_gate_will_take(official_delta, monkeypatch):
    """The dialog must never offer an action the backend then refuses."""
    official_delta(["ouroboros/safety.py"])
    import supervisor.update_merge as update_merge

    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge", lambda **_k: _plan(), raising=True
    )
    import json

    body = json.loads(asyncio.run(control.api_update_preflight(None)).body)
    route = body["protected_route"]
    assert route["offered_strategy"] == "auto_merge"
    assert route["will_route_manual"] is True
    assert route["protected_paths"] == ["ouroboros/safety.py"]


def test_preflight_clears_the_route_for_an_exempt_clean_delta(official_delta, monkeypatch):
    official_delta(["supervisor/update_merge.py"])
    import supervisor.update_merge as update_merge

    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge", lambda **_k: _plan(), raising=True
    )
    import json

    body = json.loads(asyncio.run(control.api_update_preflight(None)).body)
    assert body["protected_route"]["will_route_manual"] is False
    assert body["protected_route"]["protected_paths"] == []
