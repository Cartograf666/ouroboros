// The extracted login-card CONTROLLER (phase 2 seam). The view helpers keep
// their original assertions in harness_accounts.test.js — what is new here is
// the lifecycle the Settings section used to own privately: create → poll →
// verdict, the verify-race re-check against live account status, the store
// hold that keeps the account rows moving while a login runs, and the disposer
// that must leave nothing armed.

import assert from 'node:assert/strict';
import test from 'node:test';

import { createClaudexorStatusStore } from '../modules/claudexor_status_store.js';
import {
    LOGIN_CARD_COMPACT,
    createLoginCardController,
    loginCardHtml,
} from '../modules/harness_login_cards.js';

const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

function fakeHost() {
    return {
        innerHTML: '',
        contains: () => false,
        querySelector: () => null,
        querySelectorAll: () => [],
    };
}

function statusPayload(loggedIn) {
    return {
        daemon: { state: 'running', engine_version: '3.3.13', runtime: {} },
        harnesses: [{ id: 'codex' }],
        profiles: {
            harnessAccounts: [{ harness_id: 'codex', native_login_detected: loggedIn }],
            profiles: [],
        },
    };
}

const flush = async () => { for (let i = 0; i < 40; i += 1) await Promise.resolve(); };

test('the controller drives create → poll → Connected, and holds the status poll while it runs', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    let jobState = 'running';
    let statusReads = 0;
    const store = createClaudexorStatusStore({
        fetchImpl: async () => { statusReads += 1; return json(200, statusPayload(jobState === 'succeeded')); },
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
        pollMs: 5000,
    });
    // Off-surface subscriber: only the login hold can make this store poll.
    store.subscribe(() => {}, { visible: () => false });
    assert.equal(store.polling, false);

    const host = fakeHost();
    let settled = 0;
    const ctl = createLoginCardController({
        host,
        store,
        onSettled: () => { settled += 1; },
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return json(200, { job_id: 'job-1', job: { state: 'running', phase: 'awaiting_user' }, attach_command: '' });
            }
            if (url.startsWith('/api/claudexor/login/')) return json(200, { job: { state: jobState } });
            return json(404, {});
        },
    });

    await ctl.start('codex', '');
    assert.ok(host.innerHTML.includes('Connect codex'), 'the card rendered');
    assert.ok(host.innerHTML.includes('data-login-state'), 'a live job shows the progress line');
    assert.equal(store.polling, true, 'a live login holds the shared status poll open');

    // The 3s job poll lands a still-running snapshot, then a succeeded one.
    t.mock.timers.tick(3000);
    await flush();
    assert.ok(!host.innerHTML.includes('data-login-verdict'), 'still pending, no verdict');
    jobState = 'succeeded';
    t.mock.timers.tick(3000);
    await flush();
    assert.ok(host.innerHTML.includes('Connected.'), `verified state reached: ${host.innerHTML}`);
    assert.equal(settled, 1, 'the host was told to re-render its rows');
    assert.equal(store.polling, false, 'the settled login released the poll hold');
    assert.ok(statusReads >= 1, 'the verdict refreshed the shared status');

    ctl.dispose();
    store.dispose();
    t.mock.timers.reset();
});

test('a verify-race failure is re-checked against live account status before the card says failed', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    // codex clears its auth store when a login STARTS, so the job's own
    // verification read can say "not logged in" while the vendor login is
    // succeeding. The account rows decide, not that one stale read.
    let loggedIn = false;
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(loggedIn)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
    });
    const host = fakeHost();
    const ctl = createLoginCardController({
        host,
        store,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return json(200, { job_id: 'job-2', job: { state: 'running' } });
            }
            if (url.startsWith('/api/claudexor/login/')) {
                return json(200, { job: { state: 'failed', outcome: { reason: 'auth_not_ready' } } });
            }
            return json(404, {});
        },
    });
    await ctl.start('codex', '');
    // The account really IS logged in by the time the re-check runs.
    loggedIn = true;
    t.mock.timers.tick(3000);
    await flush();
    assert.ok(host.innerHTML.includes('Connected.'),
        `the verify-race must resolve to success, not "Sign-in failed": ${host.innerHTML}`);
    ctl.dispose();
    store.dispose();
    t.mock.timers.reset();
});

