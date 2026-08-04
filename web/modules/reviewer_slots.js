// Reviewer slots UI (phase 6.2/6.3) — the Models-page rows over the ONE
// structured setting (OUROBOROS_REVIEWER_SLOTS).
//
// Shape rules are the owner's, verbatim:
//  * ONE GROUPED combobox per slot encodes both the route kind and the target
//    — an API model, or a coding-agent harness. No separate "API or harness"
//    switch, no second target picker, no invalid combinations.
//  * The provider shown for a delegated row is the HARNESS NAME (codex,
//    claude, cursor, …) — never "Claudexor", and never a `provider::model`
//    string syntax.
//  * When a harness is selected the MODEL is a dropdown fed by Claudexor
//    discovery, not free input. API model ids keep the existing free entry
//    (catalog-assisted) the model cards already use.
//  * Effort is the EXISTING per-model effort mechanism, one dropdown per row.
// Capability badges DISPLAY facts; they configure nothing.
//
// Pure helpers live at the top and are node-tested without a DOM.

import { apiFetch } from './api_client.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';

export const ROUTE_KIND_API = 'api_chat';
export const ROUTE_KIND_SESSION = 'agent_session';
export const CUSTOM_API_CHOICE = 'api:__custom__';

export const EFFORT_CHOICES = ['none', 'low', 'medium', 'high', 'xhigh', 'max'];

// ---------------------------------------------------------------------------
// Pure helpers.
// ---------------------------------------------------------------------------

export function mintSlotId(prefix, takenIds) {
    const taken = new Set(takenIds || []);
    for (let attempt = 0; attempt < 1000; attempt += 1) {
        const candidate = `${prefix}_${Math.random().toString(36).slice(2, 8)}`;
        if (!taken.has(candidate)) return candidate;
    }
    return `${prefix}_${Date.now().toString(36)}`;
}

export function encodeRouteChoice(row) {
    const kind = row?.route?.kind || ROUTE_KIND_API;
    const target = String(row?.route?.target_id || '');
    if (kind === ROUTE_KIND_SESSION) {
        return `session:${splitSessionTarget(target).harness}`;
    }
    return `api:${target}`;
}

export function decodeRouteChoice(value) {
    const raw = String(value || '');
    if (raw.startsWith('session:')) {
        return { kind: ROUTE_KIND_SESSION, harness: raw.slice('session:'.length) };
    }
    if (raw === CUSTOM_API_CHOICE) return { kind: ROUTE_KIND_API, custom: true };
    if (raw.startsWith('api:')) return { kind: ROUTE_KIND_API, target: raw.slice('api:'.length) };
    return { kind: ROUTE_KIND_API, target: raw };
}

// Claudexor's own reviewer-panel spelling: harness[=model]. Never '::'.
export function composeSessionTarget(harness, model) {
    const h = String(harness || '').trim();
    const m = String(model || '').trim();
    return m ? `${h}=${m}` : h;
}

export function splitSessionTarget(target) {
    const raw = String(target || '');
    const eq = raw.indexOf('=');
    if (eq < 0) return { harness: raw, model: '' };
    return { harness: raw.slice(0, eq), model: raw.slice(eq + 1) };
}

export function routeChoiceGroups({ catalogModels = [], harnesses = [], currentApiTargets = [] } = {}) {
    const apiValues = [];
    const seen = new Set();
    for (const model of [...currentApiTargets, ...catalogModels]) {
        const id = String(model || '').trim();
        if (!id || seen.has(id)) continue;
        seen.add(id);
        apiValues.push({ value: `api:${id}`, label: id });
    }
    apiValues.push({ value: CUSTOM_API_CHOICE, label: 'API model — enter id…' });
    const sessionValues = (harnesses || [])
        .filter((h) => h && h.id)
        .map((h) => ({
            // Provider = the harness name itself; the engine underneath is an
            // implementation detail the row never spells.
            value: `session:${h.id}`,
            label: `${h.display_name || h.id} (coding agent)`,
            disabled: h.status && h.status !== 'ok' && !h.enabled,
        }));
    const groups = [{ label: 'API models', options: apiValues }];
    // With no daemon the group used to VANISH, while the section copy above still
    // promised subscription delivery — the owner saw a broken promise and nowhere
    // to go. Say why it is empty instead of hiding it. Name the DESTINATION by
    // its tab: Harness Accounts lives in the Providers panel, this row lives in
    // Models, so "below" pointed at a page that does not contain it.
    groups.push(sessionValues.length
        ? { label: 'Coding agents — subscriptions', options: sessionValues }
        : { label: 'Coding agents — subscriptions', options: [{
            value: '', disabled: true,
            label: 'None available — sign in under Providers → Harness Accounts',
        }] });
    return groups;
}

