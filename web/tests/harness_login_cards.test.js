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
    JOB_POLL_GIVE_UP_FAILURES,
    LOGIN_CUSTODY_RELEASED,
    LOGIN_CUSTODY_RETAINED,
    LOGIN_CUSTODY_UNKNOWN,
    LOGIN_CARD_COMPACT,
    cancelLoginJob,
    createLoginCardController,
    loginCardHtml,
    loginReleaseProven,
    reconcileLoginJob,
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
    // The producer's unconditional shape (daemon/harnesses/profiles/quota) —
    // the store refuses to derive facets from anything less.
    return {
        daemon: { state: 'running', engine_version: '3.3.13', runtime: {} },
        config_dir: '/home/agent',
        harnesses: [{ id: 'codex' }],
        profiles: {
            harnessAccounts: [{ harness_id: 'codex', native_login_detected: loggedIn }],
            profiles: [],
        },
        quota: [],
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

test('poll replaces the whole canonical envelope, preserving envelope-level device disclosure', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
    });
    const host = fakeHost();
    const ctl = createLoginCardController({
        host, store,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return json(200, { job_id: 'job-device', job: { state: 'running' },
                    attach_command: 'claudexor setup attach job-device', disclosure_native: false });
            }
            if (init.method === 'DELETE') return json(200, { job: { state: 'cancelled' } });
            return json(200, {
                job: { state: 'waiting_for_input', phase: 'awaiting_user' },
                cursor: 'c1', sequence: 2,
                deviceCode: { flow: 'chatgptDeviceCode',
                    verificationUrl: 'https://auth.example/device', userCode: 'ABCD-1234' },
            });
        },
    });
    await ctl.start('codex', '');
    t.mock.timers.tick(3000);
    await flush();
    assert.equal(ctl.active?.envelope?.sequence, 2);
    assert.equal(ctl.active?.attachCommand, 'claudexor setup attach job-device',
        'replaceable poll envelope must not erase create-only metadata');
    assert.match(host.innerHTML, /data-open-signin/);
    assert.match(host.innerHTML, /ABCD-1234/);
    await ctl.dispose();
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

test('dispose CANCELS the live job before releasing custody, and clears the card', async (t) => {
    // It used to clear ctl.active, release the hold and return — with no DELETE
    // for a live job and the card still on screen. The wizard mounts this
    // controller on a step the owner can cancel mid-login, so an orphaned job
    // kept a sign-in running server-side for a card that no longer existed.
    t.mock.timers.enable({ apis: ['setTimeout'] });
    let jobPolls = 0;
    const calls = [];
    let custodyAtDelete = 'never issued';
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
            calls.push(`${init.method || 'GET'} ${url}`);
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return json(200, { job_id: 'job-4', job: { state: 'running' } });
            }
            if (init.method === 'DELETE') {
                custodyAtDelete = ctl.active?.jobId || '';
                return json(200, { job: { state: 'cancelled' } });
            }
            jobPolls += 1;
            return json(200, { job: { state: 'running' } });
        },
    });
    await ctl.start('codex', '');
    assert.equal(store.polling, true);
    assert.ok(host.innerHTML.includes('Connect codex'), 'the card is on screen before the disposer');

    const released = await ctl.dispose();
    assert.ok(calls.includes('DELETE /api/claudexor/login/job-4'),
        `the live job must be cancelled: ${calls.join(' | ')}`);
    assert.equal(custodyAtDelete, 'job-4', 'custody was still held WHEN the DELETE went out');
    assert.equal(released, LOGIN_CUSTODY_RELEASED, 'a proven cancel releases custody');
    assert.equal(ctl.active, null, '…and only then');
    assert.equal(store.polling, false, 'the login hold was released');
    assert.equal(host.innerHTML, '', 'the disposer cleared the rendered card');
    const before = jobPolls;
    t.mock.timers.tick(60000);
    await flush();
    assert.equal(jobPolls, before, 'no job-poll timer survived the disposer');
    ctl.render();
    assert.equal(host.innerHTML, '', 'a disposed controller renders no card');
    // Idempotent, and it does not re-DELETE.
    const deletes = calls.filter((c) => c.startsWith('DELETE')).length;
    assert.equal(await ctl.dispose(), LOGIN_CUSTODY_RELEASED);
    assert.equal(calls.filter((c) => c.startsWith('DELETE')).length, deletes);
    store.dispose();
    t.mock.timers.reset();
});

