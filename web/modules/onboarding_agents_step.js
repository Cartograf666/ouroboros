// The first-run "Connect your agents" step (phase 3C).
//
// The owner's ask, verbatim: «Хочу чтобы сразу была возможность подписки
// добавить и чтобы она была красивее и прозрачнее. Чтобы было понятно что
// достаточно одного любого API ключа и если хотя бы одну подписку добавить,
// то будет зашибись + если добавить несколько подписок, то ещё более зашибись
// и что они ротироваться будут.»
//
// So the step is a VALUE LADDER, not a form: one API key already runs
// Ouroboros, one agent plan moves the delegated and commit-review work onto
// that plan, several accounts rotate. Two honesty constraints are load-bearing
// and must survive every future edit:
//
//   * A subscription NEVER satisfies the startup gate (D-1). The main agent is
//     an API LLM client; a plan cannot run it. The ladder says so in the same
//     breath as the benefit, because a wizard that implies otherwise sends the
//     owner to a first run that refuses to start.
//   * "Rides a plan" is not "free", and NOT every reviewer moves. Commit triad,
//     scope and the advisory pre-reviewer move; plan review, task acceptance
//     and skill review stay API-only today (D15). The footnote carries both.
//
// The rotation artwork is a STATIC SVG — inline, no library, no animation
// (no comparable product animates this, and motion here would be noise). It is
// `aria-hidden`: every fact it draws is in the ladder text beside it, which is
// also what survives when a short viewport hides the figure.
//
// Login is NOT reimplemented here: `harness_login_cards.js` owns the job
// lifecycle and `claudexor_status_store.js` is the only reader of
// `/api/claudexor/status`. This module owns the step's copy, its artwork, its
// per-family rows, and BOTH sides of its conversation with the completion
// endpoint — the one declaration it hands over, and how the answer is read.
// Those live here rather than inside the wizard's IIFE so every branch of
// "what did the server actually say" is asserted in node without a transport.
//
// Pure helpers up top are node-tested without a DOM.

import { claudexorStatus, accountRows, familyLabel } from './claudexor_status_store.js';
import { LOGIN_CARD_FULL, createLoginCardController } from './harness_login_cards.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';

// The families the ratified install matrix covers (subscription_install_presets
// PRESET_HARNESSES). A connected harness outside this list contributes no
// preset seat, so offering it here would promise something the compiler
// refuses.
export const AGENT_FAMILIES = [
    { harness: 'claude', label: 'Claude Code' },
    { harness: 'codex', label: 'Codex' },
    { harness: 'cursor', label: 'Cursor' },
];

// AGENT_FAMILIES still carries the label the STEP renders for a family it lists
// before any discovery has answered; the shared `familyLabel` owns every name
// shown once a payload exists.

// The three rungs, in the owner's own logic. `tone` is the one-word verdict the
// eye lands on first; `title` is the action; `body` is the honest consequence.
export const VALUE_LADDER = [
    {
        tone: 'Runs',
        title: 'One API key, or a local model',
        body: 'Ouroboros runs. That is the whole requirement, and you met it on the '
            + 'previous step.',
    },
    {
        tone: 'Better',
        title: 'Add one agent plan',
        body: 'Delegated subagents and commit review — the triad and the scope pass — '
            + 'run inside that plan instead of billing your API key per call. The main '
            + 'agent keeps using the API key or local model you configured above: a '
            + 'plan cannot run it.',
    },
    {
        tone: 'Best',
        title: 'Add several accounts',
        body: 'They rotate on their own. When one account’s window is spent, the '
            + 'next one picks the work up, so a long session does not stall on a single '
            + 'limit.',
    },
];

export const LADDER_FOOTNOTE =
    'Riding a plan is not free — it moves that work onto a subscription you already '
    + 'pay for instead of adding per-call API charges. Plan review, task acceptance and '
    + 'skill review stay on the API key for now.';

// ---------------------------------------------------------------------------
// Pure helpers.
// ---------------------------------------------------------------------------

