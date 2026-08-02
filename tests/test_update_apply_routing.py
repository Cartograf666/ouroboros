"""Managed-update protected-path routing (v6.88.0) and its fail-closed hardening (v6.88.1).

The gate exists to keep the AGENT from authoring changes to protected surfaces. Before v6.88.0 it
also blocked the SUPERVISOR-authored clean merge, which made autoupdate unreachable: measured over
this repo's 113 consecutive one-release-behind tag spans, the old rule routed 55 of them to
`manual` and the scoped rule routes 40 — longer spans still mostly block, because safety-critical
files change often and the owner is meant to see those.

These tests pin the scoped rule, the fail-closed edges (an unreadable dirty count, an unverifiable
official delta), and the v6.88.1 gate on the replace/stash/force family: a change to any protected
tier other than frozen-contract / release-invariant (safety-critical or unrecognized) is disclosed
and only overridable by an owner acknowledgement BOUND to the exact SHAs and paths disclosed,
audited before anything is prepared, and never available for a delta we could not read.
"""

import asyncio
import json
import os
import pathlib

import pytest

from ouroboros.gateway import control


class _Request:
    """Minimal stand-in for the Starlette request `api_update_apply` reads a JSON body from."""

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


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


@pytest.fixture(autouse=True)
def _the_fence_leaves_process_wide_state_behind():
    """The fence deliberately latches PROCESS-wide state that outlives the request, and both pieces
    would otherwise leak into every later test in this interpreter:

    * the in-process-writer admission flag, re-opened in production by
      `_respawn_workers_after_failed_update` — which most tests here stub out to observe the call,
      so without this teardown the first fenced test locks direct chat out for good;
    * the unproven-survivor latch, whose whole contract is that it survives until a restart, so a
      test that produces a survivor would refuse every later apply.

    The module is resolved BEFORE the yield, and the captured object is what the cleanup uses. The
    fence's cannot-reach-the-worker-module pin installs an unreachable stub at
    ``sys.modules['supervisor']`` via monkeypatch, and pytest builds the fixture closure as
    [autouse..., argnames...] — conftest's own autouse `_hide_bundled_skills` REQUESTS `monkeypatch`,
    which pulls it ahead of this fixture and therefore finalizes it LAST. An import in the teardown
    body would run against the stub and raise, erroring the whole suite behind a passing test.
    Capturing at setup time (when `sys.modules` still holds the real package) makes the cleanup
    independent of finalizer ordering."""
    from supervisor import workers

    yield
    workers.open_repo_writer_admission()
    workers._ADMITTED_REPO_WRITERS.clear()
    workers._UNPROVEN_WORKER_SURVIVORS.clear()
    workers.clear_repo_writer_blockers()


@pytest.fixture(autouse=True)
def _hermetic_custody_ledger(monkeypatch, tmp_path):
    """Point the durable custody ledger at a per-test file.

    The fence now sweeps that ledger for repository-writing services (the ones a pooled worker may
    have started, which live in no registry this process can see). Left unpatched, every fence test
    in this module would read the DEVELOPER'S real `data/state/process_ledger.jsonl` — and, worse,
    send SIGKILL to whatever it happens to list. Returned so the tests that want an entry can write
    one; the default is an absent file, which is the state the pre-existing fence pins assume."""
    import ouroboros.process_custody as process_custody

    path = tmp_path / "process_ledger.jsonl"
    monkeypatch.setattr(process_custody, "ledger_path", lambda _root: path, raising=True)
    return path


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


@pytest.mark.parametrize("dirty", [-1, True, False, "0", 0.0, None])
def test_only_a_real_integer_zero_proves_a_clean_worktree(dirty):
    """D2 (v6.88.1): the exemption fast-commits local history WITHOUT review, so it may only be
    unlocked by a count the plan actually PROVES to be zero. `plan_managed_update_merge` always
    emits a real `int`, so every other shape is a degraded or forged plan."""
    plan = _plan(local_dirty_count=dirty)
    assert control._plan_worktree_is_clean(plan) is False
    assert control._supervisor_authored_clean_merge(plan, "auto_merge") is False


def test_missing_dirty_count_is_not_treated_as_clean():
    plan = _plan()
    plan.pop("local_dirty_count")
    assert control._plan_worktree_is_clean(plan) is False
    assert control._supervisor_authored_clean_merge(plan, "auto_merge") is False


# --- the category-scoped block ---------------------------------------------------------------

def test_clean_delta_release_invariant_does_not_block_auto_merge(official_delta):
    """The owner's real case: v6.87.1 -> v6.87.3, whose only protected hit is the release
    invariant supervisor/update_merge.py in a clean delta."""
    official_delta(["supervisor/update_merge.py", "ouroboros/loop.py"])
    assert control._managed_update_protected_block(_plan(), "auto_merge") == ([], "")


def test_clean_delta_frozen_contract_does_not_block_auto_merge(official_delta):
    official_delta(["ouroboros/gateway/contracts.py", "docs/CHECKLISTS.md"])
    assert control._managed_update_protected_block(_plan(), "auto_merge") == ([], "")


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
        assert control._managed_update_protected_block(_plan(), strategy) == (
            [path], "protected_paths"
        ), strategy


def test_safety_critical_blocks_even_beside_exempt_tiers(official_delta):
    official_delta(["ouroboros/safety.py", "supervisor/update_merge.py"])
    assert control._managed_update_protected_block(_plan(), "auto_merge") == (
        ["ouroboros/safety.py"], "protected_paths"
    )


def test_exempt_tiers_still_block_the_agent_authored_paths(official_delta):
    """Unchanged behavior where the agent resolves markers and lands its OWN commit."""
    official_delta(["supervisor/update_merge.py"])
    blocked = (["supervisor/update_merge.py"], "protected_paths")
    for strategy in ("assisted", "doc_reconcile"):
        assert control._managed_update_protected_block(_plan(), strategy) == blocked, strategy
    dirty = _plan(local_dirty_count=2)
    assert control._managed_update_protected_block(dirty, "auto_merge") == blocked
    conflicting = _plan(kind="conflicting", code_conflict_paths=["ouroboros/loop.py"])
    assert control._managed_update_protected_block(conflicting, "auto_merge") == blocked


def test_unrecognized_protected_path_is_not_exempted(official_delta, monkeypatch):
    """Fail-closed: a path the categorizer does not place in a known tier keeps blocking."""
    official_delta(["some/odd/protected_doc.md"])
    monkeypatch.setattr(control, "_is_protected_for_managed_update", lambda _p: True)
    import ouroboros.runtime_mode_policy as policy

    monkeypatch.setattr(policy, "protected_path_category", lambda _p: "")
    assert control._managed_update_protected_block(_plan(), "auto_merge") == (
        ["some/odd/protected_doc.md"], "protected_paths"
    )


# --- the unverifiable official delta (v6.88.1) ------------------------------------------------

def test_missing_sha_makes_the_official_delta_unverifiable(official_delta):
    """Without both SHAs we cannot read what the release touches, so an EMPTY hit list must not
    be read as "nothing protected changed" — not even for the otherwise-exempt tiers."""
    official_delta(["supervisor/update_merge.py"])
    assert control._official_protected_hits(_plan(base_sha="")) == ([], False)
    assert control._managed_update_protected_block(_plan(target_sha=""), "auto_merge") == (
        [], "protected_delta_unverifiable"
    )


def test_failing_delta_diff_is_unverifiable(monkeypatch):
    import supervisor.git_ops as git_ops

    monkeypatch.setattr(
        git_ops, "git_capture", lambda *_a, **_k: (128, "", "fatal: bad object"), raising=True
    )
    assert control._managed_update_protected_block(_plan(), "auto_merge") == (
        [], "protected_delta_unverifiable"
    )


def test_unverifiable_delta_still_reports_the_plans_own_conflict_paths(monkeypatch):
    import supervisor.git_ops as git_ops

    monkeypatch.setattr(git_ops, "git_capture", lambda *_a, **_k: (128, "", "boom"), raising=True)
    plan = _plan(kind="conflicting", protected_conflict_paths=["BIBLE.md"])
    assert control._managed_update_protected_block(plan, "assisted") == (
        ["BIBLE.md"], "protected_delta_unverifiable"
    )


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
    assert route["reason"] == "protected_paths"
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
    assert body["protected_route"]["reason"] == ""
    assert body["protected_route"]["protected_paths"] == []


@pytest.mark.parametrize("dirty", [-1, True, False, "0", 0.0, None, "not-a-number"])
def test_preflight_offers_assisted_for_a_malformed_dirty_count(official_delta, monkeypatch, dirty):
    """The dialog renders its primary action from `offered_strategy`, so that field must carry the
    same fail-closed answer as the apply gate: every dirty-count shape that is not a real integer
    zero is offered as ASSISTED, never as Auto-update the backend would then decline."""
    official_delta(["ouroboros/loop.py"])
    import supervisor.update_merge as update_merge

    monkeypatch.setattr(
        update_merge,
        "plan_managed_update_merge",
        lambda **_k: _plan(local_dirty_count=dirty),
        raising=True,
    )
    route = json.loads(asyncio.run(control.api_update_preflight(None)).body)["protected_route"]
    assert route["offered_strategy"] == "assisted"
    assert route["will_route_manual"] is False


def test_preflight_offers_assisted_when_the_dirty_count_is_missing(official_delta, monkeypatch):
    official_delta(["ouroboros/loop.py"])
    import supervisor.update_merge as update_merge

    def _plan_without_count(**_k):
        plan = _plan()
        plan.pop("local_dirty_count")
        return plan

    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge", _plan_without_count, raising=True
    )
    route = json.loads(asyncio.run(control.api_update_preflight(None)).body)["protected_route"]
    assert route["offered_strategy"] == "assisted"


def test_preflight_reports_the_unverifiable_reason(monkeypatch):
    """`will_route_manual` is derived from the REASON, not from list truthiness — an unverifiable
    delta routes to manual with an EMPTY list, and the dialog must not read that as "all clear"."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    monkeypatch.setattr(git_ops, "git_capture", lambda *_a, **_k: (128, "", "boom"), raising=True)
    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge", lambda **_k: _plan(), raising=True
    )
    route = json.loads(asyncio.run(control.api_update_preflight(None)).body)["protected_route"]
    assert route["will_route_manual"] is True
    assert route["reason"] == "protected_delta_unverifiable"
    assert route["protected_paths"] == []


def test_unverifiable_delta_routes_manual_without_side_effects(monkeypatch):
    """Same no-mutation guarantee as a protected rejection: we could not read the delta, so we
    hand off BEFORE workers are stopped or anything is staged."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    monkeypatch.setattr(git_ops, "git_capture", lambda *_a, **_k: (128, "", "boom"), raising=True)
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

    body = json.loads(asyncio.run(control._apply_managed_merge(None, "auto_merge")).body)
    assert body["status"] == "manual"
    assert body["reason"] == "protected_delta_unverifiable"
    assert body["protected_paths"] == []  # never an unexplained empty "protected files" list
    assert called == [], f"unverifiable routing must not mutate anything, called: {called}"


def test_manual_strategy_is_read_only_even_without_shas(monkeypatch):
    """`manual` only hands the plan to the UI, so an unverifiable delta must not turn that
    read-only inspection into an error or a second handoff."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    monkeypatch.setattr(git_ops, "git_capture", lambda *_a, **_k: (128, "", "boom"), raising=True)
    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge",
        lambda **_k: _plan(base_sha="", target_sha=""), raising=True,
    )
    body = json.loads(asyncio.run(control._apply_managed_merge(None, "manual")).body)
    assert body["status"] == "manual"
    assert "reason" not in body


# --- the fail-closed dirty count on the apply path (v6.88.1) ----------------------------------

def test_malformed_dirty_count_routes_auto_merge_to_the_reviewed_task(official_delta, monkeypatch):
    official_delta(["ouroboros/loop.py"])
    import supervisor.update_merge as update_merge

    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge",
        lambda **_k: _plan(local_dirty_count="not-a-number"), raising=True,
    )
    started = []
    monkeypatch.setattr(
        control, "_start_assisted_merge",
        lambda plan: started.append(plan) or control.JSONResponse({"status": "assisted_started"}),
    )
    body = json.loads(asyncio.run(control._apply_managed_merge(None, "auto_merge")).body)
    assert body["status"] == "assisted_started"
    assert len(started) == 1


def test_dirty_post_stop_plan_aborts_the_auto_merge_fast_path(official_delta, monkeypatch):
    """The post-kill re-plan is the last chance to notice local work that appeared while workers
    were still running; a count it cannot PROVE clean must abort instead of fast-committing."""
    official_delta(["ouroboros/loop.py"])
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    plans = iter([_plan(), _plan(local_dirty_count=None, merge_commit="cccc")])
    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge", lambda **_k: next(plans), raising=True
    )
    monkeypatch.setattr(update_merge, "acquire_update_lock", lambda: object(), raising=True)
    monkeypatch.setattr(update_merge, "release_update_lock", lambda _fh: None, raising=True)
    monkeypatch.setattr(update_merge, "active_update_tx", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(workers, "kill_workers", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(control, "_respawn_workers_after_failed_update", lambda: None)
    applied = []
    for name in ("apply_managed_merge_update", "write_update_tx"):
        monkeypatch.setattr(
            update_merge, name, lambda *_a, _n=name, **_k: applied.append(_n), raising=True
        )

    resp = asyncio.run(control._apply_managed_merge(None, "auto_merge"))
    assert resp.status_code == 409
    assert "no longer a clean auto-merge" in json.loads(resp.body)["error"]
    assert applied == []


# --- the replace/stash/force family (v6.88.1) -------------------------------------------------

_REPLACE_FAMILY = ["replace", "stash", "force", "obliterate"]  # unknown -> same gated path


@pytest.fixture
def replace_env(monkeypatch):
    """Hermetic stand-in for everything the replace family touches, recording call ORDER so the
    audit-before-preparation guarantee is observable."""
    import ouroboros.utils as utils
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    env = {"calls": [], "prepare_kwargs": {}, "audited": [], "audit_ok": True, "tx": []}

    def _audit(_path, record):
        env["calls"].append("audit")
        env["audited"].append(record)
        return env["audit_ok"]

    def _prepare(strategy, **kwargs):
        env["calls"].append("prepare")
        env["prepare_kwargs"] = {"strategy": strategy, **kwargs}
        return True, {"prepared": True}

    def _reset(*_a, **_k):
        env["calls"].append("reset")
        return True, "ok"

    # No transaction until the replace path writes its own — after which reads of it see that one,
    # exactly as the real marker behaves. A stub pinned at `None` would have hidden the whole point
    # of writing it: the recovery paths decide what to do by reading this.
    monkeypatch.setattr(
        update_merge, "active_update_tx",
        lambda *_a, **_k: (env["tx"][-1] if env["tx"] else None), raising=True,
    )
    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge", lambda **_k: _plan(), raising=True
    )
    # The replace family writes its transaction marker before the destructive reset (v6.88.1 r6).
    # Captured rather than recorded in `calls`: the order these tests pin is the AUDIT/prepare/reset
    # sequence, and the marker is inspected on its own in the transaction pins below.
    monkeypatch.setattr(
        update_merge, "write_update_tx", lambda payload: env["tx"].append(dict(payload)),
        raising=True,
    )
    # v6.88.1 r6: the transaction is now written BEFORE the preparation, so every abort between the
    # two takes it back off — through the intent marker first. Both are stubbed here rather than in
    # the individual tests, because unstubbed they would unlink the DEVELOPER's real markers under
    # `data/state/` from any replace test whose preparation fails or raises. Both also ANSWER
    # whether the marker is proven gone, and the callers only proceed on a True.
    monkeypatch.setattr(
        update_merge, "clear_update_tx", lambda: env["tx"].clear() or True, raising=True
    )
    monkeypatch.setattr(git_ops, "_clear_update_intent", lambda *_a, **_k: True, raising=True)
    monkeypatch.setattr(utils, "append_jsonl", _audit, raising=True)
    monkeypatch.setattr(git_ops, "prepare_managed_update", _prepare, raising=True)
    monkeypatch.setattr(git_ops, "checkout_and_reset", _reset, raising=True)
    monkeypatch.setattr(
        workers, "kill_workers", lambda *_a, **_k: env["calls"].append("kill"), raising=True
    )
    # The lock and the respawn are part of the ORDER this fixture exists to observe: v6.88.1
    # applies the replace family under the update lock with the workers fenced, and every exit
    # after that fence must bring the pool back.
    monkeypatch.setattr(
        update_merge, "acquire_update_lock",
        lambda: env["calls"].append("lock") or object(), raising=True,
    )
    monkeypatch.setattr(update_merge, "release_update_lock", lambda _fh: None, raising=True)
    monkeypatch.setattr(
        control, "_respawn_workers_after_failed_update", lambda: env["calls"].append("respawn")
    )
    monkeypatch.setattr(control, "_request_restart", lambda _r: env["calls"].append("restart"))
    return env


def _ack(paths, base="aaaa", target="bbbb"):
    return {
        "acknowledge_protected": True,
        "acknowledged_base_sha": base,
        "acknowledged_target_sha": target,
        "acknowledged_protected_paths": paths,
    }


@pytest.mark.parametrize("strategy", _REPLACE_FAMILY)
def test_replace_family_discloses_safety_critical_instead_of_resetting(
    official_delta, replace_env, strategy
):
    """`replace` hard-resets over the constitution without any review, so a safety-critical change
    must be DISCLOSED first — and nothing may be prepared or reset while it is unacknowledged."""
    official_delta(["ouroboros/safety.py", "ouroboros/loop.py"])
    body = json.loads(
        asyncio.run(control.api_update_apply(_Request({"strategy": strategy}))).body
    )
    assert body["status"] == "manual"
    assert body["reason"] == "protected_paths"
    assert body["requires_acknowledgement"] is True
    assert body["protected_paths"] == ["ouroboros/safety.py"]
    assert (body["base_sha"], body["target_sha"]) == ("aaaa", "bbbb")
    assert replace_env["calls"] == []


@pytest.mark.parametrize("strategy", _REPLACE_FAMILY)
def test_replace_family_discloses_an_unrecognized_protected_tier(
    official_delta, replace_env, monkeypatch, strategy
):
    """Fail-closed by exclusion: only the two named agent-authorship tiers are exempt here, so a
    protected path the categorizer does not place in a known tier is DISCLOSED rather than
    hard-reset over without review. Parametrized like its safety-critical sibling because the
    unknown strategy ('obliterate') reaches this gate by ELIMINATION — the rule is the family's,
    not plain `replace`'s."""
    official_delta(["some/odd/protected_doc.md"])
    monkeypatch.setattr(control, "_is_protected_for_managed_update", lambda _p: True)
    import ouroboros.runtime_mode_policy as policy

    monkeypatch.setattr(policy, "protected_path_category", lambda _p: "")
    body = json.loads(
        asyncio.run(control.api_update_apply(_Request({"strategy": strategy}))).body
    )
    assert body["status"] == "manual"
    assert body["requires_acknowledgement"] is True
    assert body["protected_paths"] == ["some/odd/protected_doc.md"]
    assert replace_env["calls"] == []


def test_replace_ignores_the_agent_authorship_tiers(official_delta, replace_env):
    """D1: the frozen-contract / release-invariant tiers exist to stop the AGENT authoring those
    files. `replace` takes the official release verbatim, so gating them there would only make the
    escape hatch unusable without protecting anything."""
    official_delta(["ouroboros/gateway/contracts.py", "supervisor/update_merge.py"])
    body = json.loads(
        asyncio.run(control.api_update_apply(_Request({"strategy": "replace"}))).body
    )
    assert body["status"] == "ok"
    # The writers are fenced BEFORE the pin is re-resolved and prepared against (v6.88.1).
    assert replace_env["calls"] == ["lock", "kill", "prepare", "reset", "restart"]


def test_bound_acknowledgement_proceeds_and_is_audited_before_preparation(
    official_delta, replace_env
):
    """The audit record is the only durable trace that the owner waived review, so it is written
    BEFORE anything is prepared — a crash mid-update must not lose it. It is written UNDER the
    worker fence (v6.88.1), so the record describes the transition that can still actually land."""
    official_delta(["ouroboros/safety.py", "prompts/SAFETY.md"])
    paths = ["ouroboros/safety.py", "prompts/SAFETY.md"]
    body = json.loads(
        asyncio.run(control.api_update_apply(
            _Request({"strategy": "replace", **_ack(paths)})
        )).body
    )
    assert body["status"] == "ok"
    assert replace_env["calls"] == ["lock", "kill", "audit", "prepare", "reset", "restart"]
    record = replace_env["audited"][0]
    assert record["type"] == "ui_update_protected_acknowledged"
    assert record["strategy"] == "replace"
    assert (record["base_sha"], record["target_sha"]) == ("aaaa", "bbbb")
    assert record["protected_paths"] == paths
    # The preparation re-fetches, so it is pinned to the release the owner was actually shown.
    assert replace_env["prepare_kwargs"] == {
        "strategy": "replace", "expected_base_sha": "aaaa", "expected_target_sha": "bbbb"
    }


@pytest.mark.parametrize("strategy", _REPLACE_FAMILY)
@pytest.mark.parametrize("ack_over", [
    None,                                                     # no acknowledgement at all
    {"acknowledge_protected": "yes"},                         # not a real True
    {"acknowledged_base_sha": "old!"},                        # stale disclosure (base moved)
    {"acknowledged_target_sha": "cccc"},                      # a DIFFERENT release
    {"acknowledged_protected_paths": ["ouroboros/safety.py"]},  # partial echo
    {"acknowledged_protected_paths": []},                     # empty echo
    {"acknowledged_protected_paths": "prompts/SAFETY.md"},    # not a list
])
def test_acknowledgement_must_be_bound_to_what_was_disclosed(
    official_delta, replace_env, strategy, ack_over
):
    """An echo that does not match the disclosure exactly is consent to something else."""
    official_delta(["ouroboros/safety.py", "prompts/SAFETY.md"])
    body = {"strategy": strategy} if ack_over is None else dict(
        _ack(["ouroboros/safety.py", "prompts/SAFETY.md"]), strategy=strategy, **ack_over
    )
    resp = json.loads(asyncio.run(control.api_update_apply(_Request(body))).body)
    assert resp["status"] == "manual"
    assert resp["requires_acknowledgement"] is True
    assert replace_env["calls"] == []


def test_acknowledgement_cannot_override_an_unverifiable_delta(replace_env, monkeypatch):
    """Consent about a list we could not read is not informed consent, so the ack is not even
    offered — and the disclosed SHAs are withheld rather than guessed."""
    import supervisor.git_ops as git_ops

    monkeypatch.setattr(git_ops, "git_capture", lambda *_a, **_k: (128, "", "boom"), raising=True)
    body = json.loads(asyncio.run(control.api_update_apply(_Request({
        "strategy": "replace", **_ack(["ouroboros/safety.py"])
    }))).body)
    assert body["status"] == "manual"
    assert body["reason"] == "protected_delta_unverifiable"
    assert body["protected_paths"] == []
    assert "requires_acknowledgement" not in body
    assert replace_env["calls"] == []


def test_unwritable_audit_aborts_the_acknowledged_replace(official_delta, replace_env):
    """Fail closed: an override we cannot record is an override that never happened."""
    official_delta(["ouroboros/safety.py"])
    replace_env["audit_ok"] = False
    resp = asyncio.run(control.api_update_apply(_Request({
        "strategy": "replace", **_ack(["ouroboros/safety.py"])
    })))
    assert resp.status_code == 409
    assert json.loads(resp.body)["error"] == "protected_ack_audit_failed"
    assert replace_env["calls"] == ["lock", "kill", "audit", "respawn"]


@pytest.mark.parametrize("strategy", ["auto_merge", "assisted", "doc_reconcile"])
def test_acknowledgement_cannot_unlock_a_staged_strategy(official_delta, replace_env, strategy):
    """The ack contract exists ONLY in the replace-family else-branch: those strategies hard-reset
    over the constitution, so the owner is offered a bound waiver instead of a dead end. The staged
    strategies have no such branch — a safety-critical change there must reach the owner's eyes on
    the diff, so the gate ignores the ack fields entirely.

    The body below carries a FULLY-FORMED acknowledgement — the very echo (same SHAs, same exact
    path list) that unlocks `replace` in `test_bound_acknowledgement_proceeds_and_is_audited_...` —
    and it must still route to manual without auditing, preparing, killing workers or resetting.
    """
    official_delta(["ouroboros/safety.py", "ouroboros/loop.py"])
    body = json.loads(asyncio.run(control.api_update_apply(_Request(
        {"strategy": strategy, **_ack(["ouroboros/safety.py"])}
    ))).body)
    assert body["status"] == "manual"
    assert body["reason"] == "protected_paths"
    assert body["protected_paths"] == ["ouroboros/safety.py"]
    # No waiver is on offer here, so the staged manual envelope must not advertise one either.
    assert "requires_acknowledgement" not in body
    assert replace_env["calls"] == []


# --- the browser's reading of the manual envelope (v6.88.1) -----------------------------------

