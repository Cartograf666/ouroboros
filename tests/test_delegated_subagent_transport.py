"""Phase 3: Claudexor transport, the nanny verbs, and their accounting/failure classes."""

from __future__ import annotations

import datetime
import json
import pathlib

import httpx
import pytest

from ouroboros import subagents, usage_accounting as ua
from ouroboros.config import (
    CLAUDEXOR_DELEGATED_MARKER_MIN_VERSION,
    CLAUDEXOR_MIN_VERSION,
    CLAUDEXOR_PROTOCOL_MAJOR,
)
from ouroboros.gateways import claudexor as cx
from ouroboros.loop_llm_call import SUBSCRIPTION_WINDOW_EXHAUSTED, classify_llm_exception
from ouroboros.provider_models import MODEL_SETTING_KEYS
from ouroboros.tool_capabilities import (
    ACTING_SUBAGENT_TOOL_NAMES,
    LOCAL_READONLY_SUBAGENT_TOOL_NAMES,
)

NANNY_TOOLS = {"delegate_start", "delegate_wait", "delegate_cancel"}


# -- 3.1 the narrow setting key ------------------------------------------------


def test_subagent_harness_key_stays_out_of_the_model_key_sweep():
    # A session-only route is not an API model identity: leaking it into
    # MODEL_SETTING_KEYS would poison credential planning, pricing and provenance.
    assert "OUROBOROS_SUBAGENT_HARNESS" not in MODEL_SETTING_KEYS


@pytest.mark.parametrize("raw,expected", [
    ("", None),
    ("codex", subagents.DelegationRoute("codex", "", "")),
    ("codex=gpt-5.4-mini", subagents.DelegationRoute("codex", "gpt-5.4-mini", "")),
    ("codex=gpt-5.4-mini:low", subagents.DelegationRoute("codex", "gpt-5.4-mini", "low")),
])
def test_route_parsing_is_opaque(raw, expected):
    assert subagents.parse_subagent_harness(raw) == expected


def test_get_subagent_harness_reads_the_env_key(monkeypatch):
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=some-model:high")
    route = subagents.get_subagent_harness()
    assert route is not None and route.route_id == "some-route"
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "")
    assert subagents.get_subagent_harness() is None


# -- 3.5 the execution rule table ----------------------------------------------


ROUTE = subagents.DelegationRoute("some-route", "m", "low")


def test_rule_auto_without_harness_runs_native():
    res = subagents.resolve_subagent_executor("auto", route=None)
    assert (res.executor, res.reason) == ("native", "harness_not_configured")


def test_rule_auto_with_healthy_harness_delegates():
    res = subagents.resolve_subagent_executor("auto", route=ROUTE)
    assert (res.executor, res.reason) == ("harness", "harness_ready")


def test_rule_auto_with_every_profile_spent_falls_back_to_the_api_loudly():
    """Owner decision D28. It used to dispatch the child as a NANNY anyway, whose very
    first `delegate_start` was then refused with this SAME fact (executed and pinned
    below) — a spent dispatch, and the child left to improvise a fallback in prose.
    `auto` now falls back to the metered API at the one point that still costs nothing,
    typed, with the reset instant riding along so waiting stays a visible option."""
    res = subagents.resolve_subagent_executor("auto", route=ROUTE, reset_at="2030-01-01T00:00:00Z")
    assert res.executor == "native", "auto must not be dispatched onto a spent substrate"
    assert res.reason == SUBSCRIPTION_WINDOW_EXHAUSTED
    assert res.reset_at == "2030-01-01T00:00:00Z"
    assert not res.blocked, "never a permanent block while metered keys exist"


def test_rule_auto_with_unavailable_harness_falls_native_with_a_visible_marker():
    res = subagents.resolve_subagent_executor("auto", route=ROUTE, unavailable_reason="daemon_unreachable")
    assert (res.executor, res.reason) == ("native", "daemon_unreachable")


@pytest.mark.parametrize("kwargs,reason", [
    ({"route": None}, "harness_not_configured"),
    ({"route": ROUTE, "unavailable_reason": "daemon_unreachable"}, "daemon_unreachable"),
    ({"route": ROUTE, "reset_at": "2030-01-01T00:00:00Z"}, SUBSCRIPTION_WINDOW_EXHAUSTED),
])
def test_rule_explicit_harness_blocks_instead_of_spending_api_money(kwargs, reason):
    res = subagents.resolve_subagent_executor("harness", **kwargs)
    assert res.blocked and res.reason == reason


def test_rule_native_is_native_whatever_the_state():
    res = subagents.resolve_subagent_executor("native", route=ROUTE, unavailable_reason="x")
    assert (res.executor, res.reason) == ("native", "requested_native")


def test_unknown_executor_is_rejected():
    with pytest.raises(ValueError):
        subagents.resolve_subagent_executor("magic")


# -- 3.2 transport -------------------------------------------------------------


def _gateway(handler) -> cx.ClaudexorGateway:
    gateway = cx.ClaudexorGateway(cx.DaemonEndpoint("127.0.0.1", 1, "secret-token"))
    gateway._client = httpx.Client(
        base_url="http://127.0.0.1:1",
        transport=httpx.MockTransport(handler),
        headers=dict(gateway._client.headers),
    )
    return gateway


def test_discovery_missing_descriptor_is_a_typed_refusal(tmp_path):
    with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
        cx.discover_daemon(tmp_path)
    assert excinfo.value.code == "daemon_not_discovered"


def test_discovery_reads_host_port_and_token(tmp_path):
    daemon_dir = tmp_path / ".claudexor" / "v3" / "daemon"
    daemon_dir.mkdir(parents=True)
    (daemon_dir / "token").write_text("tok\n", encoding="utf-8")
    (daemon_dir / "control-api.json").write_text(json.dumps({
        "host": "127.0.0.1", "port": 4242, "tokenPath": str(daemon_dir / "token"),
    }), encoding="utf-8")
    endpoint = cx.discover_daemon(tmp_path)
    assert (endpoint.host, endpoint.port, endpoint.token) == ("127.0.0.1", 4242, "tok")


@pytest.mark.parametrize("host,loopback", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True), ("[::1]", True),
    ("127.1.2.3", True), ("fe80::1%lo0", False),
    # The exfiltration shapes: a plain external name, a name that merely LOOKS like a
    # loopback literal, an address that resolves off-host, and the wildcard bind.
    ("evil.example.com", False), ("127.0.0.1.evil.com", False),
    ("10.0.0.5", False), ("0.0.0.0", False), ("169.254.169.254", False),
])
def test_the_daemon_token_is_only_ever_sent_to_loopback(tmp_path, host, loopback):
    """P34P1.3: `discover_daemon` accepted ANY host from control-api.json and the
    gateway shipped the whole-/v2 bearer there. The loopback boundary was documented
    and unenforced, so anything able to write one file under ~/.claudexor could
    redirect the daemon token to a host it controls — token exfiltration plus
    authenticated SSRF, from a file write. The refusal is typed and happens BEFORE any
    client exists; a name is never resolved (a name that resolves to loopback now can
    resolve elsewhere on the next lookup), so only literal loopback addresses and the
    exact name `localhost` pass."""
    daemon_dir = tmp_path / ".claudexor" / "v3" / "daemon"
    daemon_dir.mkdir(parents=True)
    (daemon_dir / "token").write_text("super-secret-daemon-token\n", encoding="utf-8")
    (daemon_dir / "control-api.json").write_text(json.dumps({
        "host": host, "port": 4242, "tokenPath": str(daemon_dir / "token"),
    }), encoding="utf-8")

    if loopback:
        endpoint = cx.discover_daemon(tmp_path)
        assert endpoint.host == host and endpoint.token == "super-secret-daemon-token"
        return
    with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
        cx.discover_daemon(tmp_path)
    assert excinfo.value.code == "daemon_endpoint_not_loopback"
    assert host in str(excinfo.value)
    # The token must not have travelled: the refusal precedes client construction.
    assert "super-secret-daemon-token" not in str(excinfo.value)


@pytest.mark.parametrize("token_name, token_bytes", [
    # A path out of a JSON descriptor can carry an embedded null: `read_text` raises a
    # bare `ValueError`, which is NOT an `OSError`.
    ("to\x00ken", None),
    # The token FILE can hold bytes that are not UTF-8: `UnicodeDecodeError` is likewise
    # a `ValueError` and not an `OSError`.
    ("token", b"\xff\xfetok"),
])
def test_an_unreadable_token_is_a_typed_refusal_not_a_bare_ValueError(
        tmp_path, token_name, token_bytes):
    """The half of the v6.87.44 widening nothing could falsify.

    The suite referenced `daemon_token_unreadable` nowhere and its only `tokenPath`
    fixture was a valid path, so reverting the catch to `except OSError` left every test
    green while a `ValueError` escaped `discover_daemon` untyped — past the
    `except ClaudexorUnavailable` in `subagents.py` and `delegate.py`, as a traceback.
    The `isinstance` assertion below is the actual claim: the caller's handler catches it.
    """
    daemon_dir = tmp_path / ".claudexor" / "v3" / "daemon"
    daemon_dir.mkdir(parents=True)
    token_path = str(daemon_dir / token_name)
    if token_bytes is not None:
        pathlib.Path(token_path).write_bytes(token_bytes)
    (daemon_dir / "control-api.json").write_text(json.dumps({
        "host": "127.0.0.1", "port": 4242, "tokenPath": token_path,
    }), encoding="utf-8")

    with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
        cx.discover_daemon(tmp_path)
    assert excinfo.value.code == "daemon_token_unreadable"
    assert isinstance(excinfo.value, cx.ClaudexorUnavailable)
    # The descriptor read four lines up refuses the identical shape. Asserting the pair
    # is what keeps them from drifting apart again.
    (daemon_dir / "control-api.json").unlink()
    (daemon_dir / "control-api.json").write_bytes(b"\xff\xfe{}")
    with pytest.raises(cx.ClaudexorUnavailable) as sibling:
        cx.discover_daemon(tmp_path)
    assert sibling.value.code == "daemon_descriptor_unreadable"


def test_handshake_sends_the_protocol_header_and_pins_the_minimum_version():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["header"] = request.headers.get(cx.PROTOCOL_HEADER)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={
            "protocolMajor": CLAUDEXOR_PROTOCOL_MAJOR,
            "compatible": True,
            "engine": {"version": CLAUDEXOR_MIN_VERSION},
        })

    with _gateway(handler) as gateway:
        gateway.handshake()
    assert seen["header"] == str(CLAUDEXOR_PROTOCOL_MAJOR)
    assert seen["auth"] == "Bearer secret-token"


def test_handshake_refuses_an_engine_older_than_the_minimum():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "protocolMajor": CLAUDEXOR_PROTOCOL_MAJOR, "compatible": True,
            "engine": {"version": "0.9.0"},
        })

    with _gateway(handler) as gateway:
        with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
            gateway.handshake()
    assert excinfo.value.code == "engine_too_old"


def test_handshake_refuses_an_incompatible_protocol():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"protocolMajor": 2, "compatible": False})

    with _gateway(handler) as gateway:
        with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
            gateway.handshake()
    assert excinfo.value.code == "protocol_incompatible"


def test_project_registration_is_a_required_first_step():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/v2/projects":
            assert request.headers.get("Idempotency-Key")
            return httpx.Response(200, json={"id": "prj-1", "root": "/tmp/x"})
        return httpx.Response(404, json={
            "code": "project_not_registered", "message": "register the root first", "retryable": False,
        })

    with _gateway(handler) as gateway:
        assert gateway.register_project("/tmp/x") == "prj-1"
        with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
            gateway.start_run({"prompt": "hi"})
    assert excinfo.value.code == "project_not_registered"
    assert ("POST", "/v2/projects") in calls


def test_the_window_class_is_chosen_by_the_code_not_by_sniffing_the_context():
    """`quota` was never a Claudexor code. The classifier keys on the real one —
    `subscription_window_exhausted`, the RunFailureCode the engine actually emits — so
    an unrelated refusal carrying a stray reset-shaped key is not announced as a spent
    subscription window and put on a retry timer."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={
            "code": "subscription_window_exhausted", "message": "window spent", "retryable": True,
            "context": {"resetsAt": "2030-01-01T00:00:00Z"},
        })

    with _gateway(handler) as gateway:
        with pytest.raises(cx.ClaudexorSubscriptionWindowExhausted) as excinfo:
            gateway.get_run("run-1")
    assert excinfo.value.reset_at == "2030-01-01T00:00:00Z"

    def conflict(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={
            "code": "idempotency_conflict", "message": "same key, different body",
            "retryable": False, "context": {"cooldownUntil": "2030-01-01T00:00:00Z"},
        })

    with _gateway(conflict) as gateway:
        with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
            gateway.get_run("run-1")
    assert excinfo.value.code == "idempotency_conflict"
    assert not isinstance(excinfo.value, cx.ClaudexorSubscriptionWindowExhausted)


def test_an_unreachable_daemon_is_a_typed_refusal_not_a_crash():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _gateway(handler) as gateway:
        with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
            gateway.handshake()
    assert excinfo.value.code == "daemon_unreachable"


def test_the_daemon_token_is_never_returned_to_callers():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"snapshots": []})

    with _gateway(handler) as gateway:
        assert "secret-token" not in json.dumps(gateway.quota_snapshots())


# -- 3.4 the nanny verbs -------------------------------------------------------


def test_both_child_allowlists_can_see_the_nanny_verbs():
    assert NANNY_TOOLS <= LOCAL_READONLY_SUBAGENT_TOOL_NAMES
    assert NANNY_TOOLS <= ACTING_SUBAGENT_TOOL_NAMES


def test_there_is_no_hurry_verb():
    from ouroboros.tools import delegate

    names = {entry.name for entry in delegate.get_tools()}
    assert names == NANNY_TOOLS


def test_delegate_start_refuses_typed_when_no_route_is_configured(tmp_path, monkeypatch):
    from ouroboros.tools.delegate import _delegate_start
    from ouroboros.tools.registry import ToolContext

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    payload = json.loads(_delegate_start(ctx, "do a thing"))
    assert payload["status"] == "refused"
    assert payload["reason"] == "harness_not_configured"


# -- 3.6 accounting ------------------------------------------------------------


def test_a_subscription_session_settles_at_zero_and_keeps_the_projection_final(tmp_path):
    """A DISCLOSED zero is the free-session case: the money was spent when the plan was
    bought, so the row is final at 0.0 and the projection stays final.

    An UNDISCLOSED spend is not the same fact and must not be written as one. The engine's
    default auth preference is subscription-first with fallback to a paid key, and a route
    can bill by construction — settling those at a confident 0.0/final would hide real
    money from every budget fence while asserting the projection was complete.
    """
    from ouroboros.usage_accounting import record_subscription_session, usage_projection

    disclosed = tmp_path / "disclosed"
    record_subscription_session("s-free", drive_root=disclosed, route="r", task_id="t1",
                                root_task_id="t1", spend_usd=0.0)
    rows = [json.loads(l) for l in (disclosed / "state" / "usage_attempts.jsonl").read_text().splitlines()]
    row = next(r for r in rows if r.get("kind") == "subscription_session")
    assert row["cost_usd"] == 0.0 and row["cost_final"] is True
    assert usage_projection(disclosed)["cost_final"] is True

    charged = tmp_path / "charged"
    record_subscription_session("s-billed", drive_root=charged, route="r", task_id="t1",
                                root_task_id="t1", spend_usd=4.10)
    rows = [json.loads(l) for l in (charged / "state" / "usage_attempts.jsonl").read_text().splitlines()]
    row = next(r for r in rows if r.get("kind") == "subscription_session")
    assert row["cost_usd"] == 4.10, "a real charge must ride the ledger as money"
    assert row["cost_final"] is True

    unknown = tmp_path / "unknown"
    record_subscription_session("s-quiet", drive_root=unknown, route="r", task_id="t1",
                                root_task_id="t1")
    rows = [json.loads(l) for l in (unknown / "state" / "usage_attempts.jsonl").read_text().splitlines()]
    row = next(r for r in rows if r.get("kind") == "subscription_session")
    assert row["cost_final"] is False, "an undisclosed spend is not a proven zero"
    assert row["pricing_known"] is False
    # UNKNOWN must be None, not 0.0. A `cost_final=False` row costing 0.0 adds zero to
    # the projection's `estimated` total, and `not 0.0` is True — so the honest per-row
    # disclosure was invisible one layer up, which reported `cost_final: True` anyway.
    assert row["cost_usd"] is None
    projection = usage_projection(unknown)
    assert projection["cost_final"] is False, "an unknown session must drop finality"
    assert projection["unknown_unmetered"] == 1


def test_the_unmetered_external_row_would_have_dropped_cost_final(tmp_path):
    # The exact reason record_unmetered_external_dispatch must NOT be reused: one such
    # row makes the WHOLE projection non-final.
    ua.record_unmetered_external_dispatch("d1", drive_root=tmp_path, task_id="t1", root_task_id="t1")
    assert ua.usage_projection(tmp_path, root_task_id="t1")["cost_final"] is False


def test_a_session_is_not_counted_as_a_physical_provider_call(tmp_path):
    ua.record_subscription_session("run-2", drive_root=tmp_path, route="some-route", task_id="t2", root_task_id="t2")
    breakdown = ua.usage_breakdown(tmp_path, root_task_id="t2")
    assert breakdown["physical_calls"] == 0
    assert breakdown["subscription_sessions"] == 1


# -- 3.7 the failure class -----------------------------------------------------


def test_the_transport_error_code_is_the_failure_class_name():
    assert cx.ClaudexorSubscriptionWindowExhausted("x").code == SUBSCRIPTION_WINDOW_EXHAUSTED


def test_the_window_class_is_transient_and_scheduled_by_its_reset():
    exc = cx.ClaudexorSubscriptionWindowExhausted("spent", reset_at="2030-01-01T00:00:00Z")
    classification = classify_llm_exception(exc)
    assert classification.kind == SUBSCRIPTION_WINDOW_EXHAUSTED
    assert classification.kind != "quota_exhausted"
    assert classification.retry_same_request is True
    # Scheduled by the reset instant, never by the 60s-capped exponential backoff.
    assert classification.retry_after_sec is not None
    assert classification.retry_after_sec > 60.0
    assert classification.reset_at == "2030-01-01T00:00:00Z"


def test_a_billing_refusal_stays_permanently_classified():
    classification = classify_llm_exception(RuntimeError("402 payment required"))
    assert classification.kind == "quota_exhausted"
    assert classification.retry_same_request is False
    assert classification.retry_after_sec is None


# -- 4. the executor axis actually reaches dispatch -----------------------------


class _HealthStub:
    """A daemon that answers the manifest questions the rule table needs.

    `engine_version` is part of that answer, not decoration: the real gateway sets it
    at handshake and the mutating lane's floor reads it, so a stub without one models a
    daemon that never negotiated.
    """

    def __init__(self, *, status="ok", profiles=("readonly", "workspace_write"), reset_at="",
                 engine_version=CLAUDEXOR_DELEGATED_MARKER_MIN_VERSION):
        self.status, self.profiles, self.reset_at = status, profiles, reset_at
        self.engine_version = engine_version

    def handshake(self): return {}
    def agent_capabilities(self):
        return {"harnesses": [{
            "id": "some-route", "enabled": self.status == "ok", "status": self.status,
            "accessProfilesSupported": list(self.profiles),
        }]}

    def quota_snapshots(self):
        if not self.reset_at:
            return []
        return [{
            "subject": {"harness": "some-route"}, "freshness": "fresh",
            "constraints": [{"used_ratio": 1.0, "resets_at": self.reset_at}],
        }]

    def close(self): pass


def _dispatch(requested, *, route="some-route=weak:low", stub=None, monkeypatch=None,
              raises=None, acting=False):
    from ouroboros.gateways import claudexor as gw
    from ouroboros.subagents import dispatch_executor_resolution

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", route)

    def _make(*a, **k):
        if raises is not None:
            raise raises
        return stub if stub is not None else _HealthStub()

    monkeypatch.setattr(gw, "ClaudexorGateway", _make)
    task = {"delegation_role": "subagent", "requested_executor": requested}
    if acting:
        task["task_constraint"] = {"mode": "acting_subagent", "surface": "self_worktree"}
    return dispatch_executor_resolution(task)


def test_one_exhausted_credential_profile_does_not_take_the_harness_offline():
    """Defect D (D28): the readiness predicate reported a blocker as soon as ANY window
    of the harness was spent, so one exhausted account took the WHOLE harness offline
    while its siblings were live — an outage invented out of a healthy substrate, and
    the `harness` executor is a PIN, so the caller was refused rather than re-routed.

    Readiness is per SNAPSHOT now — the engine emits one per credential subject, so in
    practice one per account: the harness is usable while ANY of its snapshots is, and
    when they are all spent the instant reported is the EARLIEST, because the first to
    heal makes the harness usable again. The reader groups by `subject.harness` and
    deliberately never interprets `subject.subject_id`: WHICH profile a run lands on
    stays Claudexor's business, so no rotation moves into Ouroboros."""
    from ouroboros.subagents import _exhausted_window_reset_at

    def _snap(profile, *, spent, reset="2026-08-03T12:00:00Z", harness="some-route",
              freshness="fresh"):
        # `subject_id` is the REAL QuotaSubject key for a credential profile
        # (packages/schema/src/quota.ts; the object is `.strict()`, so the `profile`
        # this fixture used to invent would be rejected by the engine's own parser).
        constraint = ({"used_ratio": 1.0, "resets_at": reset} if spent
                      else {"used_ratio": 0.4, "resets_at": reset})
        return {"subject": {"harness": harness, "subject_id": profile},
                "freshness": freshness, "constraints": [constraint]}

    class _Quota:
        def __init__(self, snaps): self._snaps = snaps
        def quota_snapshots(self): return self._snaps

    # ONE of two profiles spent: the harness is still usable, so no blocker at all.
    mixed = _Quota([_snap("acct-a", spent=True, reset="2026-08-03T10:00:00Z"),
                    _snap("acct-b", spent=False)])
    assert _exhausted_window_reset_at(mixed, "some-route") == ""

    # ALL profiles spent: a blocker, at the EARLIEST reset (the first one to heal).
    both = _Quota([_snap("acct-a", spent=True, reset="2026-08-03T12:00:00Z"),
                   _snap("acct-b", spent=True, reset="2026-08-03T10:00:00Z")])
    assert _exhausted_window_reset_at(both, "some-route") == "2026-08-03T10:00:00Z"

    # A single-profile harness (no profile field at all) behaves exactly as before.
    single = _Quota([{"subject": {"harness": "some-route"}, "freshness": "fresh",
                      "constraints": [{"used_ratio": 1.0, "resets_at": "2026-08-03T09:00:00Z"}]}])
    assert _exhausted_window_reset_at(single, "some-route") == "2026-08-03T09:00:00Z"

    # Another harness's exhaustion is not ours, and a STALE snapshot never blocks.
    other = _Quota([_snap("acct-a", spent=True, harness="other-route")])
    assert _exhausted_window_reset_at(other, "some-route") == ""
    stale = _Quota([_snap("acct-a", spent=True, freshness="stale")])
    assert _exhausted_window_reset_at(stale, "some-route") == ""

    # And the live sibling wins even when the spent one is listed second.
    reordered = _Quota([_snap("acct-b", spent=False), _snap("acct-a", spent=True)])
    assert _exhausted_window_reset_at(reordered, "some-route") == ""


# One row of the rule table per case, resolved through the REAL dispatch entry point
# rather than through the pure function it wraps.
def test_dispatch_row_auto_without_a_route_runs_native(monkeypatch):
    res = _dispatch("auto", route="", monkeypatch=monkeypatch)
    assert (res.executor, res.reason) == ("native", "harness_not_configured")


def test_dispatch_row_auto_with_a_healthy_route_becomes_a_nanny(monkeypatch):
    res = _dispatch("auto", monkeypatch=monkeypatch)
    assert (res.executor, res.reason) == ("harness", "harness_ready")


