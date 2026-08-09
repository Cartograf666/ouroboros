// ONE client-side store over `GET /api/claudexor/status` (phase 2 seam).
//
// Three surfaces used to read this endpoint independently — the Harness
// Accounts panel polled it every 5s, Reviewer Slots fetched it once per
// settings load, Subagents fetched it once more — so the app held three
// copies of the same truth, three failure handlings, and three staleness
// clocks. Worse, each of them mapped "we could not ask" onto the SAME empty
// answer the daemon gives when it is simply not running, so a stopped daemon
// decorated every saved reviewer row with "(not in discovery)" and explained
// nothing (owner report, 2026-08-08).
//
// PROVENANCE IS PER FACET, never one global verdict. `/api/claudexor/status`
// fans out to independent daemon reads — the harness CATALOG, the credential
// ACCOUNTS, the QUOTA windows — and one of them can fail while its siblings
// land. Collapsing them into a single "the service is down" would mislabel
// rows just as surely as the bug this seam fixes, so every consumer asks about
// the facet it actually renders:
//   * `ok`        — this facet was read; its collection is AUTHORITATIVE
//                   (empty means empty).
//   * `not_read`  — never asked. The owned daemon starts lazily, so an idle
//                   machine answers with empty lists nobody may read as "no
//                   account connected" (BIBLE P1: a gap is not a zero).
//   * `failed`    — asked, and the answer did not arrive.
//   * `transport` — the REQUEST itself did not complete (fetch threw, non-2xx).
//                   A client-side fact about this view, never part of the wire
//                   payload; it outranks whatever the last payload said.
//   * `unread`    — this client has not read yet (store-side only; the pure
//                   `facetReadState` never answers it).
// Only `ok` licenses a row-level "(not in discovery)" claim.
//
// Wire contract: the backend stamps `payload.reads = {catalog, accounts,
// quota}`. A payload WITHOUT that block is read legacy-style — a running
// daemon meant every facet was read, anything else meant none were — so this
// store works unchanged on both, and gains precision the day the field lands.
//
// Polling is a held resource, not a background fact: the store ticks only
// while it has subscribers, the page is visible, and either some subscriber
// says its surface is on screen or a caller holds it open (a live login job).
// Everything acquired — the timer and the visibilitychange listener — is
// released by `dispose()` (DEVELOPMENT.md «UI resources carry a disposer»).
//
// Pure helpers up top are node-tested without a DOM.

import { apiFetch } from './api_client.js';

export const STATUS_ENDPOINT = '/api/claudexor/status';
export const DEFAULT_POLL_MS = 5000;

export const READ_OK = 'ok';
export const READ_NOT_READ = 'not_read';
export const READ_FAILED = 'failed';
export const READ_TRANSPORT = 'transport';
export const READ_UNREAD = 'unread';

// The independent facets of one status payload. The login-capability manifest
// read is deliberately NOT one: its failure is absorbed (fail-open) rather
// than reported, so it has no verdict to carry.
export const FACET_CATALOG = 'catalog';
export const FACET_ACCOUNTS = 'accounts';
export const FACET_QUOTA = 'quota';
export const STATUS_FACETS = [FACET_CATALOG, FACET_ACCOUNTS, FACET_QUOTA];

// ---------------------------------------------------------------------------
// Pure helpers.
// ---------------------------------------------------------------------------

export function facetReadState(payload, facet, { transportError = '' } = {}) {
    // The ONE place that decides what an empty collection MEANS, for ONE facet.
    // A request that never arrived outranks whatever the last payload said: the
    // held snapshot describes a past read, never the current one.
    if (transportError) return READ_TRANSPORT;
    const reads = payload && typeof payload === 'object' ? payload.reads : null;
    const stamped = reads ? String(reads[facet] || '') : '';
    if (stamped === READ_OK || stamped === READ_NOT_READ || stamped === READ_FAILED) return stamped;
    // LEGACY payload (a backend without the `reads` block, or a fixture that
    // predates it): the daemon state carries exactly this fact — running meant
    // every facet was read, anything else meant none were.
    return String(payload?.daemon?.state || '') === 'running' ? READ_OK : READ_NOT_READ;
}

export function readsFor(payload, { transportError = '' } = {}) {
    const out = {};
    for (const facet of STATUS_FACETS) out[facet] = facetReadState(payload, facet, { transportError });
    return out;
}

export function facetKnown(readState) {
    // The single licence for a row-level "(not in discovery)" claim.
    return readState === READ_OK;
}

