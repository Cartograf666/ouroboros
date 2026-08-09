// The Agents tab's own shape: family grouping, the ONE service banner, the
// account-row anatomy, and removal.
//
// The behaviours pinned here are the owner's report, one assertion each:
// accounts of one family are EQUIVALENT and grouped, the add affordance lives
// in the family header, the limit text is compact and humanized, and a daemon
// problem is explained once at the top instead of decorating every row.
//
// harness_accounts.test.js keeps the login-flow and payload-shape coverage; the
// two files split by subject, not by module.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import {
    createClaudexorStatusStore,
    statusUnavailableNote,
} from '../modules/claudexor_status_store.js';
import {
    accountGroups,
    accountMetaLine,
    accountName,
    familyActionLabel,
    familyLabel,
    familyStatus,
    humanizeResetAt,
    quotaSummary,
    removeAccount,
    removeAccountConfirmBody,
    rowActionLabel,
    serviceBannerLine,
} from '../modules/harness_accounts.js';
import { pinnedAccountWarning } from '../modules/reviewer_slots.js';

const MULTI = JSON.parse(readFileSync(
    fileURLToPath(new URL('./fixtures/credential_profiles_multi.json', import.meta.url)), 'utf-8'));

const RUNNING = { state: 'running', engine_version: '3.3.13', runtime: { state: 'ready' } };

function payload({ harnesses = [], profiles = {}, quota = [], daemon = RUNNING } = {}) {
    return { daemon, harnesses, profiles, quota };
}

function fakeStore(reads, { error = '', snapshot = null } = {}) {
    // The fake wraps the REAL sentence factory. An invented note ("…for your
    // quota") let a copy regression pass green: the banner assertions pinned
    // the fake's words instead of the product's ("…for your subscription
    // limits"), which is exactly the wording nothing else pins either. Same
    // reason for the DETAIL rule below: it mirrors the store's own, so a banner
    // that stopped carrying the daemon's `last_error` fails here too.
    const held = snapshot || { daemon: RUNNING };
    return {
        reads,
        facet: (name) => reads[name],
        error,
        snapshot: held,
        loading: false,
        everSettled: true,
        unavailableNote: (facet, { subject = '' } = {}) => {
            const state = reads[facet];
            const detail = error
                || (state === 'failed' ? String(held?.daemon?.last_error || '') : '');
            return statusUnavailableNote(state, { error: detail, facet, subject });
        },
    };
}

const ALL = (value) => ({ catalog: value, accounts: value, quota: value });

// ---------------------------------------------------------------------------
// Grouping: zero accounts, one account, several accounts in one family.
// ---------------------------------------------------------------------------

test('a fresh install still shows every family, each with its own way in', () => {
    // Discovery needs a running daemon and a first run has none, so with the
    // catalogue empty the three bootstrap families must still be reachable —
    // otherwise the whole onboarding path is a dead end.
    const groups = accountGroups(payload({ daemon: { state: 'not_provisioned', runtime: {} } }),
        { accountsRead: 'not_read' });
    assert.deepEqual(groups.map((g) => g.harness), ['codex', 'claude', 'cursor']);
    assert.deepEqual(groups.map((g) => g.label), ['Codex', 'Claude Code', 'Cursor']);
    for (const group of groups) {
        assert.deepEqual(group.rows, []);
        // BIBLE P1: an idle daemon was never asked, so "no account connected"
        // is a claim nobody earned.
        assert.match(group.status.label, /Not checked/);
        assert.equal(familyActionLabel(group, { daemon: { state: 'not_provisioned', runtime: {} } }),
            'Connect');
    }
});

test('an empty family that WAS read says so, and its button still connects', () => {
    const groups = accountGroups(payload({ harnesses: [{ id: 'codex', display_name: 'Codex CLI' }] }),
        { accountsRead: 'ok' });
    const codex = groups.find((g) => g.harness === 'codex');
    assert.equal(codex.label, 'Codex CLI');  // discovery wins over the bootstrap name
    assert.equal(codex.status.label, 'No account connected');
    assert.equal(familyActionLabel(codex, payload()), 'Connect');
    // A runtime that needs work carries that intent into the same button.
    assert.equal(familyActionLabel(codex,
        { daemon: { state: 'not_provisioned', runtime: { state: 'missing' } } }), 'Install & connect');
});