def test_dispatch_row_auto_with_every_profile_spent_falls_back_to_the_api(monkeypatch):
    """D28 through the REAL dispatch entry point, with the disclosure it owes.

    Three destinations (p2's `capability_delta` chain composed with this at
    synthesis): the durable `subagent_executor_resolved` row the dispatch emits, the
    child's own prompt note, and the parent-facing envelope's
    `effective_executor` / `capability_delta`."""
    from ouroboros.agent import dispatch_executor_note, resolve_dispatch_axes

    res = _dispatch("auto", stub=_HealthStub(reset_at="2030-01-01T00:00:00Z"), monkeypatch=monkeypatch)
    assert res.executor == "native" and not res.blocked
    assert res.reason == SUBSCRIPTION_WINDOW_EXHAUSTED
    assert res.reset_at == "2030-01-01T00:00:00Z"

    # Destination 2: the child is told it fell back, that the money is real, and when
    # the substrate would have healed — it must not discover any of that by spending.
    note = dispatch_executor_note(res)
    assert "CAPABILITY DELTA" in note and "METERED" in note
    assert "2030-01-01T00:00:00Z" in note

    # Destination 3: the parent reads what actually ran, and that it diverged —
    # through the REAL resolution seam, not a hand-built envelope: the dispatch
    # stamps the record and rebuilds the envelope from it (one writer).
    task = {"id": "t-child", "type": "task", "delegation_role": "subagent",
            "requested_executor": "auto"}
    resolve_dispatch_axes(task)
    envelope = task["subagent_envelope"]
    assert envelope["executor"] == "auto"
    assert envelope["effective_executor"] == "native"
    assert envelope["capability_delta"]["reason"] == SUBSCRIPTION_WINDOW_EXHAUSTED
    assert envelope["capability_delta"]["reduced"] is True

    # And the PIN keeps the opposite answer: it exists to refuse metered spend.
    pinned = _dispatch("harness", stub=_HealthStub(reset_at="2030-01-01T00:00:00Z"),
                       monkeypatch=monkeypatch)
    assert pinned.blocked and pinned.reason == SUBSCRIPTION_WINDOW_EXHAUSTED


def test_dispatch_row_auto_with_an_unavailable_route_runs_native_with_a_visible_marker(monkeypatch):
    from ouroboros.agent import dispatch_executor_note

    res = _dispatch("auto", raises=cx.ClaudexorUnavailable("daemon_unreachable", "no daemon"),
                    monkeypatch=monkeypatch)
    assert (res.executor, res.reason) == ("native", "daemon_unreachable")
    # "Visible" is the whole point of this row: the child must not discover the
    # fallback by spending.
    note = dispatch_executor_note(res)
    assert "METERED" in note and "daemon_unreachable" in note


def test_dispatch_row_explicit_harness_blocks_and_never_reaches_the_native_path(monkeypatch):
    for stub, raises in (
        (_HealthStub(status="unavailable"), None),
        (None, cx.ClaudexorUnavailable("daemon_unreachable", "no daemon")),
    ):
        res = _dispatch("harness", stub=stub, raises=raises, monkeypatch=monkeypatch)
        # The regression this exists for: a pin that silently becomes a metered native
        # run bills the owner for precisely what the pin was asked to prevent.
        assert res.executor != "native", res
        assert res.blocked, res
    res = _dispatch("harness", route="", monkeypatch=monkeypatch)
    assert res.blocked and res.reason == "harness_not_configured"


def test_dispatch_row_native_is_native_and_asks_the_daemon_nothing(monkeypatch):
    from ouroboros.gateways import claudexor as gw
    from ouroboros.subagents import dispatch_executor_resolution

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route")

    def _boom(*a, **k):
        raise AssertionError("a native request must not touch the daemon")

    monkeypatch.setattr(gw, "ClaudexorGateway", _boom)
    res = dispatch_executor_resolution({"delegation_role": "subagent", "requested_executor": "native"})
    assert (res.executor, res.reason) == ("native", "requested_native")


def test_a_blocked_pin_ends_the_task_unrun_instead_of_spending(monkeypatch):
    from ouroboros.agent import executor_blocked_outcome

    res = _dispatch("harness", raises=cx.ClaudexorUnavailable("daemon_unreachable", "x"),
                    monkeypatch=monkeypatch)
    text, usage = executor_blocked_outcome(res)
    assert usage == {"execution_status": "infra_failed",
                     "reason_code": "subagent_executor_unavailable"}
    assert "NOT run on metered API tokens" in text
    # No visible marker for a blocked run: there is no child to inform.
    from ouroboros.agent import dispatch_executor_note
    assert dispatch_executor_note(res) == ""


def test_a_plain_task_is_not_subject_to_the_executor_axis(monkeypatch):
    """The guard lives at the PRODUCTION entry point, `agent.resolve_dispatch_axes`:
    a task with no `delegation_role: subagent` resolves no axes at all and never
    reaches the daemon. (There used to be a second, test-only wrapper in `agent.py`
    carrying its own copy of this guard while production went through
    `resolve_subagent_dispatch`; the guard is pinned where it actually runs.)"""
    from ouroboros.agent import resolve_dispatch_axes
    from ouroboros.gateways import claudexor as gw

    def _boom(*a, **k):
        raise AssertionError("a plain task must not touch the daemon")

    monkeypatch.setattr(gw, "ClaudexorGateway", _boom)
    task = {"type": "improvement"}
    assert resolve_dispatch_axes(task) is None
    assert "effective_executor" not in task


def test_an_acting_child_is_health_checked_against_the_profile_it_will_ask_for(monkeypatch):
    # A route that can only read is not a usable substrate for a child that must write.
    res = _dispatch("harness", stub=_HealthStub(profiles=("readonly",)),
                    monkeypatch=monkeypatch, acting=True)
    assert res.blocked and res.reason == "access_profile_unsupported:workspace_write"
    res = _dispatch("harness", stub=_HealthStub(profiles=("readonly",)), monkeypatch=monkeypatch)
    assert res.executor == "harness"


def test_a_route_that_declares_only_the_confined_profile_is_admitted_not_refused(monkeypatch):
    """Ouroboros must not refuse the run Claudexor would admit.

    A delegated run is externally confined, so the engine rewrites `workspace_write` to
    `external_sandbox_full` before it checks the manifest — and a route whose adapter
    stands its own sandbox down in favour of that boundary declares only the confined
    profile. `opencode` is exactly that route (`["full", "external_sandbox_full",
    "inherit_native"]`, given the profile so a delegated mutating run on macOS could
    exist at all). Comparing the literal blocked a pinned `harness` executor outright
    and dropped `auto` to a metered native child for no reason on either side.
    """
    opencode = ("full", "external_sandbox_full", "inherit_native")
    res = _dispatch("harness", stub=_HealthStub(profiles=opencode),
                    monkeypatch=monkeypatch, acting=True)
    assert res.executor == "harness" and not res.blocked
    # The fallback is the DELEGATED run's alone: a read-only child asks for `readonly`,
    # the engine leaves it `readonly`, and opencode really cannot serve it.
    res = _dispatch("harness", stub=_HealthStub(profiles=opencode), monkeypatch=monkeypatch)
    assert res.blocked and res.reason == "access_profile_unsupported:readonly"
    # And a route with neither profile still refuses the acting child.
    res = _dispatch("harness", stub=_HealthStub(profiles=("readonly", "inherit_native")),
                    monkeypatch=monkeypatch, acting=True)
    assert res.blocked and res.reason == "access_profile_unsupported:workspace_write"


def test_a_stale_unknown_executor_value_degrades_to_auto_not_to_a_crash(monkeypatch):
    res = _dispatch("a-value-from-an-older-build", monkeypatch=monkeypatch)
    assert res.executor == "harness" and res.requested == "auto"


# -- 4. mutating AND read-only children, one nanny, one transport ---------------


def _delegating_ctx(tmp_path, *, acting: bool):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tools.registry import ToolContext

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    # An acting child's WRITE ROOT is its own worktree, and the run root must equal the
    # write_root the constraint granted — not whatever `active_repo_dir` happens to
    # resolve to. Before v6.87.30 this fixture had no workspace at all and asserted
    # `scope.root == repo_dir`, i.e. it pinned "hand an external shell the live Ouroboros
    # tree" as the correct shape.
    # The worktree must live OUTSIDE the data drive: an overlap is exactly what
    # `workspace_mode_block_reason` refuses, and the refusal is correct.
    worktree = tmp_path.parent / f"wt-{tmp_path.name}"
    worktree.mkdir(exist_ok=True)
    constraint = TaskConstraint(
        mode="acting_subagent" if acting else "local_readonly_subagent",
        surface="self_worktree" if acting else "",
        write_root=str(worktree) if acting else "",
    )
    ctx = ToolContext(repo_dir=repo, drive_root=tmp_path, task_constraint=constraint)
    if acting:
        ctx.workspace_root = str(worktree)
        ctx.workspace_mode = "self_worktree"
    ctx.task_id = "t-nanny"
    ctx.task_metadata = {"root_task_id": "t-root", "parent_task_id": "t-root"}
    return ctx


def _started_request(tmp_path, *, acting: bool, monkeypatch,
                     engine_version=CLAUDEXOR_DELEGATED_MARKER_MIN_VERSION, expect="started"):
    """Run _delegate_start against a stubbed gateway and return the wire request."""
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    seen = {}

    class _Stub:
        engine_version = ""

        def handshake(self): return {}
        def agent_capabilities(self):
            return {"harnesses": [{
                "id": "some-route", "enabled": True, "status": "ok",
                "accessProfilesSupported": ["readonly", "workspace_write"],
            }]}
        def quota_snapshots(self): return []
        def find_project_id(self, root): return "prj-existing"
        def register_project(self, root): raise AssertionError("must reuse the registration")
        def start_run(self, request, *, idempotency_key=""):
            seen["request"] = request
            return {"runId": "run-1", "runDir": "/tmp/run-1"}
        def close(self): pass

    _Stub.engine_version = engine_version
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    delegate._CUSTODY.clear()
    payload = json.loads(delegate._delegate_start(_delegating_ctx(tmp_path, acting=acting), "edit the README"))
    delegate._CUSTODY.clear()
    assert payload["status"] == expect, payload
    return seen.get("request"), payload


def test_a_mutating_child_runs_live_in_the_nannys_own_worktree(tmp_path, monkeypatch):
    # `live` is what makes the EXISTING workspace-patch capture see the harness's edits:
    # the harness writes the nanny's own tree, so no new patch plumbing exists at all.
    request, payload = _started_request(tmp_path, acting=True, monkeypatch=monkeypatch)
    assert request["access"] == "workspace_write"
    assert request["mode"] == "agent"
    assert request["execution"] == {"isolation": "live", "delegated": True}
    assert request["scope"] == {"kind": "project", "root": str(tmp_path.parent / f"wt-{tmp_path.name}")}
    assert payload["access"] == "workspace_write" and payload["isolation"] == "live"


def test_a_read_only_child_uses_the_same_transport_with_a_narrower_profile(tmp_path, monkeypatch):
    # One nanny, one transport: the ONLY difference is the derived profile and the run
    # shape it implies. `execution.isolation='live'` is agent-only in Claudexor — a
    # non-agent run carrying it is refused at the boundary — and a read-only child has
    # nothing to write back anyway.
    request, payload = _started_request(tmp_path, acting=False, monkeypatch=monkeypatch)
    assert request["access"] == "readonly"
    assert request["mode"] == "ask"
    assert "execution" not in request
    assert payload["access"] == "readonly"


def test_the_host_states_its_prohibitions_on_every_delegated_run(tmp_path, monkeypatch):
    request, _ = _started_request(tmp_path, acting=True, monkeypatch=monkeypatch)
    instructions = request["instructions"].lower()
    assert "git commit" in instructions and "outside this root" in instructions


def test_the_model_has_no_argument_that_could_widen_the_profile():
    from ouroboros.tools import delegate

    entry = next(e for e in delegate.get_tools() if e.name == "delegate_start")
    properties = set(entry.schema["parameters"]["properties"])
    # `retry_of` names an INVOCATION, not authority: the retry path checks ownership
    # (the requesting task's id on the durable row) and replays a body that was derived
    # from the same task's own authority, so it can rename nothing and widen nothing.
    assert properties == {"prompt", "max_seconds", "retry_of"}
    # Nothing in the schema names authority: no access, mode, isolation, root or scope.
    assert not properties & {"access", "mode", "isolation", "root", "scope", "write_surface"}


def test_a_read_only_task_cannot_obtain_workspace_write(tmp_path):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tools.delegate import _derive_authority
    from ouroboros.tools.registry import ToolContext

    for constraint in (
        None,                                                       # no constraint at all
        TaskConstraint(mode="local_readonly_subagent"),             # explicitly read-only
        TaskConstraint(mode="acting_subagent", surface=""),         # acting but unresolved surface
        TaskConstraint(mode="acting_subagent", surface="bogus"),    # acting with an invalid surface
    ):
        ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_constraint=constraint)
        ctx.task_metadata = {"parent_task_id": "p"}
        authority = _derive_authority(ctx)
        assert authority.access == "readonly", constraint
        assert authority.mode == "ask" and authority.isolation == ""


@pytest.mark.parametrize("effective,entitled,widened", [
    ("readonly", "readonly", ""),
    ("readonly", "workspace_write", ""),          # narrower than asked is fine
    ("workspace_write", "workspace_write", ""),
    ("workspace_write", "readonly", "workspace_write"),
    ("full", "workspace_write", "full"),
    ("inherit_native", "workspace_write", "inherit_native"),
    ("a-profile-from-a-future-engine", "workspace_write", "a-profile-from-a-future-engine"),
])
def test_effective_access_is_verified_not_assumed(effective, entitled, widened):
    from ouroboros.tools.delegate import _widened_access

    detail = {"lastSeq": 12, "summary": {"effectiveAccess": effective, "state": "running"}}
    assert _widened_access(detail, entitled) == widened


def test_an_undisclosed_effective_profile_is_unverified_not_compliant():
    """Absence of evidence is not evidence of narrowness.

    An earlier version returned "" (compliant) whenever the field was missing, and a test
    codified that as `# not disclosed yet: nothing to judge` — so any daemon build, harness
    or malformed response that omitted the field turned the only containment gate into a
    silent no-op while the run kept writing. It also fell back to `summary["access"]`,
    which the daemon computes as `effectiveAccess ?? the client's own request`: that
    compares our request against itself and can only ever pass.
    """
    from ouroboros.tools.delegate import _ACCESS_UNVERIFIED, _widened_access

    # Before admission there really is nothing to judge.
    assert _widened_access({"summary": {"state": "queued"}}, "readonly") == ""
    assert _widened_access({"summary": {}}, "readonly") == ""

    # Absence only means "no evidence" while the run can still ACT, and only after it
    # has produced anything. The daemon marks a run `running` at DEQUEUE — before the
    # orchestrator writes the contract the profile is derived from — so judging that
    # moment cancelled healthy runs, and judging a terminal state reported a run that
    # merely failed to start as a containment breach.
    assert _widened_access({"lastSeq": 0, "summary": {"state": "running"}}, "readonly") == ""
    for state in ("succeeded", "failed", "cancelled", "interrupted"):
        detail = {"lastSeq": 40, "summary": {"state": state}}
        assert _widened_access(detail, "readonly") == "", state

    # A live run that HAS produced events and still discloses nothing has no evidence.
    live = {"lastSeq": 12, "summary": {"state": "running"}}
    assert _widened_access(live, "readonly") == _ACCESS_UNVERIFIED

    # The echo must not be accepted as an independent witness.
    detail = {"lastSeq": 12, "summary": {"state": "running", "access": "workspace_write"}}
    assert _widened_access(detail, "workspace_write") == _ACCESS_UNVERIFIED

    # A really widened profile is still caught in every state.
    for state in ("running", "succeeded"):
        detail = {"lastSeq": 12, "summary": {"state": state, "effectiveAccess": "full"}}
        assert _widened_access(detail, "readonly") == "full", state


def test_a_succeeded_run_that_never_proved_its_profile_says_so_in_its_result():
    """P34P1.4: a SUCCEEDED run whose summary carries no `effectiveAccess` was accepted
    as compliant — a result with no evidence that the profile the host asked for is the
    profile the engine enforced, which is the name-without-proof class this module
    exists to refuse.

    Enforcement is NOT the answer for a finished run: it is over, there is nothing left
    to contain, and routing absence through the breach path would CANCEL a succeeded run
    and destroy the very result the lane exists to fetch (the v6.87.37 lesson — the
    containment gate stopped cancelling healthy runs for exactly this reason). So it is
    DISCLOSED, on the same terminal payload the parent reads, like the HOME half's
    missing fact. Both lanes get it: `readonly` staying `readonly` is the profile that
    matters most, and the `containment` block is asked only of marker-carrying runs."""
    from ouroboros.subagents import delegated_run_shape
    from ouroboros.tools.delegate import _terminal_payload

    # A succeeded run with NO disclosed profile: unverified, and it says why.
    silent = {"lastSeq": 40, "summary": {"state": "succeeded"}}
    evidence = _terminal_payload("run-1", silent, delegated_run_shape(False))["access_evidence"]
    assert evidence["verified"] is False and evidence["effective"] == ""
    assert evidence["requested"] == "readonly" and evidence["state"] == "succeeded"
    assert "SUCCEEDED without ever disclosing" in evidence["note"]

    # A succeeded run that DID disclose one is verified, with no note.
    proven = {"lastSeq": 40, "summary": {"state": "succeeded", "effectiveAccess": "readonly"}}
    evidence = _terminal_payload("run-1", proven, delegated_run_shape(False))["access_evidence"]
    assert evidence == {"requested": "readonly", "effective": "readonly",
                        "verified": True, "state": "succeeded"}

    # A run that did NOT succeed keeps the softer wording: it may never have had a
    # profile at all, so this is absence of evidence rather than a missing proof.
    for state in ("failed", "cancelled", "interrupted"):
        detail = {"lastSeq": 40, "summary": {"state": state}}
        evidence = _terminal_payload("run-1", detail, delegated_run_shape(False))["access_evidence"]
        assert evidence["verified"] is False, state
        assert "absence of evidence, not a breach" in evidence["note"], state

    # The ECHO is never a witness: the daemon computes `access` as
    # `effectiveAccess ?? our own request`, so a payload carrying only the echo must
    # still read unverified.
    echo = {"lastSeq": 40, "summary": {"state": "succeeded", "access": "readonly"}}
    assert _terminal_payload("run-1", echo, delegated_run_shape(False))[
        "access_evidence"]["verified"] is False

    # The mutating lane carries BOTH halves, and neither displaces the other.
    mutating = _terminal_payload("run-1", silent, delegated_run_shape(True))
    assert mutating["access_evidence"]["verified"] is False
    assert mutating["containment"]["verified"] is False


def test_a_mutating_run_requires_an_ACTIVE_workspace_not_merely_agreement(tmp_path):
    """Agreement alone reopened the critical it was written to close.

    `active_repo_dir_for` falls back to `repo_dir` when workspace mode is off, so a
    constraint whose `write_root` happens to name that same directory made the equality
    check pass — and handed an external shell the live repository, which is exactly the
    original defect.
    """
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.subagents import delegated_run_shape
    from ouroboros.tools.delegate import _mutating_run_root
    from ouroboros.tools.registry import ToolContext

    repo = tmp_path / "repo"
    repo.mkdir()
    ctx = ToolContext(
        repo_dir=repo, drive_root=tmp_path,
        task_constraint=TaskConstraint(mode="acting_subagent", surface="self_worktree",
                                       write_root=str(repo)),
    )
    ctx.workspace_root = None
    ctx.workspace_mode = ""
    root, refusal = _mutating_run_root(
        ctx, delegated_run_shape(True))
    assert refusal and "workspace_not_active" in refusal, refusal
    assert root == ""


def test_a_widened_run_is_cancelled_and_typed_not_reported_as_progress(tmp_path, monkeypatch):
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    cancelled = {}

    class _Stub:
        def handshake(self): return {}
        def get_run(self, rid):
            return {"lastSeq": 7, "summary": {
                "state": "cancelled" if cancelled else "running",
                "effectiveAccess": "full",
            }}
        def cancel_run(self, rid, reason=""):
            cancelled["reason"] = reason
            return {"accepted": True}
        def remove_project(self, pid): pass
        def close(self): pass

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    ctx = _delegating_ctx(tmp_path, acting=True)
    delegate._CUSTODY.clear()
    delegate._CUSTODY["run-1"] = delegate._RunCustody(
        task_id="t-nanny", route_id="some-route", model="m",
        project_id="prj", project_owned=False,
    )
    out = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))
    delegate._CUSTODY.clear()
    assert out["status"] == "refused"
    assert out["reason"] == "access_profile_widened"
    assert out["effective_access"] == "full" and out["entitled_access"] == "workspace_write"
    assert cancelled["reason"] == "access_profile_widened"


def test_a_delegated_run_can_only_be_touched_by_the_task_that_started_it(tmp_path):
    """The daemon bearer token grants the ENTIRE Claudexor API, so naming a run is
    reaching it. Without custody binding, a child could pass any run id it observed and
    read — or CANCEL — the owner's own unrelated work, or a sibling reviewer's run, and
    cancelling a reviewer destroys the verdict that was the whole point of running it."""
    import json

    import ouroboros.tools.delegate as delegate
    from ouroboros.tools.registry import ToolContext

    def _ctx(task_id):
        ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
        ctx.task_id = task_id
        ctx.task_metadata = {"root_task_id": task_id}
        return ctx

    delegate._CUSTODY.clear()
    delegate._CUSTODY["run-mine"] = delegate._RunCustody(
        task_id="task-a", route_id="codex", model="m", project_id="prj", project_owned=False,
    )

    for tool, call in (
        ("delegate_wait", lambda ctx, rid: delegate._delegate_wait(ctx, rid, wait_sec=1)),
        ("delegate_cancel", lambda ctx, rid: delegate._delegate_cancel(ctx, rid, reason="x")),
    ):
        # A run with NO durable start record anywhere: ownership is UNKNOWN, which is a
        # different fact from "demonstrably someone else's" and is refused on its own name.
        out = json.loads(call(_ctx("task-a"), "run-someone-elses"))
        assert out["status"] == "refused", (tool, out)
        assert out["reason"] == "run_ownership_unknown", (tool, out)

        # A run a SIBLING task started in the same worker process.
        out = json.loads(call(_ctx("task-b"), "run-mine"))
        assert out["status"] == "refused", (tool, out)
        assert out["reason"] == "run_not_owned", (tool, out)

    delegate._CUSTODY.clear()


def test_a_mutating_run_is_refused_when_the_root_and_the_granted_write_root_disagree(tmp_path, monkeypatch):
    """AUTHORITY and ROOT came from two different predicates and were never compared.

    Authority comes from `task_constraint` via `active_tool_profile`. The root came from
    `active_repo_dir_for`, and `ToolContext.active_repo_dir()` falls back to `repo_dir` —
    the LIVE Ouroboros source tree — whenever `is_workspace_mode()` is false, which
    `workspace_mode_block_reason` makes happen for a worktree overlapping the repo or the
    data drive, or for a task record missing its workspace fields. In that state the host
    would have handed an external SHELL `workspace_write` on its own repository, and no
    per-tool guard applies because a shell is not a tool. Two independent reviewers found
    this on the same branch.
    """
    import json

    import ouroboros.tools.delegate as delegate
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.gateways import claudexor as gw
    from ouroboros.tools.registry import ToolContext

    class _Stub:
        engine_version = CLAUDEXOR_DELEGATED_MARKER_MIN_VERSION

        def handshake(self): return {}
        def agent_capabilities(self):
            return {"harnesses": [{"id": "some-route", "enabled": True, "status": "ok",
                                   "accessProfilesSupported": ["readonly", "workspace_write"]}]}
        def quota_snapshots(self): return []
        def start_run(self, request, *, idempotency_key=""):
            raise AssertionError("must refuse before starting")
        def close(self): pass

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())

    repo = tmp_path / "repo"
    repo.mkdir()
    inside_the_drive = tmp_path / "wt"
    inside_the_drive.mkdir()

    ctx = ToolContext(
        repo_dir=repo, drive_root=tmp_path,
        task_constraint=TaskConstraint(
            mode="acting_subagent", surface="self_worktree",
            write_root=str(inside_the_drive),
        ),
    )
    ctx.task_id = "t-nanny"
    ctx.task_metadata = {"root_task_id": "t-root"}
    ctx.workspace_root = str(inside_the_drive)
    ctx.workspace_mode = "self_worktree"

    out = json.loads(delegate._delegate_start(ctx, "edit the README"))
    assert out["status"] == "refused", out
    # A worktree overlapping the data drive is refused as "not an active workspace" —
    # `workspace_mode_block_reason` fires first and is the stronger statement.
    assert out["reason"] in ("write_root_mismatch", "workspace_not_active"), out

    # And a mutating child whose constraint granted no write_root at all is refused too,
    # rather than the host picking a directory on its behalf.
    ctx.task_constraint = TaskConstraint(mode="acting_subagent", surface="self_worktree")
    out = json.loads(delegate._delegate_start(ctx, "edit the README"))
    assert out["status"] == "refused", out
    assert out["reason"] in ("write_root_missing", "workspace_not_active"), out


