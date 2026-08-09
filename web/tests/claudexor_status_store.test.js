// The ONE shared reader over /api/claudexor/status (phase 2 seam).
//
// Three surfaces used to fetch this endpoint independently, and all three
// mapped "we could not ask" onto the same empty answer a stopped daemon gives
// — which is how a saved reviewer row came to be labeled "(not in discovery)"
// with no explanation (owner report, 2026-08-08). These tests pin the two
// properties that fix stays honest by: one request per moment in time, and
// three states nobody can collapse back together.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
    DAEMON_STATES_STOPPED,
    FACET_ACCOUNTS,
    FACET_CATALOG,
    FACET_QUOTA,
    READ_FAILED,
    READ_NOT_READ,
    READ_OK,
    READ_TRANSPORT,
    READ_UNREAD,
    accountLoginConfirmed,
    accountRows,
    bindStatusSurface,
    createClaudexorStatusStore,
    facetGapClause,
    facetKnown,
    facetReadState,
    readsFor,
    statusUnavailableNote,
} from '../modules/claudexor_status_store.js';

const RUNNING = { daemon: { state: 'running', engine_version: '3.3.13', runtime: {} }, harnesses: [{ id: 'codex' }] };
const STOPPED = { daemon: { state: 'stale', runtime: { state: 'ready', version: '3.3.13' } }, harnesses: [] };

function fakeDoc({ hidden = false } = {}) {
    const listeners = {};
    return {
        hidden,
        addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
        removeEventListener(type, fn) {
            listeners[type] = (listeners[type] || []).filter((f) => f !== fn);
        },
        fire(type) { for (const fn of [...(listeners[type] || [])]) fn(); },
        count(type) { return (listeners[type] || []).length; },
    };
}

const okResponse = (body) => ({ ok: true, status: 200, json: async () => body });

// Let an in-flight async read settle while the timers are mocked (the read
// chain is a handful of microtasks, and only a SETTLED read re-arms the poll).
const flush = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve(); };

// ---------------------------------------------------------------------------
// The three states — the whole point of the seam.
// ---------------------------------------------------------------------------

test('a stopped daemon, an unreachable endpoint and a real answer are three DIFFERENT states', () => {
    // The backend serves `harnesses: []` whenever the daemon is not running,
    // and a dead fetch leaves the same emptiness — so "no harnesses" alone can
    // never tell a consumer which world it is in. The derivation must.
    assert.equal(facetReadState(RUNNING, FACET_CATALOG), READ_OK);
    assert.equal(facetReadState(STOPPED, FACET_CATALOG), READ_NOT_READ);
    // Nothing at all to derive from is NOT "the daemon is stopped": with no
    // payload the honest answer is that the read did not land.
    assert.equal(facetReadState(null, FACET_CATALOG), READ_FAILED);
    assert.equal(facetReadState(RUNNING, FACET_CATALOG, { transportError: 'HTTP 503' }), READ_TRANSPORT);
    // A transport failure over a STALE snapshot is still "we could not ask":
    // the payload in hand describes a past read, never the current one.
    assert.equal(facetReadState(RUNNING, FACET_ACCOUNTS, { transportError: 'boom' }), READ_TRANSPORT);

    // Only a read that actually happened licenses a row-level
    // "(not in discovery)" claim.
    assert.equal(facetKnown(READ_OK), true);
    assert.equal(facetKnown(READ_NOT_READ), false);
    assert.equal(facetKnown(READ_FAILED), false);
    assert.equal(facetKnown(READ_TRANSPORT), false);
    assert.equal(facetKnown(READ_UNREAD), false);
});

test('facets are INDEPENDENT: one refused read never downgrades its siblings', () => {
    // The backend stamps provenance per fanned-out read, because one can fail
    // while the others land. A consumer that collapses them into one global
    // verdict mislabels every row the surviving facets could have described.
    const partial = {
        daemon: { state: 'unreachable', last_error: 'quota read died' },
        harnesses: [{ id: 'codex' }], profiles: {}, quota: [],
        reads: { catalog: READ_OK, accounts: READ_OK, quota: READ_FAILED },
    };
    assert.deepEqual(readsFor(partial),
        { catalog: READ_OK, accounts: READ_OK, quota: READ_FAILED });
    // …even though daemon.state says unreachable, which the LEGACY derivation
    // would have read as "nothing was read".
    assert.equal(facetReadState(partial, FACET_CATALOG), READ_OK);
    assert.equal(facetReadState(partial, FACET_QUOTA), READ_FAILED);
    // A transport failure still outranks every stamp: the whole answer is old.
    assert.deepEqual(readsFor(partial, { transportError: 'net' }),
        { catalog: READ_TRANSPORT, accounts: READ_TRANSPORT, quota: READ_TRANSPORT });
});

