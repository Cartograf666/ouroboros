// Pure chat-activity helpers shared by chat.js and dependency-free node tests:
// live-card presentation projections (moved verbatim from chat.js) plus the
// in-flight direct/ephemeral turn status reducer and snapshot hydration.
import {
    accountedUpperBound,
    accountedUpperBoundWithChildren,
    formatUsdWhole,
} from './utils.js';

// Row-surface disclosure guard (v6.71.0), pure for node tests: returns the
// lineKey to toggle for a click landing on `target`, or '' when the click must
// NOT toggle (nested interactive element, or an active text selection inside
// the line).
export function liveLineRowToggleKey(target, selection = null) {
    const line = target?.closest?.('.chat-live-line.expandable');
    if (!line) return '';
    if (target.closest('button, a, input, textarea, select, label, summary, [contenteditable="true"]')) return '';
    if (selection && !selection.isCollapsed && line.contains(selection.anchorNode)) return '';
    return (line.dataset && line.dataset.liveLineKey) || '';
}

/** Convert a raw source timestamp to sortable epoch milliseconds. */
export function rawTimestampEpoch(raw) {
    if (raw == null || raw === '') return NaN;
    const epoch = typeof raw === 'number' ? raw : Date.parse(String(raw));
    return Number.isFinite(epoch) ? epoch : NaN;
}

function optionalFiniteNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
}

/** Pure presentation projection used by the header and dependency-free tests. */
export function headerBudgetPresentation(data) {
    if (!data || data.accounting_loading === true) {
        return { state: 'loading', label: 'Loading…', fillPct: 0 };
    }
    if (data?.accounting?.available === false) {
        return { state: 'unavailable', label: 'Unavailable', fillPct: 0 };
    }
    // Older state shapes did not carry accounting.available.  Keep accepting
    // them when they contain a real numeric projection, but never coerce null
    // (ledger failure in the new shape) into a convincing $0.
    const spent = optionalFiniteNumber(data.spent_usd);
    if (spent === null) {
        return { state: 'unavailable', label: 'Unavailable', fillPct: 0 };
    }
    const rawLimit = optionalFiniteNumber(data.budget_limit);
    const limit = rawLimit !== null && rawLimit > 0 ? rawLimit : 0;
    const label = typeof data.budget_text === 'string' && data.budget_text.trim()
        ? data.budget_text
        : `${formatUsdWhole(spent)} / ${limit > 0 ? formatUsdWhole(limit) : '∞'}`;
    return {
        state: 'available',
        label,
        fillPct: limit > 0 ? Math.min(100, Math.max(0, (spent / limit) * 100)) : 0,
    };
}

/**
 * Render task money without conflating unknown/non-final values with a final
 * zero.  The returned strings are card metadata, not another cost authority.
 */
export function taskCostMeta(payload = {}) {
    const has = (key) => Object.prototype.hasOwnProperty.call(payload, key);
    // Task-scope accounting evidence only (v6.82 P1): a bare `cost_usd` is NOT
    // enough — llm_round_finished carries a per-round delta under that key, and
    // rendering it as task cost lied on the card. Subagent progress_meta and
    // task_done/task_cost_finalized frames carry cost_accounting_status /
    // cost_final alongside cost_usd, so honest task-scope frames still qualify.
    const hasAccountingEvidence = [
        'cost_accounting_status', 'cost_final',
        'cost_usd_with_children', 'cost_with_children_partial',
        'accounted_upper_bound_usd', 'accounted_upper_bound_usd_with_children',
        'reserved_usd', 'unresolved_upper_bound_usd', 'unknown_unmetered',
    ].some(has);
    if (!hasAccountingEvidence) return [];
    if (payload.cost_accounting_status === 'unavailable') return ['cost unavailable'];

    // C2/F12: ONE precedence resolver, shared with the Python seams and with
    // log_events — the deprecated alias wins a diverged pair, so the read side
    // and the write side never pick opposite winners for the same record.
    const own = accountedUpperBound(payload);
    const finalKnown = payload.cost_final === true;
    const pendingKnown = payload.cost_final === false
        || payload.cost_with_children_partial === true
        || payload.cost_accounting_status === 'available' && !has('cost_final');
    const meta = [];
    if (own === null) {
        meta.push('cost pending');
    } else if (finalKnown || pendingKnown || own !== 0) {
        meta.push(`cost=$${own.toFixed(2)}${pendingKnown && !finalKnown ? ' (pending)' : ''}`);
    }

    const subtree = accountedUpperBoundWithChildren(payload);
    if (subtree !== null && (
        own === null || subtree !== own || payload.cost_with_children_partial === true
    )) {
        const partial = payload.cost_with_children_partial === true || !finalKnown;
        meta.push(`subtree=$${subtree.toFixed(2)}${partial ? ' (pending)' : ''}`);
    }
    const reserved = optionalFiniteNumber(payload.reserved_usd);
    if (reserved !== null && reserved > 0) meta.push(`reserved=$${reserved.toFixed(2)}`);
    const unresolved = optionalFiniteNumber(payload.unresolved_upper_bound_usd);
    if (unresolved !== null && unresolved > 0) meta.push(`unresolved≤$${unresolved.toFixed(2)}`);
    const unknown = optionalFiniteNumber(payload.unknown_unmetered);
    if (unknown !== null && unknown > 0) meta.push(`unmetered=${Math.trunc(unknown)}`);
    return meta;
}