test('a dispose whose cancel is UNPROVEN keeps the job id instead of forgetting it', async (t) => {
    // Same rule Close has always had (C7): a 5xx/network death means the daemon
    // may still be running the login, so custody is retained and the caller is
    // told so — the surface goes away, the honesty does not.
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
        pollMs: 5000,
    });
    store.subscribe(() => {}, { visible: () => false });
    const host = fakeHost();
    let jobPolls = 0;
    const ctl = createLoginCardController({
        host,
        store,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return json(200, { job_id: 'job-5', job: { state: 'running' } });
            }
            if (init.method === 'DELETE') return json(503, { error: 'daemon unreachable' });
            jobPolls += 1;
            return json(200, { job: { state: 'running' } });
        },
    });
    await ctl.start('codex', '');

    const released = await ctl.dispose();
    assert.equal(released, LOGIN_CUSTODY_UNKNOWN, 'an unproven cancel is reported, not swallowed');
    assert.equal(ctl.active?.jobId, 'job-5', 'the job id is RETAINED — it may still be live');
    assert.equal(host.innerHTML, '', 'the host is cleared either way');
    assert.equal(store.polling, false, 'and nothing stays armed');
    const before = jobPolls;
    t.mock.timers.tick(60000);
    await flush();
    assert.equal(jobPolls, before);
    ctl.detach();
    store.dispose();
    t.mock.timers.reset();
});

test('a Close during the create POST cancels the job that POST installs', async (t) => {
    // The busy guard DROPPED a transition that arrived while another ran, so
    // Close answered false and vanished; the create then installed a live job
    // and no DELETE was ever issued. Transitions queue now: the close is
    // applied to the job it could not see yet.
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const calls = [];
    let custodyAtDelete = 'never issued';
    let releaseCreate = null;
    const createGate = new Promise((resolve) => { releaseCreate = resolve; });
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
        pollMs: 5000,
    });
    const host = fakeHost();
    const ctl = createLoginCardController({
        host,
        store,
        fetchImpl: async (url, init = {}) => {
            calls.push(`${init.method || 'GET'} ${url}`);
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                await createGate;
                return json(200, { job_id: 'job-after-close', job: { state: 'running' } });
            }
            if (init.method === 'DELETE') {
                custodyAtDelete = ctl.active?.jobId || '';
                return json(200, { job: { state: 'cancelled' } });
            }
            return json(200, { job: { state: 'running' } });
        },
    });
    const starting = ctl.start('codex', '');
    const closing = ctl.close();          // pressed while the create is in flight
    releaseCreate();
    await starting;
    const closed = await closing;

    assert.equal(closed, LOGIN_CUSTODY_RELEASED, 'the close RAN — it is queued, never dropped');
    assert.ok(calls.includes('DELETE /api/claudexor/login/job-after-close'),
        `the job the create installed must be cancelled: ${calls.join(' | ')}`);
    assert.ok(calls.indexOf('DELETE /api/claudexor/login/job-after-close')
        > calls.indexOf('POST /api/claudexor/login'), 'the cancel follows the create');
    assert.equal(custodyAtDelete, 'job-after-close', 'custody was held WHEN the DELETE went out');
    assert.equal(ctl.active, null, 'and released only after it was proven gone');
    assert.equal(host.innerHTML, '', 'no card left behind');
    ctl.dispose();
    store.dispose();
    t.mock.timers.reset();
});