export function indexProfilesByHarness(payload) {
    // The REAL ControlCredentialProfilesResponse shape, same reader as
    // harness_accounts.accountRows: `profiles` is an array of WRAPPERS
    // `{profile, status, identity}` carrying snake_case fields. Reading flat
    // camelCase off the wrapper matched nothing, so every session row's
    // credential-profile picker was silently empty. Exported so the wire test can
    // hold it against the same golden body the account list consumes.
    const byHarness = {};
    const profiles = payload?.profiles?.profiles || [];
    for (const wrapper of Array.isArray(profiles) ? profiles : []) {
        const profile = wrapper?.profile || {};
        const harness = String(profile.harness_id || '');
        const id = String(profile.profile_id || '');
        if (!harness || !id) continue;
        (byHarness[harness] = byHarness[harness] || []).push(id);
    }
    return byHarness;
}

export function buildReviewerSlotsSetting(state) {
    const rowOut = (row) => {
        const out = {
            slot_id: String(row.slot_id || ''),
            route: { kind: row.route.kind, target_id: String(row.route.target_id || '') },
        };
        if (row.route.kind === ROUTE_KIND_SESSION && row.route.profile_id) {
            out.route.profile_id = String(row.route.profile_id);
        }
        if (row.effort) out.effort = String(row.effort);
        return out;
    };
    const advisory = state.advisory || {};
    const advisoryOut = {
        enabled: advisory.enabled !== false,
        route: { kind: advisory.route?.kind === ROUTE_KIND_SESSION ? ROUTE_KIND_SESSION : 'api',
                 target_id: String(advisory.route?.target_id || '') },
        effort: advisory.effort || 'low',
    };
    if (advisoryOut.route.kind === ROUTE_KIND_SESSION && advisory.route?.profile_id) {
        advisoryOut.route.profile_id = String(advisory.route.profile_id);
    }
    return JSON.stringify({
        triad: (state.triad || []).map(rowOut),
        scope: (state.scope || []).map(rowOut),
        advisory: advisoryOut,
    });
}

export function describeLastExecution(entry) {
    if (!entry || typeof entry !== 'object') return '';
    const effective = entry.effective || {};
    const parts = [`runs as ${effective.route || 'api_chat'}`];
    // APPLIED facts only: a session run whose telemetry predates the engine
    // receipt shows honest absence, never the requested value as applied.
    if (effective.model) parts.push(`model ${effective.model}`);
    else if (String(effective.route || '').startsWith('agent_session')) parts.push('model not disclosed');
    if (effective.profile_id) parts.push(`account ${effective.profile_id}`);
    if (effective.access) parts.push(`access ${effective.access}`);
    // No applied effort is rendered: none exists upstream, so the key is not emitted.
    if (effective.verdict_method && effective.verdict_method !== 'structured'
        && effective.verdict_method !== 'strict_parse') {
        parts.push(`verdict via ${effective.verdict_method.replace(/_/g, ' ')}`);
    }
    const deltas = Array.isArray(entry.capability_delta) ? entry.capability_delta.length : 0;
    if (deltas) parts.push(`${deltas} capability delta${deltas === 1 ? '' : 's'} disclosed`);
    if (entry.ts) parts.push(`at ${entry.ts}`);
    return parts.join(' · ');
}