/**
 * Project one frame's task-scope cost evidence into the sticky structured form
 * `{meta, ts, final}` (v6.82 P1). Returns null when the frame carries NO
 * task-scope accounting evidence (e.g. an llm_round_finished per-round delta)
 * — such frames must never touch a card's cost.
 */
export function taskCostProjection(payload = {}, rawTs = '') {
    const meta = taskCostMeta(payload);
    if (!meta.length) return null;
    const unavailable = payload.cost_accounting_status === 'unavailable';
    return {
        meta,
        ts: rawTimestampEpoch(rawTs),
        // Only a SETTLED ledger value is final. "unavailable" is an honest
        // unknown, not a settled truth: marking it final let one transient
        // ledger-read failure outrank every later real reading.
        final: payload.cost_final === true,
        unavailable,
    };
}

/**
 * Sticky per-card cost precedence (v6.82 P1). Rank unavailable < pending < final:
 * an honest reading always outranks an unknown (one transient ledger-read failure
 * must not pin the card for the whole run) and a settled value outranks both.
 * Among equal rank the newer raw source timestamp wins, so an older history replay
 * can never overwrite newer evidence; frames without evidence (null `next`) keep
 * the previous projection, so an unavailable snapshot is still sticky.
 */
export function mergeStickyCostMeta(previous, next) {
    if (!next || !Array.isArray(next.meta) || !next.meta.length) return previous || null;
    if (!previous || !Array.isArray(previous.meta) || !previous.meta.length) return next;
    // Rank: unavailable < pending < final. An `unavailable` snapshot is sticky (a
    // costless frame must not erase it) but must NOT outrank a later HONEST reading:
    // one transient ledger-read failure would otherwise pin the card to "cost
    // unavailable" for the rest of the run.
    const rank = (p) => (p.final ? 2 : (p.unavailable ? 0 : 1));
    const prevRank = rank(previous);
    const nextRank = rank(next);
    if (prevRank !== nextRank) return nextRank > prevRank ? next : previous;
    const prevTs = Number(previous.ts);
    const nextTs = Number(next.ts);
    if (Number.isFinite(prevTs) && Number.isFinite(nextTs) && nextTs < prevTs) return previous;
    // A frame whose source timestamp is unreadable must not defeat a
    // timestamped previous value of equal finality.
    if (Number.isFinite(prevTs) && !Number.isFinite(nextTs)) return previous;
    return next;
}

/**
 * Reset the sticky presentation state (collapsed activity + cost projection)
 * introduced in v6.82 P1. Used by resetLiveCardRecord; pure over the record
 * shape so dependency-free node tests can exercise the recycle path.
 */
export function clearStickyCardState(record) {
    if (!record) return record;
    record.collapsedActivity = '';
    record.costMeta = null;
    // The activity clock is cycle state too: a
    // recycled slot ('bg-consciousness', 'active') would otherwise open showing
    // the previous cycle's "Latest" time.
    record.latestActivityTs = '';
    if (record.activityEl) {
        record.activityEl.textContent = '';
        record.activityEl.removeAttribute('title');
    }
    return record;
}

/**
 * Decide the collapsed activity line text (v6.82 P1), shared by root and
 * subagent cards. Root cards show the latest activity headline ONLY when a
 * coined name occupies the title — an unnamed card's title already shows the
 * activity, so the line is suppressed to avoid duplication. Subagent titles
 * keep the role·model·id identity, so their routed progress body always feeds
 * the line. A frame without new activity keeps `previous`, so finishing a card
 * never blanks its last activity. Geometry is owned by the two-line CSS clamp;
 * this character ceiling is only a defensive DOM/accessibility bound.
 */
export const COLLAPSED_ACTIVITY_MAX = 240;

export function boundActivityPreview(value = '') {
    const candidate = String(value || '').replace(/\s+/g, ' ').trim();
    if (candidate.length <= COLLAPSED_ACTIVITY_MAX) return candidate;
    return candidate.slice(0, COLLAPSED_ACTIVITY_MAX - 1).trimEnd() + '…';
}

