"""
Ouroboros — LLM pricing and cost estimation.

Provides best-effort provider-catalog pricing, nullable cost estimation, and
usage event emission. Missing pricing is data, not a model-admission gate.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any, Dict, Optional, Tuple

import logging

from ouroboros.provider_models import normalize_model_identity, provider_for_model
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)


class PricingSchedule(tuple):
    """A tuple-compatible base price with provider prompt-length tiers."""

    def __new__(
        cls,
        base: Tuple[Optional[float], ...],
        tiers: Tuple[Tuple[int, Tuple[Optional[float], ...]], ...] = (),
    ) -> "PricingSchedule":
        value = super().__new__(cls, base)
        value.tiers = tuple(sorted(tiers, key=lambda item: int(item[0])))
        return value

# Prices are intentionally never seeded from hand-maintained model rows. Each
# provider cache contains only data returned by that exact route's catalog.
_cached_pricing: Dict[str, Dict[str, Tuple[Optional[float], ...]]] = {}
_pricing_fetched_at: Dict[str, float] = {}
_pricing_retry_after: Dict[str, float] = {}
_pricing_fetch_in_progress: set[str] = set()
_pricing_lock = threading.Lock()


def _pricing_ttl_sec() -> float:
    """Live-pricing refetch interval (provider prices/ FX rates drift). Default 6h."""
    try:
        return max(60.0, float(os.environ.get("OUROBOROS_PRICING_TTL_SEC", "") or 21600.0))
    except (TypeError, ValueError):
        return 21600.0


# --- Provider catalogue fetchers -------------------------------------------------
# Moved here from ouroboros/llm.py (v6.102.x): both are module-level, touch no client
# state, and their only production consumer is `_fetch_live_rows` below. Living in
# llm.py forced TWO lazy-import cycle workarounds — llm importing PricingSchedule from
# here, and here importing the fetchers back from llm. The move deletes both.

def fetch_openrouter_pricing(*, timeout_sec: float = 5.0) -> Dict[str, Tuple[Optional[float], ...]]:
    """Fetch OpenRouter pricing as model_id -> per-1M prices.

    Tuples are ``(input, cached_read, cache_write, output)``. Missing cache
    prices remain ``None`` instead of inheriting a synthetic coefficient.
    """
    try:
        import requests
    except ImportError:
        log.warning("requests not installed, cannot fetch pricing")
        return {}

    try:
        url = "https://openrouter.ai/api/v1/models"
        resp = requests.get(url, timeout=max(0.1, min(5.0, float(timeout_sec))))
        resp.raise_for_status()

        data = resp.json()
        models = data.get("data", [])

        pricing_dict = {}
        for model in models:
            model_id = str(model.get("id") or "").strip()

            pricing = model.get("pricing", {})
            if not pricing or pricing.get("prompt") is None or pricing.get("completion") is None:
                continue

            raw_prompt = float(pricing.get("prompt", 0))
            raw_completion = float(pricing.get("completion", 0))
            raw_cached_str = pricing.get("input_cache_read")
            raw_cached = float(raw_cached_str) if raw_cached_str is not None else None
            raw_cache_write_str = pricing.get("input_cache_write")
            raw_cache_write = float(raw_cache_write_str) if raw_cache_write_str is not None else None
            if raw_prompt < 0 or raw_completion < 0:
                continue
            if raw_cached is not None and raw_cached < 0:
                raw_cached = None
            if raw_cache_write is not None and raw_cache_write < 0:
                raw_cache_write = None

            prompt_price = round(raw_prompt * 1_000_000, 4)
            completion_price = round(raw_completion * 1_000_000, 4)
            cached_price = round(raw_cached * 1_000_000, 4) if raw_cached is not None else None
            cache_write_price = (
                round(raw_cache_write * 1_000_000, 4)
                if raw_cache_write is not None else None
            )

            if prompt_price > 1000 or completion_price > 1000:
                log.warning(f"Skipping {model_id}: prices seem wrong (prompt={prompt_price}, completion={completion_price})")
                continue

            row = (prompt_price, cached_price, cache_write_price, completion_price)

            tiers = []
            raw_overrides = pricing.get("overrides") or []
            if isinstance(raw_overrides, list):
                for override in raw_overrides:
                    if not isinstance(override, dict):
                        continue
                    try:
                        min_prompt_tokens = int(override.get("min_prompt_tokens") or 0)
                        if min_prompt_tokens <= 0:
                            continue
                        tier_raw_prompt = float(override.get("prompt", raw_prompt))
                        tier_raw_completion = float(override.get("completion", raw_completion))
                        tier_prompt = round(tier_raw_prompt * 1_000_000, 4)
                        tier_completion = round(tier_raw_completion * 1_000_000, 4)
                        override_cached = override.get("input_cache_read")
                        tier_cached = (
                            round(float(override_cached) * 1_000_000, 4)
                            if override_cached is not None else None
                        )
                        override_write = override.get("input_cache_write")
                        if override_write is not None:
                            tier_write = round(float(override_write) * 1_000_000, 4)
                        else:
                            tier_write = None
                        if tier_prompt > 1000 or tier_completion > 1000:
                            continue
                        tier_row = (tier_prompt, tier_cached, tier_write, tier_completion)
                        tiers.append((min_prompt_tokens, tier_row))
                    except (TypeError, ValueError):
                        log.warning(
                            "Skipping malformed pricing override for %s", model_id,
                        )
            if tiers:
                row = PricingSchedule(row, tuple(tiers))
            pricing_dict[model_id] = row
            normalized_model_id = normalize_model_identity(model_id)
            if normalized_model_id != model_id:
                pricing_dict[normalized_model_id] = row

        log.info(f"Fetched pricing for {len(pricing_dict)} models from OpenRouter")
        return pricing_dict

    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning(f"Failed to fetch OpenRouter pricing: {e}")
        return {}


def fetch_cloudru_pricing(*, timeout_sec: float = 5.0) -> Dict[str, Tuple[Optional[float], ...]]:
    """Fetch cloud.ru Foundation Models pricing as ``cloudru/<id>`` -> per-1M USD.

    cloud.ru's ``GET /v1/models`` returns per-model ``metadata`` with token costs
    (``prompt_tokens_cost``, ``generated_tokens_cost``, ``cache_read_tokens_cost``,
    ``cache_write_tokens_cost``) in RUB per 1M tokens — i.e. the real resale price
    the owner pays. We convert to USD via ``OUROBOROS_RUB_USD_RATE`` so the catalog
    is the SSOT for ALL cloud.ru models (no hardcoded per-model table). Models with
    ``is_billable=false`` is an exact free row; missing billability or an absent
    explicit ``OUROBOROS_RUB_USD_RATE`` stays unknown. Returns {} when the catalog
    cannot be queried. Tuples are ``(input, cached_read, cache_write, output)``."""
    api_key = (os.environ.get("CLOUDRU_FOUNDATION_MODELS_API_KEY", "") or "").strip()
    if not api_key:
        return {}
    try:
        import requests
    except ImportError:
        return {}

    base_url = (
        os.environ.get("CLOUDRU_FOUNDATION_MODELS_BASE_URL", "") or ""
    ).strip() or "https://foundation-models.api.cloud.ru/v1"
    try:
        rate = float(os.environ.get("OUROBOROS_RUB_USD_RATE", ""))
    except (TypeError, ValueError):
        return {}
    if rate <= 0:
        return {}

    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=max(0.1, min(5.0, float(timeout_sec))),
        )
        resp.raise_for_status()
        models = resp.json().get("data", []) or []

        def _rub_per_1m_to_usd(value: Any) -> Optional[float]:
            try:
                num = float(value)
            except (TypeError, ValueError):
                return None
            if num < 0:  # cloud.ru uses -1 for "n/a" (e.g. embedding output)
                return None
            return round(num / rate, 6)

        pricing_dict: Dict[str, Tuple[Optional[float], ...]] = {}
        for model in models:
            model_id = str(model.get("id") or "").strip()
            meta = model.get("metadata") if isinstance(model.get("metadata"), dict) else {}
            if not model_id or not meta or meta.get("is_billable") is None:
                continue
            if meta.get("is_billable") is False:
                pricing_dict[normalize_model_identity(f"cloudru::{model_id}")] = (0.0, 0.0, 0.0, 0.0)
                continue
            prompt_price = _rub_per_1m_to_usd(meta.get("prompt_tokens_cost"))
            output_price = _rub_per_1m_to_usd(meta.get("generated_tokens_cost"))
            if prompt_price is None or output_price is None:
                continue
            cached_price = _rub_per_1m_to_usd(meta.get("cache_read_tokens_cost"))
            cache_write_price = _rub_per_1m_to_usd(meta.get("cache_write_tokens_cost"))
            row = (
                prompt_price,
                cached_price,
                cache_write_price,
                output_price,
            )
            pricing_dict[normalize_model_identity(f"cloudru::{model_id}")] = row

        log.info(f"Fetched pricing for {len(pricing_dict)} models from cloud.ru")
        return pricing_dict
    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning(f"Failed to fetch cloud.ru pricing: {e}")
        return {}


def _fetch_live_rows(provider: str) -> Dict[str, Tuple[Optional[float], ...]]:
    if provider == "openrouter":
        return fetch_openrouter_pricing(timeout_sec=5.0)
    if provider == "cloudru":
        return fetch_cloudru_pricing(timeout_sec=5.0)
    return {}


def get_pricing(
    *, provider: str = "openrouter", allow_live_fetch: bool = True,
) -> Dict[str, Tuple[Optional[float], ...]]:
    """Return pricing from the exact provider route's live catalog.

    Direct/OpenAI-compatible/GigaChat routes have no automatic catalog here and
    therefore return an empty mapping. A cold/expired fetch is bounded to five
    seconds; failures expose unknown pricing rather than a fabricated fallback.
    """
    provider = str(provider or "").strip().lower()
    if provider not in {"openrouter", "cloudru"}:
        return {}
    with _pricing_lock:
        cached = dict(_cached_pricing.get(provider, {}))
        fresh = bool(_pricing_fetched_at.get(provider)) and (
            time.time() - _pricing_fetched_at[provider]
        ) < _pricing_ttl_sec()
        retry_later = time.time() < _pricing_retry_after.get(provider, 0.0)
        if not allow_live_fetch or fresh or retry_later or provider in _pricing_fetch_in_progress:
            return cached
        _pricing_fetch_in_progress.add(provider)
    try:
        rows = _fetch_live_rows(provider)
    except Exception as exc:
        log.warning("Failed to fetch %s pricing catalog: %s", provider, exc)
        rows = {}
    with _pricing_lock:
        _pricing_fetch_in_progress.discard(provider)
        _cached_pricing[provider] = dict(rows)
        if rows:
            _pricing_fetched_at[provider] = time.time()
            _pricing_retry_after.pop(provider, None)
        else:
            _pricing_fetched_at.pop(provider, None)
            # Avoid adding the same five-second outage delay to every dispatch.
            # This is process-local and deliberately short, not a stale tariff cache.
            _pricing_retry_after[provider] = time.time() + 30.0
        return dict(rows)


def estimate_cost_optional(model: str, prompt_tokens: int, completion_tokens: int, *,
                           cache_usage: Optional[Dict[str, Any]] = None,
                           allow_live_fetch: bool = True,
                           provider: Optional[str] = None) -> Optional[float]:
    """Estimate cost from exact provider/model data, preserving unknown as None.

    ``cache_usage`` and everything after it are KEYWORD-ONLY: the 4th slot used
    to be a positional ``cached_tokens: int``, and a stale positional caller
    would otherwise be silently coerced through the isinstance guard to ``{}``,
    dropping cache accounting invisibly. Keyword-only makes such a caller a
    loud ``TypeError`` instead.

    ``cache_usage`` folds the prompt-cache facts into one mapping (the <8-parameter
    contract; keys are the usage-row field names, all optional):

    - ``cached_tokens``: prompt tokens served from cache (read tier).
    - ``cache_write_tokens``: prompt tokens written to cache this call.
    - ``prompt_cache_ttl``: the requested write tier (``"5m"``/``"1h"``).
    - ``cache_write_tokens_by_ttl``: Anthropic's per-tier write split
      (``usage.cache_creation`` → ``{"5m": n, "1h": n}``), harvested when the
      provider reports it: on a ``1h`` request whose payload also produced 5m
      writes (e.g. a server-tool block cached at the default tier beside the 1h
      prefix) only the genuine 1h share bills the extended-tier ratio. Absent the
      split, every write bills the reported tier — the pre-split behavior, never
      a loosened ratio.
    """
    cache_row = cache_usage if isinstance(cache_usage, dict) else {}
    cached_tokens = int(cache_row.get("cached_tokens") or 0)
    cache_write_tokens = int(cache_row.get("cache_write_tokens") or 0)
    prompt_cache_ttl = cache_row.get("prompt_cache_ttl")
    cache_write_tokens_by_ttl = cache_row.get("cache_write_tokens_by_ttl")
    raw_model = str(model or "").strip()
    normalized = normalize_model_identity(raw_model)
    route = str(provider or provider_for_model(raw_model) or "openrouter").strip().lower()
    if route == "local":
        return 0.0
    model_pricing = get_pricing(provider=route, allow_live_fetch=allow_live_fetch)
    pricing = model_pricing.get(normalized)
    if not pricing:
        return None
    tiers = getattr(pricing, "tiers", ())
    for min_prompt_tokens, tier_pricing in tiers:
        if max(0, int(prompt_tokens or 0)) >= int(min_prompt_tokens):
            pricing = tier_pricing
    if len(pricing) != 4 or pricing[0] is None or pricing[3] is None:
        return None
    input_price = float(pricing[0])
    cached_price = float(pricing[1]) if pricing[1] is not None else None
    write_price = float(pricing[2]) if pricing[2] is not None else None
    output_price = float(pricing[3])
    extended_write_tokens = 0
    if write_price is not None and str(prompt_cache_ttl or "") == "1h":
        # Provider catalogs carry the DEFAULT (5m) cache-write price. Anthropic's
        # extended 1h tier bills cache writes at 2x base input versus 1.25x for
        # 5m — a documented tier RATIO, not a hand-maintained tariff — so scale
        # the catalog write price accordingly (estimates/reservations only;
        # settlement always prefers provider-reported cost).
        extended_write_tokens = max(0, int(cache_write_tokens or 0))
        if isinstance(cache_write_tokens_by_ttl, dict):
            try:
                reported_1h = int(cache_write_tokens_by_ttl.get("1h") or 0)
            except (TypeError, ValueError):
                reported_1h = extended_write_tokens
            # Clamp into the reported total: a malformed split never bills MORE
            # extended-tier tokens than were written at all.
            extended_write_tokens = max(0, min(reported_1h, extended_write_tokens))
    if cached_tokens and cached_price is None:
        return None
    if cache_write_tokens and write_price is None:
        return None
    regular_input = max(0, prompt_tokens - cached_tokens - cache_write_tokens)
    default_write_tokens = max(0, int(cache_write_tokens or 0) - extended_write_tokens)
    cost = (
        regular_input * input_price / 1_000_000
        + cached_tokens * float(cached_price or 0.0) / 1_000_000
        + default_write_tokens * float(write_price or 0.0) / 1_000_000
        + extended_write_tokens * float(write_price or 0.0) * (2.0 / 1.25) / 1_000_000
        + completion_tokens * output_price / 1_000_000
    )
    return round(cost, 6)


def infer_api_key_type(model: str, provider: Optional[str] = None) -> str:
    """Infer which API key is used based on model name."""
    provider_name = str(provider or "").strip().lower()
    if provider_name in {"local", "openrouter", "openai", "anthropic", "openai-compatible", "cloudru", "gigachat", "minimax"}:
        return provider_name
    raw_model = str(model or "").strip()
    direct_provider = provider_for_model(raw_model)
    # ``openrouter::``-prefixed and un-prefixed ids both bill OpenRouter and
    # fall through to the normalized-identity heuristics below.
    if direct_provider not in ("openrouter",):
        return direct_provider
    normalized = normalize_model_identity(raw_model)
    if normalized.startswith("openai/"):
        return "openrouter"
    if normalized.startswith("openai-compatible/"):
        return "openai-compatible"
    if normalized.startswith("cloudru/"):
        return "cloudru"
    if normalized.startswith("gigachat/"):
        return "gigachat"
    # NB: un-prefixed "minimax/..." deliberately falls through to OpenRouter below —
    # unlike cloudru/gigachat, minimax IS a real OpenRouter vendor namespace, and
    # slash-form ids stay router-style by design (direct routing uses minimax::,
    # already resolved by provider_for_model above). Classifying minimax/ as the
    # direct key would make safety.py demand MINIMAX_API_KEY on OpenRouter installs.
    if normalized.startswith(("anthropic/", "google/", "openai/", "x-ai/", "qwen/", "minimax/")):
        return "openrouter"
    if "claude" in normalized.lower():
        return "anthropic"
    return "openrouter"


def infer_provider_from_model(model: str) -> str:
    """Derive the billing provider string from a model identifier.

    Rules (same prefix logic as infer_api_key_type, returns canonical provider name):
      anthropic::*          → "anthropic"
      openai::*             → "openai"
      openai-compatible::*  → "openai-compatible"
      cloudru::*            → "cloudru"
      gigachat::*           → "gigachat"
      anything else         → "openrouter"  (un-prefixed OpenRouter routing)

    Used by review-pipeline emitters to ensure /api/cost-breakdown attribution
    is correct regardless of which provider the model actually routes through.
    """
    provider = provider_for_model(model)
    # Historical billing attribution: local-suffixed ids billed as openrouter.
    return "openrouter" if provider == "local" else provider


def infer_model_category(model: str) -> str:
    """Infer model category by comparing against configured model env vars."""
    model = str(model or "").strip()
    if model.endswith(" (local)"):
        model = model[:-8]
    normalized = normalize_model_identity(model)
    for cat, val in (
        ("main", os.environ.get("OUROBOROS_MODEL", "")),
        ("heavy", os.environ.get("OUROBOROS_MODEL_HEAVY", "")),
        ("light", os.environ.get("OUROBOROS_MODEL_LIGHT", "")),
    ):
        if val and normalized == normalize_model_identity(val):
            return cat
    # Fallbacks is a comma chain -> a model is "fallback" if it is ANY link of the chain
    # (parsed via the shared SSOT, which also honors the legacy singular env), not only
    # when it equals the whole raw comma-string.
    from ouroboros.config import parse_fallback_chain
    for fb in parse_fallback_chain():
        if fb and normalized == normalize_model_identity(fb):
            return "fallback"
    return "other"


def emit_llm_usage_event(
    event_queue: Optional[queue.Queue],
    task_id: str,
    model: str,
    usage: Dict[str, Any],
    cost: Optional[float],
    category: str = "task",
    provider: Optional[str] = None,
    source: str = "loop",
    cost_estimated: Optional[bool] = None,
) -> None:
    """
    Emit llm_usage event to the event queue.

    Args:
        event_queue: Queue to emit events to (may be None)
        task_id: Task ID for the event
        model: Model name used for the LLM call
        usage: Usage dict from LLM response
        cost: Calculated cost for this call
        category: Budget category (task, evolution, consciousness, review, summarize, other)
    """
    if not event_queue:
        return
    try:
        # Task-tree attribution from the bound usage scope (worker-side truth;
        # the supervisor additionally backfills lane/role from RUNNING).
        root_task_id = parent_task_id = ""
        try:
            from ouroboros.usage_accounting import current_usage_scope

            scope = current_usage_scope()
            if scope is not None:
                root_task_id = str(scope.root_task_id or "")
                parent_task_id = str(scope.parent_task_id or "")
        except Exception:
            pass
        resolved_provider = provider or ("local" if str(model or "").endswith(" (local)") else "openrouter")
        event_queue.put_nowait({
            "type": "llm_usage",
            "ts": utc_now_iso(),
            "task_id": task_id,
            "root_task_id": root_task_id,
            "parent_task_id": parent_task_id,
            "model": model,
            "api_key_type": infer_api_key_type(model, resolved_provider),
            "model_category": infer_model_category(model),
            "provider": resolved_provider,
            "source": source,
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "cached_tokens": int(usage.get("cached_tokens") or 0),
            "cache_write_tokens": int(usage.get("cache_write_tokens") or 0),
            "prompt_cache_ttl": str(usage.get("prompt_cache_ttl") or ""),
            "cost": cost,
            "cost_estimated": (
                bool(cost_estimated)
                if cost_estimated is not None
                else bool(usage.get("cost_estimated"))
            ),
            "usage": usage,
            "category": category,
            # Compatibility telemetry only. Monetary authority is the durable
            # physical-attempt ledger; ids allow joining without double charge.
            "accounting_authority": "physical_attempt_ledger",
            "ledger_attempt_ids": list(usage.get("ledger_attempt_ids") or []),
        })
    except Exception:
        log.debug("Failed to put llm_usage event to queue", exc_info=True)