test('several accounts of one family are grouped, counted, and called equivalent', () => {
    // The owner's ask, verbatim: «все акки клод кода должны быть эквивалентны».
    // Three codex accounts (one native + two named) become ONE card whose
    // header says they rotate — no row is the "real" one.
    const groups = accountGroups(payload({ profiles: MULTI }), { accountsRead: 'ok' });
    const codex = groups.find((g) => g.harness === 'codex');
    assert.equal(codex.rows.length, 3);
    assert.equal(codex.status.tone, 'ok');
    assert.match(codex.status.label, /3 accounts · rotating/);
    // Once a family HAS accounts its header button ADDS one, which is what
    // makes them equivalent instead of one-default-plus-extras. The affordance
    // used to hang off the native row only.
    assert.equal(familyActionLabel(codex, payload()), 'Add account');

    // The claude family in the same fixture has a failed vendor verification:
    // the header says how many need attention rather than hiding it.
    const claude = groups.find((g) => g.harness === 'claude');
    assert.equal(claude.status.tone, 'error');
    assert.match(claude.status.label, /of 2 need attention/);
});

test('one connected account reads as connected, not as a count', () => {
    const rows = [{ harness: 'codex', profile_id: 'work', kind: 'profile',
        status: { verification: 'passed', verification_source: 'vendor' } }];
    assert.deepEqual(familyStatus(rows, { accountsRead: 'ok' }), { tone: 'ok', label: 'Connected' });
    // Present but never signed in: not an alarm, and not a lie either.
    const cold = [{ harness: 'codex', profile_id: 'work', kind: 'profile', status: {} }];
    assert.deepEqual(familyStatus(cold, { accountsRead: 'ok' }),
        { tone: 'muted', label: '1 account · not signed in' });
});

test('"N accounts · rotating" counts only the accounts rotation can actually use', () => {
    // Caught by LOOKING at the rendered tab: a Cursor family holding one signed-in
    // account and one cold native row announced "2 accounts · rotating", promising
    // a rotation width that did not exist.
    const mixed = [
        { harness: 'cursor', profile_id: '', kind: 'native', status: {} },
        { harness: 'cursor', profile_id: 'ultra', kind: 'profile',
          status: { verification: 'passed', verification_source: 'local_store' } },
    ];
    assert.deepEqual(familyStatus(mixed, { accountsRead: 'ok' }),
        { tone: 'ok', label: '1 of 2 connected' });
});

test('the family button keeps its ADD intent even when the runtime needs repair', () => {
    // DISCLOSED RESIDUAL, pinned so a change to it is deliberate. `rowActionLabel`
    // hands its label to a broken runtime; this one does not, because the two
    // buttons DO different things. The family button asks for an account name
    // and then starts a login — a header reading "Fix & connect" that opens a
    // name-the-account dialog would misdescribe the click, and dropping the name
    // step would remove the add intent the card exists for. The repair is a
    // prerequisite the login card performs and reports in the foreground, and
    // the service banner above already names the fault.
    const populated = accountGroups(payload({ profiles: MULTI }), { accountsRead: 'ok' })
        .find((g) => g.harness === 'codex');
    const broken = { daemon: { state: 'stale', runtime: { state: 'error' } } };
    assert.equal(familyActionLabel(populated, broken), 'Add account');
    assert.equal(rowActionLabel(populated.rows[0], broken), 'Fix & connect');
    // An EMPTY family has no add intent to protect, so it does carry the runtime.
    const empty = { rows: [] };
    assert.equal(familyActionLabel(empty, broken), 'Fix & connect');
});

test('a row offers what it can actually do, and runtime work outranks it', () => {
    // Also from the render: a row whose vendor verification FAILED offered
    // "Connect", the label that belongs to a family with no account at all.
    const failed = { harness: 'claude', profile_id: 'valentine', kind: 'profile',
        status: { verification: 'failed', verification_source: 'vendor' } };
    const live = { harness: 'claude', profile_id: 'mironov', kind: 'profile',
        status: { verification: 'passed', verification_source: 'vendor' } };
    assert.equal(rowActionLabel(failed, payload()), 'Sign in');
    assert.equal(rowActionLabel(live, payload()), 'Sign in again');
    // A runtime that needs installing/repairing owns the label either way: that
    // work happens first whatever the row wants.
    const broken = { daemon: { state: 'not_provisioned', runtime: { state: 'error' } } };
    assert.equal(rowActionLabel(live, broken), 'Fix & connect');
});