test('a payload WITHOUT the reads block is read legacy-style, per facet', () => {
    // The backend field is landing separately; until it does, the daemon state
    // carries the same fact at coarser resolution.
    assert.deepEqual(readsFor(RUNNING), { catalog: READ_OK, accounts: READ_OK, quota: READ_OK });
    assert.deepEqual(readsFor(STOPPED),
        { catalog: READ_NOT_READ, accounts: READ_NOT_READ, quota: READ_NOT_READ });
});

test('a PRESENT but invalid reads stamp fails closed — it never falls back to the global verdict', () => {
    // A 2xx is a transport fact, not a semantic one. The stamp existed and was
    // junk, and the store answered with the daemon's global "running" — so an
    // unparseable provenance block silently became three authoritative facets,
    // the exact collapse this seam exists to end, in the other direction.
    const running = { daemon: { state: 'running' } };
    assert.equal(facetReadState({ ...running, reads: { catalog: 'weird' } }, FACET_CATALOG), READ_FAILED);
    // A PARTIAL stamp answers only for what it stamps; the unstamped siblings
    // are gaps, not inherited successes.
    const partial = { ...running, reads: { catalog: READ_OK } };
    assert.deepEqual(readsFor(partial),
        { catalog: READ_OK, accounts: READ_FAILED, quota: READ_FAILED });
    // …and a `reads` that is not even a map fails closed for every facet.
    for (const bad of [null, 'ok', 42, ['ok']]) {
        assert.deepEqual(readsFor({ ...running, reads: bad }),
            { catalog: READ_FAILED, accounts: READ_FAILED, quota: READ_FAILED },
            `reads: ${JSON.stringify(bad)} must not license a discovery claim`);
    }
});

test('unreachable is NOT stopped: only a genuinely stopped daemon gets the stopped sentence', () => {
    // The live endpoint turns a refusal in ANY of its fanned-out reads into one
    // global daemon.state = "unreachable" — the reviewer probed it with catalog
    // and accounts SUCCEEDING and only quota failing, and every facet still read
    // "never asked" while the copy announced a daemon that was in fact running.
    for (const state of DAEMON_STATES_STOPPED) {
        assert.deepEqual(readsFor({ daemon: { state } }),
            { catalog: READ_NOT_READ, accounts: READ_NOT_READ, quota: READ_NOT_READ },
            `${state} genuinely means nobody was asked`);
        // The CALM sentence, and it stops at what this state establishes:
        // nobody asked. WHY nobody asked — a stopped daemon, a runtime awaiting
        // repair, a foreign daemon on the stale port, an ownership problem — is
        // the tab banner's job, because only the daemon state can tell those
        // apart and `foreign_daemon` lands here with a daemon that IS running.
        assert.match(statusUnavailableNote(READ_NOT_READ).text, /daemon was not asked/);
        assert.doesNotMatch(statusUnavailableNote(READ_NOT_READ).text, /could not be read/);
    }
    for (const state of ['unreachable', 'error', 'unknown', '']) {
        assert.deepEqual(readsFor({ daemon: { state } }),
            { catalog: READ_FAILED, accounts: READ_FAILED, quota: READ_FAILED },
            `${state || '(no state)'} means the status could not be read`);
    }
    // …and the WORDING per state, which is what the owner actually reads.
    const wording = (state) => statusUnavailableNote(
        facetReadState({ daemon: { state } }, FACET_ACCOUNTS), { facet: FACET_ACCOUNTS }).text;
    assert.match(wording('stale'), /daemon was not asked/);
    assert.match(wording('unreachable'), /could not be read/);
    // A read that DID happen and did not land must never be dressed as a read
    // nobody attempted — that is the calm sentence, and it would tell an owner
    // whose daemon is up that nothing was even tried.
    assert.doesNotMatch(wording('unreachable'), /was not asked/);
    assert.doesNotMatch(wording('error'), /was not asked/);
    assert.doesNotMatch(wording(''), /was not asked/);
    assert.equal(statusUnavailableNote(facetReadState({ daemon: { state: 'running' } }, FACET_ACCOUNTS)), null);
});

