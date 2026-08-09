// Delegation section (Agents tab, under Review lanes) — the owner-facing face
// of the delegated-subagent capability, and the ONE place the whole subagent
// story is configured.
//
// Until this section existed the capability shipped invisible: delegation
// (OUROBOROS_SUBAGENT_HARNESS) and the write permission
// (OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS) were reachable only by hand-editing
// settings.json, so the default install ran with delegation off and no control
// said so. The counts that bound it — how many children per root, how deep the
// chain — sat two tabs away under Advanced → Runtime Limits, next to process
// worker counts they have nothing to do with; D-10 moved them here.
//
// Shape rules, same house style as Review lanes:
//  * The harness list comes from the SAME source the Accounts section reads
//    (accountRows over /api/claudexor/status) — one catalog path, one
//    login-capable discriminator, no second inventory.
//  * The MODEL is the owner's default for delegated runs (owner, 2026-08-04):
//    the `=model` tail of OUROBOROS_SUBAGENT_HARNESS, picked from the same
//    engine discovery the reviewer rows use — "Engine default model" = empty
//    tail. Effort stays derived per call; a hand-written `:effort` remainder
//    rides through verbatim and has no control here.
//  * With no subscription connected the section says so instead of rendering a
//    toggle that cannot do anything.
//
// Pure helpers live at the top and are node-tested without a DOM.

import {
    FACET_ACCOUNTS,
    FACET_CATALOG,
    READ_OK,
    READ_TRANSPORT,
    READ_UNREAD,
    accountRows,
    bindStatusSurface,
    claudexorStatus,
    facetGapClause,
    statusUnavailableNote,
} from './claudexor_status_store.js';
import { renderSegmentedField } from './page_header.js';
import { modelsGapNote, sessionModelOptions } from './reviewer_slots.js';
import { formatRelativeAge } from './ui_helpers.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';

// The owner's explicit "delegation off" (see subagents.parse_subagent_harness):
// distinguishable from an empty, never-decided value, which is what lets the
// connected-subscription default exist without overriding a real decision.
export const DELEGATION_OFF = 'off';

// ---------------------------------------------------------------------------
// Pure helpers.
// ---------------------------------------------------------------------------

export function parseSubagentRoute(value) {
    // `harness[=model][:effort]`. This UI authors the harness AND the `=model`
    // tail (the owner's default model select); a hand-written `:effort`
    // remainder is carried through VERBATIM rather than silently dropped on
    // the next save — the owner wrote it on purpose.
    const raw = String(value || '').trim();
    if (!raw || raw.toLowerCase() === DELEGATION_OFF) {
        return { harness: '', suffix: '', decided: Boolean(raw) };
    }
    const eq = raw.indexOf('=');
    if (eq < 0) return { harness: raw, suffix: '', decided: true };
    return { harness: raw.slice(0, eq), suffix: raw.slice(eq), decided: true };
}

export function composeSubagentRoute(harness, suffix) {
    const h = String(harness || '').trim();
    if (!h) return DELEGATION_OFF;
    return `${h}${String(suffix || '')}`;
}

export function splitSubagentTail(suffix) {
    // The `=model[:effort]` tail, split at the FIRST ':' exactly like the
    // backend parser (subagents.parse_subagent_harness): `model` is the owner
    // default the select now edits, `effortSuffix` is the hand-written effort
    // remainder — kept VERBATIM, leading ':' included, so composing back is
    // byte-identical when the model is untouched.
    const raw = String(suffix || '');
    const body = raw.startsWith('=') ? raw.slice(1) : raw;
    const colon = body.indexOf(':');
    if (colon < 0) return { model: body, effortSuffix: '' };
    return { model: body.slice(0, colon), effortSuffix: body.slice(colon) };
}

export function composeSubagentTail(model, effortSuffix) {
    const m = String(model || '');
    const e = String(effortSuffix || '');
    return m || e ? `=${m}${e}` : '';
}