def test_the_guards_that_protect_a_delegated_run_fail_closed(tmp_path, monkeypatch):
    """Three guards that each failed OPEN in exactly the case they existed for."""
    import ouroboros.tools.delegate as delegate
    from ouroboros.tools.registry import ToolContext

    # 1. Custody with an unknown identity on either side is refused, not waved through.
    delegate._CUSTODY.clear()
    delegate._CUSTODY["run-x"] = delegate._RunCustody(
        task_id="", route_id="r", model="m", project_id="p", project_owned=False)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "t-a"
    ctx.task_metadata = {"root_task_id": "t-a"}
    assert json.loads(delegate._delegate_cancel(ctx, "run-x"))["reason"] == "run_not_owned"
    ctx.task_id = ""
    delegate._CUSTODY["run-x"] = delegate._RunCustody(
        task_id="t-a", route_id="r", model="m", project_id="p", project_owned=False)
    assert json.loads(delegate._delegate_cancel(ctx, "run-x"))["reason"] == "run_not_owned"
    delegate._CUSTODY.clear()

    # 2. A run with no knowable deadline gets a conservative cap, never an omitted one:
    #    an omitted cap is Claudexor's 7-day schema bound on a run nobody can cancel.
    bare = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    bare.task_id = "t-a"
    bare.task_metadata = {"root_task_id": "t-a"}          # no deadline_at at all
    # The cap is the EXISTING task ceiling SSOT, not a second hardcoded one: a 1h guess
    # would have truncated a headless/benchmark run that legitimately has no deadline.
    from ouroboros.config import get_task_abs_ceiling_sec

    assert delegate._bounded_max_seconds(bare, None) == int(get_task_abs_ceiling_sec())

    # ...but never past Claudexor's own schema bound. The task ceiling clamps only from
    # BELOW, so an owner who raises it past a week would make every deadline-less start
    # send an out-of-schema value and get a 400 instead of a run.
    monkeypatch.setenv("OUROBOROS_TASK_ABS_CEILING_SEC", "1000000")
    assert delegate._bounded_max_seconds(bare, None) == delegate._CLAUDEXOR_MAX_SECONDS

    # ...and an EXPLICIT ask is clamped by the same bound. `max_seconds` is a
    # model-supplied tool argument with no maximum in its schema, so clamping only the
    # fallback branch left the ask itself able to sail past it — the same defect, one
    # branch over from the one that was fixed.
    assert delegate._bounded_max_seconds(bare, 1_000_000) == delegate._CLAUDEXOR_MAX_SECONDS
    assert delegate._bounded_max_seconds(bare, 120) == 120
    # An explicit narrower ask still wins — the cap is a floor for the unknown case only.
    assert delegate._bounded_max_seconds(bare, 120) == 120

    # 3. P34P1.8: an EXPIRED deadline is NOT the same fact as having none.
    #    `deadline_remaining_sec` answers 0.0 for both, so the fallback above handed an
    #    already-expired nanny the absolute task ceiling — hours of delegated work, and
    #    real quota, beginning after the instant its own deadline demanded it stop.
    monkeypatch.delenv("OUROBOROS_TASK_ABS_CEILING_SEC", raising=False)
    expired = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    expired.task_id = "t-a"
    expired.task_metadata = {"root_task_id": "t-a", "deadline_at": "2020-01-01T00:00:00Z"}
    assert delegate._deadline_expired(expired) is True
    assert delegate._deadline_expired(bare) is False, "no deadline is not an expired one"

    from ouroboros.deadline_utils import utc_now

    live = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    live.task_id = "t-a"
    live.task_metadata = {"root_task_id": "t-a",
                          "deadline_at": (utc_now() + datetime.timedelta(hours=1)).isoformat()}
    assert delegate._deadline_expired(live) is False
    # ...and the live deadline still NARROWS the bound, as it always did.
    assert 0 < delegate._bounded_max_seconds(live, None) <= 3600

    # The refusal is at the START, before the daemon is touched: nothing spent, nothing
    # registered, and the reason names the honest next move.
    reached = []
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")

    class _NeverReached:
        def handshake(self): reached.append("handshake"); return {}
        def close(self): pass

    from ouroboros.gateways import claudexor as _gw

    monkeypatch.setattr(_gw, "ClaudexorGateway", lambda *a, **k: _NeverReached())
    refused = json.loads(delegate._delegate_start(expired, "start something new"))
    assert refused["status"] == "refused" and refused["reason"] == "task_deadline_expired"
    assert reached == [], "an expired nanny must not even reach the daemon"


def test_the_agent_facing_cost_tells_the_same_story_as_the_ledger(tmp_path, monkeypatch):
    """`_terminal_payload` is what the nanny RELAYS to its parent, so it must not
    contradict the row. It used to hardcode `$0.00 / final` — the exact shape the
    settlement fix exists to eliminate — so a billed run settled honestly in the ledger
    and then told the reasoning path the work was free.

    This drives the real transport: a stubbed gateway returns a terminal detail carrying
    a spend, and the assertion is on what `delegate_wait` actually returned.
    """
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw
    from ouroboros.tools.registry import ToolContext

    def _wait_with_spend(spend_field):
        class _Stub:
            def handshake(self): return {}
            def get_run(self, rid):
                return {"lastSeq": 9, "summary": {"state": "succeeded", **spend_field}}
            def close(self): pass

        monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
        delegate._CUSTODY.clear()
        delegate._CUSTODY["run-1"] = delegate._RunCustody(
            task_id="t-a", route_id="r", model="m", project_id="p", project_owned=False)
        ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
        ctx.task_id = "t-a"
        ctx.task_metadata = {"root_task_id": "t-a"}
        out = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))
        delegate._CUSTODY.clear()
        return out

    billed = _wait_with_spend({"spendUsd": 4.10})["cost"]
    assert billed["cost_usd"] == 4.10, "a billed run must not be relayed as free"
    assert "BILLED" in billed["note"]

    undisclosed = _wait_with_spend({})["cost"]
    assert undisclosed["cost_usd"] is None and undisclosed["cost_final"] is False

    free = _wait_with_spend({"spendUsd": 0.0})["cost"]
    assert free["cost_usd"] == 0.0 and free["cost_final"] is True


def test_settlement_reads_the_harnesss_own_spend_field(tmp_path, monkeypatch):
    """Drives `_settle` through the real transport instead of calling the recorder.

    The round-1 test for this called `record_subscription_session(spend_usd=4.10)`
    directly — it constructed the very value it asserted and never entered `_settle`, so
    renaming the wire field to `totallyWrongFieldName` left the suite green. This one
    reads the ledger row that a delegated run actually produced.
    """
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw
    from ouroboros.tools.registry import ToolContext

    retired = []

    class _Stub:
        def handshake(self): return {}
        def get_run(self, rid):
            return {"lastSeq": 9, "summary": {"state": "succeeded", "spendUsd": 4.10,
                                              "inputTokens": 10, "outputTokens": 5}}
        def remove_project(self, pid): retired.append(pid)
        def close(self): pass

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    delegate._CUSTODY.clear()
    delegate._CUSTODY["run-1"] = delegate._RunCustody(
        task_id="t-a", route_id="r", model="m", project_id="prj-ours", project_owned=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "t-a"
    ctx.task_metadata = {"root_task_id": "t-a"}

    json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))
    delegate._CUSTODY.clear()

    rows = [json.loads(line) for line
            in (tmp_path / "state" / "usage_attempts.jsonl").read_text().splitlines()]
    row = next(r for r in rows if r.get("kind") == "subscription_session")
    assert row["cost_usd"] == 4.10, "the harness's reported spend must reach the ledger"
    assert row["cost_final"] is True
    assert retired == ["prj-ours"], "a registration we created is retired on settle"


def test_d29_applied_credential_profile_reaches_the_durable_record(tmp_path, monkeypatch):
    """D29: the APPLIED credential-profile id + access profile the engine's
    authRoute receipt discloses must land in the durable ledger row AND the
    settled event by default — 'which account paid' answered from the record."""
    payload, row, event = _settled_run(tmp_path, monkeypatch, {
        "state": "succeeded", "spendUsd": 2.5,
        "authRoute": {"profileId": "koshak", "requested": "subscription"},
        "effectiveAccess": "readonly",
    })
    assert row["credential_profile_id"] == "koshak"
    assert row["access_profile"] == "readonly"
    assert event["credential_profile_id"] == "koshak"
    assert event["access_profile"] == "readonly"


def test_d29_absent_authroute_records_empty_never_invented(tmp_path, monkeypatch):
    """Telemetry that predates the receipt records an empty applied profile —
    the fact is disclosed as unknown, never fabricated."""
    _payload, row, event = _settled_run(tmp_path, monkeypatch, {
        "state": "succeeded", "spendUsd": 0.0})
    assert row["credential_profile_id"] == ""
    assert event["credential_profile_id"] == ""


def test_the_durable_access_profile_is_the_receipt_never_our_own_request(tmp_path, monkeypatch):
    """The daemon computes `access` as `effectiveAccess ?? the client's own parsed
    request`, so it is our ask reflected back, not a witness. Reading it as a fallback
    wrote the REQUEST into a durable column that promises applied facts."""
    _payload, row, event = _settled_run(tmp_path, monkeypatch, {
        "state": "succeeded", "spendUsd": 0.0, "access": "workspace_write"})
    assert row["access_profile"] == ""
    assert event["access_profile"] == ""


def _settled_run(tmp_path, monkeypatch, summary):
    """Drive a real `_settle` for `summary`; return (agent payload, ledger row, envelope).

    The `delegate_run_settled` envelope is returned too because it RE-DERIVES the row's
    finality instead of being handed it, so the only thing keeping the two from drifting
    is a test that reads both from the same run.
    """
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw
    from ouroboros.tools.registry import ToolContext

    class _Stub:
        def handshake(self): return {}
        def get_run(self, rid): return {"lastSeq": 9, "summary": dict(summary)}
        def remove_project(self, pid): pass
        def close(self): pass

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    delegate._CUSTODY.clear()
    delegate._CUSTODY["run-1"] = delegate._RunCustody(
        task_id="t-a", route_id="r", model="m", project_id="p", project_owned=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "t-a"
    ctx.task_metadata = {"root_task_id": "t-a"}
    payload = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))
    delegate._CUSTODY.clear()
    rows = [json.loads(line) for line
            in (tmp_path / "state" / "usage_attempts.jsonl").read_text().splitlines()]
    events = [json.loads(line) for line
              in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    return (payload,
            next(r for r in rows if r.get("kind") == "subscription_session"),
            next(e for e in events if e.get("type") == "delegate_run_settled"))


def test_an_estimated_spend_is_not_a_settled_one(tmp_path, monkeypatch):
    """`spendUsd` is half the disclosure; `spendEstimated` is the other half.

    The engine really populates it (`packages/schema/src/control.ts`: "True when settled
    cash is estimated rather than exact"), and 8 of 60 live `/v2/runs` rows carried it —
    all of them as an estimated ZERO, which is the trap: reading the amount alone wrote a
    charge nobody had settled into the ledger as `cost_final=True` and relayed it to the
    agent as an already-paid subscription session, final.

    All three surfaces are asserted, because the defect this replaces was a fix that
    landed on one of them. The estimated-ZERO case in particular is what proves the
    projection: an estimated $0.00 adds nothing to `estimated_usd`, so a finality test
    that sums dollars instead of counting rows keeps reporting `cost_final: True`.

    Both AMOUNTS are asserted, because the harm this commit names is an estimated CHARGE
    written as already-paid. Testing only the estimated zero left the fix scoped to it:
    `if estimated and spend == 0`, `not (spend_estimated and spend_usd == 0.0)` and an
    `estimated_rows` that only counts free rows all passed a zero-only suite, so a build
    that still relayed an estimated $4.10 as a closed book was green on every surface.
    """
    estimated, row, _ = _settled_run(tmp_path / "est", monkeypatch, {
        "state": "succeeded", "spendUsd": 0, "spendEstimated": True,
        "inputTokens": 800318, "outputTokens": 4851})
    assert row["cost_usd"] == 0.0, "the amount is still the best fact anyone has"
    assert row["cost_final"] is False, "an ESTIMATED charge is not a settled one"
    assert estimated["cost"]["cost_final"] is False
    assert "ESTIMAT" in estimated["cost"]["note"].upper()
    assert ua.usage_projection(tmp_path / "est")["cost_final"] is False, \
        "one non-final row means the projection is not final, however little it cost"
    assert ua.usage_projection(tmp_path / "est")["estimated_usd"] == 0.0, \
        "and it is not final BECAUSE of the row, not because of the dollars"

    # The MONEY half of the same defect. An estimate with a real amount must ride the
    # ledger as money and still refuse finality on all three surfaces.
    charged, row, _ = _settled_run(tmp_path / "chg", monkeypatch, {
        "state": "succeeded", "spendUsd": 4.10, "spendEstimated": True,
        "inputTokens": 800318, "outputTokens": 4851})
    assert row["cost_usd"] == 4.10, "an estimate is still the best fact anyone has"
    assert row["cost_final"] is False, "and $4.10 unsettled is not $4.10 paid"
    assert charged["cost"]["cost_usd"] == 4.10
    assert charged["cost"]["cost_final"] is False
    charged_projection = ua.usage_projection(tmp_path / "chg")
    assert charged_projection["estimated_usd"] == 4.10, "it lands in the estimated bucket"
    assert charged_projection["confirmed_usd"] == 0.0, "and never in the confirmed one"
    assert charged_projection["cost_final"] is False

    # The control: the same amount, SETTLED, is the free-session case this row kind was
    # created for and must still leave the projection final.
    settled, row, _ = _settled_run(tmp_path / "set", monkeypatch, {
        "state": "succeeded", "spendUsd": 0, "spendEstimated": False,
        "inputTokens": 800318, "outputTokens": 4851})
    assert row["cost_final"] is True and row["cost_usd"] == 0.0
    assert settled["cost"]["cost_final"] is True
    assert ua.usage_projection(tmp_path / "set")["cost_final"] is True


@pytest.mark.parametrize("summary, cost_usd, final, disclosed, estimated", [
    # UNDISCLOSED: no amount. The envelope must not invent a zero, and the flag beside it
    # must be a definite False rather than whatever silence happened to produce.
    ({"state": "succeeded"}, None, False, False, False),
    ({"state": "succeeded", "spendUsd": 0, "spendEstimated": True}, 0.0, False, True, True),
    ({"state": "succeeded", "spendUsd": 4.10, "spendEstimated": True}, 4.10, False, True, True),
    ({"state": "succeeded", "spendUsd": 0}, 0.0, True, True, False),
    ({"state": "succeeded", "spendUsd": 4.10}, 4.10, True, True, False),
])
def test_the_settled_envelope_tells_the_same_story_as_the_row(
        tmp_path, monkeypatch, summary, cost_usd, final, disclosed, estimated):
    """`delegate_run_settled` RE-DERIVES the finality the recorder just decided.

    Nothing in the tree referenced `delegate_run_settled`, `spend_estimated` or
    `spend_disclosed` — `grep -rn` over `tests/` returned nothing — so re-zeroing an
    undisclosed `cost_usd`, dropping `not estimated` from the envelope's finality, and
    deleting the `spend_estimated` field ALL passed. Two writers of one fact with no
    reader watching is the drift this pins shut: the envelope is asserted against the row
    from the SAME run, in every cash state, so the two cannot part company silently.
    """
    _, row, envelope = _settled_run(tmp_path, monkeypatch, summary)
    assert envelope["cost_usd"] == cost_usd, "the envelope reports the row's own amount"
    assert envelope["cost_final"] is final
    assert envelope["spend_disclosed"] is disclosed
    assert envelope["spend_estimated"] is estimated
    assert envelope["cost_usd"] == row["cost_usd"], "one envelope, one story"
    assert envelope["cost_final"] == row["cost_final"], "and one finality"


def test_an_unreported_token_count_is_unknown_not_zero(tmp_path, monkeypatch):
    """The control schema: "null until a harness reported it — never render null as 0".

    Live `/v2/runs` rows really carry `inputTokens: null`, and `int(x or 0)` made a run
    that reported nothing indistinguishable in the ledger from one that genuinely used
    zero. Same rule v6.87.35 established for cost, one axis over.

    That schema sentence governs THREE fields, and `cachedInputTokens` is the third: 28 of
    60 rows on a live `/v2/runs` page carry it non-null, 27 of them non-zero (one at
    34.8M). Reading only two left the row with no `cached_tokens` key at all, which
    `_breakdown_bucket` renders as 0 beside a six-figure prompt count — exactly the
    render-unknown-as-zero shape its two siblings had just stopped doing.
    """
    _, silent, _ = _settled_run(tmp_path / "silent", monkeypatch, {
        "state": "succeeded", "spendUsd": 0, "inputTokens": None, "outputTokens": None,
        "cachedInputTokens": None})
    assert silent["prompt_tokens"] is None and silent["completion_tokens"] is None, \
        "a run that reported nothing must not be written as a run that used zero"
    assert silent["cached_tokens"] is None, "and the third field obeys the same sentence"

    _, real_zero, _ = _settled_run(tmp_path / "zero", monkeypatch, {
        "state": "succeeded", "spendUsd": 0, "inputTokens": 0, "outputTokens": 0,
        "cachedInputTokens": 0})
    assert real_zero["prompt_tokens"] == 0 and real_zero["completion_tokens"] == 0, \
        "a disclosed zero is a fact and must survive as 0, not become None"
    assert real_zero["cached_tokens"] == 0

    _, counted, _ = _settled_run(tmp_path / "counted", monkeypatch, {
        "state": "succeeded", "spendUsd": 0, "inputTokens": 10, "outputTokens": 5,
        "cachedInputTokens": 34808493})
    assert (counted["prompt_tokens"], counted["completion_tokens"]) == (10, 5)
    assert counted["cached_tokens"] == 34808493, \
        "a reported cache hit is real usage and must reach the ledger, not be dropped"
    # It reaches the reader that renders it, and is NOT folded into the grand total —
    # required, because cached is a SUBSET of input for some harnesses and disjoint for
    # others, so a sum across them means nothing.
    bucket = ua.usage_breakdown(tmp_path / "counted")
    assert bucket["cached_tokens"] == 34808493
    assert bucket["total_tokens"] == 15


def test_the_start_request_asks_for_the_substrate_it_claims(tmp_path, monkeypatch):
    """`authPreference` defaults to `auto` = subscription-first WITH fallback to a paid
    key. Asking explicitly is the difference between claiming a free session and getting
    one. Round 1 asserted this nowhere — `grep authPreference tests/` returned nothing."""
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    seen = {}

    class _Stub:
        def handshake(self): return {}
        def agent_capabilities(self):
            return {"harnesses": [{"id": "some-route", "enabled": True, "status": "ok",
                                   "accessProfilesSupported": ["readonly"]}]}
        def quota_snapshots(self): return []
        def find_project_id(self, root): return "prj-existing"
        def start_run(self, request, *, idempotency_key=""):
            seen["request"] = request
            return {"runId": "run-1"}
        def close(self): pass

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    delegate._CUSTODY.clear()
    delegate._delegate_start(_plain_ctx(tmp_path), "x")
    delegate._CUSTODY.clear()
    assert seen["request"]["authPreference"] == "subscription"
    # And the configured route is PINNED as the explicit one-element pool:
    # `primaryHarness` alone only fronts the engine's auto-pool, so without
    # this the child could fail over onto a harness the owner never named.
    assert seen["request"]["harnesses"] == ["some-route"]
    assert seen["request"]["primaryHarness"] == "some-route"


def test_a_202_handle_without_a_run_id_is_a_live_run_not_a_failure(tmp_path, monkeypatch):
    """A 202 answers with `jobId` and no `runId` when the run has not bound a run dir
    inside the daemon's start timeout. The run IS enqueued and will execute; discarding
    the handle left it live, unwaitable and uncancellable, and invited a duplicate."""
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    class _Stub:
        def handshake(self): return {}
        def agent_capabilities(self):
            return {"harnesses": [{"id": "some-route", "enabled": True, "status": "ok",
                                   "accessProfilesSupported": ["readonly"]}]}
        def quota_snapshots(self): return []
        def find_project_id(self, root): return "prj-existing"
        def start_run(self, request, *, idempotency_key=""): return {"jobId": "job-42"}   # 202: no runId yet
        def close(self): pass

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    delegate._CUSTODY.clear()
    out = json.loads(delegate._delegate_start(_plain_ctx(tmp_path), "x"))
    assert out["status"] == "started", out
    assert out["run_id"] == "job-42"
    assert "job-42" in delegate._CUSTODY, "the run must be in custody or nobody can cancel it"
    delegate._CUSTODY.clear()


def test_a_failed_ledger_write_leaves_the_session_retryable(tmp_path, monkeypatch):
    """The ledger lock can time out under worker concurrency. That is a transient, not a
    decision — marking custody settled would burn the only chance to record the row."""
    import ouroboros.tools.delegate as delegate
    import ouroboros.usage_accounting as ua
    from ouroboros.gateways import claudexor as gw
    from ouroboros.tools.registry import ToolContext

    retired = []

    class _Stub:
        def handshake(self): return {}
        def get_run(self, rid):
            return {"lastSeq": 9, "summary": {"state": "succeeded", "spendUsd": 0.0}}
        def remove_project(self, pid): retired.append(pid)
        def close(self): pass

    def _boom(*a, **k):
        raise ua.UsageAccountingError("usage accounting lock unavailable")

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    monkeypatch.setattr(ua, "record_subscription_session", _boom)
    delegate._CUSTODY.clear()
    custody = delegate._RunCustody(task_id="t-a", route_id="r", model="m",
                                   project_id="prj-ours", project_owned=True)
    delegate._CUSTODY["run-1"] = custody
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "t-a"
    ctx.task_metadata = {"root_task_id": "t-a"}

    json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))
    assert custody.settled is False, "a lost write must stay retryable"
    # Retirement is INDEPENDENT of whether the ledger write landed. The round-2 commit
    # claimed this and the fixture owned no project, so deleting the call left the suite
    # green — a leak per failed settle, and a halted run never settles again.
    assert retired == ["prj-ours"], "an owned registration must be retired even on failure"
    delegate._CUSTODY.clear()


def _plain_ctx(tmp_path):
    """A read-only nanny context: the smallest thing `_delegate_start` will accept."""
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "t-nanny"
    ctx.task_metadata = {"root_task_id": "t-root", "parent_task_id": "t-root"}
    return ctx


def test_an_unresolvable_write_root_is_a_typed_refusal_not_a_traceback(tmp_path):
    """"Can this path be resolved at all" is ONE question, not an exception set.

    `Path.resolve()` raises `ValueError` on an embedded null and `RuntimeError` on a
    symlink loop, neither of which is an `OSError`. Either escaping `_mutating_run_root`
    aborts `delegate_start` with a traceback instead of the typed refusal the function
    exists to produce — and a guard that raises delivers no decision at all.
    """
    import os

    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.subagents import delegated_run_shape
    from ouroboros.tools.delegate import _mutating_run_root, _resolved
    from ouroboros.tools.registry import ToolContext

    os.symlink(tmp_path / "b", tmp_path / "a")
    os.symlink(tmp_path / "a", tmp_path / "b")
    assert _resolved(tmp_path / "a" / "x") is None, "a symlink loop must resolve to None"
    assert _resolved("/etc/passwd\x00") is None, "an embedded null must resolve to None"
    assert _resolved(tmp_path) == tmp_path.resolve(), "an ordinary path still resolves"

    workspace = tmp_path.parent / f"ws-{tmp_path.name}"
    workspace.mkdir()
    ctx = ToolContext(
        repo_dir=tmp_path / "repo", drive_root=tmp_path,
        task_constraint=TaskConstraint(mode="acting_subagent", surface="self_worktree",
                                       write_root=str(tmp_path / "a" / "x")),
    )
    ctx.workspace_root = str(workspace)
    ctx.workspace_mode = "self_worktree"
    root, refusal = _mutating_run_root(
        ctx, delegated_run_shape(True))
    assert refusal and "write_root_mismatch" in refusal, refusal
    assert root == ""


def test_an_inactive_workspace_is_refused_even_when_the_root_is_set(tmp_path):
    """The DISTINGUISHING case for the round-3 predicate fix, which had no test.

    The old check was `workspace_mode_block_reason(ctx) == "" and workspace_root set`,
    and `workspace_mode_block_reason` returns "" precisely WHEN `workspace_mode` is
    empty — so with a root set and the mode empty, the old condition passed and handed a
    shell the fallback root. Every existing test cleared BOTH fields, which the old
    predicate also refused via its `workspace_root` leg, so reverting the fix left the
    suite green. This is the one shape that tells the two predicates apart.
    """
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tool_access import workspace_mode_block_reason
    from ouroboros.subagents import delegated_run_shape
    from ouroboros.tools.delegate import _mutating_run_root
    from ouroboros.tools.registry import ToolContext

    repo = tmp_path / "repo"
    repo.mkdir()
    ctx = ToolContext(
        repo_dir=repo, drive_root=tmp_path,
        task_constraint=TaskConstraint(mode="acting_subagent", surface="self_worktree",
                                       write_root=str(repo)),
    )
    ctx.workspace_root = str(repo)   # SET...
    ctx.workspace_mode = ""          # ...but the mode is not, so the workspace is not active

    assert workspace_mode_block_reason(ctx) == "", "the old predicate's leg is satisfied here"
    assert ctx.is_workspace_mode() is False, "yet the workspace is genuinely inactive"

    root, refusal = _mutating_run_root(
        ctx, delegated_run_shape(True))
    assert refusal, "an inactive workspace must be refused"
    assert "workspace_not_active" in refusal, refusal
    assert root == ""


