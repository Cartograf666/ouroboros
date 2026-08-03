"""Owned Claudexor daemon (D30): isolation root, discovery cutover, thin proxies.

Everything here is offline: no daemon is spawned, no network is touched. The
live login flow is the daemon's own product surface and is exercised by the
phase acceptance run, not by unit tests.
"""
import json
import pathlib

import pytest

from ouroboros import claudexor_daemon as owned


def _write_descriptor(config_dir: pathlib.Path, *, port: int = 45678) -> None:
    daemon_dir = config_dir / "daemon"
    daemon_dir.mkdir(parents=True, exist_ok=True)
    (daemon_dir / "token").write_text("tok-owned", encoding="utf-8")
    (daemon_dir / "control-api.json").write_text(json.dumps({
        "host": "127.0.0.1", "port": port, "tokenPath": str(daemon_dir / "token"),
    }), encoding="utf-8")


def test_owned_config_dir_is_data_plane():
    from ouroboros.config import DATA_DIR

    config_dir = owned.owned_config_dir()
    assert str(config_dir).startswith(str(DATA_DIR))
    # The operator's personal state must never be the owned root.
    assert ".claudexor" not in str(config_dir.relative_to(pathlib.Path(DATA_DIR)))


def test_attach_login_command_targets_the_owned_home():
    """The fallback card's copy-paste command (D30): the user's own terminal,
    the OWNED config dir — never a terminal surface inside the UI."""
    command = owned.attach_login_command("job-123")
    assert command.startswith(f"CLAUDEXOR_CONFIG_DIR={owned.owned_config_dir()} ")
    assert command.endswith("claudexor setup attach job-123")


def test_resolve_claudexord_explicit_setting_must_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("OUROBOROS_CLAUDEXOR_BIN", str(tmp_path / "missing"))
    assert owned.resolve_claudexord() == ""
    real = tmp_path / "claudexord"
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_CLAUDEXOR_BIN", str(real))
    assert owned.resolve_claudexord() == str(real)


def test_discover_daemon_prefers_owned_home_once_provisioned(monkeypatch, tmp_path):
    """The D30 cutover: default discovery flips to the owned daemon exactly
    when it is provisioned, and stays on the operator layout before that."""
    from ouroboros.gateways import claudexor as gateway_mod

    owned_dir = tmp_path / "data" / "claudexor"
    operator_home = tmp_path / "operator"
    monkeypatch.setattr(owned, "owned_config_dir", lambda: owned_dir)
    monkeypatch.setattr(owned, "owned_descriptor_path",
                        lambda: owned_dir / "daemon" / "control-api.json")
    monkeypatch.setattr(owned, "owned_daemon_provisioned",
                        lambda: (owned_dir / "daemon" / "control-api.json").is_file())
    monkeypatch.setattr(gateway_mod, "operator_home", lambda: operator_home)

    # Not provisioned: the operator layout is the discovery target (and its
    # absence is the typed refusal, proving the owned home was NOT consulted).
    with pytest.raises(gateway_mod.ClaudexorUnavailable) as err:
        gateway_mod.discover_daemon()
    assert "operator" in str(err.value)

    # Provisioned: the owned endpoint wins without any explicit home argument.
    _write_descriptor(owned_dir, port=45679)
    endpoint = gateway_mod.discover_daemon()
    assert (endpoint.port, endpoint.token) == (45679, "tok-owned")

    # An explicit home still reads that home verbatim (delegation callers).
    with pytest.raises(gateway_mod.ClaudexorUnavailable):
        gateway_mod.discover_daemon(home=operator_home)


def test_discover_daemon_at_reads_override_layout(tmp_path):
    from ouroboros.gateways.claudexor import discover_daemon_at

    _write_descriptor(tmp_path / "cfg")
    endpoint = discover_daemon_at(tmp_path / "cfg")
    assert (endpoint.host, endpoint.port) == ("127.0.0.1", 45678)


def test_stop_never_kills_a_daemon_it_did_not_start():
    manager = owned.OwnedClaudexorDaemon()
    assert manager.stop() is False  # nothing self-started -> nothing to kill