export function lastDelegationLine(entry) {
    // ONE muted receipt line under the section: what the last delegated run
    // REALLY ran as. Absence is shown as absence — no record, no line; an
    // undisclosed applied model says so instead of echoing the request; a
    // requested≠applied pair is disclosed loudly (owner decision: the run
    // completes on what the engine gave, the mismatch is advisory).
    if (!entry || typeof entry !== 'object' || !entry.run_id) return '';
    const requested = String(entry.requested_model || '');
    const applied = String(entry.applied_model || '');
    const parts = [String(entry.route || '') || 'unknown route'];
    if (requested && applied && requested !== applied) {
        parts.push(`requested ${requested} → ran ${applied}`);
    } else {
        parts.push(applied || 'model not disclosed');
    }
    const when = formatRelativeAge(Date.parse(entry.ts || ''), 'just now');
    if (when) parts.push(when);
    return `Last delegated run: ${parts.join(' · ')}`;
}

function harnessRow(payload, harnessId) {
    // The discovery ROW, not just its models: the row also carries the typed
    // `models_error` that says the model list itself could not be read, and a
    // synthetic `{models}` object dropped that fact on the floor.
    for (const harness of payload?.harnesses || []) {
        if (String(harness?.id || '') === String(harnessId || '')) return harness;
    }
    return null;
}

export function connectedHarnesses(payload) {
    // "Connected" = the accounts panel's own answer, not a second definition:
    // an account row whose verification the daemon observed AND passed — the
    // same `=== 'passed'` predicate accountLoginConfirmed applies (native rows
    // project 'passed' | ''). A failed/expired verification renders red in the
    // accounts panel; counting it as connected here would let the default-on
    // rule auto-pin a route whose every dispatch falls back to native.
    //
    // OWNER DECISION 2026-08-09 — SUBSCRIPTION-FIRST, do NOT narrow this.
    // A native/local session counts as connected here even though its
    // verification_source is `local_store` (detected on disk, not re-proved
    // against the vendor this second), and that is deliberately enough to turn
    // delegation on by default. A review pass proposed requiring vendor-level
    // verification before the default fires; the owner declined it outright:
    // Ouroboros should PREFER subscriptions over the API budget, and demanding
    // a live vendor probe would leave delegation off on exactly the machines
    // that have subscriptions sitting right there. Strengthen this bias, never
    // trade it for caution. The pin is the node test
    // 'subscription-first: a local session is enough to turn delegation on by
    // default' in web/tests/subagents_settings.test.js.
    const names = {};
    for (const harness of payload?.harnesses || []) {
        const id = String(harness?.id || '');
        if (id) names[id] = String(harness.display_name || id);
    }
    const out = [];
    for (const row of accountRows(payload)) {
        if (String(row?.status?.verification || '') !== 'passed') continue;
        if (out.some((item) => item.id === row.harness)) continue;
        out.push({ id: row.harness, label: names[row.harness] || row.harness });
    }
    return out;
}