test('an unconfirmed re-check says unknown, never a hard failure', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
    });
    const host = fakeHost();
    const ctl = createLoginCardController({
        host,
        store,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return json(200, { job_id: 'job-3', job: { state: 'running' } });
            }
            if (url.startsWith('/api/claudexor/login/')) {
                return json(200, { job: { state: 'failed', outcome: { reason: 'auth_not_ready' },
                    message: 'native Codex session is not logged in' } });
            }
            return json(404, {});
        },
    });
    await ctl.start('codex', '');
    t.mock.timers.tick(3000);
    await flush();
    // The bounded re-check sleeps between its attempts; drive them all.
    assert.ok(host.innerHTML.includes('Confirming the sign-in…'), 'the in-between state is shown');
    for (let i = 0; i < 4; i += 1) { t.mock.timers.tick(2500); await flush(); }
    assert.ok(host.innerHTML.includes('Could not confirm the sign-in yet'), host.innerHTML);
    // The engine's own sentence rides beside the fixed verdict text.
    assert.ok(host.innerHTML.includes('native Codex session is not logged in'), host.innerHTML);
    assert.ok(!host.innerHTML.includes('Sign-in failed'), 'an unproven verdict is never a failure');
    ctl.dispose();
    store.dispose();
    t.mock.timers.reset();
});

test('dispose stops the job poll, releases the store hold, and clears the card', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    let jobPolls = 0;
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
        pollMs: 5000,
    });
    store.subscribe(() => {}, { visible: () => false });
    const host = fakeHost();
    const ctl = createLoginCardController({
        host,
        store,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return json(200, { job_id: 'job-4', job: { state: 'running' } });
            }
            jobPolls += 1;
            return json(200, { job: { state: 'running' } });
        },
    });
    await ctl.start('codex', '');
    assert.equal(store.polling, true);

    ctl.dispose();
    assert.equal(store.polling, false, 'the login hold was released');
    const before = jobPolls;
    t.mock.timers.tick(60000);
    await flush();
    assert.equal(jobPolls, before, 'no job-poll timer survived the disposer');
    ctl.render();
    assert.equal(host.innerHTML, '', 'a disposed controller renders no card');
    store.dispose();
    t.mock.timers.reset();
});

test('compact mode drops the terminal fallback, the paste-code entry and Close, and keeps retry', () => {
    const active = {
        harness: 'claude', profile: '', startedAtMs: 0, engineDegraded: true,
        attachCommand: 'claudexor setup attach j1', error: '', verdict: null, confirming: false,
        job: { state: 'waiting_for_input', snapshot: { disclosures: { deviceCode: {
            flow: 'oauth_url_input', verificationUrl: 'https://example.test/signin', userCode: '' } } } },
    };
    const full = loginCardHtml(active, 999999);
    assert.ok(full.includes('data-login-code-input'), 'full keeps the optional paste-code entry');
    assert.ok(full.includes('data-login-advanced'), 'full keeps the collapsed terminal fallback');
    assert.ok(full.includes('data-login-dismiss'));

    const compact = loginCardHtml(active, 999999, { mode: LOGIN_CARD_COMPACT });
    // The sign-in action itself survives — a card that cannot start the login
    // would be worse than none.
    assert.ok(compact.includes('data-open-signin'), 'compact keeps the sign-in link');
    assert.ok(compact.includes('data-login-state'), 'compact keeps the progress line');
    assert.ok(!compact.includes('data-login-code-input'));
    assert.ok(!compact.includes('data-login-advanced'));
    assert.ok(!compact.includes('data-login-dismiss'));
    assert.ok(compact.includes(`data-login-mode="${LOGIN_CARD_COMPACT}"`));

    // A settled non-success verdict offers Try again in compact (the wizard has
    // no account row behind it to retry from).
    const failed = loginCardHtml({ ...active, verdict: { kind: 'unconfirmed', reason: 'auth_not_ready' } },
        999999, { mode: LOGIN_CARD_COMPACT });
    assert.ok(failed.includes('data-login-retry'));
    assert.ok(failed.includes('Could not confirm the sign-in yet'));

    const verified = loginCardHtml({ ...active, verdict: { kind: 'success', reason: '' } },
        999999, { mode: LOGIN_CARD_COMPACT });
    assert.ok(verified.includes('Connected.'));
    assert.ok(!verified.includes('data-login-retry'), 'nothing to retry once verified');
});
