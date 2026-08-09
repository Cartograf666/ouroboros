import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import {
    ATTACH_FALLBACK_MS,
    UNCONFIRMED_TEXT,
    accountLoginConfirmed,
    accountRows,
    attachFallbackDue,
    confirmLoginLive,
    daemonStatusLine,
    facetReadState,
    accountsKnown,
    unknownAccountsNote,
    refreshActionLabel,
    refreshActionKind,
    commitStatusPayload,
    daemonAnswered,
    READ_FACETS,
    initHarnessAccounts,
    wakeDaemon,
    refreshStatus,
    deviceCodeDisclosure,
    failureText,
    jobDetail,
    jobStateSummary,
    loginCardFace,
    loginCardHtml,
    loginInputSupport,
    loginStatusLine,
    loginVerdict,
    normalizeProfileName,
    pollResponseApplies,
    preserveCardFocus,
    promptProfileName,
    quotaSummary,
    runtimeActionLabel,
    submitLoginInput,
    verificationBadge,
    cancelLoginJob,
    loginSettleProven,
} from '../modules/harness_accounts.js';

test('managed runtime keeps one contextual Connect intent across install, repair, and update', () => {
    const payload = (runtime, daemon = {}) => ({ daemon: { state: 'not_provisioned', runtime, ...daemon } });

    // The owner-locked dictionary is exactly four labels, independent of the
    // connected state: Connect | Install & connect | Update & connect | Fix & connect.
    assert.equal(runtimeActionLabel(payload({ state: 'missing' })), 'Install & connect');
    assert.equal(runtimeActionLabel(payload({ state: 'error' })), 'Fix & connect');
    assert.equal(runtimeActionLabel(payload({ state: 'update_available' })), 'Update & connect');
    assert.equal(runtimeActionLabel(payload({ state: 'ready' })), 'Connect');

    assert.ok(daemonStatusLine(payload({ state: 'missing' })).text.includes('installs Claudexor'));
    assert.ok(daemonStatusLine(payload({ state: 'ready', version: '3.3.7' })).text.includes('3.3.7 is ready'));
    assert.ok(daemonStatusLine(payload({ state: 'installing', target_version: '3.3.7' })).text.includes('Claudexor 3.3.7'));
    const staged = daemonStatusLine(payload(
        { state: 'update_staged', staged_version: '3.3.7' },
        { state: 'running', engine_version: '3.2.1' },
    ));
    assert.equal(staged.tone, 'warn');
    assert.ok(staged.text.includes('3.3.7 is ready'));
    assert.ok(staged.text.includes('Engine 3.2.1 keeps running'));
    const repair = daemonStatusLine(payload({ state: 'error', last_error: 'checksum mismatch' }));
    assert.equal(repair.tone, 'error');
    assert.ok(repair.text.includes('Connect retries automatically'));
});

test('a slow first read says it is checking, and an idle daemon is not dressed as a fault', () => {
    // Owner report (2026-08-08): the panel sat silent for tens of seconds and then
    // showed a WARN line about the daemon "not answering" — indistinguishable from
    // breakage. Both faces are pinned: the in-flight first read announces itself
    // with its real cost, and the ordinary idle daemon reads as installed-and-lazy.
    const checking = daemonStatusLine({}, { checking: true });
    assert.equal(checking.tone, 'muted');
    assert.ok(checking.text.includes('Checking Claudexor'));
    assert.ok(/minute/.test(checking.text), 'the honest cost of the first read is stated');

    // Once ANY daemon state is known the checking line steps aside — a stable
    // line beats a flicker on every 5s poll.
    const known = daemonStatusLine(
        { daemon: { state: 'running', engine_version: '3.3.13', runtime: {} } },
        { checking: true },
    );
    assert.equal(known.tone, 'ok');
    assert.ok(known.text.includes('3.3.13'));

    const idle = daemonStatusLine({ daemon: { state: 'stale', runtime: { state: 'ready', version: '3.3.13' } } });
    assert.equal(idle.tone, 'muted', 'a lazy daemon is not a warning');
    assert.ok(idle.text.includes('3.3.13 is installed'));
    assert.ok(/starts automatically/.test(idle.text), 'the line says what happens next');
    assert.ok(!/not answering/.test(idle.text), 'no fault language for the ordinary idle state');
});

test('the login card explains foreground runtime preparation and retries the same intent', () => {
    const preparing = loginCardHtml({
        harness: 'claude', profile: '', job: null, preparingRuntime: true,
        error: '', verdict: null, confirming: false,
    });
    assert.ok(preparing.includes('Installing or checking Claudexor…'));
    assert.ok(!preparing.includes('data-login-retry'));

    const failed = loginCardHtml({
        harness: 'claude', profile: '', job: null, preparingRuntime: false,
        error: 'checksum mismatch', verdict: null, confirming: false,
    });
    assert.ok(failed.includes('checksum mismatch'));
    assert.ok(failed.includes('data-login-retry'));
    assert.ok(!failed.includes('Installing or checking Claudexor…'));
});

// GOLDEN fixture: the real /v2/credential-profiles body, produced by PARSING a
// sample through Claudexor's own Zod ControlCredentialProfilesResponse schema
// (packages/schema/src/credential-profile.ts) — not a hand-written flat map.
// If the upstream shape drifts, regenerate this file from the schema; the JS
// must consume whatever the schema emits.
const CREDENTIAL_PROFILES_RESPONSE = JSON.parse(readFileSync(
    fileURLToPath(new URL('./fixtures/credential_profiles_response.json', import.meta.url)),
    'utf-8',
));

test('both verification statuses are honest: vendor is trusted, local is neutral, never a permanent alarm', () => {
    // Q2-а: the local status has lied before (verification: passed a minute
    // before a 401), so it must never render as trusted. Finding #2: some
    // harnesses (cursor) have NO vendor probe in the engine, so a warn-toned
    // "not verified" there is an alarm nothing can ever clear — the local
    // state stays labeled unverified in WORDS, in a neutral tone.
    const vendor = verificationBadge({ status: {
        verification: 'passed', verification_source: 'vendor', last_verified_at: '2026-08-03T10:00:00Z',
    } });
    assert.equal(vendor.tone, 'ok');
    assert.ok(vendor.label.startsWith('verified live'));

    const local = verificationBadge({ status: { verification: 'passed', verification_source: 'local_store' } });
    assert.equal(local.tone, 'muted');
    assert.equal(local.label, 'local session — not verified live');

    assert.equal(verificationBadge({ status: {} }).label, 'not logged in');
    assert.equal(verificationBadge({ status: { verification: 'failed', verification_source: 'vendor' } }).tone, 'error');
});

// `freshness` is a REQUIRED member of the daemon's quota snapshot
// (@claudexor/schema quota.ts, `z.enum(['fresh','stale','unknown'])`), so every
// fixture here carries it exactly as the wire does.
test('an exhausted window is shown with its reset time, never hidden', () => {
    const snapshots = [{
        subject: { harness: 'codex', subject_id: 'koshak' }, freshness: 'fresh',
        constraints: [{ used_ratio: 1.0, resets_at: '2026-08-04T00:00:00Z' }],
    }];
    const summary = quotaSummary(snapshots, 'codex', 'koshak');
    assert.equal(summary.exhausted, true);
    assert.equal(summary.resetsAt, '2026-08-04T00:00:00Z');
    assert.ok(summary.label.includes('resets 2026-08-04T00:00:00Z'));

    const healthy = quotaSummary([{
        subject: { harness: 'codex' }, freshness: 'fresh', constraints: [{ used_ratio: 0.42 }],
    }], 'codex');
    assert.equal(healthy.exhausted, false);
    assert.equal(healthy.label, '42% of window used');
    assert.deepEqual(quotaSummary([], 'codex'), { label: '', exhausted: false, resetsAt: '' });
});

test('the card reads a window on the same bar the runtime dispatches on', () => {
    // Two ways the card and the runtime disagreed about the SAME snapshot.
    //
    // 1. STALENESS. `harness_window_wait_hint` skips any snapshot that is not
    //    `fresh` ("an old reading must not block a lane"), so a stale spent window
    //    still dispatches — while the card painted it red and named a reset time,
    //    telling the owner a lane was down that was in fact serving.
    const stale = [{
        subject: { harness: 'codex', subject_id: 'koshak' }, freshness: 'stale',
        constraints: [{ used_ratio: 1.0, resets_at: '2026-08-04T00:00:00Z' }],
    }];
    assert.deepEqual(quotaSummary(stale, 'codex', 'koshak'),
        { label: '', exhausted: false, resetsAt: '' });
    assert.equal(quotaSummary([{ ...stale[0], freshness: 'unknown' }], 'codex', 'koshak').exhausted, false);
    assert.equal(quotaSummary([{ ...stale[0], freshness: 'fresh' }], 'codex', 'koshak').exhausted, true);

    // 2. WHICH CONSTRAINT. The runtime spends a profile when ANY of its constraints
    //    is cooling down or full; the card read exhaustion off the single highest
    //    used_ratio, so a cooling 5-hour window hid behind a busier weekly one...
    const cooling = [{
        subject: { harness: 'codex', subject_id: 'koshak' }, freshness: 'fresh',
        constraints: [
            { used_ratio: 0.20, cooldown_until: '2026-08-04T00:00:00Z' },
            { used_ratio: 0.80 },
        ],
    }];
    const summary = quotaSummary(cooling, 'codex', 'koshak');
    assert.equal(summary.exhausted, true);
    assert.equal(summary.resetsAt, '2026-08-04T00:00:00Z');

    // ...and vanished entirely when the cooling constraint reported no ratio at all,
    // because a non-finite used_ratio was skipped before it could be read.
    const ratioless = [{
        subject: { harness: 'codex', subject_id: 'koshak' }, freshness: 'fresh',
        constraints: [{ cooldown_until: '2026-08-04T00:00:00Z' }],
    }];
    assert.equal(quotaSummary(ratioless, 'codex', 'koshak').exhausted, true);
});

test('a named profile\'s exhausted window is never reported as the default account\'s', () => {
    // The daemon stamps the DEFAULT subject with subject_id null and scopes every
    // cooldown to its own subject ("a profiled limit must never cool the default
    // subject down"). The row that names ONE account has to honour that: the old
    // `!subjectId ||` wildcard made the default row match every subject on the
    // harness and paint itself red off someone else's spent window.
    const snapshots = [
        { subject: { harness: 'codex', subject_id: null }, freshness: 'fresh',
          constraints: [{ used_ratio: 0.05 }] },
        { subject: { harness: 'codex', subject_id: 'koshak' }, freshness: 'fresh',
          constraints: [{ used_ratio: 1.0, resets_at: '2026-08-04T00:00:00Z' }] },
    ];
    const defaultRow = quotaSummary(snapshots, 'codex', '');
    assert.equal(defaultRow.exhausted, false);
    assert.equal(defaultRow.label, '5% of window used');
    const namedRow = quotaSummary(snapshots, 'codex', 'koshak');
    assert.equal(namedRow.exhausted, true);
    assert.equal(namedRow.resetsAt, '2026-08-04T00:00:00Z');
});