export function profileOptionsFor(profiles, savedPin) {
    // Mirrors the model list's own rule: a SAVED pin the daemon no longer discovers
    // (account signed out, daemon down, profile renamed) matched no option, so the
    // select fell back to its first entry and redrew the row as "automatic rotation".
    // The pin only LOOKED gone — until the owner saved the panel, which then really
    // did delete it, silently widening which account the reviewer may spend.
    const options = [{ value: '', label: 'Account: automatic rotation' },
        ...(profiles || []).map((p) => ({ value: p, label: `Account: ${p} (pinned)` }))];
    if (savedPin && !options.some((o) => o.value === savedPin)) {
        options.push({ value: savedPin, label: `Account: ${savedPin} (not in discovery)` });
    }
    return options;
}

export function capabilityBadge(row, harnessesById) {
    // DISPLAY-only facts: never a control (6.2).
    if (row.route.kind === ROUTE_KIND_SESSION) {
        const harness = harnessesById?.[splitSessionTarget(row.route.target_id).harness];
        const status = harness ? (harness.status || 'unknown') : 'not discovered';
        return `agent session — retrieves context with its own tools · route ${status}`;
    }
    return 'api pack delivery';
}

// ---------------------------------------------------------------------------
// DOM section (Models page). State is module-local; collect is synchronous.
// ---------------------------------------------------------------------------

const state = {
    loaded: false,
    configError: '',
    loadError: '',
    source: '',
    triad: [],
    scope: [],
    advisory: { enabled: true, route: { kind: 'api', target_id: '' }, effort: 'low' },
    limits: { triad: 10, scope: 4, advisory: 1 },
    lastExecutions: {},
    catalogModels: [],
    harnesses: [],
    profilesByHarness: {},
    onChange: () => {},
};

export function renderReviewerSlotsSection() {
    return `
        <div class="form-section" id="reviewer-slots-section">
            <h3>Reviewer Slots</h3>
            <div class="settings-section-copy">
                Commit review rows: the triad, the scope reviewers, and one optional advisory
                pre-reviewer. Each row picks its delivery in one list — an API model, or a coding
                agent running on your subscription — plus its own reasoning effort.
                Agent-session delivery applies to <strong>commit review only</strong>: plan review,
                task acceptance and skill review run on the API reviewer rows. If you delegate
                <em>every</em> commit-review or scope row, those API surfaces fall back to the shipped
                default models and spend API budget — keep at least one API row to avoid it.
            </div>
            <div id="reviewer-slots-error" class="ui-status" data-tone="error" hidden></div>
            <h4 class="reviewer-slots-heading">Triad slots <span class="muted" id="reviewer-triad-limit"></span></h4>
            <div id="reviewer-triad-rows" class="reviewer-slot-rows"></div>
            <button type="button" class="settings-ghost-btn" id="btn-add-triad-slot">Add triad slot</button>
            <h4 class="reviewer-slots-heading">Scope slots <span class="muted" id="reviewer-scope-limit"></span></h4>
            <div id="reviewer-scope-rows" class="reviewer-slot-rows"></div>
            <button type="button" class="settings-ghost-btn" id="btn-add-scope-slot">Add scope slot</button>
            <h4 class="reviewer-slots-heading">Advisory pre-reviewer</h4>
            <div id="reviewer-advisory-row" class="reviewer-slot-rows"></div>
            <div class="settings-inline-note">
                Disabling the advisory is a standing decision with a constitutional consequence:
                every reviewed commit then records an <strong>audited bypass</strong> instead of an
                advisory verdict. Nothing is skipped silently.
            </div>
        </div>
    `;
}

function harnessesById() {
    const map = {};
    for (const h of state.harnesses) map[h.id] = h;
    return map;
}

function selectHtml(attrs, groups, selected) {
    const options = groups.map((group) => {
        const body = group.options.map((opt) => {
            const sel = opt.value === selected ? ' selected' : '';
            const dis = opt.disabled ? ' disabled' : '';
            return `<option value="${escapeHtml(opt.value)}"${sel}${dis}>${escapeHtml(opt.label)}</option>`;
        }).join('');
        return group.label ? `<optgroup label="${escapeHtml(group.label)}">${body}</optgroup>` : body;
    }).join('');
    return `<select ${attrs}>${options}</select>`;
}

function effortSelectHtml(attrs, selected, surfaceDefault) {
    const options = [
        { value: '', label: `Default (${surfaceDefault})` },
        ...EFFORT_CHOICES.map((effort) => ({ value: effort, label: effort })),
    ];
    return selectHtml(attrs, [{ label: '', options }], selected || '');
}