export function connectedHarnesses(snapshot) {
    // Which preset families the daemon vouches a signed-in account for, in the
    // stable family order. `accountRows` is the SSOT projection of the status
    // payload (native CLI session + verified credential profiles alike), so
    // this step and the accounts panel can never disagree about "connected".
    const passed = new Set(
        accountRows(snapshot)
            .filter((row) => String(row?.status?.verification || '') === 'passed')
            .map((row) => String(row.harness || '')),
    );
    return AGENT_FAMILIES.map((f) => f.harness).filter((harness) => passed.has(harness));
}

export function harnessAccountCount(snapshot, harness) {
    return accountRows(snapshot).filter(
        (row) => row.harness === harness
            && String(row?.status?.verification || '') === 'passed',
    ).length;
}

export function familyLabels(harnesses, snapshot) {
    // ONE authority, shared with the Agents tab: prefer the engine's own
    // display_name, fall back to the bootstrap product names. Passing the
    // snapshot keeps this pure and node-testable like the helpers around it;
    // without it the wizard would print a raw `claude` the day the engine
    // renames a family or adds a fourth.
    return (harnesses || []).map((harness) => familyLabel(harness, snapshot));
}

function joinLabels(labels) {
    if (labels.length <= 1) return labels[0] || '';
    return `${labels.slice(0, -1).join(', ')} and ${labels[labels.length - 1]}`;
}

/**
 * The declaration the wizard posts with the completion payload.
 *
 * `subscriptionsConnected` is a REQUEST ("go look at the daemon"), never an
 * authority: the endpoint re-reads live account state and compiles the preset
 * from live discovery. It is set from what this step actually OBSERVED, so an
 * unread account facet declares nothing (BIBLE P1: a gap is not a zero).
 */
export function subscriptionDeclaration({ connected = [], skipPresets = false } = {}) {
    return {
        subscriptionsConnected: (connected || []).length > 0,
        skipSubscriptionPresets: Boolean(skipPresets),
    };
}

export const UNCONFIRMED_COMPLETION_TEXT =
    'Onboarding was not confirmed: the server answered without the completion '
    + 'receipt. Nothing here can tell whether your settings were saved — reload '
    + 'this page; if setup is still shown, finish it again.';

export const MALFORMED_RECEIPT_CODE = 'malformed_completion_receipt';

/**
 * How the wizard READS the completion endpoint's answer. Pure, so every branch
 * is asserted without a transport.
 *
 * A 2xx is NOT a completion by itself. The success envelope carries the two
 * facts everything downstream needs — the saved runtime mode and whether it
 * needs a restart — so a shape-blind `ok` would announce a finished setup while
 * silently discarding them. A body that will not parse is UNKNOWN, not empty:
 * substituting `{}` made a 200 carrying HTML (a proxy error page, a login
 * redirect) look like success, because `{}` is truthy.
 *
 * @returns {{receipt: object}|{failure: object}}
 */
export function readCompletionAnswer({ status = 0, ok = false, parsed = false, data = null } = {}) {
    const body = data && typeof data === 'object' ? data : {};
    if (!ok) {
        // TYPED refusals (daemon unreachable, a seat no live model id satisfies,
        // an install that stopped being first-run) keep their code, the engine's
        // own sentence, the skip offer, and whether bytes reached disk.
        return {
            failure: {
                message: String(body.error || `HTTP ${status}`),
                status,
                code: String(body.code || ''),
                detail: String(body.detail || ''),
                canSkip: Boolean(body.can_skip),
                saved: Boolean(body.saved),
                // `post_commit_failed` is what the server actually sends (see
                // the gateway contract); `stage` was a name this reader made up,
                // so a real post-commit envelope parsed to '' and the owner lost
                // the one word saying WHICH step failed after their settings had
                // already landed. Both spellings are read so a stored or proxied
                // older body is not silently dropped.
                stage: String(body.post_commit_failed || body.stage || ''),
            },
        };
    }
    if (!parsed
        || body.ok !== true
        || typeof body.runtime_mode !== 'string'
        || typeof body.restart_required !== 'boolean') {
        return {
            failure: {
                message: UNCONFIRMED_COMPLETION_TEXT,
                status,
                code: MALFORMED_RECEIPT_CODE,
                detail: '',
                canSkip: false,
                saved: false,
                stage: '',
            },
        };
    }
    return { receipt: body };
}