test('a surface renders more than one facet, and the note names the second gap too', () => {
    // The accounts panel renders rows AND windows, the reviewer rows render
    // routes AND account pins: a banner that consults one facet leaves the
    // other's stale value on screen dressed as fresh.
    const reads = { catalog: READ_OK, accounts: READ_OK, quota: READ_FAILED };
    assert.equal(facetGapClause(reads, [FACET_CATALOG]), '');
    assert.match(facetGapClause(reads, [FACET_QUOTA]), /^Subscription limits could not be read/);
    assert.match(facetGapClause(reads, [FACET_QUOTA]), /is last known\.$/);
    // Two gaps read as one sentence, and an unread-yet facet is not a gap.
    const both = facetGapClause({ catalog: READ_NOT_READ, accounts: READ_UNREAD, quota: READ_TRANSPORT },
        [FACET_CATALOG, FACET_ACCOUNTS, FACET_QUOTA]);
    assert.match(both, /Agents and subscription limits could not be read/);
    assert.match(both, /shown for them is last known/);
    assert.equal(facetGapClause({ catalog: READ_OK }, [FACET_CATALOG]), '');
    assert.equal(facetGapClause({}, [FACET_CATALOG]), '');
});

test('each unavailable read state has ONE shared sentence, and an ok read has none', () => {
    const down = statusUnavailableNote(READ_NOT_READ);
    // NOT READ = never asked. It is NOT a diagnosis: a runtime that needs
    // repair, a foreign daemon and an ownership problem all land here, and
    // once the backend stamps `reads` per facet a RUNNING daemon can leave
    // one facet unasked. Naming a cause here would be a lie in all four cases.
    assert.match(down.text, /daemon was not asked/);
    assert.doesNotMatch(down.text, /is not running/);
    assert.match(down.text, /saved choices are unchanged/);
    assert.doesNotMatch(down.text, /not in discovery/);

    const dead = statusUnavailableNote(READ_TRANSPORT, { error: 'HTTP 503' });
    assert.match(dead.text, /HTTP 503/);
    assert.equal(dead.tone, 'error');

    const refused = statusUnavailableNote(READ_FAILED);
    assert.match(refused.text, /could not be read/);
    // A read that did not land NEVER claims a stopped daemon: the same state is
    // reached by a running daemon refusing one read and by an `unreachable`
    // answer, and both would be mislabeled by the stopped sentence.
    assert.doesNotMatch(refused.text, /not running/);
    // All three are distinct: "nobody asked", "the request died", "it refused".
    assert.equal(new Set([down.text, dead.text, refused.text]).size, 3);

    assert.match(statusUnavailableNote(READ_UNREAD).text, /Reading your agent accounts/);
    assert.equal(statusUnavailableNote(READ_OK), null);
    for (const state of [READ_NOT_READ, READ_FAILED, READ_TRANSPORT, READ_UNREAD]) {
        for (const facet of [FACET_CATALOG, FACET_ACCOUNTS, FACET_QUOTA]) {
            assert.doesNotMatch(statusUnavailableNote(state, { facet }).text, /coding[ -]agent/i);
        }
    }

    // The note names the facet it is about, so three surfaces can share it.
    // D-10: the subject is "agents", not "coding agents" — the same
    // subscriptions build presentations and run arbitrary tasks.
    assert.match(statusUnavailableNote(READ_NOT_READ, { facet: FACET_CATALOG }).text, /your agents were never checked/);
    assert.doesNotMatch(statusUnavailableNote(READ_NOT_READ, { facet: FACET_CATALOG }).text, /coding/);
    assert.match(statusUnavailableNote(READ_NOT_READ, { facet: FACET_QUOTA }).text, /subscription limits/);

    // The ACTION slot exists and is empty on this branch — an owner action that
    // could change the answer attaches here without reshaping any consumer.
    assert.equal(down.action, null);
    assert.ok('action' in down);
});