function rowHtml(row, group) {
    const choice = encodeRouteChoice(row);
    const groups = routeChoiceGroups({
        catalogModels: state.catalogModels,
        harnesses: state.harnesses,
        currentApiTargets: row.route.kind === ROUTE_KIND_API ? [row.route.target_id] : [],
    });
    const session = row.route.kind === ROUTE_KIND_SESSION;
    const split = session ? splitSessionTarget(row.route.target_id) : { harness: '', model: '' };
    const harness = session ? harnessesById()[split.harness] : null;
    const models = harness?.models || [];
    const modelOptions = [{ value: '', label: 'Engine default model' },
        ...models.map((m) => ({ value: String(m.id || m.value || m), label: String(m.id || m.label || m) }))];
    if (split.model && !modelOptions.some((o) => o.value === split.model)) {
        modelOptions.push({ value: split.model, label: `${split.model} (not in discovery)` });
    }
    const profiles = session ? (state.profilesByHarness[split.harness] || []) : [];
    const profileOptions = profileOptionsFor(profiles, row.route.profile_id);
    const last = state.lastExecutions[row.slot_id];
    const surfaceDefault = group === 'scope' ? 'scope review effort' : 'review effort';
    return `
        <div class="reviewer-slot-row" data-slot-group="${group}" data-slot-id="${escapeHtml(row.slot_id)}">
            <div class="reviewer-slot-controls">
                ${selectHtml(`data-slot-route aria-label="Reviewer route"`, groups, choice)}
                ${row._customApi ? `<input data-slot-custom-api placeholder="provider/model-id" value="${escapeHtml(row.route.target_id || '')}" spellcheck="false">` : ''}
                ${session ? selectHtml('data-slot-model aria-label="Harness model"', [{ label: '', options: modelOptions }], split.model) : ''}
                ${session && profileOptions.length > 1 ? selectHtml('data-slot-profile aria-label="Credential account"', [{ label: '', options: profileOptions }], row.route.profile_id || '') : ''}
                ${effortSelectHtml('data-slot-effort aria-label="Reasoning effort"', row.effort, surfaceDefault)}
                <button type="button" class="settings-ghost-btn" data-slot-remove title="Remove this slot">Remove</button>
            </div>
            <div class="reviewer-slot-meta muted">${escapeHtml(capabilityBadge(row, harnessesById()))}</div>
            ${last ? `<div class="reviewer-slot-runs-as muted" title="UI projection of capability_delta (D22)">Last run: ${escapeHtml(describeLastExecution(last))}</div>` : ''}
        </div>
    `;
}

function advisoryHtml() {
    const advisory = state.advisory;
    const session = advisory.route?.kind === ROUTE_KIND_SESSION;
    const groups = [
        { label: '', options: [{ value: 'api:', label: 'Claude (Anthropic API key)' }] },
        ...routeChoiceGroups({ catalogModels: [], harnesses: state.harnesses }).slice(1),
    ];
    const choice = session ? `session:${splitSessionTarget(advisory.route.target_id).harness}` : 'api:';
    return `
        <div class="reviewer-slot-row" data-advisory-row>
            <div class="reviewer-slot-controls">
                <label class="local-toggle"><input type="checkbox" data-advisory-enabled ${advisory.enabled !== false ? 'checked' : ''}> Enabled</label>
                ${selectHtml('data-advisory-route aria-label="Advisory route"', groups, choice)}
                ${effortSelectHtml('data-advisory-effort aria-label="Advisory effort"', advisory.effort === 'low' ? '' : advisory.effort, 'low')}
            </div>
            ${state.lastExecutions.advisory_slot_1 ? `<div class="reviewer-slot-runs-as muted" title="UI projection of capability_delta (D22)">Last run: ${escapeHtml(describeLastExecution(state.lastExecutions.advisory_slot_1))}</div>` : ''}
        </div>
    `;
}