export function projectCollapsedActivity({
    isSubagent = false, suggestedName = '', headline = '', body = '', previous = '',
} = {}) {
    const current = boundActivityPreview(isSubagent ? body : headline);
    const candidate = current || boundActivityPreview(previous);
    if (!isSubagent && !String(suggestedName || '').trim()) return '';
    return candidate;
}

// v6.82 (P5): terminal card phases. 'cancelled' is a first-class terminal phase
// so a force-cancelled root resolves its card instead of re-inflating.
export function isTerminalTaskPhase(phase = '', terminal = false) {
    return Boolean(terminal) || ['done', 'lifecycle_error', 'cancelled'].includes(phase);
}

// ---------------------------------------------------------------------------
// In-flight direct/ephemeral turn status (owner decisions 1A-5A).
// ---------------------------------------------------------------------------

// Snapshot-authoritative activity kinds: only turns the server's direct
// registry tracks may be deleted by /api/state hydration. Typing frames from
// queued managed tasks carry no kind — the snapshot has no authority over them
// (they are concluded by their own final/summary frames, as before).
const SNAPSHOT_AUTHORITATIVE_KINDS = new Set(['direct_chat', 'ephemeral_decision']);

/**
 * Single status reducer for the chat header (owner decisions 2A/5A). Priority:
 * disconnected > background live card (Working...) > server-confirmed in-flight
 * turns (Thinking...) > local pending submissions (Sending...) > terminal
 * attention > idle. Pure over its inputs for dependency-free node tests.
 */
export function computeDerivedChatStatus({
    isConnected = true,
    hasActiveLiveCard = false,
    activeDirectCount = 0,
    pendingSubmissionsCount = 0,
    lastTerminalAttention = false,
} = {}) {
    if (!isConnected) {
        return { kind: 'offline', text: 'Reconnecting...', showDots: false };
    }
    if (hasActiveLiveCard) {
        return { kind: 'thinking', text: 'Working...', showDots: false };
    }
    if (activeDirectCount > 0) {
        return { kind: 'thinking', text: 'Thinking...', showDots: true };
    }
    if (pendingSubmissionsCount > 0) {
        return { kind: 'thinking', text: 'Sending...', showDots: true };
    }
    if (lastTerminalAttention) {
        return { kind: 'error', text: 'Attention', showDots: false };
    }
    return { kind: 'online', text: 'Online', showDots: false };
}

/**
 * Reconcile the client's active-turn map against one /api/state snapshot
 * (owner decision 1A). The snapshot is authoritative ONLY over registry-tracked
 * direct/ephemeral turns that existed before it was requested; it must never
 * delete (a) an activity registered by a WS typing frame AFTER the request
 * started (the barrier), or (b) a queued managed task's typing entry, which the
 * direct registry does not track (kind '').
 *
 * `concludedIds` (Set/Map with .has) is the client-side conclusion ledger: a
 * turn already concluded by its keyed final must never be re-inserted by a
 * snapshot that was captured while it still ran (activity ids are unique task
 * ids and never restart, so conclusion is final). Without this, a one-shot
 * hydration (project panels) could resurrect a finished turn indefinitely.
 */
export function computeHydratedDirectActivities(existingMap, turnsList, chatId, snapshotBarrierMs = Infinity, concludedIds = null) {
    const nextMap = new Map(existingMap || []);
    if (!Array.isArray(turnsList)) return nextMap;
    const currentChatTurns = turnsList.filter((t) => Number(t?.chat_id ?? 1) === chatId);
    const activeIdsInSnapshot = new Set();
    for (const turn of currentChatTurns) {
        const aid = String(turn?.activity_id || '').trim();
        if (!aid) continue;
        if (concludedIds && concludedIds.has(aid)) continue;
        activeIdsInSnapshot.add(aid);
        const existing = nextMap.get(aid) || {};
        nextMap.set(aid, {
            activityId: aid,
            kind: turn.kind || 'direct_chat',
            phase: turn.phase || 'thinking',
            clientMessageId: turn.client_message_id || existing.clientMessageId || '',
            // Strictly CLIENT-clock "first observed" time: the snapshot's
            // server-clock started_at must never enter the barrier comparison
            // below (clock skew would let finished activities linger).
            startedAt: existing.startedAt || Date.now(),
        });
    }
    for (const [aid, entry] of nextMap.entries()) {
        if (activeIdsInSnapshot.has(aid)) continue;
        // Deletion authority is scoped to registry-tracked kinds: a queued
        // managed task's typing entry is invisible to the direct registry and
        // is concluded by its own final/summary frame instead.
        if (!SNAPSHOT_AUTHORITATIVE_KINDS.has(String(entry?.kind || ''))) continue;
        const startedAt = Number(entry?.startedAt) || 0;
        if (startedAt >= snapshotBarrierMs) continue;
        nextMap.delete(aid);
    }
    return nextMap;
}