export function delegationView({ saved = '', payload = null, statusError = '', accountsRead = '',
    catalogRead = READ_OK, edit = null, loaded = true } = {}) {
    // The whole section as ONE value: which state to render, what the harness
    // select offers, and the muted sentence under it. `edit` is the owner's
    // unsaved choice laid over the saved value — it goes through the same
    // function so the sentence can never describe a different state than the
    // controls above it (it did: turning delegation off still read "on by
    // default", because the note was computed from the saved value alone).
    const route = parseSubagentRoute(saved);
    // This section renders TWO facets: the account list decides the whole view,
    // and the model select rides the CATALOG. Explaining only the accounts left
    // a catalog gap silently on screen — the model options would quietly narrow
    // to whatever the last read happened to hold, with nothing saying so. A
    // catalog is coalesced by SUBJECT, never by enum: dropping it because its
    // STATE equalled the accounts facet's left the model select unexplained
    // whenever both reads were in the same trouble — the accounts sentence
    // says nothing about agent discovery. (The coarse `indeterminate` yields
    // no clause by construction; its own global sentence covers everything.)
    const accountsState = statusError ? READ_TRANSPORT : String(accountsRead || '');
    const catalogClause = facetGapClause({ [FACET_CATALOG]: catalogRead }, [FACET_CATALOG]);
    const view = (value) => (catalogClause
        ? { ...value, note: `${value.note} ${catalogClause}`.trim() }
        : value);
    if (!loaded) {
        // Not read YET is not "nothing connected": until the accounts arrive the
        // section states only that it is reading (collect() already guards on the
        // same fact, so this renders nothing it would then author).
        return view({ state: 'loading', enabled: false, harness: '', model: '', modelOptions: [], suffix: '', options: [],
            note: statusUnavailableNote(READ_UNREAD, { facet: FACET_ACCOUNTS }).text });
    }
    // This section renders the ACCOUNTS facet, so that facet alone decides the
    // whole view. "Nobody could be asked", "never asked" and "asked and
    // refused" are three different sentences and none of them says anything
    // about the daemon-side account — so the note stops at what is actually
    // known. Offering the live control here let Save answer "saved" while
    // collect() returned {}; and the never-asked case used to fall through to
    // "no account connected for codex right now", a row-level accusation
    // earned only by a read that actually happened.
    const unavailable = (accountsState && accountsState !== READ_OK && accountsState !== READ_UNREAD)
        ? statusUnavailableNote(accountsState, { error: statusError, facet: FACET_ACCOUNTS })
        : null;
    if (unavailable) {
        return view({ state: 'unknown', enabled: false, harness: '', model: '', modelOptions: [], suffix: '', options: [],
            note: unavailable.text });
    }
    const connected = connectedHarnesses(payload);
    const savedHarness = route.harness;

    if (!savedHarness && !connected.length) {
        // Nothing to delegate to, so there is no control to offer. A decided
        // `off` is the owner's own answer and never re-defaults, so promising
        // "turns on by itself" over it would announce an override that will not
        // happen.
        return view(route.decided
            ? { state: 'no_subscription', enabled: false, harness: '', model: '', modelOptions: [], suffix: '', options: [],
                note: 'Delegation is off because you turned it off, and it stays off until you turn it back on. No agent subscription is connected right now; connect one under Accounts above to make delegation available again.' }
            : { state: 'no_subscription', enabled: false, harness: '', model: '', modelOptions: [], suffix: '', options: [],
                note: 'No agent subscription is connected, so there is nothing to delegate to. Connect one under Accounts above and delegation turns on by itself.' });
    }

    const defaultOn = !route.decided && connected.length > 0;
    const enabled = edit ? Boolean(edit.enabled) : (Boolean(savedHarness) || defaultOn);
    const harness = enabled
        ? String((edit && edit.harness) || savedHarness || connected[0]?.id || '')
        : '';
    // A hand-written model/effort tail belongs to the harness it was written
    // for; carrying it to another one would pin a model that harness may not serve.
    const savedTail = splitSubagentTail(harness && harness === savedHarness ? route.suffix : '');
    // The model select's value: the owner's unsaved pick when there is one,
    // else the saved default. A harness switch drops the saved tail above, so
    // the reset to "Engine default model" is VISIBLE on screen before Save —
    // accepted residual, deliberately no per-harness memory. The `:effort`
    // remainder is never edited here and rides through verbatim.
    const model = edit && edit.model !== null && edit.model !== undefined
        ? String(edit.model) : savedTail.model;
    const suffix = composeSubagentTail(model, savedTail.effortSuffix);
    // The SAME options fragment the reviewer rows use, "(not in discovery)"
    // guard included: a Save while the daemon is down must not silently erase
    // the saved model pin.
    const row = harness ? harnessRow(payload, harness) : null;
    const modelOptions = enabled && harness
        // The MODEL list rides the CATALOG facet, which is independent of the
        // accounts facet that got us here: with the catalog unread the saved
        // pin keeps its option and loses only the "(not in discovery)" claim.
        // The ROW is passed whole so the per-harness model-read gap is seen.
        ? sessionModelOptions(row, model, { catalogKnown: catalogRead === READ_OK })
        : [];
    const modelsGap = enabled && harness
        ? modelsGapNote(row, catalogRead === READ_OK) : '';

    const options = [...connected];
    if (savedHarness && !options.some((item) => item.id === savedHarness)) {
        // A SAVED route keeps an option even when discovery cannot see it right
        // now (daemon down, account signed out): dropping it would make the
        // browser redraw the row as the first connected entry, and the next Save
        // would silently re-point delegation at an account nobody chose.
        options.push({ id: savedHarness, label: `${savedHarness} (no account connected)` });
    }

    let state = 'on';
    let note = 'The delegated work runs on this subscription, on the model picked here '
        + '("Engine default model" leaves the choice to the agent); the subagent '
        + 'itself still runs on the API to drive and check it. Reasoning effort is still '
        + 'derived from each call.';
    if (!enabled) {
        state = 'off';
        note = 'Subagents run entirely on the API. Turn this on to send their work to a connected subscription.';
    } else if (!connected.some((item) => item.id === harness)) {
        note = `No connected account for ${harness} right now — delegated work runs as an ordinary subagent on the API until it is signed in again.`;
    } else if (!savedHarness) {
        // The owner never authored this; saying "on" without saying it is not
        // stored yet would misreport where subagents actually run today.
        state = 'default_on';
        note = 'On by default now that a subscription is connected. Save Settings to apply it — until then subagents still run on the API.';
    }
    if (modelsGap) {
        // The catalog listed this agent but its MODEL list refused, so the
        // select below is short for a reason nothing else on the page states.
        note = `${note} The model list for ${harness} could not be read, so the choices below may be incomplete; your saved model is kept.`;
    }
    return view({ state, enabled, harness, model, modelOptions, suffix, options, note });
}

