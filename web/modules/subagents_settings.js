// Subagents section (Models page, sibling of Reviewer Slots) — the owner-facing
// face of the delegated-subagent capability.
//
// Until this section existed the whole capability shipped invisible: delegation
// (OUROBOROS_SUBAGENT_HARNESS) and the write permission
// (OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS) were reachable only by hand-editing
// settings.json, so the default install ran with delegation off and no control
// said so.
//
// Shape rules, same house style as Reviewer Slots:
//  * The harness list comes from the SAME source the Harness Accounts panel
//    reads (accountRows over /api/claudexor/status) — one catalog path, one
//    login-capable discriminator, no second inventory.
//  * Model and effort are NOT pickable here. A delegated child derives them from
//    its own call axes (model_lane / write_surface / executor); a per-row model
//    picker would let the owner author a combination the call then overrides.
//  * With no subscription connected the section says so instead of rendering a
//    toggle that cannot do anything.
//
// Pure helpers live at the top and are node-tested without a DOM.

import { apiFetch } from './api_client.js';
import { accountRows } from './harness_accounts.js';
import { renderSegmentedField } from './page_header.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';

// The owner's explicit "delegation off" (see subagents.parse_subagent_harness):
// distinguishable from an empty, never-decided value, which is what lets the
// connected-subscription default exist without overriding a real decision.
export const DELEGATION_OFF = 'off';

// ---------------------------------------------------------------------------
// Pure helpers.
// ---------------------------------------------------------------------------

