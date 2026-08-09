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
    const listeners = [];
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
    return {
        nodes,
        listeners,
        doc: {
            hidden: false,
            activeElement: null,
            getElementById: (id) => nodes.get(id) || null,
            addEventListener: (type, fn) => listeners.push([type, fn]),
            removeEventListener: (type, fn) => {
                const idx = listeners.findIndex(([t, f]) => t === type && f === fn);
                if (idx >= 0) listeners.splice(idx, 1);
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

    step.dispose();
    assert.equal(store.subscriberCount, 0);
    assert.equal(dom.listeners.length, 0, 'the step must leave no listener behind');
    store.dispose();
});

test('Connect starts the login through the shared card controller', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const calls = [];
    const fetchImpl = async (url, init) => {
        calls.push([String(url), init?.method || 'GET']);
        if (String(url).startsWith('/api/claudexor/login')) return json(200, { job_id: 'j1', job: {} });
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
    step.dispose();
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
    step.dispose();
    store.dispose();
});
