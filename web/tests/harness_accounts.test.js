import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import {
    accountRows,
    deviceCodeDisclosure,
    jobStateSummary,
    loginCardFace,
    quotaSummary,
    verificationBadge,
} from '../modules/harness_accounts.js';

// GOLDEN fixture: the real /v2/credential-profiles body, produced by PARSING a
// sample through Claudexor's own Zod ControlCredentialProfilesResponse schema
// (packages/schema/src/credential-profile.ts) — not a hand-written flat map.
// If the upstream shape drifts, regenerate this file from the schema; the JS
// must consume whatever the schema emits.
const CREDENTIAL_PROFILES_RESPONSE = JSON.parse(readFileSync(
    fileURLToPath(new URL('./fixtures/credential_profiles_response.json', import.meta.url)),
    'utf-8',
));

test('both verification statuses are honest: vendor is trusted, local is labeled unverified', () => {
    // Q2-а: the local status has lied before (verification: passed a minute
    // before a 401), so it must never render as trusted.
    const vendor = verificationBadge({ status: {
        verification: 'passed', verification_source: 'vendor', last_verified_at: '2026-08-03T10:00:00Z',
    } });
    assert.equal(vendor.tone, 'ok');
    assert.ok(vendor.label.startsWith('verified live'));

    const local = verificationBadge({ status: { verification: 'passed', verification_source: 'local_store' } });
    assert.equal(local.tone, 'warn');
    assert.equal(local.label, 'logged in locally — not verified');

    assert.equal(verificationBadge({ status: {} }).label, 'not logged in');
    assert.equal(verificationBadge({ status: { verification: 'failed', verification_source: 'vendor' } }).tone, 'error');
});

test('an exhausted window is shown with its reset time, never hidden', () => {
    const snapshots = [{
        subject: { harness: 'codex', subject_id: 'koshak' },
        constraints: [{ used_ratio: 1.0, resets_at: '2026-08-04T00:00:00Z' }],
    }];
    const summary = quotaSummary(snapshots, 'codex', 'koshak');
    assert.equal(summary.exhausted, true);
    assert.equal(summary.resetsAt, '2026-08-04T00:00:00Z');
    assert.ok(summary.label.includes('resets 2026-08-04T00:00:00Z'));

    const healthy = quotaSummary([{
        subject: { harness: 'codex' }, constraints: [{ used_ratio: 0.42 }],
    }], 'codex');
    assert.equal(healthy.exhausted, false);
    assert.equal(healthy.label, '42% of window used');
    assert.deepEqual(quotaSummary([], 'codex'), { label: '', exhausted: false, resetsAt: '' });
});

test('the device-code disclosure is found wherever the snapshot nests it', () => {
    const job = { snapshot: { disclosures: { deviceCode: {
        flow: 'device_auth', verificationUrl: 'https://auth.example/device', userCode: 'ABCD-1234',
    } } } };
    assert.deepEqual(deviceCodeDisclosure(job), { url: 'https://auth.example/device', code: 'ABCD-1234' });
    assert.equal(deviceCodeDisclosure({ state: 'running' }), null);
    assert.equal(deviceCodeDisclosure(null), null);
});

test('job terminal states are the typed set, success is exactly succeeded', () => {
    assert.deepEqual(jobStateSummary({ state: 'succeeded' }),
        { state: 'succeeded', phase: '', terminal: true, succeeded: true });
    for (const bad of ['failed', 'cancelled', 'timed_out', 'not_supported', 'interrupted_unknown']) {
        const summary = jobStateSummary({ state: bad });
        assert.equal(summary.terminal, true, bad);
        assert.equal(summary.succeeded, false, bad);
    }
    assert.equal(jobStateSummary({ state: 'waiting_for_input', phase: 'awaiting_user' }).terminal, false);
});