function renderRows() {
    const errorBox = document.getElementById('reviewer-slots-error');
    if (errorBox) {
        errorBox.hidden = !(state.configError || state.loadError);
        errorBox.textContent = state.configError
            ? `Saved reviewer-slot configuration is invalid and blocks reviews: ${state.configError}. `
              + 'To repair it, add at least one triad slot and one scope slot below, then Save'
              + (state.triad.length && state.scope.length ? '.' : ' — an incomplete set is not saved.')
            : (state.loadError
                ? `Could not reach the reviewer-slot settings — ${state.loadError}. Your saved configuration is unchanged; retry when the connection is back.`
                : '');
    }
    const triadBox = document.getElementById('reviewer-triad-rows');
    const scopeBox = document.getElementById('reviewer-scope-rows');
    const advisoryBox = document.getElementById('reviewer-advisory-row');
    if (!triadBox || !scopeBox || !advisoryBox) return;
    triadBox.innerHTML = state.triad.map((row) => rowHtml(row, 'triad')).join('')
        || '<div class="muted">No triad slots configured.</div>';
    scopeBox.innerHTML = state.scope.map((row) => rowHtml(row, 'scope')).join('')
        || '<div class="muted">No scope slots configured.</div>';
    advisoryBox.innerHTML = advisoryHtml();
    const triadLimit = document.getElementById('reviewer-triad-limit');
    if (triadLimit) triadLimit.textContent = `(${state.triad.length}/${state.limits.triad} — the commit gate's real ceiling)`;
    const scopeLimit = document.getElementById('reviewer-scope-limit');
    if (scopeLimit) scopeLimit.textContent = `(${state.scope.length}/${state.limits.scope} — the scope pool's real width)`;
    const addTriad = document.getElementById('btn-add-triad-slot');
    if (addTriad) addTriad.disabled = state.triad.length >= state.limits.triad;
    const addScope = document.getElementById('btn-add-scope-slot');
    if (addScope) addScope.disabled = state.scope.length >= state.limits.scope;
    bindRowEvents();
}

function findRow(group, slotId) {
    const rows = group === 'scope' ? state.scope : state.triad;
    return rows.find((row) => row.slot_id === slotId) || null;
}

function bindRowEvents() {
    const section = document.getElementById('reviewer-slots-section');
    if (!section) return;
    section.querySelectorAll('.reviewer-slot-row[data-slot-id]').forEach((rowEl) => {
        const group = rowEl.dataset.slotGroup;
        const row = findRow(group, rowEl.dataset.slotId);
        if (!row) return;
        rowEl.querySelector('[data-slot-route]')?.addEventListener('change', (event) => {
            const decoded = decodeRouteChoice(event.target.value);
            if (decoded.kind === ROUTE_KIND_SESSION) {
                row.route = { kind: ROUTE_KIND_SESSION, target_id: decoded.harness, profile_id: '' };
                row._customApi = false;
            } else if (decoded.custom) {
                row.route = { kind: ROUTE_KIND_API, target_id: '' };
                row._customApi = true;
            } else {
                row.route = { kind: ROUTE_KIND_API, target_id: decoded.target };
                row._customApi = false;
            }
            renderRows();
            state.onChange();
        });
        rowEl.querySelector('[data-slot-custom-api]')?.addEventListener('input', (event) => {
            row.route.target_id = String(event.target.value || '').trim();
            state.onChange();
        });
        rowEl.querySelector('[data-slot-model]')?.addEventListener('change', (event) => {
            const split = splitSessionTarget(row.route.target_id);
            row.route.target_id = composeSessionTarget(split.harness, event.target.value);
            state.onChange();
        });
        rowEl.querySelector('[data-slot-profile]')?.addEventListener('change', (event) => {
            row.route.profile_id = String(event.target.value || '');
            state.onChange();
        });
        rowEl.querySelector('[data-slot-effort]')?.addEventListener('change', (event) => {
            row.effort = String(event.target.value || '');
            state.onChange();
        });
        rowEl.querySelector('[data-slot-remove]')?.addEventListener('click', () => {
            const rows = group === 'scope' ? state.scope : state.triad;
            const index = rows.indexOf(row);
            if (index >= 0) rows.splice(index, 1);
            renderRows();
            state.onChange();
        });
    });
    const advisoryEl = section.querySelector('[data-advisory-row]');
    if (advisoryEl) {
        advisoryEl.querySelector('[data-advisory-enabled]')?.addEventListener('change', (event) => {
            state.advisory.enabled = Boolean(event.target.checked);
            state.onChange();
        });
        advisoryEl.querySelector('[data-advisory-route]')?.addEventListener('change', (event) => {
            const decoded = decodeRouteChoice(event.target.value);
            state.advisory.route = decoded.kind === ROUTE_KIND_SESSION
                ? { kind: ROUTE_KIND_SESSION, target_id: decoded.harness }
                : { kind: 'api', target_id: '' };
            renderRows();
            state.onChange();
        });
        advisoryEl.querySelector('[data-advisory-effort]')?.addEventListener('change', (event) => {
            state.advisory.effort = String(event.target.value || '') || 'low';
            state.onChange();
        });
    }
}