# -- 5. the delegated-run marker and the containment it must actually deliver ----
#
# Without `execution.delegated`, Claudexor gives an in-place (`live`) run the OPERATOR's
# real `$HOME` — which holds `~/.claudexor/v3/daemon/token`, a bearer token for the whole
# `/v2` control API. A mutating delegated child is exactly that shape.


def _isolation_stub(monkeypatch, *, run_dir, engine_version=CLAUDEXOR_DELEGATED_MARKER_MIN_VERSION,
                    effective_access="workspace_write", state="running"):
    """A daemon serving one run whose artifacts sit under ``run_dir``."""
    from ouroboros.gateways import claudexor as gw

    cancelled = {}

    class _Stub:
        engine_version = ""

        def handshake(self): return {}
        def get_run(self, rid):
            return {"lastSeq": 7, "summary": {
                "state": "cancelled" if cancelled else state,
                "effectiveAccess": effective_access,
                "runDir": str(run_dir),
            }}
        def cancel_run(self, rid, reason=""):
            cancelled["reason"] = reason
            return {"accepted": True}
        def remove_project(self, pid): pass
        def close(self): pass

    _Stub.engine_version = engine_version
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    return cancelled


def _write_attempt(run_dir, *, isolated, home_dir, attempt="a01", mechanism="seatbelt"):
    """One clean `attempt.yaml`, in Claudexor's own applied-facts shape.

    `mechanism=None` is the record an engine writes when it applied NO OS boundary —
    3.3.0/3.3.1, which have no confinement fields at all, and any host whose engine
    ships a mechanism it cannot use here. It is a supported outcome, not a malformed
    record, which is why it is a parameter of the ordinary helper.
    """
    attempt_dir = run_dir / "attempts" / attempt
    attempt_dir.mkdir(parents=True, exist_ok=True)
    record = {"attempt_id": attempt, "harness_id": "some-route", "harness_home_dir": home_dir}
    if isolated is not None:
        record["harness_home_isolated"] = isolated
    if mechanism is not None:
        record["confinement_mechanism"] = mechanism
        record["confinement_profile_digest"] = "sha256:" + "0" * 64
        record["confinement_verified_denied_path"] = "/Users/op/.claudexor/v3/daemon"
    lines = [f"{k}: {json.dumps(v)}" for k, v in record.items()]
    (attempt_dir / "attempt.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_failed_attempt(run_dir, *, attempt="a01"):
    """An errored attempt.yaml with NO harness-HOME fields. `AC.attemptFailureRecord`
    (orchestrator.ts:3512 and :5088) spreads the applied facts in today, but
    `harness_home_isolated` is the one optional member — absent when the attempt died
    before its home was decided — and an engine older than 3.3.2 wrote none of them."""
    attempt_dir = run_dir / "attempts" / attempt
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "attempt.yaml").write_text(
        "\n".join([
            f"attempt_id: {json.dumps(attempt)}",
            'harness_id: "some-route"', "cost_usd: 0.4", "cost_estimated: true",
            "errored: true", 'phase: "harness"', 'errors:\n  - "stream ended early"',
        ]) + "\n",
        encoding="utf-8",
    )


def _waiting(tmp_path, monkeypatch, *, acting=True):
    import ouroboros.tools.delegate as delegate

    ctx = _delegating_ctx(tmp_path, acting=acting)
    delegate._CUSTODY.clear()
    delegate._CUSTODY["run-1"] = delegate._RunCustody(
        task_id="t-nanny", route_id="some-route", model="m",
        project_id="prj", project_owned=False,
    )
    # since_seq=0 so a HEALTHY run answers `progress` immediately: the distinguishing
    # signal is "was this halted as a containment fault", not how long the poll idled.
    out = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1, since_seq=0))
    delegate._CUSTODY.clear()
    return out


def test_a_mutating_run_asks_for_a_scoped_home_and_a_read_only_one_does_not(tmp_path, monkeypatch):
    """The marker is what confines an in-place run; without it the harness inherits the
    operator's `$HOME` and the daemon token in it. It must ride with `isolation: live`
    and must NOT appear on a read-only run, whose envelope is scoped already and whose
    lane has to keep working against a daemon that does not know the field."""
    request, payload = _started_request(tmp_path, acting=True, monkeypatch=monkeypatch)
    assert request["execution"]["delegated"] is True, request["execution"]
    # And what the nanny is told at START is that the home was ASKED for — never that it
    # was applied, which only the run's own artifacts can say. Dropping this leaves the
    # nanny with `isolation: live` alone, the exact shape that reads as "confined".
    assert payload["scoped_home_requested"] is True, payload
    request, payload = _started_request(tmp_path, acting=False, monkeypatch=monkeypatch)
    assert "execution" not in request
    assert payload["scoped_home_requested"] is False, payload


def test_an_engine_without_the_marker_refuses_the_mutating_lane_and_keeps_the_read_only_one(
    tmp_path, monkeypatch,
):
    """The floor is a VERSION, not hope and not a probe, and it is a floor for exactly one
    thing: whether the engine's SCHEMA accepts the marker. `RunExecution` is strict and has
    no `delegated` key below 3.3.0, so the field is a 400 (verified live against the running
    daemon), and the capability catalog lists TOP-LEVEL request keys only, so a nested marker
    is undiscoverable — the version is the only answer available.

    The refusal must be typed and must happen BEFORE the run starts, because the alternative
    is spending a dispatch on a request the engine will reject outright.
    """
    _, refusal = _started_request(tmp_path, acting=True, monkeypatch=monkeypatch,
                                  engine_version=CLAUDEXOR_MIN_VERSION, expect="refused")
    assert refusal["reason"] == "engine_rejects_delegated_marker", refusal
    assert refusal["executor"] == "blocked", refusal
    # Read-only delegation sends no marker, so the same old daemon still serves it.
    request, payload = _started_request(tmp_path, acting=False, monkeypatch=monkeypatch,
                                        engine_version=CLAUDEXOR_MIN_VERSION)
    assert payload["status"] == "started" and "execution" not in request


def test_the_dispatcher_refuses_the_same_engine_the_nanny_would(monkeypatch):
    """The twin surface. `route_health` is the ONE health reader, so the decision made at
    DISPATCH — before a token is spent — must agree with the nanny's own. An `auto` child
    falls back to a NATIVE run with the visible marker (never to an uncontained delegated
    one); an explicit `harness` pin becomes a typed blocker; read-only is untouched."""
    from ouroboros.agent import dispatch_executor_note

    old = _HealthStub(engine_version=CLAUDEXOR_MIN_VERSION)
    res = _dispatch("auto", stub=old, monkeypatch=monkeypatch, acting=True)
    assert (res.executor, res.reason) == ("native", "engine_rejects_delegated_marker")
    assert "engine_rejects_delegated_marker" in dispatch_executor_note(res)
    res = _dispatch("harness", stub=_HealthStub(engine_version=CLAUDEXOR_MIN_VERSION),
                    monkeypatch=monkeypatch, acting=True)
    assert res.blocked and res.reason == "engine_rejects_delegated_marker"
    # A read-only child needs no marker, so the same engine is a healthy substrate.
    res = _dispatch("auto", stub=_HealthStub(engine_version=CLAUDEXOR_MIN_VERSION),
                    monkeypatch=monkeypatch)
    assert (res.executor, res.reason) == ("harness", "harness_ready")


