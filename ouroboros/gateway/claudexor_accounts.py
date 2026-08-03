"""Harness Accounts HTTP surface (D30): three THIN proxies, zero auth logic.

Ouroboros's own Claudexor daemon (``claudexor_daemon.py``) owns every account
fact — profiles, login jobs, device-code custody, the two honest verification
statuses, quota windows. The browser cannot talk to the daemon directly (its
control plane is loopback-Origin-guarded and bearer-token'd; the token must
never reach a page), so these handlers translate: status aggregation, login
job create, login job read/cancel. Nothing here interprets a credential and
nothing here stores one.

Login shapes ("красота-сначала", D30): a structural device-code card wherever
the engine can host the flow itself (codex today; claude/cursor once the
engine's own upstream extension discloses their OAuth link the same way), and
the FALLBACK is a copy-paste ``claudexor setup attach <jobId>`` command the
user runs in the USER'S OWN terminal outside this UI, with the card polling
the job. There is no in-app terminal surface, and none may be added.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.gateway._helpers import json_error, request_json_or

log = logging.getLogger(__name__)

# "daemon" transport is DELIBERATELY not accepted: on macOS it is the
# Terminal.app handoff, and D30 forbids that mechanism outright — the fallback
# is the copy-paste attach command in the USER'S own terminal, never a window
# this app opens. When the engine's upstream extension makes claude/cursor
# logins fully daemon-hosted (structural OAuth disclosure, no Terminal), the
# device path below simply starts working for them and nothing here changes.
_LOGIN_TRANSPORTS = ("", "client_pty")


def _status_payload(include_models: bool) -> Dict[str, Any]:
    from ouroboros.claudexor_daemon import get_owned_daemon, owned_config_dir
    from ouroboros.gateways.claudexor import (
        ClaudexorGateway,
        ClaudexorUnavailable,
        discover_daemon_at,
    )

    daemon = get_owned_daemon().status_dict()
    payload: Dict[str, Any] = {
        "daemon": daemon,
        "config_dir": str(owned_config_dir()),
        "harnesses": [],
        "profiles": {},
        "quota": [],
    }
    if daemon.get("state") != "running":
        return payload
    try:
        endpoint = discover_daemon_at(owned_config_dir())
        with ClaudexorGateway(endpoint) as gateway:
            gateway.handshake()
            payload["daemon"]["engine_version"] = gateway.engine_version
            catalog = gateway.agent_capabilities()
            rows: List[Dict[str, Any]] = []
            for row in catalog.get("harnesses") or []:
                if not isinstance(row, dict):
                    continue
                projected = {
                    "id": str(row.get("id") or ""),
                    "display_name": str(row.get("displayName") or row.get("id") or ""),
                    "status": str(row.get("status") or ""),
                    "enabled": bool(row.get("enabled")),
                    "provider_family": str(row.get("providerFamily") or ""),
                    "json_schema_output": bool(
                        row.get("json_schema_output") or row.get("jsonSchemaOutput")
                    ),
                    "access_profiles_supported": [
                        str(v) for v in row.get("accessProfilesSupported") or []
                    ],
                }
                if include_models and projected["id"]:
                    try:
                        projected["models"] = gateway.harness_models(projected["id"])
                    except ClaudexorUnavailable as exc:
                        projected["models"] = []
                        projected["models_error"] = exc.code
                rows.append(projected)
            payload["harnesses"] = rows
            payload["profiles"] = gateway.credential_profiles()
            payload["quota"] = gateway.quota_snapshots()
    except ClaudexorUnavailable as exc:
        payload["daemon"]["state"] = "unreachable"
        payload["daemon"]["last_error"] = f"{exc.code}: {exc}"
    return payload


async def api_claudexor_status(request: Request) -> JSONResponse:
    """GET /api/claudexor/status[?include=models] — owned-daemon state plus the
    daemon's own account/quota/catalog truth. Read-only; never spawns."""
    include_models = "models" in str(request.query_params.get("include") or "")
    try:
        return JSONResponse(await asyncio.to_thread(_status_payload, include_models))
    except Exception as exc:
        log.exception("api_claudexor_status failed")
        return json_error(f"{type(exc).__name__}: Claudexor status failed")


def _build_login_request(harness: str, profile_id: str, transport: str,
                         login_flow: str) -> Dict[str, Any]:
    """The setup-job request body, honoring the engine's wire contract.

    Pure so the harness-specific transport rule is unit-testable without a
    daemon. The rule mirrors Claudexor's own setup-transport refinement, not
    Ouroboros policy: codex client_pty (the terminal-attach fallback) REQUIRES
    loginFlow=browser_redirect — device/app-server flows are daemon-owned and a
    client_pty job without it is a hard 400 — and loginFlow exists ONLY for
    codex, so it is never sent for another harness (that too is a 400).
    """
    request: Dict[str, Any] = {"harness": harness, "action": "login", "authRequest": "subscription"}
    if profile_id:
        request["profileId"] = profile_id
    # A NON-codex login with no explicit transport would default daemon-side to
    # transport=daemon — the macOS Terminal.app handoff D30 forbids. Force the
    # attach fallback instead: the card polls the sealed job, consumes the
    # structured OAuth disclosure WHEN the engine surfaces one, and otherwise
    # shows the copy-paste command for the user's own terminal.
    if harness != "codex" and not transport:
        transport = "client_pty"
    if transport:
        request["transport"] = transport
    if harness == "codex" and transport == "client_pty":
        login_flow = login_flow or "browser_redirect"
    if login_flow and harness == "codex":
        request["loginFlow"] = login_flow
    return request