test('dispose queued during create adopts a returned fence without repeating cancel', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    let releaseCreate;
    const gate = new Promise((resolve) => { releaseCreate = resolve; });
    let createStarted = false;
    let deletes = 0;
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
    });
    const host = fakeHost();
    const ctl = createLoginCardController({
        host, store,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                createStarted = true;
                await gate;
                return json(200, { job_id: 'job-create-fence', job: {
                    state: 'interrupted_unknown', outcome: { reason: 'termination_unconfirmed' },
                } });
            }
            if (init.method === 'DELETE') { deletes += 1; return json(503, {}); }
            throw new Error(`unexpected ${url}`);
        },
    });
    const starting = ctl.start('codex', '');
    await flush();
    assert.equal(createStarted, true);
    const disposing = ctl.dispose();
    releaseCreate();
    await starting;
    assert.equal(await disposing, LOGIN_CUSTODY_RETAINED);
    assert.equal(deletes, 0);
    assert.equal(ctl.active?.jobId, 'job-create-fence');
    assert.equal(host.innerHTML, '');
    ctl.detach();
    store.dispose();
    t.mock.timers.reset();
});

test('a 2xx create with no job id fails loudly instead of waiting forever', async (t) => {
    // A 200 carrying non-JSON left no job id, no error and a card polling
    // nothing: "Starting the sign-in…" with no verdict and no way out. A job
    // id is the minimum a created job must carry — without it there is nothing
    // to poll and nothing to cancel.
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
        pollMs: 5000,
    });
    store.subscribe(() => {}, { visible: () => false });
    const host = fakeHost();
    let polls = 0;
    const ctl = createLoginCardController({
        host,
        store,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return { ok: true, status: 200, json: async () => { throw new Error('not json'); } };
            }
            polls += 1;
            return json(200, { job: { state: 'running' } });
        },
    });
    await ctl.start('codex', '');
    assert.ok(host.innerHTML.includes('data-login-retry'), `the error face offers a retry: ${host.innerHTML}`);
    assert.ok(host.innerHTML.includes('no job id'), host.innerHTML);
    assert.equal(ctl.active.jobId, '', 'no job id was invented');
    assert.equal(store.polling, false, 'a failed create releases the status hold');
    t.mock.timers.tick(60000);
    await flush();
    assert.equal(polls, 0, 'nothing is polled for a job that was never created');
    const closing = ctl.close(ctl.active);
    assert.equal(host.innerHTML, '', 'Close remains usable without inventing a server identity');
    assert.equal(await closing, LOGIN_CUSTODY_UNKNOWN);
    assert.equal(await ctl.dispose(), LOGIN_CUSTODY_UNKNOWN);
    store.dispose();
    t.mock.timers.reset();
});

test('malformed 2xx polls count toward the SAME bounded give-up', async (t) => {
    // A 2xx carrying no `job` used to RESET the failure streak, so a stream of
    // them polled forever: twelve malformed answers still left a pending
    // verdict and another armed timer, and the documented ten-failure give-up
    // was unreachable.
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
        pollMs: 5000,
    });
    store.subscribe(() => {}, { visible: () => false });
    const host = fakeHost();
    let polls = 0;
    const ctl = createLoginCardController({
        host,
        store,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return json(200, { job_id: 'job-6', job: { state: 'running' } });
            }
            if (init.method === 'DELETE') return json(200, { job: { state: 'cancelled' } });
            polls += 1;
            return json(200, { ok: true });     // 2xx, no job — meaningless
        },
    });
    await ctl.start('codex', '');
    for (let i = 0; i < 12; i += 1) { t.mock.timers.tick(30000); await flush(); }

    assert.ok(polls >= JOB_POLL_GIVE_UP_FAILURES, `the chain really polled: ${polls}`);
    assert.ok(polls <= JOB_POLL_GIVE_UP_FAILURES, `and STOPPED at the documented bound: ${polls}`);
    assert.ok(host.innerHTML.includes('Could not confirm the sign-in yet'),
        `an honest unconfirmed verdict, not a forever-pending card: ${host.innerHTML}`);
    assert.equal(store.polling, false, 'the give-up released the status hold');
    await ctl.dispose();
    store.dispose();
    t.mock.timers.reset();
});