// ---------------------------------------------------------------------------
// Row anatomy.
// ---------------------------------------------------------------------------

test('the default CLI login is a row like any other, with an honest caption', () => {
    // Equivalence is layout-level: the native row uses the same two lines and
    // the same actions. What differs is a CAPTION — and the caption is the
    // truthful reason there is no Remove button on it.
    const native = { harness: 'codex', profile_id: '', kind: 'native', identity: {},
        status: { verification: 'passed', verification_source: 'local_store' } };
    assert.equal(accountName(native), 'Default CLI login');
    const meta = accountMetaLine(native, payload({ harnesses: [{ id: 'codex' }] }));
    assert.match(meta, /^Managed by the Codex CLI/);
    // Removal is a NAMED-profile affordance: this app cannot honestly sign a
    // vendor CLI out, so it does not offer to.
    assert.equal(rowActionLabel(native, payload()), 'Sign in again');
});

test('the metadata line leads with humanized usage, never a raw ISO instant', () => {
    const now = Date.parse('2026-08-09T12:00:00Z');
    const row = {
        harness: 'claude', profile_id: 'work', kind: 'profile',
        identity: { email: 'a@example.com', plan: 'Max' },
        // formatRelativeAge measures against the real clock, so the "checked"
        // fixture is anchored to it while the quota reset uses the fixed now.
        status: { verification: 'passed', verification_source: 'vendor',
            last_verified_at: new Date(Date.now() - 10 * 60000).toISOString() },
    };
    const meta = accountMetaLine(row, payload({
        quota: [{ subject: { harness: 'claude', subject_id: 'work' }, freshness: 'fresh',
            constraints: [{ used_ratio: 0.38, resets_at: '2026-08-09T14:00:00Z' }] }],
    }), { nowMs: now });
    assert.match(meta, /^38% used · resets in 2h/);
    assert.match(meta, /Max/);
    assert.match(meta, /a@example\.com/);
    assert.match(meta, /checked 10m ago/);
    assert.doesNotMatch(meta, /2026-08-09T/);
});

test('reset times are humanized across the whole range, and absence stays absent', () => {
    const now = Date.parse('2026-08-09T12:00:00Z');
    assert.equal(humanizeResetAt('2026-08-09T12:45:00Z', now), 'in 45m');
    assert.equal(humanizeResetAt('2026-08-09T14:00:00Z', now), 'in 2h');
    assert.equal(humanizeResetAt('2026-08-12T12:00:00Z', now), 'in 3d');
    assert.equal(humanizeResetAt('2026-08-09T12:00:30Z', now), 'in a moment');
    assert.equal(humanizeResetAt('', now), '');
    assert.equal(humanizeResetAt('not-a-date', now), '');
});

test('the three limit sentences are the three different facts', () => {
    const now = Date.parse('2026-08-09T12:00:00Z');
    const subject = { harness: 'codex', subject_id: 'work' };
    const spent = quotaSummary([{ subject, freshness: 'fresh',
        constraints: [{ used_ratio: 1.0, resets_at: '2026-08-09T14:00:00Z' }] }],
    'codex', 'work', { nowMs: now });
    assert.equal(spent.label, 'Limit reached · resets in 2h');
    assert.equal(spent.tone, 'warn');
    // READ, and this account has nothing to report.
    assert.equal(quotaSummary([], 'codex', 'work', { nowMs: now }).label, 'Usage unavailable');
    // NOT read: a gap, and never dressed as a full or empty window.
    assert.equal(quotaSummary([], 'codex', 'work', { quotaRead: 'not_read' }).label,
        'Limits not checked');
});

// ---------------------------------------------------------------------------
// The ONE service banner.
// ---------------------------------------------------------------------------

