"""One contract for masking settings secrets on the wire.

The Settings API answers a GET with a placeholder instead of the stored
credential, so any client can post that placeholder back — the UI does exactly
that when the owner saves without touching a secret field. Producing the mask
and recognizing it therefore have to live in the same file: once they drift, an
unrecognized placeholder is persisted as the credential and every consumer
(environment apply, provider catalogs, capability probes) sends it as an
``Authorization`` value, which the provider rejects.
"""

from __future__ import annotations

from typing import Any, Dict

# Credentials the Settings API answers a GET with a PLACEHOLDER instead of the
# stored value. The same set gates the read-side repair in
# ``config.load_settings`` and the write-side merge in
# ``gateway.settings._merge_settings_payload``.
MASKED_SECRET_SETTING_KEYS = frozenset({
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY",
    "GIGACHAT_CREDENTIALS",
    "GIGACHAT_PASSWORD",
    "ANTHROPIC_API_KEY",
    "MINIMAX_API_KEY",
    "GITHUB_TOKEN",
    "OUROBOROS_NETWORK_PASSWORD",
})


def looks_masked_secret(value: Any) -> bool:
    """Whether ``value`` is a display placeholder rather than a real secret.

    Deliberately narrow: only the shapes this codebase actually emits qualify,
    so a short but genuine credential is never mistaken for a placeholder.
    """
    text = str(value or "").strip()
    return text in ("***", "***set***") or text.endswith("...")


def strip_masked_secrets(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Blank a stored placeholder so it can never be applied as a credential.

    Read-side repair for an install an older round-trip already poisoned: the
    placeholder is dropped on load, the Settings field reads as empty, and the
    owner re-enters the real key instead of an endpoint seeing ``Bearer ***``.
    Silent by design — ``load_settings`` runs on nearly every request, so a
    warning here would be per-request noise; the emptied field is the
    owner-facing signal.

    Mutates and returns ``settings`` so a caller can wrap an existing return.
    """
    for key in MASKED_SECRET_SETTING_KEYS:
        if looks_masked_secret(settings.get(key)):
            settings[key] = ""
    return settings