def test_update_dialog_tells_the_two_manual_reasons_apart():
    """`api_update_apply` answers `manual` with two DIFFERENT typed reasons, and only one of them
    is about protected files: the tests above pin `protected_paths` carrying the disclosed list and
    `protected_delta_unverifiable` carrying an EMPTY one. So the pill dialog's post-apply branch
    must read `reason` — a single protected-files message would tell the owner a cause the backend
    explicitly refused to establish.

    Pinned at SOURCE because that branch is closure-scoped inside a DOM overlay and this repo has
    no DOM harness (the same reason web/tests/update_protected_ack.test.js pins updates.js). It
    lives in this gated module rather than beside that one because the JS suite is not part of the
    gate set, and `node --check` parses a file without ever evaluating an assertion: it would
    accept an inverted reason test or a deleted branch.
    """
    source = (
        pathlib.Path(__file__).resolve().parent.parent / "web" / "modules" / "update_status.js"
    ).read_text(encoding="utf-8")
    start = source.index("data.status === 'manual'")
    branch = source[start:source.index("setTimeout(", start)]
    assert "data.reason === 'protected_delta_unverifiable'" in branch
    # The typed reason is consulted FIRST; the protected-files wording is the else-branch, which is
    # the order the preflight note in that same module already uses.
    assert branch.index("could not be verified") < branch.index("needs manual handling")


# --- the preparation pin (v6.88.1) ------------------------------------------------------------

def test_prepare_managed_update_rejects_sha_drift_before_touching_the_repo(monkeypatch):
    """`prepare_managed_update` fetches AGAIN, so without the pin the remote could advance between
    the gate's disclosure and here and a different release would land under the old ack. The check
    runs before any rescue snapshot or update intent, so a drifted request leaves nothing behind."""
    import supervisor.git_ops as git_ops

    monkeypatch.setattr(
        git_ops, "compute_managed_update_status",
        lambda **_k: {
            "managed": True, "available": True, "current_sha": "aaaa", "latest_sha": "dddd",
        },
        raising=True,
    )
    touched = []
    for name in ("_collect_repo_sync_state", "_create_rescue_snapshot", "_write_update_intent"):
        monkeypatch.setattr(
            git_ops, name, lambda *_a, _n=name, **_k: touched.append(_n), raising=True
        )

    ok, payload = git_ops.prepare_managed_update(
        "replace", expected_base_sha="aaaa", expected_target_sha="bbbb"
    )
    assert ok is False
    assert "target moved from bbbb to dddd" in payload["error"]
    # Typed so the browser can report drift here with the SAME "click Update again" guidance the
    # gate-level stale-ack path uses, instead of falling through to a generic failure toast.
    assert payload["reason"] == "release_moved"
    assert touched == []


def test_prepare_managed_update_accepts_a_matching_pin(monkeypatch):
    """The pin must not block the normal case: unchanged SHAs fall through to the real work."""
    import supervisor.git_ops as git_ops

    monkeypatch.setattr(
        git_ops, "compute_managed_update_status",
        lambda **_k: {
            "managed": True, "available": True, "current_sha": "aaaa", "latest_sha": "bbbb",
        },
        raising=True,
    )
    reached = []
    monkeypatch.setattr(
        git_ops, "_collect_repo_sync_state",
        lambda *_a, **_k: reached.append("sync") or {"current_branch": "dev"}, raising=True
    )
    monkeypatch.setattr(
        git_ops, "_create_rescue_snapshot",
        lambda **_k: {"diff_error": "stopped here"}, raising=True
    )
    ok, payload = git_ops.prepare_managed_update(
        "replace", expected_base_sha="aaaa", expected_target_sha="bbbb"
    )
    assert ok is False
    assert "Rescue diff capture failed" in payload["error"]  # got PAST the pin
    assert reached == ["sync"]


# --- renaming a protected path away (v6.88.1 r3) ----------------------------------------------
#
# These run against a REAL two-commit repository rather than a faked diff, because the bug is a
# property of git's own rename detection: with it on, `git diff --name-only base target` prints a
# detected rename as its DESTINATION only, so moving BIBLE.md to an unprotected name produced no
# protected hit at all. A stubbed diff cannot reproduce that.

@pytest.fixture
def rename_repo(tmp_path, monkeypatch):
    """Build a repo whose second commit renames ``src`` to ``dst``, pin ``git_ops.REPO_DIR`` to it
    (that is the cwd every ``git_capture`` runs in), and return ``(base_sha, target_sha)``."""
    import subprocess

    def _make(src, dst):
        repo = tmp_path / "repo"
        repo.mkdir(parents=True, exist_ok=True)

        def _git(*args):
            return subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                cwd=str(repo), check=True, capture_output=True, text=True,
            )

        _git("init", "-b", "ouroboros")
        source = repo / src
        source.parent.mkdir(parents=True, exist_ok=True)
        # Substantial, identical content on both sides so git certainly scores this a rename.
        source.write_text("protected content\n" * 200, encoding="utf-8")
        _git("add", ".")
        _git("commit", "-m", "base")
        base = _git("rev-parse", "HEAD").stdout.strip()
        (repo / dst).parent.mkdir(parents=True, exist_ok=True)
        _git("mv", src, dst)
        _git("commit", "-m", "rename the protected path away")
        target = _git("rev-parse", "HEAD").stdout.strip()

        import supervisor.git_ops as git_ops

        monkeypatch.setattr(git_ops, "REPO_DIR", repo, raising=True)
        return base, target

    return _make


@pytest.mark.parametrize("path", [
    "BIBLE.md",
    "prompts/SAFETY.md",
    "ouroboros/safety.py",
    "ouroboros/runtime_mode_policy.py",
    "ouroboros/tools/registry.py",
    "ouroboros/tools/extension_dispatch.py",
])
def test_renaming_a_safety_critical_path_away_still_blocks(rename_repo, path):
    """Deleting the constitution by moving it to an unprotected name is exactly the change the
    owner must see. Rename detection hid it; `--no-renames` reports both endpoints."""
    import supervisor.git_ops as git_ops

    base, target = rename_repo(path, "docs/quietly_renamed.md")
    # What the pre-fix inventory saw: the destination only, so nothing protected was reported.
    _rc, detected, _e = git_ops.git_capture(["git", "diff", "-M", "--name-only", base, target])
    assert path not in detected.splitlines()

    plan = _plan(base_sha=base, target_sha=target)
    assert control._official_protected_hits(plan) == ([path], True)
    for strategy in ("auto_merge", "assisted", "doc_reconcile"):
        assert control._managed_update_protected_block(plan, strategy) == (
            [path], "protected_paths"
        ), strategy


def test_renaming_a_safety_critical_path_away_routes_the_staged_apply_to_manual(
    rename_repo, monkeypatch
):
    base, target = rename_repo("BIBLE.md", "docs/quietly_renamed.md")
    import supervisor.update_merge as update_merge

    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge",
        lambda **_k: _plan(base_sha=base, target_sha=target), raising=True,
    )
    body = json.loads(asyncio.run(control._apply_managed_merge(None, "auto_merge")).body)
    assert body["status"] == "manual"
    assert body["reason"] == "protected_paths"
    assert body["protected_paths"] == ["BIBLE.md"]


@pytest.mark.parametrize("strategy", _REPLACE_FAMILY)
def test_renaming_a_safety_critical_path_away_is_disclosed_by_the_replace_family(
    rename_repo, replace_env, monkeypatch, strategy
):
    """`replace` hard-resets to the official tree, so a release that renames the constitution away
    would silently delete it from the checkout without the owner ever being told."""
    base, target = rename_repo("ouroboros/safety.py", "ouroboros/helpers.py")
    import supervisor.update_merge as update_merge

    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge",
        lambda **_k: _plan(base_sha=base, target_sha=target), raising=True,
    )
    body = json.loads(
        asyncio.run(control.api_update_apply(_Request({"strategy": strategy}))).body
    )
    assert body["status"] == "manual"
    assert body["requires_acknowledgement"] is True
    assert body["protected_paths"] == ["ouroboros/safety.py"]
    assert replace_env["calls"] == []


# --- the producer of the dirty count / snapshot (v6.88.1 r3) ----------------------------------

def _git_capture_stub(overrides):
    """`git_capture` stand-in for `plan_managed_update_merge`: resolve the SHAs it needs, and let a
    named command fail. Keyed on the joined argv so a test can fail exactly one git call."""
    def _run(cmd, *_a, **_k):
        key = " ".join(str(part) for part in cmd)
        for prefix, result in overrides.items():
            if key.startswith(prefix):
                return result
        if key.startswith("git rev-parse --verify HEAD"):
            return 0, "aaaa", ""
        if key.startswith("git rev-parse --verify"):
            return 0, "bbbb", ""
        return 0, "", ""
    return _run


@pytest.fixture
def plan_producer(monkeypatch):
    """Drive `plan_managed_update_merge` hermetically: a resolvable managed target, a controllable
    live-repo `git_capture`, and a recorded temp-index/worktree `_git_run`."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    env = {"ran": []}

    def _set(*, capture_overrides=None, run_overrides=None):
        monkeypatch.setattr(
            git_ops, "_managed_update_target",
            lambda _b: ("managed", "ouroboros", "managed/ouroboros"), raising=True,
        )
        monkeypatch.setattr(
            git_ops, "git_capture", _git_capture_stub(capture_overrides or {}), raising=True
        )

        def _run(cmd, **_k):
            env["ran"].append(list(cmd))
            key = " ".join(str(part) for part in cmd)
            # SUBSTRING, not prefix: the temp-worktree commands are `git -C <random tmp path>
            # merge ...` / `... diff ...`, so there is no stable prefix to key them on.
            for fragment, result in (run_overrides or {}).items():
                if fragment in key:
                    return result
            return 0, "0" * 40, ""

        monkeypatch.setattr(update_merge, "_git_run", _run, raising=True)
        return env

    return _set


def test_plan_refuses_to_emit_a_dirty_count_from_a_failed_status(plan_producer):
    """`_plan_worktree_is_clean` treats integer zero as PROOF of an empty worktree and unlocks the
    unreviewed auto-merge fast path. A failed `git status` also prints zero lines, so counting them
    forges that proof — the plan must come back explicitly unverified instead."""
    import supervisor.update_merge as update_merge

    env = plan_producer(
        capture_overrides={"git status --porcelain": (128, "", "fatal: unreadable index")}
    )
    plan = update_merge.plan_managed_update_merge()

    assert plan["kind"] == "unknown"
    assert "local_dirty_count" not in plan
    assert "status failed" in plan["error"]
    assert env["ran"] == []  # bailed before building any snapshot
    assert control._plan_worktree_is_clean(plan) is False
    assert control._supervisor_authored_clean_merge(plan, "auto_merge") is False


def test_plan_refuses_to_snapshot_when_add_fails(plan_producer):
    """A failed `git add -A` leaves the temp index at bare HEAD, so the snapshot would silently
    OMIT the owner's dirty and untracked work while the plan still looked buildable."""
    import supervisor.update_merge as update_merge

    plan_producer(run_overrides={"git add -A": (1, "", "fatal: unable to index file")})
    plan = update_merge.plan_managed_update_merge()

    assert plan["kind"] == "unknown"
    assert "add -A failed" in plan["error"]
    assert "local_snapshot" not in plan
    assert control._supervisor_authored_clean_merge(plan, "auto_merge") is False


def test_plan_refuses_a_merge_dry_run_that_never_ran(plan_producer):
    """The merge's own return code is part of the same proof. Conflicts make `git merge` exit 1
    (expected), but a FATAL merge — unrelated histories, an unusable temp worktree, ENOSPC —
    leaves the temp index untouched at `local_snapshot` with nothing unmerged, so ignoring the rc
    hands `classify_conflicts` an empty list, i.e. kind "clean". With build=True the follow-on
    commit-tree would then record a 2-parent commit whose TREE is the PRE-update snapshot: the
    release reads as merged while none of its content actually landed."""
    import supervisor.update_merge as update_merge

    plan_producer(run_overrides={
        "merge --no-commit": (128, "", "fatal: refusing to merge unrelated histories")
    })
    plan = update_merge.plan_managed_update_merge(build=True)

    assert plan["kind"] == "unknown"
    assert "merge dry-run failed" in plan["error"]
    assert "merge_commit" not in plan
    assert control._supervisor_authored_clean_merge(plan, "auto_merge") is False


def test_a_conflicted_merge_dry_run_is_not_a_failure(plan_producer):
    """The other half of the same rule: exit 1 IS the conflicted outcome this planner is built to
    inspect, so it must keep classifying rather than fail closed on it."""
    import supervisor.update_merge as update_merge

    plan_producer(run_overrides={
        "merge --no-commit": (1, "", "CONFLICT (content): Merge conflict in ouroboros/loop.py")
    })
    plan = update_merge.plan_managed_update_merge()

    assert plan["kind"] == "conflicting"  # the stubbed inventory reports one unmerged path
    assert "error" not in plan


def test_plan_refuses_a_failed_conflict_inventory(plan_producer):
    """`--diff-filter=U` IS the conflict set. A failed run also prints nothing, so flattening it
    into an empty list forges the same clean-merge proof a failed `git status` would."""
    import supervisor.update_merge as update_merge

    plan_producer(run_overrides={
        "diff --name-only --diff-filter=U": (128, "", "fatal: unable to read the index")
    })
    plan = update_merge.plan_managed_update_merge(build=True)

    assert plan["kind"] == "unknown"
    assert "conflict inventory failed" in plan["error"]
    assert "merge_commit" not in plan
    assert control._supervisor_authored_clean_merge(plan, "auto_merge") is False


def test_a_timed_out_fetch_yields_a_plan_that_cannot_be_applied(plan_producer, monkeypatch):
    """A fetch we KILLED says nothing about where the remote's head actually is, and the fenced
    re-plan is the caller that reaches this — so the plan must come back unavailable rather than
    silently describing a ref of unknown freshness."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    env = plan_producer()
    monkeypatch.setattr(
        git_ops, "git_fetch_bounded",
        lambda *_a, **_k: (git_ops.FETCH_TIMEOUT_RC, "", "exceeded 300s and was terminated"),
        raising=True,
    )
    plan = update_merge.plan_managed_update_merge(fetch=True)

    assert plan["available"] is False
    assert "timed out" in plan["error"]
    assert env["ran"] == []  # bailed before building any snapshot


def test_a_failed_fetch_still_plans_against_the_last_known_ref(plan_producer, monkeypatch):
    """Deliberately NOT symmetrical with the timeout above. A fetch that failed and RETURNED
    (offline, auth, no such remote) leaves the tracking ref exactly where the disclosure read it,
    which is what the replace-family pin and `_post_stop_plan_drift` compare against — so the only
    release still reachable is the acknowledged one. `compute_managed_update_status` treats the
    same condition as a warning; failing closed here would make every offline apply of an
    already-reviewed release unreachable and buy no safety."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    monkeypatch.setattr(
        git_ops, "git_fetch_bounded", lambda *_a, **_k: (128, "", "could not read from remote"),
        raising=True,
    )
    plan_producer()
    plan = update_merge.plan_managed_update_merge(fetch=True)

    assert plan["available"] is True
    assert plan["base_sha"] == "aaaa"
    assert plan["target_sha"] == "bbbb"


def _unverified_plan(**over):
    """What the hardened producer returns when it could not read the worktree."""
    plan = {"available": True, "kind": "unknown", "base_sha": "aaaa", "target_sha": "bbbb",
            "error": "status failed: fatal: unreadable index"}
    plan.update(over)
    return plan


def test_unverified_initial_plan_routes_auto_merge_to_the_reviewed_task(
    official_delta, monkeypatch
):
    """The producer's fail-closed plan is only worth anything if the consumer refuses it: an
    unreadable worktree must reach the REVIEWED assisted path, never the fast commit."""
    official_delta(["ouroboros/loop.py"])
    import supervisor.update_merge as update_merge

    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge", lambda **_k: _unverified_plan(), raising=True
    )
    started = []
    monkeypatch.setattr(
        control, "_start_assisted_merge",
        lambda plan: started.append(plan) or control.JSONResponse({"status": "assisted_started"}),
    )
    body = json.loads(asyncio.run(control._apply_managed_merge(None, "auto_merge")).body)
    assert body["status"] == "assisted_started"
    assert len(started) == 1