test('the banner is the only place a service problem is explained', () => {
    // Owner report (2026-08-08): a stopped daemon decorated every saved row
    // with "(not in discovery)" and explained nothing. One sentence, at the
    // top, that names the whole tab.
    // A service line that EXPLAINS why nothing was read speaks first: the idle
    // daemon's own sentence carries what happens next ("starts automatically on
    // the next login"), which the generic note does not. The generic note is the
    // fallback for when the service line has nothing concrete to say.
    const line = serviceBannerLine(fakeStore(ALL('not_read'), {
        snapshot: { daemon: { state: 'stale', runtime: { state: 'ready' } } },
    }));
    assert.match(line.text, /agent daemon is not running/);
    assert.match(line.text, /starts automatically/);
    assert.equal(line.tone, 'muted');
    // And when the service line explains NOTHING about the gap — a daemon that
    // is up and healthy while the reads are unstamped — the generic note is what
    // shows, because "Claudexor ready" printed over unread facts is the
    // reassuring lie this whole precedence rule exists to prevent.
    const healthy = serviceBannerLine(fakeStore(ALL('not_read'), {
        snapshot: { daemon: { state: 'running', engine_version: '3.3.13' } },
    }));
    assert.match(healthy.text, /agents, accounts and limits/);
    assert.doesNotMatch(healthy.text, /Claudexor ready/);
    // Healthy: the ordinary lifecycle sentence, unchanged.
    assert.match(serviceBannerLine(fakeStore(ALL('ok'))).text, /Claudexor ready/);
});

test('a BROKEN service is never reported as "nothing below is missing or wrong"', () => {
    // The reachable lie: every settled state that is not `running` leaves all
    // three facets unread, so the benign not-read note used to be the ONLY
    // sentence on the tab — while the row buttons beside it said "Fix &
    // connect". The whole error/warn vocabulary daemonStatusLine speaks was
    // unreachable in exactly the states that need it.
    const broken = (daemon) => serviceBannerLine(fakeStore(ALL('not_read'), { snapshot: { daemon } }));

    const repair = broken({ state: 'stale', runtime: { state: 'error', last_error: 'checksum mismatch' } });
    assert.equal(repair.tone, 'error');
    assert.match(repair.text, /needs repair/);
    assert.match(repair.text, /checksum mismatch/);
    assert.doesNotMatch(repair.text, /Nothing below is missing or wrong/);

    const foreign = broken({ state: 'foreign_daemon', runtime: { state: 'ready' } });
    assert.equal(foreign.tone, 'warn');
    assert.match(foreign.text, /Another daemon answered/);

    const owned = broken({ state: 'stale', ownership_problem: 'home owned by another install', runtime: {} });
    assert.equal(owned.tone, 'error');
    assert.match(owned.text, /not managed from here/);

    const unknown = broken({ state: 'unreachable', last_error: 'connection refused', runtime: {} });
    assert.equal(unknown.tone, 'error');
    assert.match(unknown.text, /connection refused/);

    // The IDLE daemon is not a fault — it keeps the calm muted tone — but its
    // own line is the MORE informative one, so it speaks instead of the generic
    // note. This is what makes the first-run sentence ("No accounts connected
    // yet. Connect installs Claudexor…") reachable at all: every stopped state
    // leaves all three facets unread, so while only warn/error could win, the
    // sentence written for a fresh install could never be printed.
    const idle = broken({ state: 'stale', runtime: { state: 'ready', version: '3.3.13' } });
    assert.equal(idle.tone, 'muted');
    assert.match(idle.text, /agent daemon is not running/);
    const firstRun = broken({ state: 'not_provisioned', runtime: {} });
    assert.equal(firstRun.tone, 'muted');
    assert.match(firstRun.text, /No accounts connected yet/);

    // A read that FAILED is itself a report, not a reassurance: it survives a
    // broken runtime rather than being replaced by it.
    const refused = serviceBannerLine(fakeStore(ALL('failed'), {
        snapshot: { daemon: { state: 'stale', runtime: { state: 'error' } } },
    }));
    assert.equal(refused.tone, 'warn');
    assert.match(refused.text, /could not be read/);
});