def _login_create(body: Dict[str, Any]) -> Dict[str, Any]:
    from ouroboros.claudexor_daemon import attach_login_command, get_owned_daemon
    from ouroboros.gateways.claudexor import ClaudexorGateway

    harness = str(body.get("harness") or "").strip()
    if not harness:
        raise ValueError("harness is required")
    profile_id = str(body.get("profile_id") or "").strip()
    transport = str(body.get("transport") or "").strip()
    if transport not in _LOGIN_TRANSPORTS:
        raise ValueError("transport must be omitted or 'client_pty' (the Terminal.app handoff transport is not offered)")
    login_flow = str(body.get("login_flow") or "").strip()
    # Provisioning moment: the FIRST login action is what spawns the owned
    # daemon (and thereby flips default discovery to it) — an owner action,
    # never a boot-time side effect.
    endpoint = get_owned_daemon().ensure_running()
    with ClaudexorGateway(endpoint) as gateway:
        gateway.handshake()
        if profile_id:
            try:
                gateway.create_credential_profile(harness, profile_id)
            except Exception:
                # An existing registration is fine; the login job below is
                # what decides whether the profile is usable.
                log.debug("credential-profile create skipped", exc_info=True)
        request_body = _build_login_request(harness, profile_id, transport, login_flow)
        job = gateway.setup_job_create(request_body)
    job_id = str(job.get("id") or job.get("jobId") or "")
    out = {"job": job, "job_id": job_id}
    if request_body.get("transport") == "client_pty" and job_id:
        # The fallback card's copy-paste command, run OUTSIDE this UI. Read
        # from the REQUEST actually sent (the non-codex default is forced
        # inside the builder, not in the caller's local variable).
        out["attach_command"] = attach_login_command(job_id)
    return out


async def api_claudexor_login(request: Request) -> JSONResponse:
    """POST /api/claudexor/login — create one login job on the owned daemon."""
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    body: Dict[str, Any] = await request_json_or(request, {})
    try:
        return JSONResponse(await asyncio.to_thread(_login_create, dict(body or {})))
    except ValueError as exc:
        return json_error(str(exc), 400)
    except ClaudexorUnavailable as exc:
        return json_error(f"{exc.code}: {exc}", 503)
    except Exception as exc:
        log.exception("api_claudexor_login failed")
        return json_error(f"{type(exc).__name__}: Claudexor login failed")


def _login_job_read(job_id: str) -> Dict[str, Any]:
    from ouroboros.claudexor_daemon import attach_login_command, owned_config_dir
    from ouroboros.gateways.claudexor import ClaudexorGateway, discover_daemon_at

    endpoint = discover_daemon_at(owned_config_dir())
    with ClaudexorGateway(endpoint) as gateway:
        gateway.handshake()
        snapshot = gateway.setup_job_snapshot(job_id)
    return {"job": snapshot, "attach_command": attach_login_command(job_id)}


def _login_job_cancel(job_id: str) -> Dict[str, Any]:
    from ouroboros.claudexor_daemon import owned_config_dir
    from ouroboros.gateways.claudexor import ClaudexorGateway, discover_daemon_at

    endpoint = discover_daemon_at(owned_config_dir())
    with ClaudexorGateway(endpoint) as gateway:
        gateway.handshake()
        return gateway.setup_job_cancel(job_id)


async def api_claudexor_login_job(request: Request) -> JSONResponse:
    """GET/DELETE /api/claudexor/login/{job_id} — poll or cancel one job.

    The GET snapshot carries the transient device-code disclosure when the
    flow has one; the DELETE is the card's cancel action.
    """
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    job_id = str(request.path_params.get("job_id") or "").strip()
    if not job_id:
        return json_error("job_id is required", 400)
    try:
        if request.method == "DELETE":
            return JSONResponse(await asyncio.to_thread(_login_job_cancel, job_id))
        return JSONResponse(await asyncio.to_thread(_login_job_read, job_id))
    except ClaudexorUnavailable as exc:
        return json_error(f"{exc.code}: {exc}", 503)
    except Exception as exc:
        log.exception("api_claudexor_login_job failed")
        return json_error(f"{type(exc).__name__}: Claudexor login job read failed")


__all__ = [
    "api_claudexor_login",
    "api_claudexor_login_job",
    "api_claudexor_status",
]
