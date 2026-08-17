"""Phase D: route_health pinned-profile pass-through + structural disclosure.

The live incident (2026-08-18): reviewer slots pinned a verified credential
profile on a route whose harness row reads status="unavailable" FOREVER by
design (agy has no default credential store — engine INV-135; only named
profiles work). `route_health` refused on the row status BEFORE the pinned
credentialProfileId ever reached the engine, so a working pin could never be
used. Owner decision (batch 2, 1A/2): when a profile is pinned, the row-status
refusal is SKIPPED and the ENGINE's typed refusal is authoritative. Everything
else — catalog absence, access-profile fit, the delegated-marker version
floor, quota windows — still applies unchanged.

Offline unit fixtures only: a duck-typed gateway answering the two manifest
questions route_health asks. No daemon, no network.
"""

from ouroboros.subagents import delegated_run_shape, route_health


class _Gateway:
    """agy-shaped catalog row: unavailable forever, structurally non-delegable."""

    engine_version = "9.9.9"

    def __init__(self, *, status="unavailable", enabled=False, delegation=None):
        self._row = {
            "id": "agy-like", "enabled": enabled, "status": status,
            "accessProfilesSupported": ["readonly", "workspace_write"],
        }
        if delegation is not None:
            self._row["delegation"] = delegation

    def agent_capabilities(self):
        return {"harnesses": [dict(self._row)]}

    def quota_snapshots(self):
        return []


def test_a_pinned_profile_skips_only_the_row_status_refusal():
    shape = delegated_run_shape(False)
    gw = _Gateway()
    # Unpinned: today's typed refusal is byte-identical (probe_subagent_executor
    # and tools/delegate.py pass no profile — their behavior must not move).
    assert route_health(gw, "agy-like", shape) == ("route_status_unavailable", "")
    # Pinned: the row-status refusal is skipped; the shape checks still pass, so
    # the request proceeds and the ENGINE's typed answer is authoritative.
    assert route_health(gw, "agy-like", shape, pinned_profile="acct-1") == ("", "")


def test_a_pinned_profile_does_not_skip_the_catalog_absence_refusal():
    shape = delegated_run_shape(False)
    # A route the catalog does not carry at all has no engine row to be
    # authoritative about: still refused, pinned or not.
    assert route_health(_Gateway(), "missing-route", shape, pinned_profile="acct-1") == (
        "route_not_in_capability_catalog", "")


def test_a_pinned_profile_does_not_skip_the_shape_checks():
    # The access-profile check judges the SHAPE, not the credentials: a pin
    # cannot make a read-only route writable.
    class _ReadOnly(_Gateway):
        def __init__(self):
            super().__init__(status="ok", enabled=True)
            self._row["accessProfilesSupported"] = ["readonly"]

    acting = delegated_run_shape(True)
    unavailable, _ = route_health(_ReadOnly(), "agy-like", acting, pinned_profile="acct-1")
    assert unavailable == "access_profile_unsupported:workspace_write"
    # And the delegated-marker version floor stays: an engine that would 400 the
    # marker is refused before a token is spent, pinned or not.
    old = _Gateway(status="ok", enabled=True)
    old.engine_version = "0.0.1"
    unavailable, _ = route_health(old, "agy-like", acting, pinned_profile="acct-1")
    assert unavailable == "engine_rejects_delegated_marker"


def test_the_status_refusal_code_carries_the_structural_delegation_fact():
    """Phase D3 (disclosure only): the catalog row carries typed delegation facts
    (`delegation: {available, reason, ...}` — verified live on engine 3.5.0; agy
    reports available=false, reason="manifest_unsupported"). When a DELEGATED
    shape is refused on row status and the row says it structurally cannot run
    delegated work, the refusal code carries that fact — same refusal, refined
    code, no new gate."""
    structural = _Gateway(
        delegation={"available": False, "reason": "manifest_unsupported"})
    acting = delegated_run_shape(True)
    unavailable, _ = route_health(structural, "agy-like", acting)
    assert unavailable == "route_status_unavailable:delegation_manifest_unsupported"
    # A non-delegated shape does not consult the delegation facts: they describe
    # delegated (MCP-injected) work only.
    assert route_health(structural, "agy-like", delegated_run_shape(False)) == (
        "route_status_unavailable", "")
    # A row without the delegation object (older engines, plain outage) keeps
    # today's code byte-identically.
    assert route_health(_Gateway(), "agy-like", acting) == ("route_status_unavailable", "")
    # And a delegation object that is merely absent-of-detail is not structural:
    # only an EXPLICIT available=false speaks.
    vague = _Gateway(delegation={})
    assert route_health(vague, "agy-like", acting) == ("route_status_unavailable", "")


def test_blocked_pin_wording_is_structural_when_the_manifest_cannot_delegate():
    """Phase D3: the blocked-pin terminal must not tell the parent to
    «Reschedule once the route recovers» when the incapability is structural —
    on agy that honestly means «wait forever»."""
    from ouroboros.agent import executor_blocked_outcome
    from ouroboros.subagents import SubagentExecutorResolution

    structural, usage = executor_blocked_outcome(SubagentExecutorResolution(
        requested="harness", executor="blocked",
        reason="route_status_unavailable:delegation_manifest_unsupported"))
    assert "Reschedule once the route recovers" not in structural
    assert "structurally cannot run delegated work" in structural
    assert "NOT run on metered API tokens" in structural  # the pin still spent nothing
    assert usage == {"execution_status": "infra_failed",
                     "reason_code": "subagent_executor_unavailable"}
    # A plain (possibly transient) outage keeps today's wording unchanged.
    transient, _ = executor_blocked_outcome(SubagentExecutorResolution(
        requested="harness", executor="blocked", reason="route_status_degraded"))
    assert "Reschedule once the route recovers" in transient
