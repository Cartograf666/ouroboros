// The first-run Agents step (phase 3C). What is asserted here is what the
// owner is PROMISED: the ladder's honesty, the rotation artwork's inertness,
// what zero / one / several connected accounts declare to the completion
// endpoint, and that the step holds nothing open after it is disposed.

import assert from 'node:assert/strict';
import test from 'node:test';

import { createClaudexorStatusStore } from '../modules/claudexor_status_store.js';
import {
    AGENT_FAMILIES,
    LADDER_FOOTNOTE,
    MALFORMED_RECEIPT_CODE,
    VALUE_LADDER,
    agentsOutcomeText,
    agentsStepHtml,
    completionFailureNotice,
    connectedHarnesses,
    createAgentsStep,
    familyListHtml,
    familyStatusText,
    ladderHtml,
    readCompletionAnswer,
    rotationDiagramSvg,
    subscriptionDeclaration,
} from '../modules/onboarding_agents_step.js';

const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
const flush = async () => { for (let i = 0; i < 40; i += 1) await Promise.resolve(); };

function snapshotWith(harnesses) {
    // Shaped like the producer's own answer. `quota` is UNCONDITIONAL there —
    // `_status_payload` sets daemon/harnesses/profiles/quota before it reaches
    // the daemon at all — and the shared store requires all four before it will
    // derive a facet from a 2xx body (a 200 carrying an unrelated object used to
    // sail through as an authoritative empty world). A fixture missing one of
    // them is not a legacy wire; it is a body the real endpoint never sends.
    return {
        daemon: { state: 'running' },
        reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' },
        harnesses: [{ id: 'claude' }, { id: 'codex' }, { id: 'cursor' }],
        profiles: {
            harnessAccounts: harnesses.map((harness) => ({
                harness_id: harness, native_login_detected: true,
            })),
            profiles: [],
        },
        quota: [],
    };
}

// ---------------------------------------------------------------------------
// The ladder.
// ---------------------------------------------------------------------------

test('the ladder is three rungs and states the launch gate honestly', () => {
    assert.equal(VALUE_LADDER.length, 3);

    const [runs, better, best] = VALUE_LADDER;
    // Rung 1: the access step ALREADY satisfied the requirement.
    assert.match(runs.title, /API key/i);
    assert.match(runs.body, /Ouroboros runs/i);
    // Rung 2: the benefit and the D-1 limit in the same breath — a plan moves
    // delegated work and commit review, and CANNOT run the main agent.
    assert.match(better.body, /delegated subagents/i);
    assert.match(better.body, /commit review/i);
    assert.match(better.body, /main\s+agent keeps using the API key or local model/i);
    assert.match(better.body, /a plan cannot run it/i);
    // Rung 3: rotation, in the owner's own terms.
    assert.match(best.body, /rotate/i);
    assert.match(best.body, /window is spent/i);

    // No rung may imply a subscription is what starts Ouroboros.
    for (const rung of VALUE_LADDER) {
        assert.doesNotMatch(rung.body, /(subscription|plan) (alone )?(is enough|starts Ouroboros)/i);
    }
});

test('the footnote refuses both easy lies: "free", and "every reviewer moves"', () => {
    assert.match(LADDER_FOOTNOTE, /not free/i);
    assert.match(LADDER_FOOTNOTE, /already\s+pay for/i);
    // The surfaces that stay on the API key are NAMED (D15), not glossed over.
    assert.match(LADDER_FOOTNOTE, /Plan review, task acceptance and skill review/i);
    assert.match(LADDER_FOOTNOTE, /stay on the API key/i);
    assert.doesNotMatch(LADDER_FOOTNOTE, /all reviewers|every reviewer/i);
});

test('the step renders the ladder, one row per family, and blocks nothing', () => {
    const html = agentsStepHtml();
    const rows = familyListHtml(snapshotWith([]));

    for (const rung of VALUE_LADDER) assert.ok(html.includes(rung.title), rung.title);
    for (const family of AGENT_FAMILIES) {
        assert.ok(rows.includes(family.label), family.label);
        assert.ok(rows.includes(`data-agent-connect="${family.harness}"`), family.harness);
    }
    assert.ok(html.includes('id="agents-login-host"'));
    assert.ok(html.includes('id="agents-outcome"'));
    // SKIPPABLE: the step owns no input at all, so nothing on it can be
    // required, invalid, or in the way of Continue.
    assert.doesNotMatch(html + rows, /<input|required/);
});