// ---------------------------------------------------------------------------
// DOM section (Agents → Delegation). State is module-local; collect is synchronous.
// ---------------------------------------------------------------------------

const state = {
    loaded: false,
    saved: '',
    payload: null,
    statusError: '',
    // The ACCOUNTS facet's own read state, so this section can tell "we could
    // not ask" from "never asked" from "asked and refused" from a real, empty
    // account list. Independent of the catalog facet, which the model select
    // rides — one can be authoritative while the other was never read.
    accountsRead: '',
    catalogRead: READ_OK,
    store: claudexorStatus,
    disposers: [],
    // The owner's unsaved answer only; everything derived from it (which route,
    // which options, which sentence) stays in delegationView. `model: null`
    // means "no unsaved model edit" — '' is a real answer (Engine default).
    enabled: null,
    harness: '',
    model: null,
    onChange: () => {},
};

export function renderSubagentsSection() {
    return `
        <div class="form-section" id="subagents-section">
            <h3>Delegation</h3>
            <div class="settings-section-copy">
                Where Ouroboros's subagents run, how many of them there may be, and how far they
                may write. By default a subagent is an ordinary child on your API budget.
                Delegation hands its work to a connected agent subscription — that part spends the
                subscription's window; the subagent itself still runs on the API to drive and
                check it.
            </div>
            <div id="subagents-rows" class="reviewer-slot-rows"></div>
            <div class="settings-effort-card">
                <label>Allow mutative subagents</label>
                <input id="s-allow-mutative-subagents" type="hidden" value="on">
                ${renderSegmentedField({
                    target: 's-allow-mutative-subagents',
                    title: 'Applies on the next task; no restart required.',
                    options: [
                        { value: 'off', label: 'Off' },
                        { value: 'auto', label: 'Auto' },
                        { value: 'on', label: 'On' },
                    ],
                })}
                <div class="settings-inline-note">
                    Whether a subagent may WRITE — in an isolated git worktree of this repo, an external
                    workspace, or a from-scratch project — and return patches for the parent to review.
                    Read-only subagents are always allowed, and this applies to delegated and API
                    subagents alike. With no explicit choice (Auto) the runtime mode decides:
                    Advanced and Pro allow every surface; Light allows only children that build
                    OUTSIDE the Ouroboros runtime (external workspaces and from-scratch projects)
                    and keeps worktree-of-this-repo children off. <strong>Human controlled:</strong>
                    the agent cannot self-enable it; applies on the next task, no restart.
                </div>
            </div>
            <div class="form-grid two">
                <div class="form-field">
                    <label>Active subagents per root</label>
                    <input id="s-active-subagents" type="number" min="1" max="500" value="6">
                    <div class="settings-inline-note">How many children one root task may run at once.</div>
                </div>
                <div class="form-field">
                    <label>Subagent depth</label>
                    <!-- 0 is a real owner choice ("no delegation at all"), honoured
                         structurally since v6.79.0 — it must be reachable here. -->
                    <input id="s-subagent-depth" type="number" min="0" max="10" value="2">
                    <div class="settings-inline-note">How deep the chain of subagents may nest. <code>0</code> turns delegation off entirely.</div>
                </div>
            </div>
            <!-- Two paths nobody edits in a normal week, kept out of the way so
                 the tab stays scannable (the house "More options" pattern). -->
            <details class="settings-subsection" id="delegation-advanced">
                <summary>Advanced — where subagents check out their work</summary>
                <div class="settings-subsection-body">
                    <div class="form-grid two">
                        <div class="form-field">
                            <label>Subagent worktree root</label>
                            <input id="s-subagent-worktree-root" type="text" placeholder="~/Ouroboros/subagent_worktrees">
                        </div>
                        <div class="form-field">
                            <label>Subagent projects root (genesis)</label>
                            <input id="s-subagent-projects-root" type="text" placeholder="~/Ouroboros/projects">
                        </div>
                    </div>
                    <div class="settings-inline-note">
                        Where an acting subagent checks out a git worktree of this repo, or builds a
                        from-scratch (<code>genesis</code>) project. Both live outside the app repo and
                        data. Genesis projects are durable and never auto-removed; worktrees are
                        cleaned by the GC retention setting in Advanced. Leave a root blank for the
                        default under <code>~/Ouroboros/</code>.
                    </div>
                </div>
            </details>
        </div>
    `;
}