export function shouldPollStatus({
    hasSubscribers = false, hidden = false, surfaceVisible = false, held = false,
} = {}) {
    // The whole polling gate, pure. Each status tick fans out to four
    // CLI-probing daemon round-trips, so a background timer nobody is looking
    // at is pure waste: it needs a live subscriber, a visible page, and either
    // a surface actually on screen or a caller holding it open (a live login
    // job). An explicit `refresh()` is a request, not a poll, and is never
    // gated by this — that is what makes the first paint immediate.
    if (!hasSubscribers) return false;
    if (hidden) return false;
    return Boolean(held || surfaceVisible);
}

// What each facet is CALLED in a sentence to the owner.
// D-10: "agent", never "coding agent" — these agents also build presentations
// and run ordinary tasks, and this sentence is now rendered by the first-run
// wizard as well as Settings.
const FACET_SUBJECT = {
    [FACET_CATALOG]: 'agents',
    [FACET_ACCOUNTS]: 'agent accounts',
    [FACET_QUOTA]: 'subscription limits',
};

export function statusUnavailableNote(readState, { error = '', facet = FACET_ACCOUNTS } = {}) {
    // ONE sentence per read state, shared by every consumer, so the app cannot
    // explain the same gap three different ways. `null` = nothing to say.
    //
    // `action` is a SLOT, deliberately null here: this branch of the repo has
    // no owner action that can change the answer. The daemon is lazy by design,
    // and an owner-initiated "wake it and read again" is the natural occupant —
    // when such an endpoint exists, filling this field is the whole change, and
    // a surface that already renders `note.action` needs no rewrite. Shape:
    // `{ kind, label, run }` — `kind` names the action, `label` is the button
    // text, `run()` performs it and resolves once the store has re-read.
    const subject = FACET_SUBJECT[facet] || FACET_SUBJECT[FACET_ACCOUNTS];
    if (readState === READ_TRANSPORT) {
        return {
            tone: 'error', action: null,
            text: `Could not read your ${subject}${error ? ` (${error})` : ''}, so nothing `
                + 'could be listed here. Your saved choices are unchanged — retry when the '
                + 'connection is back.',
        };
    }
    if (readState === READ_NOT_READ) {
        return {
            tone: 'muted', action: null,
            text: `Ouroboros’s agent daemon is not running, so your ${subject} were never `
                + 'checked. Nothing below is missing or wrong — your saved choices are '
                + 'unchanged, and the daemon starts automatically on the next login or '
                + 'delegated run.',
        };
    }
    if (readState === READ_FAILED) {
        // The daemon IS running and one read refused: telling the owner it is
        // not running would be a second lie, and its siblings stay authoritative.
        return {
            tone: 'warn', action: null,
            text: `The agent daemon did not answer for your ${subject}${error ? ` (${error})` : ''}. `
                + 'Nothing below is missing or wrong — your saved choices are unchanged.',
        };
    }
    if (readState === READ_UNREAD) {
        return { tone: 'muted', action: null, text: `Reading your ${subject}…` };
    }
    return null;
}

export function accountRows(payload) {
    // Consume the REAL ControlCredentialProfilesResponse shape (Claudexor
    // packages/schema/src/credential-profile.ts): `profiles` is an ARRAY of
    // wrapper objects `{profile, status, identity}` with snake_case fields, and
    // `harnessAccounts` is an ARRAY of per-harness authority rows — not the flat
    // camelCase maps an earlier draft invented. Fields are read exactly as the
    // Zod schema names them so the golden fixture and the wire agree.
    //
    // Lives with the store because it is a projection OF the payload: the
    // accounts panel, the Subagents section and the login verify-race all read
    // it, and a second reader would be a second definition of "connected".
    const rows = [];
    const profiles = payload?.profiles?.profiles || [];
    const harnessAccounts = payload?.profiles?.harnessAccounts || [];
    for (const native of Array.isArray(harnessAccounts) ? harnessAccounts : []) {
        rows.push({
            harness: String(native?.harness_id || ''),
            profile_id: '',
            kind: 'native',
            identity: native?.identity || {},
            // Both engine schema versions declare this row
            // additionalProperties:false with NO status field, so presence
            // projects as local-store evidence — detected, liveness unproven.
            status: {
                verification: native?.native_login_detected ? 'passed' : '',
                verification_source: 'local_store',
            },
        });
    }
    for (const wrapper of Array.isArray(profiles) ? profiles : []) {
        const profile = wrapper?.profile || {};
        rows.push({
            harness: String(profile.harness_id || ''),
            profile_id: String(profile.profile_id || ''),
            display_name: String(profile.display_name || ''),
            kind: 'profile',
            identity: wrapper?.identity || {},
            status: wrapper?.status || {},
        });
    }
    return rows.filter((row) => row.harness);
}