test('the REAL store, fed a corrupted runtime, reaches the repair sentence', async () => {
    // Not a hand-set reads map: the actual payload a broken install serves,
    // through the actual provenance mapping. This is what makes the case
    // REACHABLE rather than theoretical — the daemon is not serving, so every
    // facet honestly lands on "never asked", and the benign sentence used to be
    // the only thing on the tab while the buttons beside it said "Fix & connect".
    const body = {
        daemon: {
            state: 'stale',
            runtime: { state: 'error', last_error: 'engine checksum mismatch' },
        },
        harnesses: [], profiles: {}, quota: [],
    };
    const store = createClaudexorStatusStore({
        fetchImpl: async () => ({ ok: true, json: async () => body }),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
    });
    await store.refresh();
    assert.deepEqual(store.reads, { catalog: 'not_read', accounts: 'not_read', quota: 'not_read' });

    const line = serviceBannerLine(store);
    assert.equal(line.tone, 'error');
    assert.match(line.text, /Claudexor needs repair: engine checksum mismatch/);
    assert.doesNotMatch(line.text, /Nothing below is missing or wrong/);
    // The banner and the controls under it now tell the owner the same story.
    assert.equal(rowActionLabel({ status: {} }, store.snapshot), 'Fix & connect');
    store.dispose();
});

test('a card does not contradict itself: the header is dated by the same read as its rows', () => {
    // The row badge stops claiming "Verified live" when the ACCOUNTS read never
    // landed. The header lozenge counts the very same rows, so it obeys the very
    // same provenance — otherwise one card says "Connected" in green over rows
    // that each say "last known", and the owner has to decide which half to
    // believe. An account that needs ATTENTION keeps its tone: a dated warning
    // is still a warning, and muting it would hide the one row worth acting on.
    const live = [{ harness: 'codex', profile_id: 'work', kind: 'profile',
        status: { verification: 'passed', verification_source: 'vendor' } }];
    const broken = [{ harness: 'codex', profile_id: 'work', kind: 'profile',
        status: { verification: 'failed' } }];

    assert.deepEqual(familyStatus(live, { accountsRead: 'ok' }), { tone: 'ok', label: 'Connected' });
    for (const gap of ['not_read', 'failed', 'transport']) {
        const dated = familyStatus(live, { accountsRead: gap });
        assert.equal(dated.tone, 'muted', `${gap} must not paint a green aggregate`);
        assert.match(dated.label, /Connected — last known/);
        assert.equal(familyStatus(broken, { accountsRead: gap }).tone, 'error',
            `${gap} must not mute an account that needs attention`);
    }
    // Two rows, one signed in: the count is still honest, just dated.
    assert.match(familyStatus([...live, { harness: 'codex', profile_id: 'cold', kind: 'profile', status: {} }],
        { accountsRead: 'failed' }).label, /1 of 2 connected — last known/);
});

test('an UNREACHABLE daemon reaches the banner as ONE coarse verdict, carrying its own reason', async () => {
    // Where the two fixes meet. The store stopped reading `unreachable` as a
    // stopped daemon (it is what the endpoint answers when a RUNNING daemon
    // refused ONE of its fanned-out reads), and the tab's banner must carry
    // that through: the honest sentence, the error-toned service line it
    // outranks, and the daemon's OWN last_error — which on this payload is the
    // only explanation of the refusal there is. A banner that assembled the
    // sentence from the copy factory itself would drop it silently.
    //
    // SYNTHESIS: the store's second round narrowed what this payload licenses.
    // A legacy answer with no `reads` stamp does NOT say which read failed —
    // the probe against the live producer had the catalogue and the accounts
    // landing while only the quota refused — so three per-facet `failed`
    // verdicts would pin the quota's error on two reads that succeeded. The
    // banner therefore says ONE coarse thing about the whole answer. What this
    // test was written to protect is unchanged and still asserted below: the
    // daemon's reason survives to the pixels, and neither lie is printed.
    const store = createClaudexorStatusStore({
        fetchImpl: async () => ({
            ok: true,
            json: async () => ({
                daemon: { state: 'unreachable', last_error: 'quota_read_failed: window read died' },
                harnesses: [{ id: 'codex' }], profiles: {}, quota: [],
            }),
        }),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
    });
    await store.refresh();
    assert.deepEqual(store.reads,
        { catalog: 'indeterminate', accounts: 'indeterminate', quota: 'indeterminate' });

    const line = serviceBannerLine(store);
    assert.match(line.text, /did not finish answering/);
    assert.match(line.text, /window read died/, "the daemon's own reason survives to the pixels");
    // Neither of the two lies: not "nobody asked" (the reads were made), and
    // not the green lifecycle line over lists that never arrived.
    assert.doesNotMatch(line.text, /was not asked/);
    assert.doesNotMatch(line.text, /Claudexor ready/);
    // …and no facet is accused: the catalogue read is right there in the
    // payload, so naming it (or the accounts) would be the misattribution.
    assert.doesNotMatch(line.text, /agent accounts could not be read/);
    assert.doesNotMatch(line.text, /Your agents could not be read/);
    assert.equal(line.tone, 'warn');
    store.dispose();
});