def test_unverified_post_stop_plan_aborts_the_auto_merge_fast_path(official_delta, monkeypatch):
    """Same guarantee on the re-plan taken after the workers are stopped: if git could not be read
    THERE, nothing may be committed on the strength of it."""
    official_delta(["ouroboros/loop.py"])
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    plans = iter([_plan(), _unverified_plan(merge_commit="cccc")])
    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge", lambda **_k: next(plans), raising=True
    )
    monkeypatch.setattr(update_merge, "acquire_update_lock", lambda: object(), raising=True)
    monkeypatch.setattr(update_merge, "release_update_lock", lambda _fh: None, raising=True)
    monkeypatch.setattr(update_merge, "active_update_tx", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(workers, "kill_workers", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(control, "_respawn_workers_after_failed_update", lambda: None)
    applied = []
    for name in ("apply_managed_merge_update", "write_update_tx"):
        monkeypatch.setattr(
            update_merge, name, lambda *_a, _n=name, **_k: applied.append(_n), raising=True
        )

    resp = asyncio.run(control._apply_managed_merge(None, "auto_merge"))
    assert resp.status_code == 409
    assert "no longer a clean auto-merge" in json.loads(resp.body)["error"]
    assert applied == []


# --- target drift across the worker stop, staged strategies (v6.88.1 r3) ----------------------

@pytest.fixture
def staged_apply_env(monkeypatch):
    """Everything the staged flows touch after the workers are stopped, recorded so a drifted
    re-plan can be shown to reach NONE of it."""
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    env = {"staged": []}
    monkeypatch.setattr(update_merge, "acquire_update_lock", lambda: object(), raising=True)
    monkeypatch.setattr(update_merge, "release_update_lock", lambda _fh: None, raising=True)
    monkeypatch.setattr(update_merge, "active_update_tx", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(workers, "kill_workers", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(control, "_respawn_workers_after_failed_update", lambda: None)
    for name in (
        "apply_managed_merge_update",
        "write_update_tx",
        "materialize_assisted_merge_live",
        "create_rescue_local_ref",
        "enqueue_assisted_resolution_task",
    ):
        monkeypatch.setattr(
            update_merge, name, lambda *_a, _n=name, **_k: env["staged"].append(_n), raising=True
        )
    return env


def _sequenced_plans(monkeypatch, plans):
    import supervisor.update_merge as update_merge

    seq = iter(plans)
    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge", lambda **_k: next(seq), raising=True
    )


def _sequenced_deltas(monkeypatch, deltas):
    """`git diff base..target` answers a different official delta on each gate evaluation."""
    import supervisor.git_ops as git_ops

    seq = iter(deltas)
    monkeypatch.setattr(
        git_ops, "git_capture",
        lambda *_a, **_k: (0, "\n".join(next(seq, [])), ""), raising=True,
    )


def test_target_drift_after_stopping_workers_aborts_the_auto_merge(official_delta, monkeypatch,
                                                                   staged_apply_env):
    """The gate approved base aaaa -> target bbbb. The post-stop re-plan resolves the tracking ref
    AGAIN, and a racing fetch can have moved it — landing that release would apply a target the
    owner never saw."""
    official_delta(["ouroboros/loop.py"])
    _sequenced_plans(monkeypatch, [_plan(), _plan(target_sha="cccc", merge_commit="dddd")])

    resp = asyncio.run(control._apply_managed_merge(None, "auto_merge"))
    body = json.loads(resp.body)
    assert resp.status_code == 409
    assert body["reason"] == "release_moved"
    assert "target moved from bbbb to cccc" in body["error"]
    assert staged_apply_env["staged"] == []


def test_protected_change_appearing_after_the_stop_aborts_the_auto_merge(monkeypatch,
                                                                        staged_apply_env):
    """Same window, worse payload: the ref moved to a target that changes a safety-critical file.
    The complete protected authority — not just a SHA comparison — reruns on the post-stop plan."""
    _sequenced_deltas(monkeypatch, [["ouroboros/loop.py"], ["ouroboros/safety.py"]])
    _sequenced_plans(monkeypatch, [_plan(), _plan(merge_commit="dddd")])

    body = json.loads(asyncio.run(control._apply_managed_merge(None, "auto_merge")).body)
    assert body["status"] == "manual"
    assert body["reason"] == "protected_paths"
    assert body["protected_paths"] == ["ouroboros/safety.py"]
    assert staged_apply_env["staged"] == []


def test_target_drift_after_stopping_workers_aborts_the_assisted_merge(official_delta, monkeypatch,
                                                                       staged_apply_env):
    """The assisted flow MATERIALIZES its re-plan into the live worktree, so a drifted target would
    be merged into the owner's checkout and handed to the agent to commit."""
    official_delta(["ouroboros/loop.py"])
    conflicting = dict(kind="conflicting", code_conflict_paths=["ouroboros/loop.py"])
    _sequenced_plans(monkeypatch, [
        _plan(**conflicting),
        _plan(target_sha="cccc", local_snapshot="ssss", **conflicting),
    ])

    resp = asyncio.run(control._apply_managed_merge(None, "assisted"))
    assert resp.status_code == 409
    assert json.loads(resp.body)["reason"] == "release_moved"
    assert staged_apply_env["staged"] == []


def test_protected_change_appearing_after_the_stop_aborts_the_assisted_merge(monkeypatch,
                                                                            staged_apply_env):
    _sequenced_deltas(monkeypatch, [["ouroboros/loop.py"], ["BIBLE.md"]])
    conflicting = dict(kind="conflicting", code_conflict_paths=["ouroboros/loop.py"])
    _sequenced_plans(monkeypatch, [_plan(**conflicting), _plan(local_snapshot="ssss", **conflicting)])

    body = json.loads(asyncio.run(control._apply_managed_merge(None, "assisted")).body)
    assert body["status"] == "manual"
    assert body["reason"] == "protected_paths"
    assert body["protected_paths"] == ["BIBLE.md"]
    assert staged_apply_env["staged"] == []


# --- the replace-family writer fence (v6.88.1 r3) ---------------------------------------------

def _failed_fence(_reason):
    """A fence that could not be established with NOTHING from the prior generation left running.

    The fence answers with a typed `_FenceResult`, not a bool, precisely because this outcome and
    the survivor one demand opposite recoveries — see `_survivor_fence`.
    """
    return control._FenceResult(False, [])


def _survivor_fence(_reason):
    """A fence that could not be established and left a prior-generation writer ALIVE."""
    return control._FenceResult(False, [object()])


def test_replace_family_refuses_a_release_that_moved_under_the_fence(
    official_delta, replace_env, monkeypatch
):
    """The disclosure named aaaa -> bbbb, but by the time the writers were fenced the target had
    moved. The acknowledgement (and the base the reset would run from) described the OLD
    transition, so nothing is prepared: the pool comes back and the owner gets a fresh
    disclosure."""
    official_delta(["ouroboros/loop.py"])  # nothing protected — the DRIFT alone must block
    _sequenced_plans(monkeypatch, [_plan(), _plan(target_sha="cccc")])

    body = json.loads(
        asyncio.run(control.api_update_apply(_Request({"strategy": "replace"}))).body
    )
    assert body["status"] == "manual"
    assert body["reason"] == "release_moved"
    assert (body["base_sha"], body["target_sha"]) == ("aaaa", "cccc")
    assert replace_env["calls"] == ["lock", "kill", "respawn"]


def test_replace_family_rechecks_the_protected_set_under_the_fence(replace_env, monkeypatch):
    """The disclosed set is re-derived, not remembered: a protected change that only becomes
    visible once the writers are down still reaches the owner before any hard reset."""
    _sequenced_deltas(monkeypatch, [["ouroboros/loop.py"], ["ouroboros/safety.py"]])

    body = json.loads(
        asyncio.run(control.api_update_apply(_Request({"strategy": "replace"}))).body
    )
    assert body["status"] == "manual"
    assert body["requires_acknowledgement"] is True
    assert body["protected_paths"] == ["ouroboros/safety.py"]
    assert replace_env["calls"] == ["lock", "kill", "respawn"]


def test_replace_family_aborts_when_the_worker_fence_cannot_be_established(
    official_delta, replace_env, monkeypatch
):
    """Without the exclusion the pin proves nothing, so an un-fenced apply must not proceed."""
    official_delta(["ouroboros/loop.py"])
    monkeypatch.setattr(control, "_fence_workers_for_update", _failed_fence)

    resp = asyncio.run(control.api_update_apply(_Request({"strategy": "replace"})))
    assert resp.status_code == 409
    assert "could not stop workers" in json.loads(resp.body)["error"]
    # ...and the pool comes back: a fence can fail HALF-WAY (kill_workers tears workers down one by
    # one before clearing the pool, and the verification pass reports survivors it could not kill),
    # so an abort that skipped the respawn could end task processing until a manual restart.
    assert replace_env["calls"] == ["lock", "respawn"]


def test_worker_fence_reports_a_failed_kill(monkeypatch):
    """`kill_workers` raising used to be logged and ignored; it now fails the fence."""
    import supervisor.workers as workers

    def _boom(**_kwargs):
        raise RuntimeError("worker teardown failed")

    monkeypatch.setattr(workers, "kill_workers", _boom, raising=True)
    fence = control._fence_workers_for_update("reason")
    assert fence.ok is False
    # A raise is NOT a survivor report: nothing was proven to be still running, so this outcome is
    # the one a replacement pool may answer. The distinction is the reason the fence is typed.
    assert fence.survivors == []


def test_replace_family_prepares_the_pin_it_re_resolved_under_the_fence(
    official_delta, replace_env
):
    """The happy path still ends in a prepared, pinned update — and the pin handed to
    `prepare_managed_update` is the FENCED re-resolution, not the pre-lock disclosure."""
    official_delta(["ouroboros/loop.py"])
    body = json.loads(
        asyncio.run(control.api_update_apply(_Request({"strategy": "replace"}))).body
    )
    assert body["status"] == "ok"
    assert replace_env["calls"] == ["lock", "kill", "prepare", "reset", "restart"]
    assert replace_env["prepare_kwargs"] == {
        "strategy": "replace", "expected_base_sha": "aaaa", "expected_target_sha": "bbbb"
    }


# --- the replace family is a TRANSACTION too (v6.88.1 r6) -------------------------------------

def test_the_replace_family_records_the_transaction_before_the_destructive_reset(
    official_delta, replace_env, monkeypatch
):
    """The replace family hard-resets the LIVE checkout but recorded only a target intent, so every
    post-mutation recovery read "no transaction is active", took that as proof nothing was staged,
    and handed the tree back to a fresh worker pool and to in-process chat wherever the reset had
    left it. It now writes the same marker the staged flows do, BEFORE the mutation, pinned to the
    SHA re-resolved under the fence.

    v6.88.1 r6 moves it one step earlier still — ahead of the PREPARATION — because preparation ends
    by publishing the restart-consumed update intent, and a crash in the old window left that intent
    on disk with no transaction to bound it."""
    import supervisor.update_merge as update_merge

    official_delta(["ouroboros/loop.py"])
    monkeypatch.setattr(
        update_merge, "write_update_tx",
        lambda payload: replace_env["calls"].append("tx") or replace_env["tx"].append(dict(payload)),
        raising=True,
    )

    body = json.loads(
        asyncio.run(control.api_update_apply(_Request({"strategy": "replace"}))).body
    )
    assert body["status"] == "ok"
    assert replace_env["calls"] == ["lock", "kill", "tx", "prepare", "reset", "restart"]
    tx = replace_env["tx"][0]
    # `pre_update_sha` is the FENCED base, which is the whole point: it is what a rollback returns
    # to, and a pre-fence read could name a commit a live worker had already moved off.
    assert tx["pre_update_sha"] == "aaaa"
    assert tx["target_sha"] == "bbbb"
    # The existing committed-update phase, not a new one: it already means "HEAD must carry
    # `merge_commit`, else roll back to `pre_update_sha`", and for a replace that commit IS the
    # target — so the boot finalizer and the admission reconcile handle this marker unchanged.
    assert (tx["phase"], tx["merge_commit"]) == ("pending_boot_smoke", "bbbb")


@pytest.mark.parametrize("outcome", ["returned_false", "raised"])
def test_a_failed_replace_checkout_rolls_back_the_tree_it_may_have_moved(
    official_delta, replace_env, monkeypatch, outcome
):
    """`checkout_and_reset` can fail AFTER it has already moved the live checkout, so the old
    `_clear_update_intent()` + bare respawn put a new worker generation and direct chat onto a
    half-updated tree. Both failure shapes now take the proven-rollback gate."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    official_delta(["ouroboros/loop.py"])

    def _failed_reset(*_a, **_k):
        replace_env["calls"].append("reset")
        if outcome == "raised":
            raise RuntimeError("index.lock exists")
        return False, "index.lock exists"

    monkeypatch.setattr(git_ops, "checkout_and_reset", _failed_reset, raising=True)
    monkeypatch.setattr(
        update_merge, "rollback_managed_update",
        lambda why: replace_env["calls"].append(f"rollback:{why}") or (True, "rolled back to aaaa"),
        raising=True,
    )
    monkeypatch.setattr(control, "_repository_is_recovered", lambda: True)

    resp = asyncio.run(control.api_update_apply(_Request({"strategy": "replace"})))
    assert resp.status_code == 409
    body = json.loads(resp.body)
    assert "checkout failed" in body["error"]
    assert body["rollback"] == "rolled back to aaaa"
    # The undo runs BEFORE the writers come back, and only a PROVEN undo lets them back at all.
    assert replace_env["calls"] == [
        "lock", "kill", "prepare", "reset", "rollback:replace_checkout_failed", "respawn"
    ]


def test_an_unproven_replace_rollback_keeps_the_writers_out(
    official_delta, replace_env, monkeypatch
):
    """And the other half of that gate: an undo we could not prove keeps the pool down and
    admission closed rather than reviving writers onto an unknown tree."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    official_delta(["ouroboros/loop.py"])
    monkeypatch.setattr(
        git_ops, "checkout_and_reset",
        lambda *_a, **_k: replace_env["calls"].append("reset") or (False, "index.lock exists"),
        raising=True,
    )
    monkeypatch.setattr(
        update_merge, "rollback_managed_update",
        lambda _why: (False, "no pre_update_sha in update tx marker"), raising=True,
    )

    resp = asyncio.run(control.api_update_apply(_Request({"strategy": "replace"})))
    assert resp.status_code == 409
    assert json.loads(resp.body)["reason"] == "update_recovery_failed"
    assert "respawn" not in replace_env["calls"]


def test_a_failed_restart_request_does_not_undo_an_update_that_landed(
    official_delta, replace_env, monkeypatch
):
    """Once the checkout returns ok the update is COMMITTED. An exception from the restart request
    used to unwind into the post-fence recovery, which found no transaction, called the update
    "aborted after stopping workers" and respawned the pool over a freshly reset tree — reporting a
    landed update as a failure. (With the transaction in place it would now ROLL BACK a good
    update, which is worse.) The only thing that failed is the restart, so that is what is said."""
    def _no_restart(_request):
        replace_env["calls"].append("restart")
        raise RuntimeError("no restart callback is wired")

    official_delta(["ouroboros/loop.py"])
    monkeypatch.setattr(control, "_request_restart", _no_restart)

    resp = asyncio.run(control.api_update_apply(_Request({"strategy": "replace"})))
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["status"] == "ok"
    assert body["restarting"] is False
    assert "restart manually" in body["warning"]
    assert replace_env["calls"] == ["lock", "kill", "prepare", "reset", "restart"], (
        "a committed update is neither rolled back nor respawned over"
    )


def test_a_failed_restart_request_does_not_undo_an_auto_merge_that_landed(
    official_delta, monkeypatch
):
    """The same hazard on the merge family, where it is strictly worse than on replace: by the time
    the restart is requested the merge is APPLIED and the pre-restart smoke has PASSED, and a LIVE
    `pending_boot_smoke` transaction is on disk for the boot finalizer. A raise here reached
    `_recover_after_post_fence_exception`, which takes the transaction branch and hard-resets the
    tree back to `pre_update_sha` — destroying a landed, smoke-verified update and reporting it as
    an abort. `staged_apply_env` cannot drive this (its `apply_managed_merge_update` stub returns
    None, so the `ok, msg` unpack never gets that far), hence the local wiring."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    calls = []
    official_delta(["ouroboros/loop.py"])
    monkeypatch.setattr(update_merge, "acquire_update_lock", lambda: object(), raising=True)
    monkeypatch.setattr(update_merge, "release_update_lock", lambda _fh: None, raising=True)
    monkeypatch.setattr(update_merge, "active_update_tx", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(workers, "kill_workers", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge",
        lambda **_k: _plan(merge_commit="cccc"), raising=True,
    )
    monkeypatch.setattr(
        git_ops, "_create_rescue_snapshot", lambda *_a, **_k: calls.append("rescue"), raising=True
    )
    monkeypatch.setattr(
        update_merge, "write_update_tx", lambda tx: calls.append("tx"), raising=True
    )
    monkeypatch.setattr(
        update_merge, "apply_managed_merge_update",
        lambda *_a, **_k: calls.append("apply") or (True, "merged"), raising=True,
    )
    monkeypatch.setattr(
        update_merge, "update_restart_smoke",
        lambda *_a, **_k: calls.append("smoke") or {"ok": True}, raising=True,
    )
    monkeypatch.setattr(
        update_merge, "rollback_managed_update",
        lambda why: calls.append(f"rollback:{why}") or (True, "rolled back"), raising=True,
    )
    monkeypatch.setattr(
        control, "_respawn_workers_after_failed_update", lambda: calls.append("respawn")
    )

    def _no_restart(_request):
        calls.append("restart")
        raise RuntimeError("no restart callback is wired")

    monkeypatch.setattr(control, "_request_restart", _no_restart)

    resp = asyncio.run(control._apply_managed_merge(None, "auto_merge"))
    body = json.loads(resp.body)

    assert resp.status_code == 200
    assert body["status"] == "ok"
    assert body["strategy"] == "auto_merge"
    assert body["restarting"] is False
    assert "restart manually" in body["warning"]
    assert calls == ["rescue", "tx", "apply", "smoke", "restart"], (
        "a landed, smoke-verified merge is neither rolled back nor respawned over"
    )


# --- the browser's reading of the three drift windows (v6.88.1 r3) ----------------------------

def test_update_dialog_reports_every_drift_window_with_one_message():
    """A release can move at three points — before the bound re-POST (fresh disclosure), before the
    fenced re-plan (typed `release_moved`), and before prepare's own fetch (a 409 carrying that
    same reason, which `jsonPost` RAISES). All three refuse the drifted release and all three want
    the same next action, so the owner must not see a generic 'Update failed' for one of them.

    They do NOT all get the same WORDING, though. Only the first window follows a disclosure
    dialog; the other two fire for any update, including an unprotected one where the owner
    confirmed nothing and no protected path changed. So the confirmation/protected-review sentence
    belongs to the acknowledged branch alone and the other two get neutral text — a shared message
    would state two things that are simply false on the unprotected path.

    Pinned at SOURCE in this gated module for the reason documented on the sibling
    `update_status.js` pin: the JS suite is not in the gate set and `node --check` never evaluates
    an assertion.

    The wording must stay INSIDE the branch, not be hoisted into a shared constant above
    `applyUpdate`: `web/tests/update_protected_ack.test.js` pins it by slicing the source between
    the acknowledgement re-POST and the generic manual toast, so a hoist moves it out of the slice
    and silently breaks a suite this gate set does not run."""
    source = (
        pathlib.Path(__file__).resolve().parent.parent / "web" / "modules" / "updates.js"
    ).read_text(encoding="utf-8")
    # `prepare_managed_update`'s 409 is converted back into a response so it reaches a typed branch
    # rather than the generic 'Update failed' catch.
    assert "err?.body?.reason === 'release_moved'" in source
    # Two branches, both before the generic manual toast: the acknowledged window keyed on the flag
    # the re-POST sets, the other two keyed on the typed reason the backend sent.
    assert "if (releaseMoved) {" in source
    assert "if (data.reason === 'release_moved') {" in source
    re_ack = source.index("acknowledged_protected_paths: data.protected_paths")
    generic_manual = source.index("Update needs manual handling")
    assert re_ack < source.index("if (releaseMoved) {") < generic_manual
    assert re_ack < source.index("if (data.reason === 'release_moved') {") < generic_manual
    # Exactly one place claims the owner confirmed something, and it is the acknowledged branch.
    assert source.count("release moved since you confirmed") == 1
    confirmed = source.index("release moved since you confirmed")
    assert re_ack < confirmed < source.index("if (data.reason === 'release_moved') {")
    # The neutral branch itself (up to the generic manual branch that follows it) must not borrow
    # either false claim.
    neutral_start = source.index("if (data.reason === 'release_moved') {")
    neutral = source[neutral_start:source.index("data.status === 'manual'", neutral_start)]
    assert "recheck the changed release" in neutral
    assert "confirmed" not in neutral and "protected" not in neutral


def test_update_dialog_reports_a_held_update_lock_as_retryable():
    """The boot check-on-restart thread holds the exclusive update lock across its fetch, so an
    apply can lose the race through no fault of the owner and the very next click will work.
    Reported as retryable rather than as the generic 'Update failed'. Source-pinned here for the
    same reason as the sibling drift pin above."""
    source = (
        pathlib.Path(__file__).resolve().parent.parent / "web" / "modules" / "updates.js"
    ).read_text(encoding="utf-8")
    assert "const UPDATE_LOCK_HELD_MESSAGE" in source
    assert "err?.body?.reason === 'update_lock_held'" in source
    assert "isUpdateLockHeldError(err)" in source


# --- the caller's message must survive a plan-carrying error (v6.88.1 r4) ---------------------

def test_a_plan_carrying_error_does_not_overwrite_the_endpoint_message():
    """`{"error": <contextual>, **plan}` resolves to the PLAN's error: the later splat wins in a
    dict display. Every plan-carrying 409 in the apply path used that order, so a degraded plan
    silently replaced the endpoint's explanation with low-level git text."""
    resp = control._plan_error_response(_unverified_plan(), "no managed update available")
    body = json.loads(resp.body)

    assert resp.status_code == 409
    assert body["error"] == "no managed update available"
    # The producer's own diagnostic is preserved, just not in the field that speaks for the API.
    assert body["plan_error"] == "status failed: fatal: unreadable index"
    assert body["base_sha"] == "aaaa"


def test_an_unavailable_plan_reports_the_endpoints_own_message(monkeypatch):
    """End to end on the other shadowing site: `plan_managed_update_merge` emits its own `error`
    on every unavailable result, so this 409 used to answer "could not resolve target/HEAD"."""
    import supervisor.update_merge as update_merge

    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge",
        lambda **_k: {"available": False, "kind": "unavailable",
                      "error": "could not resolve target/HEAD"},
        raising=True,
    )
    resp = asyncio.run(control._apply_managed_merge(None, "auto_merge"))
    body = json.loads(resp.body)

    assert resp.status_code == 409
    assert body["error"] == "no managed update available"
    assert body["plan_error"] == "could not resolve target/HEAD"


# --- the worker fence is mandatory on the STAGED paths too (v6.88.1 r4) -----------------------

def _raising_kill(**_kwargs):
    raise RuntimeError("worker teardown failed")


def _counted_plans(monkeypatch, plans):
    """Sequenced plans plus a call log, so a test can prove the post-stop re-plan never ran."""
    import supervisor.update_merge as update_merge

    seq, calls = iter(plans), []

    def _next_plan(**_k):
        calls.append(1)
        return next(seq)

    monkeypatch.setattr(update_merge, "plan_managed_update_merge", _next_plan, raising=True)
    return calls


def test_auto_merge_aborts_when_the_worker_fence_cannot_be_established(
    official_delta, monkeypatch, staged_apply_env
):
    """Everything the post-stop recheck proves — clean tree, same base/target, unchanged protected
    set — holds only while nothing else can write. If `kill_workers` raised, the pool is in an
    unknown state and a survivor can advance HEAD between that recheck and the fast commit, so the
    apply must not even re-plan."""
    official_delta(["ouroboros/loop.py"])
    import supervisor.workers as workers

    calls = _counted_plans(monkeypatch, [_plan(), _plan(merge_commit="dddd")])
    monkeypatch.setattr(workers, "kill_workers", _raising_kill, raising=True)

    resp = asyncio.run(control._apply_managed_merge(None, "auto_merge"))
    assert resp.status_code == 409
    assert "could not stop workers" in json.loads(resp.body)["error"]
    assert len(calls) == 1  # the pre-kill gate plan only — no re-plan behind a failed fence
    assert staged_apply_env["staged"] == []


def test_assisted_merge_aborts_when_the_worker_fence_cannot_be_established(
    official_delta, monkeypatch, staged_apply_env
):
    """Same guarantee where it matters most: the assisted flow stages a REAL merge into the LIVE
    worktree, so an unfenced run would hand the agent a tree a surviving worker is still editing."""
    official_delta(["ouroboros/loop.py"])
    import supervisor.workers as workers

    conflicting = dict(kind="conflicting", code_conflict_paths=["ouroboros/loop.py"])
    calls = _counted_plans(
        monkeypatch, [_plan(**conflicting), _plan(local_snapshot="ssss", **conflicting)]
    )
    monkeypatch.setattr(workers, "kill_workers", _raising_kill, raising=True)

    resp = asyncio.run(control._apply_managed_merge(None, "assisted"))
    assert resp.status_code == 409
    assert "could not stop workers" in json.loads(resp.body)["error"]
    assert len(calls) == 1
    assert staged_apply_env["staged"] == []


# --- no exit past the fence may leave the pool down (v6.88.1 r4) ------------------------------

def test_replace_family_respawns_the_pool_when_the_apply_raises(
    official_delta, replace_env, monkeypatch
):
    """The fence is taken BEFORE the fenced gate and the preparation, and both shell out to git and
    touch the filesystem — so both can RAISE. Letting that exception reach `api_update_apply`'s
    blanket handler answered a 500 with every worker dead until the process restarted."""
    official_delta(["ouroboros/loop.py"])
    import supervisor.git_ops as git_ops

    def _boom(*_a, **_k):
        raise RuntimeError("rescue snapshot exploded")

    monkeypatch.setattr(git_ops, "prepare_managed_update", _boom, raising=True)

    resp = asyncio.run(control.api_update_apply(_Request({"strategy": "replace"})))
    assert resp.status_code == 409
    assert "after stopping workers" in json.loads(resp.body)["error"]
    assert replace_env["calls"] == ["lock", "kill", "respawn"]


# --- a held update lock is transient, not a failure (v6.88.1 r4) ------------------------------

def _lock_held():
    raise RuntimeError("managed_update.lock is held by another update operation")


@pytest.mark.parametrize("strategy,over", [
    ("auto_merge", {}),
    ("assisted", dict(kind="conflicting", code_conflict_paths=["ouroboros/loop.py"])),
])
def test_a_held_update_lock_is_typed_on_the_staged_paths(
    official_delta, monkeypatch, strategy, over
):
    """The boot check-on-restart thread now holds this same lock across its fetch, so losing the
    race is routine and the next click succeeds. Typed so the UI says "try again in a moment"
    instead of reporting the update as failed."""
    official_delta(["ouroboros/loop.py"])
    import supervisor.update_merge as update_merge

    monkeypatch.setattr(update_merge, "active_update_tx", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge", lambda **_k: _plan(**over), raising=True
    )
    monkeypatch.setattr(update_merge, "acquire_update_lock", _lock_held, raising=True)

    resp = asyncio.run(control._apply_managed_merge(None, strategy))
    assert resp.status_code == 409
    assert json.loads(resp.body)["reason"] == "update_lock_held"


def test_a_held_update_lock_is_typed_for_the_replace_family(
    official_delta, replace_env, monkeypatch
):
    """And nothing is stopped when the lock is lost: the fence is taken UNDER it."""
    official_delta(["ouroboros/loop.py"])
    import supervisor.update_merge as update_merge

    monkeypatch.setattr(update_merge, "acquire_update_lock", _lock_held, raising=True)

    resp = asyncio.run(control.api_update_apply(_Request({"strategy": "replace"})))
    assert resp.status_code == 409
    assert json.loads(resp.body)["reason"] == "update_lock_held"
    assert replace_env["calls"] == []


def _bounded_fetch_spy(monkeypatch, result=(0, "", "")):
    """Record every trip through `git_fetch_bounded` and return the recorder."""
    import supervisor.git_ops as git_ops

    remotes = []
    monkeypatch.setattr(
        git_ops, "git_fetch_bounded",
        lambda remote, **_k: remotes.append(remote) or result, raising=True,
    )
    return remotes


def test_the_boot_update_check_fetch_is_bounded(monkeypatch):
    """The boot check holds the exclusive update lock across its fetch (so it cannot move a
    tracking ref an in-flight apply has already gated), and `acquire_update_lock` is non-blocking —
    so an UNBOUNDED fetch would make every owner-initiated apply answer a lock-held 409 for as long
    as the remote hangs. It must therefore go through `git_fetch_bounded`, which owns a real wall
    clock, and never through bare `git_capture`, which has no timeout at all."""
    import supervisor.git_ops as git_ops

    monkeypatch.setattr(
        git_ops, "_managed_update_target",
        lambda *_a, **_k: ("managed", "dev", "managed/dev"), raising=True,
    )
    monkeypatch.setattr(
        git_ops, "ensure_official_update_remote", lambda *_a, **_k: (True, ""), raising=True
    )
    monkeypatch.setattr(git_ops, "_read_managed_repo_meta", lambda *_a, **_k: {}, raising=True)

    seen = []

    def _capture(cmd, *_a, **_k):
        seen.append(cmd)
        return 1, "", "offline"

    monkeypatch.setattr(git_ops, "git_capture", _capture, raising=True)
    bounded = _bounded_fetch_spy(monkeypatch, result=(1, "", "offline"))
    state = git_ops.compute_managed_update_status(fetch=True)

    assert bounded == ["managed"], "the managed status check must still fetch"
    assert not [cmd for cmd in seen if "fetch" in cmd], "and never through the unbounded helper"
    assert any(str(w).startswith("fetch_error:") for w in state["warnings"])


def test_the_planners_fetch_is_bounded_too(plan_producer, monkeypatch):
    """The OTHER fetch site, and the dangerous one: `_apply_replace_family_fenced` re-plans through
    `plan_managed_update_merge(fetch=True)` with the update lock held AND the whole worker pool
    already stopped, so a stalled remote there is not an exception the fenced try/except can
    convert — it never returns, and the pool stays dead with the lock held. Nothing normalizes THIS
    remote to https either (`ensure_official_update_remote` runs only in the status check), so an
    http-only bound would not bind it at all. Pinned beside its sibling so the two sites cannot
    drift apart again."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    plan_producer()
    seen, stub = [], git_ops.git_capture

    def _capture(cmd, *a, **k):
        seen.append(list(cmd))
        return stub(cmd, *a, **k)

    monkeypatch.setattr(git_ops, "git_capture", _capture, raising=True)
    bounded = _bounded_fetch_spy(monkeypatch)
    update_merge.plan_managed_update_merge(fetch=True)

    assert bounded == ["managed"], "the fenced re-plan must still fetch"
    assert not [cmd for cmd in seen if "fetch" in cmd], "and never through the unbounded helper"


@pytest.mark.skipif(os.name != "posix", reason="process-group kill is POSIX-only")
def test_the_bounded_fetch_terminates_a_stalled_transport(tmp_path, monkeypatch):
    """The bound has to hold for a transport git's own http low-speed knobs cannot touch — those
    are curl options and reach the HTTP(S) transport only, while the planner fetches whatever the
    managed manifest resolved. So this drives a REAL fetch over SSH, the transport the knobs are
    blind to, whose helper never answers, and requires the call to come back anyway.

    It also proves the kill reaches git's CHILD: the sleeping ssh helper inherits our pipes, so
    killing git alone would leave us blocked reading an fd nobody is left to close — which is the
    very hang being bounded."""
    import subprocess
    import time

    import supervisor.git_ops as git_ops

    repo = tmp_path / "stalled-remote-repo"
    repo.mkdir()

    def _git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            cwd=str(repo), check=True, capture_output=True, text=True,
        )

    _git("init", "-b", "ouroboros")
    _git("remote", "add", "stalled", "ssh://git@ouroboros.invalid/ouroboros.git")
    # `git_fetch_bounded` inherits os.environ, so this is the ssh git will actually run: one that
    # never speaks. No network is touched — the helper is replaced before any connection. The stub
    # must SWALLOW ssh's host/command arguments (a bare `sleep 120` would choke on them and exit
    # instantly, letting git fail on its own with rc=128 before the bound ever fires).
    stub = tmp_path / "stalled-ssh"
    stub.write_text("#!/bin/sh\nexec sleep 120\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("GIT_SSH_COMMAND", str(stub))
    monkeypatch.setattr(git_ops, "REPO_DIR", repo, raising=True)

    started = time.monotonic()
    rc, _out, err = git_ops.git_fetch_bounded("stalled", timeout=2.0)
    elapsed = time.monotonic() - started

    assert rc == git_ops.FETCH_TIMEOUT_RC
    assert "terminated" in err
    assert elapsed < 60, "the fetch must be cut off by the wall clock, not by the remote"


@pytest.mark.skipif(os.name != "posix", reason="process-group kill is POSIX-only")
def test_the_bounded_fetch_kills_the_group_after_its_leader_is_gone(tmp_path, monkeypatch):
    """The sibling above stalls with git STILL ALIVE, so naming the group by asking the OS for it
    works there. This is the case that made asking the wrong thing to do: the transport helper
    exits, git gives up and exits too, and the only process left is a grandchild that inherited our
    stdout and stderr — so `communicate()` stays blocked on fds nobody is left to close even though
    the process we launched is gone. `os.getpgid(proc.pid)` cannot answer for a leader in that
    state, and its raise was swallowed by the blanket `except`, meaning the ONE process actually
    holding the fetch open was never signalled. The group id is therefore recorded at launch, where
    `start_new_session=True` guarantees it equals the pid, and killed directly.

    `os.getpgid` is monkeypatched to raise here so the regression is deterministic rather than
    dependent on when the OS reaps a leader: if the kill ever goes back to looking the group up,
    this test fails with the helper still running."""
    import signal
    import subprocess
    import time

    import supervisor.git_ops as git_ops

    repo = tmp_path / "orphaned-helper-repo"
    repo.mkdir()

    def _git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            cwd=str(repo), check=True, capture_output=True, text=True,
        )

    _git("init", "-b", "ouroboros")
    _git("remote", "add", "orphaned", "ssh://git@ouroboros.invalid/ouroboros.git")
    # The stub backgrounds a child that keeps this fetch's pipes and then EXITS, which is what makes
    # git exit as well. No network is touched. The child records its pid so the kill can be checked
    # against a real process rather than against a log line.
    helper_pid_file = tmp_path / "helper.pid"
    stub = tmp_path / "orphaning-ssh"
    stub.write_text(
        "#!/bin/sh\n"
        f"sh -c 'echo $$ > {helper_pid_file}; exec sleep 120' &\n"
        "exec true\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("GIT_SSH_COMMAND", str(stub))
    monkeypatch.setattr(git_ops, "REPO_DIR", repo, raising=True)

    def _no_lookup(_pid):
        raise ProcessLookupError("the leader is gone; the group cannot be looked up")

    monkeypatch.setattr(os, "getpgid", _no_lookup, raising=True)

    started = time.monotonic()
    rc, _out, err = git_ops.git_fetch_bounded("orphaned", timeout=2.0)
    elapsed = time.monotonic() - started

    assert rc == git_ops.FETCH_TIMEOUT_RC
    assert "terminated" in err
    assert elapsed < 60, "the fetch must be cut off by the wall clock, not by the helper"

    helper_pid = int(helper_pid_file.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(helper_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(helper_pid, signal.SIGKILL)  # do not leak it out of the test run
        raise AssertionError(
            f"the transport helper {helper_pid} outlived the fetch that was holding it"
        )


def test_the_group_liveness_probe_reports_a_group_it_could_not_answer_for_as_alive(monkeypatch):
    """The kill above is VERIFIED, and the probe that verifies it exists for callers who need "is
    anything left". So it fails CLOSED: only an explicit `ProcessLookupError` — the OS saying the
    group is gone — counts as gone. A refused signal, a bad id, or Windows (which has no group to
    probe this way) all answer "still alive", because "we could not tell" is not the same fact as
    "nothing is left", and the cheap cost of the wrong guess is one extra log line."""
    from ouroboros import platform_layer

    class _Os:
        """Only the one call the probe makes; patched as the MODULE's `os` so the real one — which
        the interpreter running this test needs — is never touched."""

        def __init__(self, raises):
            self.raises = raises

        def killpg(self, _pgid, _sig):
            if self.raises is not None:
                raise self.raises

    def _probe(raises, *, windows=False):
        monkeypatch.setattr(platform_layer, "IS_WINDOWS", windows, raising=True)
        monkeypatch.setattr(platform_layer, "os", _Os(raises), raising=True)
        return platform_layer.process_group_is_alive(4242)

    assert _probe(ProcessLookupError("no such group")) is False, "the OS said it is gone"
    assert _probe(None) is True, "the signal landed, so something is there"
    for unanswerable in (PermissionError("not ours"), OSError("EINVAL"), ValueError("bad id")):
        assert _probe(unanswerable) is True, unanswerable
    assert _probe(ProcessLookupError("no such group"), windows=True) is True, (
        "no group to probe is not a proof that nothing is left"
    )


def test_a_stalled_fenced_re_plan_gives_the_pool_back(official_delta, replace_env, monkeypatch):
    """What the bound is FOR. The fenced re-plan runs with the pool stopped and the exclusive lock
    held; the timeout turns a hang into an ordinary unavailable plan, and the replace path must
    then take its normal failed-apply exit — respawn the workers, prepare nothing — instead of
    holding the lock (and the dead pool) until the process is restarted by hand."""
    official_delta([])
    import supervisor.update_merge as update_merge

    plans = iter([
        _plan(),  # the pre-lock disclosure fetch answered
        {"available": False, "kind": "unavailable",
         "error": "managed fetch timed out: fetch from 'managed' exceeded 300s and was terminated"},
    ])
    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge", lambda **_k: next(plans), raising=True
    )

    resp = asyncio.run(control.api_update_apply(_Request({"strategy": "replace"})))

    assert resp.status_code == 409
    body = json.loads(resp.body)
    assert body["error"] == "no managed update available"
    assert "timed out" in body["plan_error"]
    assert replace_env["calls"] == ["lock", "kill", "respawn"]


# --- the fence must be VERIFIED, not inferred (v6.88.1 r5) ------------------------------------

class _FakeWorker:
    """A worker handle with the FULL surface the survivor retry uses — `pid`, `join` and
    `is_alive`. Anything less and `terminate_worker_survivors` would report the handle through its
    blanket `except` (an unreadable handle is not a dead one), which is a real code path but not
    the one these tests document: a regression that deleted the kill/join/re-check outright would
    still have left them green. The deliberately unreadable case has its own `_Opaque` fake."""

    def __init__(self, alive, pid=0):
        self.proc = self
        self.pid = pid
        self._alive = alive

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return self._alive


def _fenced_pool(monkeypatch, workers_by_id):
    """Install a pool for the fence to verify, and return the pids its survivor retry killed."""
    import ouroboros.platform_layer as platform_layer
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "WORKERS", dict(workers_by_id), raising=True)
    # The real one clears WORKERS unconditionally after a best-effort teardown; that is exactly
    # why the fence snapshots the handles BEFORE calling it.
    monkeypatch.setattr(
        workers, "kill_workers",
        lambda **_k: workers.WORKERS.clear(), raising=True,
    )
    killed = []
    monkeypatch.setattr(
        platform_layer, "kill_pid_tree", lambda pid: killed.append(pid), raising=True
    )
    return killed


def test_the_fence_fails_when_a_worker_outlives_the_kill(monkeypatch):
    """`kill_workers` joins with a timeout, force-kills survivors without checking the outcome, and
    then clears `WORKERS` regardless — so a process tree that outlived both joins raises nothing and
    used to look exactly like success. The SHA pin, the post-stop drift recheck and the clean-tree
    proof are all justified by "nothing else can write", so an unproven stop must fail the fence."""
    killed = _fenced_pool(
        monkeypatch, {1: _FakeWorker(alive=False, pid=11), 2: _FakeWorker(alive=True, pid=22)}
    )
    fence = control._fence_workers_for_update("reason")
    assert fence.ok is False
    # The HANDLE is carried back, not just a count: `kill_workers` has already dropped it from
    # ``WORKERS``, so this list is the only remaining evidence that a prior writer may still be
    # running — and it is what stops the caller from starting a replacement pool beside it.
    assert len(fence.survivors) == 1
    # And it got there through the real retry — process-tree kill, join, re-check — not through the
    # error fallback: only the worker that outlived the stop was retried, and it is still alive.
    assert killed == [22]


def test_the_fence_passes_only_when_every_worker_is_proven_dead(monkeypatch):
    _fenced_pool(monkeypatch, {1: _FakeWorker(alive=False), 2: _FakeWorker(alive=False)})
    assert control._fence_workers_for_update("reason") == control._FenceResult(True, [])


def test_an_unreadable_worker_handle_is_not_counted_as_dead(monkeypatch):
    """Fail closed on the read itself: a handle we cannot interrogate is not a worker we are
    entitled to call stopped. This is the ONE case that is meant to reach the survivor list through
    the retry's blanket `except` — the handle genuinely cannot be killed, joined or re-checked."""
    class _Opaque:
        @property
        def proc(self):
            raise OSError("handle gone")

    killed = _fenced_pool(monkeypatch, {1: _Opaque()})
    fence = control._fence_workers_for_update("reason")
    assert fence.ok is False
    assert len(fence.survivors) == 1  # unreadable is carried as unproven, not discarded as dead
    assert killed == [], "nothing to kill: the handle could not even be read"


def test_the_fence_snapshot_cannot_race_a_respawn(monkeypatch):
    """The snapshot IS the proof, so nothing may install a worker into the pool between it and the
    kill: such a handle gets torn down by `kill_workers` but is never inspected by the
    verification pass, and the fence would then report success on a stop it never proved. Reading
    `WORKERS` in the caller left exactly that window open — every other mutator serializes on the
    worker lifecycle lock, so snapshot and kill have to happen inside ONE hold of it."""
    import threading

    import supervisor.workers as workers

    monkeypatch.setattr(workers, "WORKERS", {1: _FakeWorker(alive=False)}, raising=True)
    intruded = threading.Event()
    race = {}

    def _respawn_like():
        # What `respawn_worker` does: swap a fresh slot in under the lifecycle lock.
        with workers._WORKER_LIFECYCLE_LOCK:
            workers.WORKERS[2] = _FakeWorker(alive=True)
            intruded.set()

    def _kill(**_k):
        thread = threading.Thread(target=_respawn_like, daemon=True)
        race["thread"] = thread
        thread.start()
        race["landed"] = intruded.wait(timeout=0.5)
        workers.WORKERS.clear()

    monkeypatch.setattr(workers, "kill_workers", _kill, raising=True)

    assert control._fence_workers_for_update("reason") == control._FenceResult(True, [])
    race["thread"].join(timeout=5)  # it lands once the fence releases the lock, never during
    assert race["landed"] is False, "a respawn must not be able to interleave with the fence"


def test_a_raising_teardown_still_hands_back_the_handles_it_captured(monkeypatch):
    """`kill_workers` clears ``WORKERS`` PARTWAY THROUGH and keeps going (task bookkeeping, queue
    snapshot, audit append), so a raise from that tail leaves the pool empty with a writer still
    running. When the teardown exception propagated, the fence lost the handles with it and
    answered `(False, [])` — the outcome the abort path reads as "nothing survived, a replacement
    pool is safe", which is how two generations end up on one checkout.

    The teardown therefore returns a RECEIPT and the verification pass runs on the failing path
    with the same evidence it runs on for the succeeding one."""
    import supervisor.workers as workers

    survivor = _FakeWorker(alive=True, pid=22)
    _fenced_pool(monkeypatch, {1: _FakeWorker(alive=False, pid=11), 2: survivor})

    def _clear_then_raise(**_k):
        workers.WORKERS.clear()
        raise RuntimeError("zombie prevention cleanup failed")

    monkeypatch.setattr(workers, "kill_workers", _clear_then_raise, raising=True)

    fence = control._fence_workers_for_update("reason")
    assert fence.ok is False
    assert fence.survivors == [survivor]
    # ...and the caller's abort keeps the pool DOWN, because a survivor is not a respawn case.
    resp = control._abort_after_failed_fence(fence, "what")
    assert json.loads(resp.body)["reason"] == "worker_fence_survivors"


def test_a_raising_teardown_with_every_worker_dead_refuses_but_stays_respawnable(monkeypatch):
    """The other half of the receipt: the handles ARE all proven dead, so a replacement pool is
    safe — but the teardown did not finish, so the update does not get to run behind it."""
    import supervisor.workers as workers

    _fenced_pool(monkeypatch, {1: _FakeWorker(alive=False, pid=11)})

    def _clear_then_raise(**_k):
        workers.WORKERS.clear()
        raise RuntimeError("zombie prevention cleanup failed")

    monkeypatch.setattr(workers, "kill_workers", _clear_then_raise, raising=True)

    assert control._fence_workers_for_update("reason") == control._FenceResult(False, [])


# --- no non-restarting exit past the fence may leave the pool down (v6.88.1 r5) ---------------

@pytest.mark.parametrize("strategy,over", [
    ("auto_merge", {}),
    ("assisted", dict(kind="conflicting", code_conflict_paths=["ouroboros/loop.py"])),
])
def test_a_failed_fence_respawns_the_pool_on_the_staged_paths(
    official_delta, monkeypatch, staged_apply_env, strategy, over
):
    """`kill_workers` performs fallible teardown BEFORE it clears the pool, so a raise can land
    after some workers are already dead, and a verification failure means survivors it could not
    kill. Either way the staged flows used to return straight out, leaving normal task processing
    unavailable with no restart coming to revive it."""
    official_delta(["ouroboros/loop.py"])
    _counted_plans(monkeypatch, [_plan(**over), _plan(**over)])
    monkeypatch.setattr(control, "_fence_workers_for_update", _failed_fence)
    respawned = []
    monkeypatch.setattr(
        control, "_respawn_workers_after_failed_update", lambda: respawned.append(1)
    )

    resp = asyncio.run(control._apply_managed_merge(None, strategy))
    assert resp.status_code == 409
    assert "could not stop workers" in json.loads(resp.body)["error"]
    assert respawned == [1]
    assert staged_apply_env["staged"] == []


@pytest.mark.parametrize("strategy,over", [
    ("auto_merge", {}),
    ("assisted", dict(kind="conflicting", code_conflict_paths=["ouroboros/loop.py"])),
])
def test_a_raise_past_the_fence_respawns_the_pool_on_the_staged_paths(
    official_delta, monkeypatch, staged_apply_env, strategy, over
):
    """Post-fence planning, the rescue snapshot, the tx marker and the apply all shell out to git or
    touch the filesystem. An exception there escaped through the `finally` that releases only the
    lock, so the API answered a 500 with every worker dead until the process restarted."""
    official_delta(["ouroboros/loop.py"])
    import supervisor.git_ops as git_ops

    _counted_plans(
        monkeypatch,
        [_plan(**over), _plan(merge_commit="dddd", local_snapshot="ssss", **over)],
    )

    def _boom(*_a, **_k):
        raise RuntimeError("rescue snapshot exploded")

    monkeypatch.setattr(git_ops, "_create_rescue_snapshot", _boom, raising=True)
    respawned = []
    monkeypatch.setattr(
        control, "_respawn_workers_after_failed_update", lambda: respawned.append(1)
    )

    resp = asyncio.run(control._apply_managed_merge(None, strategy))
    assert resp.status_code == 409
    assert "after stopping workers" in json.loads(resp.body)["error"]
    assert respawned == [1]


# --- a fence with SURVIVORS may not be answered with a replacement pool (v6.88.1 r6) ----------

def test_the_survivor_retry_reports_a_worker_it_still_cannot_kill(monkeypatch):
    """`kill_workers` makes one force-kill pass whose outcome it never checks. This is the retry
    that turns "probably dead" into an answer: kill the whole process TREE (a worker's own children
    hold the checkout too), join, and hand back only what is still alive."""
    import ouroboros.platform_layer as platform_layer
    import supervisor.workers as workers

    class _Proc:
        def __init__(self, pid, alive):
            self.pid, self._alive = pid, alive

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self._alive

    class _Handle:
        def __init__(self, pid, alive):
            self.proc = _Proc(pid, alive)

    killed = []
    monkeypatch.setattr(
        platform_layer, "kill_pid_tree", lambda pid: killed.append(pid), raising=True
    )
    reaped, stubborn = _Handle(11, alive=False), _Handle(22, alive=True)

    assert workers.terminate_worker_survivors([reaped, stubborn]) == [stubborn]
    assert killed == [11, 22]


def test_a_fence_with_survivors_never_starts_a_replacement_pool(
    official_delta, replace_env, monkeypatch
):
    """The FIX_FIRST case. A survivor is no longer in ``WORKERS`` (kill_workers cleared it), so
    `ensure_worker_pool_started` would see an empty pool and run a fresh generation against the same
    checkout as the writer that would not die. This exit therefore stays down — typed, so the owner
    is told a restart is required rather than being handed a silent half-recovery."""
    official_delta(["ouroboros/loop.py"])
    monkeypatch.setattr(control, "_fence_workers_for_update", _survivor_fence)

    resp = asyncio.run(control.api_update_apply(_Request({"strategy": "replace"})))
    body = json.loads(resp.body)

    assert resp.status_code == 409
    assert body["reason"] == "worker_fence_survivors"
    assert "restarted" in body["error"]
    assert replace_env["calls"] == ["lock"], "no respawn beside a writer we could not prove dead"


@pytest.mark.parametrize("strategy,over", [
    ("auto_merge", {}),
    ("assisted", dict(kind="conflicting", code_conflict_paths=["ouroboros/loop.py"])),
])
def test_a_fence_with_survivors_keeps_the_staged_paths_down(
    official_delta, monkeypatch, staged_apply_env, strategy, over
):
    """Same rule on the staged flows — and nothing is staged either."""
    official_delta(["ouroboros/loop.py"])
    _counted_plans(monkeypatch, [_plan(**over), _plan(**over)])
    monkeypatch.setattr(control, "_fence_workers_for_update", _survivor_fence)
    respawned = []
    monkeypatch.setattr(
        control, "_respawn_workers_after_failed_update", lambda: respawned.append(1)
    )

    resp = asyncio.run(control._apply_managed_merge(None, strategy))
    assert resp.status_code == 409
    assert json.loads(resp.body)["reason"] == "worker_fence_survivors"
    assert respawned == []
    assert staged_apply_env["staged"] == []


def test_a_latched_survivor_refuses_the_next_apply_before_it_touches_anything(
    official_delta, replace_env, monkeypatch
):
    """The survivor refusal PROMISES that task processing stays down until a restart, so something
    has to remember it. Nothing did: `kill_workers` had already cleared ``WORKERS``, the update lock
    was released by the caller's `finally`, and an immediate retry therefore snapshotted an EMPTY
    pool, found no survivors, drained no chat turn and manufactured a clean fence proof while the
    un-killable prior writer was still running — the exact coexistence the refusal exists to
    prevent. The fence now latches the handles and re-reads that latch first."""
    official_delta(["ouroboros/loop.py"])
    killed = _fenced_pool(monkeypatch, {1: _FakeWorker(alive=True, pid=77)})

    first = asyncio.run(control.api_update_apply(_Request({"strategy": "replace"})))
    assert first.status_code == 409
    assert json.loads(first.body)["reason"] == "worker_fence_survivors"

    # The pool is empty now and a fresh fence would sail through it, so the SECOND apply is the one
    # that matters: same typed refusal, and it never reaches preparation.
    second = asyncio.run(control.api_update_apply(_Request({"strategy": "replace"})))
    assert second.status_code == 409
    assert json.loads(second.body)["reason"] == "worker_fence_survivors"
    assert replace_env["calls"] == ["lock", "lock"], "no prepare, no reset, no respawn"
    # And it is a re-verified refusal, not a cached one: the latched handle is killed and re-checked
    # again, so a survivor that finally died would clear the latch instead of blocking forever.
    assert killed == [77, 77]


# --- the fence also excludes the IN-PROCESS writers (v6.88.1 r6) ------------------------------

def test_the_drain_fails_closed_on_the_in_process_chat_writers(monkeypatch):
    """Stopping the worker pool excludes nothing in this process: the direct-chat agent is a THREAD
    holding the full operator-control tool profile. It cannot be killed, so the fence drains it —
    and a liveness probe it could not even run is not a proof of quiescence."""
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "chat_turn_liveness", lambda: (True, "t-42", None), raising=True)
    assert workers.drain_repo_writers(timeout=0.0, poll=0.01) == ["direct_chat_turn:t-42"]

    def _unreadable():
        raise RuntimeError("liveness probe failed")

    monkeypatch.setattr(workers, "chat_turn_liveness", _unreadable, raising=True)
    assert workers.drain_repo_writers(timeout=0.0, poll=0.01) == ["direct_chat_turn:unreadable"]

    monkeypatch.setattr(workers, "chat_turn_liveness", lambda: (False, None, None), raising=True)
    assert workers.drain_repo_writers(timeout=0.0, poll=0.01) == []