@pytest.mark.parametrize("engine, serves_read_only, admits_mutating", [
    # Below the TRANSPORT floor: no lane at all, refused at handshake.
    ("3.1.9", False, False),
    # The engine the operator is actually RUNNING. A floor above this one is not caution,
    # it is an outage: read-only delegation stops working against the only live daemon.
    ("3.2.0", True, False),
    ("3.2.1", True, False),
    # The MARKER lands in 3.3.0: `RunExecution` gains `delegated` and the request stops
    # being a 400. 3.3.0-3.3.1 apply no OS boundary and 3.3.2 applies one only where the
    # host has a mechanism — a difference this floor deliberately does NOT try to encode,
    # because a version cannot: the run is admitted and what it actually got is read back
    # per attempt and disclosed.
    ("3.3.0", True, True),
    ("3.3.1", True, True),
    ("3.3.2", True, True),
    ("3.4.0", True, True),
])
def test_the_two_floors_sit_at_the_measured_bands(engine, serves_read_only, admits_mutating):
    """The floor VALUES, not just the code that reads them (docs/DELEGATED_ADMISSION.md).

    Every other test here spells the old engine `CLAUDEXOR_MIN_VERSION` and the new one
    `CLAUDEXOR_DELEGATED_MARKER_MIN_VERSION`, so the wiring is pinned and the NUMBERS are not:
    both constants could be moved to any pair with the transport floor below the mutating
    one and the whole suite stayed green. That is how a transport floor came to sit above
    the operator's own running daemon and a mutating floor came to sit at the release that
    ships one host's boundary.

    The bands are measured, not assumed (2026-08-03, live 3.2.0 daemon + the Claudexor
    tree): the read-only body comes back with the fake-root error and `fieldErrors: {}`,
    while the mutating body is rejected on `/execution/delegated` before the root is even
    looked at, and `RunExecution.delegated` first exists in 3.3.0. The mutating floor is
    the MARKER release for that reason and no other — the boundary that ships in 3.3.2 is
    macOS-only (`docs/DELEGATED_CONFINEMENT.md` §8), so pinning here to 3.3.2 would have
    encoded "a boundary exists" into a number that says the same thing on a host where
    none does.
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "protocolMajor": CLAUDEXOR_PROTOCOL_MAJOR, "compatible": True,
            "engine": {"version": engine},
        })

    with _gateway(handler) as gateway:
        if serves_read_only:
            gateway.handshake()
        else:
            with pytest.raises(cx.ClaudexorUnavailable) as excinfo:
                gateway.handshake()
            assert excinfo.value.code == "engine_too_old"

    # The two floors are asked of the SHAPE, at the one health reader. An engine between
    # them serves read-only and refuses mutating — the asymmetry is the whole design, and
    # collapsing the floors would cost the owner a working lane.
    stub = _HealthStub(engine_version=engine)
    acting = subagents.route_health(stub, "some-route", subagents.delegated_run_shape(True))[0]
    assert (acting == "") is admits_mutating, acting
    if not admits_mutating:
        assert acting == "engine_rejects_delegated_marker"
    assert subagents.route_health(
        stub, "some-route", subagents.delegated_run_shape(False))[0] == "", \
        "read-only sends no marker, so no engine that can talk at all may lose the lane"


def test_asking_for_a_scoped_home_is_not_evidence_that_one_was_applied(tmp_path, monkeypatch):
    """The whole point: the request is a request. An engine that accepted the marker and
    then ran the harness in the operator's own home has produced a CONTAINMENT FAULT, and
    the only witness is the attempt's own artifact — Claudexor projects the applied HOME
    fact onto no `/v2` response (only the boundary half reaches `candidates[].confinement`)."""
    run_dir = tmp_path / "run-1"
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setattr(cx, "operator_home", lambda: home)

    # (a) the engine recorded the fact as NOT applied
    cancelled = _isolation_stub(monkeypatch, run_dir=run_dir)
    _write_attempt(run_dir, isolated=False, home_dir=str(home))
    out = _waiting(tmp_path, monkeypatch)
    assert out["status"] == "refused" and out["reason"] == "home_isolation_not_applied", out
    assert cancelled["reason"] == "home_isolation_not_applied"

    # (b) it claims isolation while naming the operator's own home — the claim is the lie
    # the artifact check exists to catch, so the boolean alone is not the verification.
    cancelled = _isolation_stub(monkeypatch, run_dir=run_dir)
    _write_attempt(run_dir, isolated=True, home_dir=str(home))
    out = _waiting(tmp_path, monkeypatch)
    assert out["reason"] == "home_isolation_not_applied", out

    # (c) it recorded no fact at all: UNPROVEN, which is not the same as breached. A
    # fault needs a fact; the honesty of an undisclosed attempt belongs in the report,
    # not in a cancellation. See the failure-record test below for why absence is
    # the ordinary case rather than a suspicious one.
    cancelled = _isolation_stub(monkeypatch, run_dir=run_dir)
    _write_attempt(run_dir, isolated=None, home_dir="")
    out = _waiting(tmp_path, monkeypatch)
    assert out["status"] == "progress" and cancelled == {}, out

    # (d) a scoped home really applied: the run is left alone and keeps reporting progress
    cancelled = _isolation_stub(monkeypatch, run_dir=run_dir)
    _write_attempt(run_dir, isolated=True, home_dir=str(tmp_path / "scoped-home"))
    out = _waiting(tmp_path, monkeypatch)
    assert out["status"] == "progress", out
    assert cancelled == {}

    # (e) P34P1.5: a HOME NESTED inside the operator's own home. The check was equality
    # ONLY, so `$HOME/tmp/harness` passed as isolated — while DELEGATED_ADMISSION.md §8
    # calls exactly that a breach, and rightly: `~/.claudexor/v3/daemon/token` is still
    # reachable by a relative walk from there, which is the whole /v2 control API.
    for nested in (home / "tmp" / "harness", home / "sub", home / "a" / "b" / "c"):
        nested.mkdir(parents=True, exist_ok=True)
        cancelled = _isolation_stub(monkeypatch, run_dir=run_dir)
        _write_attempt(run_dir, isolated=True, home_dir=str(nested))
        out = _waiting(tmp_path, monkeypatch)
        assert out["reason"] == "home_isolation_not_applied", (nested, out)
        assert cancelled["reason"] == "home_isolation_not_applied"

    # ...and a SIBLING of the operator home is still legitimately scoped: the fix must
    # not turn "shares a parent directory" into a breach.
    sibling = tmp_path / "operator-home-2"
    sibling.mkdir(exist_ok=True)
    cancelled = _isolation_stub(monkeypatch, run_dir=run_dir)
    _write_attempt(run_dir, isolated=True, home_dir=str(sibling))
    out = _waiting(tmp_path, monkeypatch)
    assert out["status"] == "progress" and cancelled == {}, out


def test_absence_of_the_artifact_is_no_evidence_and_a_read_only_run_is_never_faulted(
    tmp_path, monkeypatch,
):
    """Two ways this check could be wrong in the OTHER direction, both of which would
    cancel healthy runs: an attempt writes its record when it FINISHES, so a young run
    legitimately has none; and a read-only child never sent the marker, so its artifacts
    say nothing about a confinement it did not ask for."""
    run_dir = tmp_path / "run-1"
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setattr(cx, "operator_home", lambda: home)

    cancelled = _isolation_stub(monkeypatch, run_dir=run_dir)
    out = _waiting(tmp_path, monkeypatch)          # no attempts dir at all
    assert out["status"] == "progress" and cancelled == {}

    cancelled = _isolation_stub(monkeypatch, run_dir=run_dir, effective_access="readonly")
    _write_attempt(run_dir, isolated=False, home_dir=str(home))
    out = _waiting(tmp_path, monkeypatch, acting=False)
    assert out["status"] == "progress", out
    assert cancelled == {}


def test_an_attempt_that_recorded_no_home_fact_is_not_a_containment_fault(tmp_path, monkeypatch):
    """An attempt record can legitimately state no HOME fact. `AC.attemptFailureRecord`
    (orchestrator.ts:3512 and :5088) spreads the applied facts into an errored record
    today, but `harness_home_isolated` is the one OPTIONAL member — omitted when the
    attempt died before its home was decided — and an engine older than 3.3.2 wrote
    attempt_id/harness_id/cost/errored/phase/errors and nothing else. "a01 errored, a02
    repaired it" is the ORDINARY path of the converge loop that Ouroboros's own
    `mode: agent` run takes, so a missing fact must be no evidence — exactly the line
    `_widened_access` already draws for an undisclosed access profile.

    Faulting on it cancels a correctly-confined, finished, SUCCESSFUL run and throws its
    terminal payload away, and tells the nanny that an ordinary harness failure was a
    containment fault it must not retry."""
    run_dir = tmp_path / "run-1"
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setattr(cx, "operator_home", lambda: home)

    # The engine's own repair loop: a01 errored, a02 ran confined, the run succeeded.
    cancelled = _isolation_stub(monkeypatch, run_dir=run_dir, state="succeeded")
    _write_failed_attempt(run_dir, attempt="a01")
    _write_attempt(run_dir, isolated=True, home_dir=str(tmp_path / "scoped"), attempt="a02")
    out = _waiting(tmp_path, monkeypatch)
    assert out["status"] == "terminal" and cancelled == {}, out
    # Honest, though: one attempt proved nothing, so the run's confinement is not proven.
    # `os_boundary` is empty for the same reason — a01 named no mechanism, and one
    # unconfined attempt is an unconfined run.
    assert out["containment"] == {
        "verified": False, "attempts": 2, "disclosed": 1, "os_boundary": "",
        "note": "not every attempt of this run recorded a harness-HOME fact, so its "
                "confinement is UNPROVEN — do not report it as isolated",
    }, out

    # And a lone failed attempt on a live run is a task failure, not a containment fault.
    cancelled = _isolation_stub(monkeypatch, run_dir=(only := tmp_path / "run-2"))
    _write_failed_attempt(only, attempt="a01")
    out = _waiting(tmp_path, monkeypatch)
    assert out["status"] == "progress" and cancelled == {}, out


def test_the_relayed_result_never_claims_an_isolation_no_artifact_proves(tmp_path, monkeypatch):
    """What the nanny hands its parent must distinguish PROVEN from merely asked: a run
    that disclosed no harness-HOME fact is unproven, and reporting it as isolated is the
    same untrue claim in a different place."""
    from ouroboros.subagents import delegated_run_shape
    from ouroboros.tools.delegate import _terminal_payload

    run_dir = tmp_path / "run-1"
    detail = {"summary": {"state": "succeeded", "runDir": str(run_dir)}}

    payload = _terminal_payload("run-1", detail, delegated_run_shape(True))
    assert payload["containment"]["verified"] is False
    assert "UNPROVEN" in payload["containment"]["note"]

    # An artifact that records a BREACH must not read as proof either: this verdict is
    # judged by the same predicate that halts the run, not by having been reached after
    # it, so it cannot be turned into a false "verified" by a change of call site.
    monkeypatch.setattr(cx, "operator_home", lambda: tmp_path / "operator-home")
    _write_attempt(run_dir, isolated=True, home_dir=str(tmp_path / "operator-home"))
    assert _terminal_payload("run-1", detail, delegated_run_shape(True))[
        "containment"]["verified"] is False

    _write_attempt(run_dir, isolated=True, home_dir=str(tmp_path / "scoped-home"))
    payload = _terminal_payload("run-1", detail, delegated_run_shape(True))
    assert payload["containment"] == {
        "verified": True, "attempts": 1, "disclosed": 1, "os_boundary": "seatbelt",
        "note": "every attempt recorded a scoped harness HOME outside the operator's own "
                "AND an applied seatbelt boundary, proven against a path it denies",
    }
    # A mechanism WITHOUT the denied path it was proven against is a promise, not an
    # applied fact — the exact shape 3.3.2's evidence block exists to replace.
    _write_attempt(run_dir, isolated=True, home_dir=str(tmp_path / "scoped-home"),
                   mechanism=None)
    unproven = tmp_path / "run-1" / "attempts" / "a01" / "attempt.yaml"
    unproven.write_text(unproven.read_text(encoding="utf-8")
                        + 'confinement_mechanism: "seatbelt"\n', encoding="utf-8")
    claimed = _terminal_payload("run-1", detail, delegated_run_shape(True))["containment"]
    assert claimed["os_boundary"] == "" and claimed["verified"] is False, claimed

    # A read-only run asked for nothing, so it claims nothing.
    assert "containment" not in _terminal_payload("run-1", detail, delegated_run_shape(False))


def test_a_run_with_no_os_boundary_is_disclosed_in_three_places_and_still_allowed(
    tmp_path, monkeypatch,
):
    """The scoped HOME is not the boundary, so a run that got only the HOME must not read
    like a run that got both — and it must still RUN.

    Before this, the two were BYTE-IDENTICAL here: an attempt with a kernel-enforced
    boundary and an attempt with none both produced
    `{verified: true, ... "every attempt recorded a scoped harness HOME outside the
    operator's own"}`, because the reader asked only about `harness_home_isolated`. The
    only thing standing between that report and a genuinely unconfined run was a VERSION
    floor pinned at the release that ships the boundary — and Claudexor's own
    `docs/DELEGATED_CONFINEMENT.md` §8 says that boundary is macOS-only, so the same
    number means "confined" on one host and nothing on another.

    The fix is not a refusal and not an OS test. Ouroboros asks the engine what it
    APPLIED, and where nothing was applied it says so LOUDLY in the three places
    AGENTS.md names — the durable record, the child's prompt, and the parent's result —
    while the work goes ahead (the child already holds a shell in this worktree).
    """
    from ouroboros.subagents import delegated_run_shape
    from ouroboros.tools.delegate import _terminal_payload

    run_dir = tmp_path / "run-1"
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setattr(cx, "operator_home", lambda: home)
    detail = {"summary": {"state": "succeeded", "runDir": str(run_dir)}}
    scoped = str(tmp_path / "scoped-home")

    # (1) THE PARENT'S RESULT distinguishes the two runs. This is the assertion the old
    # reader could not make: same HOME evidence, opposite verdicts.
    _write_attempt(run_dir, isolated=True, home_dir=scoped, mechanism="seatbelt")
    confined = _terminal_payload("run-1", detail, delegated_run_shape(True))["containment"]
    _write_attempt(run_dir, isolated=True, home_dir=scoped, mechanism=None)
    bare = _terminal_payload("run-1", detail, delegated_run_shape(True))["containment"]
    assert confined != bare, "a boundary and no boundary must not report identically"
    assert (confined["os_boundary"], confined["verified"]) == ("seatbelt", True), confined
    assert (bare["os_boundary"], bare["verified"]) == ("", False), bare
    assert "NO OS-ENFORCED BOUNDARY" in bare["note"], bare
    assert "daemon token" in bare["note"], "say what is reachable, not just that it failed"

    # The predicate is the APPLIED MECHANISM, never the host OS. A mechanism Ouroboros
    # has never heard of counts as a boundary: the day a Linux one ships, this reader is
    # already right, and it never had a `sys.platform` branch to go stale.
    _write_attempt(run_dir, isolated=True, home_dir=scoped, mechanism="landlock")
    future = _terminal_payload("run-1", detail, delegated_run_shape(True))["containment"]
    assert (future["os_boundary"], future["verified"]) == ("landlock", True), future

    # (2) THE DURABLE RECORD carries it, and the run is NOT cancelled or refused.
    _write_attempt(run_dir, isolated=True, home_dir=scoped, mechanism=None)
    cancelled = _isolation_stub(monkeypatch, run_dir=run_dir, state="succeeded")
    out = _waiting(tmp_path, monkeypatch)
    assert out["status"] == "terminal" and cancelled == {}, out
    assert out["containment"]["os_boundary"] == "", out
    events = [json.loads(line) for line in
              (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    unconfined = [e for e in events if e["type"] == "delegate_run_unconfined"]
    assert len(unconfined) == 1, events
    assert unconfined[0]["run_id"] == "run-1" and unconfined[0]["os_boundary"] == ""
    assert "NO OS-ENFORCED BOUNDARY" in unconfined[0]["note"]

    # A run that DID get a boundary writes no such line — the durable record states the
    # gap, it does not narrate every healthy run.
    _write_attempt(run_dir, isolated=True, home_dir=scoped, mechanism="seatbelt")
    _isolation_stub(monkeypatch, run_dir=run_dir, state="succeeded")
    _waiting(tmp_path, monkeypatch)
    events = [json.loads(line) for line in
              (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len([e for e in events if e["type"] == "delegate_run_unconfined"]) == 1, events


def test_the_child_is_told_its_boundary_is_a_request_and_not_a_fact(tmp_path, monkeypatch):
    """Destination 2. The child is the only party that can act on this at the time it
    matters, and it is also the party that writes the answer the parent reads — so it is
    told, in its own instructions, not to describe itself as sandboxed.

    It cannot be told WHICH way it went: nothing at start knows. The engine decides per
    attempt and records the fact afterwards, so the honest thing to hand the child is the
    uncertainty plus the behaviour it implies. A read-only child asked for no boundary and
    is told nothing about one."""
    request, _ = _started_request(tmp_path, acting=True, monkeypatch=monkeypatch)
    instructions = request["instructions"]
    assert "not guaranteed" in instructions.lower(), instructions
    assert "sandboxed or confined" in instructions, instructions
    assert "Work as if there is no boundary" in instructions, instructions

    request, _ = _started_request(tmp_path, acting=False, monkeypatch=monkeypatch)
    assert "boundary" not in request["instructions"].lower(), request["instructions"]


# -- 3.8 custody is durable, not process-local ---------------------------------


def _nanny_ctx(tmp_path, task_id="t-a"):
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = task_id
    ctx.task_metadata = {"root_task_id": task_id, "parent_task_id": task_id}
    return ctx


def _event_types(tmp_path):
    path = tmp_path / "logs" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line).get("type") for line in path.read_text().splitlines() if line.strip()]


class _LiveRunStub:
    """A daemon whose run starts and keeps running."""

    def __init__(self, run_id="run-live", state="running"):
        self.run_id, self.state, self.cancels = run_id, state, []

    def handshake(self): return {}
    def agent_capabilities(self):
        return {"harnesses": [{"id": "some-route", "enabled": True, "status": "ok",
                               "accessProfilesSupported": ["readonly"]}]}
    def quota_snapshots(self): return []
    def find_project_id(self, root): return "prj-existing"
    def start_run(self, request, *, idempotency_key=""): return {"runId": self.run_id}
    # `effectiveAccess` is what the daemon DERIVES, and the containment reader treats an
    # undisclosed profile on a run that has already produced journal events as unverified.
    # A read-only fixture that omits it is not a narrower daemon, it is an unfaithful one.
    def get_run(self, rid):
        return {"lastSeq": 1, "summary": {"state": self.state, "effectiveAccess": "readonly"}}
    def cancel_run(self, rid, reason=""):
        self.cancels.append((rid, reason))
        return {"accepted": True, "status": "accepted"}
    def remove_project(self, pid): pass
    def close(self): pass


def test_custody_survives_the_worker_that_started_the_run(tmp_path, monkeypatch):
    """A worker crash, a restart or a lost response used to leave a LIVE mutating run
    that nothing could wait on, cancel or settle — and the process-local dict then
    refused the OWNING task itself, because the only record of ownership died with the
    process. Ownership now replays from the durable `delegate_run_started` row, and an
    id with no durable record at all is UNKNOWN, which is a different answer from
    "belongs to someone else"."""
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    stub = _LiveRunStub()
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: stub)
    delegate._CUSTODY.clear()
    ctx = _nanny_ctx(tmp_path)
    assert json.loads(delegate._delegate_start(ctx, "review the diff"))["status"] == "started"

    delegate._CUSTODY.clear()          # the worker died; only the durable rows remain
    resumed = json.loads(delegate._delegate_wait(ctx, "run-live", wait_sec=1))
    assert resumed["status"] == "no_progress", resumed
    cancelled = json.loads(delegate._delegate_cancel(ctx, "run-live", reason="restart"))
    assert cancelled["status"] in {"requested", "confirmed"}, cancelled
    assert stub.cancels, "the restarted owner must be able to actually stop its own run"

    delegate._CUSTODY.clear()
    sibling = json.loads(delegate._delegate_wait(_nanny_ctx(tmp_path, "t-b"), "run-live", wait_sec=1))
    assert sibling["reason"] == "run_not_owned", sibling
    unknown = json.loads(delegate._delegate_wait(ctx, "run-never-seen", wait_sec=1))
    assert unknown["reason"] == "run_ownership_unknown", unknown
    delegate._CUSTODY.clear()


def test_the_invocation_id_is_reused_on_retry_and_fresh_per_intended_start(
        tmp_path, monkeypatch):
    """One LOGICAL INVOCATION ID per intended invocation, reused ONLY by explicit
    token. Both wire-level failure shapes are pinned: a fresh uuid4 per POST (an
    accepted start whose response was lost comes back as a SECOND live run) and any
    content-matched reuse (an INTENDED new start of the same prompt silently
    inheriting the old handle -- the owner's contract: intended new start = NEW id).
    A start with an unknown outcome hands back pending_invocation_id; only a call
    presenting it as retry_of replays the invocation -- the STORED canonical body,
    byte-identical by construction even when the route config drifted between the
    attempts, under the original key (the engine 409s a same-key-different-digest
    replay). A bound or definitely refused invocation is never replayed."""
    import httpx

    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    script = ["transport_error", "ok", "ok", "definite_refusal", "transport_error"]
    keys, bodies = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/handshake":
            return httpx.Response(200, json={"protocolMajor": CLAUDEXOR_PROTOCOL_MAJOR,
                                             "compatible": True,
                                             "engine": {"version": CLAUDEXOR_MIN_VERSION}})
        if path == "/v2/agent-capabilities":
            return httpx.Response(200, json={"harnesses": [
                {"id": "some-route", "enabled": True, "status": "ok",
                 "accessProfilesSupported": ["readonly"]}]})
        if path == "/v2/quota":
            return httpx.Response(200, json={"snapshots": []})
        if path == "/v2/projects":
            return httpx.Response(200, json={"projects": [{"id": "prj-existing", "root": str(tmp_path)}]})
        keys.append(request.headers.get("Idempotency-Key"))
        bodies.append(json.loads(request.read()))
        action = script.pop(0)
        if action == "transport_error":
            raise httpx.ConnectError("daemon fell over mid-POST")
        if action == "definite_refusal":
            return httpx.Response(400, json={"code": "bad_request", "message": "no"})
        return httpx.Response(200, json={"runId": f"run-{len(keys)}"})

    real_gateway = cx.ClaudexorGateway   # captured before the name is patched below

    def _fresh(*_a, **_k):
        gateway = real_gateway(cx.DaemonEndpoint("127.0.0.1", 1, "secret-token"))
        gateway._client = httpx.Client(base_url="http://127.0.0.1:1",
                                       transport=httpx.MockTransport(handler),
                                       headers=dict(gateway._client.headers))
        return gateway

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")
    monkeypatch.setattr(gw, "ClaudexorGateway", _fresh)
    delegate._CUSTODY.clear()
    ctx = _nanny_ctx(tmp_path)
    prompt = "the same intended work"

    # 1. Outcome unknown: the refusal HANDS BACK the retry token. Nothing else may
    #    ever resurrect this invocation.
    lost = json.loads(delegate._delegate_start(ctx, prompt, max_seconds=120))
    assert lost["status"] == "refused" and lost["reason"] == "daemon_unreachable"
    token = lost["pending_invocation_id"]
    assert token == keys[0] and "retry_of" in lost["retry_hint"]

    # 2. A plain identical call is an INTENDED NEW start: fresh id, never the token.
    fresh = json.loads(delegate._delegate_start(ctx, prompt))
    assert fresh["status"] == "started"
    assert keys[1] != token, "content-matched reuse is forbidden: new intention, new id"
    assert fresh["idempotent_recovery"] is False

    # 3. Only the EXPLICIT token replays the invocation -- the STORED body verbatim,
    #    even though the route config drifted between the attempts.
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:high")
    retried = json.loads(delegate._delegate_start(ctx, prompt, retry_of=token))
    assert retried["status"] == "started" and retried["idempotent_recovery"] is True
    assert keys[2] == token, "the retry must present the original invocation id"
    assert bodies[2] == bodies[0], "the retry must replay the RECORDED body, not re-derive it"
    assert bodies[2]["maxSeconds"] == 120 and bodies[2]["effort"] == "low"

    # The id lives in the run's durable record and survives the worker.
    delegate._CUSTODY.clear()
    assert dc.replay(tmp_path)[retried["run_id"]].invocation_id == token

    # 4-5. A bound invocation is never re-posted; an unknown token is refused.
    again = json.loads(delegate._delegate_start(ctx, prompt, retry_of=token))
    assert again["reason"] == "invocation_already_started"
    assert again["run_id"] == retried["run_id"]
    ghost = json.loads(delegate._delegate_start(ctx, prompt, retry_of="no-such-invocation"))
    assert ghost["reason"] == "unknown_invocation"

    # 6. A DEFINITE refusal offers no token: the id is dead, the next start is new.
    refused = json.loads(delegate._delegate_start(ctx, prompt))
    assert refused["status"] == "refused" and "pending_invocation_id" not in refused

    # 7-8. The token replays the recorded invocation, so a divergent prompt is a
    #    confusion, not a merge.
    lost2 = json.loads(delegate._delegate_start(ctx, prompt))
    assert lost2["reason"] == "daemon_unreachable"
    mismatch = json.loads(delegate._delegate_start(
        ctx, "an entirely different ask", retry_of=lost2["pending_invocation_id"]))
    assert mismatch["reason"] == "retry_prompt_mismatch"

    assert len(keys) == 5, "refused retry_of shapes must never reach the wire"
    assert len({keys[0], keys[1], keys[3], keys[4]}) == 4, "one id per intended invocation"
    delegate._CUSTODY.clear()


def test_a_retry_testifies_about_the_stored_invocation_not_the_current_config(
        tmp_path, monkeypatch):
    """A retry POSTs the STORED canonical body — so every fact written or said about
    it must come from the stored invocation too. The old branch re-derived the
    pre-flight health check, the root, the project and the custody/attempt rows from
    the CURRENT route/model/workspace context, so the durable record and the parent's
    result described a configuration the run never had (Codex audit
    run-b62c202d72db). Drift EVERYTHING before the retry — route id, model, effort,
    active root, and make the current route unknown to the daemon — and the retry
    must still replay, health-check and testify the recorded invocation."""
    import httpx

    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    root_a = tmp_path / "root-a"; root_a.mkdir()
    root_b = tmp_path / "root-b"; root_b.mkdir()
    drive = tmp_path / "drive"; drive.mkdir()

    script = ["transport_error", "ok"]
    keys, bodies, registrations, removals = [], [], [], []
    projects: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/handshake":
            return httpx.Response(200, json={"protocolMajor": CLAUDEXOR_PROTOCOL_MAJOR,
                                             "compatible": True,
                                             "engine": {"version": CLAUDEXOR_MIN_VERSION}})
        if path == "/v2/agent-capabilities":
            # Only the ORIGINAL route exists. The drifted current config below names
            # route-b, which the daemon has never heard of: a health check asked about
            # the current route refuses the retry outright.
            return httpx.Response(200, json={"harnesses": [
                {"id": "route-a", "enabled": True, "status": "ok",
                 "accessProfilesSupported": ["readonly"]}]})
        if path == "/v2/quota":
            return httpx.Response(200, json={"snapshots": []})
        if path == "/v2/projects" and request.method == "GET":
            return httpx.Response(200, json={"projects": [
                {"id": pid, "root": known} for known, pid in projects.items()]})
        if path == "/v2/projects" and request.method == "POST":
            body = json.loads(request.read())
            pid = f"prj-{len(projects) + 1}"
            projects[str(body["root"])] = pid
            registrations.append(str(body["root"]))
            return httpx.Response(200, json={"id": pid})
        if request.method == "DELETE" and path.startswith("/v2/projects/"):
            removals.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={})
        assert path == "/v2/runs", path
        keys.append(request.headers.get("Idempotency-Key"))
        bodies.append(json.loads(request.read()))
        action = script.pop(0)
        if action == "transport_error":
            raise httpx.ConnectError("daemon fell over mid-POST")
        if action == "definite_refusal":
            return httpx.Response(400, json={"code": "bad_request", "message": "no"})
        return httpx.Response(200, json={"runId": f"run-{len(keys)}"})

    real_gateway = cx.ClaudexorGateway

    def _fresh(*_a, **_k):
        gateway = real_gateway(cx.DaemonEndpoint("127.0.0.1", 1, "secret-token"))
        gateway._client = httpx.Client(base_url="http://127.0.0.1:1",
                                       transport=httpx.MockTransport(handler),
                                       headers=dict(gateway._client.headers))
        return gateway

    def _ctx(repo_dir):
        from ouroboros.tools.registry import ToolContext

        ctx = ToolContext(repo_dir=repo_dir, drive_root=drive)
        ctx.task_id = "t-a"
        ctx.task_metadata = {"root_task_id": "t-a", "parent_task_id": "t-a"}
        return ctx

    monkeypatch.setattr(gw, "ClaudexorGateway", _fresh)
    delegate._CUSTODY.clear()

    # 1. The intended start: route-a=model-old:low at root-a. It registers and OWNS
    #    the project for root-a, then the POST's outcome is lost.
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "route-a=model-old:low")
    lost = json.loads(delegate._delegate_start(_ctx(root_a), "the intended work",
                                               max_seconds=120))
    assert lost["reason"] == "daemon_unreachable"
    token = lost["pending_invocation_id"]
    prj_a = projects[str(root_a)]

    # 2. EVERYTHING drifts before the retry: route id, model, effort and active root.
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "route-b=model-new:high")

    # 2a. A refused token performs no daemon work at all — the old branch registered
    #     a project for the CURRENT root before even reading the record.
    ghost = json.loads(delegate._delegate_start(_ctx(root_b), "the intended work",
                                                retry_of="no-such-invocation"))
    assert ghost["reason"] == "unknown_invocation"
    assert str(root_b) not in projects, "a refused retry must not register projects"

    # 2b. A retry whose attempt row cannot land keeps the ORIGINAL attempt's facts
    #     alive: the owned project is NOT retired (a run may exist behind the lost
    #     POST) and the invocation stays pending, so a later retry still works.
    monkeypatch.setattr(dc, "record_start_requested", lambda *a, **k: False)
    unwritable = json.loads(delegate._delegate_start(_ctx(root_b), "the intended work",
                                                     retry_of=token))
    assert unwritable["reason"] == "start_request_row_unwritable"
    assert removals == [], "an unknown original outcome must keep its project"
    monkeypatch.undo()
    monkeypatch.setattr(gw, "ClaudexorGateway", _fresh)
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "route-b=model-new:high")

    # 3. The real retry: health is asked about the STORED route (the current route-b
    #    is not in the daemon's catalog at all), the wire carries the STORED body,
    #    and no project is registered for the drifted root.
    retried = json.loads(delegate._delegate_start(_ctx(root_b), "the intended work",
                                                  retry_of=token))
    assert retried["status"] == "started", retried
    assert bodies[-1] == bodies[0], "the retry replays the RECORDED body"
    assert keys[-1] == token
    assert str(root_b) not in projects, "a retry binds no NEW resources"

    # THE CLAIM: the tool result testifies the invocation it REPLAYED.
    assert retried["route"] == "route-a"
    assert retried["model"] == "model-old"
    assert retried["effort"] == "low"
    assert retried["root"] == str(root_a)

    # ... and so do the durable rows, attempt and custody alike.
    rows = [json.loads(line) for line
            in (drive / "logs" / "events.jsonl").read_text().splitlines() if line.strip()]
    attempts = [r for r in rows if r.get("type") == dc.START_REQUESTED
                and r.get("invocation_id") == token]
    started = [r for r in rows if r.get("type") == dc.STARTED
               and r.get("run_id") == retried["run_id"]][-1]
    original = attempts[0]
    for row in attempts[1:]:
        for fact in ("route", "project_id", "project_owned", "idempotency_key",
                     "max_seconds", "request"):
            assert row[fact] == original[fact], f"retry attempt re-derived {fact}"
    for fact, expected in (("route", "route-a"), ("model", "model-old"),
                           ("effort", "low"), ("root", str(root_a)),
                           ("project_id", prj_a), ("project_owned", True),
                           ("idempotency_key", original["idempotency_key"])):
        assert started[fact] == expected, f"custody row lies about {fact}: {started[fact]!r}"
    assert dc.replay(drive)[retried["run_id"]].model == "model-old"

    # 4. A DEFINITE refusal of a retry settles the STORED attempt's resources: the
    #    project the original start registered and owned is the one retired.
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "route-a=model-old:low")
    root_c = tmp_path / "root-c"; root_c.mkdir()
    script[:] = ["transport_error", "definite_refusal"]
    lost2 = json.loads(delegate._delegate_start(_ctx(root_c), "other work"))
    assert lost2["reason"] == "daemon_unreachable"
    prj_c = projects[str(root_c)]
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "route-b=model-new:high")
    refused = json.loads(delegate._delegate_start(
        _ctx(root_b), "other work", retry_of=lost2["pending_invocation_id"]))
    assert refused["status"] == "refused" and refused["project_retired"] is True
    assert removals == [prj_c], "the retired project is the stored attempt's own"
    delegate._CUSTODY.clear()


def test_custody_rows_outlive_the_child_drive_they_were_written_from(tmp_path, monkeypatch):
    """A live subagent runs on an isolated child drive that headless pruning DELETES, so a
    custody row written there cannot outlive the run it governs. The rows go to the
    canonical (budget) root instead — the existing SSOT for "survives the child" — and
    every fixture that passes only `drive_root` makes the two the same directory, so
    nothing here is proved unless the roots actually differ."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw
    from ouroboros.tools.registry import ToolContext

    canonical, child = tmp_path / "canonical", tmp_path / "child"
    child.mkdir(parents=True)
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _LiveRunStub())
    delegate._CUSTODY.clear()
    ctx = ToolContext(repo_dir=tmp_path, drive_root=child)
    ctx.task_id = "t-a"
    ctx.task_metadata = {"root_task_id": "t-a", "budget_drive_root": str(canonical)}

    assert json.loads(delegate._delegate_start(ctx, "review the diff"))["status"] == "started"
    assert (canonical / "logs" / "events.jsonl").exists(), "custody must live on the canonical root"
    assert not (child / "logs" / "events.jsonl").exists(), "not on the drive that gets pruned"

    import shutil

    shutil.rmtree(child)                # headless pruning reaps the child drive
    delegate._CUSTODY.clear()           # and the worker that held the memo is gone
    root = dc.custody_root(ctx)
    assert dc.lookup(root, "t-a", "run-live")[0] == dc.OWNED
    assert [c.run_id for c in dc.open_runs(root)] == ["run-live"]
    delegate._CUSTODY.clear()


def test_delegated_spend_settles_into_the_canonical_budget_ledger(tmp_path, monkeypatch):
    """P34R.1: `ledger_root` was stored from ctx.drive_root — the DISPOSABLE child
    drive on a split-root task — while the custody rows themselves already went to the
    canonical root. `settle_run` then wrote the subscription-session ledger row to
    `custody.ledger_root`, so the delegated spend never reached the canonical budget
    ledger and was erased with the child drive's pruning. The ledger row and the
    custody row must share the same durable root, and the durable STARTED row must
    NAME that root, because a restarted worker settles from the row, not from a ctx."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw
    from ouroboros.tools.registry import ToolContext

    canonical, child = tmp_path / "canonical", tmp_path / "child"
    child.mkdir(parents=True)
    canonical.mkdir(parents=True)

    class _Terminal(_LiveRunStub):
        def get_run(self, rid):
            return {"lastSeq": 3, "summary": {"state": "succeeded", "spendUsd": 1.25,
                                              "effectiveAccess": "readonly",
                                              "inputTokens": 10, "outputTokens": 5}}

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Terminal())
    delegate._CUSTODY.clear()
    ctx = ToolContext(repo_dir=tmp_path, drive_root=child)
    ctx.task_id = "t-a"
    ctx.task_metadata = {"root_task_id": "t-a", "budget_drive_root": str(canonical)}

    assert json.loads(delegate._delegate_start(ctx, "review the diff"))["status"] == "started"
    done = json.loads(delegate._delegate_wait(ctx, "run-live", wait_sec=1))
    assert done["settlement"]["settled"] is True
    assert done["settlement"]["ledger_recorded"] is True

    ledger = pathlib.Path("state") / "usage_attempts.jsonl"
    assert (canonical / ledger).exists(), \
        "delegated spend must land in the canonical budget ledger"
    assert not (child / ledger).exists(), \
        "never on the child drive that headless pruning deletes"
    rows = [json.loads(line) for line in (canonical / ledger).read_text().splitlines()
            if '"subscription_session"' in line]
    assert rows and rows[-1]["cost_usd"] == 1.25 and rows[-1]["cost_final"] is True
    started = [json.loads(line) for line
               in (canonical / "logs" / "events.jsonl").read_text().splitlines()
               if '"delegate_run_started"' in line][-1]
    assert started["ledger_root"] == str(dc.custody_root(ctx)), \
        "the durable row must name the canonical root, not the disposable child drive"
    delegate._CUSTODY.clear()


def test_durable_truncation_is_disclosed_never_a_bare_slice(tmp_path):
    """P34R.5: durable/cognitive surfaces in the delegation core hand-rolled `[:N]`
    slices — the containment-incident row cut its EVIDENCE at 500 chars with no
    marker at all, and the primary-output disclosure reason at 300. Every bound now
    goes through the shared `truncate_review_artifact` contract: the cut is marked,
    the original length is named, and the anti-waste floor never spends a marker
    longer than the text it saves."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate

    entry = dc.RunCustody(run_id="run-x", task_id="t-a", route_id="r")
    dc.record_containment_fault(tmp_path, entry, "cancel_unverified", "E" * 5000)
    fault = dc.open_containment_faults(tmp_path)[0]
    assert fault["detail"].startswith("E" * 2000)
    assert "OMISSION NOTE" in fault["detail"] and "original length 5000" in fault["detail"]

    # The anti-waste floor: a cut that saves fewer chars than its own marker
    # passes the text through whole instead of destroying it.
    entry2 = dc.RunCustody(run_id="run-y", task_id="t-a", route_id="r")
    dc.record_containment_fault(tmp_path, entry2, "cancel_unverified", "F" * 2010)
    fault2 = [f for f in dc.open_containment_faults(tmp_path) if f["run_id"] == "run-y"][0]
    assert fault2["detail"] == "F" * 2010

    class _Boom:
        def get_run_artifact(self, rid, path):
            raise RuntimeError("Z" * 900)

    primary = {"truncated": True, "path": "out.md", "bytes": 10, "text": "abc"}
    _resolved_primary, ok, disclosure = delegate._resolve_full_primary_output(
        _Boom(), "run-x", primary)
    assert ok is False
    assert "OMISSION NOTE" in disclosure["reason"] and "original length" in disclosure["reason"]


def test_an_absent_run_closes_only_after_its_registration_is_discharged(tmp_path):
    """P34R.4: `close_absent_run` emitted CLOSED_ABSENT even when `retire_project`
    failed and left `project_owned=True`; replay then cleared ownership wholesale, so
    the failed retirement was never retried and the owned daemon registration leaked
    PERMANENTLY. The absent-run fact and the registration obligation are two different
    things: custody now closes only once the obligation is discharged, the deferred
    close stays in open_runs (disclosed by PROJECT_RETIRE_FAILED), and the next sweep
    retries. A 404 on the REMOVE counts as discharged — absence is discharge."""
    import ouroboros.delegate_custody as dc
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    class _AbsentRunGateway:
        """get_run 404s (run gone); remove_project is temporarily unreachable."""
        def __init__(self): self.removals, self.remove_fails = [], True
        def handshake(self): return {}
        def get_run(self, rid):
            raise ClaudexorUnavailable("not_found", "no such run", status_code=404)
        def remove_project(self, pid):
            self.removals.append(pid)
            if self.remove_fails:
                raise ClaudexorUnavailable("daemon_unreachable", "socket died", status_code=0)
        def close(self): pass

    gateway = _AbsentRunGateway()
    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-gone", task_id="t-a", route_id="r", model="m",
        project_id="prj-owned", project_owned=True, ledger_root=str(tmp_path)))
    dc._CUSTODY.clear()

    # 1. Retirement unreachable: the close is DEFERRED, not faked.
    out = dc.reconcile_orphaned_runs(tmp_path, set(), gateway_factory=lambda: gateway)
    assert [o["action"] for o in out] == ["absent"]
    kinds = _event_types(tmp_path)
    assert "delegate_run_project_retire_failed" in kinds, "the failure is disclosed"
    assert "delegate_run_closed_absent" not in kinds, \
        "custody must not close over an undischarged registration"
    open_now = dc.open_runs(tmp_path)
    assert [c.run_id for c in open_now] == ["run-gone"] and open_now[0].project_owned is True

    # 2. The daemon recovers: the retry discharges the obligation and ONLY THEN closes.
    gateway.remove_fails = False
    dc._CUSTODY.clear()
    out = dc.reconcile_orphaned_runs(tmp_path, set(), gateway_factory=lambda: gateway)
    assert [o["action"] for o in out] == ["absent"]
    kinds = _event_types(tmp_path)
    assert "delegate_run_closed_absent" in kinds and "delegate_run_project_retired" in kinds
    assert dc.open_runs(tmp_path) == []
    assert gateway.removals == ["prj-owned", "prj-owned"], "the retirement was RETRIED"

    # 3. Absence is discharge: a 404 on the remove itself closes the run.
    class _AllGone(_AbsentRunGateway):
        def remove_project(self, pid):
            raise ClaudexorUnavailable("not_found", "no such project", status_code=404)

    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-gone-2", task_id="t-b", route_id="r", model="m",
        project_id="prj-2", project_owned=True, ledger_root=str(tmp_path)))
    dc._CUSTODY.clear()
    out = dc.reconcile_orphaned_runs(tmp_path, set(), gateway_factory=lambda: _AllGone())
    assert [o["action"] for o in out] == ["absent"]
    assert dc.open_runs(tmp_path) == []
    dc._CUSTODY.clear()


