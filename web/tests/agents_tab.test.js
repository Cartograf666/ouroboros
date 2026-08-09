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
    return {
        reads,
        facet: (name) => reads[name],
        error,
        snapshot: snapshot || { daemon: RUNNING },
        loading: false,
        everSettled: true,
        unavailableNote: (facet) => ({
            tone: 'warn', action: null,
            text: `The agent daemon did not answer for your ${facet}.`,
        }),
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
    const line = serviceBannerLine(fakeStore(ALL('not_read'), {
        snapshot: { daemon: { state: 'stale', runtime: { state: 'ready' } } },
    }));
    assert.match(line.text, /agents, accounts and limits/);
    assert.match(line.text, /agent daemon is not running/);
    // Healthy: the ordinary lifecycle sentence, unchanged.
    assert.match(serviceBannerLine(fakeStore(ALL('ok'))).text, /Claudexor ready/);
});

test('one refused facet never withdraws the authority of the other two', () => {
    // PER FACET, never a global verdict: with the catalogue and accounts read,
    // a quota refusal must not read as "the service is down".
    const line = serviceBannerLine(fakeStore({ catalog: 'ok', accounts: 'ok', quota: 'failed' }));
    assert.match(line.text, /quota/);
    assert.match(line.text, /Everything else on this tab was read normally/);
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