function addRow(group) {
    const rows = group === 'scope' ? state.scope : state.triad;
    const limit = group === 'scope' ? state.limits.scope : state.limits.triad;
    if (rows.length >= limit) return;
    const taken = [...state.triad, ...state.scope].map((row) => row.slot_id);
    rows.push({
        slot_id: mintSlotId(group === 'scope' ? 'scope' : 'triad', taken),
        route: { kind: ROUTE_KIND_API, target_id: '' },
        effort: '',
        _customApi: true,
    });
    renderRows();
    state.onChange();
}

export async function reloadReviewerSlots() {
    try {
        const resp = await apiFetch('/api/reviewer-slots', { cache: 'no-store' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        state.loadError = '';
        state.configError = String(data.config_error || '');
        state.source = String(data.source || '');
        state.limits = data.limits || state.limits;
        state.lastExecutions = data.last_executions || {};
        state.triad = Array.isArray(data.triad) ? data.triad.map((row) => ({ ...row })) : [];
        state.scope = Array.isArray(data.scope) ? data.scope.map((row) => ({ ...row })) : [];
        state.advisory = data.advisory
            ? { ...data.advisory, route: { ...(data.advisory.route || {}) } }
            : state.advisory;
        // The VIEW loaded even when the saved value is invalid — that is exactly the
        // state the owner repairs from, and treating it as "not loaded" made the
        // save drop the repair (see collectReviewerSlots).
        state.loaded = true;
    } catch (error) {
        // A transport failure is NOT a verdict on the saved configuration: the
        // config-error banner accuses the owner's settings of blocking review, and
        // a network blip must never say that. Separate field, separate sentence.
        state.loaded = false;
        state.loadError = `could not load reviewer slots: ${error.message || error}`;
    }
    try {
        const resp = await apiFetch('/api/claudexor/status?include=models', { cache: 'no-store' });
        const data = await resp.json().catch(() => ({}));
        state.harnesses = Array.isArray(data.harnesses) ? data.harnesses : [];
        state.profilesByHarness = indexProfilesByHarness(data);
    } catch (error) {
        state.harnesses = [];
        state.profilesByHarness = {};
    }
    renderRows();
}

export function initReviewerSlots({ onChange } = {}) {
    state.onChange = typeof onChange === 'function' ? onChange : () => {};
    document.getElementById('btn-add-triad-slot')?.addEventListener('click', () => addRow('triad'));
    document.getElementById('btn-add-scope-slot')?.addEventListener('click', () => addRow('scope'));
    document.addEventListener('settings-model-catalog:updated', (event) => {
        const items = event?.detail?.items || [];
        state.catalogModels = items.map((item) => String(item.value || item.id || '')).filter(Boolean);
        renderRows();
    });
    // The initial load is driven by settings.js loadSettings(), which awaits
    // reloadReviewerSlots() BEFORE taking the clean-draft baseline — otherwise
    // the async arrival of the rows would read as an unsaved edit.
}

export function collectReviewerSlots() {
    // Never author the setting from an UNLOADED view: an unrelated save must not
    // overwrite the owner's configuration with an empty page. A config_error is
    // NOT that case — the stored value is already invalid and blocking review, the
    // endpoint returns no rows with it, and refusing here made the documented
    // repair path swallow the owner's replacement rows and still report success.
    if (state.loadError || !state.loaded) return {};
    if (!state.triad.length || !state.scope.length) return {};
    return { OUROBOROS_REVIEWER_SLOTS: buildReviewerSlotsSetting(state) };
}
