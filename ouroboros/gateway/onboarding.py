"""Atomic onboarding completion — ONE owner-scoped save (D-8).

Web onboarding used to finish with TWO writes: ``POST /api/settings`` with the
wizard payload, then ``POST /api/owner/runtime-mode``. A failure between them
left an install whose providers were saved and whose runtime mode was not, and
there was no seam where an install-time decision (the agent subscription
preset, the fresh-install ``light`` safety default) could be part of the same
transaction. ``POST /api/onboarding/complete`` replaces both with one ordered
transaction:

1. re-prove FRESH-INSTALL status server-side — a browser boolean is a request,
   never an authority;
2. validate the wizard payload through the SHARED setup validator and the
   startup gate (a subscription alone never satisfies it, D-1);
3. read ONE fresh Claudexor snapshot when the payload declares subscriptions
   were connected, and compile the preset from LIVE discovery;
4. apply the ordinary provider normalization FIRST, then add the structured
   preset keys on top (R8: normalization is continuous re-derivation, the
   preset is an install-time transaction — they must not be taught about each
   other);
5. persist settings + runtime mode + safety default + the one-shot preset
   marker in a single write whose eligibility is re-proved under the settings
   lock;
6. only then start the supervisor.

A daemon that cannot answer at save time is a TYPED failure that persists
NOTHING and keeps the wizard open, with an explicit "finish without agent
defaults" escape hatch (``skipSubscriptionPresets``). Saving a guessed model id
is never the fallback: the id would be written into the reviewer configuration
the owner believes is live, and would only fail later, inside a real review.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.gateway._helpers import json_error
from ouroboros.gateway.owner_settings import (
    CommitBoundary,
    SettingsLockUnavailable,
    SettingsPreconditionFailed,
    _owner_audit,
    _owner_write_settings,
    post_commit_failure_response,
)
from ouroboros.server_runtime import (
    apply_runtime_provider_defaults,
    has_startup_ready_provider,
)
from ouroboros.settings_setup_contract import (
    ONBOARDING_COMPLETED_KEY,
    parse_subscription_intent,
)
from ouroboros.subscription_install_presets import (
    PRESET_HARNESSES,
    PRESET_MARKER_KEY,
    HarnessDiscovery,
    SubscriptionInstallPreset,
    compile_install_preset,
)

log = logging.getLogger(__name__)

# The ONE owner-facing sentence for every way the preset step can fail. The
# machine-readable ``code`` beside it says which; the copy stays constant so the
# wizard does not have to translate engine vocabulary.
PRESET_UNVERIFIED_MESSAGE = (
    "The agent accounts were connected, but their models could not be "
    "verified right now, so nothing was saved. Restart or repair the agent "
    "engine and try again, or finish without agent defaults."
)


@dataclass(frozen=True)
class PresetFailure:
    """Why the preset step could not run. Typed, and never half-applied."""

    code: str
    detail: str

    def as_response(self) -> JSONResponse:
        return json_error(
            PRESET_UNVERIFIED_MESSAGE, 503,
            code=self.code, detail=self.detail, can_skip=True, saved=False,
        )


# ---------------------------------------------------------------------------
# Reading the live account/model snapshot (the ONE Claudexor read).
# ---------------------------------------------------------------------------


# The daemon's credential-kind enum is {config_dir_login, oauth_token, api_key}.
# The first two ARE a signed-in vendor session — what a subscription is. The
# third is metered API spend, which is precisely what a preset row must never
# become (D-3): such a row would either refuse at review time or quietly bill
# the owner's API key for work they connected a subscription to cover.
_SUBSCRIPTION_CREDENTIAL_KINDS = frozenset({"config_dir_login", "oauth_token"})
# ``next_up.route`` for the native/default subject. The daemon names it
# ``local_session`` for a CLI login and ``api_key`` for a configured key; older
# daemons omit the field, in which case ``native_login_detected`` (the engine's
# own "a vendor login is detected") carries the claim on its own.
_API_KEY_NATIVE_ROUTE = "api_key"
# Harness rows the engine will not run at all. Anything else it publishes
# (``ok``, ``degraded``) stays admissible: degradation is the engine's business,
# and a preset seat still resolves against the models it discovered.
_UNRUNNABLE_HARNESS_STATUS = frozenset({"unavailable"})


def _discovery_rows(harnesses: Any) -> Dict[str, Dict[str, Any]]:
    return {
        str(row.get("id") or ""): row
        for row in (harnesses or [])
        if isinstance(row, dict) and str(row.get("id") or "")
    }


def _profile_index(profiles: Any) -> Dict[Tuple[str, str], Dict[str, Any]]:
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for wrapper in (profiles or []):
        if not isinstance(wrapper, dict):
            continue
        profile = wrapper.get("profile")
        if not isinstance(profile, dict):
            continue
        key = (str(profile.get("harness_id") or ""), str(profile.get("profile_id") or ""))
        index[key] = wrapper
    return index


def _next_up_verdict(
    harness: str, account: Dict[str, Any], profiles: Dict[Tuple[str, str], Dict[str, Any]],
) -> Tuple[bool, str]:
    """Would an UNPINNED run of this harness route through a subscription seat?

    ``next_up`` is the daemon's own server-computed answer to "who would an
    unpinned run use next", derived from enabled profiles + native readiness +
    quota. Ouroboros reads that answer instead of re-deriving one: the rotation
    is the engine's (D28), and a second policy here would disagree with it the
    first time an account changed. The only judgement left is whether the seat
    the engine names is a SUBSCRIPTION or an API key."""
    next_up = account.get("next_up") if isinstance(account.get("next_up"), dict) else {}
    kind = str(next_up.get("kind") or "")
    if kind == "none":
        return False, str(next_up.get("reason") or "the engine has nothing routable for it")
    if kind == "native":
        if not account.get("native_login_detected"):
            return False, "no signed-in session is detected for the default account"
        if not account.get("native_credentials_enabled"):
            return False, "its default login is disabled in the engine's credential ladder"
        route = str(next_up.get("route") or "")
        if route == _API_KEY_NATIVE_ROUTE:
            return False, "an unpinned run would route through an API key, not a subscription"
        return True, f"default session ({route or 'route not reported'})"
    if kind == "profile":
        profile_id = str(next_up.get("profileId") or "")
        wrapper = profiles.get((harness, profile_id))
        if wrapper is None:
            return False, f"the engine names account {profile_id!r}, which it does not list"
        profile = wrapper.get("profile") if isinstance(wrapper.get("profile"), dict) else {}
        status = wrapper.get("status") if isinstance(wrapper.get("status"), dict) else {}
        credential_kind = str(profile.get("credential_kind") or "")
        if credential_kind not in _SUBSCRIPTION_CREDENTIAL_KINDS:
            return False, (f"account {profile_id!r} is an API credential "
                           f"({credential_kind or 'kind not reported'}), not a subscription")
        if not profile.get("enabled"):
            return False, f"account {profile_id!r} is disabled"
        availability = str(status.get("availability") or "")
        if availability and availability != "available":
            return False, f"account {profile_id!r} is {availability}"
        if str(status.get("verification") or "") != "passed":
            return False, f"account {profile_id!r} has not verified"
        return True, f"account {profile_id!r} ({credential_kind})"
    return False, f"the engine reports an unknown routing state {kind or 'none given'!r}"


def subscription_routable_harnesses(
    snapshot: Dict[str, Any],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """``(routable, refused)`` — which preset harnesses can actually run a
    subscription session right now, and why the others cannot.

    An account being SIGNED IN is not the question the preset needs answered.
    The question is whether a subscription session would actually start, and the
    daemon already answers it: the harness row says whether the harness itself
    is enabled and runnable, and the per-harness accounts row says which
    credential an unpinned run would take next. Both come from the engine."""
    routable: Dict[str, str] = {}
    refused: Dict[str, str] = {}
    rows = _discovery_rows(snapshot.get("harnesses"))
    payload = snapshot.get("profiles") if isinstance(snapshot.get("profiles"), dict) else {}
    profiles = _profile_index(payload.get("profiles"))
    accounts = {
        str(row.get("harness_id") or ""): row
        for row in (payload.get("harnessAccounts") or [])
        if isinstance(row, dict)
    }
    for harness in PRESET_HARNESSES:
        account = accounts.get(harness)
        if account is None:
            continue  # the engine publishes no accounts authority for it: silent absence
        row = rows.get(harness)
        if row is None:
            refused[harness] = "the engine does not list this harness"
            continue
        if not row.get("enabled"):
            refused[harness] = "the engine has this harness disabled"
            continue
        status = str(row.get("status") or "")
        if status in _UNRUNNABLE_HARNESS_STATUS:
            refused[harness] = f"the engine reports it {status}"
            continue
        ok, evidence = _next_up_verdict(harness, account, profiles)
        (routable if ok else refused)[harness] = evidence
    return routable, refused


def verified_harness_discoveries(
    snapshot: Dict[str, Any],
) -> Tuple[Tuple[HarnessDiscovery, ...], Optional[PresetFailure]]:
    """Turn one ``/api/claudexor/status?include=models`` snapshot into the
    compiler's input, or a typed failure. PURE — unit-testable with no daemon."""
    daemon = snapshot.get("daemon") if isinstance(snapshot, dict) else None
    state = str((daemon or {}).get("state") or "")
    if state != "running":
        return (), PresetFailure(
            "daemon_unavailable",
            f"The agent engine is {state or 'not running'}"
            + (f" ({(daemon or {}).get('last_error')})" if (daemon or {}).get("last_error") else ""),
        )
    routable, refused = subscription_routable_harnesses(snapshot)
    rows = _discovery_rows(snapshot.get("harnesses"))
    wanted = [h for h in PRESET_HARNESSES if h in routable]
    if not wanted:
        detail = "; ".join(f"{harness}: {reason}" for harness, reason in sorted(refused.items()))
        return (), PresetFailure(
            "no_verified_account",
            "The engine can run no subscription session for "
            f"{', '.join(PRESET_HARNESSES)}." + (f" {detail}" if detail else ""),
        )
    discoveries: List[HarnessDiscovery] = []
    for harness in wanted:
        row = rows.get(harness) or {}
        if row.get("models_error"):
            return (), PresetFailure(
                "models_unavailable",
                f"Model discovery for {harness} failed: {row.get('models_error')}",
            )
        model_ids = tuple(
            str(model.get("id") or "")
            for model in (row.get("models") or [])
            if isinstance(model, dict) and str(model.get("id") or "")
        )
        if not model_ids:
            return (), PresetFailure(
                "models_unavailable",
                f"The engine listed no models for {harness}.",
            )
        discoveries.append(HarnessDiscovery(harness_id=harness, model_ids=model_ids))
    return tuple(discoveries), None