def test_the_fence_fails_when_an_in_process_writer_will_not_drain(monkeypatch):
    """A turn that will not quiesce fails the fence rather than being written over — the clean-tree
    and base proofs are claims about the WHOLE runtime, not just the pool."""
    import supervisor.workers as workers

    _fenced_pool(monkeypatch, {})
    monkeypatch.setattr(
        workers, "drain_repo_writers", lambda *_a, **_k: ["direct_chat_turn:t-42"], raising=True
    )

    fence = control._fence_workers_for_update("reason")
    assert fence.ok is False
    # No worker SURVIVED — the pool really is down, it is a thread that would not finish. So this
    # abort is the recoverable kind and may bring the pool back.
    assert fence.survivors == []
    assert workers.repo_writer_admission_closed(), "admission stays shut behind a failed fence"


def test_a_closed_admission_refuses_a_direct_chat_turn(monkeypatch):
    """Closing admission is the half of the fence that a `kill_workers` cannot do. It has to be
    VISIBLE: the turn is refused with an explanation instead of being silently dropped or deferred
    into a window whose tools would refuse it anyway."""
    import supervisor.workers as workers

    ran, told = [], []
    monkeypatch.setattr(
        workers, "_handle_chat_direct_locked", lambda *_a, **_k: ran.append(1), raising=True
    )
    monkeypatch.setattr(
        workers, "send_with_budget", lambda _chat, text, *_a, **_k: told.append(text), raising=True
    )

    workers.close_repo_writer_admission("managed_update: test")
    workers.handle_chat_direct(1, "edit a file for me")
    assert ran == []
    assert told and "managed update is holding the repository" in told[0]

    workers.open_repo_writer_admission()
    workers.handle_chat_direct(1, "edit a file for me")
    assert ran == [1]


def test_an_admitted_turn_is_visible_to_the_drain_before_it_looks_busy(monkeypatch):
    """The fail-open the lease closes. `handle_chat_direct` used to check a flag and only much later
    — after a budget/state read and, on first use, the construction of the whole agent — reach
    `_run_chat_task`, which is what sets the `_busy` flag `chat_turn_liveness` reads. A turn admitted
    microseconds before the fence closed therefore reported quiescent for that entire startup
    window, the fence returned ok, and the update ran beside a writer holding the full
    operator-control tool profile. Admission and registration are now one atomic step."""
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "chat_turn_liveness", lambda: (False, None, None), raising=True)

    lease = workers.acquire_repo_writer_lease("direct_chat")
    assert lease, "admission was open, so the turn is in"
    workers.close_repo_writer_admission("managed_update: test")

    # Not busy — this turn has not reached `_run_chat_task` — but it is unambiguously ADMITTED.
    assert workers.drain_repo_writers(timeout=0.0, poll=0.01) == [lease]
    assert workers.acquire_repo_writer_lease("direct_chat") is None, "and nothing may join it"

    workers.release_repo_writer_lease(lease)
    assert workers.drain_repo_writers(timeout=0.0, poll=0.01) == []


def test_a_queued_direct_turn_rechecks_admission_after_it_waits(monkeypatch):
    """Both chat routes decided admission BEFORE taking their serialization lock, so a turn that
    passed the check and then queued behind another turn began AFTER the fence closed without ever
    looking again. The decision is re-taken inside the boundary; the pre-check is only there to save
    the owner a wait."""
    import supervisor.workers as workers

    ran, told = [], []
    monkeypatch.setattr(
        workers, "_handle_chat_direct_locked", lambda *_a, **_k: ran.append(1), raising=True
    )
    monkeypatch.setattr(
        workers, "send_with_budget", lambda _chat, text, *_a, **_k: told.append(text), raising=True
    )
    # Admission was OPEN when this turn queued ...
    monkeypatch.setattr(
        workers, "_refuse_closed_repo_writer_admission", lambda _chat: False, raising=True
    )
    # ... and the fence closed it while the turn waited for `_chat_agent_lock`.
    workers.close_repo_writer_admission("managed_update: test")

    workers.handle_chat_direct(1, "edit a file for me")

    assert ran == [], "a queued turn must re-decide admission inside the serialization boundary"
    assert told and "managed update is holding the repository" in told[0]


def test_a_queued_ephemeral_turn_rechecks_admission_after_it_waits(monkeypatch):
    """Same hole on the ephemeral route, which runs the SAME agent config and tool profile — and it
    queues on its own lock, so it has its own version of the wait."""
    import supervisor.state as state
    import supervisor.workers as workers

    told = []
    monkeypatch.setattr(state, "load_state", lambda *_a, **_k: {}, raising=True)
    monkeypatch.setattr(state, "budget_remaining", lambda *_a, **_k: 100.0, raising=True)
    monkeypatch.setattr(
        workers, "send_with_budget", lambda _chat, text, *_a, **_k: told.append(text), raising=True
    )
    monkeypatch.setattr(
        workers, "_run_chat_task",
        lambda *_a, **_k: pytest.fail("an ephemeral turn ran behind a closed admission"),
        raising=True,
    )
    monkeypatch.setattr(
        workers, "_refuse_closed_repo_writer_admission", lambda _chat: False, raising=True
    )
    workers.close_repo_writer_admission("managed_update: test")

    workers.handle_chat_ephemeral(1, "edit a file for me")

    assert told and "managed update is holding the repository" in told[0]


def test_a_fence_that_cannot_reach_the_worker_module_is_a_typed_refusal(monkeypatch):
    """Every caller branches on the fence RESULT inside a `finally` that releases the update lock,
    so an exception escaping the fence unwinds into the blanket 500 handler — the one answer no
    caller can act on, and the one the owner cannot tell apart from a server fault. Even "the worker
    module could not be reached at all" therefore comes back typed."""
    import sys

    class _UnreachableSupervisorPackage:
        """A `supervisor` package whose attributes cannot be read, so `from supervisor import
        workers` raises instead of binding."""

        def __getattr__(self, name):
            raise ImportError(f"cannot import supervisor.{name}")

    monkeypatch.setitem(sys.modules, "supervisor", _UnreachableSupervisorPackage())

    assert control._fence_workers_for_update("reason") == control._FenceResult(False, [])


# --- the THIRD writer class: services rooted in the checkout (v6.88.1 r6) ---------------------

class _FakeServiceProc:
    """A service process handle with the surface the service manager reads: `pid`, `poll`, `wait`."""

    def __init__(self):
        self.pid = 4242
        self.rc = None

    def poll(self):
        return self.rc

    def wait(self, timeout=None):
        return self.rc


def _repo_rooted_service(monkeypatch, *, dies):
    """Register ONE running service holding a managed-repository writer lease."""
    import ouroboros.tools.services as services
    import supervisor.workers as workers

    lease = workers.acquire_repo_writer_lease("service:web")
    proc = _FakeServiceProc()
    record = services.ServiceRecord(
        name="web",
        service_id="t-1:web",
        task_id="t-1",
        cmd=["python", "-m", "http.server"],
        cwd="/repo",
        log_path=pathlib.Path("web.log"),
        proc=proc,
        pgid=0,  # no group => the manager falls back to the process-tree kill below
        repo_writer_lease=lease,
    )
    monkeypatch.setattr(services, "_SERVICES", {record.service_id: record}, raising=True)
    monkeypatch.setattr(
        services, "kill_process_tree",
        (lambda p: setattr(p, "rc", -9)) if dies else (lambda _p: None),
        raising=True,
    )
    return lease, record