test('a model-scoped window never paints the whole account exhausted — it is a compact note', () => {
    // The daemon schema's own words (@claudexor/schema quota.ts): a non-null
    // applies_to_models is a per-model cap, and "a model-specific cap never
    // cools a different model on the same subject". Painting the whole account
    // "window exhausted" off one is the same class of misreport as the
    // wildcard-subject bug above — a block reported that will not happen.
    const subject = { harness: 'claude', subject_id: 'abstractdl' };
    const mixed = quotaSummary([{
        subject, freshness: 'fresh',
        constraints: [
            { id: 'fable-window', label: 'Fable window', applies_to_models: ['claude-fable-5'],
              used_ratio: 1.0, resets_at: '2026-08-08T00:00:00Z' },
            { applies_to_models: null, used_ratio: 0.4 },
        ],
    }], 'claude', 'abstractdl');
    assert.equal(mixed.exhausted, false);
    // The account bar stays the GLOBAL window's; the spent scope is still said.
    assert.equal(mixed.label, '40% of window used · Fable window spent');

    // Scoped-only spent (cooldown, no ratio): the note IS the label, no red.
    const scopedOnly = quotaSummary([{
        subject, freshness: 'fresh',
        constraints: [{ id: 'fable-window', label: 'Fable window',
            applies_to_models: ['claude-fable-5'], cooldown_until: '2026-08-08T00:00:00Z', used_ratio: null }],
    }], 'claude', 'abstractdl');
    assert.equal(scopedOnly.exhausted, false);
    assert.equal(scopedOnly.label, 'Fable window spent');

    // A scoped window that is merely busy says nothing at account level.
    assert.deepEqual(quotaSummary([{
        subject, freshness: 'fresh',
        constraints: [{ label: 'Fable window', applies_to_models: ['claude-fable-5'], used_ratio: 0.8 }],
    }], 'claude', 'abstractdl'), { label: '', exhausted: false, resetsAt: '' });

    // A GLOBAL window (applies_to_models null/omitted = every model) keeps the
    // account-level exhausted behavior exactly as before.
    const global = quotaSummary([{
        subject, freshness: 'fresh',
        constraints: [{ applies_to_models: null, used_ratio: 1.0, resets_at: '2026-08-08T00:00:00Z' }],
    }], 'claude', 'abstractdl');
    assert.equal(global.exhausted, true);
    assert.ok(global.label.startsWith('window exhausted'));

    // Without a label, the note falls back to the constraint id, then models.
    assert.equal(quotaSummary([{
        subject, freshness: 'fresh',
        constraints: [{ id: 'fable_5h', applies_to_models: ['claude-fable-5'], used_ratio: 1.0 }],
    }], 'claude', 'abstractdl').label, 'fable_5h spent');
});

// ---------------------------------------------------------------------------
// Add account: pywebview's WKWebView implements no window.prompt (it answers
// null silently), so the flow runs on the in-house input dialog.
// ---------------------------------------------------------------------------