// ---------------------------------------------------------------------------
// The rotation artwork.
// ---------------------------------------------------------------------------

test('the rotation diagram is inert artwork: no script, no animation, aria-hidden', () => {
    const svg = rotationDiagramSvg();

    assert.match(svg, /aria-hidden="true"/);
    assert.match(svg, /focusable="false"/);
    assert.match(svg, /role="presentation"/);
    assert.doesNotMatch(svg, /<script|<foreignObject|<animate|<set\b/i);
    // No event handlers and no external references of any kind.
    assert.doesNotMatch(svg, /\son[a-z]+=/i);
    assert.doesNotMatch(svg, /https?:\/\//);
    // Colour and size come from CSS classes so the figure inherits the theme;
    // the only url() is the local arrow marker.
    assert.doesNotMatch(svg, /\sfill="(?!none)/);
    assert.doesNotMatch(svg, /\sstroke="/);
    assert.doesNotMatch(svg, /font-size=/);

    // The three things it must draw for the loop to read at a glance.
    assert.match(svg, /API key or local model/);
    assert.match(svg, /runs the main agent/);
    assert.match(svg, /Agent plans/);
    assert.match(svg, /one window spent/);
    assert.match(svg, /the next takes over/);
});

test('the ladder text survives on its own — the artwork carries no unique fact', () => {
    const html = ladderHtml();
    for (const rung of VALUE_LADDER) assert.ok(html.includes(rung.title), rung.title);
    // Everything the figure says is also in the prose beside it, which is what
    // the short-viewport rule keeps when it drops the figure.
    assert.match(html, /rotate/i);
    assert.match(html, /window is spent/i);
});

// ---------------------------------------------------------------------------
// Zero / one / several connected accounts.
// ---------------------------------------------------------------------------

test('nothing connected: no declaration, and the outcome says so plainly', () => {
    const snapshot = snapshotWith([]);
    assert.deepEqual(connectedHarnesses(snapshot), []);
    assert.deepEqual(subscriptionDeclaration({ connected: [] }), {
        subscriptionsConnected: false, skipSubscriptionPresets: false,
    });
    const text = agentsOutcomeText([]);
    assert.match(text, /No agent account connected/i);
    assert.match(text, /Settings → Agents/);
});

test('one connected account declares the preset request and promises nothing certain', () => {
    const snapshot = snapshotWith(['claude']);
    assert.deepEqual(connectedHarnesses(snapshot), ['claude']);
    assert.deepEqual(subscriptionDeclaration({ connected: ['claude'] }), {
        subscriptionsConnected: true, skipSubscriptionPresets: false,
    });

    const text = agentsOutcomeText(['claude']);
    assert.match(text, /Claude Code is connected/);
    assert.match(text, /commit review/);
    assert.match(text, /delegated subagents/);
    // Conditional by construction: the compiler may still refuse a seat.
    assert.match(text, /will try to/);
    assert.match(text, /nothing is changed/);
    assert.doesNotMatch(text, /guarantee|always/i);
});

test('several accounts are named in family order and the rows say they rotate', () => {
    const snapshot = snapshotWith(['cursor', 'claude']);
    assert.deepEqual(connectedHarnesses(snapshot), ['claude', 'cursor']);
    assert.match(agentsOutcomeText(['claude', 'cursor']), /Claude Code and Cursor are connected/);

    // Two accounts in ONE family is the rotation case the owner asked about.
    const twoInOne = snapshotWith([]);
    twoInOne.profiles.profiles = [
        { profile: { harness_id: 'codex', profile_id: 'a', enabled: true }, status: { verification: 'passed' } },
        { profile: { harness_id: 'codex', profile_id: 'b', enabled: true }, status: { verification: 'passed' } },
    ];
    assert.deepEqual(familyStatusText(twoInOne, 'codex'), {
        tone: 'ok', text: '2 accounts connected · they rotate',
    });
    assert.deepEqual(familyStatusText(twoInOne, 'claude'), { tone: 'muted', text: 'Not connected' });
});

test('a family the engine renames is spoken in the engine words, never as a raw id', () => {
    // The step used to keep its OWN map of three families and fall through to
    // the harness id, while the Agents tab preferred the engine's display_name.
    // Two authorities is how an owner ends up reading "claude" in a sentence.
    // Both now go through the store's `familyLabel`, so a renamed family — or a
    // fourth one the engine adds — reaches this text spelled properly.
    const renamed = snapshotWith(['claude']);
    renamed.harnesses = [{ id: 'claude', display_name: 'Claude Code Max' },
                         { id: 'codex' }, { id: 'cursor' }];
    const text = agentsOutcomeText(['claude'], { snapshot: renamed });
    assert.match(text, /Claude Code Max is connected/);
    assert.doesNotMatch(text, /\bclaude\b/);

    // A family with no product name of its own is still never printed raw...
    const fourth = snapshotWith([]);
    fourth.harnesses = [{ id: 'gemini_cli', display_name: 'Gemini CLI' }];
    assert.match(agentsOutcomeText(['gemini_cli'], { snapshot: fourth }),
                 /Gemini CLI is connected/);

    // ...and with no payload at all the bootstrap product names still apply,
    // which is exactly what every surface printed before the two merged.
    assert.match(agentsOutcomeText(['claude', 'cursor']), /Claude Code and Cursor are connected/);
});

test('an unread account facet claims nothing — a gap is not a zero', () => {
    const rows = familyListHtml(snapshotWith(['claude']), { accountsKnown: false });
    assert.ok(rows.includes('Not checked'));
    assert.doesNotMatch(rows, /Not connected/);
    assert.match(agentsOutcomeText([], { accountsKnown: false }), /could not be checked/i);
});

test('the owner skip produces a declaration that asks for NO preset', () => {
    assert.deepEqual(subscriptionDeclaration({ connected: ['claude', 'codex'], skipPresets: true }), {
        subscriptionsConnected: true, skipSubscriptionPresets: true,
    });
    const text = agentsOutcomeText(['claude'], { skipPresets: true });
    assert.match(text, /finish without agent defaults/i);
    assert.match(text, /stay on your API access/i);
});

// ---------------------------------------------------------------------------
// A typed completion failure.
// ---------------------------------------------------------------------------

test('a typed refusal keeps its real reason and offers the escape it was given', () => {
    const error = new Error('The agent accounts were connected, but their models could not be verified right now, so nothing was saved.');
    error.code = 'daemon_unavailable';
    error.detail = 'The agent engine is unreachable (connect_failed: boom)';
    error.canSkip = true;

    const notice = completionFailureNotice(error);
    assert.equal(notice.code, 'daemon_unavailable');
    assert.equal(notice.canSkip, true);
    // BOTH halves reach the owner: the constant sentence AND the engine's own.
    assert.match(notice.text, /could not be verified/);
    assert.match(notice.text, /connect_failed: boom/);
});

test('an untyped failure is not dressed up as a skippable preset problem', () => {
    const notice = completionFailureNotice(new Error('HTTP 500'));
    assert.equal(notice.canSkip, false);
    assert.equal(notice.saved, false);
    assert.equal(notice.text, 'HTTP 500');
});

test('a failure AFTER the bytes reached disk never claims nothing was saved', () => {
    // The endpoint distinguishes a refusal (nothing persisted) from a failure
    // in a post-commit stage. Reporting the second as "nothing was saved" would
    // repeat, one layer up, the exact dishonesty the atomic write removed — and
    // would send the owner back to re-enter settings that already exist.
    const error = new Error('Onboarding completion failed.');
    error.saved = true;
    error.stage = 'supervisor_start';
    error.canSkip = true;

    const notice = completionFailureNotice(error);
    assert.equal(notice.saved, true);
    assert.match(notice.text, /settings WERE written/i);
    assert.match(notice.text, /supervisor_start/);
    assert.doesNotMatch(notice.text, /nothing was saved/i);
    // And the escape hatch is withdrawn: with bytes on disk, "finish without
    // agent defaults" would be a SECOND write, not an alternative to the first.
    assert.equal(notice.canSkip, false);

    // The `stage` above was hand-built, so it proved the PROSE and not the
    // reader. This is the envelope `post_commit_failure_response` really sends
    // — the field is named `post_commit_failed`, and the reader used to look
    // for `stage`, so a genuine post-commit failure reached the owner with the
    // one word identifying the failed step silently blanked.
    const real = readCompletionAnswer({
        status: 500,
        ok: false,
        parsed: true,
        data: {
            error: 'Settings were saved to disk, but the supervisor start step failed afterwards: RuntimeError: boom',
            status: 'saved_with_post_commit_error',
            saved: true,
            post_commit_failed: 'supervisor start',
        },
    });
    assert.equal(real.failure.stage, 'supervisor start');
    assert.equal(real.failure.saved, true);
    assert.match(completionFailureNotice(real.failure).text, /supervisor start/);
});

// ---------------------------------------------------------------------------
// Reading the completion answer.
// ---------------------------------------------------------------------------

test('a 2xx without the success envelope is a failure, not a completion', () => {
    // Everything downstream reads this body: the saved runtime mode and whether
    // it needs a restart. A shape-blind `ok` announced a finished setup while
    // silently discarding both — and an unparseable body used to become `{}`,
    // which is truthy.
    const bad = [
        { status: 200, ok: true, parsed: false, data: null },                  // HTML / empty
        { status: 200, ok: true, parsed: true, data: {} },                     // no envelope
        { status: 200, ok: true, parsed: true, data: { ok: false } },          // explicit failure
        { status: 200, ok: true, parsed: true, data: { ok: true } },           // no receipt fields
        { status: 200, ok: true, parsed: true, data: { ok: true, runtime_mode: 'pro' } },
        { status: 200, ok: true, parsed: true, data: { ok: true, restart_required: true } },
    ];
    for (const answer of bad) {
        const read = readCompletionAnswer(answer);
        assert.ok(read.failure, JSON.stringify(answer));
        assert.equal(read.failure.code, MALFORMED_RECEIPT_CODE);
        assert.equal(read.failure.canSkip, false);
        assert.match(read.failure.message, /not confirmed/i);
    }

    const good = readCompletionAnswer({
        status: 200, ok: true, parsed: true,
        data: { ok: true, status: 'saved', runtime_mode: 'pro', restart_required: true, preset: {} },
    });
    assert.ok(good.receipt);
    assert.equal(good.receipt.restart_required, true);
    assert.equal(good.receipt.runtime_mode, 'pro');
});

test('a typed refusal keeps every field the wizard renders', () => {
    const read = readCompletionAnswer({
        status: 503, ok: false, parsed: true,
        data: {
            error: 'models could not be verified', code: 'daemon_unavailable',
            detail: 'engine unreachable', can_skip: true, saved: false,
        },
    });
    assert.deepEqual(read.failure, {
        message: 'models could not be verified', status: 503, code: 'daemon_unavailable',
        detail: 'engine unreachable', canSkip: true, saved: false, stage: '',
    });
});

// ---------------------------------------------------------------------------
// The controller: it reads the SHARED store, and releases everything.
// ---------------------------------------------------------------------------

function fakeDom() {
    const documentListeners = [];
    const windowListeners = [];
    const nodes = new Map();
    const make = (id) => {
        const node = {
            id,
            innerHTML: '',
            textContent: '',
            hidden: false,
            dataset: {},
            contains: () => false,
            querySelector: () => null,
            querySelectorAll: (selector) => (
                node.id === 'agents-family-list' && selector === '[data-agent-connect]'
                    ? node.buttons
                    : []
            ),
            buttons: [],
        };
        return node;
    };
    for (const id of ['agents-family-list', 'agents-status-note', 'agents-outcome', 'agents-login-host']) {
        nodes.set(id, make(id));
    }
    const defaultView = {
        addEventListener: (type, fn) => windowListeners.push([type, fn]),
        removeEventListener: (type, fn) => {
            const idx = windowListeners.findIndex(([t, f]) => t === type && f === fn);
            if (idx >= 0) windowListeners.splice(idx, 1);
        },
    };
    return {
        nodes,
        documentListeners,
        windowListeners,
        doc: {
            hidden: false,
            activeElement: null,
            defaultView,
            getElementById: (id) => nodes.get(id) || null,
            addEventListener: (type, fn) => documentListeners.push([type, fn]),
            removeEventListener: (type, fn) => {
                const idx = documentListeners.findIndex(([t, f]) => t === type && f === fn);
                if (idx >= 0) documentListeners.splice(idx, 1);
            },
        },
    };
}

test('the step reads the shared store — it never fetches the status endpoint itself', async () => {
    const urls = [];
    const store = createClaudexorStatusStore({
        fetchImpl: async (url) => { urls.push(url); return json(200, snapshotWith(['codex'])); },
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
        pollMs: 5000,
    });
    const dom = fakeDom();
    const seen = [];
    const step = createAgentsStep({ doc: dom.doc, store, onChange: (c) => seen.push(c) });

    step.mount();
    await flush();

    // ONE read, through the store's own endpoint — no second reader.
    assert.deepEqual(urls, ['/api/claudexor/status']);
    assert.deepEqual(step.connected, ['codex']);
    assert.deepEqual(seen, [['codex']]);
    assert.deepEqual(step.declaration(), {
        subscriptionsConnected: true, skipSubscriptionPresets: false,
    });
    assert.ok(dom.nodes.get('agents-family-list').innerHTML.includes('Codex'));
    assert.match(dom.nodes.get('agents-outcome').textContent, /Codex is connected/);

    assert.equal(await step.dispose(), 'released');
    step.detach();
    assert.equal(store.subscriberCount, 0);
    assert.equal(dom.documentListeners.length, 0, 'pagehide must never bind to Document');
    assert.equal(dom.windowListeners.length, 0, 'the step must leave no Window listener behind');
    store.dispose();
});

test('Connect starts the login through the shared card controller', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const calls = [];
    const fetchImpl = async (url, init) => {
        calls.push([String(url), init?.method || 'GET']);
        if (String(url).startsWith('/api/claudexor/login') && (init?.method || 'GET') === 'DELETE') {
            return json(200, { job: { state: 'cancelled' } });
        }
        if (String(url).startsWith('/api/claudexor/login')) {
            return json(200, { job_id: 'j1', job: { state: 'running' } });
        }
        return json(200, snapshotWith([]));
    };
    const store = createClaudexorStatusStore({
        fetchImpl,
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
        pollMs: 5000,
    });
    const dom = fakeDom();
    const list = dom.nodes.get('agents-family-list');
    const handlers = [];
    list.buttons = [{
        getAttribute: () => 'claude',
        addEventListener: (_type, fn) => handlers.push(fn),
    }];

    const step = createAgentsStep({ doc: dom.doc, store, fetchImpl });
    step.mount();
    await flush();

    assert.ok(handlers.length >= 1, 'every family row wires its own Connect');
    handlers[handlers.length - 1]();
    await flush();

    assert.ok(calls.some(([url, method]) => url === '/api/claudexor/login' && method === 'POST'));
    // The login card renders into the step's own host, never a second surface.
    assert.match(dom.nodes.get('agents-login-host').innerHTML, /harness-login-card/);
    await step.dispose();
    step.detach();
    store.dispose();
});

test('unknown sign-in cleanup stays retryable until explicit local detach', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    let cancels = 0;
    const fetchImpl = async (url, init) => {
        const u = String(url);
        const method = init?.method || 'GET';
        if (u.startsWith('/api/claudexor/login') && method === 'POST') {
            return json(200, { job_id: 'j1', job: { state: 'running' } });
        }
        if (u.startsWith('/api/claudexor/login') && method === 'DELETE') {
            cancels += 1;
            return json(503, { error: 'daemon unreachable' });   // never proven gone
        }
        if (u.startsWith('/api/claudexor/login')) return json(200, { job: { state: 'running' } });
        return json(200, snapshotWith([]));
    };
    const store = createClaudexorStatusStore({
        fetchImpl,
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
        pollMs: 5000,
    });
    const dom = fakeDom();
    const handlers = [];
    dom.nodes.get('agents-family-list').buttons = [{
        getAttribute: () => 'claude',
        addEventListener: (_type, fn) => handlers.push(fn),
    }];

    const step = createAgentsStep({ doc: dom.doc, store, fetchImpl });
    step.mount();
    await flush();
    handlers[handlers.length - 1]();
    await flush();

    assert.equal(await step.dispose(), 'unknown');
    assert.ok(cancels >= 1, 'the disposer must actually attempt the cancel');

    // Unknown transport remains retryable against the same attached job.
    const before = cancels;
    assert.equal(await step.dispose(), 'unknown');
    assert.ok(cancels > before, 'unknown cleanup must stay retryable');

    step.detach();
    const detachedAt = cancels;
    assert.equal(await step.dispose(), 'unknown', 'detach must not fabricate release proof');
    assert.equal(cancels, detachedAt, 'local detach initiates no further cancel');

    store.dispose();
});

test('terminal-unconfirmed cleanup is retained without repeating cancel', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    let cancels = 0;
    const fetchImpl = async (url, init) => {
        const u = String(url);
        const method = init?.method || 'GET';
        if (u === '/api/claudexor/login' && method === 'POST') {
            return json(200, { job_id: 'j1', job: { state: 'running' } });
        }
        if (u.startsWith('/api/claudexor/login') && method === 'DELETE') {
            cancels += 1;
            return json(200, {
                job: { state: 'failed', outcome: { reason: 'termination_unconfirmed' } },
            });
        }
        if (u.startsWith('/api/claudexor/login')) return json(200, { job: { state: 'running' } });
        return json(200, snapshotWith([]));
    };
    const store = createClaudexorStatusStore({
        fetchImpl,
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
        pollMs: 5000,
    });
    const dom = fakeDom();
    const handlers = [];
    dom.nodes.get('agents-family-list').buttons = [{
        getAttribute: () => 'claude',
        addEventListener: (_type, fn) => handlers.push(fn),
    }];
    const step = createAgentsStep({ doc: dom.doc, store, fetchImpl });
    step.mount();
    await flush();
    handlers[handlers.length - 1]();
    await flush();

    assert.equal(await step.dispose(), 'retained');
    assert.equal(cancels, 1);
    assert.equal(await step.dispose(), 'retained');
    assert.equal(cancels, 1, 'known retained custody must not repeat a pointless cancel');
    step.detach();

    store.dispose();
});