export function completionFailureNotice(error) {
    // What the wizard SHOWS when completion did not succeed, and whether the
    // finish-without-agent-defaults escape is genuinely on offer.
    //
    // The endpoint answers typed refusals — the daemon is unreachable, a seat
    // no live model id satisfies, an install that stopped being first-run — and
    // each carries the engine's own sentence in `detail`. Collapsing those into
    // one generic "failed to save" leaves the owner holding a wizard that will
    // not finish and no way to know why, which is precisely the state the
    // atomic endpoint was built to avoid.
    //
    // `saved` is read HONESTLY. A refusal persisted nothing; a failure in a
    // post-commit stage did persist, and telling the owner otherwise would
    // repeat one layer up the exact dishonesty the atomic write removed. The
    // escape hatch is only offered when nothing is on disk — once settings
    // exist, "finish without agent defaults" would be a second write, not an
    // alternative to the first.
    const message = String(error?.message || error || 'Failed to save onboarding settings.');
    const detail = String(error?.detail || '').trim();
    const saved = Boolean(error?.saved);
    const parts = [message];
    if (detail) parts.push(detail);
    if (saved) {
        parts.push('Your settings WERE written'
            + `${error?.stage ? ` — the step that failed afterwards was ${String(error.stage)}` : ''}`
            + '. Restart Ouroboros to pick them up rather than filling this in again.');
    }
    return {
        code: String(error?.code || ''),
        saved,
        canSkip: Boolean(error?.canSkip) && !saved,
        text: parts.join(' — '),
    };
}

export function agentsOutcomeText(connected = [],
                                  { accountsKnown = true, skipPresets = false, snapshot } = {}) {
    // What the owner is told BEFORE finishing. Every verb is conditional,
    // because the compiler can still refuse a seat no live model id satisfies.
    if (!accountsKnown) {
        return 'Your agent accounts could not be checked, so nothing is assumed here. '
            + 'Finishing now leaves reviewers and subagents on your API access.';
    }
    if (!connected.length) {
        return 'No agent account connected. Ouroboros will run everything on the API '
            + 'access you configured — you can connect an account any time in '
            + 'Settings → Agents.';
    }
    const labels = joinLabels(familyLabels(connected, snapshot));
    if (skipPresets) {
        return `${labels} ${connected.length === 1 ? 'is' : 'are'} connected, but you chose to `
            + 'finish without agent defaults: reviewers and subagents stay on your API '
            + 'access. Everything stays editable in Settings → Agents.';
    }
    return `${labels} ${connected.length === 1 ? 'is' : 'are'} connected. When you finish, `
        + 'Ouroboros will try to move commit review, the scope pass, the advisory '
        + 'pre-review and delegated subagents onto '
        + `${connected.length === 1 ? 'it' : 'them'}. If those models cannot be read at `
        + 'that moment nothing is changed, and you can finish without agent defaults.';
}

export function familyStatusText(snapshot, harness, { accountsKnown = true } = {}) {
    if (!accountsKnown) return { tone: 'muted', text: 'Not checked' };
    const count = harnessAccountCount(snapshot, harness);
    if (!count) return { tone: 'muted', text: 'Not connected' };
    return {
        tone: 'ok',
        text: count === 1 ? 'Connected' : `${count} accounts connected · they rotate`,
    };
}

// ---------------------------------------------------------------------------
// The rotation diagram: ONE static SVG, no script, no animation.
// ---------------------------------------------------------------------------

// Ids are page-unique by prefix: the wizard mounts this beside other inline SVG
// one day, and a duplicate marker id silently repaints someone else's arrows.
const ARROW_ID = 'ouro-agents-rotation-arrow';