test('Add account never touches window.prompt and asks through the in-house dialog', async () => {
    // REGRESSION guard for the dead desktop button: the module must not call
    // window.prompt at all — under pywebview it is a silent no-op. (The call
    // form, so a comment may still name the hazard.)
    const source = readFileSync(new URL('../modules/harness_accounts.js', import.meta.url), 'utf8');
    assert.ok(!/window\s*\.\s*prompt\s*\(/.test(source));
    assert.ok(source.includes("from './confirm_dialog.js'"));

    // An already-valid name asks exactly once, for TEXT input.
    const calls = [];
    const name = await promptProfileName({ dialogImpl: async (options) => {
        calls.push(options);
        return { confirmed: true, value: 'backup' };
    } });
    assert.equal(name, 'backup');
    assert.equal(calls.length, 1);
    assert.equal(calls[0].input, true);
    // The alphabet is stated up front, so normalization is never a surprise.
    assert.ok(calls[0].body.includes('anything else becomes "-"'));

    // Cancel, and a name that normalizes to nothing, are quiet no-ops.
    assert.equal(await promptProfileName({ dialogImpl: async () => ({ confirmed: false, value: 'x' }) }), '');
    assert.equal(await promptProfileName({ dialogImpl: async () => ({ confirmed: true, value: '   ' }) }), '');
});

test('a name normalization would change is shown back, editable, BEFORE any login starts', async () => {
    // The owner types "Работа": the profile alphabet turns that into "------",
    // and starting a login under that name silently is exactly the trap the
    // prompt() flow had. The dialog re-opens with the normalized name visible
    // AND editable; only an explicit confirm of a stable name proceeds.
    const rounds = [];
    const answers = [
        { confirmed: true, value: 'Работа' },
        { confirmed: true, value: 'work-2' },
    ];
    const name = await promptProfileName({ dialogImpl: async (options) => {
        rounds.push(options);
        return answers[rounds.length - 1];
    } });
    assert.equal(name, 'work-2');
    assert.equal(rounds.length, 2);
    assert.ok(rounds[1].body.includes('"Работа" will be saved as "------"'));
    assert.equal(rounds[1].initialValue, '------');

    // Accepting the shown normalized name as-is also works (one extra round).
    const folds = [];
    const folded = await promptProfileName({ dialogImpl: async (options) => {
        folds.push(options);
        return { confirmed: true, value: folds.length === 1 ? 'Work' : options.initialValue };
    } });
    assert.equal(folded, 'work');
    assert.equal(folds.length, 2);
    assert.equal(folds[1].initialValue, 'work');

    // The normalization itself, pinned.
    assert.equal(normalizeProfileName(' Work '), 'work');
    assert.equal(normalizeProfileName('Работа'), '------');
    assert.equal(normalizeProfileName('a b/c'), 'a-b-c');
    assert.equal(normalizeProfileName('ok_name-1'), 'ok_name-1');
    assert.equal(normalizeProfileName(''), '');
});

test('the device-code disclosure is found wherever the snapshot nests it', () => {
    const job = { snapshot: { disclosures: { deviceCode: {
        flow: 'chatgptDeviceCode', verificationUrl: 'https://auth.example/device', userCode: 'ABCD-1234',
    } } } };
    assert.deepEqual(deviceCodeDisclosure(job),
        { url: 'https://auth.example/device', code: 'ABCD-1234', flow: 'chatgptDeviceCode' });
    assert.equal(deviceCodeDisclosure({ state: 'running' }), null);
    assert.equal(deviceCodeDisclosure(null), null);
});

test('a URL-ONLY disclosure renders: the flow discriminates, not the code field', () => {
    // Claudexor's SetupDeviceCodeDisclosure (packages/schema/src/setup.ts):
    // `userCode` is EMPTY for the browser-callback (`chatgpt`) and `oauth_url`
    // flows — the latter is the sign-in link a TERMINAL-mode claude/cursor login
    // prints. Requiring both fields matched neither, so a published link showed
    // nothing at all; the login card is the whole point of D30's structural face.
    for (const flow of ['oauth_url', 'chatgpt', 'oauth_url_input']) {
        const job = { snapshot: { disclosures: { deviceCode: {
            flow, verificationUrl: 'https://claude.ai/oauth/authorize?x=1', userCode: '',
        } } } };
        assert.deepEqual(deviceCodeDisclosure(job),
            { url: 'https://claude.ai/oauth/authorize?x=1', code: '', flow }, flow);
        // …and the card must actually pick the structural face for it.
        assert.equal(loginCardFace({ mode: 'attach', attachCommand: 'cmd', job }), 'device', flow);
    }
    // A node carrying neither is still not a disclosure.
    assert.equal(deviceCodeDisclosure({ snapshot: { verificationUrl: 'https://a/b' } }), null);
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

test('the POLLED snapshot ENVELOPE is read, so the login poll can actually terminate', () => {
    // GET /v2/setup/jobs/:id/snapshot answers ControlSetupJobSnapshot —
    // {job, cursor, sequence, deviceCode?} — while POST /v2/setup/jobs answers a
    // bare ControlSetupJob. Reading only the top level saw a state on the create
    // response and NEVER on a poll, so the card's terminal banner never rendered
    // and the 3-second poll ran forever.
    const envelope = {
        job: { jobId: 'j1', state: 'succeeded', phase: 'completed' },
        cursor: 'c1', sequence: 7,
    };
    assert.deepEqual(jobStateSummary(envelope),
        { state: 'succeeded', phase: 'completed', terminal: true, succeeded: true });
    assert.equal(jobStateSummary({ job: { state: 'failed', phase: 'login' } }).terminal, true);
    assert.equal(jobStateSummary({ job: { state: 'waiting_for_input' } }).terminal, false);
    // The bare create-response job keeps working through the same reader.
    assert.equal(jobStateSummary({ state: 'cancelled' }).terminal, true);
});

test('account rows consume the REAL schema shape: array of {profile,status,identity} wrappers + harnessAccounts array', () => {
    // The status endpoint nests the daemon body under payload.profiles.
    const rows = accountRows({ profiles: CREDENTIAL_PROFILES_RESPONSE });
    assert.equal(rows.length, 2);  // one native pseudo-row + one registered profile

    const native = rows.find((row) => row.kind === 'native');
    assert.equal(native.harness, 'codex');  // read from harness_id (snake_case), not harnessId
    // A native login detected locally is still only local_store evidence.
    assert.equal(verificationBadge(native).label, 'local session — not verified live');

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
    assert.equal(byId.backup.label, 'local session — not verified live');
    assert.equal(byId.main.tone, 'error');                      // vendor said failed
    // A claude native row with no login shows "not logged in", not a lie.
    const claudeNative = rows.find((r) => r.kind === 'native' && r.harness === 'claude');
    assert.equal(verificationBadge(claudeNative).label, 'not logged in');
});

test('the attach command is DEMOTED: never a card face, only a due fallback', () => {
    // The owner rejected terminal-first login ("Via your terminal" buttons and
    // an attach-command card body). A job with a command but nothing
    // structured renders the WAITING face; the command surfaces only through
    // attachFallbackDue as a collapsed Advanced affordance.
    const attachOnly = { attachCommand: 'CLAUDEXOR_CONFIG_DIR=/d claudexor setup attach j1', startedAtMs: 1000, job: { state: 'waiting_for_input' } };
    assert.equal(loginCardFace(attachOnly), 'progress');
    // The SAME job once the engine surfaces a structured OAuth disclosure:
    // the structural card wins — no terminal needed.
    assert.equal(loginCardFace({ ...attachOnly, job: {
        state: 'waiting_for_input',
        snapshot: { disclosures: { deviceCode: { flow: 'chatgptDeviceCode', verificationUrl: 'https://a/b', userCode: 'XY-12' } } },
    } }), 'device');
    // Errors outrank everything; nothing at all = progress; no job = none.
    assert.equal(loginCardFace({ error: 'nope', attachCommand: 'cmd', job: {} }), 'error');
    assert.equal(loginCardFace({ job: { state: 'running' } }), 'progress');
    assert.equal(loginCardFace(null), 'none');
});

test('card shape 2 keys on the disclosure FLOW string — the typed enum decides, no harness branching', () => {
    // The engine's 3.3.7 FINAL contract: `oauth_url_input` is the disclosure
    // flow for a job that also accepts a pasted code (claude's
    // manual-callback path); `oauth_url`/`chatgpt` stay link-only. The enum
    // decides for ANY harness — no boolean sidecar, no name fallback.
    const withInput = { snapshot: { disclosures: { deviceCode: {
        flow: 'oauth_url_input', verificationUrl: 'https://platform.claude.com/oauth/authorize?x=1', userCode: '' } } } };
    assert.equal(loginInputSupport(withInput), true);
    // A URL-only disclosure: shape 1, no input — even when the harness that
    // produced it happens to be claude (the flow is the truth, not the name).
    for (const flow of ['oauth_url', 'chatgpt', 'chatgptDeviceCode']) {
        const job = { snapshot: { disclosures: { deviceCode: {
            flow, verificationUrl: 'https://cursor.com/loginDeepControl?x=1', userCode: '' } } } };
        assert.equal(loginInputSupport(job), false, flow);
    }
    // No disclosure at all: no input field.
    assert.equal(loginInputSupport({ state: 'running' }), false);
    assert.equal(loginInputSupport(null), false);
});

test('the verdict never contradicts the state, and never fails off a verification-race read', () => {
    // The owner's live finding: a codex login SUCCEEDED while the card said
    // "Login failed · completed" — the engine's post-login probe read the
    // auth store codex clears at login start. Verification-flavored failures
    // are 'recheck' (judged by live account status), not final failures.
    assert.equal(loginVerdict({ job: { state: 'running', phase: 'awaiting_user' } }).kind, 'pending');
    assert.equal(loginVerdict({ job: { state: 'succeeded', phase: 'completed' } }).kind, 'success');
    for (const reason of ['capability_verification_failed', 'auth_not_ready']) {
        const verdict = loginVerdict({ job: { state: 'failed', phase: 'completed', outcome: { reason } } });
        assert.equal(verdict.kind, 'recheck', reason);
        assert.equal(verdict.reason, reason);
    }
    // A failure with NO typed reason is also unproven — recheck.
    assert.equal(loginVerdict({ job: { state: 'failed', phase: 'completed' } }).kind, 'recheck');
    // Genuine failures stay final, with their typed reason carried.
    const launch = loginVerdict({ job: { state: 'failed', outcome: { reason: 'launch_failed' } } });
    assert.deepEqual(launch, { kind: 'failure', reason: 'launch_failed' });
    assert.equal(loginVerdict({ job: { state: 'timed_out', outcome: { reason: 'timed_out' } } }).kind, 'failure');
    assert.equal(loginVerdict({ job: { state: 'cancelled', outcome: { reason: 'cancelled_by_user' } } }).kind, 'failure');
    // Wording: a real failure names its reason in words, no enum glue.
    assert.equal(failureText('launch_failed'), 'Sign-in failed — launch failed.');
});

test('the live state line renders plain words and NOTHING on a terminal job', () => {
    // "Login failed · completed" is structurally impossible: terminal jobs
    // render a verdict, and this line answers '' for them.
    assert.equal(loginStatusLine({ job: { state: 'failed', phase: 'completed' } }), '');
    assert.equal(loginStatusLine({ job: { state: 'succeeded', phase: 'completed' } }), '');
    assert.equal(loginStatusLine({ job: { state: 'queued', phase: 'preparing' } }), 'Starting the sign-in…');
    assert.equal(loginStatusLine({ job: { state: 'waiting_for_input', phase: 'launching' } }), 'Waiting for the sign-in link…');
    assert.equal(loginStatusLine({ job: { state: 'running', phase: 'verifying' } }), 'Checking the sign-in…');
    const disclosed = { job: { state: 'waiting_for_input', phase: 'awaiting_user' },
        snapshot: { disclosures: { deviceCode: { flow: 'oauth_url', verificationUrl: 'https://a/b', userCode: '' } } } };
    assert.equal(loginStatusLine(disclosed), 'Waiting for you to finish signing in in the browser…');
});

test('accountLoginConfirmed reads the exact harness+profile row from live status', () => {
    const payload = { profiles: {
        harnessAccounts: [
            { harness_id: 'codex', native_login_detected: true, identity: {} },
            { harness_id: 'claude', native_login_detected: false, identity: {} },
        ],
        profiles: [
            { profile: { harness_id: 'codex', profile_id: 'koshak' },
              status: { verification: 'passed', verification_source: 'vendor' }, identity: {} },
        ],
    } };
    // The default account (empty profile id) is confirmed by the daemon's own
    // local-store detection — the same evidence the row badge renders.
    assert.equal(accountLoginConfirmed(payload, 'codex', ''), true);
    assert.equal(accountLoginConfirmed(payload, 'claude', ''), false);
    // A named profile is judged by ITS row, never the native pseudo-row.
    assert.equal(accountLoginConfirmed(payload, 'codex', 'koshak'), true);
    assert.equal(accountLoginConfirmed(payload, 'codex', 'other'), false);
    assert.equal(accountLoginConfirmed({}, 'codex', ''), false);
});

function fakeResponse(status, body) {
    return { ok: status >= 200 && status < 300, status, json: async () => body };
}

test('submitLoginInput posts the code once and types the 404 capability gap (mock fetch)', async () => {
    const calls = [];
    const ok = await submitLoginInput('j 1', 'ABCD-1234', { fetchImpl: async (url, init) => {
        calls.push({ url, init });
        return fakeResponse(200, { ok: true, job: {} });
    } });
    assert.deepEqual(ok, { ok: true, degraded: false, conflict: '', error: '' });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, '/api/claudexor/login/j%201/input');
    assert.equal(calls[0].init.method, 'POST');
    assert.deepEqual(JSON.parse(calls[0].init.body), { value: 'ABCD-1234' });

    // DEGRADED-ENGINE PATH: the gateway's typed 404 (input_not_supported —
    // the engine predates the route or reaped the job) is `degraded`, so the
    // card falls back to Advanced instead of dead-ending on a raw error.
    const degraded = await submitLoginInput('j1', 'X', {
        fetchImpl: async () => fakeResponse(404, { error: 'input route not available', code: 'input_not_supported' }),
    });
    assert.equal(degraded.ok, false);
    assert.equal(degraded.degraded, true);
    // Any other failure is an ordinary error, NOT a capability degrade.
    const busy = await submitLoginInput('j1', 'X', { fetchImpl: async () => fakeResponse(503, { error: 'daemon down' }) });
    assert.deepEqual(busy, { ok: false, degraded: false, conflict: '', error: 'daemon down' });
    const dead = await submitLoginInput('j1', 'X', { fetchImpl: async () => { throw new Error('network gone'); } });
    assert.equal(dead.degraded, false);
    assert.ok(dead.error.includes('network gone'));
});

test('a 409 input conflict carries the engine code: the callback already completed', async () => {
    // Typed by the engine (final contract): setup_input_not_applicable means
    // the flow moved past the code step — e.g. claude's localhost callback
    // completed on its own. An ANSWER, not an error: the card shows a quiet
    // "no code needed" note and lets the job poll land the verdict.
    const result = await submitLoginInput('j1', 'ABCD', {
        fetchImpl: async () => fakeResponse(409, {
            error: 'input is not applicable to this flow/phase',
            code: 'setup_input_not_applicable',
        }),
    });
    assert.deepEqual(result, {
        ok: false, degraded: false, conflict: 'setup_input_not_applicable',
        error: 'input is not applicable to this flow/phase',
    });
});

test('a 409 repeat is typed too: the server is authoritative over the double-submit guard', async () => {
    // setup_input_already_submitted: our busy/sent guard prevents UI repeats,
    // but the server owns the truth (e.g. a second tab already sent a code).
    // The card treats it as already-sent, never as a failure.
    const result = await submitLoginInput('j1', 'ABCD', {
        fetchImpl: async () => fakeResponse(409, {
            error: 'a code was already submitted for this job',
            code: 'setup_input_already_submitted',
        }),
    });
    assert.equal(result.conflict, 'setup_input_already_submitted');
    assert.equal(result.degraded, false);
    assert.equal(result.ok, false);
    // A 409 with no code still classifies as a conflict, never a raw error.
    const untyped = await submitLoginInput('j1', 'ABCD', {
        fetchImpl: async () => fakeResponse(409, { error: 'conflict' }),
    });
    assert.equal(untyped.conflict, 'conflict');
});

test('confirmLoginLive re-polls live account status briefly instead of trusting one stale read', async () => {
    // First poll: the account still looks logged out (the stale window).
    // Second poll: the login shows up — confirmed, loop ends early.
    const cold = { profiles: { harnessAccounts: [{ harness_id: 'codex', native_login_detected: false }], profiles: [] } };
    const warm = { profiles: { harnessAccounts: [{ harness_id: 'codex', native_login_detected: true }], profiles: [] } };
    let polls = 0;
    const slept = [];
    const confirmed = await confirmLoginLive('codex', '', {
        fetchImpl: async () => fakeResponse(200, ++polls >= 2 ? warm : cold),
        attempts: 4, delayMs: 7, sleepImpl: async (ms) => { slept.push(ms); },
    });
    assert.equal(confirmed.confirmed, true);
    assert.equal(polls, 2);
    assert.deepEqual(slept, [7]);   // no sleep before the first poll
    assert.deepEqual(confirmed.payload, warm);

    // Still cold after every attempt: unconfirmed, with the last payload so
    // the caller can render the rows it actually saw.
    let coldPolls = 0;
    const unconfirmed = await confirmLoginLive('codex', '', {
        fetchImpl: async () => { coldPolls += 1; return fakeResponse(200, cold); },
        attempts: 3, delayMs: 1, sleepImpl: async () => {},
    });
    assert.equal(unconfirmed.confirmed, false);
    assert.equal(coldPolls, 3);   // bounded — it does not poll forever
    assert.deepEqual(unconfirmed.payload, cold);

    // A card closed mid-check aborts without a verdict.
    const stale = await confirmLoginLive('codex', '', {
        fetchImpl: async () => fakeResponse(200, cold),
        attempts: 3, delayMs: 1, sleepImpl: async () => {}, isStale: () => true,
    });
    assert.equal(stale.stale, true);
});

test('the Advanced fallback is due on a disclosure that never comes, or an engine that predates the modes', () => {
    const base = { attachCommand: 'CLAUDEXOR_CONFIG_DIR=/d claudexor setup attach j1', startedAtMs: 100000, engineDegraded: false, job: { state: 'waiting_for_input' } };
    // Inside the grace window: not due — the card just says it is waiting.
    assert.equal(attachFallbackDue(base, 100000 + ATTACH_FALLBACK_MS - 1), false);
    // Window elapsed with no disclosure: due.
    assert.equal(attachFallbackDue(base, 100000 + ATTACH_FALLBACK_MS), true);
    // An engine the create answer flagged as pre-disclosure: due immediately.
    assert.equal(attachFallbackDue({ ...base, engineDegraded: true }, 100001), true);
    // A rendered disclosure keeps the fallback hidden (link-first, always)…
    const disclosed = { ...base, job: { snapshot: { disclosures: { deviceCode: {
        flow: 'oauth_url', verificationUrl: 'https://a/b', userCode: '' } } } } };
    assert.equal(attachFallbackDue(disclosed, 100000 + ATTACH_FALLBACK_MS * 2), false);
    // …unless the engine is degraded (the input route 404'd mid-flow).
    assert.equal(attachFallbackDue({ ...disclosed, engineDegraded: true }, 100001), true);
    // No command = nothing to fall back to (the daemon-hosted codex flow).
    assert.equal(attachFallbackDue({ ...base, attachCommand: '' }, 100000 + ATTACH_FALLBACK_MS * 2), false);
    assert.equal(attachFallbackDue(null, 999999), false);
});

// ---------------------------------------------------------------------------
// Card rendering: the sign-in link is a PRIMARY click target, the verdict owns
// the card once it lands, and a re-check that ran out is not a failure.
// ---------------------------------------------------------------------------

function cardWithUrl(url, extra = {}) {
    return {
        harness: 'claude', profile: '', jobId: 'j1', attachCommand: '', startedAtMs: 0,
        job: { state: 'waiting_for_input', phase: 'awaiting_user',
            snapshot: { disclosures: { deviceCode: { flow: 'oauth_url', verificationUrl: url, userCode: '' } } } },
        ...extra,
    };
}

test('the disclosed sign-in URL is rendered only for http/https, through the house helper', () => {
    // The link is the card's primary action now — one click, engine-supplied
    // text. utils.safeExternalHrefAttr is the single house gate for that
    // (http/https only, escaped by the helper), and everything else must
    // render NO clickable link rather than a scheme the browser will execute.
    const safe = loginCardHtml(cardWithUrl('https://platform.claude.com/oauth/authorize?x=1&y=2'), 0);
    assert.ok(safe.includes('href="https://platform.claude.com/oauth/authorize?x=1&amp;y=2"'));
    assert.ok(safe.includes('data-open-signin'));
    assert.ok(loginCardHtml(cardWithUrl('http://127.0.0.1:1455/callback'), 0).includes('data-open-signin'));

    for (const hostile of [
        'javascript:alert(document.cookie)',
        'JavaScript:alert(1)',
        'data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==',
        'vbscript:msgbox(1)',
        'file:///etc/passwd',
        'not a url at all',
        '//evil.example/oauth',
    ]) {
        const html = loginCardHtml(cardWithUrl(hostile), 0);
        assert.ok(!html.includes('data-open-signin'), hostile);
        assert.ok(!html.includes('href='), hostile);
        assert.ok(html.includes('data-unsafe-signin-link'), hostile);
        // …and the raw scheme never reaches the DOM as an attribute value.
        assert.ok(!html.includes(hostile), hostile);
    }
});

test('a settled verdict silences the live status line, so the card never says both', () => {
    // The owner hit a card reading "Waiting for the sign-in link…" beside a
    // verdict: an overlapping poll tick applied a snapshot captured before the
    // job settled. Two guards, and this is the rendering half.
    const pending = cardWithUrl('https://a.example/b');
    assert.ok(loginCardHtml(pending, 0).includes('data-login-state'));

    const settled = { ...pending, verdict: { kind: 'success', reason: '' } };
    const html = loginCardHtml(settled, 0);
    assert.ok(!html.includes('data-login-state'));
    assert.ok(html.includes('Connected.'));
    // Same while the live re-check is deciding.
    assert.ok(!loginCardHtml({ ...pending, confirming: true }, 0).includes('data-login-state'));
});

test('an exhausted re-check says the sign-in is UNCONFIRMED, never that it failed', () => {
    // The row it waits for routinely lands a tick after the bounded re-poll
    // gives up, so a hard "Sign-in failed" there is a lie about a login that
    // may have succeeded. A genuine typed failure keeps its own wording.
    const unconfirmed = loginCardHtml(cardWithUrl('https://a.example/b', {
        verdict: { kind: 'unconfirmed', reason: 'auth_not_ready' } }), 0);
    assert.ok(unconfirmed.includes(UNCONFIRMED_TEXT));
    assert.ok(!unconfirmed.includes('Sign-in failed'));
    assert.ok(UNCONFIRMED_TEXT.includes('Refresh'));

    const failed = loginCardHtml(cardWithUrl('https://a.example/b', {
        verdict: { kind: 'failure', reason: 'launch_failed' } }), 0);
    assert.ok(failed.includes(failureText('launch_failed')));
    assert.ok(!failed.includes(UNCONFIRMED_TEXT));
});

test("a settled non-success verdict carries the engine's own explanation", () => {
    // The masking bug the owner hit: a codex login ended `auth_not_ready` and
    // the card showed only the fixed UNCONFIRMED_TEXT ("check the account row
    // above"), which reads as "wait a moment" — while the daemon had already
    // settled it terminally and said why. That sentence was in the snapshot
    // the card was holding and reached no reader; the two verdict texts are
    // fixed constants, so nothing else could ever carry it.
    const message = 'codex native session was not ready before the verification'
        + ' deadline: native Codex session is not logged in';
    // The POLL envelope NESTS the job, which is where the field really lands.
    const nested = cardWithUrl('https://a.example/b', {
        job: { job: { state: 'failed', phase: 'completed', message } },
        verdict: { kind: 'unconfirmed', reason: 'auth_not_ready' },
    });
    const unconfirmed = loginCardHtml(nested, 0);
    assert.ok(unconfirmed.includes('data-login-detail'));
    assert.ok(unconfirmed.includes(message));
    // The verdict wording itself is unchanged — this is additive.
    assert.ok(unconfirmed.includes(UNCONFIRMED_TEXT));

    // A typed failure gets it too: its reason is a category, not a sentence.
    assert.ok(loginCardHtml({ ...nested, verdict: { kind: 'failure', reason: 'launch_failed' } }, 0)
        .includes('data-login-detail'));

    // Never beside "Connected." (a stale message must not contradict success),
    // and never while the job is unsettled (the status line owns the card).
    assert.ok(!loginCardHtml({ ...nested, verdict: { kind: 'success', reason: '' } }, 0)
        .includes('data-login-detail'));
    assert.ok(!loginCardHtml({ ...nested, verdict: null }, 0).includes('data-login-detail'));
    assert.ok(!loginCardHtml({ ...nested, confirming: true, verdict: null }, 0)
        .includes('data-login-detail'));

    // Engine-supplied text is escaped like every other disclosure on this card.
    const hostile = loginCardHtml(cardWithUrl('https://a.example/b', {
        job: { job: { state: 'failed', message: '<img src=x onerror=alert(1)>' } },
        verdict: { kind: 'unconfirmed', reason: 'auth_not_ready' },
    }), 0);
    assert.ok(!hostile.includes('<img'));
    assert.ok(hostile.includes('&lt;img'));

    // jobDetail itself: both levels, trimmed, and total over junk.
    assert.equal(jobDetail({ message: '  hi  ' }), 'hi');
    assert.equal(jobDetail({ job: { message: 'deep' } }), 'deep');
    assert.equal(jobDetail({ message: '   ', job: { message: 'deep' } }), 'deep');
    assert.equal(jobDetail({ message: 42 }), '');
    assert.equal(jobDetail({}), '');
    assert.equal(jobDetail(null), '');
});

test("the engine explanation reaches the card from EITHER envelope level", () => {
    // The dual-level read is asserted on jobDetail() above, but the RENDER path
    // was only ever exercised with the POLL envelope ({job:{...}}). CREATE
    // answers a BARE ControlSetupJob, and the login card holds whichever of the
    // two last landed on it — `startLogin` writes `data.job` from the create
    // answer, and the poll tick overwrites it later. So a regression that
    // reached only one level would leave the other silently mute.
    const message = 'native Codex session is not logged in';
    const levels = {
        create_bare_job: { state: 'failed', phase: 'completed', message },
        poll_envelope: { job: { state: 'failed', phase: 'completed', message } },
    };
    for (const [label, job] of Object.entries(levels)) {
        const html = loginCardHtml(cardWithUrl('https://a.example/b', {
            job, verdict: { kind: 'unconfirmed', reason: 'auth_not_ready' },
        }), 0);
        assert.ok(html.includes('data-login-detail'), label);
        assert.ok(html.includes(message), label);
    }
    // Precedence when BOTH levels speak: the top level wins. Not a preference —
    // the poll writes the envelope it received, so the outer value is the fresher
    // reading of the two and must not be shadowed by a stale nested one.
    assert.equal(jobDetail({ message: 'outer', job: { message: 'inner' } }), 'outer');
});

test("the engine explanation is escaped in full and never truncated", () => {
    // Untrusted external text on an owner-facing surface, so two separate
    // properties. ESCAPING: the existing suite asserts `<img …>` only, while the
    // house helper escapes six characters — an unescaped `&` or quote is the same
    // class of defect one character over, and this line sits inside an element
    // whose attributes are built by the same interpolation.
    const hostile = `Tom & Jerry's "quoted" <b>bold</b> \`tick\``;
    const html = loginCardHtml(cardWithUrl('https://a.example/b', {
        job: { job: { state: 'failed', message: hostile } },
        verdict: { kind: 'failure', reason: 'launch_failed' },
    }), 0);
    for (const raw of ['&', '<', '>', '"', "'", '`']) {
        // Each hostile character reaches the DOM only in escaped form: the raw
        // one may still appear as HTML the card itself wrote (its own tags), so
        // the assertion is on the escaped entity being present…
        assert.ok(html.includes({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;',
        }[raw]), raw);
    }
    // …and on no fragment of the payload surviving as live markup.
    assert.ok(!html.includes('<b>bold</b>'));
    assert.ok(html.includes('&lt;b&gt;bold&lt;/b&gt;'));

    // NO TRUNCATION (BIBLE P1): this is the only place a settled login says WHY,
    // so a long engine sentence must arrive whole. The daemon's real ones already
    // chain a cause onto a summary; nothing bounds their length.
    const long = `${'the daemon explained at length: '.repeat(80)}end.`;
    const longHtml = loginCardHtml(cardWithUrl('https://a.example/b', {
        job: { job: { state: 'failed', message: long } },
        verdict: { kind: 'unconfirmed', reason: 'auth_not_ready' },
    }), 0);
    assert.ok(longHtml.includes(long));
    assert.ok(!longHtml.includes('…]'));   // no omission marker of any house shape
});

test('a settled failure with NO engine sentence renders the verdict alone', () => {
    // The absence path, in the render surface rather than only on jobDetail():
    // most settled jobs carry no `message` at all, so the common case must add
    // no empty element and — the specific hazard of interpolating an optional
    // field — no stringified `undefined`/`null` where a sentence would go.
    for (const job of [
        { job: { state: 'failed', phase: 'completed' } },          // absent
        { job: { state: 'failed', message: '' } },                 // empty
        { job: { state: 'failed', message: '   ' } },              // whitespace
        { job: { state: 'failed', message: null } },               // explicit null
    ]) {
        const html = loginCardHtml(cardWithUrl('https://a.example/b', {
            job, verdict: { kind: 'unconfirmed', reason: 'auth_not_ready' },
        }), 0);
        assert.ok(!html.includes('data-login-detail'), JSON.stringify(job));
        assert.ok(!html.includes('undefined'), JSON.stringify(job));
        assert.ok(!html.includes('null'), JSON.stringify(job));
        // The verdict itself is untouched by the missing detail.
        assert.ok(html.includes(UNCONFIRMED_TEXT), JSON.stringify(job));
    }
});

test('the verify-race incident, composed end to end: recheck runs out and the card still says why', async () => {
    // The owner's actual incident shape. Its three steps are each asserted
    // above in isolation, which is exactly how the defect survived: every part
    // worked and the composition still rendered only a fixed constant. This
    // walks the same steps the settle path walks, in order, on one job.
    //
    // (settleVerdict itself is not exported — it re-renders the live DOM — so
    // this composes the exported steps rather than executing that function. It
    // pins the CHAIN, not settleVerdict's own wiring; that remains untested.)
    const message = 'codex native session was not ready before the verification'
        + ' deadline: native Codex session is not logged in';
    const job = { job: { state: 'failed', phase: 'completed', message, outcome: { reason: 'auth_not_ready' } } };

    // 1. The job settled failed, but on a reason a verification race fabricates.
    const verdict = loginVerdict(job);
    assert.equal(verdict.kind, 'recheck');
    assert.equal(verdict.reason, 'auth_not_ready');

    // 2. The bounded live re-check never sees the row appear.
    const cold = { profiles: { harnessAccounts: [{ harness_id: 'codex', native_login_detected: false }], profiles: [] } };
    const check = await confirmLoginLive('codex', '', {
        fetchImpl: async () => fakeResponse(200, cold),
        attempts: 2, delayMs: 1, sleepImpl: async () => {},
    });
    assert.equal(check.confirmed, false);

    // 3. So the card takes the unconfirmed verdict — and BOTH halves land: the
    //    honest "unknown" wording AND the daemon's own sentence. Before the fix
    //    step 3 produced the constant alone, which reads as "wait a moment" for
    //    a job the daemon had already settled terminally.
    const html = loginCardHtml(cardWithUrl('https://a.example/b', {
        job, verdict: check.confirmed ? { kind: 'success', reason: '' } : { kind: 'unconfirmed', reason: verdict.reason },
    }), 0);
    assert.ok(html.includes(UNCONFIRMED_TEXT));
    assert.ok(html.includes(message));
    assert.ok(!html.includes('Sign-in failed'));
});

test('a poll answer applies only to the job it was captured for, and only while unsettled', () => {
    // The ordering rule behind the contradictory card: two overlapping async
    // ticks can land out of order, so an OLDER snapshot must never be written
    // over a job that has already settled — or onto a card that has since been
    // closed or reopened for another account.
    const active = { jobId: 'j1' };
    assert.equal(pollResponseApplies(active, active), true);
    assert.equal(pollResponseApplies(active, { jobId: 'j2' }), false);   // reopened
    assert.equal(pollResponseApplies(active, null), false);              // closed
    assert.equal(pollResponseApplies(null, null), false);
    assert.equal(pollResponseApplies({ ...active, verdict: { kind: 'success' } },
        active), false);
    const confirming = { jobId: 'j1', confirming: true };
    assert.equal(pollResponseApplies(confirming, confirming), false);
});

// ---------------------------------------------------------------------------
// The 3-second poll re-render must not eat the caret. Minimal element stubs
// (the repo's house idiom — no jsdom) plus node's fake timers, so the re-render
// cadence itself is what the assertion runs through.
// ---------------------------------------------------------------------------

function fakeCodeInput({ disabled = false, start = 3, end = 5 } = {}) {
    const calls = { focus: 0, range: null };
    return {
        disabled, value: 'ABCD-1234', selectionStart: start, selectionEnd: end,
        hasAttribute: (name) => name === 'data-login-code-input',
        focus() { calls.focus += 1; },
        setSelectionRange(from, to) { calls.range = [from, to]; },
        calls,
    };
}

function fakeCardHost(replacement, focused) {
    return {
        swaps: 0,
        contains: (node) => node === focused,
        querySelector: () => replacement,
    };
}

test('the paste-code field survives every poll re-render, caret and selection intact', (t) => {
    t.mock.timers.enable({ apis: ['setInterval'] });
    const typing = fakeCodeInput({ start: 3, end: 5 });
    const replacement = fakeCodeInput({ start: 0, end: 0 });
    const host = fakeCardHost(replacement, typing);
    const doc = { activeElement: typing };
    // Exactly what the job poll does: swap the card's DOM on every tick.
    setInterval(() => preserveCardFocus(host, () => { host.swaps += 1; }, doc), 3000);

    t.mock.timers.tick(3000);
    assert.equal(host.swaps, 1);
    assert.equal(replacement.calls.focus, 1);
    assert.deepEqual(replacement.calls.range, [3, 5]);
    t.mock.timers.tick(3000);
    assert.equal(host.swaps, 2);
    assert.equal(replacement.calls.focus, 2, 'every tick restores focus, not just the first');
    t.mock.timers.reset();
});

test('a re-render never STEALS focus, and never focuses a field the code already left', () => {
    // Nothing in the card focused: the swap happens, the caret stays wherever
    // the owner actually put it (another field, another section).
    const elsewhere = { hasAttribute: () => false };
    const replacement = fakeCodeInput();
    const host = fakeCardHost(replacement, null);
    preserveCardFocus(host, () => { host.swaps += 1; }, { activeElement: elsewhere });
    assert.equal(host.swaps, 1);
    assert.equal(replacement.calls.focus, 0);

    // Focused, but the code was accepted meanwhile: the replacement renders
    // disabled and must not be focused (nor asked for a selection range).
    const typing = fakeCodeInput();
    const sent = fakeCodeInput({ disabled: true });
    const host2 = fakeCardHost(sent, typing);
    preserveCardFocus(host2, () => { host2.swaps += 1; }, { activeElement: typing });
    assert.equal(host2.swaps, 1);
    assert.equal(sent.calls.focus, 0);
    assert.equal(sent.calls.range, null);

    // No document at all (module imported in node): the swap still runs.
    const host3 = fakeCardHost(replacement, null);
    preserveCardFocus(host3, () => { host3.swaps += 1; }, null);
    assert.equal(host3.swaps, 1);
});


// ---------------------------------------------------------------------------
// C7: login-job serialization — a new login only after the old one is gone.
// ---------------------------------------------------------------------------

test('cancelLoginJob reports gone only on ok/404/410; failures and network death are NOT cancelled', async () => {
    const mk = (status, ok) => async () => ({ ok, status });
    assert.equal(await cancelLoginJob('job-1', mk(200, true)), true);
    assert.equal(await cancelLoginJob('job-1', mk(404, false)), true);   // already gone
    assert.equal(await cancelLoginJob('job-1', mk(410, false)), true);   // already gone
    assert.equal(await cancelLoginJob('job-1', mk(503, false)), false);  // daemon may still run it
    assert.equal(await cancelLoginJob('job-1', mk(500, false)), false);
    assert.equal(await cancelLoginJob('job-1', async () => { throw new Error('net'); }), false);
    assert.equal(await cancelLoginJob('', async () => { throw new Error('must not be called'); }), true);
});

test('startLogin centralizes the C7 guard: cancel-or-refuse BEFORE the new login POST', () => {
    // ESM keeps startLogin internal state untestable directly; pin the control
    // flow at the source level (same source-based technique as the HTML pins
    // in this file): the guard must sit inside startLogin ahead of the POST,
    // and a failed cancellation must return without starting a second job.
    const src = readFileSync(fileURLToPath(new URL('../modules/harness_accounts.js', import.meta.url)), 'utf8');
    const fn = src.slice(src.indexOf('async function startLogin'));
    const guardAt = fn.indexOf('cancelLoginJob(prev.jobId)');
    const postAt = fn.indexOf("apiFetch('/api/claudexor/login'");
    assert.ok(guardAt > -1, 'startLogin must call cancelLoginJob for a live previous job');
    assert.ok(postAt > -1);
    assert.ok(guardAt < postAt, 'the C7 guard must run before the new login POST');
    const guarded = fn.slice(guardAt, postAt);
    assert.match(guarded, /if \(!cancelled && !settledMeanwhile\) \{[\s\S]*?return;/,
        'a failed cancel (with the job still unsettled) must refuse the new login');
});


test('loginSettleProven: only a TERMINAL job snapshot proves the settle — an unconfirmed verdict does NOT', () => {
    assert.equal(loginSettleProven(null), false);
    assert.equal(loginSettleProven({}), false);
    assert.equal(loginSettleProven({ job: { state: 'running' } }), false);
    // Lost contact: the give-up verdict must NEVER read as proof of settle —
    // the job may still be live, and treating it as settled would let a
    // dismiss/restart drop or duplicate a live login (round b7).
    assert.equal(loginSettleProven({ job: { state: 'running' }, verdict: { kind: 'unconfirmed' } }), false);
    assert.equal(loginSettleProven({ job: null, verdict: { kind: 'unconfirmed' } }), false);
    assert.equal(loginSettleProven({ job: { state: 'succeeded' } }), true);
    assert.equal(loginSettleProven({ job: { state: 'failed' } }), true);
    assert.equal(loginSettleProven({ job: { state: 'cancelled' } }), true);
});


// The wiring of the loading state, not just its wording: a revert that deletes
// the flag plumbing (keeping daemonStatusLine intact) must go RED here.
function stubStatusDom() {
    const host = { offsetParent: {}, innerHTML: '', querySelectorAll: () => [] };
    const statusEl = { textContent: '', dataset: {} };
    const lines = [];
    const el = {
        'harness-accounts-rows': host,
        'harness-daemon-status': new Proxy(statusEl, {
            set(target, key, value) {
                if (key === 'textContent') lines.push(value);
                target[key] = value;
                return true;
            },
        }),
        'harness-login-card': null,
    };
    globalThis.document = { getElementById: (id) => el[id] ?? null, hidden: false };
    return { lines };
}

test('the status poll is single-flight and announces the first read before it lands', async () => {
    const previousDocument = globalThis.document;
    const { lines } = stubStatusDom();
    try {
        let started = 0;
        let release = null;
        const gate = new Promise((resolve) => { release = resolve; });
        const fetchImpl = async () => {
            started += 1;
            await gate;
            return { ok: true, json: async () => ({ daemon: { state: 'running', engine_version: '3.3.13', runtime: {} } }) };
        };

        const first = refreshStatus({ force: true, fetchImpl });
        // Painted BEFORE the response: the panel says it is checking, muted.
        assert.ok(lines.some((line) => line.includes('Checking Claudexor')),
            'the first read announces itself before it lands');

        // Every caller while one read is live SHARES it — the 5s interval must not
        // stack reads over a request that takes tens of seconds (each fans out to
        // four CLI-probing daemon GETs).
        const joined = [refreshStatus({ force: true, fetchImpl }), refreshStatus({ fetchImpl })];
        assert.equal(started, 1, 'overlapping polls did not start a second read');

        release();
        await Promise.all([first, ...joined]);
        assert.equal(started, 1, 'the shared read served every caller');
        assert.ok(lines.at(-1).includes('3.3.13'), 'the settled read replaces the checking line');

        // After the first settle the checking line never returns: a stable line
        // beats flickering muted<->error on every tick.
        lines.length = 0;
        const fourth = refreshStatus({ force: true, fetchImpl });
        assert.equal(started, 2, 'a settled read releases the single-flight slot');
        assert.ok(!lines.some((line) => line.includes('Checking Claudexor')),
            'no pre-request repaint once anything has been said');
        release();
        await fourth;
    } finally {
        globalThis.document = previousDocument;
    }
});

test('an unread facet is never rendered as an authoritative empty', () => {
    // THE bug the owner caught: three harnesses labelled "no account connected"
    // while two claude profiles, a cursor profile and two native sessions sat in
    // the agent home — the daemon is lazy, so nothing had been read.
    const idle = { reads: { catalog: 'not_read', accounts: 'not_read', quota: 'not_read' },
                   daemon: { state: 'stale', runtime: { state: 'ready', version: '3.3.13' } } };
    assert.equal(facetReadState(idle, 'accounts'), 'not_read');
    assert.equal(accountsKnown(idle), false);
    assert.match(unknownAccountsNote(idle), /not checked/i);
    assert.match(unknownAccountsNote(idle), /not running/i);

    // A read that ANSWERED makes emptiness authoritative — otherwise the hedge
    // could never step aside and the panel could never say anything at all.
    const read = { reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' }, daemon: { state: 'running' } };
    assert.equal(accountsKnown(read), true);
    assert.equal(unknownAccountsNote(read), '');

    // Facets are INDEPENDENT: reviewer slots read the catalog, so an accounts
    // failure must not blank a catalog that landed.
    const partial = { reads: { catalog: 'ok', accounts: 'failed', quota: 'ok' }, daemon: { state: 'unreachable' } };
    assert.equal(facetReadState(partial, 'catalog'), 'ok');
    assert.equal(facetReadState(partial, 'accounts'), 'failed');
    // A refused read is NOT "the daemon is not running" — it was running and one
    // endpoint died; saying otherwise would be a second lie.
    assert.ok(!/not running/i.test(unknownAccountsNote(partial)));

    // LEGACY payload (older backend / older fixture): the daemon state carried
    // exactly this fact, so consumers keep working without the block.
    assert.equal(facetReadState({ daemon: { state: 'running' } }, 'accounts'), 'ok');
    assert.equal(facetReadState({ daemon: { state: 'stale' } }, 'accounts'), 'not_read');
    // A request that never completed outranks whatever the last payload said.
    assert.equal(facetReadState(read, 'accounts', { transportError: 'HTTP 500' }), 'transport');
});

test('the Refresh button tells the truth about what it does, and wakes the daemon once', async () => {
    // With the daemon asleep a plain re-read returns the same nothing forever
    // (status never spawns), so there the button is an explicit owner start and
    // its label says so. Live, it stays a plain re-read.
    assert.equal(refreshActionLabel({ daemon: { state: 'running' } }), 'Refresh');
    assert.match(refreshActionLabel({ daemon: { state: 'stale' } }), /starts the agent daemon/i);

    const previousDocument = globalThis.document;
    const { lines } = stubStatusDom();
    try {
        let started = 0;
        let release = null;
        const gate = new Promise((resolve) => { release = resolve; });
        const fetchImpl = async (url, opts) => {
            assert.equal(url, '/api/claudexor/wake');
            assert.equal(opts?.method, 'POST');
            started += 1;
            await gate;
            return { ok: true, json: async () => ({
                daemon: { state: 'running', engine_version: '3.3.13', runtime: {} },
                reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' },
            }) };
        };

        const first = wakeDaemon({ fetchImpl });
        // A cold runtime install takes real time; a second click must not start
        // a second provisioning.
        const second = wakeDaemon({ fetchImpl });
        assert.equal(started, 1, 'the wake is single-flighted');

        release();
        await Promise.all([first, second]);
        assert.ok(lines.at(-1).includes('3.3.13'), 'the woken daemon replaces the line');
    } finally {
        globalThis.document = previousDocument;
    }
});

test('a first request that never lands says so instead of judging the daemon', () => {
    // Page load against a restarting backend: the very FIRST status GET dies,
    // so there is no reading to qualify. The transport branch required a prior
    // daemon state, so this fell through to the aggregate fallback and printed
    // "Daemon unknown" in error tone — a verdict about the daemon assembled
    // from zero data, next to a row that correctly said nothing was checked.
    const line = daemonStatusLine(null, { transportError: 'Failed to fetch' });
    assert.equal(line.tone, 'warn');
    assert.match(line.text, /nothing has been read yet/i);
    assert.ok(!/Daemon unknown/i.test(line.text));
    assert.match(line.text, /Failed to fetch/);

    // With a reading already on screen the wording still qualifies THAT reading.
    const withReading = daemonStatusLine(
        { daemon: { state: 'running', engine_version: '3.3.13', runtime: {} },
          reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' } },
        { transportError: 'HTTP 502' });
    assert.match(withReading.text, /last reading/i);
});

test('a failed refresh keeps the last reading but stops calling it current', () => {
    // The MIRROR of the false absence, and the owner has not seen it yet: a
    // swallowed failure used to leave "Claudexor ready" and green badges
    // standing indefinitely after the endpoint died.
    const live = { daemon: { state: 'running', engine_version: '3.3.13', runtime: {} },
                   reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' } };
    const fresh = daemonStatusLine(live, {});
    assert.equal(fresh.tone, 'ok');

    const stale = daemonStatusLine(live, { transportError: 'HTTP 502' });
    assert.equal(stale.tone, 'warn');
    assert.match(stale.text, /last reading/i);
    assert.match(stale.text, /502/);
    assert.ok(!/Claudexor ready/.test(stale.text), 'no health claim from a failed read');
});

test('a read block that is present but unusable is never authoritative', () => {
    // The block itself can arrive broken — `reads: null` from a drifting
    // backend, a string, a number. Treating those as "no block at all" sent
    // them down the legacy branch, where a running daemon turns unknown into
    // `ok`: the original false absence, restored through the very field added
    // to prevent it. Only a payload with NO `reads` property is legacy.
    const running = { daemon: { state: 'running' } };
    for (const broken of [null, 'ok', 7, []]) {
        assert.equal(facetReadState({ ...running, reads: broken }, 'accounts'), 'failed',
            `reads:${JSON.stringify(broken)} was treated as a legacy payload`);
    }
    assert.equal(facetReadState(running, 'accounts'), 'ok', 'a real legacy payload still works');
    assert.equal(facetReadState({ daemon: { state: 'stale' } }, 'accounts'), 'not_read');
});

test('the refresh button says exactly what pressing it does', () => {
    // Label and handler were written apart and drifted: on an `unreachable`
    // daemon the label promised a plain re-read (and a comment claimed it
    // "does not resurrect anything") while the click called wake. One
    // predicate now feeds both, so the button cannot promise less than it does.
    assert.equal(refreshActionKind({ daemon: { state: 'running' } }), 'refresh');
    assert.equal(refreshActionLabel({ daemon: { state: 'running' } }), 'Refresh');
    for (const state of ['unreachable', 'stale', 'not_provisioned', 'foreign_daemon', '']) {
        assert.equal(refreshActionKind({ daemon: { state } }), 'wake', `state ${state}`);
        assert.match(refreshActionLabel({ daemon: { state } }), /starts the agent daemon/i,
            `the label hides the start on state ${state}`);
    }
});

test('a recovered daemon clears the error from an earlier failed wake', async () => {
    // The daemon comes up on its own at the next login or delegated run. The
    // failed-wake message had no way to expire, so the panel went on insisting
    // the daemon could not be started while it was answering reads — a stale
    // error is the same class of lie as a stale absence.
    const previousDocument = globalThis.document;
    const { lines } = stubStatusDom();
    try {
        // The state a wake actually happens in: the button only routes to wake
        // when nothing has answered. Establish it instead of inheriting whatever
        // payload an earlier test left in module state.
        commitStatusPayload({ daemon: { state: 'stale', engine_version: '3.3.13', runtime: {} },
                              reads: { catalog: 'not_read', accounts: 'not_read', quota: 'not_read' } });
        await wakeDaemon({ fetchImpl: async () => ({
            ok: false, status: 503, json: async () => ({ error: 'claudexord_not_installed: no binary' }),
        }) });
        assert.match(lines.at(-1), /Could not start the agent daemon/i);

        await refreshStatus({ force: true, fetchImpl: async () => ({ ok: true, json: async () => ({
            daemon: { state: 'running', engine_version: '3.3.14', runtime: {} },
            reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' },
        }) }) });
        assert.ok(!/Could not start the agent daemon/i.test(lines.at(-1)),
            'the stale wake error outlived the daemon that came up anyway');
        assert.ok(lines.at(-1).includes('3.3.14'));
    } finally {
        globalThis.document = previousDocument;
    }
});

test('the refresh button routes the click through the same predicate as its label', async () => {
    // The label and the handler were written apart and disagreed once already.
    // Pinning the predicate alone does not stop the handler from ignoring it —
    // replacing its body with an unconditional wake left every test green.
    const previousDocument = globalThis.document;
    const { lines } = stubStatusDom();
    const clicks = [];
    const base = globalThis.document.getElementById;
    globalThis.document.getElementById = (id) => (id === 'btn-harness-refresh'
        ? { addEventListener: (_ev, fn) => clicks.push(fn) }
        : base(id));
    globalThis.document.addEventListener = () => {};
    globalThis.document.querySelector = () => null;
    const previousWindow = globalThis.window;
    globalThis.window = { addEventListener: () => {} };
    // The module owns its poll timer privately; neutralise it here so the test
    // process can exit instead of ticking against a stubbed document forever.
    const previousSetInterval = globalThis.setInterval;
    globalThis.setInterval = () => 0;
    const previousFetch = globalThis.fetch;
    const urls = [];
    globalThis.fetch = async (url) => {
        urls.push(String(url));
        return { ok: true, json: async () => ({ daemon: { state: 'running', engine_version: '3.3.14', runtime: {} },
                                                reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' } }) };
    };
    try {
        initHarnessAccounts();
        assert.equal(clicks.length, 1, 'the refresh button lost its listener');

        // Daemon asleep -> the press must START it.
        commitStatusPayload({ daemon: { state: 'stale' }, reads: {} });
        urls.length = 0;
        await clicks[0]();
        assert.ok(urls.some((u) => u.includes('/api/claudexor/wake')),
            'a sleeping daemon was only re-read, so the button could not help');

        // Daemon live -> the press must stay a plain re-read.
        commitStatusPayload({ daemon: { state: 'running' }, reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' } });
        urls.length = 0;
        await clicks[0]();
        assert.ok(urls.every((u) => !u.includes('/api/claudexor/wake')),
            'a live daemon was provisioned by a button that says Refresh');
    } finally {
        globalThis.setInterval = previousSetInterval;
        globalThis.window = previousWindow;
        globalThis.fetch = previousFetch;
        globalThis.document = previousDocument;
    }
});

test('the first read says it is reading, not that the daemon is down', async () => {
    // The first paint happens BEFORE any answer — the fan-out probes each
    // coding-agent CLI and takes tens of seconds. The branch that says so was
    // keyed on a null payload while the renderer handed it a normalized `{}`,
    // so it never ran: every bootstrap row spent that window announcing "the
    // agent daemon is not running" about a daemon that was answering, next to a
    // status line that said "Checking Claudexor…".
    //
    // A FRESH module instance (the query string defeats the ESM cache) is the
    // only way to see the true first paint: module state is shared, and by the
    // time the other tests have run a payload is already committed.
    const previousDocument = globalThis.document;
    const { lines } = stubStatusDom();
    const host = globalThis.document.getElementById('harness-accounts-rows');
    try {
        const fresh = await import('../modules/harness_accounts.js?first-paint');
        let release = null;
        const gate = new Promise((resolve) => { release = resolve; });
        const pending = fresh.refreshStatus({ force: true, fetchImpl: async () => {
            await gate;
            return { ok: true, json: async () => ({ daemon: { state: 'stale', engine_version: '3.3.13', runtime: {} },
                                                    reads: { catalog: 'not_read', accounts: 'not_read', quota: 'not_read' } }) };
        } });
        assert.match(host.innerHTML, /reading the daemon status/,
            'the first paint announced a verdict about a daemon nobody had asked');
        assert.ok(!/the agent daemon is not running/.test(host.innerHTML));
        release();
        await pending;
        // Once a read SETTLES on a genuinely sleeping daemon, the verdict is
        // earned and must appear.
        assert.match(host.innerHTML, /the agent daemon is not running/);
        assert.ok(lines.length > 0);
    } finally {
        globalThis.document = previousDocument;
    }
});

test('the wake and the poll never overlap, whichever starts first', async () => {
    // Two writers, two orders. A poll STARTED DURING a wake joins it (no second
    // GET runs). A wake pressed DURING a poll waits that poll out before
    // POSTing, so the wake's daemon-side read causally follows the poll's
    // commit and the unconditional wake commit can never resurrect an older
    // snapshot — the reviewer's probe showed exactly that rollback when the
    // wake fired mid-poll without waiting.
    const previousDocument = globalThis.document;
    const { lines } = stubStatusDom();
    try {
        // Order 1: wake first, poll joins.
        commitStatusPayload({ daemon: { state: 'stale', engine_version: '3.3.13', runtime: {} },
                              reads: { catalog: 'not_read', accounts: 'not_read', quota: 'not_read' } });
        let releaseWake = null;
        const wakeGate = new Promise((resolve) => { releaseWake = resolve; });
        let polls = 0;
        const wake = wakeDaemon({ fetchImpl: async () => {
            await wakeGate;
            return { ok: true, json: async () => ({
                daemon: { state: 'running', engine_version: '3.3.14', runtime: {} },
                reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' } }) };
        } });
        const joined = refreshStatus({ force: true, fetchImpl: async () => {
            polls += 1;
            return { ok: true, json: async () => ({ daemon: { state: 'stale', runtime: {} }, reads: {} }) };
        } });
        assert.equal(polls, 0, 'a second reader was started while the wake was in flight');
        releaseWake();
        await Promise.all([wake, joined]);
        assert.ok(lines.at(-1).includes('3.3.14'), 'the wake reading did not survive');

        // Order 2: poll first, wake waits it out before POSTing.
        commitStatusPayload({ daemon: { state: 'stale', engine_version: '3.3.14', runtime: {} },
                              reads: { catalog: 'not_read', accounts: 'not_read', quota: 'not_read' } });
        let releasePoll = null;
        const pollGate = new Promise((resolve) => { releasePoll = resolve; });
        const events = [];
        const slowPoll = refreshStatus({ force: true, fetchImpl: async () => {
            await pollGate;
            events.push('poll-answered');
            return { ok: true, json: async () => ({
                daemon: { state: 'running', engine_version: '3.3.99', runtime: {} },
                reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' } }) };
        } });
        const lateWake = wakeDaemon({ fetchImpl: async () => {
            events.push('wake-posted');
            return { ok: true, json: async () => ({
                daemon: { state: 'running', engine_version: '3.4.00', runtime: {} },
                reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' } }) };
        } });
        releasePoll();
        await Promise.all([slowPoll, lateWake]);
        assert.deepEqual(events, ['poll-answered', 'wake-posted'],
            'the wake POSTed before the in-flight poll finished');
        assert.ok(lines.at(-1).includes('3.4.00'),
            'the wake reading (causally later) did not win');
    } finally {
        globalThis.document = previousDocument;
    }
});

test('a refusal that arrives after the daemon started answering is not shown', async () => {
    // The wake is pressed only when nothing answers — but an ordinary poll can
    // commit a LIVE reading while the wake is still in flight. A refusal landing
    // afterwards then planted "Could not start the agent daemon" on top of a
    // panel already listing that daemon's accounts. The failure was real; it had
    // stopped mattering.
    const previousDocument = globalThis.document;
    const { lines } = stubStatusDom();
    try {
        commitStatusPayload({ daemon: { state: 'stale', engine_version: '3.3.13', runtime: {} },
                              reads: { catalog: 'not_read', accounts: 'not_read', quota: 'not_read' } });
        let releaseWake = null;
        const gate = new Promise((resolve) => { releaseWake = resolve; });
        const wake = wakeDaemon({ fetchImpl: async () => {
            await gate;
            return { ok: false, status: 503, json: async () => ({ error: 'claudexord_not_installed: no binary' }) };
        } });
        // ...the daemon comes up on its own and a poll commits that reading.
        commitStatusPayload({ daemon: { state: 'running', engine_version: '3.3.14', runtime: {} },
                              reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' } });
        releaseWake();
        await wake;
        assert.ok(!/Could not start the agent daemon/i.test(lines.at(-1)),
            'a moot refusal was printed over a daemon that is answering');
        assert.ok(lines.at(-1).includes('3.3.14'));
    } finally {
        globalThis.document = previousDocument;
    }
});

test('a payload with no unread facets never renders an empty list', () => {
    // The contract declares `daemon` optional, and a payload carrying only a
    // reads block took the partial-refusal branch with an EMPTY complement —
    // rendering the sentence " was not read. What those cover is unknown.",
    // which starts with a space and names nothing.
    const noDaemon = { reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' } };
    const line = daemonStatusLine(noDaemon, {});
    assert.ok(!/^\s/.test(line.text), 'the line begins with an empty facet list');
    assert.ok(!/was not read/.test(line.text));
});

test('a staged update does not claim the engine is serving when nothing answered', () => {
    // The runtime branches used to return ABOVE the facet logic, so 26 of the 27
    // facet vectors went unnamed — and update_staged did worse than hide them:
    // "Engine X keeps running until then" is a positive claim about a daemon
    // that, in this window, answered nothing, printed over a button offering to
    // START it. One screen saying both "running" and "needs starting".
    const silentStaged = { daemon: { state: 'unreachable', engine_version: '3.3.13',
                                     runtime: { state: 'update_staged', staged_version: '3.3.14' } },
                           reads: { catalog: 'not_read', accounts: 'not_read', quota: 'not_read' } };
    const line = daemonStatusLine(silentStaged, {});
    assert.ok(!/keeps running/.test(line.text), 'claimed the engine is serving on a reading that saw nothing');
    assert.match(line.text, /were not read/);
    assert.match(line.text, /staged/, 'the staged update is still disclosed');

    // Everything read: the staged-update line is earned and keeps its wording.
    const servingStaged = { daemon: { state: 'running', engine_version: '3.3.13',
                                      runtime: { state: 'update_staged', staged_version: '3.3.14' } },
                            reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' } };
    assert.match(daemonStatusLine(servingStaged, {}).text, /keeps running/);

    // installing / error keep their own wording but stop hiding the facets.
    const installing = { daemon: { state: 'unreachable', runtime: { state: 'installing', target_version: '3.3.14' } },
                         reads: { catalog: 'ok', accounts: 'failed', quota: 'failed' } };
    assert.match(daemonStatusLine(installing, {}).text, /accounts and quota were not read/);
    const broken = { daemon: { state: 'unreachable', runtime: { state: 'error', last_error: 'exit 1' } },
                     reads: { catalog: 'ok', accounts: 'failed', quota: 'ok' } };
    assert.match(daemonStatusLine(broken, {}).text, /accounts was not read/);
});

test('a login seen online is not un-seen by a reading that landed meanwhile', () => {
    // The generation is captured once for a SERIES of confirmation attempts. A
    // commit landing between attempt 1 and attempt 2 marked the whole series
    // stale — including the attempt that watched the account come online — and
    // the card announced "not confirmed" about an account that had just
    // connected. Seeing the login is monotone evidence.
    const src = readFileSync(new URL('../modules/harness_accounts.js', import.meta.url), 'utf8');
    const body = src.split('async function settleVerdict(')[1].split('\n}\n')[0];
    assert.ok(!/superseded\s*\?\s*accountLoginConfirmed/.test(body),
        'the verdict is still gated on the series-wide generation');
    assert.match(body, /check\.confirmed\s*\n?\s*\|\|/,
        'a positive confirmation no longer wins on its own');
});

test('a facet that failed without the aggregate hearing it is still reported', () => {
    // An envelope in the wrong shape is a FAILED read, not an exception, so the
    // daemon goes on reporting `running`. The status line trusted that literal
    // and printed green "Claudexor ready" directly above a row saying "accounts
    // not checked — the daemon did not answer this read": one screen, two
    // contradictory claims, the reassuring one on top.
    const drifted = { daemon: { state: 'running', engine_version: '3.3.13', runtime: {} },
                      config_dir: '/x', reads: { catalog: 'failed', accounts: 'failed', quota: 'ok' } };
    const line = daemonStatusLine(drifted, {});
    assert.equal(line.tone, 'warn', 'a running daemon with two dead reads was called ready');
    assert.ok(!/Claudexor ready/.test(line.text));
    assert.match(line.text, /catalog and accounts were not read/);

    // Everything read: the green claim is earned and must still be made.
    const whole = { daemon: { state: 'running', engine_version: '3.3.13', runtime: {} },
                    config_dir: '/x', reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' } };
    assert.equal(daemonStatusLine(whole, {}).tone, 'ok');
    assert.match(daemonStatusLine(whole, {}).text, /Claudexor ready/);

    // A legacy payload with no read block at all keeps the old meaning.
    const legacy = { daemon: { state: 'running', engine_version: '3.3.13', runtime: {} }, config_dir: '/x' };
    assert.equal(daemonStatusLine(legacy, {}).tone, 'ok');
});

test('a partial refusal is not announced as a dead daemon', () => {
    // The aggregate reports `unreachable` whenever ANY read refused, so the
    // status line printed red "Daemon unreachable" directly above the account
    // rows the same poll had just delivered — the panel contradicting itself,
    // with the false half on top.
    const partial = { daemon: { state: 'unreachable', last_error: 'daemon_unreachable: quota refused', runtime: {} },
                      reads: { catalog: 'ok', accounts: 'ok', quota: 'failed' } };
    const line = daemonStatusLine(partial, {});
    assert.equal(line.tone, 'warn', 'a daemon that answered two reads was called dead');
    assert.ok(!/unreachable$/.test(line.text));
    assert.match(line.text, /quota was not read/,
        'the line must name WHICH facet is missing, not claim everything visible was read');
    assert.ok(!/shown below was read/.test(line.text));
    assert.match(line.text, /quota refused/, 'the reason the owner needs was dropped');

    // Total silence keeps the hard verdict.
    const silent = { daemon: { state: 'unreachable', last_error: 'handshake failed', runtime: {} },
                     reads: { catalog: 'not_read', accounts: 'not_read', quota: 'not_read' } };
    assert.equal(daemonStatusLine(silent, {}).tone, 'error');
    assert.match(daemonStatusLine(silent, {}).text, /Daemon unreachable/);
});

test('a daemon that answered some reads is not called dead by the aggregate', async () => {
    // The partial refusal this whole surface exists for: quota times out while
    // the catalog and the account store both land, and the backend still
    // reports the aggregate as `unreachable`. A predicate written on that
    // aggregate kept a failed wake's error standing above accounts that were
    // genuinely read, and made Refresh promise to start a daemon already
    // answering — the coarse-signal mistake, committed inside the fix for it.
    const partial = { daemon: { state: 'unreachable', engine_version: '3.3.13', runtime: {} },
                      reads: { catalog: 'ok', accounts: 'ok', quota: 'failed' } };
    assert.equal(daemonAnswered(partial), true, 'two landed reads read as no answer at all');
    // The ACCOUNTS facet is not privileged: a catalog that landed alone still
    // proves the daemon answered. Without this, keying the predicate on
    // `reads.accounts` alone passes every other fixture.
    assert.equal(daemonAnswered({ daemon: { state: 'unreachable' },
        reads: { catalog: 'ok', accounts: 'failed', quota: 'failed' } }), true);
    assert.equal(daemonAnswered({ daemon: { state: 'unreachable' },
        reads: { catalog: 'failed', accounts: 'failed', quota: 'ok' } }), true);
    // A read block that is not a facet MAP answers for nothing — an array is
    // `typeof 'object'`, and asking it directly (instead of through the shared
    // reader) made `['ok']` look like an answered facet while the reader called
    // every facet failed. Two readers of one field, disagreeing.
    assert.equal(daemonAnswered({ daemon: { state: 'unreachable' }, reads: ['ok'] }), false);
    assert.equal(refreshActionKind({ daemon: { state: 'unreachable' }, reads: ['ok'] }), 'wake');
    assert.deepEqual(READ_FACETS, ['catalog', 'accounts', 'quota']);
    assert.equal(refreshActionKind(partial), 'refresh',
        'the button offered to start a daemon that is answering');

    // Nothing read at all — a pre-fan-out discovery/handshake failure — still
    // means the daemon never spoke, and there the button must start it.
    const silent = { daemon: { state: 'unreachable' },
                     reads: { catalog: 'not_read', accounts: 'not_read', quota: 'not_read' } };
    assert.equal(daemonAnswered(silent), false);
    assert.equal(refreshActionKind(silent), 'wake');

    const previousDocument = globalThis.document;
    const { lines } = stubStatusDom();
    try {
        // The state a wake actually happens in: the button only routes to wake
        // when nothing has answered. Establish it instead of inheriting whatever
        // payload an earlier test left in module state.
        commitStatusPayload({ daemon: { state: 'stale', engine_version: '3.3.13', runtime: {} },
                              reads: { catalog: 'not_read', accounts: 'not_read', quota: 'not_read' } });
        await wakeDaemon({ fetchImpl: async () => ({
            ok: false, status: 503, json: async () => ({ error: 'claudexord_not_installed: no binary' }) }) });
        assert.match(lines.at(-1), /claudexord_not_installed/);
        await refreshStatus({ force: true, fetchImpl: async () => ({ ok: true, json: async () => partial }) });
        assert.ok(!/Could not start the agent daemon/i.test(lines.at(-1)),
            'a stale wake error stood over accounts the daemon had just handed over');
    } finally {
        globalThis.document = previousDocument;
    }
});

test('a wake error expires only when the daemon is proven up', async () => {
    // Both edges of the same lie. The error must not outlive the daemon coming
    // up on its own (login, delegated run) — but it must also not be wiped by a
    // 200 that REPORTS the daemon still down: the reason the owner asked for
    // then vanishes within one 5s tick, before it can be read.
    const previousDocument = globalThis.document;
    const { lines } = stubStatusDom();
    try {
        const stillDown = { daemon: { state: 'stale', engine_version: '3.3.13', runtime: {} },
                            reads: { catalog: 'not_read', accounts: 'not_read', quota: 'not_read' } };
        const up = { daemon: { state: 'running', engine_version: '3.3.14', runtime: {} },
                     reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' } };

        // The state a wake actually happens in: the button only routes to wake
        // when nothing has answered. Establish it instead of inheriting whatever
        // payload an earlier test left in module state.
        commitStatusPayload({ daemon: { state: 'stale', engine_version: '3.3.13', runtime: {} },
                              reads: { catalog: 'not_read', accounts: 'not_read', quota: 'not_read' } });
        await wakeDaemon({ fetchImpl: async () => ({
            ok: false, status: 503, json: async () => ({ error: 'claudexord_not_installed: no binary' }) }) });
        assert.match(lines.at(-1), /claudexord_not_installed/, 'the refusal reached the owner');

        await refreshStatus({ force: true, fetchImpl: async () => ({ ok: true, json: async () => stillDown }) });
        assert.match(lines.at(-1), /Could not start the agent daemon/i,
            'a 200 that says the daemon is STILL down erased the reason it failed');

        await refreshStatus({ force: true, fetchImpl: async () => ({ ok: true, json: async () => up }) });
        assert.ok(!/Could not start the agent daemon/i.test(lines.at(-1)),
            'the error outlived the daemon that came up');
        assert.ok(lines.at(-1).includes('3.3.14'));
    } finally {
        globalThis.document = previousDocument;
    }
});

test('the login confirmation commits under the same generation rule as the poll', () => {
    // STRUCTURAL pin: settleVerdict is module-private, so the guard is read out
    // of the source rather than exercised. It matters because that path fetches
    // the status endpoint DIRECTLY, outside refreshStatus — a wake that landed
    // while the confirmation was in flight has already committed a newer
    // payload, and the comment on readGeneration promises a read issued before
    // a wake never commits after it.
    const src = readFileSync(new URL('../modules/harness_accounts.js', import.meta.url), 'utf8');
    const body = src.split('async function settleVerdict(')[1].split('\n}\n')[0];
    assert.match(body, /const generation = state\.readGeneration/,
        'the confirmation does not capture a generation before awaiting');
    // The comparison must GATE the commit, not merely appear near it: a dead
    // `const ok = generation === state.readGeneration;` beside an unconditional
    // commit would satisfy a looser pin while the invariant is broken.
        const named = body.match(/const (\w+) = generation !== state\.readGeneration/);
    const guard = named
        ? new RegExp(`if \\([^)]*!${named[1]}[^)]*\\)\\s*commitStatusPayload\\(`)
        : /if \([^)]*generation === state\.readGeneration[^)]*\)\s*commitStatusPayload\(/;
    assert.match(body, guard, 'the generation check does not gate the commit');
    // ...and the VERDICT must not be gated by it. A confirmation that SAW the
    // login is monotone evidence — the generation is captured once for a series
    // of attempts, so gating the verdict on it threw away a later attempt that
    // watched the account come online.
    assert.match(body, /const confirmed = check\.confirmed\s*\n?\s*\|\|\s*accountLoginConfirmed\(state\.payload/,
        'a positive confirmation is discarded when its series was superseded');
    // ...and the confirmation must JOIN an in-flight wake, not read beside it:
    // it hits the status endpoint directly, so without the join it is the
    // third writer in the exact race the wake serialization exists to settle.
    assert.match(body, /if \(state\.wakeInFlight\) await state\.wakeInFlight/,
        'the login confirmation races an in-flight wake as a third writer');
    // ...and the capture must happen BEFORE the await it is protecting.
    assert.ok(body.indexOf('const generation = state.readGeneration')
        < body.indexOf('await confirmLoginLive'),
        'the generation is captured after the await, which measures nothing');
});

test('every path that commits a fresh reading retires staleness the same way', () => {
    // Three producers of a fresh payload (poll, wake, login confirmation) went
    // through three different commits, so which errors got retired depended on
    // which one answered. One function owns it now.
    const src = readFileSync(new URL('../modules/harness_accounts.js', import.meta.url), 'utf8');
    const assignments = src.match(/state\.payload = /g) || [];
    assert.equal(assignments.length, 1,
        'a payload is committed outside commitStatusPayload; the retire rules will drift again');
    assert.ok(/export function commitStatusPayload/.test(src));
});

test('a status read that started before the fresh one never commits over it', async () => {
    // The generation guard's remaining job. The wake no longer overlaps the
    // poll (causal serialization), but the login CONFIRMATION reads the status
    // endpoint outside refreshStatus and commits through commitStatusPayload —
    // so a poll that started before that commit still carries an older reading
    // and must not land on top of it.
    const previousDocument = globalThis.document;
    const { lines } = stubStatusDom();
    try {
        const fresh = { daemon: { state: 'running', engine_version: '3.3.14', runtime: {} },
                        reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' } };
        const stale = { daemon: { state: 'stale', engine_version: '3.3.13', runtime: {} },
                        reads: { catalog: 'not_read', accounts: 'not_read', quota: 'not_read' } };
        // (a) The older read RESOLVES after the fresh commit: it must be dropped.
        let releasePoll = null;
        const gate = new Promise((resolve) => { releasePoll = resolve; });
        const slowPoll = refreshStatus({ force: true, fetchImpl: async () => {
            await gate;
            return { ok: true, json: async () => stale };
        } });
        commitStatusPayload(fresh);   // the confirmation's commit path
        releasePoll();
        await slowPoll;
        // A dropped read does not render at all (the early return skips the
        // post-commit paint — the winning reader owns the screen). What the
        // owner must never see is the ROLLBACK: no line from the stale body.
        assert.ok(!lines.some((l) => l.includes('3.3.13') || /not running/i.test(l)),
            'the stale poll reached the screen after losing the generation race');

        // (b) The older read FAILS instead: it must not brand the fresh reading stale.
        let failPoll = null;
        const failGate = new Promise((resolve) => { failPoll = resolve; });
        const failingPoll = refreshStatus({ force: true, fetchImpl: async () => {
            await failGate;
            throw new Error('socket hang up');
        } });
        commitStatusPayload(fresh);
        failPoll();
        await failingPoll;
        assert.ok(!lines.some((l) => /last reading/i.test(l)),
            'a stale failure marked a fresh reading stale');
    } finally {
        globalThis.document = previousDocument;
    }
});

test('a failed status request marks the view stale, and a failed wake is never silent', async () => {
    // Both halves of the WIRING (the pure-helper tests above pass even if the
    // plumbing is reverted): refreshStatus must record the transport failure,
    // and wakeDaemon must surface an error the owner asked for by clicking.
    const previousDocument = globalThis.document;
    const { lines } = stubStatusDom();
    try {
        const okOnce = async () => ({ ok: true, json: async () => ({
            daemon: { state: 'running', engine_version: '3.3.13', runtime: {} },
            reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' },
        }) });
        await refreshStatus({ force: true, fetchImpl: okOnce });
        assert.ok(lines.at(-1).includes('3.3.13'), 'a good read renders the live line');

        await refreshStatus({ force: true, fetchImpl: async () => ({ ok: false, status: 502, json: async () => ({}) }) });
        assert.match(lines.at(-1), /last reading/i, 'a failed read stops claiming current truth');
        assert.match(lines.at(-1), /502/);

        // A wake the owner pressed that refuses (typed 503, or a 404 from an
        // older backend) must SAY so instead of silently returning to idle.
        // The state a wake actually happens in: the button only routes to wake
        // when nothing has answered. Establish it instead of inheriting whatever
        // payload an earlier test left in module state.
        commitStatusPayload({ daemon: { state: 'stale', engine_version: '3.3.13', runtime: {} },
                              reads: { catalog: 'not_read', accounts: 'not_read', quota: 'not_read' } });
        await wakeDaemon({ fetchImpl: async () => ({ ok: false, status: 503, json: async () => ({ error: 'claudexord_not_installed: no binary' }) }) });
        assert.match(lines.at(-1), /Could not start the agent daemon/i);
        assert.match(lines.at(-1), /claudexord_not_installed/);
    } finally {
        globalThis.document = previousDocument;
    }
});