test('the first read in flight states its COST, not a bare "reading…"', () => {
    // The daemon re-probes every agent CLI on each read, so first paint is tens
    // of seconds. An unexplained silent panel reads as broken, not as loading
    // (owner report, 2026-08-08) — a per-facet "Reading your agents…" would
    // have thrown that sentence away.
    const store = {
        reads: ALL('unread'), facet: (n) => ALL('unread')[n], error: '',
        snapshot: null, loading: true, everSettled: false,
        unavailableNote: (facet, { subject = '' } = {}) => statusUnavailableNote('unread', { facet, subject }),
    };
    const line = serviceBannerLine(store);
    assert.match(line.text, /Checking Claudexor/);
    assert.match(line.text, /minute/);
    // The per-facet sentence the banner deliberately does NOT use here is the
    // real one, so this stays a choice between two live sentences rather than
    // between the product's and a stub's.
    assert.match(store.unavailableNote('catalog').text, /Reading your agents…/);
    assert.doesNotMatch(line.text, /Reading your/);
});

test('one refused facet never withdraws the authority of the other two', () => {
    // PER FACET, never a global verdict: with the catalogue and accounts read,
    // a quota refusal must not read as "the service is down". The facet is
    // named in the PRODUCT's words — "subscription limits", the copy the store
    // owns — not in a word this test invented for it.
    const line = serviceBannerLine(fakeStore({ catalog: 'ok', accounts: 'ok', quota: 'failed' }));
    assert.equal(line.tone, 'warn');
    assert.match(line.text, /Your subscription limits could not be read/);
    assert.match(line.text, /Your agents and agent accounts were read normally/);
});

test('a partial gap names EVERY facet it lost, and protects only what it kept', () => {
    // "Everything else on this tab was read normally" was written for one
    // failure and applied to any number of them: with two facets down it told
    // the owner one had failed and the other two were fine. Latent only until
    // the backend stamps `reads` per facet — which is precisely the change that
    // makes a mixed verdict possible.
    const two = serviceBannerLine(fakeStore({ catalog: 'ok', accounts: 'failed', quota: 'failed' }));
    assert.match(two.text, /agent accounts and subscription limits/);
    assert.match(two.text, /Your agents were read normally/);
    assert.doesNotMatch(two.text, /Everything else/);

    // DIFFERENT failures are different sentences, and the worst tone wins.
    const mixed = serviceBannerLine(fakeStore(
        { catalog: 'ok', accounts: 'transport', quota: 'not_read' },
        { error: 'HTTP 503' }));
    assert.equal(mixed.tone, 'error');
    assert.match(mixed.text, /Could not read your agent accounts \(HTTP 503\)/);
    assert.match(mixed.text, /your subscription limits were never checked/);
    assert.match(mixed.text, /Your agents were read normally/);

    // Nothing read OK at all: no reassurance is appended, because there is
    // nothing left to reassure about.
    const none = serviceBannerLine(fakeStore(
        { catalog: 'failed', accounts: 'failed', quota: 'not_read' }));
    assert.match(none.text, /agents and agent accounts/);
    assert.match(none.text, /your subscription limits were never checked/);
    assert.doesNotMatch(none.text, /were read normally/);
});