export function rotationDiagramSvg() {
    // Top to bottom in one glance: the API/local source runs the main agent,
    // the stack of agent accounts runs reviews and subagents, and both feed the
    // one Ouroboros in the middle. The cycle glyph beside the stack plus its
    // caption say what rotation MEANS — "one window spent, the next takes
    // over". Stacked rather than side-by-side because the figure lives in a
    // ~360px column of the step: a wide composition scaled its labels down to
    // an unreadable size, and readability at a glance is the whole point.
    //
    // The coordinate space is authored at roughly the rendered width, so the
    // 12px CSS text lands at 12px on screen instead of being scaled by the
    // viewBox. Strokes are `currentColor` (the figure inherits the wizard's
    // ink); the accent is spent once, on the thing everything points at.
    return `
<svg class="agents-rotation-svg" viewBox="0 0 360 200" role="presentation" aria-hidden="true" focusable="false">
  <defs>
    <marker id="${ARROW_ID}" viewBox="0 0 8 8" refX="6.5" refY="4" markerWidth="6" markerHeight="6" orient="auto">
      <path class="agents-rotation-fill" d="M0 1 L7 4 L0 7 Z"></path>
    </marker>
  </defs>
  <g class="agents-rotation-ink">
    <rect x="2" y="0" width="356" height="38" rx="9"></rect>
    <text x="16" y="17">API key or local model</text>
    <text x="16" y="32" class="agents-rotation-sub">runs the main agent</text>

    <path d="M180 40 V58" marker-end="url(#${ARROW_ID})"></path>

    <rect x="2" y="62" width="356" height="40" rx="11" class="agents-rotation-core"></rect>
    <text x="180" y="80" text-anchor="middle" class="agents-rotation-core-label">Ouroboros</text>
    <text x="180" y="95" text-anchor="middle" class="agents-rotation-sub">one agent, one budget</text>

    <path d="M180 122 V104" marker-end="url(#${ARROW_ID})"></path>

    <rect x="2" y="124" width="356" height="76" rx="9" class="agents-rotation-group"></rect>
    <text x="16" y="142">Agent plans</text>
    <text x="16" y="157" class="agents-rotation-sub">run reviews and subagents</text>
    <rect x="16" y="163" width="102" height="15" rx="5"></rect>
    <text x="24" y="175" class="agents-rotation-sub">Account 1</text>
    <rect x="16" y="181" width="102" height="15" rx="5"></rect>
    <text x="24" y="193" class="agents-rotation-sub">Account 2</text>

    <path d="M146 167 a13 13 0 1 1 -10 21" marker-end="url(#${ARROW_ID})"></path>

    <text x="182" y="175" class="agents-rotation-sub">one window spent —</text>
    <text x="182" y="191" class="agents-rotation-sub">the next takes over</text>
  </g>
</svg>`;
}

// ---------------------------------------------------------------------------
// View.
// ---------------------------------------------------------------------------

// NOTE ON ELEMENTS: nothing load-bearing here is a `<p>`. The wizard's
// short-viewport rule hides `.panel-card p` wholesale (it is how the flow stays
// unscrolled on a laptop), and the ladder is the one thing on this step that
// must survive that adaptation — what gets hidden instead is the ARTWORK, whose
// every fact is in this text anyway.
export function ladderHtml() {
    const rungs = VALUE_LADDER.map((rung) => `
        <li class="agent-rung">
            <span class="agent-rung-tone">${escapeHtml(rung.tone)}</span>
            <span class="agent-rung-text">
                <span class="agent-rung-title">${escapeHtml(rung.title)}</span>
                <span class="agent-rung-body">${escapeHtml(rung.body)}</span>
            </span>
        </li>
    `).join('');
    return `
        <div class="agent-ladder-layout">
            <ol class="agent-ladder">${rungs}</ol>
            <figure class="agents-rotation-figure">${rotationDiagramSvg()}</figure>
        </div>
        <div class="agent-ladder-note">${escapeHtml(LADDER_FOOTNOTE)}</div>
    `;
}