def _harness_capability(snapshot: Dict[str, Any], connected: Sequence[str]) -> Dict[str, Any]:
    """Disclosure-only evidence recorded in the receipt (never a gate)."""
    rows = _discovery_rows(snapshot.get("harnesses"))
    routable, _refused = subscription_routable_harnesses(snapshot)
    return {
        harness: {
            "status": str((rows.get(harness) or {}).get("status") or ""),
            "access_profiles_supported": list(
                (rows.get(harness) or {}).get("access_profiles_supported") or []),
            # WHICH seat the engine said an unpinned run would take. The rows are
            # unpinned by design (D28), so this records the evidence, not a pin.
            "subscription_route": routable.get(harness, ""),
        }
        for harness in connected
    }


def _read_harness_snapshot() -> Dict[str, Any]:
    """The ONE blocking Claudexor read, through the SAME projection the accounts
    panel uses (no second discovery path)."""
    from ouroboros.gateway.claudexor_accounts import _status_payload

    return _status_payload(True)


async def resolve_install_preset(
) -> Tuple[Optional[SubscriptionInstallPreset], Optional[PresetFailure]]:
    """One fresh snapshot -> one compiled preset, or a typed failure."""
    try:
        snapshot = await asyncio.to_thread(_read_harness_snapshot)
    except Exception as exc:  # a dead/broken engine is a failure, not a crash
        log.warning("Claudexor snapshot for onboarding presets failed", exc_info=True)
        return None, PresetFailure("daemon_unavailable", f"{type(exc).__name__}: {exc}")
    discoveries, failure = verified_harness_discoveries(snapshot)
    if failure is not None:
        return None, failure
    preset = compile_install_preset(
        discoveries,
        capability=_harness_capability(snapshot, [d.harness_id for d in discoveries]),
    )
    if not preset.ok:
        refusal = preset.refusal.as_dict() if preset.refusal else {}
        return None, PresetFailure(
            str(refusal.get("code") or "preset_refused"),
            str(refusal.get("message") or "The preset could not be compiled."),
        )
    return preset, None