def test_a_service_started_in_the_checkout_is_a_writer_the_fence_can_see(monkeypatch):
    """`start_service` runs an arbitrary command in the active workspace and leaves a PROCESS
    behind — a `keep_alive` one outlives the whole task. Neither the pool teardown (it is not a
    worker) nor the chat drain (it is not a turn) reaches it, so the fence used to succeed with a
    repository writer still running. It takes the same admission lease as the in-process writers,
    which is what makes it visible; terminating it retires the lease."""
    import ouroboros.tools.services as services
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "chat_turn_liveness", lambda: (False, None, None), raising=True)
    lease, record = _repo_rooted_service(monkeypatch, dies=True)

    assert workers.drain_repo_writers(timeout=0.0, poll=0.01) == [lease]
    assert services.terminate_repo_rooted_services() == []
    assert record.repo_writer_lease == "", "the lease is retired only against a PROVEN dead process"
    assert workers.drain_repo_writers(timeout=0.0, poll=0.01) == []


def test_a_service_that_will_not_die_keeps_its_lease_and_fails_the_fence(monkeypatch):
    """The refusal needs no machinery of its own: a service we could not stop simply keeps its
    lease, so the fence's existing drain reports it and the update is refused on exactly the
    evidence every other in-process writer is refused on."""
    import ouroboros.tools.services as services
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "chat_turn_liveness", lambda: (False, None, None), raising=True)
    lease, record = _repo_rooted_service(monkeypatch, dies=False)

    assert services.terminate_repo_rooted_services() == [lease]
    assert record.repo_writer_lease == lease
    assert workers.drain_repo_writers(timeout=0.0, poll=0.01) == [lease]


def test_the_fence_terminates_the_services_rooted_in_the_checkout(monkeypatch):
    """The wiring itself: a fence that stopped the pool but left a repo-rooted service running would
    hard-reset the tree underneath it."""
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "chat_turn_liveness", lambda: (False, None, None), raising=True)
    _fenced_pool(monkeypatch, {})
    lease, record = _repo_rooted_service(monkeypatch, dies=True)

    assert control._fence_workers_for_update("reason") == control._FenceResult(True, [])
    assert record.proc.poll() is not None, "the fence stopped it"
    assert workers.admitted_repo_writers() == [], f"and retired {lease}"


def test_only_services_inside_the_checkout_take_a_repository_lease():
    """An external-workspace service writes nothing the update touches, so leasing it would refuse
    updates for no reason. A root we cannot resolve at all is the fail-CLOSED case: it counts as
    the repository, which costs nothing while admission is open."""
    import ouroboros.tools.services as services

    class _Ctx:
        repo_dir = pathlib.Path("/managed/repo")

    assert services._service_writes_managed_repo(_Ctx(), pathlib.Path("/managed/repo")) is True
    assert services._service_writes_managed_repo(_Ctx(), pathlib.Path("/managed/repo/web")) is True
    assert services._service_writes_managed_repo(_Ctx(), pathlib.Path("/elsewhere/app")) is False

    class _UnreadableCtx:
        @property
        def repo_dir(self):
            raise OSError("no repo root")

    assert services._service_writes_managed_repo(_UnreadableCtx(), pathlib.Path("/elsewhere")) is True


def test_a_repo_rooted_service_cannot_start_behind_a_closed_admission():
    """The other half: closing admission has to stop NEW services too, or the fence drains the ones
    it found and a fresh one starts behind it."""
    import ouroboros.tools.services as services
    import supervisor.workers as workers

    workers.close_repo_writer_admission("managed_update: test")
    lease, refusal = services._acquire_repo_writer_lease("service:web")
    assert lease == ""
    assert refusal.startswith("⚠️ SERVICE_REPO_LOCKED")
    assert "managed_update: test" in refusal

    workers.open_repo_writer_admission()
    lease, refusal = services._acquire_repo_writer_lease("service:web")
    assert lease and refusal == ""
    workers.release_repo_writer_lease(lease)


# --- ...and the ones a POOLED WORKER started, which no registry here can see (v6.88.1 r7) ------
#
# `_SERVICES` and the admission lease both live in MODULE GLOBALS. A `start_service` executed
# inside a pooled multiprocessing worker fills the WORKER's copies, so the supervisor's fence
# enumerates nothing — and the service is spawned into its own session with an arbitrary command,
# so killing the worker does not kill it either. The durable custody ledger is the registry both
# processes share, so the marker and the sweep go there.


class _FastGraceClock:
    """`time` for `terminate_repo_writer_processes`, so the 3s kill grace is not real seconds.

    Every reading advances, so the FIRST post-kill probe of a survivor is already past the deadline
    — which is exactly the boundary the production loop is written to hit, just reached instantly.
    """

    def __init__(self):
        self._t = 0.0

    def monotonic(self):
        self._t += 10.0
        return self._t

    def sleep(self, _seconds):
        return None


def _ledgered_repo_writer(ledger, *, pid=9911, pgid=9911, repo_writer=True, purpose="service:web"):
    """Write ONE custody record for a service rooted in the checkout.

    No `fingerprint` block, so `_fingerprint_matches` reduces to the liveness probe the tests drive
    directly — the identity check itself has its own coverage in the custody suite."""
    entry = {
        "ts": "2026-01-01T00:00:00Z", "pid": pid, "pgid": pgid, "purpose": purpose,
        "scope": "task", "owner_task": "t-1", "session_id": "s-1",
    }
    if repo_writer:
        entry["repo_writer"] = True
    ledger.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return entry


def _custody_liveness(monkeypatch, *, alive):
    """Drive the ledger sweep's probes and capture what it killed. `alive` is a mutable one-key dict
    so a test can make the kill actually work."""
    import ouroboros.process_custody as process_custody

    killed = []
    monkeypatch.setattr(process_custody, "pid_is_alive", lambda _pid: alive["v"], raising=True)
    monkeypatch.setattr(
        process_custody, "process_group_is_alive", lambda _pgid: alive["v"], raising=True
    )
    monkeypatch.setattr(
        process_custody, "kill_process_group_id", lambda pgid: killed.append(pgid), raising=True
    )
    return killed


def test_a_worker_started_service_is_marked_in_the_ledger_and_a_plain_process_is_not(
    _hermetic_custody_ledger,
):
    """The marker is what makes the sweep both exhaustive and NARROW: the ledger also carries
    workspace executors, browser helpers and fetch subprocesses, and a fence that killed those would
    be a denial of service dressed up as a safety gate. It is written only when true, so every
    record that existed before this change keeps its exact shape."""
    import ouroboros.process_custody as process_custody

    plain = process_custody.record_process(
        pathlib.Path("/unused"), pid=os.getpid(), cmd=["x"], purpose="browser", scope="task",
    )
    marked = process_custody.record_process(
        pathlib.Path("/unused"), pid=os.getpid(), cmd=["x"], purpose="service:web", scope="task",
        repo_writer=True,
    )

    assert "repo_writer" not in plain
    assert marked["repo_writer"] is True
    written = [
        json.loads(line)
        for line in _hermetic_custody_ledger.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert [entry.get("repo_writer") for entry in written] == [None, True]


def test_start_service_marks_exactly_the_services_that_write_the_checkout():
    """One predicate, two consumers. `_service_writes_managed_repo` decides BOTH the in-process
    lease and the durable ledger marker, from the same evaluation — two independent judgements of
    "is this in the checkout" would eventually disagree, and the disagreement that matters is a
    service the lease covers and the ledger does not.

    Pinned at SOURCE: driving `_start_service` to its spawn needs a real workspace, a real log file
    and a real subprocess, and what this pin is about is the binding, not the spawn."""
    import inspect

    import ouroboros.tools.services as services

    source = inspect.getsource(services._start_service)
    predicate = source.index("writes_managed_repo = _service_writes_managed_repo(ctx, workdir)")
    lease = source.index("if writes_managed_repo:", predicate)
    marker = source.index("repo_writer=writes_managed_repo,", lease)
    assert predicate < lease < marker
    assert "_acquire_repo_writer_lease(" in source[lease:marker]


def test_the_fence_is_blocked_by_a_ledgered_service_it_cannot_prove_dead(
    monkeypatch, _hermetic_custody_ledger
):
    """The BLOCK, end to end. The fence used to exempt worker-started services on the theory that
    they die with their worker's process tree; they do not. So the ledgered writer is killed by
    GROUP and then re-probed, and a fence that cannot prove it gone refuses — an arbitrary command
    still running inside a checkout the update is about to hard-reset is a writer like any other."""
    import ouroboros.process_custody as process_custody
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "chat_turn_liveness", lambda: (False, None, None), raising=True)
    monkeypatch.setattr(process_custody, "time", _FastGraceClock(), raising=True)
    _fenced_pool(monkeypatch, {})
    _ledgered_repo_writer(_hermetic_custody_ledger)
    killed = _custody_liveness(monkeypatch, alive={"v": True})  # nothing we send makes it exit

    fence = control._fence_workers_for_update("reason")

    assert fence.ok is False
    assert fence.survivors == [], "it is not a pool worker: there is no handle to latch"
    assert fence.blocked == ("service:web#9911",)
    assert killed == [9911], "it reached the refusal through a real kill, not a skipped one"


def test_a_ledgered_service_proven_dead_does_not_block_the_update(
    monkeypatch, _hermetic_custody_ledger
):
    """The other half, and the normal path this must not break: the sweep is a kill plus a PROOF,
    so a service that actually exits leaves the fence clean."""
    import ouroboros.process_custody as process_custody
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "chat_turn_liveness", lambda: (False, None, None), raising=True)
    _fenced_pool(monkeypatch, {})
    _ledgered_repo_writer(_hermetic_custody_ledger)
    alive = {"v": True}
    killed = _custody_liveness(monkeypatch, alive=alive)
    monkeypatch.setattr(
        process_custody, "kill_process_group_id",
        lambda pgid: killed.append(pgid) or alive.__setitem__("v", False), raising=True,
    )

    assert control._fence_workers_for_update("reason") == control._FenceResult(True, [])
    assert killed == [9911]


def test_a_ledgered_process_that_is_not_a_repo_writer_is_left_alone(
    monkeypatch, _hermetic_custody_ledger
):
    """The narrowing, pinned as behaviour rather than as a comment: an unmarked record is a browser
    helper or a workspace executor, and the update fence has no business killing it."""
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "chat_turn_liveness", lambda: (False, None, None), raising=True)
    _fenced_pool(monkeypatch, {})
    _ledgered_repo_writer(_hermetic_custody_ledger, repo_writer=False, purpose="browser")
    killed = _custody_liveness(monkeypatch, alive={"v": True})

    assert control._fence_workers_for_update("reason") == control._FenceResult(True, [])
    assert killed == [], "an unmarked ledger entry is not the update's business"


def test_an_unreadable_custody_ledger_is_not_evidence_of_an_empty_one(monkeypatch, tmp_path):
    """`_read_ledger` swallows OSError and answers `[]` — which is the reaper's correct degradation
    and the fence's worst one: "no repository writers" is exactly the answer a gate must never be
    handed by a file it could not read. The sweep probes readability first and lets the failure
    reach the caller as a refusal."""
    import ouroboros.process_custody as process_custody
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "chat_turn_liveness", lambda: (False, None, None), raising=True)
    _fenced_pool(monkeypatch, {})
    unreadable = tmp_path / "ledger-that-is-a-directory"
    unreadable.mkdir()
    monkeypatch.setattr(process_custody, "ledger_path", lambda _root: unreadable, raising=True)

    fence = control._fence_workers_for_update("reason")

    assert fence.ok is False
    assert fence.blocked == ("repo_service_ledger_unreadable",)


def test_a_blocked_fence_refuses_without_starting_a_replacement_pool(monkeypatch):
    """The abort's third arm. A replacement pool started here would run a fresh generation of
    writers beside a service still writing the checkout, which is the exact condition the fence
    exists to exclude. Nothing needs latching: the ledger entry stays live, so the next apply
    re-derives the same refusal from the same evidence."""
    respawned = []
    monkeypatch.setattr(
        control, "_respawn_workers_after_failed_update", lambda: respawned.append(1)
    )

    resp = control._abort_after_failed_fence(
        control._FenceResult(False, [], ("service:web#9911",)), "nothing was staged"
    )
    body = json.loads(resp.body)

    assert resp.status_code == 409
    assert body["reason"] == "repo_service_fence_blocked"
    assert "restarted" in body["error"] and "nothing was staged" in body["error"]
    assert respawned == []


# --- a lease-holding record must stay reachable from `_SERVICES` (v6.88.1 r7) ------------------
#
# One root cause, four sites. `_SERVICES` is the ONLY place the fence (through
# `terminate_repo_rooted_services`) can find a repo-rooted service, and `_retire_repo_writer_lease_
# if_dead` is the only thing that hands a lease back. So any path that drops a record while its
# lease is still held leaks that lease for the life of the process — and a leaked lease fails
# `drain_repo_writers`, i.e. refuses EVERY later managed update, forever. The rule below is the
# whole fix: retire the lease before losing the record, or keep the record until the lease is gone.


def test_restarting_a_dead_service_retires_the_lease_of_the_record_it_replaces(monkeypatch):
    """`_start_service` OVERWRITES `_SERVICES[key]` on success, after which the old record is
    unreachable. Its process is already known exited (that is what the "already running" check just
    established), so its lease is retired in the window where it still can be."""
    import ouroboros.tools.services as services
    import supervisor.workers as workers

    lease, record = _repo_rooted_service(monkeypatch, dies=False)
    record.proc.rc = 0  # the previous generation exited on its own
    monkeypatch.setattr(services, "_service_key", lambda *_a, **_k: record.service_id, raising=True)
    # Fail the very next step, so nothing past the retire window runs and the pin stays hermetic.
    monkeypatch.setattr(
        services, "resolve_shell_cwd",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no workspace")), raising=True,
    )

    out = services._start_service(object(), ["python", "-m", "http.server"], name="web")

    assert out.startswith("⚠️ SERVICE_CWD_ERROR")
    assert record.repo_writer_lease == ""
    assert workers.admitted_repo_writers() == [], f"the replaced record's {lease} did not leak"


def test_stopping_a_service_that_will_not_die_keeps_it_findable_by_the_fence(monkeypatch, tmp_path):
    """`_stop_service` used to POP the record before stopping it. A service that then survived
    `kill_process_tree` plus the bounded wait kept its lease with its record already gone —
    unreachable by `terminate_repo_rooted_services` and releasable by nothing. It stays registered
    instead, so the fence keeps seeing it and keeps retrying it."""
    import ouroboros.tools.services as services
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "chat_turn_liveness", lambda: (False, None, None), raising=True)
    lease, record = _repo_rooted_service(monkeypatch, dies=False)
    record.log_path = tmp_path / "web.log"  # log finalization is not what this pins; keep it local
    monkeypatch.setattr(services, "_service_key", lambda *_a, **_k: record.service_id, raising=True)

    class _Ctx:
        drive_root = str(tmp_path)

    services._stop_service(_Ctx(), name="web")

    assert record.repo_writer_lease == lease, "an unproven death does not retire the lease"
    assert services._SERVICES.get(record.service_id) is record
    assert services.terminate_repo_rooted_services() == [lease], "so the fence still reports it"
    assert workers.drain_repo_writers(timeout=0.0, poll=0.01) == [lease]


def test_stopping_a_service_that_does_die_still_forgets_it(monkeypatch, tmp_path):
    """The other half, and the normal path: a proven-dead service retires its lease inside the stop,
    so the record is removed exactly as before. Non-repo-rooted services hold no lease at all, so
    for them this removal is unconditional and the behaviour is unchanged."""
    import ouroboros.tools.services as services
    import supervisor.workers as workers

    _lease, record = _repo_rooted_service(monkeypatch, dies=True)
    record.log_path = tmp_path / "web.log"
    monkeypatch.setattr(services, "_service_key", lambda *_a, **_k: record.service_id, raising=True)

    class _Ctx:
        drive_root = str(tmp_path)

    services._stop_service(_Ctx(), name="web")

    assert record.repo_writer_lease == ""
    assert services._SERVICES == {}
    assert workers.admitted_repo_writers() == []


def test_the_shutdown_sweep_does_not_clear_a_lease_it_could_not_retire(monkeypatch):
    """`kill_all_services` cleared the whole registry up front. On a panic/restart sweep that
    dropped an undead repo-rooted service's lease into the same unreachable state — and this one is
    reached from shutdown paths, so the next update in this process would be refused by a writer
    nothing could name. Records go only after the stop proves them dead."""
    import ouroboros.tools.services as services
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "chat_turn_liveness", lambda: (False, None, None), raising=True)
    monkeypatch.setattr(
        services, "executor_kill_all_services", lambda *_a, **_k: [], raising=True
    )
    lease, record = _repo_rooted_service(monkeypatch, dies=False)

    assert [p["state"] for p in services.kill_all_services(None)] == ["running"]
    assert services._SERVICES.get(record.service_id) is record
    assert services.terminate_repo_rooted_services() == [lease]

    # And once it finally dies, the same sweep retires the lease and forgets the record.
    record.proc.rc = -9
    services.kill_all_services(None)
    assert services._SERVICES == {}
    assert workers.admitted_repo_writers() == []


def test_a_service_that_started_but_failed_to_finish_starting_stays_registered(monkeypatch):
    """`_start_service`'s exception path used to release the lease unconditionally after a
    best-effort teardown that is itself allowed to fail — handing admission back to a process that
    may still be running in the checkout. A lease may only go once its process is PROVEN gone, so a
    half-started service whose teardown failed is REGISTERED instead: `_SERVICES` is both the only
    place the fence can find it and the only place its lease can ever be retired from."""
    import ouroboros.tools.services as services
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "chat_turn_liveness", lambda: (False, None, None), raising=True)
    monkeypatch.setattr(services, "_SERVICES", {}, raising=True)
    monkeypatch.setattr(services, "kill_process_tree", lambda _p: None, raising=True)
    lease = workers.acquire_repo_writer_lease("service:web")
    proc = _FakeServiceProc()  # never exits: the teardown above does not work

    services._register_failed_repo_writer_start(
        service_name="web", key="t-1:web", task_id="t-1",
        cmd=["python", "-m", "http.server"], workdir="/repo", log_path=pathlib.Path("web.log"),
        proc=proc, cwd_root="", repo_writer_lease=lease,
    )

    record = services._SERVICES["t-1:web"]
    assert record.repo_writer_lease == lease
    assert services.terminate_repo_rooted_services() == [lease], "the fence can reach it"
    assert workers.drain_repo_writers(timeout=0.0, poll=0.01) == [lease], "and refuses on it"

    proc.rc = -9  # once it is proven gone the same sweep hands the lease back
    assert services.terminate_repo_rooted_services() == []
    assert workers.admitted_repo_writers() == []


# --- the rollback's `True` is a PROOF, not a report (v6.88.1 r6) ------------------------------

_PRE_SHA = "a" * 40


def _rollback_env(monkeypatch, **overrides):
    """Drive `rollback_managed_update` over a scripted `git_capture`.

    The defaults describe a rollback that fully worked; each override names the ONE command that
    did not. `git clean -fd` runs twice (a best-effort pass before the checkout and the checked one
    after the reset), so only the second is scripted.
    """
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    cleared, cleans = [], []
    intent_cleared = overrides.pop("intent_cleared", True)
    tx_cleared = overrides.pop("tx_cleared", True)
    scripted = {
        "checkout": (0, "", ""),
        "reset": (0, "", ""),
        "clean": (0, "", ""),
        "head": (0, _PRE_SHA, ""),
        "pre": (0, _PRE_SHA, ""),
        "status": (0, "", ""),
    }
    scripted.update(overrides)

    def _capture(cmd, *_a, **_k):
        cmd = [str(part) for part in cmd]
        if cmd == ["git", "checkout", "-B", "ouroboros", _PRE_SHA]:
            return scripted["checkout"]
        if cmd == ["git", "reset", "--hard", _PRE_SHA]:
            return scripted["reset"]
        if cmd == ["git", "clean", "-fd"]:
            cleans.append(1)
            return scripted["clean"] if len(cleans) >= 2 else (0, "", "")
        if cmd == ["git", "rev-parse", "HEAD"]:
            return scripted["head"]
        if cmd == ["git", "rev-parse", _PRE_SHA]:
            return scripted["pre"]
        if cmd == ["git", "status", "--porcelain"]:
            return scripted["status"]
        return (0, "", "")  # the forensic tag read/write and the pre-checkout reset

    monkeypatch.setattr(
        update_merge, "read_update_tx",
        lambda: {"pre_update_sha": _PRE_SHA, "pre_update_branch": "ouroboros"}, raising=True,
    )
    monkeypatch.setattr(
        update_merge, "clear_update_tx",
        lambda: bool(cleared.append("tx")) or tx_cleared, raising=True,
    )
    monkeypatch.setattr(update_merge, "append_jsonl", lambda *_a, **_k: None, raising=True)
    # v6.88.1 r6: `_clear_update_intent` now ANSWERS whether the marker is proven gone, and the
    # rollback clears the transaction only on a True. Recorded in the same list as the transaction
    # so the ORDER between the two is observable — it is the whole point of the fix.
    monkeypatch.setattr(
        git_ops, "_clear_update_intent",
        lambda *_a, **_k: bool(cleared.append("intent") or intent_cleared), raising=True,
    )
    monkeypatch.setattr(git_ops, "git_capture", _capture, raising=True)
    return cleared


def test_a_proven_rollback_clears_the_transaction_last(monkeypatch):
    import supervisor.update_merge as update_merge

    cleared = _rollback_env(monkeypatch)
    assert update_merge.rollback_managed_update("test") == (True, f"rolled back to {_PRE_SHA[:12]}")
    # INTENT first, transaction last. The intent is consumed by the next boot's `checkout_and_reset`,
    # so a transaction cleared ahead of it would leave a standing "reset onto the update target"
    # instruction that the bootstrap — finding no transaction — applies as an ordinary checkout,
    # silently undoing the rollback this function just proved.
    assert cleared == ["intent", "tx"]


def test_a_rollback_that_cannot_remove_the_intent_keeps_the_transaction(monkeypatch):
    """The inverse hole, and the reason the intent goes first: if the marker cannot be proven gone,
    the transaction is the ONLY thing that still explains it to the boot path — an intent WITH a
    transaction is a recoverable managed update, an intent ALONE is an unsupervised hard reset. So
    a failed unlink refuses the rollback rather than completing the cleanup halfway."""
    import supervisor.update_merge as update_merge

    cleared = _rollback_env(monkeypatch, intent_cleared=False)
    ok, msg = update_merge.rollback_managed_update("test")

    assert ok is False
    assert "update intent marker" in msg
    assert cleared == ["intent"], "the transaction must survive an unprovable intent removal"


@pytest.mark.parametrize("broken,expected", [
    ("reset", "reset --hard"),
    ("clean", "clean -fd"),
    ("status", "could not verify a clean tree"),
])
def test_a_rollback_command_that_failed_is_not_reported_as_success(monkeypatch, broken, expected):
    """`reset --hard` and `clean -fd` had their return codes DISCARDED, after which this cleared the
    transaction and answered `True` — and every caller reads that `True` as the licence to re-open
    in-process writer admission and start a fresh worker pool. A locked index or an un-removable
    untracked file therefore put writers onto a half-updated tree with the only record of the
    pre-update SHA already deleted."""
    import supervisor.update_merge as update_merge

    cleared = _rollback_env(monkeypatch, **{broken: (1, "", "permission denied")})
    ok, msg = update_merge.rollback_managed_update("test")
    assert ok is False
    assert expected in msg
    assert cleared == [], "the marker is the only record of where to return to"


@pytest.mark.parametrize("broken,expected", [
    ({"head": (0, "b" * 40, "")}, "rollback did not restore"),
    ({"head": (128, "", "fatal: bad revision")}, "rollback did not restore"),
    ({"pre": (128, "", "fatal: unknown revision")}, "rollback did not restore"),
    ({"status": (0, " M ouroboros/loop.py\n?? junk\n", "")}, "left the tree dirty: 2"),
])
def test_a_rollback_is_verified_against_the_checkout_itself(monkeypatch, broken, expected):
    """Return codes are not the whole proof: the commands can all succeed and still leave a tree
    that is not the pre-update one. So HEAD is re-read and must resolve to the transaction's EXACT
    `pre_update_sha`, and `git status --porcelain` must run and come back empty."""
    import supervisor.update_merge as update_merge

    cleared = _rollback_env(monkeypatch, **broken)
    ok, msg = update_merge.rollback_managed_update("test")
    assert ok is False
    assert expected in msg
    assert cleared == []


# --- post-fence recovery is PHASE-AWARE (v6.88.1 r6) ------------------------------------------

def _recovery_env(monkeypatch, *, tx, rolled_back=(True, "rolled back to aaaa"), recovered=True):
    """Drive `_recover_after_post_fence_exception` and record what it did, in order."""
    import supervisor.update_merge as update_merge

    calls = []
    monkeypatch.setattr(update_merge, "active_update_tx", lambda *_a, **_k: tx, raising=True)
    monkeypatch.setattr(
        update_merge, "rollback_managed_update",
        lambda why: calls.append(f"rollback:{why}") or rolled_back, raising=True,
    )
    monkeypatch.setattr(
        control, "_repository_is_recovered", lambda: calls.append("verify") or recovered
    )
    monkeypatch.setattr(
        control, "_respawn_workers_after_failed_update", lambda: calls.append("respawn")
    )
    return calls