test('a 2xx poll carrying an EMPTY job object is a failure, not a healthy pending read', async (t) => {
    // Reachable on the real wire, not hypothetical: the gateway normalizes a
    // non-object engine reply to `{}` (gateways/claudexor.py::setup_job_call),
    // so the proxy answers {job:{}} — an object, so the old guard called it a
    // success, reset the failure streak and armed another timer. Twelve of
    // them left the verdict null and the card pending for good.
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
        pollMs: 5000,
    });
    store.subscribe(() => {}, { visible: () => false });
    const host = fakeHost();
    let polls = 0;
    const ctl = createLoginCardController({
        host,
        store,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return json(200, { job_id: 'job-7', job: { state: 'running' } });
            }
            if (init.method === 'DELETE') return json(200, { job: { state: 'cancelled' } });
            polls += 1;
            return json(200, { job: {} });          // present, and says nothing
        },
    });
    await ctl.start('codex', '');
    for (let i = 0; i < 12; i += 1) { t.mock.timers.tick(30000); await flush(); }

    assert.equal(polls, JOB_POLL_GIVE_UP_FAILURES,
        `an empty job must count toward the bounded give-up: ${polls} polls`);
    assert.ok(host.innerHTML.includes('Could not confirm the sign-in yet'),
        `and the card settles honestly instead of polling forever: ${host.innerHTML}`);
    await ctl.dispose();
    store.dispose();
    t.mock.timers.reset();
});

test('duplicate starts are COALESCED, so a double-click cannot create-cancel-create', async (t) => {
    // Serializing was not enough: the queue ran the second start AFTER the
    // first, whose C7 guard then cancelled the job the first had installed and
    // created another — so the device link the owner was reading was
    // invalidated as it appeared, and the daemon saw two creates.
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const calls = [];
    let releaseCreate = null;
    const gate = new Promise((resolve) => { releaseCreate = resolve; });
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
        pollMs: 5000,
    });
    const host = fakeHost();
    let created = 0;
    const ctl = createLoginCardController({
        host,
        store,
        fetchImpl: async (url, init = {}) => {
            calls.push(`${init.method || 'GET'} ${url}`);
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                created += 1;
                await gate;
                return json(200, { job_id: `job-${created}`, job: { state: 'running' } });
            }
            if (init.method === 'DELETE') return json(200, { job: { state: 'cancelled' } });
            return json(200, { job: { state: 'running' } });
        },
    });
    const first = ctl.start('codex', '');
    const second = ctl.start('codex', '');           // the second click
    assert.equal(first, second, 'the same pending start is shared, not queued behind itself');
    releaseCreate();
    await Promise.all([first, second]);

    assert.equal(created, 1, `exactly one job was created: ${calls.join(' | ')}`);
    assert.equal(calls.filter((c) => c.startsWith('DELETE')).length, 0,
        'and nothing was cancelled to make room for a duplicate');
    assert.equal(ctl.active.jobId, 'job-1');

    // A DIFFERENT account is not the same start, and once a start has settled
    // the next one is a real (guarded) restart — coalescing is not caching.
    await ctl.start('codex', 'work');
    assert.equal(created, 2);
    assert.ok(calls.includes('DELETE /api/claudexor/login/job-1'), 'the C7 guard still runs');
    await ctl.dispose();
    store.dispose();
    t.mock.timers.reset();
});

