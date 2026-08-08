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
from ouroboros.server_runtime import (
    apply_runtime_provider_defaults,
    has_startup_ready_provider,
)
from ouroboros.settings_setup_contract import parse_subscription_intent
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


def _vouched_harness_ids(profiles: Any) -> set:
    """Harnesses the DAEMON vouches an account for.

    Two independent proofs, both the daemon's own: a native pseudo-row with
    ``native_login_detected`` (the local CLI session), or a registered
    credential profile whose verification PASSED. Ouroboros interprets no
    credential of its own here — it only reads which lanes the engine says it
    can actually run."""
    vouched: set = set()
    if not isinstance(profiles, dict):
        return vouched
    for row in profiles.get("harnessAccounts") or []:
        if isinstance(row, dict) and row.get("native_login_detected"):
            vouched.add(str(row.get("harness_id") or ""))
    for wrapper in profiles.get("profiles") or []:
        if not isinstance(wrapper, dict):
            continue
        profile = wrapper.get("profile")
        status = wrapper.get("status")
        if not isinstance(profile, dict) or not isinstance(status, dict):
            continue
        if not profile.get("enabled"):
            continue
        if str(status.get("verification") or "") == "passed":
            vouched.add(str(profile.get("harness_id") or ""))
    vouched.discard("")
    return vouched


def _discovery_rows(harnesses: Any) -> Dict[str, Dict[str, Any]]:
    return {
        str(row.get("id") or ""): row
        for row in (harnesses or [])
        if isinstance(row, dict) and str(row.get("id") or "")
    }


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
    vouched = _vouched_harness_ids(snapshot.get("profiles"))
    rows = _discovery_rows(snapshot.get("harnesses"))
    wanted = [h for h in PRESET_HARNESSES if h in vouched]
    if not wanted:
        return (), PresetFailure(
            "no_verified_account",
            "The engine vouches no signed-in account for "
            f"{', '.join(PRESET_HARNESSES)}.",
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
    return {
        harness: {
            "status": str((rows.get(harness) or {}).get("status") or ""),
            "access_profiles_supported": list(
                (rows.get(harness) or {}).get("access_profiles_supported") or []),
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
    blocking overlay. It is the authority for install-time behaviour precisely
    because it cannot be forged from a payload — and because the preset marker's
    ABSENCE proves nothing (every install that predates presets lacks it too).
    """
    return not has_startup_ready_provider(settings)


def preset_eligible(settings: Dict[str, Any]) -> bool:
    """Install-time AND not already presetted (D-4: install only, no re-write)."""
    if str(settings.get(PRESET_MARKER_KEY) or "").strip():
        return False
    return install_is_unconfigured(settings)


def _fresh_settings_file() -> bool:
    """No settings.json at all — the narrow condition under which onboarding may
    author the ``light`` safety default (same rule the desktop wizard uses)."""
    from ouroboros.settings_setup_contract import wizard_authors_safety_light

    return wizard_authors_safety_light()


def _write_precondition(expect_preset: bool, expect_safety_light: bool):
    """Re-prove eligibility INSIDE the settings lock, against the state this
    write is about to overwrite."""
    def _check() -> str:
        from ouroboros.config import SETTINGS_PATH, load_settings

        if expect_safety_light and SETTINGS_PATH.exists():
            return ("A settings file appeared while onboarding was being saved; "
                    "refusing to author the first-install safety default over it.")
        if expect_preset and not preset_eligible(load_settings()):
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
             pending_mode: str, safety_light: bool, preset_applied: bool) -> None:
    """The ONE write, plus the established post-save seams."""
    from ouroboros.config import apply_settings_to_env, get_runtime_mode
    from ouroboros.gateway.settings import (
        _apply_settings_save_side_effects,
        _owner_write_settings,
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
    )
    # The RUNNING process keeps its boot runtime mode; the owner's next-boot
    # choice lives on disk only (identical to the endpoint this replaces).
    env_view = dict(current)
    env_view["OUROBOROS_RUNTIME_MODE"] = get_runtime_mode()
    apply_settings_to_env(env_view)
    _start_supervisor_if_needed_for_request(request, current)
    changed = [
        key for key in current
        if str(current.get(key, "") or "") != str(old_settings.get(key, "") or "")
    ]
    _apply_settings_save_side_effects(request, current, old_settings, changed)


async def api_onboarding_complete(request: Request) -> JSONResponse:
    """POST /api/onboarding/complete — finish onboarding in ONE transaction."""
    from ouroboros.config import get_runtime_mode, normalize_runtime_mode
    from ouroboros.gateway.settings import SettingsPreconditionFailed, _owner_audit

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

    pending_mode = normalize_runtime_mode(current.get("OUROBOROS_RUNTIME_MODE"))
    active_mode = get_runtime_mode()
    try:
        await asyncio.to_thread(
            _persist, request, old_settings, current, pending_mode, safety_light,
            preset is not None,
        )
    except SettingsPreconditionFailed as exc:
        return json_error(str(exc), 409, code="onboarding_state_changed", saved=False)
    except PermissionError as exc:
        return json_error(str(exc), 403, saved=False)
    except Exception as exc:
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
    "verified_harness_discoveries",
]