test('a store over a stopped daemon reports every facet as never-read', async () => {
    const store = createClaudexorStatusStore({
        fetchImpl: async () => okResponse(STOPPED), doc: fakeDoc(),
    });
    await store.refresh();
    assert.deepEqual(store.reads,
        { catalog: READ_NOT_READ, accounts: READ_NOT_READ, quota: READ_NOT_READ });
    assert.equal(store.catalogKnown, false);
    assert.equal(store.accountsKnown, false);
    assert.equal(store.error, '');           // nothing failed — we DID get an answer
    assert.deepEqual(store.snapshot, STOPPED);
    store.dispose();
});

test('the store says the right sentence for EVERY daemon state it can be served', async () => {
    // One case per state, because the sentence is what the owner reads and the
    // mislabel is invisible in the data. The reviewer's live probe: catalog and
    // accounts SUCCEEDED, quota failed, the payload carried both successes —
    // and the panel announced a daemon that was not running.
    const cases = [
        ['running', READ_OK, null],
        ['stale', READ_NOT_READ, /daemon was not asked/],
        ['not_provisioned', READ_NOT_READ, /daemon was not asked/],
        ['foreign_daemon', READ_NOT_READ, /daemon was not asked/],
        ['unreachable', READ_FAILED, /could not be read/],
        ['error', READ_FAILED, /could not be read/],
        ['', READ_FAILED, /could not be read/],
    ];
    for (const [daemonState, expected, wording] of cases) {
        const store = createClaudexorStatusStore({
            fetchImpl: async () => okResponse({ daemon: { state: daemonState }, harnesses: [] }),
            doc: fakeDoc(),
        });
        await store.refresh();
        assert.deepEqual(store.reads, { catalog: expected, accounts: expected, quota: expected },
            `daemon.state=${daemonState || '(none)'}`);
        const note = store.unavailableNote(FACET_ACCOUNTS);
        if (wording === null) {
            assert.equal(note, null, `daemon.state=${daemonState} needs no note`);
        } else {
            assert.match(note.text, wording, `daemon.state=${daemonState}`);
            if (expected !== READ_NOT_READ) {
                assert.doesNotMatch(note.text, /was not asked/,
                    `daemon.state=${daemonState} was asked — it just did not answer`);
            }
        }
        store.dispose();
    }
});

test("a read that did not land carries the daemon's own explanation", async () => {
    // The exact payload the reviewer probed: the daemon IS running, one fanned
    // -out read refused, and the endpoint collapsed that into `unreachable`
    // plus a last_error. Routing the state to the shared sentence must not lose
    // the only detail the owner has.
    const store = createClaudexorStatusStore({
        fetchImpl: async () => okResponse({
            daemon: { state: 'unreachable', last_error: 'quota_read_failed: window read died' },
            harnesses: [{ id: 'codex' }], profiles: {}, quota: [],
        }),
        doc: fakeDoc(),
    });
    await store.refresh();
    const note = store.unavailableNote(FACET_ACCOUNTS);
    assert.match(note.text, /could not be read/);
    assert.match(note.text, /window read died/, "the daemon's own reason survives");
    assert.doesNotMatch(note.text, /not running/);
    // A stopped daemon keeps its calm line — a crashed daemon also lands in
    // `stale`, and that decision is deliberate.
    const stopped = createClaudexorStatusStore({
        fetchImpl: async () => okResponse({ daemon: { state: 'stale', last_error: 'boom' } }),
        doc: fakeDoc(),
    });
    await stopped.refresh();
    assert.doesNotMatch(stopped.unavailableNote(FACET_ACCOUNTS).text, /boom/);
    store.dispose();
    stopped.dispose();
});

test('a 200 that is not a status answer is a protocol failure, not an empty world', async () => {
    // A 200 carrying non-JSON parsed to {} and sailed through every facet
    // derivation, so the app confidently rendered "nothing is connected" off a
    // body it never understood.
    for (const body of [null, {}, [], 'ok', { daemon: 'running' }]) {
        const store = createClaudexorStatusStore({
            fetchImpl: async () => ({ ok: true, status: 200, json: async () => body }),
            doc: fakeDoc(),
        });
        await store.refresh();
        assert.deepEqual(store.reads,
            { catalog: READ_TRANSPORT, accounts: READ_TRANSPORT, quota: READ_TRANSPORT },
            `body ${JSON.stringify(body)} must not read as an answer`);
        assert.match(store.error, /could not be understood/);
        assert.equal(store.accountsKnown, false);
        store.dispose();
    }
    // A body that IS a status answer still lands, empty collections included.
    const good = createClaudexorStatusStore({
        fetchImpl: async () => okResponse(RUNNING), doc: fakeDoc(),
    });
    await good.refresh();
    assert.equal(good.error, '');
    assert.equal(good.accountsKnown, true);
    good.dispose();
});