test('pagehide binds to Window, preserves bfcache, and detaches late work synchronously', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const calls = [];
    let resolveCreate;
    const createResponse = new Promise((resolve) => { resolveCreate = resolve; });
    const fetchImpl = async (url, init) => {
        const method = init?.method || 'GET';
        calls.push([String(url), method]);
        if (String(url) === '/api/claudexor/login' && method === 'POST') return createResponse;
        if (String(url).startsWith('/api/claudexor/login')) {
            return json(200, { job: { state: 'running' } });
        }
        return json(200, snapshotWith([]));
    };
    const store = createClaudexorStatusStore({
        fetchImpl,
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
        pollMs: 5000,
    });
    const dom = fakeDom();
    const handlers = [];
    dom.nodes.get('agents-family-list').buttons = [{
        getAttribute: () => 'claude',
        addEventListener: (_type, fn) => handlers.push(fn),
    }];
    const step = createAgentsStep({ doc: dom.doc, store, fetchImpl });
    step.mount();
    await flush();

    assert.deepEqual(dom.documentListeners, [], 'Document is not the pagehide target');
    assert.equal(dom.windowListeners.length, 1);
    const [type, onPageHide] = dom.windowListeners[0];
    assert.equal(type, 'pagehide');
    handlers[handlers.length - 1]();
    await flush();

    onPageHide({ persisted: true });
    assert.equal(dom.windowListeners.length, 1, 'bfcache keeps the live step mounted');

    const lifecycleBefore = calls.filter(([url]) => url.startsWith('/api/claudexor/login')).length;
    onPageHide({ persisted: false });
    assert.equal(dom.nodes.get('agents-login-host').innerHTML, '', 'detach clears the card synchronously');
    assert.equal(dom.windowListeners.length, 0, 'detach removes the exact captured Window listener');
    assert.equal(
        calls.filter(([url]) => url.startsWith('/api/claudexor/login')).length,
        lifecycleBefore,
        'departure initiates no create, cancel, or reconcile request',
    );
    handlers[handlers.length - 1]();
    await flush();
    assert.equal(
        calls.filter(([url]) => url.startsWith('/api/claudexor/login')).length,
        lifecycleBefore,
        'an old Connect handler cannot recreate login work after detach',
    );

    resolveCreate(json(200, { job_id: 'j1', job: { state: 'running' } }));
    await flush();
    assert.equal(dom.nodes.get('agents-login-host').innerHTML, '', 'late create cannot repaint after detach');
    assert.equal(
        calls.filter(([url, method]) => url.startsWith('/api/claudexor/login') && method === 'GET').length,
        0,
        'late create cannot arm polling after detach',
    );
    store.dispose();
});

test('the skip choice is reflected in the outcome the owner reads before finishing', async () => {
    const store = createClaudexorStatusStore({
        fetchImpl: async () => json(200, snapshotWith(['claude'])),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
        pollMs: 5000,
    });
    const dom = fakeDom();
    const step = createAgentsStep({ doc: dom.doc, store });
    step.mount();
    await flush();

    step.setSkipPresets(true);
    assert.match(dom.nodes.get('agents-outcome').textContent, /finish without agent defaults/i);
    assert.deepEqual(step.declaration(), {
        subscriptionsConnected: true, skipSubscriptionPresets: true,
    });
    step.detach();
    store.dispose();
});