test('an unknown dispose can retry cancellation against the same retained job id', async (t) => {
    // The verdict is only useful if the caller can act on it. A retained job
    // must stay cancellable — otherwise a host that refuses to remount while
    // custody is held is stuck forever.
    t.mock.timers.enable({ apis: ['setTimeout'] });
    let deleteStatus = 503;
    const deletes = [];
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
        pollMs: 5000,
    });
    const host = fakeHost();
    const ctl = createLoginCardController({
        host,
        store,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return json(200, { job_id: 'job-8', job: { state: 'running' } });
            }
            if (init.method === 'DELETE') {
                deletes.push(url);
                return deleteStatus === 200
                    ? json(200, { job: { state: 'cancelled' } })
                    : json(deleteStatus, {});
            }
            return json(200, { job: { state: 'running' } });
        },
    });
    await ctl.start('codex', '');

    assert.equal(await ctl.dispose(), LOGIN_CUSTODY_UNKNOWN, 'the daemon refusal leaves custody unknown');
    assert.equal(ctl.active?.jobId, 'job-8');
    assert.equal(deletes.length, 1);

    // The retry re-runs the SAME proven-cancel path — it used to answer a
    // permanent `false` off the idempotence branch and never try again.
    assert.equal(await ctl.dispose(), LOGIN_CUSTODY_UNKNOWN, 'still refused, still honest');
    assert.equal(deletes.length, 2, 'and it really re-attempted the cancel');
    deleteStatus = 200;
    assert.equal(await ctl.dispose(), LOGIN_CUSTODY_RELEASED, 'the daemon answers, custody is released');
    assert.equal(ctl.active, null);
    assert.equal(deletes.length, 3);
    // …and now it is idempotent again: nothing left to cancel.
    assert.equal(await ctl.dispose(), LOGIN_CUSTODY_RELEASED);
    assert.equal(deletes.length, 3);
    store.dispose();
    t.mock.timers.reset();
});

test('first active Close retains terminal-unconfirmed custody; second recovery Close detaches synchronously', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const calls = [];
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
    });
    const host = fakeHost();
    const ctl = createLoginCardController({
        host, store,
        fetchImpl: async (url, init = {}) => {
            calls.push(`${init.method || 'GET'} ${url}`);
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return json(200, { job_id: 'job-recovery', job: { state: 'running' } });
            }
            if (init.method === 'DELETE') return json(200, { job: {
                state: 'interrupted_unknown', outcome: { reason: 'termination_unconfirmed' },
            } });
            return json(200, { job: { state: 'running' } });
        },
    });
    await ctl.start('codex', '');

    assert.equal(await ctl.close(), LOGIN_CUSTODY_RETAINED);
    assert.equal(calls.filter((call) => call.startsWith('DELETE ')).length, 1);
    assert.equal(ctl.active?.jobId, 'job-recovery');
    assert.match(host.innerHTML, /data-login-reconcile/);

    const before = calls.length;
    const second = ctl.close(ctl.active);
    assert.equal(host.innerHTML, '', 'the recovery-face Close hides synchronously');
    assert.equal(ctl.active, null);
    assert.equal(ctl.disposed, true);
    assert.equal(calls.length, before, 'local detach starts no lifecycle HTTP');
    assert.equal(await second, LOGIN_CUSTODY_RETAINED);
    assert.equal(await ctl.dispose(), LOGIN_CUSTODY_RETAINED,
        'idempotent cleanup must not relabel local detach as release proof');
    assert.equal(await ctl.close(), LOGIN_CUSTODY_RETAINED);
    await ctl.start('codex', '');
    assert.equal(calls.length, before);
    store.dispose();
    t.mock.timers.reset();
});

test('dispose on an already-visible recovery face returns retained without repeating cancel', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    let deletes = 0;
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
    });
    const host = fakeHost();
    const ctl = createLoginCardController({
        host, store,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return json(200, { job_id: 'job-fence', job: {
                    state: 'interrupted_unknown', outcome: { reason: 'termination_unconfirmed' },
                } });
            }
            if (init.method === 'DELETE') { deletes += 1; return json(503, {}); }
            throw new Error(`unexpected ${url}`);
        },
    });
    await ctl.start('codex', '');
    assert.match(host.innerHTML, /data-login-reconcile/);
    assert.equal(await ctl.dispose(), LOGIN_CUSTODY_RETAINED);
    assert.equal(deletes, 0, 'repeating cancel cannot reconcile terminal-unconfirmed custody');
    assert.equal(ctl.active?.jobId, 'job-fence');
    assert.equal(await ctl.dispose(), LOGIN_CUSTODY_RETAINED);
    assert.equal(deletes, 0);
    assert.equal(ctl.detach(), LOGIN_CUSTODY_RETAINED);
    assert.equal(await ctl.dispose(), LOGIN_CUSTODY_RETAINED);
    assert.equal(deletes, 0);
    store.dispose();
    t.mock.timers.reset();
});