test('a non-2xx answer is a transport error, not a silent stale snapshot', async () => {
    // The old private poller kept the last good payload and said NOTHING on an
    // HTTP failure, so a dead endpoint was indistinguishable from a healthy
    // idle daemon.
    let ok = true;
    const store = createClaudexorStatusStore({
        fetchImpl: async () => (ok
            ? okResponse(RUNNING)
            : { ok: false, status: 503, json: async () => ({ error: 'daemon exploded' }) }),
        doc: fakeDoc(),
    });
    await store.refresh();
    assert.equal(store.catalogKnown, true);
    ok = false;
    await store.refresh();
    assert.equal(store.facet(FACET_CATALOG), READ_TRANSPORT);
    assert.equal(store.facet(FACET_ACCOUNTS), READ_TRANSPORT);
    assert.match(store.error, /daemon exploded/);
    // The snapshot is kept for whoever still wants to show it, but it no
    // longer counts as discovery.
    assert.equal(store.catalogKnown, false);
    assert.deepEqual(store.snapshot, RUNNING);
    store.dispose();
});

// ---------------------------------------------------------------------------
// One request per moment in time.
// ---------------------------------------------------------------------------

test('concurrent refreshes share ONE http request and ONE snapshot', async () => {
    let started = 0;
    let release = null;
    const gate = new Promise((resolve) => { release = resolve; });
    const store = createClaudexorStatusStore({
        fetchImpl: async () => { started += 1; await gate; return okResponse(RUNNING); },
        doc: fakeDoc(),
    });
    const calls = [store.refresh(), store.refresh(), store.refresh()];
    assert.equal(started, 1, 'overlapping callers did not start a second read');
    release();
    const answers = await Promise.all(calls);
    assert.equal(started, 1, 'the shared read served every caller');
    assert.equal(new Set(answers).size, 1, 'every caller got the SAME snapshot object');
    // A settled read releases the slot: the next call really reads again.
    await store.refresh();
    assert.equal(started, 2);
    store.dispose();
});

test('includeModels is sticky-upgrading: no later read downgrades the shared snapshot', async () => {
    const urls = [];
    let release = null;
    const gate = new Promise((resolve) => { release = resolve; });
    const store = createClaudexorStatusStore({
        fetchImpl: async (url) => { urls.push(url); await gate; return okResponse(RUNNING); },
        doc: fakeDoc(),
    });
    // The accounts panel's model-less poll is in flight when the review-lane
    // rows ask for discovery: the upgrade cannot be served by that request, so ONE
    // follow-up is queued — and every upgrading caller shares it.
    const plain = store.refresh();
    const upgrade = [store.refresh({ includeModels: true }), store.refresh({ includeModels: true })];
    assert.equal(urls.length, 1);
    release();
    await Promise.all([plain, ...upgrade]);
    assert.deepEqual(urls, ['/api/claudexor/status', '/api/claudexor/status?include=models']);
    assert.equal(store.includesModels, true);

    // Sticky: a later plain refresh KEEPS models rather than downgrading the
    // snapshot the review-lane and delegation selects depend on.
    await store.refresh();
    assert.equal(urls.at(-1), '/api/claudexor/status?include=models');
    assert.equal(store.includesModels, true);
    store.dispose();
});

// ---------------------------------------------------------------------------
// Polling is a held resource, and every acquisition has a disposer.
// ---------------------------------------------------------------------------