export function accountLoginConfirmed(payload, harness, profileId = '') {
    // The account-status truth the login verify-race re-check reads: the row
    // for THIS harness+profile shows a login — vendor-verified or the daemon's
    // own local-store detection (accountRows already projects both as 'passed').
    const row = accountRows(payload).find((r) =>
        r.harness === String(harness || '')
        && String(r.profile_id || '') === String(profileId || ''));
    return String(row?.status?.verification || '') === 'passed';
}

// ---------------------------------------------------------------------------
// The store.
// ---------------------------------------------------------------------------

/**
 * @param {object} [options]
 * @param {Function} [options.fetchImpl]  transport (default: the gateway client)
 * @param {Function|object} [options.doc] `document`, or a getter for it
 * @param {number} [options.pollMs]       tick spacing while polling is held open
 * @returns {object} the store
 */
export function createClaudexorStatusStore({
    fetchImpl = apiFetch,
    doc = () => (typeof document === 'undefined' ? null : document),
    pollMs = DEFAULT_POLL_MS,
} = {}) {
    const getDoc = typeof doc === 'function' ? doc : () => doc;
    const inner = {
        snapshot: null,
        error: '',
        loading: false,
        everSettled: false,
        generation: 0,
        // Sticky-upgrading: the moment ANY consumer needs per-harness model
        // discovery every later read keeps carrying it. Downgrading the shared
        // snapshot would silently empty the model selects of whichever surface
        // did not happen to trigger the last read.
        includeModels: false,
        snapshotHasModels: false,
        inFlight: null,
        inFlightModels: false,
        queued: null,
        timer: 0,
        disposed: false,
        listeners: new Set(),
        holds: new Set(),
        visibilityBound: null,
    };

    function facet(name) {
        // The store adds ONE dimension the wire cannot carry: this client has
        // not read yet. Everything else is the pure, payload-level answer.
        if (!inner.everSettled) return READ_UNREAD;
        return facetReadState(inner.snapshot, name, { transportError: inner.error });
    }

    function reads() {
        const out = {};
        for (const name of STATUS_FACETS) out[name] = facet(name);
        return out;
    }

    function view() {
        return {
            snapshot: inner.snapshot,
            error: inner.error,
            loading: inner.loading,
            everSettled: inner.everSettled,
            generation: inner.generation,
            reads: reads(),
        };
    }

    function notify() {
        const payload = view();
        for (const entry of [...inner.listeners]) {
            try {
                entry.listener(payload);
            } catch (err) { /* one broken consumer must not stop the others */ }
        }
    }

    function shouldPoll() {
        if (inner.disposed) return false;
        const document_ = getDoc();
        let surfaceVisible = false;
        for (const entry of inner.listeners) {
            if (entry.visible && entry.visible()) { surfaceVisible = true; break; }
        }
        // A hidden page pauses EVERY reason to poll, a held login included:
        // nothing is on screen to update, and the daemon read is expensive
        // (it re-probes each coding-agent CLI).
        return shouldPollStatus({
            hasSubscribers: inner.listeners.size > 0,
            hidden: Boolean(document_ && document_.hidden),
            surfaceVisible,
            held: inner.holds.size > 0,
        });
    }

    function clearTimer() {
        if (inner.timer) clearTimeout(inner.timer);
        inner.timer = 0;
    }

    function armPoll() {
        clearTimer();
        if (!shouldPoll()) return;
        // A setTimeout CHAIN, never an interval: the next tick is armed only
        // after the previous read settled, so a read slower than the interval
        // can never stack a second one behind it.
        inner.timer = setTimeout(() => {
            inner.timer = 0;
            if (!shouldPoll()) return;
            refresh();
        }, pollMs);
    }

    function ensureVisibilityListener() {
        if (inner.visibilityBound || inner.disposed) return;
        const document_ = getDoc();
        if (!document_ || typeof document_.addEventListener !== 'function') return;
        inner.visibilityBound = () => {
            if (inner.disposed) return;
            armPoll();
            // Resuming means catching up, not merely re-arming: the snapshot
            // held across a hidden stretch is as old as that stretch.
            if (!document_.hidden && shouldPoll()) refresh();
        };
        document_.addEventListener('visibilitychange', inner.visibilityBound);
    }

    function startRead(withModels) {
        inner.loading = true;
        inner.inFlightModels = withModels;
        // A pre-request repaint only until the first read SETTLES: with
        // anything already said, repainting each tick is churn — and against a
        // persistently failing endpoint it flickers state every tick.
        if (!inner.everSettled) notify();
        const url = withModels ? `${STATUS_ENDPOINT}?include=models` : STATUS_ENDPOINT;
        const read = (async () => {
            let payload = null;
            let error = '';
            try {
                const resp = await fetchImpl(url, { cache: 'no-store' });
                const data = await resp.json().catch(() => ({}));
                if (resp && resp.ok) payload = data;
                else error = String((data && data.error) || `HTTP ${resp ? resp.status : 'error'}`);
            } catch (err) {
                error = String(err?.message || err);
            }
            if (inner.disposed) return inner.snapshot;
            inner.loading = false;
            inner.everSettled = true;
            inner.inFlight = null;
            if (payload) {
                inner.snapshot = payload;
                inner.snapshotHasModels = withModels;
                inner.error = '';
            } else {
                // The snapshot is KEPT (a consumer may still want to show what
                // it had), but the service state now says nobody could be
                // asked — so no consumer may read absence as discovery.
                inner.error = error || 'unreachable';
            }
            inner.generation += 1;
            notify();
            armPoll();
            return inner.snapshot;
        })();
        inner.inFlight = read;
        return read;
    }

    /**
     * Read the status once. Concurrent callers SHARE one HTTP request.
     * `includeModels` upgrades the store permanently; a caller that needs
     * models while a model-less read is already in flight is served by ONE
     * queued follow-up read, shared by every other upgrading caller.
     */
    function refresh({ includeModels = false } = {}) {
        if (inner.disposed) return Promise.resolve(inner.snapshot);
        if (includeModels) inner.includeModels = true;
        const wantModels = inner.includeModels;
        if (inner.inFlight) {
            if (!wantModels || inner.inFlightModels) return inner.inFlight;
            if (!inner.queued) {
                inner.queued = inner.inFlight.then(
                    () => { inner.queued = null; return startRead(true); },
                    () => { inner.queued = null; return startRead(true); },
                );
            }
            return inner.queued;
        }
        return startRead(wantModels);
    }

    /**
     * @param {Function} listener called with the store view on every settle
     * @param {{visible?: Function}} [options] `visible()` answers whether this
     *        consumer's surface is on screen; only such a subscriber can make
     *        the store poll.
     * @returns {Function} the disposer
     */
    function subscribe(listener, { visible = null } = {}) {
        if (typeof listener !== 'function' || inner.disposed) return () => {};
        const entry = { listener, visible: typeof visible === 'function' ? visible : null };
        inner.listeners.add(entry);
        ensureVisibilityListener();
        armPoll();
        let released = false;
        return () => {
            if (released) return;
            released = true;
            inner.listeners.delete(entry);
            armPoll();
        };
    }

    /**
     * Keep polling alive regardless of surface visibility — a live login job
     * needs the account rows to move under it.
     * @returns {Function} the release disposer
     */
    function holdPolling(reason = '') {
        if (inner.disposed) return () => {};
        const hold = { reason: String(reason || '') };
        inner.holds.add(hold);
        ensureVisibilityListener();
        armPoll();
        let released = false;
        return () => {
            if (released) return;
            released = true;
            inner.holds.delete(hold);
            armPoll();
        };
    }

    function dispose() {
        inner.disposed = true;
        clearTimer();
        const document_ = getDoc();
        if (inner.visibilityBound && document_?.removeEventListener) {
            document_.removeEventListener('visibilitychange', inner.visibilityBound);
        }
        inner.visibilityBound = null;
        inner.listeners.clear();
        inner.holds.clear();
        inner.inFlight = null;
        inner.queued = null;
    }

    return {
        get snapshot() { return inner.snapshot; },
        get error() { return inner.error; },
        get loading() { return inner.loading; },
        get everSettled() { return inner.everSettled; },
        get generation() { return inner.generation; },
        // PER-FACET provenance — the primary question. `reads` is the whole map;
        // the three convenience getters are the questions each surface asks.
        get reads() { return reads(); },
        get catalogKnown() { return facetKnown(facet(FACET_CATALOG)); },
        get accountsKnown() { return facetKnown(facet(FACET_ACCOUNTS)); },
        get quotaKnown() { return facetKnown(facet(FACET_QUOTA)); },
        get includesModels() { return inner.snapshotHasModels; },
        get polling() { return Boolean(inner.timer); },
        get subscriberCount() { return inner.listeners.size; },
        facet,
        unavailableNote(name = FACET_ACCOUNTS) {
            return statusUnavailableNote(facet(name), { error: inner.error, facet: name });
        },
        refresh,
        subscribe,
        holdPolling,
        dispose,
    };
}

// The app-wide instance every UI surface shares.
export const claudexorStatus = createClaudexorStatusStore();