def test_ensure_running_without_binary_is_a_typed_refusal(monkeypatch, tmp_path):
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    import ouroboros.config as config_mod
    # Ownership is verified FIRST (never adopt); this test is about the binary,
    # so the home must be legitimately ours: under the (patched) data plane.
    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(owned, "owned_config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(owned, "owned_daemon_provisioned", lambda: False)
    monkeypatch.setattr(owned, "resolve_claudexord", lambda: "")
    manager = owned.OwnedClaudexorDaemon()
    with pytest.raises(ClaudexorUnavailable) as err:
        manager.ensure_running()
    assert err.value.code == "claudexord_not_installed"


def test_status_payload_not_provisioned_never_spawns(monkeypatch, tmp_path):
    from ouroboros.gateway.claudexor_accounts import _status_payload

    monkeypatch.setattr(owned, "owned_config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(owned, "owned_daemon_provisioned", lambda: False)
    monkeypatch.setattr(owned, "get_owned_daemon", lambda: owned.OwnedClaudexorDaemon())
    payload = _status_payload(include_models=True)
    assert payload["daemon"]["state"] == "not_provisioned"
    assert payload["harnesses"] == [] and payload["quota"] == []
    assert not (tmp_path / "cfg").exists()  # read-only: nothing provisioned


def test_login_endpoint_validates_before_any_daemon_work():
    import asyncio

    from starlette.requests import Request

    from ouroboros.gateway.claudexor_accounts import api_claudexor_login

    async def _call(body: dict):
        payload = json.dumps(body).encode()

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        request = Request({
            "type": "http", "method": "POST", "path": "/api/claudexor/login",
            "headers": [(b"content-type", b"application/json")], "query_string": b"",
        }, receive)
        return await api_claudexor_login(request)

    missing = asyncio.run(_call({}))
    assert missing.status_code == 400 and b"harness is required" in missing.body
    bad_transport = asyncio.run(_call({"harness": "codex", "transport": "carrier"}))
    assert bad_transport.status_code == 400 and b"transport" in bad_transport.body


# ---------------------------------------------------------------------------
# Phase 6, owner directive #1: the executor fact reaches the chat frame.
# «бейдж точно нужен, но не рекламный … что ТУТ бабл \ субагент на codex»
# ---------------------------------------------------------------------------


def _agent_with_metadata(task, task_id="child-1"):
    import types

    from ouroboros.agent import OuroborosAgent

    agent = object.__new__(OuroborosAgent)
    agent._current_task_metadata = {
        "delegation_role": "subagent", "role": "impl", "root_task_id": "r",
        "parent_task_id": "p", "model": "m", "task_group_id": "g",
    }
    agent._current_task_id = task_id
    # Since synthesis the fact is read from the ONE record the dispatch
    # resolution stamped onto the task (`resolve_subagent_dispatch` ->
    # record_fields) — the same principle this file always asserted ("a
    # projection of the decision, never a second derivation"), one level
    # stronger: the projection reads the durable record, not a live object.
    agent._record_executor_facts(task if isinstance(task, dict) else {})
    return agent, types


def test_resolved_harness_route_reaches_the_frame_assembler():
    """The chip's fact comes from the ONE place the executor was decided: the
    dispatch resolution is stamped onto the live metadata that the canonical
    frame assembler already projects — never re-derived per surface."""
    import types

    agent, _ = _agent_with_metadata(
        {"effective_executor": "harness", "executor_route": "codex"})
    frame = agent._subagent_progress_meta("running")
    assert frame["executor_route"] == "codex"
    # The frame keeps carrying the execution facts it always did.
    assert frame["subagent_event"] == "running"
    assert frame["delegation_role"] == "subagent"


def test_no_executor_fact_when_the_run_is_native_blocked_or_undecided():
    """Absent fact -> empty/absent, so the renderer draws NO chip: the native
    API path is the ordinary case and must not print 'api' on every bubble."""
    import types

    native, _ = _agent_with_metadata(
        {"effective_executor": "native", "executor_route": ""}, "child-2")
    assert native._subagent_progress_meta("running")["executor_route"] == ""
    # A blocked or unresolved dispatch records nothing at all.
    blocked, _ = _agent_with_metadata(
        {"effective_executor": "blocked", "executor_route": "codex"}, "child-3")
    assert "executor_route" not in blocked._current_task_metadata
    undecided, _ = _agent_with_metadata({}, "child-4")
    assert "executor_route" not in undecided._current_task_metadata


def test_the_executor_fact_survives_history_replay_and_the_frozen_contract():
    """End-to-end plumbing: the field is in the progress-meta allowlist (so a
    reloaded bubble keeps its chip) and in BOTH contract mirrors."""
    from ouroboros.gateway.contracts import ChatOutbound
    from ouroboros.gateway.history import _PROGRESS_META_FIELDS

    assert "executor_route" in _PROGRESS_META_FIELDS
    assert "executor_route" in ChatOutbound.__annotations__
    js = (pathlib.Path(__file__).resolve().parents[1] / "web" / "modules" / "api_types.js")
    assert "executor_route" in js.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Stale owned-daemon lifecycle (owner directive, pre-synthesis): dead -> restart
# under the same supervision + reconcile; alive-but-foreign -> typed disclosure,
# no kill; foreign home -> never adopt.
# ---------------------------------------------------------------------------



def _stale_home(config_dir: pathlib.Path, *, marker_data_dir: str) -> None:
    """A provisioned home whose daemon is DEAD: descriptor points at a closed
    port, token present, ownership marker written."""
    import json
    import socket

    daemon_dir = config_dir / "daemon"
    daemon_dir.mkdir(parents=True, exist_ok=True)
    (daemon_dir / "token").write_text("tok-dead", encoding="utf-8")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()  # port free again -> connection refused = dead daemon
    (daemon_dir / "control-api.json").write_text(json.dumps({
        "host": "127.0.0.1", "port": dead_port,
        "tokenPath": str(daemon_dir / "token"),
    }), encoding="utf-8")
    (config_dir / owned.OWNERSHIP_MARKER).write_text(json.dumps({
        "owner": "ouroboros", "data_dir": marker_data_dir,
    }), encoding="utf-8")


def _point_owned_home(monkeypatch, config_dir: pathlib.Path, data_dir: pathlib.Path) -> None:
    monkeypatch.setattr(owned, "owned_config_dir", lambda: config_dir)
    monkeypatch.setattr(owned, "owned_descriptor_path",
                        lambda: config_dir / "daemon" / "control-api.json")
    monkeypatch.setattr(owned, "owned_daemon_provisioned",
                        lambda: (config_dir / "daemon" / "control-api.json").is_file())
    import ouroboros.config as config_mod
    monkeypatch.setattr(config_mod, "DATA_DIR", data_dir)


def test_dead_owned_daemon_is_restarted_and_reconciled(monkeypatch, tmp_path):
    """The stale case end-to-end: descriptor exists, daemon dead, ownership
    marker OURS -> ensure_running restarts under the same supervision
    chokepoint and reconciles by fresh discovery + an AUTHENTICATED handshake
    against the NEW descriptor the restarted daemon wrote.

    The scripted daemon serves its /v2/handshake IN-PROCESS (the sandbox kills
    exec'd children that bind sockets); the supervised child is a harmless
    sleeper, so the supervision arguments and the stop() path stay real.
    """
    import http.server
    import json as _json
    import subprocess as sp
    import sys
    import threading

    data_dir = tmp_path / "data"
    config_dir = data_dir / "claudexor"
    _point_owned_home(monkeypatch, config_dir, data_dir)
    _stale_home(config_dir, marker_data_dir=str(data_dir.resolve()))
    old_descriptor = (config_dir / "daemon" / "control-api.json").read_text()

    monkeypatch.setattr(owned, "resolve_claudexord", lambda: sys.executable)
    spawned: dict = {}
    servers: list = []
    import ouroboros.process_custody as custody_mod

    def fake_spawn(cmd, **kwargs):
        # The SAME chokepoint ensure_running calls; acts like claudexord:
        # mint a fresh token, serve an authenticated handshake, REWRITE the
        # discovery descriptor — then hand back a real supervised child.
        spawned["cmd"] = list(cmd)
        spawned["kwargs"] = {k: kwargs.get(k) for k in ("purpose", "scope")}
        home = pathlib.Path(kwargs["env"]["CLAUDEXOR_CONFIG_DIR"])
        daemon_dir = home / "daemon"
        daemon_dir.mkdir(parents=True, exist_ok=True)
        token = "tok-restarted"
        (daemon_dir / "token").write_text(token, encoding="utf-8")

        class _Daemon(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                # Drain the request body or the NEXT keep-alive request parses
                # mid-stream (the "{json}GET" unsupported-method failure shape).
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                ok = self.headers.get("Authorization") == f"Bearer {token}"
                body = _json.dumps({"compatible": True, "protocolMajor": 3,
                                    "engine": {"version": "9.9.9"}}).encode() if ok else b"{}"
                self.send_response(200 if ok else 401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), _Daemon)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        (daemon_dir / "control-api.json").write_text(_json.dumps({
            "host": "127.0.0.1", "port": server.server_address[1],
            "tokenPath": str(daemon_dir / "token"),
        }), encoding="utf-8")
        # A NEW process group, exactly like the real spawn_supervised child:
        # stop() kills by GROUP id, and a group-sharing fake would take the
        # test process down with it (the SIGKILL-137 this fixture first hit).
        return sp.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                        stdin=sp.DEVNULL, stdout=sp.DEVNULL, stderr=sp.DEVNULL,
                        start_new_session=True)

    monkeypatch.setattr(custody_mod, "spawn_supervised", fake_spawn)

    manager = owned.OwnedClaudexorDaemon()
    assert manager.status_dict()["state"] == "stale"
    try:
        endpoint = manager.ensure_running()
        # Reconciled: the NEW descriptor was re-read and answered our token.
        new_descriptor = (config_dir / "daemon" / "control-api.json").read_text()
        assert new_descriptor != old_descriptor
        assert endpoint.port == _json.loads(new_descriptor)["port"]
        assert spawned["kwargs"] == {"purpose": "claudexor_daemon", "scope": "session"}
        assert manager.status_dict()["state"] == "running"
        # The provision moment (re)wrote OUR ownership marker.
        assert owned.read_ownership_marker()["data_dir"] == str(data_dir.resolve())
        # Restart-only-ours: stop() terminates the SELF-STARTED child.
        assert manager.stop() is True
    finally:
        manager.stop()
        for server in servers:
            server.shutdown()


def test_foreign_responder_on_stale_port_is_disclosed_not_killed(monkeypatch, tmp_path):
    """A live daemon that REFUSES our token on the stale port is foreign:
    typed disclosure, no kill — and it does not block restarting OUR daemon."""
    import http.server
    import json as _json
    import threading

    data_dir = tmp_path / "data"
    config_dir = data_dir / "claudexor"
    _point_owned_home(monkeypatch, config_dir, data_dir)
    _stale_home(config_dir, marker_data_dir=str(data_dir.resolve()))

    class _Refuser(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body = b"{}"
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    foreign = http.server.HTTPServer(("127.0.0.1", 0), _Refuser)
    threading.Thread(target=foreign.serve_forever, daemon=True).start()
    try:
        descriptor = config_dir / "daemon" / "control-api.json"
        body = _json.loads(descriptor.read_text())
        body["port"] = foreign.server_address[1]
        descriptor.write_text(_json.dumps(body), encoding="utf-8")

        manager = owned.OwnedClaudexorDaemon()
        status = manager.status_dict()
        assert status["state"] == "foreign_daemon"
        assert "REFUSED our home's token" in (status["last_error"] or "")
        # No kill: stop() only ever touches a self-started process.
        assert manager.stop() is False
    finally:
        foreign.shutdown()


def test_a_home_marked_for_another_data_plane_is_never_adopted(monkeypatch, tmp_path):
    """The never-adopt rule: a marker naming a different data plane makes
    ensure_running refuse typed BEFORE any spawn — restart there = adoption."""
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    data_dir = tmp_path / "data"
    config_dir = data_dir / "claudexor"
    _point_owned_home(monkeypatch, config_dir, data_dir)
    _stale_home(config_dir, marker_data_dir=str(tmp_path / "someone-elses-data"))
    monkeypatch.setattr(owned, "resolve_claudexord", lambda: "/bin/true")

    manager = owned.OwnedClaudexorDaemon()
    assert manager.status_dict()["ownership_problem"]
    with pytest.raises(ClaudexorUnavailable) as err:
        manager.ensure_running()
    assert err.value.code == "foreign_daemon_home"