test('explicit reconcile retains on 409, becomes safe on proof, and only a later retry creates', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const calls = [];
    let reconcileRound = 0;
    let creates = 0;
    const retainedJob = {
        state: 'interrupted_unknown', outcome: { reason: 'termination_unconfirmed' },
    };
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
    });
    const host = fakeHost();
    const ctl = createLoginCardController({
        host, store,
        fetchImpl: async (url, init = {}) => {
            calls.push(`${init.method || 'GET'} ${url}`);
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                creates += 1;
                return json(200, { job_id: `job-${creates}`, job: creates === 1
                    ? retainedJob : { state: 'running' } });
            }
            if (url.endsWith('/reconcile')) {
                reconcileRound += 1;
                return reconcileRound === 1
                    ? json(409, { error: 'process group is still present',
                        code: 'setup_termination_unconfirmed',
                        required_actions: ['retry_setup_reconciliation'] })
                    : json(200, { job: { ...retainedJob,
                        terminationReconciliation: { status: 'empty' } } });
            }
            if (init.method === 'DELETE') return json(200, { job: { state: 'cancelled' } });
            return json(200, { job: { state: 'running' } });
        },
    });
    await ctl.start('codex', '');
    assert.match(host.innerHTML, /data-login-reconcile/, 'a create fence lands directly in recovery');

    const first = await ctl.reconcile(ctl.active);
    assert.equal(first.status, LOGIN_CUSTODY_RETAINED);
    assert.equal(ctl.active?.jobId, 'job-1');
    assert.match(host.innerHTML, /process group is still present/);
    assert.match(host.innerHTML, /data-login-reconcile/);
    assert.equal(creates, 1, 'reconcile never creates');

    const second = await ctl.reconcile(ctl.active);
    assert.equal(second.status, LOGIN_CUSTODY_RELEASED);
    assert.match(host.innerHTML, /data-login-retry/);
    assert.match(host.innerHTML, /no longer blocking/);
    assert.equal(creates, 1, 'successful reconcile still creates nothing');

    await ctl.start('codex', '');
    assert.equal(creates, 2, 'only the later explicit retry creates exactly once');
    ctl.detach();
    store.dispose();
    t.mock.timers.reset();
});

test('an absent reconcile lands the unavailable face, whose next Close actually hides it', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const calls = [];
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
    });
    const host = fakeHost();
    const ctl = createLoginCardController({
        host, store,
        fetchImpl: async (url, init = {}) => {
            calls.push(`${init.method || 'GET'} ${url}`);
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return json(200, { job_id: 'job-gone', job: {
                    state: 'interrupted_unknown', outcome: { reason: 'termination_unconfirmed' },
                } });
            }
            if (url.endsWith('/reconcile')) return json(410, {});
            throw new Error(`unexpected ${url}`);
        },
    });
    await ctl.start('codex', '');
    const result = await ctl.reconcile(ctl.active);
    assert.equal(result.status, LOGIN_CUSTODY_RELEASED);
    assert.match(host.innerHTML, /no longer available/);
    const before = calls.length;
    assert.equal(await ctl.close(ctl.active), LOGIN_CUSTODY_RELEASED);
    assert.equal(host.innerHTML, '');
    assert.equal(calls.length, before, 'closing the informational face starts no request');
    ctl.detach();
    store.dispose();
    t.mock.timers.reset();
});

