"""Per-model concurrency cap (#4, cyber-racing post-mortem): prevent a self-inflicted DoS.

When a task's main loop, its in-process subagent threads, and owner status pings fire at the
SAME rate-limited model at once, the provider answers a storm of 429s and otherwise-good work
dies ``provider_unavailable`` with work already done. A process-local
``threading.BoundedSemaphore`` per resolved route (``model``, ``use_local``) serializes
concurrent provider calls to a small cap; excess threads WAIT (bounded by the task deadline)
instead of all firing and getting rate-limited.

PLACEMENT — ONE seam, ``LLMClient.chat``. The guard used to be opened by each caller, and
only 4 of the 12 runtime provider call sites did so: scope/triad/plan/skill review, deep
self-review, dialogue consolidation, semantic dedup, skill publish and the supervisor's own
call ran with no cap at all, so the documented 3 was uncapped for most traffic. Guarding the
one method every production call passes through closes that class instead of its instances —
a thirteenth call site inherits the cap without knowing it exists. ``model_call_slot`` is
therefore RE-ENTRANT per route (see ``_HELD``): the callers that still open their own slot
keep their deadline-bounded outer wait and the inner seam passes through, so one logical call
waits once and holds one permit.

DISCLOSED RESIDUAL — ``LLMClient.chat_async`` is NOT behind the seam. It is an ``async def``,
and this is a blocking ``threading`` primitive: acquiring inside it would stall whatever event
loop awaited it. Its only runtime caller is ``review_execution``'s fallback branch, reached
only when the client exposes no ``chat`` (test doubles), and it wraps the coroutine in
``asyncio.run`` on its own thread. Production traffic therefore goes through ``chat``. A
future caller that awaits ``chat_async`` on a shared loop would be uncapped; capping it needs
an async-aware primitive, not this one.

SCOPE — PER-PROCESS only (exactly like ``fallback_cooldown``): heavy worker tasks run in
SEPARATE processes, so this caps the calls WITHIN one process, NOT across the multi-worker
swarm. Concretely, at the default ``OUROBOROS_MAX_WORKERS=10`` the provider can still see up
to 10×cap = 30 concurrent calls on one route, plus the server process — the honest number,
stated because "per-process only" alone reads much smaller than it is. A cross-worker governor
(supervisor-mediated admission / a shared lease) is future work.

Design constraints (codex review):
- Wrap ONLY the actual provider call per attempt — NOT the retry/fallback chain, and NEVER a
  backoff sleep. Now structural: the seam sits inside ``llm.chat``, which dispatches exactly
  one attempt (retries live in ``loop_llm_call``).
- Sync primitive (the calls run on threads, not an asyncio loop), so a ``threading`` semaphore.
- Default-on, FAIL-SOFT: if disabled, mis-resolved, or the slot can't be acquired before the
  task deadline, proceed WITHOUT throttling — never block a task past its deadline, never raise.
- Semaphore wait time is NOT a provider failure (the caller's cooldown classifier never sees it).
- Keyed like ``fallback_cooldown`` on (model, use_local); the model id already carries the
  provider prefix (``z-ai/glm-5.2``, ``cloudru::…``), so this separates distinct routes.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from typing import Optional

_LOCK = threading.Lock()
_SEMAPHORES: dict = {}
# Routes this THREAD is already inside. The guard lives at the one provider seam
# (``LLMClient.chat``), so an outer caller that also opens a slot — the main loop,
# safety, project naming — would otherwise make the same logical call wait twice and
# consume two permits of a cap-3 route. Re-entry is a pass-through instead: one wait
# per logical call, and the OUTERMOST holder keeps its deadline-bounded wait rather
# than having the inner no-deadline ceiling replace it.
_HELD = threading.local()

def _max_slot_wait_sec() -> float:
    """Hard ceiling (seconds) a single call WAITS for a slot when the task has no deadline,
    so a wedged provider can never park a worker forever. SSOT: config SETTINGS_DEFAULTS."""
    from ouroboros.config import SETTINGS_DEFAULTS

    default = SETTINGS_DEFAULTS["OUROBOROS_MODEL_SLOT_MAX_WAIT_SEC"]
    try:
        return float(os.environ.get("OUROBOROS_MODEL_SLOT_MAX_WAIT_SEC", default))
    except (TypeError, ValueError):
        return float(default)


def _cap() -> int:
    """Max concurrent provider calls allowed per (model, use_local) route. <=0 disables.
    The default comes from the config SSOT (SETTINGS_DEFAULTS), not a hardcoded literal."""
    from ouroboros.config import SETTINGS_DEFAULTS

    default = SETTINGS_DEFAULTS.get("OUROBOROS_MODEL_MAX_CONCURRENCY", 3)
    try:
        return int(os.environ.get("OUROBOROS_MODEL_MAX_CONCURRENCY", default))
    except (TypeError, ValueError):
        try:
            return int(default)
        except (TypeError, ValueError):
            return 3


def enabled() -> bool:
    # Single SSOT knob: OUROBOROS_MODEL_MAX_CONCURRENCY (<=0 disables the guard). No
    # separate enable flag — one config surface only (P7 minimalism / DEVELOPMENT SSOT).
    return _cap() > 0


def _route_key(model: str, use_local: bool) -> tuple:
    # The cap is part of the key so changing OUROBOROS_MODEL_MAX_CONCURRENCY at runtime
    # (settings hot-reload) takes effect on the next call — a route's existing semaphore
    # is never silently stuck at the old cap. Shared with the re-entry set so a thread
    # cannot mark one key and wait on another.
    return (str(model or ""), bool(use_local), max(1, _cap()))


def _semaphore_for(model: str, use_local: bool) -> threading.BoundedSemaphore:
    key = _route_key(model, use_local)
    with _LOCK:
        sem = _SEMAPHORES.get(key)
        if sem is None:
            sem = threading.BoundedSemaphore(key[2])
            _SEMAPHORES[key] = sem
        return sem


@contextlib.contextmanager
def model_call_slot(model: str, use_local: bool = False, deadline_ts: Optional[float] = None):
    """Hold a per-route concurrency slot around ONE provider call.

    Fail-soft: if disabled, or the slot can't be acquired before the task deadline (or the
    no-deadline wait ceiling), proceed WITHOUT a slot (no throttle) rather than blocking
    the task past its deadline. Never raises out of the context setup.
    """
    if not enabled():
        yield
        return
    try:
        key = _route_key(model, use_local)
        sem = _semaphore_for(model, use_local)
    except Exception:
        yield
        return
    held = getattr(_HELD, "keys", None)
    if held is None:
        held = set()
        _HELD.keys = held
    if key in held:
        # Already inside this route on this thread — pass through (see _HELD).
        yield
        return
    # Bound the wait by the remaining deadline (epoch seconds) and the hard ceiling.
    timeout = _max_slot_wait_sec()
    if deadline_ts:
        timeout = max(0.0, min(timeout, float(deadline_ts) - time.time()))
    acquired = False
    try:
        acquired = sem.acquire(timeout=timeout) if timeout > 0 else False
    except Exception:
        acquired = False
    # Marked whether or not the permit was won: a fail-soft miss still means this
    # logical call already paid its one wait, so a nested seam must not wait again.
    held.add(key)
    try:
        yield
    finally:
        held.discard(key)
        if acquired:
            try:
                sem.release()
            except (ValueError, RuntimeError):
                pass


def reset_for_tests() -> None:
    with _LOCK:
        _SEMAPHORES.clear()
    # Only this thread's marks: a leaked mark would silently disable the guard for
    # every later call on the same thread, which is exactly the failure this closes.
    _HELD.keys = set()