def test_an_unresolved_containment_fault_cannot_age_out_of_the_health_view(tmp_path):
    """P34R.3: `open_containment_faults` scanned only the last 4 MB of the canonical
    event log, so an UNRESOLVED containment fault — an overpowered run that may still
    be live — silently vanished from the health invariants once later unrelated
    traffic buried its row, despite the stated contract that it stays CRITICAL until
    a terminal receipt resolves it. Incidents now live in their own compact durable
    projection that is read WHOLE; the event-log tail remains as the fallback surface
    for a fault whose compact write failed."""
    import ouroboros.delegate_custody as dc

    entry = dc.RunCustody(run_id="run-fault", task_id="t-a", route_id="r")
    dc.record_containment_fault(tmp_path, entry, "cancel_unverified", "engine went dark")

    # Bury the fault under MORE than the tail window of later unrelated custody rows.
    noise = json.dumps({"type": "delegate_run_reconciled", "run_id": "run-noise",
                        "task_id": "t-b", "pad": "x" * 1500})
    events = dc.event_log_path(tmp_path)
    with events.open("a", encoding="utf-8") as fh:
        for _ in range(3000):
            fh.write(noise + "\n")
    assert events.stat().st_size > dc._FAULT_SCAN_TAIL_BYTES, "the fault is outside the tail"

    open_faults = dc.open_containment_faults(tmp_path)
    assert [f["run_id"] for f in open_faults] == ["run-fault"], \
        "an unresolved incident must never age out of the health view"
    assert open_faults[0]["reason"] == "cancel_unverified"

    # A resolution clears it durably, and later noise cannot reopen it.
    dc.resolve_containment_fault(tmp_path, entry, "verified_terminal")
    assert dc.open_containment_faults(tmp_path) == []
    with events.open("a", encoding="utf-8") as fh:
        for _ in range(200):
            fh.write(noise + "\n")
    assert dc.open_containment_faults(tmp_path) == []

    # Fallback surface: a fault whose COMPACT write failed is still visible through
    # the event-log tail — either landing alone keeps the incident visible.
    other = tmp_path / "other-drive"
    (other / "logs").mkdir(parents=True)
    dc._faults_path(other).mkdir()          # the compact append will fail loudly
    dc.record_containment_fault(other, entry, "cancel_unreachable", "")
    assert [f["run_id"] for f in dc.open_containment_faults(other)] == ["run-fault"]


def test_every_pre_custody_exit_names_the_registration_it_created(tmp_path, monkeypatch):
    """P34P1.7: a registration created before start_run is retired on every TYPED
    pre-custody exit, but an UNTYPED one — a bug here, a timeout, a signal — left the
    durable trail with a bare `start_requested` row and no disposition. The row already
    named the project (so the reviewer's "permanently orphaned" was not literally true,
    proven by execution), but nothing said the attempt had ended, so a reader could not
    tell a live start from a dead one.

    The registration is still NOT retired on an untyped exit: that outcome says nothing
    about whether the POST reached the daemon, and destroying state on missing
    information is the one thing this module forbids. It is NAMED, with a typed reason,
    and the exception continues on its way — disclosure, not a swallow."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    class _Untyped(_LiveRunStub):
        removed: list = []

        def find_project_id(self, root): return ""
        def register_project(self, root): return "prj-owned"
        def remove_project(self, pid): _Untyped.removed.append(pid)
        def start_run(self, request, *, idempotency_key=""):
            raise MemoryError("an untyped failure between register_project and custody")

    stub = _Untyped()
    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: stub)
    delegate._CUSTODY.clear()

    with pytest.raises(MemoryError):
        delegate._delegate_start(_nanny_ctx(tmp_path), "work")

    rows = [json.loads(l) for l in
            (tmp_path / "logs" / "events.jsonl").read_text().splitlines() if l.strip()]
    failed = [r for r in rows if r.get("type") == dc.START_FAILED]
    assert [r["project_id"] for r in failed] == ["prj-owned"]
    assert failed[0]["reason"] == "pre_custody_exit_MemoryError"
    assert failed[0]["definite"] is False, "an untyped exit is not a definite refusal"
    assert failed[0]["project_retired"] is False
    assert failed[0]["invocation_id"], "the invocation is named, so it can be recovered"
    assert stub.removed == [], "an unknown outcome never destroys the registration"

    # The invocation stays recoverable by the durable sweep (P34R.2), which is what
    # makes retaining the registration the right answer rather than a leak.
    pending = dc.pending_invocations(tmp_path)
    assert [p["project_id"] for p in pending] == ["prj-owned"]
    delegate._CUSTODY.clear()


def test_reconciliation_recovers_a_pending_invocation_whose_worker_died(tmp_path, monkeypatch):
    """P34R.2: /v2/runs accepts the POST, the response is lost, and the worker dies
    before record_started — only the START_REQUESTED row remains. The run-keyed sweep
    could not see it: a live mutating run stayed uncollected FOREVER, and the retry
    token never reached any model. The durable sweep now recovers pending invocations
    on the SAME owner-is-gone predicate: the stored canonical body is re-POSTed under
    the invocation's own wire key (the engine replay returns the ORIGINAL handle), the
    recovered run gets its custody row from the stored invocation facts, and the
    ordinary settle-or-cancel path collects it. Negative shapes: a live owner's pending
    invocation is untouched; a definite refusal retires the invocation AND the
    registration the original attempt owned; an unreachable daemon leaves it pending."""
    import httpx

    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    script = ["transport_error"]
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/handshake":
            return httpx.Response(200, json={"protocolMajor": CLAUDEXOR_PROTOCOL_MAJOR,
                                             "compatible": True,
                                             "engine": {"version": CLAUDEXOR_MIN_VERSION}})
        if path == "/v2/agent-capabilities":
            return httpx.Response(200, json={"harnesses": [
                {"id": "some-route", "enabled": True, "status": "ok",
                 "accessProfilesSupported": ["readonly"]}]})
        if path == "/v2/quota":
            return httpx.Response(200, json={"snapshots": []})
        if path == "/v2/projects":
            return httpx.Response(200, json={"projects": []}) if request.method == "GET" \
                else httpx.Response(200, json={"id": "prj-owned"})
        assert path == "/v2/runs", path
        posted.append((request.headers.get("Idempotency-Key"), json.loads(request.read())))
        if script.pop(0) == "transport_error":
            raise httpx.ConnectError("daemon fell over mid-POST")
        return httpx.Response(200, json={"runId": "run-recovered"})

    real_gateway = cx.ClaudexorGateway

    def _fresh(*_a, **_k):
        gateway = real_gateway(cx.DaemonEndpoint("127.0.0.1", 1, "secret-token"))
        gateway._client = httpx.Client(base_url="http://127.0.0.1:1",
                                       transport=httpx.MockTransport(handler),
                                       headers=dict(gateway._client.headers))
        return gateway

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")
    monkeypatch.setattr(gw, "ClaudexorGateway", _fresh)
    delegate._CUSTODY.clear()
    ctx = _nanny_ctx(tmp_path)

    # The durable residue of the crash, produced through the REAL path: an accepted
    # POST whose response was lost. Only START_REQUESTED names the invocation.
    lost = json.loads(delegate._delegate_start(ctx, "the intended work", max_seconds=60))
    token = lost["pending_invocation_id"]
    delegate._CUSTODY.clear()            # the worker that knew the token is gone
    assert [r["invocation_id"] for r in dc.pending_invocations(tmp_path)] == [token]
    assert dc.open_runs(tmp_path) == [], "no run row exists: the run-keyed sweep is blind here"

    # 1. The owner is ALIVE: its pending invocation is untouched (the owner holds
    #    the retry token and decides).
    assert dc.reconcile_orphaned_runs(tmp_path, {"t-a"}, gateway_factory=_fresh) == []
    assert len(posted) == 1

    # 2. The owner is GONE: the sweep replays the stored body under the stored key,
    #    the daemon returns the run it (now) holds, and the ordinary path collects it.
    class _TerminalRecovery:
        removed: list = []

        def handshake(self): return {}
        def start_run(self, request, *, idempotency_key=""):
            posted.append((idempotency_key, dict(request)))
            return {"runId": "run-recovered"}
        def get_run(self, rid):
            return {"lastSeq": 2, "summary": {"state": "succeeded", "spendUsd": 0.5,
                                              "effectiveAccess": "readonly"}}
        def remove_project(self, pid): _TerminalRecovery.removed.append(pid)
        def close(self): pass

    outcomes = dc.reconcile_orphaned_runs(tmp_path, set(), gateway_factory=lambda: _TerminalRecovery())
    assert [o["action"] for o in outcomes] == ["settled"] and outcomes[0]["settled"] is True
    key, body = posted[-1]
    assert key == token, "recovery must present the invocation's own wire key"
    assert body == posted[0][1], "recovery must replay the RECORDED canonical body"
    started = [json.loads(line) for line
               in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
               if '"delegate_run_started"' in line][-1]
    assert started["run_id"] == "run-recovered"
    assert started["recovered_from_pending_invocation"] is True
    assert started["route"] == "some-route" and started["model"] == "weak-model"
    assert started["idempotency_key"], "the stored lookup key rides the recovered row"
    assert dc.pending_invocations(tmp_path) == [], "a recovered invocation is bound, not pending"
    again = dc.reconcile_orphaned_runs(tmp_path, set(), gateway_factory=lambda: _TerminalRecovery())
    assert again == [], "a settled recovery does not repeat"

    # 3. A DEFINITE refusal at recovery retires the invocation and the registration
    #    the original attempt owned; an unreachable daemon leaves it pending.
    script[:] = ["transport_error"]
    lost2 = json.loads(delegate._delegate_start(ctx, "other intended work"))
    token2 = lost2["pending_invocation_id"]
    delegate._CUSTODY.clear()

    class _Refusing:
        def __init__(self): self.removed = []
        def handshake(self): return {}
        def start_run(self, request, *, idempotency_key=""):
            raise ClaudexorUnavailable("bad_request", "no", status_code=400)
        def remove_project(self, pid): self.removed.append(pid)
        def close(self): pass

    class _Unreachable:
        def handshake(self): return {}
        def start_run(self, request, *, idempotency_key=""):
            raise ClaudexorUnavailable("daemon_unreachable", "down", status_code=0)
        def close(self): pass

    down = dc.reconcile_orphaned_runs(tmp_path, set(), gateway_factory=lambda: _Unreachable())
    assert [o["action"] for o in down] == ["recovery_unreachable"]
    assert [r["invocation_id"] for r in dc.pending_invocations(tmp_path)] == [token2], \
        "an unknown outcome never destroys the invocation"
    refusing = _Refusing()
    gone = dc.reconcile_orphaned_runs(tmp_path, set(), gateway_factory=lambda: refusing)
    assert [o["action"] for o in gone] == ["invocation_retired"]
    assert refusing.removed == ["prj-owned"], "the ORIGINAL attempt's owned registration is discharged"
    assert dc.pending_invocations(tmp_path) == []
    assert dc.invocation_record(tmp_path, token2)["state"] == "failed_definite"
    delegate._CUSTODY.clear()


def test_a_start_whose_custody_row_did_not_land_does_not_claim_to_be_custodied(tmp_path, monkeypatch):
    """`append_jsonl` returns whether the write landed precisely so important events can be
    handled rather than pretended; custody discarded that signal and logged the loss at
    DEBUG. The write that IS the new SSOT was therefore best-effort: a failed row left a
    LIVE overpowered run that only this process could name — the exact leak the module
    exists to close, silently reintroduced under the fix.

    Only the STARTED (and, for the twin check, SETTLED) appends fail here: a failed
    START_REQUESTED row now refuses the launch before any POST
    (test_no_post_fires_when_the_start_request_row_did_not_land), so the uncustodied
    shape this test pins is the narrower one — the request row landed, the run really
    started, and the row that IS custody did not land."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _LiveRunStub())
    real_append = dc.append_jsonl

    def _started_row_lost(path, obj):
        if obj.get("type") in ("delegate_run_started", "delegate_run_settled"):
            return False
        return real_append(path, obj)

    monkeypatch.setattr(dc, "append_jsonl", _started_row_lost)
    delegate._CUSTODY.clear()
    out = json.loads(delegate._delegate_start(_nanny_ctx(tmp_path), "review the diff"))
    delegate._CUSTODY.clear()

    assert out["run_id"] == "run-live", "the run really did start; that is not in doubt"
    assert out["custody_durable"] is False
    assert out["status"] == "started_uncustodied", (
        "a start nothing outside this worker can name must not wear the plain name")
    assert "CUSTODY IS NOT DURABLE" in out["note"]
    assert dc.lookup(tmp_path, "t-a", "run-live")[0] == dc.UNKNOWN, "the premise of the claim"

    # The twin surface, the same predicate: `settled` means "the durable fact exists". A
    # settlement whose row never landed stays retryable instead of closing custody on a
    # claim that dies with this process.
    entry = dc.RunCustody(run_id="run-2", task_id="t-a", route_id="r", model="m",
                          project_id="p", project_owned=False, ledger_root=str(tmp_path))
    entry.ledger_recorded = True
    settlement = dc.settle_run(tmp_path, _LiveRunStub(), entry,
                               {"summary": {"state": "succeeded", "spendUsd": 0.0}})
    assert settlement["settled"] is False and entry.settled is False


@pytest.mark.parametrize("status_code,retired,remove_absent", [
    (422, True, False),     # the daemon ANSWERED and refused: no run was bound
    (0, False, False),      # transport error: the POST's fate is unknown, a run may be live
    (503, False, False),    # 5xx: same — an unverified outcome is not grounds to destroy state
    # The daemon has no such registration: absence IS discharge, the same answer
    # `retire_project` settles on, not a failure to report.
    (422, True, True),
])
def test_a_failed_start_does_not_leave_the_registration_it_created(
    tmp_path, monkeypatch, status_code, retired, remove_absent,
):
    """The project is registered BEFORE `start_run`. A start failure used to leave that
    registration behind with nothing anywhere naming its id — and the id must be durably
    named whether or not the registration can be safely retired."""
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    live = {"prj-new"}

    class _Stub(_LiveRunStub):
        def find_project_id(self, root): return ""
        def register_project(self, root): return "prj-new"
        def remove_project(self, pid):
            if remove_absent:
                live.discard(pid)   # it was never there to begin with
                raise gw.ClaudexorUnavailable("project_not_found", "gone", status_code=404)
            live.discard(pid)
        def start_run(self, request, *, idempotency_key=""):
            raise gw.ClaudexorUnavailable("run_start_failed", "no run", status_code=status_code)

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    delegate._CUSTODY.clear()
    out = json.loads(delegate._delegate_start(_nanny_ctx(tmp_path), "x"))
    delegate._CUSTODY.clear()
    assert out["status"] == "refused" and out["reason"] == "run_start_failed"
    assert out["project_retired"] is retired, out
    assert (live == set()) is retired, "only a definite refusal may retire the registration"
    if not retired:
        assert out["project_retention_reason"] == "start_outcome_unknown_run_may_exist"
    rows = [json.loads(l) for l in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    named = [r for r in rows if r.get("type") == "delegate_run_start_failed"]
    assert named and named[0]["project_id"] == "prj-new", "the id must be durably named"


def test_a_queued_handle_with_no_run_id_names_its_registration_like_its_twin(tmp_path, monkeypatch):
    """The untreated twin of the branch above. Here the POST SUCCEEDED (2xx) and only the
    handle was unusable, so a run is MORE likely live against the registration — yet this
    branch retired nothing and durably named nothing, and with no run id the orphan
    reconciler can never see it either. Both branches now leave the same durable trace."""
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    live = {"prj-new"}

    class _Stub(_LiveRunStub):
        def find_project_id(self, root): return ""
        def register_project(self, root): return "prj-new"
        def remove_project(self, pid): live.discard(pid)
        def start_run(self, request, *, idempotency_key=""): return {"status": "queued"}

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    delegate._CUSTODY.clear()
    out = json.loads(delegate._delegate_start(_nanny_ctx(tmp_path), "x"))
    delegate._CUSTODY.clear()
    assert out["reason"] == "queued_without_run_id"
    assert out["project_id"] == "prj-new", "the retained registration must be named"
    assert out["project_retired"] is False and live == {"prj-new"}, (
        "an accepted POST is never grounds to destroy the registration a run may use")
    assert out["project_retention_reason"] == "start_outcome_unknown_run_may_exist"
    rows = [json.loads(l) for l in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    named = [r for r in rows if r.get("type") == "delegate_run_start_failed"]
    assert named and named[0]["project_id"] == "prj-new", "the id must be durably named"
    assert named[0]["reason"] == "queued_without_run_id"


# -- 3.9 cancellation reports only what it verified ----------------------------


@pytest.mark.parametrize("accepted,state,expected,may_be_live", [
    (True, "cancelled", "confirmed", False),
    (True, "running", "requested", True),
    (False, "running", "failed", True),
])
def test_cancel_never_claims_more_than_a_terminal_receipt_proves(
    tmp_path, monkeypatch, accepted, state, expected, may_be_live,
):
    """`status: cancelled` used to be returned for all of these — including a daemon
    that REFUSED the control while the run kept mutating."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    class _Stub(_LiveRunStub):
        def cancel_run(self, rid, reason=""):
            return {"accepted": accepted, "status": "accepted" if accepted else "rejected"}
        def get_run(self, rid):
            return {"lastSeq": 3, "summary": {"state": state, "spendUsd": 0.0}}

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    delegate._CUSTODY.clear()
    delegate._CUSTODY["run-1"] = delegate._RunCustody(
        run_id="run-1", task_id="t-a", route_id="r", model="m", project_id="p", project_owned=False)
    out = json.loads(delegate._delegate_cancel(_nanny_ctx(tmp_path), "run-1", reason="stuck"))
    delegate._CUSTODY.clear()
    assert out["status"] == expected, out
    assert out["run_may_still_be_live"] is may_be_live, out
    faults = dc.open_containment_faults(tmp_path)
    assert bool(faults) is (expected == "failed"), (expected, faults)


def test_an_unverifiable_cancel_is_a_loud_durable_incident(tmp_path, monkeypatch):
    """A cancel that never reached the daemon left a typed refusal and nothing else: an
    overpowered mutating run stayed live with no durable trace and no owner-visible
    signal. It is now a containment fault that rides the health invariants until a
    terminal receipt clears it."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    class _Deaf(_LiveRunStub):
        def cancel_run(self, rid, reason=""):
            raise gw.ClaudexorUnavailable("daemon_unreachable", "connection refused")

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Deaf())
    delegate._CUSTODY.clear()
    delegate._CUSTODY["run-1"] = delegate._RunCustody(
        run_id="run-1", task_id="t-a", route_id="r", model="m", project_id="p", project_owned=False)
    out = json.loads(delegate._delegate_cancel(_nanny_ctx(tmp_path), "run-1"))
    assert out["status"] == "containment_fault_run_may_still_be_live", out
    assert out["run_may_still_be_live"] is True
    faults = dc.open_containment_faults(tmp_path)
    assert [f["run_id"] for f in faults] == ["run-1"], faults

    invariants = _health_invariants(tmp_path)
    assert "DELEGATED RUN MAY STILL BE LIVE" in invariants, invariants
    assert "run-1" in invariants

    # A later VERIFIED terminal receipt clears the incident — the fault is a live
    # condition, not a permanent scar.
    class _Stopped(_LiveRunStub):
        def cancel_run(self, rid, reason=""): return {"accepted": True, "status": "accepted"}
        def get_run(self, rid):
            return {"lastSeq": 4, "summary": {"state": "cancelled", "spendUsd": 0.0}}

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stopped())
    again = json.loads(delegate._delegate_cancel(_nanny_ctx(tmp_path), "run-1"))
    delegate._CUSTODY.clear()
    assert again["status"] == "confirmed", again
    assert dc.open_containment_faults(tmp_path) == []
    assert "DELEGATED RUN MAY STILL BE LIVE" not in _health_invariants(tmp_path)


def test_cancelling_a_run_this_module_already_settled_is_not_an_incident(tmp_path, monkeypatch):
    """`settle_run` short-circuits on `custody.settled`; its twin `cancel_and_verify` never
    consulted it, and its `cancel_run` failure branch declared a containment fault WITHOUT
    reading the run — with the read three lines below, unused. So an ordinary cancel of an
    already-settled run (the daemon answers 409 `run_already_terminal`) manufactured a
    permanent CRITICAL against a run this very module had recorded as closed."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    class _Finished(_LiveRunStub):
        def get_run(self, rid):
            return {"lastSeq": 9, "summary": {"state": "succeeded", "spendUsd": 0.0,
                                              "inputTokens": 1, "outputTokens": 1}}
        def cancel_run(self, rid, reason=""):
            raise gw.ClaudexorUnavailable("run_already_terminal", "conflict", status_code=409)

    class _Deaf(_LiveRunStub):
        def cancel_run(self, rid, reason=""):
            raise gw.ClaudexorUnavailable("daemon_unreachable", "connection refused")
        def get_run(self, rid):
            raise gw.ClaudexorUnavailable("daemon_unreachable", "connection refused")

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Finished())
    delegate._CUSTODY.clear()
    entry = delegate._RunCustody(run_id="run-1", task_id="t-a", route_id="r", model="m",
                                 project_id="p", project_owned=False, root_task_id="t-a",
                                 ledger_root=str(tmp_path))
    dc.record_started(tmp_path, entry)
    ctx = _nanny_ctx(tmp_path)
    assert json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))["settlement"]["settled"] is True

    # The daemon then goes away entirely — the common shape, since a finished run is often
    # the last thing it did. Nothing can be read back, so only the durable settlement this
    # module already wrote can answer, and it does.
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Deaf())
    after_settlement = json.loads(delegate._delegate_cancel(ctx, "run-1", reason="ordinary"))
    assert after_settlement["status"] == "confirmed", after_settlement
    assert after_settlement["run_may_still_be_live"] is False
    assert dc.open_containment_faults(tmp_path) == []
    assert "DELEGATED RUN MAY STILL BE LIVE" not in _health_invariants(tmp_path)

    # The other half of the same defect, on a run with NO settlement to short-circuit on:
    # the refused control is not a verdict about the RUN, so the state read decides, and a
    # run that has already stopped is confirmed rather than faulted.
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Finished())
    dc.record_started(tmp_path, delegate._RunCustody(
        run_id="run-2", task_id="t-a", route_id="r", model="m", project_id="p",
        project_owned=False, root_task_id="t-a", ledger_root=str(tmp_path)))
    unsettled = json.loads(delegate._delegate_cancel(ctx, "run-2", reason="stuck"))
    delegate._CUSTODY.clear()
    assert unsettled["status"] == "confirmed", unsettled
    assert dc.open_containment_faults(tmp_path) == []
    assert dc.replay(tmp_path)["run-2"].settled is True, "the read that confirmed it also settles it"


def _health_invariants(tmp_path):
    """Run the real health-invariant builder over a drive with nothing else in it."""
    from ouroboros.context import build_health_invariants

    class _Env:
        drive_root = tmp_path

        def drive_path(self, rel=""):
            return tmp_path / rel

        def repo_path(self, rel=""):
            return tmp_path / "repo" / rel

    return build_health_invariants(_Env())


# -- 3.10 settlement is atomic --------------------------------------------------


def test_settlement_claims_terminal_only_when_the_durable_facts_landed(tmp_path, monkeypatch):
    """A failed project retirement was suppressed and `settled=True` written anyway, so
    the retry that would have released it could never happen. Both obligations are
    idempotent, so an unfinished settlement is simply retried — and the retry must not
    double-write the ledger row."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    failing = {"now": True}

    class _Stub(_LiveRunStub):
        def get_run(self, rid):
            return {"lastSeq": 9, "summary": {"state": "succeeded", "spendUsd": 0.0,
                                              "inputTokens": 3, "outputTokens": 2}}
        def remove_project(self, pid):
            if failing["now"]:
                raise gw.ClaudexorUnavailable("daemon_unreachable", "cannot retire")

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    delegate._CUSTODY.clear()
    entry = delegate._RunCustody(
        run_id="run-1", task_id="t-a", route_id="r", model="m",
        project_id="prj-ours", project_owned=True, root_task_id="t-a", ledger_root=str(tmp_path))
    assert dc.record_started(tmp_path, entry) is True, "the authoritative row must land"
    ctx = _nanny_ctx(tmp_path)

    first = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))
    assert first["settlement"]["settled"] is False, "a failed retirement is not a settlement"
    assert entry.settled is False and entry.project_owned is True
    assert "delegate_run_settled" not in _event_types(tmp_path)

    failing["now"] = False
    second = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))
    delegate._CUSTODY.clear()
    assert second["settlement"]["settled"] is True, "the retry must be able to finish"
    assert "delegate_run_settled" in _event_types(tmp_path)
    rows = [json.loads(l) for l
            in (tmp_path / "state" / "usage_attempts.jsonl").read_text().splitlines()]
    sessions = [r for r in rows if r.get("kind") == "subscription_session"]
    assert len(sessions) == 1, "the idempotent ledger row must not be written twice"
    assert dc.replay(tmp_path)["run-1"].settled is True

    # An idempotent re-start writes a SECOND started row for the same run. Replaying it
    # must not forget the settlement, or the orphan sweep would be handed a run that has
    # already finished and would try to cancel and re-retire it forever.
    dc.record_started(tmp_path, delegate._RunCustody(
        run_id="run-1", task_id="t-a", route_id="r", model="m",
        project_id="prj-ours", project_owned=True, root_task_id="t-a", ledger_root=str(tmp_path)))
    delegate._CUSTODY.clear()
    assert dc.replay(tmp_path)["run-1"].settled is True
    assert "run-1" not in {c.run_id for c in dc.open_runs(tmp_path)}