test('polling needs a subscriber AND a visible surface; unsubscribing disarms the timer', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    let reads = 0;
    let visible = false;
    const doc = fakeDoc();
    const store = createClaudexorStatusStore({
        fetchImpl: async () => { reads += 1; return okResponse(RUNNING); },
        doc, pollMs: 5000,
    });
    // No subscriber: an explicit read runs, but nothing is armed afterwards.
    await store.refresh();
    assert.equal(reads, 1);
    assert.equal(store.polling, false, 'a store nobody listens to never arms a timer');

    const off = store.subscribe(() => {}, { visible: () => visible });
    assert.equal(store.polling, false, 'subscribed but off-screen: still no timer');
    visible = true;
    // A visibility event re-evaluates the gate (and catches up on the stretch
    // spent hidden).
    doc.fire('visibilitychange');
    await flush();
    assert.equal(reads, 2, 'becoming visible catches up on the hidden stretch');
    assert.equal(store.polling, true, 'visible surface + subscriber: polling');

    t.mock.timers.tick(5000);
    await flush();
    assert.equal(reads, 3, 'the armed tick performed exactly one more read');

    off();
    assert.equal(store.polling, false, 'the unsubscribe disposer disarmed the timer');
    assert.equal(store.subscriberCount, 0);
    store.dispose();
    t.mock.timers.reset();
});

test('any bound surface can keep the poll armed — not only the accounts panel', async (t) => {
    // The two sections that moved off the accounts panel's tab subscribed with
    // NO visibility predicate and no activation hook, so the store had nobody
    // who could say "I am on screen": polling stayed false and the fetch count
    // stuck at 1, while their comments promised the daemon recovering would be
    // picked up without a reload. Here the accounts panel is HIDDEN and only the
    // other surface is visible.
    t.mock.timers.enable({ apis: ['setTimeout'] });
    let reads = 0;
    const elements = {
        'harness-accounts-rows': { offsetParent: null },        // its tab is not active
        'reviewer-triad-rows': { offsetParent: {} },            // this surface IS on screen
    };
    const doc = fakeDoc();
    doc.getElementById = (id) => elements[id] || null;
    const win = (() => {
        const listeners = {};
        return {
            addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
            removeEventListener(type, fn) {
                listeners[type] = (listeners[type] || []).filter((f) => f !== fn);
            },
            fire(type) { for (const fn of [...(listeners[type] || [])]) fn(); },
            count(type) { return (listeners[type] || []).length; },
        };
    })();
    const store = createClaudexorStatusStore({
        fetchImpl: async () => { reads += 1; return okResponse(RUNNING); }, doc, pollMs: 5000,
    });
    const releaseAccounts = bindStatusSurface(store, {
        elementId: 'harness-accounts-rows', listener: () => {}, doc, win,
    });
    const releaseReviewer = bindStatusSurface(store, {
        elementId: 'reviewer-triad-rows', listener: () => {}, includeModels: true, doc, win,
    });
    await store.refresh();
    assert.equal(reads, 1);
    assert.equal(store.polling, true, 'the visible surface arms the timer on its own');

    t.mock.timers.tick(5000);
    await flush();
    assert.equal(reads, 2, 'a SECOND tick happened for a surface that is not the accounts panel');

    // Activation is judged by the same predicate — no tab name anywhere — so the
    // section that is on screen catches up, and the hidden one costs nothing.
    win.fire('ouro:settings-subtab-shown');
    await flush();
    assert.equal(reads, 3, 'reaching the surface reads immediately, without a reload');

    // Hide it: the last visible surface leaving disarms the timer.
    elements['reviewer-triad-rows'].offsetParent = null;
    doc.fire('visibilitychange');
    assert.equal(store.polling, false);

    releaseReviewer();
    releaseAccounts();
    assert.equal(store.subscriberCount, 0, 'the binding disposer released the subscription');
    assert.equal(win.count('ouro:page-shown'), 0, 'and both window listeners');
    assert.equal(win.count('ouro:settings-subtab-shown'), 0);
    store.dispose();
    t.mock.timers.reset();
});