export function familyRowHtml(family, { status, connected = false }) {
    // Equivalent rows (docs/DESIGN.md §6): no family gets extra weight for being
    // first or already connected — only its status text and its action change.
    return `
        <div class="agent-family-row" data-agent-family="${escapeHtml(family.harness)}">
            <span class="agent-family-identity">
                <span class="agent-family-name">${escapeHtml(family.label)}</span>
                <span class="agent-family-status" data-tone="${escapeHtml(status.tone)}">${escapeHtml(status.text)}</span>
            </span>
            <button type="button" class="btn btn-secondary" data-agent-connect="${escapeHtml(family.harness)}">
                ${escapeHtml(connected ? 'Add another' : 'Connect')}
            </button>
        </div>
    `;
}

export function familyListHtml(snapshot, { accountsKnown = true } = {}) {
    const connected = new Set(accountsKnown ? connectedHarnesses(snapshot) : []);
    return AGENT_FAMILIES.map((family) => familyRowHtml(family, {
        status: familyStatusText(snapshot, family.harness, { accountsKnown }),
        connected: connected.has(family.harness),
    })).join('');
}

export function agentsStepHtml() {
    // The static skeleton. Everything that moves (family rows, the service
    // note, the outcome sentence) is patched in place afterwards, so a status
    // tick never rebuilds the login card mounted between them.
    return `
        <div class="panel-card agent-ladder-card">
            <h3>What an agent plan changes</h3>
            ${ladderHtml()}
        </div>
        <div class="panel-card" id="agents-accounts">
            <h3>Your agent accounts</h3>
            <div id="agents-status-note" class="agent-service-note" hidden></div>
            <div id="agents-family-list" class="agent-family-list"></div>
            <div id="agents-login-host"></div>
            <div id="agents-outcome" class="agent-outcome"></div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// The controller.
// ---------------------------------------------------------------------------

/**
 * @param {object} options
 * @param {Function} [options.doc]        `document`, or a getter for it
 * @param {object}   [options.store]      the shared Claudexor status store
 * @param {Function} [options.fetchImpl]  transport for the login card
 * @param {Function} [options.isVisible]  is the Agents step on screen right now
 * @param {Function} [options.onChange]   called with the connected harness list
 * @returns {object} controller
 */
export function createAgentsStep({
    doc = () => (typeof document === 'undefined' ? null : document),
    store = claudexorStatus,
    fetchImpl = null,
    isVisible = () => true,
    onChange = () => {},
} = {}) {
    const getDoc = typeof doc === 'function' ? doc : () => doc;
    const state = {
        connected: [],
        skipPresets: false,
        disposed: false,
        unsubscribe: null,
        pageHideBound: null,
        pageHideTarget: null,
        listHtml: null,
    };

    function el(id) {
        return getDoc()?.getElementById(id) || null;
    }

    function accountsKnown() {
        return Boolean(store.accountsKnown);
    }

    // FULL, not compact (phase 2 disclosed the gap): compact omits the
    // paste-code entry and the collapsed terminal fallback, so a Claude login
    // whose localhost callback cannot complete — the owner's browser on another
    // machine, exactly the remote/headless first run — would have no way to
    // finish. A dead end is worst at first run, so onboarding mounts the same
    // card Settings mounts.
    function createLogin() {
        return createLoginCardController({
            host: () => el('agents-login-host'),
            store,
            mode: LOGIN_CARD_FULL,
            doc: getDoc,
            ...(fetchImpl ? { fetchImpl } : {}),
            onSettled: () => { store.refresh(); paint(); },
        });
    }

    let login = createLogin();

    function ensureLogin() {
        if (!login || login.disposed) login = createLogin();
        return login;
    }

    function paint() {
        if (state.disposed) return;
        const snapshot = store.snapshot;
        const known = accountsKnown();
        const list = el('agents-family-list');
        if (list) {
            // Rebuild ONLY on a real change. The status store settles every few
            // seconds; replacing three unchanged rows on every tick is pointless
            // churn, and it opens a window where a click lands on a button that
            // is being replaced.
            const html = familyListHtml(snapshot, { accountsKnown: known });
            if (html !== state.listHtml) {
                state.listHtml = html;
                list.innerHTML = html;
                bindConnectButtons(list);
            }
        }
        const note = el('agents-status-note');
        if (note) {
            const unavailable = store.unavailableNote('accounts');
            note.hidden = !unavailable;
            note.textContent = unavailable ? unavailable.text : '';
            if (unavailable) note.dataset.tone = unavailable.tone;
        }
        const outcome = el('agents-outcome');
        if (outcome) {
            outcome.textContent = agentsOutcomeText(state.connected, {
                accountsKnown: known,
                skipPresets: state.skipPresets,
                snapshot,
            });
        }
        login?.render();
    }

    function bindConnectButtons(list) {
        list.querySelectorAll('[data-agent-connect]').forEach((button) => {
            button.addEventListener('click', () => {
                if (state.disposed) return;
                ensureLogin().start(button.getAttribute('data-agent-connect'), '');
            });
        });
    }

    function adopt(view) {
        const next = view && view.reads && view.reads.accounts === 'ok'
            ? connectedHarnesses(store.snapshot)
            : [];
        const changed = next.join(',') !== state.connected.join(',');
        state.connected = next;
        paint();
        if (changed) onChange([...state.connected]);
    }

    /** Mount into the step DOM the wizard just rendered. */
    function mount() {
        if (state.disposed) return;
        if (!state.unsubscribe) {
            state.unsubscribe = store.subscribe(adopt, { visible: isVisible });
            const pageHideTarget = getDoc()?.defaultView;
            if (pageHideTarget?.addEventListener) {
                // The wizard never unmounts itself, so the ONE listener this
                // controller holds is released on a real unload as well as by
                // dispose(). A bfcache hide (`persisted`) is NOT an unload: the
                // page can come back, and tearing down here would restore a
                // wizard whose Agents step no longer reads anything.
                state.pageHideTarget = pageHideTarget;
                state.pageHideBound = (event) => {
                    if (event?.persisted !== true) detach();
                };
                pageHideTarget.addEventListener('pagehide', state.pageHideBound);
            }
        }
        // A remount rebuilds the DOM the wizard just replaced.
        state.listHtml = null;
        paint();
        store.refresh();
    }

    /**
     * Ask the login controller for its exact custody result.
     *
     * Cleanup and local departure are deliberately separate. `retained` means
     * another cancel is pointless; `unknown` remains retryable for the wizard's
     * existing bounded window. Neither result is rewritten as release proof.
     *
     * @returns {Promise<'released'|'retained'|'unknown'>}
     */
    async function dispose() {
        if (state.disposed) return 'unknown';
        // The controller remains the custody SSOT even after its own
        // recovery-face Close: repeated dispose returns its remembered honest
        // detach status without sending another lifecycle request.
        return login ? login.dispose() : 'released';
    }

    /** Leave locally without claiming the daemon released process custody. */
    function detach() {
        if (state.disposed) return;
        state.disposed = true;
        login?.detach();
        if (state.unsubscribe) state.unsubscribe();
        state.unsubscribe = null;
        if (state.pageHideBound && state.pageHideTarget?.removeEventListener) {
            state.pageHideTarget.removeEventListener('pagehide', state.pageHideBound);
        }
        state.pageHideBound = null;
        state.pageHideTarget = null;
        state.listHtml = null;
        login = null;
    }

    return {
        mount,
        paint,
        dispose,
        detach,
        get connected() { return [...state.connected]; },
        // The payload the family names are spoken from. The wizard's review
        // step renders the SAME families one screen later, and without this it
        // fell back to the bootstrap product names — so an engine rename showed
        // up on the Agents step and then silently un-renamed itself in the
        // summary. Same store, same snapshot, one spelling.
        get snapshot() { return store?.snapshot || null; },
        get accountsKnown() { return accountsKnown(); },
        setSkipPresets(value) { state.skipPresets = Boolean(value); paint(); },
        declaration({ skipPresets = state.skipPresets } = {}) {
            return subscriptionDeclaration({ connected: state.connected, skipPresets });
        },
    };
}