def test_a_retirement_that_landed_is_not_replayed_as_still_owned(tmp_path, monkeypatch):
    """Settlement's two obligations can fail independently. When the RETIREMENT landed
    and the ledger write did not, the durable replay must know the registration is gone
    — otherwise a restart retries `remove_project` on an already-removed project and the
    settlement can never complete."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    import ouroboros.usage_accounting as ua
    from ouroboros.gateways import claudexor as gw

    removed = []

    class _Stub(_LiveRunStub):
        def get_run(self, rid):
            return {"lastSeq": 9, "summary": {"state": "succeeded", "spendUsd": 0.0}}
        def remove_project(self, pid): removed.append(pid)

    def _boom(*a, **k):
        raise ua.UsageAccountingError("usage accounting lock unavailable")

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    monkeypatch.setattr(ua, "record_subscription_session", _boom)
    delegate._CUSTODY.clear()
    dc.record_started(tmp_path, delegate._RunCustody(
        run_id="run-1", task_id="t-a", route_id="r", model="m",
        project_id="prj-ours", project_owned=True, root_task_id="t-a", ledger_root=str(tmp_path)))
    json.loads(delegate._delegate_wait(_nanny_ctx(tmp_path), "run-1", wait_sec=1))
    delegate._CUSTODY.clear()          # the worker restarts

    replayed = dc.replay(tmp_path)["run-1"]
    assert removed == ["prj-ours"]
    assert replayed.project_owned is False, "a retirement that landed must replay as landed"
    assert replayed.ledger_recorded is False and replayed.settled is False


# -- 3.11 a large result is delivered, not severed -----------------------------


def test_a_large_delegated_result_is_delivered_whole_or_declared_partial(tmp_path, monkeypatch):
    """`final_summary`/`primary_output` carry the run's real work product and Claudexor
    returns up to 256 KiB. The 15k head-truncation cut it mid-string and destroyed the
    JSON, so a large review came back as an unparseable fragment that still looked like
    a verdict. The payload now bounds ITSELF and the remainder is a readable artifact."""
    from ouroboros.loop_tool_execution import _truncate_tool_result
    from ouroboros.tool_capabilities import tool_result_limit

    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    verdict = "V" * 120_000

    class _Stub(_LiveRunStub):
        def get_run(self, rid):
            return {"lastSeq": 9, "primaryOutput": verdict,
                    "finalSummary": "S" * 60_000,
                    "outcomeBanner": "B" * 40_000,
                    "summary": {"state": "succeeded", "spendUsd": 0.0}}

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    delegate._CUSTODY.clear()
    delegate._CUSTODY["run-1"] = delegate._RunCustody(
        run_id="run-1", task_id="t-a", route_id="r", model="m", project_id="p", project_owned=False)
    ctx = _nanny_ctx(tmp_path)
    raw = delegate._delegate_wait(ctx, "run-1", wait_sec=1)
    delegate._CUSTODY.clear()

    limit = tool_result_limit("delegate_wait")
    assert len(raw) <= limit, "the producer must fit the budget the truncator applies"
    assert _truncate_tool_result(raw, "delegate_wait", {}) == raw, "outer truncation must not fire"
    payload = json.loads(raw)          # the fatal symptom: this used to be unparseable

    delivery = payload["output_delivery"]
    assert delivery["complete"] is False and delivery["consumed"] is False
    assert "primary_output" not in payload, "a preview must not wear the whole field's name"
    assert payload["primary_output_preview"] and payload["primary_output_preview"] in verdict

    artifact = delivery["artifact"]
    assert artifact["root"] == "task_drive"
    staged = pathlib.Path(artifact["abs_path"]).read_text(encoding="utf-8")
    assert json.loads(staged)["primary_output"] == verdict, "the whole result must survive"
    assert delivery["read_next"]["tool"] == "read_file"

    # The advertised chunk read really works, with a stable cursor over an immutable
    # file — and it works for the READ-ONLY nanny, which is the common caller and the
    # one whose access policy could have made the whole contract unreachable.
    from ouroboros.tool_access import LOCAL_READONLY_SUBAGENT_MODE
    from ouroboros.tools.core import _read_file
    from ouroboros.contracts.task_constraint import TaskConstraint

    ctx.task_constraint = TaskConstraint(mode=LOCAL_READONLY_SUBAGENT_MODE)
    head = _read_file(ctx, path=artifact["path"], root="task_drive", start_line=1, max_lines=5)
    tail = _read_file(ctx, path=artifact["path"], root="task_drive",
                      start_line=artifact["lines"], max_lines=5)
    assert "BLOCKED" not in head and "NOT_FOUND" not in head and "ERROR" not in head
    assert head != tail, "start_line must be a real cursor, not a no-op"


def _read_artifact_whole(ctx, artifact, step=7):
    """Cover the staged artifact contiguously, like a real reader: line windows, plus
    the start_char sub-line cursor for any line longer than the delivery budget (a cut
    window only credits the delivered prefix)."""
    from ouroboros.tool_capabilities import tool_result_limit
    from ouroboros.tools.core import _read_file

    stride = tool_result_limit("read_file") - 5_000
    lines = pathlib.Path(artifact["abs_path"]).read_text(encoding="utf-8").splitlines(keepends=True)
    for line_no, line in enumerate(lines, start=1):
        offset = 0
        while offset == 0 or offset < len(line):
            _read_file(ctx, path=artifact["path"], root="task_drive",
                       start_line=line_no, max_lines=1, start_char=offset)
            offset += stride


def test_the_coverage_ack_binds_to_what_delivery_actually_hands_the_model(
        tmp_path, monkeypatch):
    """P34R.7 (scope reviewer, p34.part2 gate) claimed the ack credits characters the
    delivery layer cuts, because it runs before _annotate_reread and the 80K cap. The
    executed probe REFUTED it: the reread note is APPENDED and the outer truncator
    KEEPS THE HEAD (s[:limit]), so the note can only lose its own tail — it never
    displaces body characters — and the ack's budget math mirrors the real truncator
    to the character. This test PINS that equivalence on the real seam (tool ->
    annotation -> real _truncate_tool_result), so a future reordering — prepending
    the note, a tail-keep truncator, a second budget constant — cannot silently turn
    the rejected finding true: on every shape, the interval the ack credits must not
    exceed the window-body characters actually present in the delivered string."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw
    from ouroboros.loop_tool_execution import _truncate_tool_result
    from ouroboros.tool_capabilities import tool_result_limit
    from ouroboros.tools.core import _read_file

    budget = tool_result_limit("read_file")

    class _Stub(_LiveRunStub):
        def get_run(self, rid):
            return {"lastSeq": 9, "primaryOutput": "V" * (budget * 2),
                    "summary": {"state": "succeeded", "spendUsd": 0.0}}

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    delegate._CUSTODY.clear()
    delegate._READ_COVERAGE.clear()
    dc.record_started(tmp_path, delegate._RunCustody(
        run_id="run-1", task_id="t-a", route_id="r", model="m",
        project_id="p", project_owned=False, root_task_id="t-a", ledger_root=str(tmp_path)))
    ctx = _nanny_ctx(tmp_path)
    artifact = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1)
                          )["output_delivery"]["artifact"]
    content = pathlib.Path(artifact["abs_path"]).read_text(encoding="utf-8")
    import hashlib as _hl
    identity = (f"{pathlib.Path(artifact['abs_path']).resolve()}|"
                f"{_hl.sha256(content.encode('utf-8', 'replace')).hexdigest()}")
    lines = content.splitlines(keepends=True)
    long_no, long_line = max(enumerate(lines, start=1), key=lambda p: len(p[1]))
    assert len(long_line) > budget + 1000

    def delivered_body(delivered, window_body, hdr):
        if hdr not in delivered:
            return 0
        after = delivered.split(hdr, 1)[1]
        lo, hi, best = 0, min(len(after), len(window_body)), 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if after.startswith(window_body[:mid]):
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        return best

    def call(start_char):
        before = sum(b - a for a, b in delegate._READ_COVERAGE.get(identity, []))
        result = _read_file(ctx, path=artifact["path"], root="task_drive",
                            start_line=long_no, max_lines=1, start_char=start_char)
        delivered = _truncate_tool_result(result, "read_file",
                                          {"path": artifact["path"], "root": "task_drive"})
        after = sum(b - a for a, b in delegate._READ_COVERAGE.get(identity, []))
        hdr = result.split("\n", 1)[0] + "\n"
        return result, delivered_body(delivered, long_line[start_char:], hdr), after - before

    # Shape A: rendering just under the budget; the repeat's appended note pushes the
    # annotated result over it — the rejected finding's exact scenario.
    offset = len(long_line) - (budget - 200)
    r1, d1, c1 = call(offset)
    assert len(r1) <= budget and c1 <= d1, (c1, d1)
    r2, d2, c2 = call(offset)
    assert len(r2) > budget, "the annotated repeat must exceed the budget here"
    assert c2 <= max(0, d2), (c2, d2)
    assert d2 == d1, "an appended note must never displace delivered body characters"

    # Shape B: the rendering alone exceeds the budget; ack == the truncator's cut.
    delegate._READ_COVERAGE.clear()
    r3, d3, c3 = call(0)
    assert len(r3) > budget and c3 == d3, (c3, d3)
    r4, d4, c4 = call(0)
    assert c4 <= max(0, d4) and d4 == d3, (c4, d4, d3)
    delegate._CUSTODY.clear()
    delegate._READ_COVERAGE.clear()


def test_reading_the_staged_artifact_whole_writes_the_canonical_acknowledgement(
        tmp_path, monkeypatch):
    """Owner doctrine D7: a delegated result is OBTAINED only after the artifact is
    read to EOF — meaning proven CONTINUOUS coverage from the first line to the last,
    not a cursor that merely touched the end. The canonical acknowledgement is a typed
    row written exactly when the windows have covered the whole artifact — carrying the
    byte length and hash of what was staged — written once, replayed across restarts,
    and surfaced on a re-wait. It gates NOTHING: partial reads still work, full reads
    still work, the only change is that the record can now tell the two apart."""
    import hashlib

    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw
    from ouroboros.tools.core import _read_file

    class _Stub(_LiveRunStub):
        def get_run(self, rid):
            return {"lastSeq": 9, "primaryOutput": "V" * 120_000,
                    "summary": {"state": "succeeded", "spendUsd": 0.0}}

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    delegate._CUSTODY.clear()
    delegate._READ_COVERAGE.clear()
    dc.record_started(tmp_path, delegate._RunCustody(
        run_id="run-1", task_id="t-a", route_id="r", model="m",
        project_id="p", project_owned=False, root_task_id="t-a", ledger_root=str(tmp_path)))
    ctx = _nanny_ctx(tmp_path)

    first = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))
    artifact = first["output_delivery"]["artifact"]
    assert first["output_delivery"]["consumed"] is False
    assert "delegate_run_output_consumed" not in _event_types(tmp_path)
    spilled = [json.loads(l) for l in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
               if '"delegate_run_output_spilled"' in l]
    assert spilled and spilled[-1]["sha256"] == artifact["sha256"], \
        "the staged fact must durably carry what was staged"
    assert spilled[-1]["full_content"] is True

    # A head read is served in full and acknowledges nothing.
    head = _read_file(ctx, path=artifact["path"], root="task_drive", start_line=1, max_lines=5)
    assert "BLOCKED" not in head and "ERROR" not in head
    assert "delegate_run_output_consumed" not in _event_types(tmp_path)

    # THE NEGATIVE THAT DEFINES THE CONTRACT: a tail window whose end touches EOF, with
    # the middle never read, is NOT full reading and must not acknowledge. (The first
    # cut of this feature acknowledged exactly this shape.)
    tail = _read_file(ctx, path=artifact["path"], root="task_drive",
                      start_line=artifact["lines"], max_lines=5)
    assert "BLOCKED" not in tail and "ERROR" not in tail
    assert "delegate_run_output_consumed" not in _event_types(tmp_path), \
        "head+tail with a skipped middle must never acknowledge"
    gap_wait = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))
    assert gap_wait["output_delivery"]["consumed"] is False

    # Filling the gap — contiguous coverage of every line — IS the acknowledgement.
    _read_artifact_whole(ctx, artifact)
    rows = [json.loads(l) for l in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
            if '"delegate_run_output_consumed"' in l]
    assert len(rows) == 1, "the acknowledgement is canonical: one row, not one per read"
    staged_bytes = pathlib.Path(artifact["abs_path"]).read_bytes()
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["bytes"] == len(staged_bytes) == artifact["bytes"]
    assert rows[0]["sha256"] == hashlib.sha256(staged_bytes).hexdigest() == artifact["sha256"]
    assert rows[0]["lines"] == artifact["lines"]

    # Reading it whole again does not write a second acknowledgement.
    _read_artifact_whole(ctx, artifact)
    assert sum(1 for t in _event_types(tmp_path) if t == "delegate_run_output_consumed") == 1

    # A re-wait on the terminal run now reports the durable fact in its disposition.
    second = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))
    assert second["output_delivery"]["consumed"] is True

    # The fact survives a worker restart, like every other custody fact.
    delegate._CUSTODY.clear()
    replayed = dc.replay(tmp_path)["run-1"]
    assert replayed.output_consumed is True
    assert replayed.output_complete is True
    assert replayed.output_artifact == artifact["path"]
    delegate._CUSTODY.clear()


def test_an_unread_result_is_a_loud_durable_fact_at_settlement(tmp_path, monkeypatch):
    """Owner directive: full-output consumption must be LOAD-BEARING before settlement.
    Until now the D7 acknowledgement was pure disclosure — the module said so in words,
    'nothing anywhere blocks on its absence' — so a delegated result could be paid for
    and never collected with nothing but a boolean field to notice it.

    WHY NOT A HARD GATE (the (a) option), proven by the call order right here:
    `delegate_wait` SETTLES and only then builds the payload that STAGES the artifact.
    Refusing to settle until the read happened would refuse the step that creates the
    thing to read, and would hold back the LEDGER ROW for money already spent; cancelled
    and failed runs commonly have no output at all and would strand in `open_runs`
    forever. So (b): the money settles immediately and the OMISSION becomes a typed
    durable fact on three surfaces — the settlement row, the parent's result, and the
    health invariants — self-clearing the moment the read lands."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    class _Huge(_LiveRunStub):
        def get_run(self, rid):
            return {"lastSeq": 9, "primaryOutput": "V" * 120_000,
                    "summary": {"state": "succeeded", "spendUsd": 0.0,
                                "effectiveAccess": "readonly"}}

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Huge())
    delegate._CUSTODY.clear()
    delegate._READ_COVERAGE.clear()
    dc.record_started(tmp_path, delegate._RunCustody(
        run_id="run-live", task_id="t-a", route_id="r", model="m",
        project_id="p", project_owned=False, root_task_id="t-a", ledger_root=str(tmp_path)))
    ctx = _nanny_ctx(tmp_path)

    first = json.loads(delegate._delegate_wait(ctx, "run-live", wait_sec=1))
    artifact = first["output_delivery"]["artifact"]

    # 1. The money settled — never held hostage to a disclosure.
    assert first["settlement"]["settled"] is True
    assert first["settlement"]["ledger_recorded"] is True
    # 2. ...and the omission is named, on the settlement row AND in words to the parent.
    assert "NOT COLLECTED" in first["result_not_collected"]
    assert "delegate_run_settled_unread" in _event_types(tmp_path)
    # 3. ...and it stays visible until the read happens.
    unread = dc.settled_unread_outputs(tmp_path)
    assert [c.run_id for c in unread] == ["run-live"]

    # ONCE PER RUN, not once per poll: a re-wait on an already settled run must not
    # append a second identical omission row (which would read as a second omission),
    # while still telling the parent the result is STILL not collected.
    repeat = json.loads(delegate._delegate_wait(ctx, "run-live", wait_sec=1))
    assert "NOT COLLECTED" in repeat["result_not_collected"]
    assert sum(1 for t in _event_types(tmp_path) if t == "delegate_run_settled_unread") == 1

    # It survives the worker that settled it: the fact is durable, not process-local —
    # and a restarted worker does not repeat the row either, because the flag replays.
    delegate._CUSTODY.clear()
    assert [c.run_id for c in dc.settled_unread_outputs(tmp_path)] == ["run-live"]
    restarted = json.loads(delegate._delegate_wait(ctx, "run-live", wait_sec=1))
    assert "NOT COLLECTED" in restarted["result_not_collected"]
    assert sum(1 for t in _event_types(tmp_path) if t == "delegate_run_settled_unread") == 1

    # THE READ CLEARS IT, on every surface, with no second settlement needed.
    _read_artifact_whole(ctx, artifact)
    assert dc.settled_unread_outputs(tmp_path) == []
    again = json.loads(delegate._delegate_wait(ctx, "run-live", wait_sec=1))
    assert "result_not_collected" not in again, "a collected result must stop nagging"
    assert again["output_delivery"]["consumed"] is True

    # NEGATIVE HALVES — the shapes that must never owe this, or the fact becomes noise
    # and legitimate flows deadlock on a warning they cannot discharge:
    #   (a) a run whose payload fit INLINE staged nothing;
    inline = dc.RunCustody(run_id="r-inline", task_id="t-a", settled=True)
    assert dc.settled_output_unread(inline) is False
    #   (b) a run whose staged content was only a PREVIEW was never acknowledgeable;
    preview = dc.RunCustody(run_id="r-prev", task_id="t-a", settled=True,
                            output_artifact="delegated_runs/r-prev.json",
                            output_complete=False)
    assert dc.settled_output_unread(preview) is False
    #   (c) a run that is not settled yet owes nothing here (it is still in flight).
    live = dc.RunCustody(run_id="r-live", task_id="t-a", settled=False,
                         output_artifact="delegated_runs/r-live.json",
                         output_complete=True)
    assert dc.settled_output_unread(live) is False
    delegate._CUSTODY.clear()
    delegate._READ_COVERAGE.clear()


def test_no_post_fires_when_the_start_request_row_did_not_land(tmp_path, monkeypatch):
    """Codex audit, claim 2, proven by run before fixing: with the event-log append
    failing, the POST still fired and the run started with NO durable request row --
    a worker death before record_started would leave a live overpowered run that
    nothing durable names. The POST is now conditional on the row landing: a broken
    event log refuses the start, typed, with the created registration retired."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    posts = []

    class _Stub(_LiveRunStub):
        def start_run(self, request, *, idempotency_key=""):
            posts.append(idempotency_key)
            return {"runId": "run-1"}

    monkeypatch.setenv("OUROBOROS_SUBAGENT_HARNESS", "some-route=weak-model:low")
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    real_append = dc.append_jsonl

    def broken_append(path, row):
        if row.get("type") == "delegate_run_start_requested":
            return False               # append_jsonl's own "did not land" signal
        return real_append(path, row)

    monkeypatch.setattr(dc, "append_jsonl", broken_append)
    delegate._CUSTODY.clear()
    out = json.loads(delegate._delegate_start(_nanny_ctx(tmp_path), "do the work"))
    delegate._CUSTODY.clear()
    assert out["status"] == "refused"
    assert out["reason"] == "start_request_row_unwritable"
    assert posts == [], "the POST must be conditional on the durable request row"
    assert "delegate_run_started" not in _event_types(tmp_path)


def test_a_line_the_delivery_layer_cut_is_not_covered(tmp_path, monkeypatch):
    """Codex audit, claim 1: coverage must bind to what the DELIVERY layer actually
    hands the model, not to source-file line ranges. read_file's result is cut at
    tool_result_limit("read_file") by the outer truncator, so a single line longer
    than that budget renders a window the model only ever sees the head of. Crediting
    the whole line marked an artifact fully read while ~40K chars never reached the
    model. The cut remainder is reachable — and only creditable — through start_char,
    the sub-line cursor."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw
    from ouroboros.tool_capabilities import UNTRUNCATED_TOOL_RESULTS, tool_result_limit
    from ouroboros.tools.core import _read_file

    # The premise the whole test rests on: these reads ARE outer-truncated.
    assert "read_file" not in UNTRUNCATED_TOOL_RESULTS
    budget = tool_result_limit("read_file")

    class _Stub(_LiveRunStub):
        def get_run(self, rid):
            # One ~120K-char JSON line in the staged artifact: longer than any
            # deliverable read_file window.
            return {"lastSeq": 9, "primaryOutput": "V" * 120_000,
                    "summary": {"state": "succeeded", "spendUsd": 0.0}}

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    delegate._CUSTODY.clear()
    delegate._READ_COVERAGE.clear()
    dc.record_started(tmp_path, delegate._RunCustody(
        run_id="run-1", task_id="t-a", route_id="r", model="m",
        project_id="p", project_owned=False, root_task_id="t-a", ledger_root=str(tmp_path)))
    ctx = _nanny_ctx(tmp_path)
    first = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))
    artifact = first["output_delivery"]["artifact"]

    # THE NEGATIVE CODEX NAMES: a full line-window sweep — the pre-fix notion of
    # "whole file", no sub-line cursor — must NOT acknowledge, because the long
    # line's window is cut at delivery and the model never received its tail.
    line = 1
    while line <= artifact["lines"]:
        _read_file(ctx, path=artifact["path"], root="task_drive",
                   start_line=line, max_lines=7)
        line += 7
    assert "delegate_run_output_consumed" not in _event_types(tmp_path), \
        "a line the delivery layer cut is NOT covered"
    delegate._CUSTODY.clear()
    assert dc.replay(tmp_path)["run-1"].output_consumed is False

    # The remainder is reachable through the sub-line cursor, and only DELIVERED
    # chunks accumulate: advancing start_char across the long line completes coverage.
    staged_lines = pathlib.Path(artifact["abs_path"]).read_text(encoding="utf-8").splitlines(keepends=True)
    stride = budget - 5_000                     # safely below any delivered body size
    for line_no, line in enumerate(staged_lines, start=1):
        offset = 0
        while offset < len(line):
            view = _read_file(ctx, path=artifact["path"], root="task_drive",
                              start_line=line_no, max_lines=1, start_char=offset)
            if offset:
                assert f"(from char {offset} of this window)" in view.splitlines()[0], \
                    "the sub-line cursor must be disclosed in the header"
            offset += stride
    assert sum(1 for t in _event_types(tmp_path) if t == "delegate_run_output_consumed") == 1, \
        "delivered-chunk coverage of every character is the acknowledgement"
    delegate._CUSTODY.clear()


def test_a_restaged_different_artifact_does_not_inherit_the_old_acknowledgement(
        tmp_path, monkeypatch):
    """Codex audit, claim 5, proven by run before fixing: after a full read + ack of
    artifact A, a re-wait re-staged DIFFERENT bytes at the same path and the delivery
    still said consumed:true — the old ack transferred by PATH to content never read.
    The ack is hash-bound now: a re-stage with a different sha resets consumed (in
    process and in replay), the new content owes its own full read, and a second
    acknowledgement row for the new bytes is legitimate."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    class _Stub(_LiveRunStub):
        payload = "A" * 30_000 + "\n" + ("x\n" * 200)
        def get_run(self, rid):
            return {"lastSeq": 9, "primaryOutput": self.payload,
                    "summary": {"state": "succeeded", "spendUsd": 0.0}}

    stub = _Stub()
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: stub)
    delegate._CUSTODY.clear()
    delegate._READ_COVERAGE.clear()
    dc.record_started(tmp_path, delegate._RunCustody(
        run_id="run-1", task_id="t-a", route_id="r", model="m",
        project_id="p", project_owned=False, root_task_id="t-a", ledger_root=str(tmp_path)))
    ctx = _nanny_ctx(tmp_path)

    first = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))
    artifact = first["output_delivery"]["artifact"]
    _read_artifact_whole(ctx, artifact)
    acks = lambda: sum(1 for t in _event_types(tmp_path) if t == "delegate_run_output_consumed")
    assert acks() == 1

    # Identical re-stage keeps the acknowledgement: same bytes, same fact.
    same = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))
    assert same["output_delivery"]["consumed"] is True

    # DIFFERENT content re-staged at the same path: the old ack must not transfer.
    stub.payload = "B" * 30_000 + "\n" + ("y\n" * 300)
    changed = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))
    artifact2 = changed["output_delivery"]["artifact"]
    assert artifact2["sha256"] != artifact["sha256"]
    assert changed["output_delivery"]["consumed"] is False, \
        "an acknowledgement names bytes, never a path"
    delegate._CUSTODY.clear()
    assert dc.replay(tmp_path)["run-1"].output_consumed is False, \
        "the reset must survive a worker restart"

    # The new content earns its own acknowledgement by being read whole.
    _read_artifact_whole(ctx, artifact2)
    assert acks() == 2
    delegate._CUSTODY.clear()
    assert dc.replay(tmp_path)["run-1"].output_consumed is True
    delegate._CUSTODY.clear()