test('account rows consume the REAL schema shape: array of {profile,status,identity} wrappers + harnessAccounts array', () => {
    // The status endpoint nests the daemon body under payload.profiles.
    const rows = accountRows({ profiles: CREDENTIAL_PROFILES_RESPONSE });
    assert.equal(rows.length, 2);  // one native pseudo-row + one registered profile

    const native = rows.find((row) => row.kind === 'native');
    assert.equal(native.harness, 'codex');  // read from harness_id (snake_case), not harnessId
    // A native login detected locally is still only local_store evidence.
    assert.equal(verificationBadge(native).label, 'logged in locally — not verified');

    const profile = rows.find((row) => row.kind === 'profile');
    // Read from the NESTED wrapper.profile.* snake_case fields, not a flat map.
    assert.equal(profile.harness, 'codex');
    assert.equal(profile.profile_id, 'koshak');
    assert.equal(profile.display_name, 'Koshak');
    assert.equal(profile.identity.email, 'koshak@example.com');
    // The vendor-verified status flows straight through from wrapper.status.
    assert.equal(verificationBadge(profile).tone, 'ok');
    assert.ok(verificationBadge(profile).label.startsWith('verified live'));
});

test('the invented flat camelCase shape yields NOTHING (guards against the regression)', () => {
    // The exact shape an earlier draft consumed — a flat map with camelCase
    // keys and harnessAccounts-as-object. The real schema never emits it, so
    // reading it must produce zero rows, not silently-empty harness fields.
    const rows = accountRows({ profiles: {
        harnessAccounts: { codex: { native_login_detected: true } },
        profiles: [{ harnessId: 'codex', profileId: 'backup' }],
    } });
    assert.equal(rows.length, 0);
});

test('DTO end-to-end: EMPTY and MULTI-ACCOUNT schema-parsed bodies', () => {
    // Both fixtures came through Claudexor's own Zod schema. Empty body:
    // zero rows, no invented natives, no crash.
    assert.deepEqual(accountRows({ profiles: { profiles: [], harnessAccounts: [] } }), []);
    assert.deepEqual(accountRows({ profiles: {} }), []);
    assert.deepEqual(accountRows({}), []);

    const MULTI = JSON.parse(readFileSync(
        fileURLToPath(new URL('./fixtures/credential_profiles_multi.json', import.meta.url)),
        'utf-8',
    ));
    const rows = accountRows({ profiles: MULTI });
    // 2 native pseudo-rows + 3 profiles, per harness.
    assert.equal(rows.length, 5);
    assert.deepEqual(rows.filter((r) => r.kind === 'profile').map((r) => `${r.harness}:${r.profile_id}`),
        ['codex:koshak', 'codex:backup', 'claude:main']);
    // Mixed verification renders each truth on its own row.
    const byId = Object.fromEntries(rows.filter((r) => r.kind === 'profile')
        .map((r) => [r.profile_id, verificationBadge(r)]));
    assert.equal(byId.koshak.tone, 'ok');                       // vendor-verified
    assert.equal(byId.backup.label, 'logged in locally — not verified');
    assert.equal(byId.main.tone, 'error');                      // vendor said failed
    // A claude native row with no login shows "not logged in", not a lie.
    const claudeNative = rows.find((r) => r.kind === 'native' && r.harness === 'claude');
    assert.equal(verificationBadge(claudeNative).label, 'not logged in');
});

test('the login card prefers a STRUCTURED disclosure over the copy-paste fallback', () => {
    // A sealed attach job with no structured facts yet: copy-paste face.
    assert.equal(loginCardFace({ mode: 'attach', attachCommand: 'CLAUDEXOR_CONFIG_DIR=/d claudexor setup attach j1', job: { state: 'waiting_for_input' } }), 'attach');
    // The SAME job once the engine (upstream extension) surfaces a structured
    // OAuth disclosure: the structural card wins — no terminal needed.
    assert.equal(loginCardFace({ mode: 'attach', attachCommand: 'cmd', job: {
        state: 'waiting_for_input',
        snapshot: { disclosures: { deviceCode: { flow: 'device_auth', verificationUrl: 'https://a/b', userCode: 'XY-12' } } },
    } }), 'device');
    // Errors outrank everything; nothing structured and no command = progress.
    assert.equal(loginCardFace({ error: 'nope', mode: 'attach', attachCommand: 'cmd', job: {} }), 'error');
    assert.equal(loginCardFace({ mode: 'device', job: { state: 'running' } }), 'progress');
    assert.equal(loginCardFace(null), 'none');
});