def test_a_post_fence_exception_before_any_staging_just_respawns(monkeypatch):
    """Nothing was written, so there is nothing to undo — and refusing to revive the pool here
    would end task processing over an exception that mutated nothing."""
    calls = _recovery_env(monkeypatch, tx=None)

    resp = control._recover_after_post_fence_exception(RuntimeError("boom"), context="auto_merge")

    assert resp.status_code == 409
    assert calls == ["respawn"]
    assert "boom" in json.loads(resp.body)["error"]


def test_a_post_fence_exception_over_a_live_transaction_rolls_back_first(monkeypatch):
    """auto_merge hard-applies the merge BEFORE the smoke that can raise, and assisted materializes
    MERGE_HEAD before its later writes can. A handler that only respawned revived the general pool
    on top of the new HEAD, the staged merge and a live transaction."""
    calls = _recovery_env(monkeypatch, tx={"phase": "pending_boot_smoke"})

    resp = control._recover_after_post_fence_exception(RuntimeError("smoke blew up"), context="auto_merge")
    body = json.loads(resp.body)

    assert resp.status_code == 409
    assert body["reason"] == "rolled_back"
    # Rolled back AND verified before a single writer comes back.
    assert calls == ["rollback:auto_merge_post_fence_exception", "verify", "respawn"]


def test_an_unprovable_rollback_keeps_every_writer_locked_out(monkeypatch):
    """The fail-closed edge: a rollback we cannot PROVE is not a rollback. Re-admitting writers on
    an unverified tree is what the update transaction exists to prevent, so this outcome waits for
    the restart/boot recovery path instead."""
    calls = _recovery_env(monkeypatch, tx={"phase": "assisted_resolution"}, recovered=False)

    resp = control._recover_after_post_fence_exception(RuntimeError("boom"), context="assisted")
    body = json.loads(resp.body)

    assert resp.status_code == 409
    assert body["reason"] == "update_recovery_failed"
    assert "respawn" not in calls


def test_an_unreadable_transaction_marker_is_not_read_as_unmutated(monkeypatch):
    """Fail closed on the READ too: a marker we cannot open is not permission to assume nothing was
    staged, so recovery still runs the rollback path."""
    import supervisor.update_merge as update_merge

    calls = _recovery_env(monkeypatch, tx={}, recovered=False)

    def _unreadable(*_a, **_k):
        raise OSError("tx marker unreadable")

    monkeypatch.setattr(update_merge, "active_update_tx", _unreadable, raising=True)

    resp = control._recover_after_post_fence_exception(RuntimeError("boom"), context="replace")

    assert json.loads(resp.body)["reason"] == "update_recovery_failed"
    assert "respawn" not in calls


# --- assisted_started must be a promise something is resolving the merge (v6.88.1 r6) ---------

_CONFLICTING = dict(
    kind="conflicting", code_conflict_paths=["ouroboros/loop.py"], local_snapshot="ssss",
)


@pytest.fixture
def assisted_stage_env(monkeypatch):
    """Everything `_start_assisted_merge_fenced` touches once the fence is taken, stubbed so the
    staging can be driven all the way to its END — the point where it either hands the checkout to
    a resolver or has to undo what it staged. `staged_apply_env` deliberately cannot reach here (it
    stubs the whole staging away), so this fixture is the one that exercises the last step."""
    import supervisor.git_ops as git_ops
    import supervisor.state as state
    import supervisor.update_merge as update_merge

    env = {
        "rolled_back": [], "respawned": [], "staged": [], "verified": [], "ready_timeouts": [],
    }

    def _record(name, result=None):
        return lambda *_a, **_k: env["staged"].append(name) or result

    # Nothing protected in the delta, so the post-stop re-gate passes on the same release.
    monkeypatch.setattr(
        git_ops, "git_capture", lambda *_a, **_k: (0, "ouroboros/loop.py", ""), raising=True
    )
    monkeypatch.setattr(git_ops, "_create_rescue_snapshot", _record("rescue"), raising=True)
    monkeypatch.setattr(state, "load_state", lambda *_a, **_k: {}, raising=True)
    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge", lambda **_k: _plan(**_CONFLICTING), raising=True
    )
    monkeypatch.setattr(update_merge, "create_rescue_local_ref", _record("ref"), raising=True)
    monkeypatch.setattr(update_merge, "write_update_tx", _record("tx"), raising=True)
    monkeypatch.setattr(
        update_merge, "materialize_assisted_merge_live",
        _record("materialize", (True, "staged")), raising=True,
    )
    monkeypatch.setattr(
        update_merge, "enqueue_assisted_resolution_task",
        lambda tx, **kw: (
            env["ready_timeouts"].append(kw.get("ready_timeout")) or str(tx.get("task_id") or "")
        ),
        raising=True,
    )
    monkeypatch.setattr(
        update_merge, "rollback_managed_update",
        lambda why: env["rolled_back"].append(why) or (True, "rolled back"), raising=True,
    )
    # Stubbed rather than left real for the reason `_orphan_watchdog_env` gives: it reads the live
    # tx marker and shells out to git (through the `git_capture` this fixture already fakes), so a
    # real call would decide these pins from the developer's own checkout.
    monkeypatch.setattr(
        update_merge, "managed_update_repository_is_recovered",
        lambda: env["verified"].append(1) or True, raising=True,
    )
    monkeypatch.setattr(
        control, "_respawn_workers_after_failed_update", lambda: env["respawned"].append(1)
    )
    return env


def _queued_pending(monkeypatch):
    """An isolated PENDING plus the enqueue that fills it, so the drop-on-failure is observable."""
    from supervisor import queue as _queue

    monkeypatch.setattr(_queue, "PENDING", [], raising=True)
    monkeypatch.setattr(
        _queue, "enqueue_task",
        lambda task, front=False, **_k: _queue.PENDING.insert(0, dict(task)) or dict(task),
        raising=True,
    )
    return _queue


def test_the_assisted_resolver_task_is_dropped_when_no_worker_can_run_it(monkeypatch):
    """`ensure_worker_pool_started` answers "I did not refuse", not "a worker exists", and the
    health loop only respawns slots already in ``WORKERS`` — so an empty pool never self-recovers.
    A queued resolver with no executor would then sit in front of the queue forever, so it is
    removed again and the failure is RAISED instead of logged."""
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    queue = _queued_pending(monkeypatch)
    monkeypatch.setattr(workers, "ensure_worker_pool_started", lambda **_k: True, raising=True)
    monkeypatch.setattr(
        workers, "worker_pool_admission_state",
        lambda *_a, **_k: {"available": False}, raising=True,
    )

    with pytest.raises(RuntimeError):
        update_merge.enqueue_assisted_resolution_task({"task_id": "update_assisted_merge_dead"})
    assert queue.PENDING == []


def test_the_assisted_resolver_task_survives_a_started_pool(monkeypatch):
    """The other side of the same contract: a proven pool returns the id and LEAVES the task
    queued."""
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    queue = _queued_pending(monkeypatch)
    monkeypatch.setattr(workers, "ensure_worker_pool_started", lambda **_k: True, raising=True)
    monkeypatch.setattr(
        workers, "worker_pool_admission_state",
        lambda *_a, **_k: {"available": True}, raising=True,
    )
    monkeypatch.setattr(workers, "wait_for_ready_worker", lambda *_a, **_k: True, raising=True)

    task_id = update_merge.enqueue_assisted_resolution_task({"task_id": "update_assisted_merge_ok"})

    assert task_id == "update_assisted_merge_ok"
    assert [t["id"] for t in queue.PENDING] == ["update_assisted_merge_ok"]


# --- "started" is what the WORKER says, not what the spawn call returns (v6.88.1 r6) ------------


def test_a_pool_that_never_answers_ready_is_not_a_started_resolver(monkeypatch):
    """The pool that runs the resolver boots on code merged into the live worktree seconds earlier,
    so the likely failure is the merge breaking an import — and that failure produces a FULL
    ``WORKERS`` of children that already exited. Counting handles calls that a started resolver and
    promises `assisted_started` over a staged merge nothing will ever finish. The handshake comes
    from the worker itself; without it the task is dropped and the failure is raised.

    What it does NOT do is tear the pool down. This function has two callers with opposite recovery
    needs (see the boot pin below), so the teardown belongs where the matching respawn is."""
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    queue = _queued_pending(monkeypatch)
    stopped = []
    monkeypatch.setattr(workers, "ensure_worker_pool_started", lambda **_k: True, raising=True)
    monkeypatch.setattr(
        workers, "worker_pool_admission_state",
        lambda *_a, **_k: {"available": True}, raising=True,  # handles exist...
    )
    monkeypatch.setattr(
        workers, "wait_for_ready_worker", lambda *_a, **_k: False, raising=True,  # ...nobody home
    )
    monkeypatch.setattr(
        workers, "kill_workers", lambda **kw: stopped.append(kw), raising=True
    )

    with pytest.raises(RuntimeError, match="ready"):
        update_merge.enqueue_assisted_resolution_task({"task_id": "update_assisted_merge_unready"})

    assert queue.PENDING == [], "no resolver task is left in front of a queue nothing reads"
    assert stopped == [], "teardown is the caller's decision, not a side effect of the enqueue"


def test_a_ready_timeout_during_boot_recovery_does_not_destroy_the_pool(monkeypatch):
    """The FIX_FIRST. `_recover_assisted_on_boot` is documented non-destructive and does NOT respawn,
    so the teardown this callee used to perform unconditionally was pure damage there: `kill_workers`
    empties ``WORKERS`` and drains PENDING to terminal failures, and `_ensure_workers_healthy_locked`
    only refills slots already present — so one slow import at boot ended task processing for the
    life of the process AND failed every task the queue had just restored.

    A ready timeout at boot is a recoverable condition: the resolver task is dropped, the caller is
    told, and everything else is exactly as it was."""
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    queue = _queued_pending(monkeypatch)
    queue.PENDING.append({"id": "user-task-restored-from-the-snapshot"})
    pool = {0: _FakeWorker(alive=True), 1: _FakeWorker(alive=True)}
    stopped = []
    monkeypatch.setattr(workers, "WORKERS", pool, raising=True)
    monkeypatch.setattr(workers, "ensure_worker_pool_started", lambda **_k: True, raising=True)
    monkeypatch.setattr(
        workers, "worker_pool_admission_state",
        lambda *_a, **_k: {"available": True}, raising=True,
    )
    monkeypatch.setattr(workers, "wait_for_ready_worker", lambda *_a, **_k: False, raising=True)
    monkeypatch.setattr(
        workers, "kill_workers",
        lambda **kw: stopped.append(kw) or workers.WORKERS.clear(), raising=True,
    )

    with pytest.raises(RuntimeError, match="ready"):
        update_merge.enqueue_assisted_resolution_task({"task_id": "update_assisted_merge_boot"})

    assert stopped == []
    assert sorted(workers.WORKERS) == [0, 1], "boot recovery does not get to end task processing"
    assert [t["id"] for t in queue.PENDING] == ["user-task-restored-from-the-snapshot"], (
        "and the restored user tasks are not drained to terminal failures"
    )


def test_the_ready_wait_answers_no_the_moment_the_last_candidate_dies(monkeypatch):
    """The wait must be bounded by the ANSWER, not only by its ceiling: once no candidate is alive,
    no further waiting can change the result, and the assisted flow is holding the update lock with
    a merge staged in the live worktree while it blocks here."""
    import time

    import supervisor.workers as workers

    class _DeadProc:
        def is_alive(self):
            return False

    class _ReadySignal:
        def is_set(self):
            return False

    monkeypatch.setattr(
        workers, "WORKERS",
        {0: workers.Worker(wid=0, proc=_DeadProc(), in_q=None, ready=_ReadySignal())},
        raising=True,
    )

    started = time.monotonic()
    answered = workers.wait_for_ready_worker(timeout=30.0, poll=0.01)

    assert answered is False
    assert time.monotonic() - started < 5, "a settled answer is not waited out"


def test_the_ready_wait_takes_the_worker_that_actually_came_up(monkeypatch):
    """A pool is ready when ONE slot can run the task — the resolver is a single task. A dead slot
    beside a live ready one must not make the answer no, and an unset signal on a live slot must not
    make it yes."""
    import supervisor.workers as workers

    class _Proc:
        def __init__(self, alive):
            self._alive = alive

        def is_alive(self):
            return self._alive

    class _Signal:
        def __init__(self, ready):
            self._ready = ready

        def is_set(self):
            return self._ready

    def _pool(*slots):
        return {
            i: workers.Worker(wid=i, proc=_Proc(alive), in_q=None, ready=_Signal(ready))
            for i, (alive, ready) in enumerate(slots)
        }

    monkeypatch.setattr(workers, "WORKERS", _pool((False, False), (True, True)), raising=True)
    assert workers.wait_for_ready_worker(timeout=0.0, poll=0.01) is True

    monkeypatch.setattr(workers, "WORKERS", _pool((True, False)), raising=True)
    assert workers.wait_for_ready_worker(timeout=0.0, poll=0.01) is False, (
        "a live process that has not finished booting is not a resolver yet"
    )

    monkeypatch.setattr(workers, "WORKERS", {}, raising=True)
    assert workers.wait_for_ready_worker(timeout=0.0, poll=0.01) is False


def test_the_worker_raises_its_ready_signal_only_after_the_agent_exists():
    """Where the signal is emitted is the whole point: raised at process start it would prove
    nothing, because `make_agent` imports the freshly merged code and is precisely what a bad update
    breaks — and the crash branch above it RETURNS, so a worker that never got an agent must leave
    the signal down.

    Pinned at SOURCE rather than by calling `worker_main`: everything before `make_agent` is
    process-wide (an `OUROBOROS_IN_WORKER` env pin, `create_new_session`, a parent lifeline thread,
    a global log sink), so running it in the test process would reconfigure the test process."""
    import inspect

    import supervisor.workers as workers

    source = inspect.getsource(workers.worker_main)
    ready = source.index("ready.set()")
    assert source.index("make_agent(") < ready, "the signal is not a proof of a booted agent"
    assert source.index('"make_agent", _e') < ready, "the crash branch returns before it"
    assert ready < source.index("while True:"), "and it is raised before the dispatch loop blocks"


def test_a_resolver_that_could_not_start_rolls_the_staging_back(monkeypatch, assisted_stage_env):
    """The FIX_FIRST case end to end: the caller used to return `assisted_started` unconditionally,
    leaving a live transaction and a materialized merge in the worktree with nothing resolving
    them. It must undo the staging and report the failure instead."""
    import supervisor.update_merge as update_merge

    def _no_resolver(_tx, **_k):
        raise RuntimeError("no worker could be started for the assisted resolution task")

    monkeypatch.setattr(
        update_merge, "enqueue_assisted_resolution_task", _no_resolver, raising=True
    )

    resp = control._start_assisted_merge_fenced(_plan(**_CONFLICTING), "dev")
    body = json.loads(resp.body)

    assert resp.status_code == 409
    assert body["reason"] == "assisted_resolver_unavailable"
    assert assisted_stage_env["rolled_back"] == ["assisted_resolver_unavailable"]
    assert assisted_stage_env["respawned"] == [1]


def test_the_apply_path_stops_the_unready_pool_before_it_resets_the_tree(
    monkeypatch, assisted_stage_env
):
    """The other side of the teardown move. Boot recovery must not lose its pool, but THIS caller
    must: the rollback below is a hard reset of the live worktree, so processes that never answered
    ready cannot still be running on it when the tree moves. Order is the whole property — the kill
    happens BEFORE the rollback, and `_rollback_and_respawn` rebuilds the pool afterwards from the
    restored checkout."""
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    order = []
    monkeypatch.setattr(
        update_merge, "enqueue_assisted_resolution_task",
        lambda _tx, **_k: (_ for _ in ()).throw(
            RuntimeError("no worker became ready to run the assisted resolution task")
        ),
        raising=True,
    )
    monkeypatch.setattr(
        workers, "kill_workers", lambda **kw: order.append(("kill", kw)), raising=True
    )
    monkeypatch.setattr(
        update_merge, "rollback_managed_update",
        lambda why: order.append(("rollback", why)) or (True, "rolled back"), raising=True,
    )

    resp = control._start_assisted_merge_fenced(_plan(**_CONFLICTING), "dev")

    assert json.loads(resp.body)["reason"] == "assisted_resolver_unavailable"
    assert [step for step, _ in order] == ["kill", "rollback"]
    assert order[0][1]["terminal_status"] == "interrupted"
    assert assisted_stage_env["respawned"] == [1], "and the pool comes back on the restored tree"


def test_a_started_resolver_reports_assisted_started(assisted_stage_env):
    """And the happy path still promises what it delivers."""
    resp = control._start_assisted_merge_fenced(_plan(**_CONFLICTING), "dev")
    body = json.loads(resp.body)

    assert body["status"] == "assisted_started"
    assert assisted_stage_env["rolled_back"] == []


def test_the_interactive_apply_bounds_the_ready_handshake_shorter_than_boot(assisted_stage_env):
    """The handshake wait runs SYNCHRONOUSLY inside the async apply handler, with the pool down and
    writer admission closed — so the module default (a minute, sized for boot recovery, where no
    request is waiting) would hold the gateway event loop for that minute before answering. The
    apply path passes its own, shorter ceiling; the outcome of a "no" is the same rollback either
    way, so only the ceiling differs."""
    import supervisor.workers as workers

    control._start_assisted_merge_fenced(_plan(**_CONFLICTING), "dev")

    assert assisted_stage_env["ready_timeouts"] == [control._APPLY_READY_TIMEOUT_SEC]
    assert 0 < control._APPLY_READY_TIMEOUT_SEC < workers.WORKER_READY_TIMEOUT_SEC


def test_the_staging_holds_the_in_process_fence_through_its_own_rollback(
    monkeypatch, assisted_stage_env
):
    """Staging used to re-open in-process writer admission BEFORE it knew a resolver existed. That
    put the failure branch's rollback — a hard reset of the live worktree — beside direct-chat turns
    that had just been re-admitted, which is the exact unfenced-writer condition the fence exists to
    exclude, and it can discard owner edits made in that window. Admission is handed back only
    through `_respawn_workers_after_failed_update`, i.e. AFTER the rollback."""
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    seen = []

    def _no_resolver(_tx, **_k):
        seen.append(("enqueue", workers.repo_writer_admission_closed()))
        raise RuntimeError("no worker could be started for the assisted resolution task")

    monkeypatch.setattr(
        update_merge, "enqueue_assisted_resolution_task", _no_resolver, raising=True
    )
    monkeypatch.setattr(
        update_merge, "rollback_managed_update",
        lambda why: seen.append(("rollback", workers.repo_writer_admission_closed()))
        or (True, "rolled back"),
        raising=True,
    )

    workers.close_repo_writer_admission("managed_update: test")  # what the fence did
    resp = control._start_assisted_merge_fenced(_plan(**_CONFLICTING), "dev")

    assert resp.status_code == 409
    assert [where for where, _closed in seen] == ["enqueue", "rollback"]
    assert all(closed for _where, closed in seen), "no writer is admitted during the rollback"
    # The fixture stubs the respawn helper (the only thing that re-admits), so it is still shut.
    assert workers.repo_writer_admission_closed()


def test_a_started_resolver_keeps_the_checkout_fenced_against_chat(assisted_stage_env):
    """The resolver worker is the ONE authorized writer while MERGE_HEAD and conflict markers sit in
    the live worktree, so handing it the pool must not globally re-admit unrelated chat writers.
    Admission comes back where the resolution ENDS: a committed merge restarts the process, and a
    resolver task that ends without committing is caught by `abort_orphaned_assisted_tx`."""
    import supervisor.workers as workers

    workers.close_repo_writer_admission("managed_update: test")
    resp = control._start_assisted_merge_fenced(_plan(**_CONFLICTING), "dev")

    assert json.loads(resp.body)["status"] == "assisted_started"
    assert workers.repo_writer_admission_closed()


def _orphan_watchdog_env(monkeypatch, *, tx=("valid", {"phase": "assisted_resolution", "task_id": "t-1"}),
                         rolled_back=(True, "rolled back"), recovered=True, lock_held=False):
    """Drive `abort_orphaned_assisted_tx` hermetically and record WHAT it did, in order, together
    with the admission state at each step — every pin here is about that ordering.

    `managed_update_repository_is_recovered` is stubbed rather than left real: it shells out to git
    and reads the live tx marker, so a real call would make these pins depend on the developer's
    own checkout."""
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    seen = []

    def _acquire():
        if lock_held:
            raise RuntimeError("managed_update.lock is held by another update operation")
        return object()

    monkeypatch.setattr(update_merge, "read_update_tx_strict", lambda: tx, raising=True)
    monkeypatch.setattr(update_merge, "acquire_update_lock", _acquire, raising=True)
    monkeypatch.setattr(update_merge, "release_update_lock", lambda _fh: None, raising=True)
    monkeypatch.setattr(
        update_merge, "rollback_managed_update",
        lambda why: seen.append(("rollback", workers.repo_writer_admission_closed()))
        or rolled_back,
        raising=True,
    )
    monkeypatch.setattr(
        update_merge, "managed_update_repository_is_recovered",
        lambda: seen.append(("verify", workers.repo_writer_admission_closed())) or recovered,
        raising=True,
    )
    monkeypatch.setattr(
        workers, "ensure_worker_pool_started",
        lambda **_k: seen.append(("pool", workers.repo_writer_admission_closed())) or True,
        raising=True,
    )
    # The watchdog journals to `DRIVE_ROOT/logs/supervisor.jsonl`. These pins are about admission
    # ordering, not about the journal, so the write is stubbed out rather than redirected.
    monkeypatch.setattr(update_merge, "_log_supervisor", lambda _payload: None, raising=True)

    workers.close_repo_writer_admission("managed_update: test")  # what the fence did
    return seen


def test_an_orphaned_assisted_resolution_hands_the_checkout_back(monkeypatch):
    """The other end of that lease. If the fence's admission were never re-opened after a resolver
    task that died without committing, direct chat would stay refused for the life of the process —
    so the watchdog that rolls the orphaned merge back is also what re-admits writers, and only
    after the rollback has been PROVEN to restore the tree."""
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    seen = _orphan_watchdog_env(monkeypatch)

    assert update_merge.abort_orphaned_assisted_tx("t-1")["acted"] is True

    assert [where for where, _closed in seen] == ["rollback", "verify", "pool"]
    assert seen[0][1], "the rollback still runs behind the fence"
    assert seen[1][1], "and so does the verification of it"
    assert not seen[2][1], "the pool is only re-started once writers are re-admitted"
    assert workers.repo_writer_admission_closed() == ""


def test_a_failed_orphan_rollback_keeps_writer_admission_closed(monkeypatch):
    """`rollback_managed_update` returns False when it could not restore the pre-update checkout
    (no `pre_update_sha`, a failed `checkout -B`). Re-admitting on that boolean regardless dropped
    direct and ephemeral chat turns into a checkout still carrying the failed assisted merge — and
    started the general pool on the same tree. Both stay down for the restart/recovery path."""
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    seen = _orphan_watchdog_env(
        monkeypatch, rolled_back=(False, "rollback checkout -B dev aaaa failed")
    )

    out = update_merge.abort_orphaned_assisted_tx("t-1")

    assert out["acted"] is True and out["rolled_back"] is False
    assert out["reason"] == "update_recovery_failed"
    assert [where for where, _closed in seen] == ["rollback"], "no pool behind a failed rollback"
    assert workers.repo_writer_admission_closed(), "and no writer is re-admitted either"


def test_an_unprovable_orphan_rollback_keeps_writer_admission_closed(monkeypatch):
    """The same refusal one step further out: the rollback CLAIMED success but the tree could not be
    independently proven clean (a leftover MERGE_HEAD, unmerged entries, a surviving tx marker, or a
    check that would not run). An unverified rollback is not a rollback."""
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    seen = _orphan_watchdog_env(monkeypatch, recovered=False)

    out = update_merge.abort_orphaned_assisted_tx("t-1")

    assert out["recovered"] is False and out["reason"] == "update_recovery_failed"
    assert [where for where, _closed in seen] == ["rollback", "verify"]
    assert workers.repo_writer_admission_closed()


# --- the closed admission needs an owner that survives a missed watchdog call (v6.88.1 r7) -----

def test_a_watchdog_that_declines_still_hands_a_stranded_admission_back(monkeypatch):
    """`abort_orphaned_assisted_tx` is invoked once per `task_done`, and most invocations decline:
    the tx is absent, or it belongs to some other task. Those branches used to walk away from an
    admission flag nobody else owns, so a fence whose re-open was missed refused direct chat for the
    life of the process. Declining now reconciles instead."""
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    _orphan_watchdog_env(monkeypatch, tx=("absent", {}))

    out = update_merge.abort_orphaned_assisted_tx("t-1")

    assert out["acted"] is False
    assert out["admission"] == {"reopened": True, "reason": "no_active_update"}
    assert workers.repo_writer_admission_closed() == ""


def test_a_declining_watchdog_does_not_reopen_behind_a_live_apply(monkeypatch):
    """The fail-closed half of the same branch. A fence is established BEFORE the transaction marker
    is written, so a reconcile that only checked the marker could re-admit chat writers inside that
    window. The update lock is what closes it: an apply holds it across the fence, the marker write
    and the apply, so a lock it cannot take is proof an update owns the closure."""
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    _orphan_watchdog_env(monkeypatch, tx=("absent", {}), lock_held=True)

    out = update_merge.abort_orphaned_assisted_tx("t-1")

    assert out["admission"] == {"reopened": False, "reason": "update_in_flight"}
    assert workers.repo_writer_admission_closed(), "the fence keeps the checkout"