# ---------------------------------------------------------------------------
# The install-time latch (server-side authority).
# ---------------------------------------------------------------------------


def install_is_unconfigured(settings: Dict[str, Any]) -> bool:
    """Is this install still IN onboarding, as the server itself sees it?

    The same predicate that decides whether ``GET /api/onboarding`` mounts the
    blocking overlay. It is NOT install-time on its own: an install that has run
    for a year and whose one provider key stopped working answers True here too,
    which is why ``preset_eligible`` requires two further proofs."""
    return not has_startup_ready_provider(settings)


def preset_eligible(settings: Dict[str, Any]) -> bool:
    """May this save apply the install-time agent preset (D-4)?

    Three independent proofs, because "no working provider" alone is a state an
    OLD install reaches whenever its key stops working — and presetting there
    would overwrite reviewer/subagent choices the owner made themselves:

    * onboarding has never completed here (the durable ``…COMPLETED_AT`` fact,
      written on EVERY completion — skipped and subscription-less included);
    * no preset generation has been applied;
    * the install still has no settings file at all, the same "genuinely fresh
      install" rule the wizard already uses for the ``light`` safety default.

    Marker ABSENCE alone would prove nothing (every install that predates this
    release lacks both), which is what the file-existence proof answers."""
    if str(settings.get(ONBOARDING_COMPLETED_KEY) or "").strip():
        return False
    if str(settings.get(PRESET_MARKER_KEY) or "").strip():
        return False
    if not install_is_unconfigured(settings):
        return False
    return _fresh_settings_file()