test('poll 404 settles immediately to unavailable instead of entering failure backoff', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    let polls = 0;
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
    });
    const host = fakeHost();
    const ctl = createLoginCardController({
        host, store,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return json(200, { job_id: 'job-poll-gone', job: { state: 'running' } });
            }
            polls += 1;
            return json(404, {});
        },
    });
    await ctl.start('codex', '');
    t.mock.timers.tick(3000);
    await flush();
    assert.equal(polls, 1);
    assert.match(host.innerHTML, /no longer available/);
    t.mock.timers.tick(60000);
    await flush();
    assert.equal(polls, 1, 'absent is terminal client evidence, not a retryable poll failure');
    assert.equal(await ctl.close(ctl.active), LOGIN_CUSTODY_RELEASED);
    assert.equal(host.innerHTML, '');
    ctl.detach();
    store.dispose();
    t.mock.timers.reset();
});

test('a stale in-flight GET cannot overwrite reconciled-safe state', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    let releasePoll;
    const pollGate = new Promise((resolve) => { releasePoll = resolve; });
    let pollStarted = false;
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
    });
    const host = fakeHost();
    const retainedJob = {
        state: 'interrupted_unknown', outcome: { reason: 'termination_unconfirmed' },
    };
    const ctl = createLoginCardController({
        host, store,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return json(200, { job_id: 'job-stale', job: { state: 'running' } });
            }
            if (url.endsWith('/reconcile')) return json(200, { job: { ...retainedJob,
                terminationReconciliation: { status: 'empty' } } });
            if (init.method === 'DELETE') return json(200, { job: retainedJob });
            pollStarted = true;
            await pollGate;
            return json(200, { job: { state: 'running', phase: 'awaiting_user' } });
        },
    });
    await ctl.start('codex', '');
    t.mock.timers.tick(3000);
    await flush();
    assert.equal(pollStarted, true);
    assert.equal(await ctl.close(), LOGIN_CUSTODY_RETAINED);
    assert.equal((await ctl.reconcile(ctl.active)).status, LOGIN_CUSTODY_RELEASED);
    assert.match(host.innerHTML, /no longer blocking/);
    releasePoll();
    await flush();
    assert.match(host.innerHTML, /no longer blocking/);
    assert.ok(!host.innerHTML.includes('Waiting for the sign-in link'));
    ctl.detach();
    store.dispose();
    t.mock.timers.reset();
});
test('a terminal GET overtaking an unconfirmed DELETE keeps the settled card visible', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    let releaseDelete; const deleteGate = new Promise((resolve) => { releaseDelete = resolve; });
    const calls = { create: 0, delete: 0, get: 0 }; const store = createClaudexorStatusStore({ fetchImpl: async () => json(200, statusPayload(true)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} } });
    const host = fakeHost(); const ctl = createLoginCardController({ host, store, fetchImpl: async (url, init = {}) => {
        if (url === '/api/claudexor/login' && init.method === 'POST') {
            calls.create += 1; return json(200, { job_id: 'job-delete-race', job: { state: 'running' } });
        }
            if (init.method === 'DELETE') { calls.delete += 1; await deleteGate;
                return json(503, { error: 'daemon busy' }); }
        calls.get += 1; return json(200, { job: { state: 'succeeded' } });
    } });
    await ctl.start('codex', '');
    const closing = ctl.close(ctl.active); await flush();
    assert.equal(calls.delete, 1, 'the active-card Close owns one DELETE');
    t.mock.timers.tick(3000); await flush();
    assert.match(host.innerHTML, /Connected\./);
    releaseDelete(); assert.equal(await closing, LOGIN_CUSTODY_RELEASED);
    assert.equal(ctl.active?.verdict?.kind, 'success'); assert.match(host.innerHTML, /Connected\./,
        'the late DELETE must not erase the settled face');
    assert.deepEqual(calls, { create: 1, delete: 1, get: 1 });
    t.mock.timers.tick(60000); await flush();
    assert.equal(calls.get, 1, 'a settled face never rearms job polling');
    ctl.detach(); store.dispose(); t.mock.timers.reset();
});