export function parseSubagentRoute(value) {
    // `harness[=model][:effort]`. The harness is the only part this UI authors;
    // a hand-written model/effort tail is carried through VERBATIM rather than
    // silently dropped on the next save — the owner wrote it on purpose.
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

export function connectedHarnesses(payload) {
    // "Connected" = the accounts panel's own answer, not a second definition:
    // an account row whose verification the daemon actually observed
    // (native_login_detected for a default account, a profile status otherwise).
    // A harness that is merely DISCOVERED has no account behind it, and offering
    // it here would produce a route whose every dispatch falls back to native.
    const names = {};
    for (const harness of payload?.harnesses || []) {
        const id = String(harness?.id || '');
        if (id) names[id] = String(harness.display_name || id);
    }
    const out = [];
    for (const row of accountRows(payload)) {
        if (!String(row?.status?.verification || '')) continue;
        if (out.some((item) => item.id === row.harness)) continue;
        out.push({ id: row.harness, label: names[row.harness] || row.harness });
    }
    return out;
}

export function delegationView({ saved = '', payload = null, statusError = '', edit = null } = {}) {
    // The whole section as ONE value: which state to render, what the harness
    // select offers, and the muted sentence under it. `edit` is the owner's
    // unsaved choice laid over the saved value — it goes through the same
    // function so the sentence can never describe a different state than the
    // controls above it (it did: turning delegation off still read "on by
    // default", because the note was computed from the saved value alone).
    const route = parseSubagentRoute(saved);
    const connected = connectedHarnesses(payload);
    const savedHarness = route.harness;

    if (!savedHarness && !connected.length) {
        // Nothing to delegate to, so there is no control to offer. Failing to
        // ASK is a different sentence from the owner having no subscription:
        // one is a fact about his accounts, the other is this page's problem.
        return statusError
            ? { state: 'unknown', enabled: false, harness: '', suffix: '', options: [],
                note: `Could not read the coding-agent accounts (${statusError}). Delegation is unchanged; reopen Settings when the connection is back.` }
            : { state: 'no_subscription', enabled: false, harness: '', suffix: '', options: [],
                note: 'No coding-agent subscription is connected, so there is nothing to delegate to. Sign one in under Providers → Harness Accounts and delegation turns on by itself.' };
    }

    const defaultOn = !route.decided && connected.length > 0;
    const enabled = edit ? Boolean(edit.enabled) : (Boolean(savedHarness) || defaultOn);
    const harness = enabled
        ? String((edit && edit.harness) || savedHarness || connected[0]?.id || '')
        : '';
    // A hand-written model/effort tail belongs to the harness it was written
    // for; carrying it to another one would pin a model that harness may not serve.
    const suffix = harness && harness === savedHarness ? route.suffix : '';

    const options = [...connected];
    if (savedHarness && !options.some((item) => item.id === savedHarness)) {
        // A SAVED route keeps an option even when discovery cannot see it right
        // now (daemon down, account signed out): dropping it would make the
        // browser redraw the row as the first connected entry, and the next Save
        // would silently re-point delegation at an account nobody chose.
        options.push({ id: savedHarness, label: `${savedHarness} (no account connected)` });
    }

    let state = 'on';
    let note = 'Delegated work runs on this subscription. Model and reasoning effort come from each call, not from here.';
    if (!enabled) {
        state = 'off';
        note = 'Subagents run on the API. Turn this on to spend a connected subscription instead.';
    } else if (!connected.some((item) => item.id === harness)) {
        note = `No connected account for ${harness} right now — delegated work runs as an ordinary subagent on the API until it is signed in again.`;
    } else if (!savedHarness) {
        // The owner never authored this; saying "on" without saying it is not
        // stored yet would misreport where subagents actually run today.
        state = 'default_on';
        note = 'On by default now that a subscription is connected. Save Settings to apply it — until then subagents still run on the API.';
    }
    if (statusError) {
        note = `Could not read the coding-agent accounts (${statusError}). ${note}`;
    }
    return { state, enabled, harness, suffix, options, note };
}

// ---------------------------------------------------------------------------
// DOM section (Models page). State is module-local; collect is synchronous.
// ---------------------------------------------------------------------------

const state = {
    loaded: false,
    saved: '',
    payload: null,
    statusError: '',
    // The owner's unsaved answer only; everything derived from it (which route,
    // which options, which sentence) stays in delegationView.
    enabled: null,
    harness: '',
    onChange: () => {},
};

export function renderSubagentsSection() {
    return `
        <div class="form-section" id="subagents-section">
            <h3>Subagents</h3>
            <div class="settings-section-copy">
                Where Ouroboros's subagents run. By default a subagent is an ordinary child on your
                API budget. Delegation hands it to a connected coding-agent subscription instead —
                same work, spent against that subscription's window rather than tokens.
            </div>
            <div id="subagents-rows" class="reviewer-slot-rows"></div>
            <div class="settings-effort-card">
                <label>Allow Mutative Subagents</label>
                <input id="s-allow-mutative-subagents" type="hidden" value="on">
                ${renderSegmentedField({
                    target: 's-allow-mutative-subagents',
                    title: 'Applies on the next task; no restart required.',
                    options: [{ value: 'off', label: 'Off' }, { value: 'on', label: 'On' }],
                })}
                <div class="settings-inline-note">
                    Whether a subagent may WRITE — in an isolated git worktree of this repo, an external
                    workspace, or a from-scratch project — and return patches for the parent to review.
                    Read-only subagents are always allowed, and this applies to delegated and API
                    subagents alike. With no explicit choice the runtime mode decides: off in Light,
                    on in Advanced and Pro. <strong>Human controlled:</strong> the agent cannot
                    self-enable it; applies on the next task, no restart.
                </div>
            </div>
        </div>
    `;
}

function currentView() {
    return delegationView({
        saved: state.saved,
        payload: state.payload,
        statusError: state.statusError,
        edit: state.enabled === null ? null : { enabled: state.enabled, harness: state.harness },
    });
}

function renderRows() {
    const host = document.getElementById('subagents-rows');
    if (!host) return;
    const view = currentView();
    const offerControls = view.state !== 'no_subscription' && view.state !== 'unknown';
    const options = view.options.map((item) => {
        const selected = item.id === view.harness ? ' selected' : '';
        return `<option value="${escapeHtml(item.id)}"${selected}>${escapeHtml(item.label)}</option>`;
    }).join('');
    host.innerHTML = `
        <div class="reviewer-slot-row" data-subagent-row>
            ${offerControls ? `
            <div class="reviewer-slot-controls">
                <select data-subagent-delegation aria-label="Delegate subagents">
                    <option value="off"${view.enabled ? '' : ' selected'}>Subagents run on the API</option>
                    <option value="on"${view.enabled ? ' selected' : ''}>Delegate to a coding agent</option>
                </select>
                ${view.enabled ? `<select data-subagent-harness aria-label="Coding agent">${options}</select>` : ''}
            </div>` : ''}
            <div class="reviewer-slot-meta muted">${escapeHtml(view.note)}</div>
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
        renderRows();
        state.onChange();
    });
}

export function applySubagentsSettings(settings) {
    state.saved = String(settings?.OUROBOROS_SUBAGENT_HARNESS ?? '').trim();
    state.enabled = null;
    state.harness = '';
    renderRows();
}

export async function reloadSubagentsSection() {
    try {
        const resp = await apiFetch('/api/claudexor/status', { cache: 'no-store' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        state.payload = data;
        state.statusError = '';
    } catch (error) {
        state.payload = null;
        state.statusError = String(error.message || error);
    }
    state.loaded = true;
    renderRows();
}

export function initSubagentsSection({ onChange } = {}) {
    state.onChange = typeof onChange === 'function' ? onChange : () => {};
    // The initial load is driven by settings.js loadSettings(), which awaits
    // reloadSubagentsSection() BEFORE taking the clean-draft baseline — otherwise
    // the async arrival of the accounts would read as an unsaved edit.
}

export function collectSubagentsSettings() {
    // Never author the route from an UNLOADED or unreadable view: an unrelated
    // save must not turn delegation off because this page could not reach the
    // daemon (same rule as collectReviewerSlots).
    if (!state.loaded || state.statusError) return {};
    const view = currentView();
    if (view.state === 'no_subscription') return {};
    return { OUROBOROS_SUBAGENT_HARNESS: composeSubagentRoute(view.harness, view.suffix) };
}