@pytest.mark.parametrize("phase", ["committing_assisted", "pending_boot_smoke"])
def test_a_restart_pending_update_keeps_the_admission_it_closed(monkeypatch, phase):
    """The two phases that END in a process restart: the merge is committed or the commit is in
    flight. The restart is what re-admits writers there, so the reconcile must not do it early — the
    checkout is carrying the update's own new HEAD."""
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    monkeypatch.setattr(
        update_merge, "read_update_tx_strict", lambda: ("valid", {"phase": phase}), raising=True
    )
    monkeypatch.setattr(update_merge, "acquire_update_lock", lambda: object(), raising=True)
    monkeypatch.setattr(update_merge, "release_update_lock", lambda _fh: None, raising=True)
    workers.close_repo_writer_admission("managed_update: test")

    assert update_merge.reconcile_repo_writer_admission() == {
        "reopened": False, "reason": "restart_pending",
    }
    assert workers.repo_writer_admission_closed()


def test_the_reconcile_will_not_reopen_onto_an_unrecovered_checkout(monkeypatch):
    """No transaction marker is not the same as a clean tree — a rollback can clear the marker and
    still leave MERGE_HEAD or unmerged entries behind. The reconcile is the LAST owner of the flag,
    so it proves the checkout before it hands it to chat."""
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    monkeypatch.setattr(update_merge, "read_update_tx_strict", lambda: ("absent", {}), raising=True)
    monkeypatch.setattr(update_merge, "acquire_update_lock", lambda: object(), raising=True)
    monkeypatch.setattr(update_merge, "release_update_lock", lambda _fh: None, raising=True)
    monkeypatch.setattr(
        update_merge, "managed_update_repository_is_recovered", lambda: False, raising=True
    )
    workers.close_repo_writer_admission("managed_update: test")

    assert update_merge.reconcile_repo_writer_admission() == {
        "reopened": False, "reason": "repository_unrecovered",
    }
    assert workers.repo_writer_admission_closed()


def test_an_open_admission_is_never_reconciled_through_the_update_lock(monkeypatch):
    """The timer calls this every 60s on a process that is almost never updating. It must answer
    from the flag alone — taking the exclusive update lock on every tick would put the maintenance
    pass in contention with the boot check-on-restart thread and any apply."""
    import supervisor.update_merge as update_merge
    import supervisor.workers as workers

    def _never(*_a, **_k):
        raise AssertionError("the reconcile must not touch the update lock when admission is open")

    monkeypatch.setattr(update_merge, "acquire_update_lock", _never, raising=True)
    workers.open_repo_writer_admission()

    assert update_merge.reconcile_repo_writer_admission() == {
        "reopened": False, "reason": "already_open",
    }


# --- the preflight envelope for an unverifiable delta (v6.88.1 r6) ----------------------------