test('a partial gap obeys the SAME fault precedence as a total one', () => {
    // The full-gap and partial-gap branches are one decision, and fixing only
    // one half is how this class survives to the next review. The backend
    // stamps `reads` per facet on every answer, so a mixed verdict is an
    // ordinary state — this is exactly the shape that produces one, at which point a muted "these were
    // never asked · the rest read normally" would quietly swallow a runtime that
    // needs repair.
    const broken = { daemon: { state: 'stale', runtime: { state: 'error', last_error: 'checksum' } } };

    const muted = serviceBannerLine(fakeStore(
        { catalog: 'ok', accounts: 'not_read', quota: 'not_read' }, { snapshot: broken }));
    assert.equal(muted.tone, 'error');
    assert.match(muted.text, /needs repair: checksum/);

    // A partial gap that is itself a REPORT still outranks the fault — a
    // refused read is not a reassurance and must not be replaced by one.
    const refused = serviceBannerLine(fakeStore(
        { catalog: 'ok', accounts: 'failed', quota: 'not_read' }, { snapshot: broken }));
    assert.equal(refused.tone, 'warn');
    assert.match(refused.text, /Your agent accounts could not be read/);
    assert.match(refused.text, /Your agents were read normally/);

    // A healthy daemon leaves the partial sentence exactly as it was.
    const healthy = serviceBannerLine(fakeStore(
        { catalog: 'ok', accounts: 'not_read', quota: 'not_read' }));
    assert.equal(healthy.tone, 'muted');
    assert.match(healthy.text, /agent accounts and subscription limits were never checked/);
});

// ---------------------------------------------------------------------------
// Removal.
// ---------------------------------------------------------------------------

test('removing a named account goes through the engine contract, and says so', () => {
    const calls = [];
    const fetchImpl = async (url, init) => {
        calls.push([url, init.method]);
        return { ok: true, json: async () => ({ ok: true }) };
    };
    return removeAccount('codex', 'work', { fetchImpl }).then((answer) => {
        assert.deepEqual(answer, { ok: true });
        assert.deepEqual(calls, [['/api/claudexor/credential-profiles/codex/work', 'DELETE']]);
        // The confirmation states the two facts an owner needs before agreeing:
        // nothing is deleted vendor-side, and a pinned reviewer row survives.
        const body = removeAccountConfirmBody('work', 'Codex');
        assert.match(body, /Ouroboros deletes nothing on the Codex side/);
        assert.match(body, /Reviewer rows pinned to this account stay visible/);
    });
});

test('a refused removal is reported as a refusal, never as a removal', () => {
    const fetchImpl = async () => ({ ok: false, status: 409, json: async () => ({ error: 'in use' }) });
    return assert.rejects(() => removeAccount('codex', 'work', { fetchImpl }), /in use/);
});

test('a review row pinned to a removed account stays visible with ONE warning', () => {
    // The row must not silently reroute to automatic rotation: that would widen
    // which account the reviewer may spend without the owner deciding it.
    const state = {
        triad: [{ slot_id: 't1', route: { kind: 'agent_session', target_id: 'codex', profile_id: 'work' } }],
        scope: [{ slot_id: 's1', route: { kind: 'agent_session', target_id: 'codex', profile_id: 'koshak' } }],
        advisory: { route: { kind: 'agent_session', target_id: 'claude', profile_id: 'main' } },
        profilesByHarness: { codex: ['koshak'], claude: ['main'] },
        accountsKnown: true,
    };
    const warning = pinnedAccountWarning(state);
    assert.match(warning, /A review row is pinned/);
    assert.match(warning, /codex · work/);
    assert.doesNotMatch(warning, /koshak/);   // still discovered
    assert.doesNotMatch(warning, /main/);     // still discovered
    assert.match(warning, /refuse rather than reroute/);

    // Every pin present: nothing to say.
    assert.equal(pinnedAccountWarning({ ...state, profilesByHarness: {
        codex: ['work', 'koshak'], claude: ['main'] } }), '');
    // Accounts never read: the pin only LOOKS missing, and the tab's banner is
    // already saying nobody could be asked (BIBLE P1).
    assert.equal(pinnedAccountWarning({ ...state, accountsKnown: false }), '');
    // Two missing pins count as two, in one sentence.
    assert.match(pinnedAccountWarning({ ...state, profilesByHarness: { codex: ['koshak'] } }),
        /2 review rows are pinned/);
});

test('familyLabel prefers live discovery and falls back to the product name', () => {
    assert.equal(familyLabel('claude', payload()), 'Claude Code');
    assert.equal(familyLabel('claude', payload({ harnesses: [{ id: 'claude', display_name: 'Claude Code CLI' }] })),
        'Claude Code CLI');
    // An unknown harness is named by its own id rather than invented.
    assert.equal(familyLabel('mystery', payload()), 'mystery');
});