function currentView() {
    // The model select renders only while delegation is enabled, and its
    // handler re-asserts enabled=true (same as the harness handler), so a
    // non-null model edit never rides a null enabled.
    return delegationView({
        saved: state.saved,
        payload: state.payload,
        statusError: state.statusError,
        accountsRead: state.accountsRead,
        catalogRead: state.catalogRead,
        edit: state.enabled === null
            ? null
            : { enabled: state.enabled, harness: state.harness, model: state.model },
        loaded: state.loaded,
    });
}

function renderRows() {
    const host = document.getElementById('subagents-rows');
    if (!host) return;
    const view = currentView();
    const offerControls = view.state !== 'no_subscription' && view.state !== 'unknown' && view.state !== 'loading';
    const options = view.options.map((item) => {
        const selected = item.id === view.harness ? ' selected' : '';
        return `<option value="${escapeHtml(item.id)}"${selected}>${escapeHtml(item.label)}</option>`;
    }).join('');
    const modelOptions = view.modelOptions.map((opt) => {
        const selected = opt.value === view.model ? ' selected' : '';
        return `<option value="${escapeHtml(opt.value)}"${selected}>${escapeHtml(opt.label)}</option>`;
    }).join('');
    const lastLine = lastDelegationLine(state.payload?.subagent_last_delegation);
    host.innerHTML = `
        <div class="reviewer-slot-row" data-subagent-row>
            ${offerControls ? `
            <div class="reviewer-slot-controls">
                <select data-subagent-delegation aria-label="Delegate subagents">
                    <option value="off"${view.enabled ? '' : ' selected'}>Subagents run on the API</option>
                    <option value="on"${view.enabled ? ' selected' : ''}>Delegate to an agent subscription</option>
                </select>
                ${view.enabled ? `<select data-subagent-harness aria-label="Agent">${options}</select>` : ''}
                ${view.enabled ? `<select data-subagent-model aria-label="Agent model">${modelOptions}</select>` : ''}
            </div>` : ''}
            <div class="reviewer-slot-meta muted">${escapeHtml(view.note)}</div>
            ${lastLine ? `<div class="reviewer-slot-meta muted">${escapeHtml(lastLine)}</div>` : ''}
        </div>
    `;
    bindRowEvents();
}

function bindRowEvents() {
    const host = document.getElementById('subagents-rows');
    if (!host) return;
    host.querySelector('[data-subagent-delegation]')?.addEventListener('change', (event) => {
        // Only the ANSWER is stored; which route that resolves to (the saved one,
        // or the first connected subscription) stays delegationView's decision.
        state.enabled = event.target.value === 'on';
        renderRows();
        state.onChange();
    });
    host.querySelector('[data-subagent-harness]')?.addEventListener('change', (event) => {
        state.enabled = true;
        state.harness = String(event.target.value || '');
        // The model belongs to the harness it was picked for: switching resets
        // the unsaved pick, and delegationView drops the saved tail, so the
        // select visibly shows "Engine default model" (accepted residual).
        state.model = null;
        renderRows();
        state.onChange();
    });
    host.querySelector('[data-subagent-model]')?.addEventListener('change', (event) => {
        state.enabled = true;
        state.model = String(event.target.value || '');
        renderRows();
        state.onChange();
    });
}