def _fresh_settings_file() -> bool:
    """No settings.json at all — the narrow condition under which onboarding may
    author the ``light`` safety default (same rule the desktop wizard uses)."""
    from ouroboros.settings_setup_contract import wizard_authors_safety_light

    return wizard_authors_safety_light()


def _write_precondition(expect_preset: bool, expect_safety_light: bool):
    """Re-prove eligibility INSIDE the settings lock, against the state this
    write is about to overwrite.

    The re-read is ``load_settings_lock_held``: the settings lock is not
    re-entrant, so the ordinary ``load_settings()`` would wait out its full 2s
    timeout and then read anyway — two seconds added to every onboarding save
    for a lock it already holds."""
    def _check() -> str:
        from ouroboros.config import SETTINGS_PATH, load_settings_lock_held

        if expect_safety_light and SETTINGS_PATH.exists():
            return ("A settings file appeared while onboarding was being saved; "
                    "refusing to author the first-install safety default over it.")
        if expect_preset and not preset_eligible(load_settings_lock_held()):
            return ("This install is no longer in first-run onboarding; refusing "
                    "to apply install-time agent defaults over it.")
        return ""

    return _check


# ---------------------------------------------------------------------------
# The endpoint.
# ---------------------------------------------------------------------------


def _prepared_settings(body: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
    """(old_settings, prepared_settings, error) through the SHARED validator."""
    from ouroboros.config import load_settings
    from ouroboros.onboarding_wizard import prepare_onboarding_settings

    old_settings = load_settings()
    prepared, error = prepare_onboarding_settings(body, old_settings)
    if error:
        return old_settings, {}, str(error)
    normalized, _changed, _keys = apply_runtime_provider_defaults(prepared)
    if not has_startup_ready_provider(normalized):
        # D-1: the launch gate is API-key-or-local-model. An agent
        # subscription is an amplifier, never the thing that satisfies it.
        return old_settings, {}, (
            "Add at least one API key or a local model before finishing. An "
            "agent subscription strengthens Ouroboros but cannot run the "
            "main model on its own."
        )
    return old_settings, normalized, ""


def _persist(request: Request, old_settings: Dict[str, Any], current: Dict[str, Any],
             pending_mode: str, safety_light: bool, preset_applied: bool,
             boundary: CommitBoundary) -> None:
    """The ONE write, plus the established post-save seams.

    ``boundary`` is committed the instant the bytes land, so the endpoint can
    distinguish "the transaction was refused" from "the transaction landed and
    a later step failed" — two facts that were previously both reported as
    ``saved=False``."""
    from ouroboros.config import apply_settings_to_env, get_runtime_mode
    from ouroboros.gateway.settings import (
        _apply_settings_save_side_effects,
        _start_supervisor_if_needed_for_request,
    )

    to_save = dict(current)
    to_save["OUROBOROS_RUNTIME_MODE"] = pending_mode
    authored = ("OUROBOROS_SAFETY_MODE",) if safety_light else ()
    _owner_write_settings(
        to_save,
        authored_keys=authored,
        allow_safety_lowering=safety_light,
        precondition=_write_precondition(preset_applied, safety_light),
        boundary=boundary,
    )
    # The RUNNING process keeps its boot runtime mode; the owner's next-boot
    # choice lives on disk only (identical to the endpoint this replaces).
    boundary.at("environment projection")
    env_view = dict(current)
    env_view["OUROBOROS_RUNTIME_MODE"] = get_runtime_mode()
    apply_settings_to_env(env_view)
    boundary.at("supervisor start")
    _start_supervisor_if_needed_for_request(request, current)
    boundary.at("hot-reload")
    changed = [
        key for key in current
        if str(current.get(key, "") or "") != str(old_settings.get(key, "") or "")
    ]
    _apply_settings_save_side_effects(request, current, old_settings, changed)


async def api_onboarding_complete(request: Request) -> JSONResponse:
    """POST /api/onboarding/complete — finish onboarding in ONE transaction."""
    from ouroboros.config import get_runtime_mode, normalize_runtime_mode
    from ouroboros.utils import utc_now_iso

    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return json_error("JSON body must be an object.", 400)

    old_settings, current, error = _prepared_settings(body)
    if error:
        return json_error(error, 400)

    subscriptions_connected, skip_presets = parse_subscription_intent(body)
    eligible = preset_eligible(old_settings)
    safety_light = _fresh_settings_file()
    if safety_light:
        # Rev.3-2 parity with the desktop wizard: a genuinely FRESH install
        # authors the new-install ``light`` safety coverage here, because the
        # shared validator must not (web/Docker also reach it through the
        # non-owner generic settings path). Eligibility is "no settings file
        # yet"; the persist seam re-proves it under the lock.
        current["OUROBOROS_SAFETY_MODE"] = "light"
    preset: Optional[SubscriptionInstallPreset] = None
    preset_reason = "not_requested"
    if not eligible:
        preset_reason = "not_install_time"
    elif skip_presets:
        preset_reason = "skipped_by_owner"
    elif subscriptions_connected:
        preset, failure = await resolve_install_preset()
        if failure is not None:
            return failure.as_response()
        preset_reason = "applied"
        # R8 ordering: provider normalization has ALREADY run over `current`;
        # the structured preset keys land on top of it, never through it.
        current.update(preset.settings_keys())

    # The durable completion fact rides in the SAME write, whatever the preset
    # did: a completion that connected nothing must still close the window.
    current[ONBOARDING_COMPLETED_KEY] = utc_now_iso()
    pending_mode = normalize_runtime_mode(current.get("OUROBOROS_RUNTIME_MODE"))
    active_mode = get_runtime_mode()
    boundary = CommitBoundary()
    try:
        await asyncio.to_thread(
            _persist, request, old_settings, current, pending_mode, safety_light,
            preset is not None, boundary,
        )
    except Exception as exc:
        if boundary.committed:
            # The transaction LANDED; a post-save step did not. Saying
            # "nothing was saved" here would send the owner back through an
            # onboarding that is already complete (BIBLE P1).
            return post_commit_failure_response(exc, boundary)
        if isinstance(exc, SettingsPreconditionFailed):
            return json_error(str(exc), 409, code="onboarding_state_changed", saved=False)
        if isinstance(exc, SettingsLockUnavailable):
            return json_error(str(exc), 503, code="settings_locked", saved=False, can_skip=True)
        if isinstance(exc, PermissionError):
            return json_error(str(exc), 403, saved=False)
        log.exception("onboarding completion failed")
        return json_error(f"{type(exc).__name__}: {exc}", 500, saved=False)

    _owner_audit(request, "onboarding_complete", {
        "runtime_mode": pending_mode,
        "preset": preset_reason,
        "preset_harnesses": list(preset.connected) if preset else [],
        "subscriptions_connected": subscriptions_connected,
    })
    payload: Dict[str, Any] = {
        "ok": True,
        "status": "saved",
        "runtime_mode": pending_mode,
        "restart_required": active_mode != pending_mode,
        "preset": {
            "applied": preset is not None,
            "reason": preset_reason,
            "harnesses": list(preset.connected) if preset else [],
            "receipt": dict(preset.receipt) if preset else {},
        },
    }
    return JSONResponse(payload)


__all__ = [
    "PRESET_UNVERIFIED_MESSAGE",
    "PresetFailure",
    "api_onboarding_complete",
    "install_is_unconfigured",
    "preset_eligible",
    "resolve_install_preset",
    "subscription_routable_harnesses",
    "verified_harness_discoveries",
]