test('unknown dispose remains retryable while an already-flying poll continuation stays inert', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    let releasePoll;
    const pollGate = new Promise((resolve) => { releasePoll = resolve; });
    let polls = 0;
    let deletes = 0;
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, statusPayload(false)),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
    });
    const host = fakeHost();
    const ctl = createLoginCardController({
        host, store,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                return json(200, { job_id: 'job-dispose', job: { state: 'running' } });
            }
            if (init.method === 'DELETE') { deletes += 1; return json(503, { error: 'down' }); }
            polls += 1;
            await pollGate;
            return json(200, { job: { state: 'running' } });
        },
    });
    await ctl.start('codex', '');
    t.mock.timers.tick(3000);
    await flush();
    assert.equal(polls, 1);
    assert.equal(await ctl.dispose(), LOGIN_CUSTODY_UNKNOWN);
    assert.equal(ctl.disposed, true);
    assert.equal(ctl.active?.jobId, 'job-dispose');
    assert.equal(host.innerHTML, '');

    releasePoll();
    await flush();
    t.mock.timers.tick(60000);
    await flush();
    assert.equal(polls, 1, 'disposed state fences repaint and reschedule');
    assert.equal(await ctl.dispose(), LOGIN_CUSTODY_UNKNOWN);
    assert.equal(deletes, 2, 'disposed+active unknown cleanup retries the same DELETE');
    assert.equal(ctl.detach(), LOGIN_CUSTODY_UNKNOWN);
    assert.equal(await ctl.dispose(), LOGIN_CUSTODY_UNKNOWN,
        'after local detach, unknown must not become false release proof');
    assert.equal(deletes, 2);
    store.dispose();
    t.mock.timers.reset();
});

test('typed reconcile transport classifies proof, retryable conflict, malformed success and absence', async () => {
    const retained = await reconcileLoginJob('j1', async () => json(409, {
        error: 'still present', code: 'setup_termination_unconfirmed',
        required_actions: ['retry_setup_reconciliation'],
    }));
    assert.equal(retained.status, LOGIN_CUSTODY_RETAINED);
    assert.deepEqual(retained.requiredActions, ['retry_setup_reconciliation']);
    const safe = await reconcileLoginJob('j1', async () => json(200, { job: {
        state: 'interrupted_unknown', outcome: { reason: 'termination_unconfirmed' },
        terminationReconciliation: { status: 'empty' },
    } }));
    assert.equal(safe.status, LOGIN_CUSTODY_RELEASED);
    assert.equal((await reconcileLoginJob('j1', async () => json(200, {}))).status,
        LOGIN_CUSTODY_UNKNOWN);
    const malformed = await reconcileLoginJob('j1', async () => json(200, { job: {} }));
    assert.equal(malformed.status, LOGIN_CUSTODY_UNKNOWN);
    assert.equal(malformed.envelope, null, 'malformed success cannot erase the latest valid envelope');
    assert.equal((await reconcileLoginJob('j1', async () => json(200, {
        job: { state: 'cancelling' },
    }))).status, LOGIN_CUSTODY_RETAINED);
    for (const status of [404, 410]) {
        const gone = await reconcileLoginJob('j1', async () => json(status, {}));
        assert.equal(gone.status, LOGIN_CUSTODY_RELEASED);
        assert.equal(gone.absent, true);
    }
});

test('compact mode drops the terminal fallback, the paste-code entry and Close, and keeps retry', () => {
    const active = {
        harness: 'claude', profile: '', startedAtMs: 0, engineDegraded: true,
        attachCommand: 'claudexor setup attach j1', error: '', verdict: null, confirming: false,
        envelope: { job: { state: 'waiting_for_input' }, deviceCode: {
            flow: 'oauth_url_input', verificationUrl: 'https://example.test/signin', userCode: '' } },
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