export function applySubagentsSettings(settings) {
    state.saved = String(settings?.OUROBOROS_SUBAGENT_HARNESS ?? '').trim();
    state.enabled = null;
    state.harness = '';
    state.model = null;
    renderRows();
}

export function renderSignature(store) {
    // Everything this section RENDERS, so a repaint is skipped only when the
    // pixels would really be identical. The model list was keyed on its LENGTH,
    // and a swap at equal count (a harness renaming or replacing a model)
    // therefore left the section showing the model that no longer exists and
    // never offering the one that does — the select is built from these very
    // ids. Compare the ids, not how many of them there are.
    const snapshot = store.snapshot || {};
    return [
        store.reads,
        store.error,
        connectedHarnesses(snapshot),
        (snapshot.harnesses || []).map((harness) => [
            String(harness?.id || ''),
            (harness?.models || []).map((model) => String(model?.id || model?.value || model || '')),
            // The typed model-read gap is RENDERED (it adds a sentence to the
            // note and withdraws the not-in-discovery label), so a later probe
            // that succeeds must repaint even when the list stays the same.
            String(harness?.models_error || ''),
        ]),
        snapshot.subagent_last_delegation || null,
    ];
}

function adoptStoreSnapshot() {
    // includeModels: the same status payload, plus per-harness model discovery
    // for the default-model select. The flag is STICKY on the store, so the
    // accounts panel's own polls keep carrying models once this section has
    // asked once — no surface can silently downgrade another's snapshot.
    state.accountsRead = state.store.facet(FACET_ACCOUNTS);
    state.catalogRead = state.store.facet(FACET_CATALOG);
    state.statusError = state.store.error || '';
    state.payload = state.statusError ? null : state.store.snapshot;
    // "Not read YET" is not "read and found nothing": the pre-request paint
    // must keep saying it is reading, or a connected owner briefly sees
    // "No agent subscription is connected".
    state.loaded = state.store.everSettled;
}

export async function reloadSubagentsSection() {
    await state.store.refresh({ includeModels: true });
    adoptStoreSnapshot();
    renderRows();
}

export function initSubagentsSection({ onChange, store = claudexorStatus } = {}) {
    destroySubagentsSection();
    state.onChange = typeof onChange === 'function' ? onChange : () => {};
    state.store = store;
    // Stay in sync with the ONE status read: when the daemon comes up while
    // Settings is open, this section stops saying "could not be listed" without
    // the owner reloading the page. That needs the SHARED surface binding — a
    // bare subscribe() carries no visibility predicate, and the store never
    // polls for a subscriber that cannot say it is on screen, so nothing ever
    // arrived to react to. Re-render only on a state change that this section
    // actually renders: a repaint on every poll tick would drop the caret out
    // of a control the owner is using.
    let signature = '';
    state.disposers.push(bindStatusSurface(state.store, {
        elementId: 'subagents-rows',
        includeModels: true,
        listener: () => {
            const next = JSON.stringify(renderSignature(state.store));
            adoptStoreSnapshot();
            if (next === signature) return;
            signature = next;
            renderRows();
        },
    }));
    // The initial load is driven by settings.js loadSettings(), which awaits
    // reloadSubagentsSection() BEFORE taking the clean-draft baseline — otherwise
    // the async arrival of the accounts would read as an unsaved edit.
}

export function destroySubagentsSection() {
    for (const dispose of state.disposers.splice(0)) {
        try { dispose(); } catch (err) { /* a broken disposer must not block the rest */ }
    }
}

export function collectSubagentsSettings() {
    // Never author the route from an UNLOADED or unreadable view: an unrelated
    // save must not turn delegation off because this page could not reach the
    // daemon (same rule as collectReviewerSlots). A STOPPED daemon is the same
    // class — the section rendered no control, so it has no answer to save.
    if (!state.loaded || state.statusError) return {};
    const view = currentView();
    if (view.state === 'no_subscription' || view.state === 'unknown' || view.state === 'loading') return {};
    return { OUROBOROS_SUBAGENT_HARNESS: composeSubagentRoute(view.harness, view.suffix) };
}
