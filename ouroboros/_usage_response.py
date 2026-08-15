"""Pure provider-response usage normalization for physical accounting."""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        return value
    for method_name in ("model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return method()
            except Exception:
                pass
    return value


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def usage_from_response(response: Any) -> Tuple[Dict[str, Any], Optional[float], bool]:
    """Extract common usage/cost facts without retaining response text."""
    payload: Any = _plain(response)
    if not isinstance(payload, dict) and callable(getattr(response, "json", None)):
        try:
            payload = response.json()
        except Exception:
            payload = None
    usage: Any = payload.get("usage") if isinstance(payload, dict) else getattr(response, "usage", None)
    usage = _plain(usage)
    if not isinstance(usage, dict):
        usage = {}
    cache_read = int(usage.get("cache_read_input_tokens") or usage.get("cached_tokens")
                     or usage.get("precached_prompt_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens")
                      or usage.get("cache_write_tokens") or 0)
    prompt_value = usage.get("prompt_tokens")
    if prompt_value is None and any(
        key in usage for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
    ):
        # Anthropic native input_tokens excludes cache reads and writes.
        prompt = int(usage.get("input_tokens") or 0) + cache_read + cache_write
    else:
        prompt = int(prompt_value or usage.get("input_tokens") or 0)
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if isinstance(details, dict):
        cache_read = int(details.get("cached_tokens") or cache_read)
        cache_write = int(
            cache_write
            or details.get("cache_write_tokens")
            or details.get("cache_creation_tokens")
            or details.get("cache_creation_input_tokens")
            or 0
        )
    normalized = {
        **usage,
        "prompt_tokens": prompt,
        "completion_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        "cached_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }
    creation = usage.get("cache_creation")
    if isinstance(creation, dict):
        split = {
            tier: int(creation.get(key) or 0)
            for tier, key in (("5m", "ephemeral_5m_input_tokens"),
                              ("1h", "ephemeral_1h_input_tokens"))
            if int(creation.get(key) or 0) > 0
        }
        if split:
            normalized["cache_write_tokens_by_ttl"] = split
    completion = int(normalized["completion_tokens"])
    if (isinstance(payload, dict) and isinstance(payload.get("error"), dict)
            and prompt == 0 and completion == 0):
        return normalized, 0.0, True
    candidates = (
        usage.get("cost"), usage.get("total_cost"),
        payload.get("total_cost_usd") if isinstance(payload, dict) else None,
        getattr(response, "total_cost_usd", None),
    )
    cost = next((number for value in candidates if (number := _number(value)) is not None), None)
    return normalized, cost, cost is not None