def test_a_truncated_primary_output_is_resolved_from_the_artifact_route(
        tmp_path, monkeypatch):
    """`primaryOutput.text` on the run detail is a bounded 256 KiB PREVIEW
    (control-api PRIMARY_OUTPUT_PREVIEW_BYTES) beside `bytes` and `truncated`. A
    truncated preview must never be staged or acknowledged as the result: the full
    file comes from GET /v2/runs/:id/artifacts/<path>, verified against the reported
    size before it may wear the plain name."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    full_text = "W" * 120_000
    preview = full_text[:4_000]
    fetched_paths = []

    class _Stub(_LiveRunStub):
        def get_run(self, rid):
            return {"lastSeq": 9,
                    "primaryOutput": {"kind": "answer", "path": "final/answer.md",
                                      "text": preview, "bytes": len(full_text),
                                      "truncated": True},
                    "summary": {"state": "succeeded", "spendUsd": 0.0}}
        def get_run_artifact(self, rid, path):
            fetched_paths.append((rid, path))
            return full_text.encode("utf-8")

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Stub())
    delegate._CUSTODY.clear()
    delegate._READ_COVERAGE.clear()
    dc.record_started(tmp_path, delegate._RunCustody(
        run_id="run-1", task_id="t-a", route_id="r", model="m",
        project_id="p", project_owned=False, root_task_id="t-a", ledger_root=str(tmp_path)))
    ctx = _nanny_ctx(tmp_path)

    out = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))
    assert fetched_paths == [("run-1", "final/answer.md")], \
        "the full artifact must be fetched from the artifacts route, not trusted from the preview"
    delivery = out["output_delivery"]
    assert delivery["primary_output_full"]["fetched"] is True
    assert delivery["primary_output_full"]["verified"] == "size"
    artifact = delivery["artifact"]
    staged = json.loads(pathlib.Path(artifact["abs_path"]).read_text(encoding="utf-8"))
    assert staged["primary_output"]["text"] == full_text, "the STAGED result must be the full text"
    assert staged["primary_output"]["truncated"] is False

    # And the verified-full staging is what makes the acknowledgement reachable.
    _read_artifact_whole(ctx, artifact)
    assert sum(1 for t in _event_types(tmp_path) if t == "delegate_run_output_consumed") == 1
    delegate._CUSTODY.clear()


def test_an_unresolvable_truncated_output_is_disclosed_and_never_acknowledged(
        tmp_path, monkeypatch):
    """When the full artifact cannot be fetched — or fails size and preview-prefix
    verification — the result stays a PREVIEW: typed disclosure in the delivery, no
    acknowledgement ever (even after reading the staged file whole), and the custody
    replay says the staging was incomplete. Disclosure, not refusal: the preview is
    still delivered and readable."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    class _FetchFails(_LiveRunStub):
        def get_run(self, rid):
            return {"lastSeq": 9,
                    "primaryOutput": {"kind": "answer", "path": "final/answer.md",
                                      "text": "small preview", "bytes": 999_999,
                                      "truncated": True},
                    "summary": {"state": "succeeded", "spendUsd": 0.0}}
        def get_run_artifact(self, rid, path):
            raise gw.ClaudexorUnavailable("http_404", "no such artifact", status_code=404)

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _FetchFails())
    delegate._CUSTODY.clear()
    delegate._READ_COVERAGE.clear()
    dc.record_started(tmp_path, delegate._RunCustody(
        run_id="run-1", task_id="t-a", route_id="r", model="m",
        project_id="p", project_owned=False, root_task_id="t-a", ledger_root=str(tmp_path)))
    ctx = _nanny_ctx(tmp_path)

    # Small payload -> the INLINE branch: even inline-fitting must not claim complete.
    out = json.loads(delegate._delegate_wait(ctx, "run-1", wait_sec=1))
    delivery = out["output_delivery"]
    assert delivery["complete"] is False and delivery["consumed"] is False
    assert delivery["primary_output_full"]["fetched"] is False
    assert "http_404" in delivery["primary_output_full"]["reason"]
    assert "INCOMPLETE AT THE SOURCE" in delivery["note"]

    # Large unverifiable payload -> the SPILL branch: staged as incomplete, unackable.
    big_preview = "P" * 120_000

    class _WrongBytes(_FetchFails):
        def get_run(self, rid):
            return {"lastSeq": 9,
                    "primaryOutput": {"kind": "answer", "path": "final/answer.md",
                                      "text": big_preview, "bytes": 999_999,
                                      "truncated": True},
                    "summary": {"state": "succeeded", "spendUsd": 0.0}}
        def get_run_artifact(self, rid, path):
            return b"entirely different content"     # fails size AND prefix checks

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _WrongBytes())
    dc.record_started(tmp_path, delegate._RunCustody(
        run_id="run-2", task_id="t-a", route_id="r", model="m",
        project_id="p", project_owned=False, root_task_id="t-a", ledger_root=str(tmp_path)))
    out2 = json.loads(delegate._delegate_wait(ctx, "run-2", wait_sec=1))
    delivery2 = out2["output_delivery"]
    assert delivery2["artifact"], "the preview is still delivered, staged and readable"
    assert delivery2["primary_output_full"]["fetched"] is True
    assert delivery2["primary_output_full"]["verified"] == ""
    assert "verification_failed" in delivery2["primary_output_full"]["reason"]
    spilled = [json.loads(l) for l in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
               if '"delegate_run_output_spilled"' in l]
    assert spilled[-1]["full_content"] is False

    # Reading the staged preview whole must NOT acknowledge: it is not the result.
    _read_artifact_whole(ctx, delivery2["artifact"])
    assert "delegate_run_output_consumed" not in _event_types(tmp_path)
    delegate._CUSTODY.clear()
    assert dc.replay(tmp_path)["run-2"].output_complete is False
    assert dc.replay(tmp_path)["run-2"].output_consumed is False
    delegate._CUSTODY.clear()


def test_a_reconciled_run_with_an_unread_artifact_is_visible_as_uncollected(
        tmp_path, monkeypatch):
    """The third "launched and never collected" recurrence, made structural: when the
    reconciler closes a run whose staged artifact has no EOF acknowledgement, its
    durable RECONCILED row says so — `staged_output_consumed: false` beside the
    artifact path — instead of the loss being inferable only from ledger discipline."""
    import ouroboros.delegate_custody as dc
    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    class _Stub(_LiveRunStub):
        def __init__(self):
            super().__init__()
            self.retire_ok = False
        def get_run(self, rid):
            return {"lastSeq": 9, "primaryOutput": "V" * 120_000,
                    "summary": {"state": "succeeded", "spendUsd": 0.0}}
        def remove_project(self, pid):
            if not self.retire_ok:
                raise RuntimeError("daemon busy")

    stub = _Stub()
    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: stub)
    delegate._CUSTODY.clear()
    dc.record_started(tmp_path, delegate._RunCustody(
        run_id="run-1", task_id="t-gone", route_id="r", model="m",
        project_id="prj", project_owned=True, root_task_id="t-gone", ledger_root=str(tmp_path)))

    # The nanny sees the terminal preview (artifact staged) but the settlement cannot
    # finish, and the task dies without ever reading the artifact to EOF.
    out = json.loads(delegate._delegate_wait(_nanny_ctx(tmp_path, "t-gone"), "run-1", wait_sec=1))
    assert out["output_delivery"]["artifact"], "this scenario is about a staged artifact"
    assert out["settlement"]["settled"] is False
    delegate._CUSTODY.clear()          # the worker is gone

    stub.retire_ok = True
    results = dc.reconcile_orphaned_runs(tmp_path, {"t-alive"}, gateway_factory=lambda: stub)
    assert [r["run_id"] for r in results] == ["run-1"]
    assert results[0]["staged_output_consumed"] is False
    assert results[0]["staged_output"] == out["output_delivery"]["artifact"]["path"]
    reconciled = [json.loads(l) for l
                  in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
                  if '"delegate_run_reconciled"' in l]
    assert reconciled and reconciled[-1]["staged_output_consumed"] is False, \
        "the uncollected shape must be durable, not only returned"
    delegate._CUSTODY.clear()


def test_the_progress_payload_survives_a_verbose_harness_too(tmp_path, monkeypatch):
    """The sibling surface of the terminal payload: a harness-supplied timeline title is
    unbounded, and twelve long ones push the PROGRESS payload past the same cap, where
    head-truncation severs the same JSON."""
    from ouroboros.loop_tool_execution import _truncate_tool_result
    from ouroboros.tool_capabilities import tool_result_limit

    import ouroboros.tools.delegate as delegate
    from ouroboros.gateways import claudexor as gw

    class _Chatty(_LiveRunStub):
        def get_run(self, rid):
            return {"lastSeq": 42, "summary": {"state": "running", "effectiveAccess": "readonly"},
                    "timeline": [{"type": "tool", "title": "T" * 20_000, "severity": "info"}
                                 for _ in range(30)]}

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _Chatty())
    delegate._CUSTODY.clear()
    delegate._CUSTODY["run-1"] = delegate._RunCustody(
        run_id="run-1", task_id="t-a", route_id="r", model="m", project_id="p", project_owned=False)
    raw = delegate._delegate_wait(_nanny_ctx(tmp_path), "run-1", wait_sec=1, since_seq=1)
    delegate._CUSTODY.clear()
    assert len(raw) <= tool_result_limit("delegate_wait")
    assert _truncate_tool_result(raw, "delegate_wait", {}) == raw
    payload = json.loads(raw)
    assert payload["status"] == "progress"
    # P34R.5: the bound is the SHARED disclosed contract, not a hand-rolled slice —
    # every cut label carries the omission marker AND the original length.
    assert all("OMISSION NOTE" in row["title"] and "original length 20000" in row["title"]
               for row in payload["timeline_tail"])
    assert all(len(row["title"]) < 500 for row in payload["timeline_tail"])


# -- 3.12 reconciliation on restart / parent terminalization -------------------


def test_an_orphaned_delegated_run_is_reconciled_when_its_owner_is_gone(tmp_path, monkeypatch):
    """The predicate is the one `process_custody.reap_orphaned_processes` already owns:
    the owning task is no longer in the supervisor's live set. A delegated run has no
    pid, so the process reaper cannot see it — but it is still spending quota and still
    writing to a workspace."""
    import ouroboros.delegate_custody as dc

    live = _LiveRunStub(run_id="run-orphan")
    finished = _LiveRunStub(run_id="run-done")
    finished.get_run = lambda rid: {"lastSeq": 2, "summary": {"state": "succeeded", "spendUsd": 0.0}}

    for stub, task in ((live, "t-gone"), (finished, "t-also-gone")):
        dc.record_started(tmp_path, dc.RunCustody(
            run_id=stub.run_id, task_id=task, route_id="r", model="m",
            project_id="p", project_owned=False, root_task_id=task, ledger_root=str(tmp_path)))
    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-alive", task_id="t-running", route_id="r", model="m",
        project_id="p", project_owned=False, root_task_id="t-running", ledger_root=str(tmp_path)))
    dc._CUSTODY.clear()

    class _Router(_LiveRunStub):
        def get_run(self, rid):
            return (finished if rid == "run-done" else live).get_run(rid)
        def cancel_run(self, rid, reason=""):
            return live.cancel_run(rid, reason)

    outcomes = dc.reconcile_orphaned_runs(tmp_path, {"t-running"}, gateway_factory=_Router)
    dc._CUSTODY.clear()
    by_run = {row["run_id"]: row for row in outcomes}
    assert set(by_run) == {"run-orphan", "run-done"}, "a live owner's run must be left alone"
    assert by_run["run-orphan"]["action"] == "cancelled"
    assert live.cancels == [("run-orphan", "owner_task_gone")]
    assert by_run["run-done"]["action"] == "settled" and by_run["run-done"]["settled"] is True

    # Unknown liveness reconciles nothing: never mass-cancel on missing information.
    live.cancels.clear()
    assert dc.reconcile_orphaned_runs(tmp_path, None, gateway_factory=_Router) == []
    assert live.cancels == []
    dc._CUSTODY.clear()


def test_what_the_daemon_says_is_absent_is_closed_not_faulted_forever(tmp_path):
    """One root cause at two surfaces: a 404 is the daemon ANSWERING that the thing is not
    there, and both were read as "we could not find out".

    A run the daemon does not have was treated exactly like an unreachable daemon, so it
    was never settled, stayed in `open_runs`, and was re-faulted on EVERY pass — a
    permanent CRITICAL health invariant that no cancel or settlement could ever clear. Its
    sibling: a registration the daemon does not have kept `project_owned` true, so a
    terminal run could never finish settling and was reconciled forever."""
    import ouroboros.delegate_custody as dc

    class _NoSuchRun(_LiveRunStub):
        def get_run(self, rid):
            raise cx.ClaudexorUnavailable("run_not_found", "no such run", status_code=404)

    class _NoSuchProject(_LiveRunStub):
        def get_run(self, rid):
            return {"lastSeq": 2, "summary": {"state": "succeeded", "spendUsd": 0.0}}
        def remove_project(self, pid):
            raise cx.ClaudexorUnavailable("project_not_found", "no such project", status_code=404)

    dc._CUSTODY.clear()
    for run_id, task in (("run-gone", "t-gone"), ("run-owns-a-dead-project", "t-also-gone")):
        dc.record_started(tmp_path, dc.RunCustody(
            run_id=run_id, task_id=task, route_id="r", model="m", project_id="prj-ours",
            project_owned=run_id.endswith("project"), root_task_id=task, ledger_root=str(tmp_path)))
    dc._CUSTODY.clear()

    class _Router(_LiveRunStub):
        def get_run(self, rid):
            return (_NoSuchProject() if rid.endswith("project") else _NoSuchRun()).get_run(rid)
        def remove_project(self, pid): _NoSuchProject().remove_project(pid)

    passes = []
    for _ in range(3):
        passes.append(dc.reconcile_orphaned_runs(tmp_path, {"t-live"}, gateway_factory=_Router))
        dc._CUSTODY.clear()
    assert [row["action"] for row in passes[0]] == ["absent", "settled"], passes[0]
    assert passes[1] == [] and passes[2] == [], "a closed run must not be reconciled again"
    assert dc.open_runs(tmp_path) == [], "neither run may stay open"
    assert dc.open_containment_faults(tmp_path) == []
    assert "DELEGATED RUN MAY STILL BE LIVE" not in _health_invariants(tmp_path)
    types = _event_types(tmp_path)
    assert "delegate_run_containment_fault" not in types, "absence is not a containment fault"
    assert "delegate_run_project_retire_failed" not in types, "absence IS discharge"
    # An absent run is CLOSED, not settled: no ledger row is invented for a run the daemon
    # cannot even describe.
    assert "delegate_run_closed_absent" in types
    rows = [json.loads(l) for l in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    ledgered = [r for r in rows if r.get("type") == "delegate_run_ledger_recorded"]
    assert [r["run_id"] for r in ledgered] == ["run-owns-a-dead-project"], ledgered

    # A daemon that is merely UNREACHABLE still faults: absence and ignorance stay apart.
    class _Deaf(_LiveRunStub):
        def get_run(self, rid):
            raise cx.ClaudexorUnavailable("daemon_unreachable", "connection refused")

    dc.record_started(tmp_path, dc.RunCustody(
        run_id="run-unknown", task_id="t-gone", route_id="r", model="m", project_id="p",
        project_owned=False, root_task_id="t-gone", ledger_root=str(tmp_path)))
    dc._CUSTODY.clear()
    assert [row["action"] for row
            in dc.reconcile_orphaned_runs(tmp_path, {"t-live"}, gateway_factory=_Deaf)] == ["unreadable"]
    dc._CUSTODY.clear()
    assert [f["run_id"] for f in dc.open_containment_faults(tmp_path)] == ["run-unknown"]


def test_a_terminalizing_parent_releases_the_run_it_still_holds(tmp_path):
    """The in-process twin of reconciliation. A parent that finishes while its delegated
    run is still going used to leave it mutating until the next 10-minute sweep; the
    loop's own resource-release point now settles or cancels it like any held resource.
    A task that delegated nothing must pay nothing for this."""
    import ouroboros.delegate_custody as dc

    live = _LiveRunStub(run_id="run-held")
    dc._CUSTODY.clear()
    dc._CUSTODY["run-held"] = dc.RunCustody(run_id="run-held", task_id="t-parent", route_id="r",
                                            model="m", project_id="p", project_owned=False,
                                            ledger_root=str(tmp_path))
    assert dc.release_task_runs(tmp_path, "t-someone-else", gateway_factory=lambda: live) == []
    assert live.cancels == [], "another task's run is not this task's to release"

    outcomes = dc.release_task_runs(tmp_path, "t-parent", gateway_factory=lambda: live)
    dc._CUSTODY.clear()
    assert [row["action"] for row in outcomes] == ["cancelled"]
    assert live.cancels == [("run-held", "owner_task_gone")]


def test_the_loops_own_release_point_reaches_the_delegated_reconciler(tmp_path, monkeypatch):
    """`release_task_runs` only helps if something CALLS it. The test beside this one drives
    the function directly, so it passed with the loop's wiring deleted — and the loop is
    the ordinary path: without it a terminalized parent leaves its run mutating until the
    next ten-minute sweep. The release must also read the CANONICAL root, not the child
    drive the subagent runs on, or it looks for custody where none was written."""
    from types import SimpleNamespace

    import ouroboros.delegate_custody as dc
    import ouroboros.loop as loop

    released = []
    monkeypatch.setattr(dc, "release_task_runs",
                        lambda root, task_id, **kw: released.append((str(root), task_id)) or [])
    canonical = tmp_path / "canonical"
    inner = SimpleNamespace(drive_root=tmp_path / "child",
                            task_metadata={"budget_drive_root": str(canonical)})
    loop._cleanup_loop_resources(None, loop._LoopExitContext(
        tools=SimpleNamespace(_ctx=inner), drive_root=tmp_path, task_id="t-parent",
        event_queue=None, drive_logs=tmp_path / "logs", accumulated_usage={}, llm_trace={}))
    assert released == [(str(canonical), "t-parent")], released


def test_the_startup_sweep_reconciles_delegated_runs_too(monkeypatch):
    """Nothing is running yet at supervisor startup, so every open delegated run is by
    definition ownerless. The only server-side test covered the PERIODIC tick, so the
    startup half could be deleted without a single failure — and it is the half that
    catches the runs the generation that died was watching."""
    import server
    import ouroboros.delegate_custody as dc
    import ouroboros.process_custody as pc

    seen = {}
    monkeypatch.setattr(pc, "reap_orphaned_processes", lambda root, **kw: [])
    monkeypatch.setattr(dc, "reconcile_orphaned_runs",
                        lambda root, **kw: seen.setdefault("live", kw.get("running_task_ids")) or [])
    monkeypatch.setattr(server, "_installed_skill_names", lambda: None)
    server._startup_custody_sweep()
    assert seen["live"] == set(), "an empty live set is the point: nothing survived the restart"


def test_both_custody_surfaces_see_the_same_live_task_set(monkeypatch):
    """The periodic sweep must hand the delegated reconciler the SAME live task set the
    process reaper gets. Two copies of "is the owner still running" is exactly how one
    custody surface ends up reaping while its twin does not."""
    import time

    import server
    import ouroboros.delegate_custody as dc
    import ouroboros.process_custody as pc
    import supervisor.queue as queue

    seen = {}
    monkeypatch.setattr(pc, "reap_orphaned_processes",
                        lambda root, **kw: seen.__setitem__("processes", kw.get("running_task_ids")) or [])
    monkeypatch.setattr(dc, "reconcile_orphaned_runs",
                        lambda root, **kw: seen.__setitem__("delegated", kw.get("running_task_ids")) or [])
    monkeypatch.setattr(server, "_installed_skill_names", lambda: None)
    monkeypatch.setitem(queue.RUNNING, "t-live", {})
    server._periodic_supervisor_maintenance([0.0], [time.time()])
    assert seen["processes"] == seen["delegated"] == {"t-live"}, seen


def test_a_breach_whose_cancel_was_never_verified_is_not_reported_as_cancelled(
    tmp_path, monkeypatch
):
    """A containment BREACH stops the run through the one verified cancel path, and the
    sentence the agent reads comes from that cancel's typed outcome.

    The ad-hoc cancel this replaced swallowed every exception into a log line and then
    said "The run was cancelled. Do not retry it" unconditionally — so a daemon that
    REFUSED the cancel, or that could not be reached to confirm it, left an overpowered
    run mutating a workspace while the agent was told it had stopped. That is exactly
    what `record_containment_fault`'s own contract forbids: an incident must surface as
    a critical health invariant, "never as a reassuring string in a tool result".
    """
    from ouroboros.gateways import claudexor as gw
    from ouroboros.gateways.claudexor import ClaudexorUnavailable
    import ouroboros.delegate_custody as dc

    run_dir = tmp_path / "run-1"
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setattr(cx, "operator_home", lambda: home)

    class _RefusingStub:
        engine_version = CLAUDEXOR_DELEGATED_MARKER_MIN_VERSION

        def handshake(self): return {}

        def get_run(self, rid):
            # Still RUNNING: the cancel changed nothing the daemon will confirm.
            return {"lastSeq": 7, "summary": {
                "state": "running", "effectiveAccess": "workspace_write",
                "runDir": str(run_dir),
            }}

        def cancel_run(self, rid, reason=""):
            raise ClaudexorUnavailable("control_refused", "daemon refused the cancel")

        def remove_project(self, pid): pass
        def close(self): pass

    monkeypatch.setattr(gw, "ClaudexorGateway", lambda *a, **k: _RefusingStub())
    _write_attempt(run_dir, isolated=False, home_dir=str(home))

    out = _waiting(tmp_path, monkeypatch)

    assert out["status"] == "refused" and out["reason"] == "home_isolation_not_applied", out
    # The typed outcome rides out with the refusal instead of a comforting sentence.
    assert out["cancel_outcome"] == dc.CANCEL_CONTAINMENT_FAULT, out
    assert "CONTAINMENT FAULT" in out["detail"], out["detail"]
    assert "MAY STILL BE LIVE" in out["detail"], out["detail"]
    assert "The run was cancelled." not in out["detail"], out["detail"]


def test_the_configured_wait_ceiling_cannot_promise_more_than_the_tool_can_serve():
    """`OUROBOROS_DELEGATE_WAIT_MAX_SEC` accepted up to 86,400 while `delegate_wait`'s
    own per-call executor timeout is 2100 and the tool is neither per-call-timeout
    configurable nor deadline-clamped — so everything above 2100 bought a KILLED tool
    call instead of the graceful typed no-progress return the wait exists to give.
    The two numbers are pinned together here so they cannot drift apart again."""
    import os

    from ouroboros.config import DELEGATE_WAIT_CEILING_SEC, get_delegate_wait_max_sec
    from ouroboros.loop_tool_execution import _DEADLINE_CLAMPED_TOOLS, _PER_CALL_TIMEOUT_TOOLS
    from ouroboros.tools.delegate import get_tools

    entry = next(e for e in get_tools() if e.schema["name"] == "delegate_wait")
    assert DELEGATE_WAIT_CEILING_SEC == entry.timeout_sec
    # ...and neither escape hatch applies to this tool, which is why the ToolEntry
    # value really is the bound.
    assert "delegate_wait" not in _PER_CALL_TIMEOUT_TOOLS
    assert "delegate_wait" not in _DEADLINE_CLAMPED_TOOLS

    previous = os.environ.get("OUROBOROS_DELEGATE_WAIT_MAX_SEC")
    os.environ["OUROBOROS_DELEGATE_WAIT_MAX_SEC"] = "7200"
    try:
        assert get_delegate_wait_max_sec() == DELEGATE_WAIT_CEILING_SEC
    finally:
        if previous is None:
            os.environ.pop("OUROBOROS_DELEGATE_WAIT_MAX_SEC", None)
        else:
            os.environ["OUROBOROS_DELEGATE_WAIT_MAX_SEC"] = previous