test('a hidden page pauses polling, and a login hold keeps it awake off-surface', () => {
    const doc = fakeDoc();
    const store = createClaudexorStatusStore({
        fetchImpl: async () => okResponse(RUNNING), doc, pollMs: 5000,
    });
    store.subscribe(() => {}, { visible: () => true });
    assert.equal(store.polling, true);

    doc.hidden = true;
    doc.fire('visibilitychange');
    assert.equal(store.polling, false, 'document.hidden pauses the poll');
    doc.hidden = false;
    doc.fire('visibilitychange');
    assert.equal(store.polling, true, 'and it resumes on visibility');

    // Off-surface (settings closed) the poll stops — unless a login job holds it.
    const store2 = createClaudexorStatusStore({
        fetchImpl: async () => okResponse(RUNNING), doc: fakeDoc(), pollMs: 5000,
    });
    store2.subscribe(() => {}, { visible: () => false });
    assert.equal(store2.polling, false);
    const release = store2.holdPolling('login-job');
    assert.equal(store2.polling, true, 'a live login keeps the account rows moving');
    release();
    assert.equal(store2.polling, false, 'the hold disposer releases it');
    store.dispose();
    store2.dispose();
});

test('dispose releases the timer, the listeners and the visibilitychange handler', async () => {
    const doc = fakeDoc();
    let reads = 0;
    const store = createClaudexorStatusStore({
        fetchImpl: async () => { reads += 1; return okResponse(RUNNING); }, doc, pollMs: 5000,
    });
    const seen = [];
    store.subscribe((v) => seen.push(v.reads.accounts), { visible: () => true });
    store.holdPolling('login-job');
    await store.refresh();
    assert.ok(seen.length > 0);
    assert.equal(doc.count('visibilitychange'), 1);
    assert.equal(store.polling, true);

    store.dispose();
    assert.equal(store.polling, false, 'no timer left armed');
    assert.equal(doc.count('visibilitychange'), 0, 'the document listener is removed');
    assert.equal(store.subscriberCount, 0);
    // A disposed store neither notifies nor reads again.
    const before = reads;
    seen.length = 0;
    await store.refresh();
    assert.equal(reads, before, 'a disposed store performs no further reads');
    assert.deepEqual(seen, []);
    // …and it is idempotent (a double destroy must not throw).
    store.dispose();
});

test('subscribers are notified with the settled view, and one broken listener cannot silence the rest', async () => {
    const store = createClaudexorStatusStore({
        fetchImpl: async () => okResponse(RUNNING), doc: fakeDoc(),
    });
    const seen = [];
    store.subscribe(() => { throw new Error('this consumer is broken'); });
    store.subscribe((v) => seen.push(v));
    await store.refresh();
    // Two paints for the FIRST read only: the announcement before it lands
    // ("Checking Claudexor…", which the panel needs because the daemon probes
    // every CLI and can take a minute) and the settled answer. Later reads
    // notify once — a repaint per poll tick is churn.
    assert.equal(seen.length, 2);
    assert.equal(seen[0].loading, true);
    // Before the first read settles EVERY facet is honestly "unread" — the
    // store's own dimension, which the wire cannot carry.
    assert.deepEqual(seen[0].reads,
        { catalog: READ_UNREAD, accounts: READ_UNREAD, quota: READ_UNREAD });
    assert.deepEqual(seen[1].reads, { catalog: READ_OK, accounts: READ_OK, quota: READ_OK });
    assert.equal(seen[1].generation, 1);
    assert.equal(seen[1].loading, false);
    seen.length = 0;
    await store.refresh();
    assert.equal(seen.length, 1, 'no pre-request repaint once anything has been said');
    assert.equal(store.everSettled, true);
    store.dispose();
});

// ---------------------------------------------------------------------------
// The payload projection lives with the payload.
// ---------------------------------------------------------------------------

test('accountRows and accountLoginConfirmed read the wire shape from ONE place', () => {
    const payload = { profiles: {
        harnessAccounts: [{ harness_id: 'codex', native_login_detected: true }],
        profiles: [{ profile: { harness_id: 'claude', profile_id: 'work' }, status: { verification: 'passed', verification_source: 'vendor' } }],
    } };
    const rows = accountRows(payload);
    assert.deepEqual(rows.map((r) => [r.harness, r.profile_id, r.kind]),
        [['codex', '', 'native'], ['claude', 'work', 'profile']]);
    assert.equal(accountLoginConfirmed(payload, 'codex', ''), true);
    assert.equal(accountLoginConfirmed(payload, 'claude', 'work'), true);
    assert.equal(accountLoginConfirmed(payload, 'claude', 'other'), false);
    assert.equal(accountLoginConfirmed({}, 'codex', ''), false);
});