def test_preflight_publishes_no_paths_for_an_unverifiable_protected_delta(monkeypatch):
    """The authority deliberately KEEPS the plan's own protected conflict paths when the delta diff
    fails, because they are still a reason to route manual — but they are not a statement about what
    the release touches. `protected_paths` is the BLOCKED-paths field in every apply envelope, and
    the frozen contract (and this endpoint's own comment) says an unverifiable delta carries an
    empty one, so forwarding them here published a claim the backend refused to establish."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    monkeypatch.setattr(
        update_merge, "plan_managed_update_merge",
        lambda **_k: _plan(kind="conflicting", protected_conflict_paths=["BIBLE.md"]),
        raising=True,
    )
    monkeypatch.setattr(
        git_ops, "git_capture", lambda *_a, **_k: (128, "", "fatal: bad object"), raising=True
    )

    route = json.loads(asyncio.run(control.api_update_preflight(None)).body)["protected_route"]

    assert route["reason"] == "protected_delta_unverifiable"
    assert route["will_route_manual"] is True
    assert route["protected_paths"] == []


def test_preflight_still_publishes_the_blocked_paths_it_did_verify(official_delta, monkeypatch):
    """The other half: a delta it COULD read still names what blocks, or the dialog has nothing to
    disclose."""
    import supervisor.update_merge as update_merge

    monkeypatch.setattr(update_merge, "plan_managed_update_merge", lambda **_k: _plan(), raising=True)
    official_delta(["ouroboros/safety.py", "ouroboros/loop.py"])

    route = json.loads(asyncio.run(control.api_update_preflight(None)).body)["protected_route"]

    assert route["reason"] == "protected_paths"
    assert route["protected_paths"] == ["ouroboros/safety.py"]


# --- the bounded fetch must reap the whole transport tree on Windows too (v6.88.1 r6) ---------

def test_the_bounded_fetch_reaps_the_transport_tree_on_non_posix(monkeypatch):
    """`proc.kill()` ends git alone. The transport helpers it spawns (ssh, git-remote-https,
    credential helpers) inherit our pipes, so an orphaned one leaves the reap blocked on an fd
    nobody is left to close — the very hang this ceiling exists to bound. POSIX solves it with a new
    session + killpg; Windows has no sessions, so the spawn asks for a process GROUP and the kill
    goes through `kill_pid_tree` (`taskkill /F /T`), which walks the tree.

    Driven with a faked spawn because the real branch is unreachable on this platform."""
    import subprocess

    import ouroboros.platform_layer as platform_layer
    import supervisor.git_ops as git_ops

    class _WindowsOs:
        """`os` as `git_fetch_bounded` sees it on Windows; everything else is the real module."""

        name = "nt"

        def __getattr__(self, item):
            return getattr(os, item)

    spawned, killed = {}, []

    class _StalledFetch:
        """A fetch whose transport helper never answers, so the wall clock has to cut it off."""

        pid = 4242

        def __init__(self, _cmd, **kwargs):
            spawned.update(kwargs)
            self.returncode = None
            self._timed_out = False

        def communicate(self, timeout=None):
            if not self._timed_out:
                self._timed_out = True
                raise subprocess.TimeoutExpired("git fetch", timeout or 0)
            return "", ""

    monkeypatch.setattr(git_ops, "os", _WindowsOs(), raising=True)
    monkeypatch.setattr(subprocess, "Popen", _StalledFetch, raising=True)
    monkeypatch.setattr(
        platform_layer, "kill_pid_tree", lambda pid: killed.append(pid), raising=True
    )

    rc, _out, err = git_ops.git_fetch_bounded("managed", timeout=0.01)

    assert rc == git_ops.FETCH_TIMEOUT_RC
    assert "terminated" in err
    assert killed == [4242], "killing git alone orphans the helpers holding our pipes"
    # And the spawn has to give that kill a tree to walk in the first place.
    assert "creationflags" in spawned
    assert "start_new_session" not in spawned


# --- the pill dialog reads the typed 409 refusals too (v6.88.1 r6) ----------------------------

def test_the_pill_dialog_tells_the_typed_409_refusals_apart():
    """The staged paths answer typed 409s the owner can act on — a held update lock is transient
    (the boot check-on-restart thread holds it across its fetch) and a moved release just needs a
    fresh disclosure. `jsonPost` RAISES those, and collapsing the rejection into a message string
    reported both as generic failures, while the detailed panel already told them apart.

    Source-pinned in this gated module for the reason on the sibling `update_status.js` pin above:
    the JS suite is not in the gate set and `node --check` never evaluates an assertion."""
    source = (
        pathlib.Path(__file__).resolve().parent.parent / "web" / "modules" / "update_status.js"
    ).read_text(encoding="utf-8")
    # The catch must carry the typed reason across, not just the message.
    assert "e.status === 409 && e.body && e.body.reason" in source
    lock_held = source.index("data.reason === 'update_lock_held'")
    moved = source.index("data.reason === 'release_moved'")
    generic = source.index("Did not complete:")
    assert lock_held < generic and moved < generic, "both must precede the generic failure text"
    assert "Try again in a moment" in source
    # A moved release is not manual handling: this dialog builds its disclosure on open only.
    assert "reopen this dialog" in source


def test_the_pill_dialog_leaves_a_drifted_release_with_somewhere_to_go():
    """The drift branch sits AHEAD of the manual branch, so it also catches the replace-family 200
    that carries BOTH (`status: manual` + `reason: release_moved` + the freshly disclosed
    protected_paths, control.py `_apply_replace_family`). That response used to reach the manual
    branch, which NAMES those paths and hands the owner to the detailed Updates panel; swallowing it
    left a dead overlay and named the newly protected paths nowhere.

    So the neutral branch excludes a drift that disclosed protected paths (it falls through to the
    manual branch), and owns an exit for the ones it keeps — the disclosure is built once, on open,
    so the dialog cannot re-render itself against the new release."""
    source = (
        pathlib.Path(__file__).resolve().parent.parent / "web" / "modules" / "update_status.js"
    ).read_text(encoding="utf-8")
    moved = source.index("data.reason === 'release_moved'")
    manual = source.index("data.status === 'manual'")
    assert moved < manual, "the typed reason is still read before the generic manual handoff"
    # The exclusion that lets a protected-path drift through to the branch that names them.
    guard = source.index("Array.isArray(data.protected_paths) && data.protected_paths.length", moved)
    assert guard < manual, "the drift branch must not swallow a disclosure it cannot render"
    # And the branch it keeps closes the overlay instead of stranding the owner on it.
    exit_call = source.index("overlay.remove(); refresh();", moved)
    assert exit_call < manual


# --- an unverified preflight is not an actionable dialog (v6.88.1 r6) --------------------------


def test_the_pill_dialog_offers_nothing_on_a_preflight_it_could_not_verify():
    """`safe()` converts a failed preflight into `null`, and the planner answers a degraded check
    with `kind: 'unknown'` and NO dirty count — and every field the dialog reads then falls back to
    the reassuring value, so it rendered "0 local change(s) · clean merge" over a checkout nobody
    had looked at and offered to update it. That is the one claim this dialog must never invent.

    Pinned at SOURCE in this gated module for the reason on the sibling `update_status.js` pins
    above: the JS suite is not in the gate set and `node --check` never evaluates an assertion."""
    source = (
        pathlib.Path(__file__).resolve().parent.parent / "web" / "modules" / "update_status.js"
    ).read_text(encoding="utf-8")
    # The count must arrive as an integer. `plan.local_dirty_count || 0` is exactly the coercion
    # the backend refuses to make, so it may not reappear here.
    assert "Number.isInteger(plan.local_dirty_count)" in source
    assert "plan.local_dirty_count || 0" not in source
    # All three unverified shapes are excluded: no response at all, no available plan, a degraded one.
    verified = source.index("const verified =")
    assert "Boolean(pre) && plan.available === true && kind !== 'unknown' && dirtyCount !== null" in source
    # ...and the exclusion RETURNS, ahead of everything that renders or applies.
    bail = source.index("renderUnverifiedPreflight(overlay, pre, plan);", verified)
    assert bail < source.index("update-dialog-meta")
    assert bail < source.index("apiClient.updateApply")
    # The clean-merge label comes from the plan's own kind, not from an empty conflict list.
    meta = source.index("update-dialog-meta")
    assert "kind === 'clean' ? ' · clean merge' : ''" in source[meta:source.index("\n", meta)]


def test_the_unverified_preflight_dialog_is_retry_or_details_only():
    """What replaces the invented state: the two honest actions. It must offer no apply of any
    kind — an update the owner has to reopen a dialog to start is a smaller harm than one started
    against a checkout state nobody established."""
    source = (
        pathlib.Path(__file__).resolve().parent.parent / "web" / "modules" / "update_status.js"
    ).read_text(encoding="utf-8")
    start = source.index("function renderUnverifiedPreflight")
    body = source[start:source.index("\n    async function openUpdateDialog", start)]

    assert "data-retry" in body and "openUpdateDialog();" in body, "Retry re-runs the check"
    assert "data-details" in body and "openDashboardTab?.('updates')" in body
    assert "data-close" in body
    # No strategy button, and nothing that could post one.
    assert "data-strategy" not in body
    assert "updateApply" not in body
    # It says what failed rather than describing a tree it never read.
    assert "could not be reached" in body and "did not complete" in body
    assert "clean merge" not in body and "local change(s)" not in body


# --- an update that LANDED is not a failure just because the restart did not (v6.88.1 r7) ------
#
# `{status:'ok', restarting:false, warning:…}` is a terminal frame both apply families now emit:
# the commit is in the checkout and the smoke test passed, but the restart could not be requested.
# Both browser consumers read `restarting` as if it were the success flag, in opposite directions —
# one reported the applied update as a failure, the other announced a restart that is not coming.
# Neither reading leaves the owner able to act, and both are about a checkout that has ALREADY
# moved. Source-pinned in this gated module for the reason the sibling JS pins above give: the JS
# suite is not in the gate set and `node --check` never evaluates an assertion.


def test_the_pill_dialog_reports_an_applied_update_whose_restart_was_refused():
    """`status === 'ok' && restarting` sent that frame down the chain to the generic failure text,
    which invites the owner to retry an update already in their tree. Success is decided by
    `status`; `restarting` only chooses the wording."""
    source = (
        pathlib.Path(__file__).resolve().parent.parent / "web" / "modules" / "update_status.js"
    ).read_text(encoding="utf-8")

    assert "data.status === 'ok' && data.restarting" not in source, (
        "`restarting` is a second question, not part of the success test"
    )
    ok = source.index("data.status === 'ok'")
    wording = source.index("data.restarting", ok)
    generic = source.index("'Update did not complete.'")
    assert ok < wording < generic, "the applied-but-not-restarting frame never reaches the else"
    # And it says what is left to do, from the frame's own warning where there is one.
    branch = source[ok:generic]
    assert "data.warning" in branch
    assert "manually" in branch


def test_the_updates_panel_does_not_promise_a_restart_that_was_refused():
    """The opposite defect at the other render site: the panel's terminal toast was unconditional,
    so it told the owner to wait for a restart that never comes — and left the Update button
    disabled for that wait, with no next action at all. Narrowed to the exact frame, so every other
    terminal answer keeps its wording verbatim."""
    source = (
        pathlib.Path(__file__).resolve().parent.parent / "web" / "modules" / "updates.js"
    ).read_text(encoding="utf-8")

    frame = source.index("data.status === 'ok' && !data.restarting")
    restarting_toast = source.index("Update prepared. Server is restarting.")
    assert frame < restarting_toast, "the refused-restart frame is read first"
    branch = source[frame:restarting_toast]
    assert "data.warning" in branch, "the backend says what is left to do; pass it through"
    assert "restoreBtn();" in branch, "no restart is coming, so the owner keeps a next action"
    assert "'warning'" in branch, "an applied update is not an error and not a plain success"
    assert "keep" in branch, "the preserved local branch is still named"


# --- BLOCK: a failed ledger append is not a written one (v6.88.1 r6) ---------------------------
#
# `append_jsonl` REPORTS its failure: it exhausts its retries and answers False rather than raising.
# Both custody durability points ignored that boolean, so "the ledger has no record of this
# process" became a silent success — and for a repository writer the ledger IS the fence's only
# handle. The regressions drive the REPORTING shape specifically: the raising one was already
# handled, and it is the case that never reached production.


def test_a_ledger_append_that_reports_failure_is_not_treated_as_custody(monkeypatch):
    """`record_process` must refuse to hand back a repo-writer record it could not durably write.
    `spawn_supervised` handles exceptions only, so a False swallowed here returned a running
    repo-rooted service that the update fence has no way to see."""
    import ouroboros.process_custody as process_custody

    monkeypatch.setattr(process_custody, "append_jsonl", lambda *_a, **_k: False, raising=True)

    with pytest.raises(OSError) as excinfo:
        process_custody.record_process(
            pathlib.Path("/unused"), pid=os.getpid(), cmd=["x"], purpose="service:web",
            scope="task", repo_writer=True,
        )
    assert "custody ledger append failed" in str(excinfo.value)


def test_a_failed_append_for_an_advisory_scope_is_not_fatal(monkeypatch, _hermetic_custody_ledger):
    """...and ONLY for a repository writer. For every other scope the ledger is the reaper's
    advisory registry, whose degradation is "an orphan is reaped a generation later" — and the
    direct callers (`supervisor.workers`, `workspace_executor`, `local_model`,
    `extension_companion`) were written against a `record_process` that could not fail. Raising at
    them would convert a filesystem fault into new failure paths in code this fix is not about, so
    the widened contract is deliberately narrowed back and pinned here."""
    import ouroboros.process_custody as process_custody

    monkeypatch.setattr(process_custody, "append_jsonl", lambda *_a, **_k: False, raising=True)

    record = process_custody.record_process(
        pathlib.Path("/unused"), pid=os.getpid(), cmd=["x"], purpose="browser", scope="task",
    )

    assert record["pid"] == os.getpid(), "an advisory scope still gets its (undurable) record back"


def test_an_unkillable_writer_is_handed_back_to_its_caller_with_its_handle(monkeypatch):
    """The pooled-worker case, which holds NO admission lease and therefore has nothing but the
    process itself left to control. `RepoWriterCustodyError` named the situation but did not carry
    the `Popen`: the raise happens inside `spawn_supervised`, before the object has been returned to
    anyone, so `_start_service` saw its own `proc` still None and could neither re-tear-down nor
    register the survivor. It also gave up after one latch attempt; the kill and the latch now
    ALTERNATE for a bounded number of rounds, so a transient ENOSPC or a group that was merely slow
    to be reaped is not mistaken for a terminal one."""
    import ouroboros.process_custody as process_custody

    proc = _FakeServiceProc()
    kills, latches = [], []
    monkeypatch.setattr(
        process_custody.subprocess, "Popen", lambda *_a, **_k: proc, raising=True
    )
    monkeypatch.setattr(process_custody, "process_group_id", lambda _pid: 4242, raising=True)
    monkeypatch.setattr(process_custody, "process_start_time", lambda _pid: "", raising=True)
    monkeypatch.setattr(process_custody, "_live_cmd_sha256", lambda _pid: "", raising=True)
    monkeypatch.setattr(
        process_custody, "append_jsonl",
        lambda *_a, **_k: bool(latches.append(1)) and False, raising=True,
    )
    monkeypatch.setattr(
        process_custody, "_kill_and_prove_dead",
        lambda *args, **_k: bool(kills.append(args)) and False, raising=True,
    )

    with pytest.raises(process_custody.RepoWriterCustodyError) as excinfo:
        process_custody.spawn_supervised(
            ["python", "-m", "http.server"], drive_root=pathlib.Path("/unused"),
            purpose="service:web", scope="task", repo_writer=True,
        )

    assert excinfo.value.proc is proc, "the caller cannot control what it was never handed"
    rounds = process_custody._REPO_WRITER_REPUDIATION_ROUNDS
    assert len(kills) == rounds and len(latches) == rounds + 1, (
        "one append for the record itself, then a kill and a latch per bounded round"
    )


def test_a_start_with_no_lease_still_tears_down_the_handle_the_error_carried(monkeypatch, tmp_path):
    """`_start_service` inside a pooled WORKER takes no lease (that registry, and the fence that
    reads it, live in the supervisor), so retaining a lease is not an answer available there. What
    is available is the handle the error now carries: it is recovered BEFORE the teardown block, so
    the survivor gets one more bounded termination attempt instead of being walked away from as a
    start that never happened."""
    import ouroboros.process_custody as process_custody
    import ouroboros.protected_artifacts as protected_artifacts
    import ouroboros.tools.services as services

    workdir = tmp_path / "repo"
    workdir.mkdir()
    proc = _FakeServiceProc()
    torn_down = []

    class _Ctx:
        task_id = "t-1"
        drive_root = tmp_path

    def _boom(*_a, **_k):
        raise process_custody.RepoWriterCustodyError("neither killed nor latched", proc=proc)

    monkeypatch.setattr(services, "_SERVICES", {}, raising=True)
    monkeypatch.setattr(
        services, "resolve_shell_cwd",
        lambda *_a, **_k: (workdir, "active_workspace", []), raising=True,
    )
    monkeypatch.setattr(protected_artifacts, "shell_block_reason", lambda *_a, **_k: "", raising=True)
    monkeypatch.setattr(services, "_executor_can_run_cwd", lambda *_a, **_k: False, raising=True)
    monkeypatch.setattr(services, "task_service_teardown", lambda _ctx: "stop", raising=True)
    monkeypatch.setattr(services, "bootstrap_process_path", lambda: None, raising=True)
    monkeypatch.setattr(
        services, "_service_writes_managed_repo", lambda *_a, **_k: True, raising=True
    )
    # A pooled worker: no admission registry to lease from, so the start proceeds unleased.
    monkeypatch.setattr(
        services, "_acquire_repo_writer_lease", lambda _kind: ("", ""), raising=True
    )
    monkeypatch.setattr(
        services, "kill_process_tree", lambda p: torn_down.append(p), raising=True
    )
    monkeypatch.setattr(process_custody, "spawn_supervised", _boom, raising=True)

    out = services._start_service(_Ctx(), ["python", "-m", "http.server"], name="web")

    assert out.startswith("⚠️ SERVICE_START_ERROR")
    assert torn_down == [proc], "the handle the error carried is the one thing left to act on"


def test_a_repudiated_writer_that_can_be_latched_leaves_a_fence_visible_record(monkeypatch):
    """Postcondition 2 of repudiation, and the reason a False from `append_jsonl` is not cosmetic:
    the minimal entry is what makes an unkillable survivor visible to `live_repo_writer_processes`,
    so the next fence refuses the update instead of resetting the checkout under it."""
    import ouroboros.process_custody as process_custody

    latched = []
    monkeypatch.setattr(
        process_custody, "_kill_and_prove_dead", lambda *_a, **_k: False, raising=True
    )
    monkeypatch.setattr(
        process_custody, "append_jsonl",
        lambda _path, entry, *_a, **_k: bool(latched.append(entry)) or True, raising=True,
    )
    monkeypatch.setattr(process_custody, "process_group_id", lambda _pid: 7, raising=True)

    process_custody._repudiate_unregistered_repo_writer(
        pathlib.Path("/unused"), _FakeServiceProc(),
        purpose="service:web", scope="task", owner_task_id="t-1",
    )

    assert [entry["pid"] for entry in latched] == [4242]
    assert latched[0]["repo_writer"] is True, "only a marked entry is enumerated by the fence"


def test_an_unkillable_unlatchable_writer_cannot_leave_repudiation_quietly(monkeypatch):
    """Returning from `_repudiate_unregistered_repo_writer` ASSERTS one of exactly two
    postconditions: the writer is proven dead, or a fence-visible record exists. Neither held here
    and the function returned anyway — it logged and fell through, after which `spawn_supervised`
    raised its ORDINARY registration error. The caller could not tell "nothing is running" from "a
    repository writer is, and no fence can see it", so the original mandatory-custody failure
    survived the fix for it. An unavailable latch now escalates back to the only other postcondition
    (a second kill attempt, whose first signal has had the whole failing-append window to land) and,
    failing that, raises a DISTINCT error so a caller holding the admission lease can keep it."""
    import ouroboros.process_custody as process_custody

    kills = []
    monkeypatch.setattr(
        process_custody, "_kill_and_prove_dead",
        lambda *args, **_k: bool(kills.append(args)) and False, raising=True,
    )
    monkeypatch.setattr(process_custody, "append_jsonl", lambda *_a, **_k: False, raising=True)
    monkeypatch.setattr(process_custody, "process_group_id", lambda _pid: 0, raising=True)

    with pytest.raises(process_custody.RepoWriterCustodyError) as excinfo:
        process_custody._repudiate_unregistered_repo_writer(
            pathlib.Path("/unused"), _FakeServiceProc(),
            purpose="service:web", scope="task", owner_task_id="t-1",
        )

    assert "no fence-visible custody record" in str(excinfo.value)
    assert len(kills) == process_custody._REPO_WRITER_REPUDIATION_ROUNDS, \
        "every bounded round retries the kill; a failed latch is never accepted as an outcome"


def test_a_latch_that_raises_is_the_same_terminal_failure_as_one_that_reports(monkeypatch):
    """The two shapes `append_jsonl` can fail in must not diverge here: an exception and a False
    both mean "the fence was never told", so both have to reach the same refusal."""
    import ouroboros.process_custody as process_custody

    def _raise(*_a, **_k):
        raise OSError("read-only ledger")

    monkeypatch.setattr(
        process_custody, "_kill_and_prove_dead", lambda *_a, **_k: False, raising=True
    )
    monkeypatch.setattr(process_custody, "append_jsonl", _raise, raising=True)
    monkeypatch.setattr(process_custody, "process_group_id", lambda _pid: 0, raising=True)

    with pytest.raises(process_custody.RepoWriterCustodyError):
        process_custody._repudiate_unregistered_repo_writer(
            pathlib.Path("/unused"), _FakeServiceProc(),
            purpose="service:web", scope="task", owner_task_id="t-1",
        )


# --- BLOCK: a dead leader is not proof the GROUP is gone (v6.88.1 r6) --------------------------
#
# A repo-rooted service is spawned into its own process group running an arbitrary command, so a
# child that inherited the checkout keeps writing to it after the launcher exits. The strict
# fingerprint keys on the LEADER's pid, so both the fence sweep and the durable reaper concluded
# "the process we recorded is gone" and dropped the only handle on what was still running.


def test_a_live_group_after_the_leader_exits_is_still_a_repo_writer(
    monkeypatch, _hermetic_custody_ledger
):
    """`live_repo_writer_processes` must keep answering with the record while the recorded GROUP is
    alive, even though the leader pid is not."""
    import ouroboros.process_custody as process_custody

    _ledgered_repo_writer(_hermetic_custody_ledger)
    monkeypatch.setattr(process_custody, "pid_is_alive", lambda _pid: False, raising=True)
    monkeypatch.setattr(process_custody, "process_group_is_alive", lambda _pgid: True, raising=True)

    live = process_custody.live_repo_writer_processes(pathlib.Path("/unused"))

    assert [entry["pid"] for entry in live] == [9911]


def test_the_reaper_keeps_a_repo_writer_record_whose_group_outlived_its_leader(
    monkeypatch, _hermetic_custody_ledger
):
    """The durable sweep pruned the entry the moment the leader pid stopped answering — deleting
    the fence's only handle on the surviving group, permanently."""
    import ouroboros.process_custody as process_custody

    _ledgered_repo_writer(_hermetic_custody_ledger)
    monkeypatch.setattr(process_custody, "pid_is_alive", lambda _pid: False, raising=True)
    monkeypatch.setattr(process_custody, "process_group_is_alive", lambda _pgid: True, raising=True)

    process_custody.reap_orphaned_processes(pathlib.Path("/unused"))

    survivors = [
        json.loads(line)
        for line in _hermetic_custody_ledger.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert [entry["pid"] for entry in survivors] == [9911]


def test_a_group_only_record_is_killed_by_group_and_then_stops_blocking(
    monkeypatch, _hermetic_custody_ledger
):
    """Admitting a leader-dead record on a bare pgid probe has to come with a way OUT of it.

    Retaining such a record unsignalled was the first attempt, on the argument that the probe
    re-verifies no identity (POSIX frees a pgid once its group empties, and `process_group_is_alive`
    additionally fails closed on PermissionError). But nothing else signals it and nothing prunes it
    while its group answers, so the fence refused every update and every repo-rooted service start
    until the orphan happened to exit — an unbounded lockout with no operator action that clears it.
    So it is killed by group like any other ledgered writer, and the record self-clears once the
    group is gone. The identity risk this accepts is narrower than plain pid reuse: the branch is
    only reached when the recorded LEADER pid is already dead."""
    import ouroboros.process_custody as process_custody

    _ledgered_repo_writer(_hermetic_custody_ledger)
    alive = {"v": True}
    killed = _custody_liveness(monkeypatch, alive=alive)
    monkeypatch.setattr(process_custody, "pid_is_alive", lambda _pid: False, raising=True)
    monkeypatch.setattr(
        process_custody, "kill_process_group_id",
        lambda pgid: killed.append(pgid) or alive.__setitem__("v", False), raising=True,
    )

    assert process_custody.terminate_repo_writer_processes(pathlib.Path("/unused")) == []
    assert killed == [9911], "the group the leader left behind is the thing to signal"


def test_a_group_only_record_that_survives_the_kill_still_refuses_the_update(
    monkeypatch, _hermetic_custody_ledger
):
    """The fail-closed half: a group we signalled and cannot prove gone (a kill that raised EPERM
    because the pgid now belongs to another user, say) is still a blocker. That is the one standing
    refusal left, and no action available to this process would clear it."""
    import ouroboros.process_custody as process_custody

    _ledgered_repo_writer(_hermetic_custody_ledger)
    killed = _custody_liveness(monkeypatch, alive={"v": True})
    monkeypatch.setattr(process_custody, "pid_is_alive", lambda _pid: False, raising=True)
    monkeypatch.setattr(process_custody, "time", _FastGraceClock(), raising=True)

    remaining = process_custody.terminate_repo_writer_processes(pathlib.Path("/unused"))

    assert remaining == ["service:web#9911"]
    assert killed == [9911], "refused only AFTER a real attempt, never instead of one"


def test_a_dead_leader_with_a_live_group_keeps_its_admission_lease(monkeypatch):
    """The in-process half of the same defect: `_retire_repo_writer_lease_if_dead` released the
    lease on `proc.poll()` alone, so a fence saw no repository writer while the service's own
    children were still writing the checkout it was about to hard-reset."""
    import ouroboros.tools.services as services
    import supervisor.workers as workers

    lease, record = _repo_rooted_service(monkeypatch, dies=False)
    record.pgid = 9911
    record.proc.rc = -9  # the LEADER is gone
    monkeypatch.setattr(services, "process_group_is_alive", lambda _pgid: True, raising=True)

    assert services._retire_repo_writer_lease_if_dead(record) is False
    assert record.repo_writer_lease == lease
    assert workers.admitted_repo_writers() == [lease]

    # ...and once the group is gone too the lease comes straight back: the new check must not
    # become a permanent refusal of every later update.
    monkeypatch.setattr(services, "process_group_is_alive", lambda _pgid: False, raising=True)
    assert services._retire_repo_writer_lease_if_dead(record) is True
    assert workers.admitted_repo_writers() == []


def test_stopping_a_service_waits_for_the_group_it_signalled(monkeypatch):
    """`_stop_record` waits on the LEADER only. With the lease now gated on the group as well, a
    group that was killed but not yet reaped would hold its lease for the whole fence and refuse a
    legitimate update on scheduling latency alone — so the stop gets a bounded grace window."""
    import ouroboros.tools.services as services
    import supervisor.workers as workers

    _lease, record = _repo_rooted_service(monkeypatch, dies=False)
    record.pgid = 9911
    alive = {"v": True}

    def _probe(_pgid):
        # Alive on the first probe, gone on the next: exactly the un-reaped window.
        was, alive["v"] = alive["v"], False
        return was

    monkeypatch.setattr(
        services, "kill_process_group_id",
        lambda _pgid: setattr(record.proc, "rc", -9), raising=True,
    )
    monkeypatch.setattr(services, "process_group_is_alive", _probe, raising=True)
    monkeypatch.setattr(services, "time", _FastGraceClock(), raising=True)

    services._stop_record(record)

    assert record.repo_writer_lease == ""
    assert workers.admitted_repo_writers() == []


def test_stopping_an_unleased_service_does_not_wait_on_its_group(monkeypatch):
    """The grace window exists for the LEASE, so it is gated on the lease. `pgid` is set for every
    locally spawned service, so gating on it alone made ordinary task and shutdown teardown — which
    walk every record in a loop — pay up to the full group-reap grace each, polling for an answer
    nothing on those paths reads."""
    import ouroboros.tools.services as services

    _lease, record = _repo_rooted_service(monkeypatch, dies=False)
    record.pgid = 9911
    services._release_repo_writer_lease(record.repo_writer_lease)
    record.repo_writer_lease = ""
    probes = []
    monkeypatch.setattr(services, "kill_process_group_id", lambda _pgid: None, raising=True)
    monkeypatch.setattr(
        services, "process_group_is_alive",
        lambda pgid: bool(probes.append(pgid)) or True, raising=True,
    )

    services._stop_record(record)

    assert probes == [], "no lease, no decision riding on the group, no wait"


def test_a_restart_cannot_drop_a_record_whose_group_still_holds_the_lease(monkeypatch):
    """A successful start OVERWRITES `_SERVICES[key]`, after which nothing can reach the previous
    record again — so its lease is retired first. That call used to discard its result, because an
    exited leader was proof the retirement succeeded. It is not any more: leader-exited-but-group-
    alive is precisely the case the group check was added to catch, and it is now a False at a site
    written for a function that could not fail. Dropping the record there would strand its lease in
    the admission registry for the life of the process (refusing every later managed update) and
    remove the fence's only handle on the surviving group, so the start is refused instead."""
    import ouroboros.tools.services as services
    import supervisor.workers as workers

    lease, record = _repo_rooted_service(monkeypatch, dies=False)
    record.pgid = 9911
    record.proc.rc = -9  # the LEADER exited; its group did not
    monkeypatch.setattr(services, "kill_process_group_id", lambda _pgid: None, raising=True)
    monkeypatch.setattr(services, "process_group_is_alive", lambda _pgid: True, raising=True)
    monkeypatch.setattr(services, "time", _FastGraceClock(), raising=True)

    class _Ctx:
        task_id = "t-1"

    refusal = services._start_service(_Ctx(), ["python", "-m", "http.server"], name="web")

    assert refusal.startswith("⚠️ SERVICE_REPO_LOCKED")
    assert services._SERVICES["t-1:web"] is record, "the only handle on the group must survive"
    assert record.repo_writer_lease == lease
    assert workers.admitted_repo_writers() == [lease]


def test_an_unrecordable_repository_writer_keeps_its_lease_instead_of_handing_it_back(
    monkeypatch, tmp_path
):
    """The other end of the repudiation postcondition, and what makes the distinct error worth
    raising. `_start_service`'s spawn-failure handler releases the lease whenever `proc is None`, on
    the reasoning that nothing was ever started. That reasoning does not survive
    `RepoWriterCustodyError`: the raise comes from INSIDE `spawn_supervised`, so this frame never
    receives the handle even though a repository writer is running — and that error means
    specifically that custody could neither prove it dead nor make it visible to the fence.
    Releasing there would leave a writer inside the checkout with nothing left to refuse on."""
    import ouroboros.process_custody as process_custody
    import ouroboros.tools.services as services
    import ouroboros.protected_artifacts as protected_artifacts
    import supervisor.workers as workers

    workdir = tmp_path / "repo"
    workdir.mkdir()

    class _Ctx:
        task_id = "t-1"
        drive_root = tmp_path

    def _boom(*_a, **_k):
        raise process_custody.RepoWriterCustodyError("neither killed nor latched")

    monkeypatch.setattr(services, "_SERVICES", {}, raising=True)
    monkeypatch.setattr(
        services, "resolve_shell_cwd",
        lambda *_a, **_k: (workdir, "active_workspace", []), raising=True,
    )
    monkeypatch.setattr(protected_artifacts, "shell_block_reason", lambda *_a, **_k: "", raising=True)
    monkeypatch.setattr(services, "_executor_can_run_cwd", lambda *_a, **_k: False, raising=True)
    monkeypatch.setattr(services, "task_service_teardown", lambda _ctx: "stop", raising=True)
    monkeypatch.setattr(services, "bootstrap_process_path", lambda: None, raising=True)
    monkeypatch.setattr(
        services, "_service_writes_managed_repo", lambda *_a, **_k: True, raising=True
    )
    monkeypatch.setattr(process_custody, "spawn_supervised", _boom, raising=True)

    out = services._start_service(_Ctx(), ["python", "-m", "http.server"], name="web")

    assert out.startswith("⚠️ SERVICE_START_ERROR")
    assert len(workers.admitted_repo_writers()) == 1, (
        "the lease is the last blocker between an invisible writer and a hard reset"
    )
    assert services._SERVICES == {}, "and no half-started record claims the key"


# --- FIX_FIRST: no persistent intent may exist without its transaction (v6.88.1 r6) ------------
#
# The update INTENT is consumed by the next boot's `checkout_and_reset` as an instruction to hard
# reset onto the update target. The TRANSACTION is the only record of where to return to. An intent
# that exists without one is an unsupervised update with no rollback point, and the lifecycle had a
# window at BOTH ends: preparation published the intent before the transaction was written, and
# every cleanup path cleared the transaction first, or only.


def test_the_transaction_is_written_before_the_preparation_publishes_the_intent():
    """Ordering pin. `prepare_managed_update` ENDS by writing the intent, so a transaction written
    after it left a crash window in which the marker outlived nothing at all."""
    import inspect

    source = inspect.getsource(control._apply_replace_family_fenced)
    assert source.index("write_update_tx(") < source.index("prepare_managed_update(")


def test_a_failed_preparation_removes_the_intent_before_the_transaction(
    official_delta, replace_env, monkeypatch
):
    """Unwinding the pre-written transaction has exactly one safe order, and it is the one the
    rollback uses: prove the intent gone, THEN clear the transaction that explains it."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    official_delta(["ouroboros/loop.py"])
    order = []
    monkeypatch.setattr(
        git_ops, "prepare_managed_update",
        lambda *_a, **_k: (False, {"error": "no"}), raising=True,
    )
    monkeypatch.setattr(
        git_ops, "_clear_update_intent",
        lambda *_a, **_k: bool(order.append("intent")) or True, raising=True,
    )
    monkeypatch.setattr(
        update_merge, "clear_update_tx", lambda: bool(order.append("tx")) or True, raising=True
    )

    resp = asyncio.run(control.api_update_apply(_Request({"strategy": "replace"})))

    assert resp.status_code == 409
    assert order == ["intent", "tx"]
    assert "respawn" in replace_env["calls"], "nothing was mutated; the pool comes back"


def test_a_preparation_whose_intent_cannot_be_removed_keeps_the_transaction(
    official_delta, replace_env, monkeypatch
):
    """Fail closed: an intent WITH a transaction is a state the boot path recovers, an intent alone
    is an unsupervised reset. So an unprovable removal keeps the transaction and keeps the writers
    locked out rather than finishing the cleanup halfway."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    official_delta(["ouroboros/loop.py"])
    cleared = []
    monkeypatch.setattr(
        git_ops, "prepare_managed_update",
        lambda *_a, **_k: (False, {"error": "no"}), raising=True,
    )
    monkeypatch.setattr(git_ops, "_clear_update_intent", lambda *_a, **_k: False, raising=True)
    monkeypatch.setattr(update_merge, "clear_update_tx", lambda: cleared.append(1), raising=True)

    resp = asyncio.run(control.api_update_apply(_Request({"strategy": "replace"})))
    body = json.loads(resp.body)

    assert resp.status_code == 409
    assert body["reason"] == "update_recovery_failed"
    assert cleared == []
    assert "respawn" not in replace_env["calls"], "writers stay out until a restart"


def test_a_preparation_whose_transaction_cannot_be_removed_locks_the_writers_out(
    official_delta, replace_env, monkeypatch
):
    """The other half of the same unwind. The intent is proven gone, so the boot path can no longer
    recover anything — but a transaction still on disk means `active_update_tx()` goes on demanding
    recovery, and the pre-update SHA it names no longer describes the checkout the pool would come
    back to. Respawning here would put writers on that tree, so the response locks down instead."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    official_delta(["ouroboros/loop.py"])
    monkeypatch.setattr(
        git_ops, "prepare_managed_update",
        lambda *_a, **_k: (False, {"error": "no"}), raising=True,
    )
    monkeypatch.setattr(git_ops, "_clear_update_intent", lambda *_a, **_k: True, raising=True)
    monkeypatch.setattr(update_merge, "clear_update_tx", lambda: False, raising=True)

    resp = asyncio.run(control.api_update_apply(_Request({"strategy": "replace"})))
    body = json.loads(resp.body)

    assert resp.status_code == 409
    assert body["reason"] == "update_recovery_failed"
    assert "respawn" not in replace_env["calls"], "writers stay out until a restart"


def test_a_rollback_that_cannot_remove_the_transaction_is_not_a_rollback(monkeypatch):
    """`clear_update_tx` used to return nothing, so this caller — the one that had already proven
    the tree restored — answered `True` over a marker still on disk. The caller reads that `True` as
    the licence to re-admit writers, while the surviving transaction makes the NEXT boot recover an
    update that has already been undone, re-resetting a tree that is now correct."""
    import supervisor.update_merge as update_merge

    cleared = _rollback_env(monkeypatch, tx_cleared=False)
    ok, msg = update_merge.rollback_managed_update("test")

    assert ok is False
    assert "update transaction marker" in msg
    assert cleared == ["intent", "tx"], "the removal is attempted; it is the PROOF that fails"


def test_clearing_the_update_transaction_answers_whether_it_is_proven_gone(monkeypatch, tmp_path):
    """The mirror of the intent pin below, and the reason all three callers above can be trusted:
    the unlink's own outcome is not the fact they act on — the marker is re-stat'ed afterwards."""
    import supervisor.update_merge as update_merge

    marker = tmp_path / "update_tx.json"
    monkeypatch.setattr(update_merge, "_update_tx_marker_path", lambda: marker, raising=True)

    assert update_merge.clear_update_tx() is True, "already absent is proven absent"

    marker.write_text("{}", encoding="utf-8")
    assert update_merge.clear_update_tx() is True
    assert not marker.exists()

    marker.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        pathlib.Path, "unlink",
        lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("read-only")), raising=True,
    )
    assert update_merge.clear_update_tx() is False, "a marker still on disk is not cleared"


def test_clearing_the_update_intent_answers_whether_it_is_proven_gone(monkeypatch, tmp_path):
    """The unlink's own outcome is not the fact the callers act on — the marker is re-stat'ed. An
    unlink that raised used to be logged and swallowed, reporting a marker still on disk as gone."""
    import supervisor.git_ops as git_ops

    marker = tmp_path / "update_intent.json"
    monkeypatch.setattr(git_ops, "_update_intent_marker_path", lambda: marker, raising=True)

    assert git_ops._clear_update_intent() is True, "already absent is proven absent"

    marker.write_text("{}", encoding="utf-8")
    assert git_ops._clear_update_intent() is True
    assert not marker.exists()

    marker.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        pathlib.Path, "unlink",
        lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("read-only")), raising=True,
    )
    assert git_ops._clear_update_intent() is False, "a marker still on disk is not cleared"


def test_a_healthy_finalization_removes_the_intent_before_the_transaction(monkeypatch):
    """The healthy boot path cleared ONLY the transaction, leaving the intent for the next boot to
    consume: a second, unrequested hard reset onto the same target with no transaction to bound
    it."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    order = []
    monkeypatch.setattr(git_ops, "git_capture", lambda *_a, **_k: (0, "c" * 40, ""), raising=True)
    monkeypatch.setattr(
        git_ops, "_clear_update_intent",
        lambda *_a, **_k: bool(order.append("intent")) or True, raising=True,
    )
    monkeypatch.setattr(
        update_merge, "clear_update_tx", lambda: bool(order.append("tx")) or True, raising=True
    )
    monkeypatch.setattr(update_merge, "append_jsonl", lambda *_a, **_k: None, raising=True)

    out = update_merge._finalize_pending_boot_smoke({"merge_commit": "c" * 40}, True)

    assert out == {"finalized": True}
    assert order == ["intent", "tx"]


def test_a_finalization_that_cannot_remove_the_intent_is_not_finalized(monkeypatch):
    """...and it counts the boot. The deferral used to return before `boot_attempts` was persisted,
    which made the `attempts >= 2` rollback backstop below it unreachable on precisely the failure
    it guards: an unremovable marker deferred identically on every boot, forever."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    cleared, written = [], []
    monkeypatch.setattr(git_ops, "git_capture", lambda *_a, **_k: (0, "c" * 40, ""), raising=True)
    monkeypatch.setattr(git_ops, "_clear_update_intent", lambda *_a, **_k: False, raising=True)
    monkeypatch.setattr(
        update_merge, "clear_update_tx", lambda: bool(cleared.append(1)) or True, raising=True
    )
    monkeypatch.setattr(update_merge, "write_update_tx", written.append, raising=True)
    monkeypatch.setattr(update_merge, "append_jsonl", lambda *_a, **_k: None, raising=True)

    out = update_merge._finalize_pending_boot_smoke({"merge_commit": "c" * 40}, True)

    assert out["finalized"] is False
    assert cleared == [], "the transaction is what keeps the leftover intent recoverable"
    assert out["boot_attempts"] == 1 and written[-1]["boot_attempts"] == 1


def test_a_finalization_that_cannot_remove_the_transaction_is_not_finalized(monkeypatch):
    """The same rule one marker along: `clear_update_tx` swallowed its unlink failure and returned
    nothing, so a transaction still on disk — one `active_update_tx()` goes on treating as an update
    in progress — was reported as `finalized: true`."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    written = []
    monkeypatch.setattr(git_ops, "git_capture", lambda *_a, **_k: (0, "c" * 40, ""), raising=True)
    monkeypatch.setattr(git_ops, "_clear_update_intent", lambda *_a, **_k: True, raising=True)
    monkeypatch.setattr(update_merge, "clear_update_tx", lambda: False, raising=True)
    monkeypatch.setattr(update_merge, "write_update_tx", written.append, raising=True)
    monkeypatch.setattr(update_merge, "append_jsonl", lambda *_a, **_k: None, raising=True)

    out = update_merge._finalize_pending_boot_smoke({"merge_commit": "c" * 40}, True)

    assert out["finalized"] is False
    assert written[-1]["boot_attempts"] == 1


def test_a_second_boot_that_still_cannot_clear_the_markers_reaches_the_rollback(monkeypatch):
    """The bounded exit. The healthy branch returns before the counter on a cleanup failure, so the
    deferral has to FALL THROUGH to the shared escalation — otherwise the only path out of an
    unremovable marker is a human noticing. Second boot, counter at 1: rollback."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    rolled = []
    monkeypatch.setattr(git_ops, "git_capture", lambda *_a, **_k: (0, "c" * 40, ""), raising=True)
    monkeypatch.setattr(git_ops, "_clear_update_intent", lambda *_a, **_k: False, raising=True)
    monkeypatch.setattr(update_merge, "clear_update_tx", lambda: True, raising=True)
    monkeypatch.setattr(update_merge, "write_update_tx", lambda _tx: None, raising=True)
    monkeypatch.setattr(update_merge, "append_jsonl", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(
        update_merge, "rollback_managed_update",
        lambda reason: (rolled.append(reason) or True, "rolled back"), raising=True,
    )

    out = update_merge._finalize_pending_boot_smoke(
        {"merge_commit": "c" * 40, "boot_attempts": 1}, True
    )

    assert out["finalized"] is False
    assert rolled == ["post_boot_smoke_failed"], "the backstop must be reachable from this failure"


def test_a_leftover_intent_means_the_repository_is_not_recovered(monkeypatch, tmp_path):
    """`managed_update_repository_is_recovered` decides whether writers may be let back onto the
    checkout. An intent changes no file, so it read as recovered — and handed writers a tree that
    was scheduled to be hard-reset out from under them at the next boot."""
    import supervisor.git_ops as git_ops
    import supervisor.update_merge as update_merge

    marker = tmp_path / "update_intent.json"
    monkeypatch.setattr(update_merge, "active_update_tx", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(git_ops, "_update_intent_marker_path", lambda: marker, raising=True)
    # A clean index and no resolvable MERGE_HEAD: every OTHER recovery condition is satisfied, so
    # the intent is the only thing this pin can be answering.
    monkeypatch.setattr(
        git_ops, "git_capture",
        lambda cmd, *_a, **_k: (1, "", "") if "MERGE_HEAD" in cmd else (0, "", ""), raising=True,
    )

    assert update_merge.managed_update_repository_is_recovered() is True

    marker.write_text("{}", encoding="utf-8")
    assert update_merge.managed_update_repository_is_recovered() is False


def test_a_checkout_that_cannot_clear_the_consumed_intent_is_not_ok():
    """`checkout_and_reset` returns the boolean its callers use to decide whether writers may come
    back. An intent that survives the checkout it described is consumed AGAIN on the next boot, so
    an unprovable removal is reported as a failed checkout. Source-pinned: reaching this line needs
    a real clone, a real remote and a real reset, and what it pins is the guard, not the checkout."""
    import inspect

    import supervisor.git_ops as git_ops

    source = inspect.getsource(git_ops.checkout_and_reset)
    guard = source.index("if not _clear_update_intent():")
    refusal = source.index("its marker could not be removed", guard)
    assert guard < refusal < source.index('return True, "ok"', guard)


def test_the_bootstrap_refuses_to_apply_an_orphaned_intent_as_an_ordinary_reset():
    """With no transaction the bootstrap picks `rescue_and_reset`, and ordinary reset recovery is
    performed by CONSUMING any intent it finds. An intent that outlived its transaction would
    therefore be applied here as an unsupervised hard reset onto an update target, with no rollback
    point recorded anywhere. Source-pinned: `_bootstrap_supervisor_repo` needs a launcher-managed
    process and a real `safe_restart` to drive, and what this pins is the guard's placement
    relative to that restart."""
    import inspect

    import server

    source = inspect.getsource(server._bootstrap_supervisor_repo)
    guard = source.index("if not _managed_update_active:")
    refusal = source.index("refusing to bootstrap rather than apply it as an ordinary reset")
    restart = source.index('safe_restart(reason="bootstrap"')
    assert guard < refusal < restart, "the guard must precede the restart that would consume it"
    assert "_clear_update_intent()" in source[guard:refusal]


# --- NIT: an unknown strategy is COERCED, and nothing pinned it (v6.88.1 r6) -------------------

def test_prepare_managed_update_coerces_an_unknown_strategy_to_replace(monkeypatch):
    """Every routing test stubs `prepare_managed_update`, so the coercion at its head was covered by
    nothing: a typo'd or newly added strategy silently becomes the DESTRUCTIVE replace rather than
    being refused. Pinned through the intent the preparation publishes, which is where the resolved
    strategy is durably recorded."""
    import supervisor.git_ops as git_ops

    written = {}
    monkeypatch.setattr(
        git_ops, "compute_managed_update_status",
        lambda **_k: {"managed": True, "available": True, "current_sha": "a" * 40,
                      "latest_sha": "b" * 40, "target_ref": "origin/ouroboros"},
        raising=True,
    )
    monkeypatch.setattr(git_ops, "_collect_repo_sync_state", lambda: {}, raising=True)
    monkeypatch.setattr(git_ops, "_create_rescue_snapshot", lambda **_k: {}, raising=True)
    monkeypatch.setattr(git_ops, "_rescue_untracked_incomplete", lambda _r: "", raising=True)
    monkeypatch.setattr(
        git_ops, "_compute_ref_ahead_count", lambda *_a, **_k: (True, 0, ""), raising=True
    )
    monkeypatch.setattr(git_ops, "_write_update_intent", written.update, raising=True)
    monkeypatch.setattr(git_ops, "append_jsonl", lambda *_a, **_k: None, raising=True)

    ok, _payload = git_ops.prepare_managed_update("teleport")

    assert ok is True
    assert written["strategy"] == "replace", (
        "an unrecognized strategy resolves to the destructive family; that has to be visible"
    )
