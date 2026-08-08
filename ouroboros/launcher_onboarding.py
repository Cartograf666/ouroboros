"""First-run onboarding as the desktop launcher presents it.

Extracted from ``launcher.py`` (which stays the process/window orchestrator)
because the wizard is no longer a detached pre-server document: the gateway
starts first and this module only opens a window onto its ``/onboarding`` page.
"""

from __future__ import annotations

import logging

from ouroboros.config import (
    apply_settings_to_env as _apply_settings_to_env,
    load_settings,
    save_settings,
)
from ouroboros.onboarding_wizard import prepare_onboarding_settings
from ouroboros.server_runtime import apply_runtime_provider_defaults, has_startup_ready_provider

# The launcher's own logger: these lines belong in launcher.log next to the
# startup sequence they are part of.
log = logging.getLogger("launcher")


def prepare_first_run_settings() -> tuple[dict, bool]:
    """Normalize provider defaults; answer whether first-run onboarding is due.

    Runs BEFORE the managed server starts, because the answer decides what the
    launcher shows once it is healthy. The onboarding SURFACE is not rendered
    here: the live server serves it (``present_first_run_onboarding``).
    """
    settings, provider_defaults_changed, _provider_default_keys = apply_runtime_provider_defaults(load_settings())
    # Persist the pre-server normalization ONLY for an install that already has a
    # settings file. On a FRESH install this save would CREATE the file before the
    # owner's first onboarding save, and every fresh-install proof is gated on
    # that freshness — safety-light authorship, install-time agent presets (a
    # local-first launch with LOCAL_MODEL_SOURCE in the environment reaches here
    # with changed=True and would silently lose Light). Nothing is dropped: the
    # completion save persists the same normalization, and the managed server
    # keeps the mirror-image guard in its lifespan.
    from ouroboros.config import SETTINGS_PATH as _settings_path

    if provider_defaults_changed and _settings_path.exists():
        # Owner-process boundary: a first-run/provider save may elevate runtime mode.
        save_settings(settings, allow_elevation=True)
    _apply_settings_to_env(settings)
    return settings, not has_startup_ready_provider(settings)


def present_first_run_onboarding(settings: dict, port: int, *, headless: bool = False) -> dict:
    """Show first-run onboarding served by the ALREADY-RUNNING managed gateway.

    D-8: one wizard on every host. The setup window loads the same live
    ``/onboarding`` page a browser owner sees and can reach ``/api/*`` while the
    owner is still setting up — which is what makes connecting an agent
    subscription during first-run possible at all. A gateway without a
    supervisor is a supported runtime state (ARCHITECTURE §2), so nothing new
    is started to make this work.

    Returns ``{"saved": bool, "restart_required": bool}``. Closing the window
    without saving stays non-fatal: startup continues and the main window's
    blocking overlay still offers the same wizard.
    """
    outcome = {"saved": False, "restart_required": False}

    if headless:
        # Browser mode (#56): no GUI backend for a setup window, so the
        # ESTABLISHED web-onboarding flow (/api/onboarding probe + blocking
        # overlay + hot supervisor start after save) is the first-run surface —
        # the same path Docker/browser installs already use.
        log.info(
            "First-run setup window skipped: no GUI backend; onboarding is "
            "served in the browser."
        )
        print(
            "No GUI backend (GTK/QT) for the setup window; onboarding is "
            "served in the browser at the URL below.",
            flush=True,
        )
        return outcome

    import webview

    class OnboardingHostApi:
        """Window-lifecycle bridge for the desktop setup window.

        Deliberately NOT a settings authority: the gateway persists completion,
        exactly as for a browser owner. ``save_wizard`` is the one disclosed
        exception and exists only while the atomic completion endpoint is not
        deployed (see the seam in ``web/modules/onboarding_wizard.js``).
        """

        def save_wizard(self, data: dict) -> str:
            prepared_settings, error = prepare_onboarding_settings(data, settings)
            if error:
                return error
            # Rev.3-2 (v6.82.0): a FRESH wizard save explicitly authors the
            # new-install "light" safety coverage (prepare_onboarding_settings only
            # adds it when the settings file carries no explicit choice). Name the
            # key as authored and allow this owner-driven onboarding write past the
            # full->light ratchet; every other save path keeps the ratchet intact.
            # The DESKTOP host authors it — the shared validator must not, because
            # web/Docker onboarding posts the same payload through the non-owner
            # generic /api/settings path. Eligibility is a fresh install (no
            # settings file); the persist seam re-proves it under the lock.
            from ouroboros.settings_setup_contract import wizard_authors_safety_light

            wizard_authors_safety = wizard_authors_safety_light()
            if wizard_authors_safety:
                prepared_settings["OUROBOROS_SAFETY_MODE"] = "light"
            settings.update(prepared_settings)
            settings.update(apply_runtime_provider_defaults(settings)[0])
            try:
                save_settings(
                    settings,
                    allow_elevation=True,
                    onboarding_safety_default=wizard_authors_safety,
                )
                _apply_settings_to_env(settings)
                # Window teardown belongs to onboarding_finished, which the page
                # calls next: this method only persists.
                return "ok"
            except Exception as exc:
                return f"Failed to save: {exc}"

        def onboarding_finished(self, result: dict | None = None) -> str:
            payload = result if isinstance(result, dict) else {}
            if payload.get("ok", True):
                outcome["saved"] = True
            if payload.get("restart_required"):
                outcome["restart_required"] = True
            for window in webview.windows:
                window.destroy()
            return "ok"

    webview.create_window(
        "Ouroboros — Setup",
        url=f"http://127.0.0.1:{port}/onboarding",
        js_api=OnboardingHostApi(),
        width=980,
        height=780,
        min_size=(840, 640),
    )
    webview.start()
    return outcome


__all__ = ["prepare_first_run_settings", "present_first_run_onboarding"]
