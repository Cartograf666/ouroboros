// Harness Accounts (D30) — the Providers-tab section over the owned Claudexor
// daemon's account surface, MCP-card style: one row per account, two HONEST
// verification statuses, quota with a resets_at timer, login cards.
//
// The status payload comes from the SHARED store (`claudexor_status_store.js`)
// — this section no longer owns a private poll — and the login card is the
// SHARED controller (`harness_login_cards.js`), so the onboarding wizard mounts
// the same flow instead of reimplementing device codes, backoff and the
// verify-race.
//
// Pure helpers up top are node-tested without a DOM.

import {
    FACET_ACCOUNTS,
    READ_FAILED,
    READ_OK,
    READ_TRANSPORT,
    READ_UNREAD,
    accountRows,
    claudexorStatus,
    statusUnavailableNote,
} from './claudexor_status_store.js';
import { openConfirmDialog } from './confirm_dialog.js';
import { createLoginCardController } from './harness_login_cards.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';

// ---------------------------------------------------------------------------
// Pure helpers.
// ---------------------------------------------------------------------------

export function verificationBadge(profile) {
    // Q2-а: both statuses are shown honestly — vendor-verified is trusted,
    // local-store presence stays labeled "not verified live" in WORDS, but in
    // a NEUTRAL tone (owner finding #2): the engine has no vendor probe for
    // some harnesses (cursor), so a warning-toned "not verified" there is a
    // permanent alarm nothing can clear — noise, not honesty. "local session"
    // is the daemon's own name for the route (next_up.route).
    const status = profile?.status || profile || {};
    const source = String(status.verification_source || '');
    const verification = String(status.verification || '');
    const at = String(status.last_verified_at || '');
    if (source === 'vendor' && verification === 'passed') {
        return { tone: 'ok', label: `verified live${at ? ` ${at}` : ''}` };
    }
    if (verification === 'passed') {
        return { tone: 'muted', label: 'local session — not verified live' };
    }
    if (verification) {
        return { tone: 'error', label: `verification ${verification}` };
    }
    return { tone: 'muted', label: 'not logged in' };
}

export function quotaSummary(snapshots, harnessId, subjectId = '') {
    // The exhausted window is SHOWN with its reset time, never hidden (Q2-б):
    // hiding it would make the D28 fallback to API money unexplainable.
    const rows = (snapshots || []).filter((snap) => {
        const subject = snap?.subject || {};
        if (String(subject.harness || '') !== String(harnessId)) return false;
        // EXACT subject, including the default account's empty id. The old
        // `!subjectId ||` wildcard made the native row match EVERY subject on the
        // harness, so the default account reported a named profile's exhausted
        // window — red styling and all — as its own.
        if (String(subject.subject_id || '') !== String(subjectId)) return false;
        // The RUNTIME ignores a stale reading ("an old reading must not block a lane",
        // subagents.py `harness_window_wait_hint`), so a card that paints one red is
        // reporting a block that will not happen: the lane still dispatches. Same bar,
        // same answer, on both sides of the glass.
        return String(snap?.freshness || '') === 'fresh';
    });
    let worst = null;
    // The runtime's own bar, per snapshot: spent when a constraint is cooling down OR
    // its window is fully used — ANY constraint, not just the one with the highest
    // ratio. Reading exhaustion off `worst` alone hid a cooling constraint whenever
    // some other window happened to report a larger used_ratio, and dropped it
    // entirely when the cooling one reported no ratio at all.
    let exhausted = false;
    let exhaustedResetsAt = '';
    const scopedSpent = [];
    for (const snap of rows) {
        for (const constraint of snap.constraints || []) {
            const used = Number(constraint.used_ratio);
            const spent = Boolean(constraint.cooldown_until) || (Number.isFinite(used) && used >= 1.0);
            const models = Array.isArray(constraint.applies_to_models)
                ? constraint.applies_to_models.filter(Boolean) : [];
            if (models.length) {
                // A non-null applies_to_models is a PER-MODEL cap — the daemon
                // schema's own words: "a model-specific cap never cools a
                // different model on the same subject" (@claudexor/schema
                // quota.ts). So it must never paint the whole account
                // exhausted, and its ratio is not the account's bar: a spent
                // scope becomes a compact note beside the account label.
                if (spent) scopedSpent.push(String(constraint.label || constraint.id || models.join(', ')));
                continue;
            }
            if (spent && !exhausted) {
                exhausted = true;
                exhaustedResetsAt = String(constraint.cooldown_until || constraint.resets_at || '');
            }
            if (!Number.isFinite(used)) continue;
            if (!worst || used > worst.used) {
                worst = { used, resetsAt: String(constraint.resets_at || constraint.cooldown_until || '') };
            }
        }
    }
    const note = scopedSpent.length ? `${[...new Set(scopedSpent)].join(', ')} spent` : '';
    if (!worst && !exhausted && !note) return { label: '', exhausted: false, resetsAt: '' };
    const resetsAt = exhausted ? (exhaustedResetsAt || worst?.resetsAt || '') : (worst?.resetsAt || '');
    const base = exhausted
        ? `window exhausted — resets ${resetsAt || 'soon'}`
        : (worst ? `${Math.min(100, Math.round(worst.used * 100))}% of window used` : '');
    return {
        exhausted,
        resetsAt,
        label: [base, note].filter(Boolean).join(' · '),
    };
}

export function normalizeProfileName(raw) {
    // The profile-id alphabet the login request accepts: lowercased, and every
    // character outside it becomes '-'.
    return String(raw || '').trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-');
}

export async function promptProfileName({ dialogImpl = openConfirmDialog } = {}) {
    // pywebview's WKWebView implements no window.prompt — it answers null
    // silently, so the old prompt()-based Add-account flow was a dead button on
    // the desktop app. The in-house input dialog asks instead, and it loops
    // until the typed name already IS its normalized form: a name that
    // normalization would change ("Работа" → "------", "Work" → "work") is
    // shown back, editable, BEFORE any login starts — never rewritten silently.
    let initialValue = '';
    let body = 'Name for the additional account (e.g. work, backup).'
        + ' Lowercase letters, digits, "-" and "_" — anything else becomes "-".';
    for (;;) {
        const answer = await dialogImpl({ title: 'Add account', body, input: true, initialValue });
        if (!answer?.confirmed) return '';
        const raw = String(answer.value || '').trim();
        const normalized = normalizeProfileName(raw);
        if (!normalized) return '';
        if (normalized === raw) return normalized;
        initialValue = normalized;
        body = `"${raw}" will be saved as "${normalized}" — edit the name or continue.`;
    }
}

export function runtimeActionLabel(payload) {
    const state = String(payload?.daemon?.runtime?.state || '');
    if (state === 'error') return 'Fix & connect';
    if (state === 'missing') return 'Install & connect';
    if (state === 'update_available') return 'Update & connect';
    return 'Connect';
}

export function daemonStatusLine(payload, { checking = false } = {}) {
    const daemon = payload?.daemon || {};
    const runtime = daemon.runtime || {};
    const runtimeState = String(runtime.state || '');
    const status = String(daemon.state || 'unknown');
    // Nothing read yet and a read in flight: SAY so, and say what it costs. The
    // daemon re-probes every coding-agent CLI on each read, so first paint is
    // tens of seconds — and an unexplained silent panel reads as "broken", not
    // as "loading" (owner report, 2026-08-08).
    if (checking && !daemon.state) {
        return { tone: 'muted', text: 'Checking Claudexor… the first read probes each coding-agent CLI and can take a minute or more.' };
    }
    if (daemon.ownership_problem) {
        return { tone: 'error', text: `This daemon home is not managed from here: ${daemon.ownership_problem}` };
    }
    if (runtimeState === 'installing') {
        const version = runtime.target_version ? ` ${runtime.target_version}` : '';
        return { tone: 'muted', text: `Installing or checking Claudexor${version}…` };
    }
    if (runtimeState === 'error') {
        const detail = runtime.last_error ? `: ${runtime.last_error}.` : '.';
        return { tone: 'error', text: `Claudexor needs repair${detail} Connect retries automatically.` };
    }
    if (runtimeState === 'update_staged') {
        const target = runtime.staged_version || runtime.target_version || '?';
        const current = daemon.engine_version || runtime.version || '?';
        return { tone: 'warn', text: `Claudexor ${target} is ready and will activate after the daemon next restarts. Engine ${current} keeps running until then.` };
    }
    if (status === 'running') {
        return { tone: 'ok', text: `Claudexor ready (engine ${daemon.engine_version || '?'}) · home ${payload.config_dir || ''}` };
    }
    if (status === 'not_provisioned') {
        if (runtimeState === 'ready') {
            const version = runtime.version ? ` ${runtime.version}` : '';
            return { tone: 'muted', text: `Claudexor${version} is ready. Connect an account to start Ouroboros’s own agent daemon.` };
        }
        return { tone: 'muted', text: 'No accounts connected yet. Connect installs Claudexor and starts Ouroboros’s own agent daemon automatically.' };
    }
    if (status === 'stale') {
        // NOT a warning: the daemon is LAZY by design (the status read never
        // spawns it), so "home exists, nothing answering" is the ordinary idle
        // state, not a fault. Lead with what is true and what happens next; a
        // genuine RUNTIME fault renders through the `error` branch above.
        // Disclosed residual (both review lenses, 2026-08-08): `stale` is also
        // what a CRASHED daemon lands in — the state machine cannot tell the two
        // apart (the detail lives only in last_error, which the warn-toned line
        // never showed either), so the only thing a crash loses here is the
        // alarming tone. The sentence stays true for it: ensure_running restarts
        // a dead daemon on the next login or delegated run, and a crash mid-run
        // surfaces through that run's own typed failure, not this panel. Hence
        // no "yet" — that word would claim it had never started.
        const version = runtime.version ? ` ${runtime.version}` : '';
        return { tone: 'muted', text: `Claudexor${version} is installed; the agent daemon is not running. It starts automatically on the next login or delegated run.` };
    }
    if (status === 'foreign_daemon') {
        return { tone: 'warn', text: 'Another daemon answered on the stale port (not ours — left untouched). The next login restarts our own daemon on a fresh port.' };
    }
    return { tone: 'error', text: `Daemon ${status}${daemon.last_error ? `: ${daemon.last_error}` : ''}` };
}

// The coding-agent families a fresh install can connect BEFORE the daemon
// exists. Discovery needs a running daemon, and on first run there is none —
// so with nothing discovered the UI still offers a Connect affordance, and the
// first Connect is exactly what provisions the owned daemon (D30). Presentation
// only; the login flow itself stays harness-agnostic (asks device_auth, falls
// back to the terminal command).
export const BOOTSTRAP_HARNESSES = ['codex', 'claude', 'cursor'];

// Re-exported so the accounts surface keeps ONE import path for the payload
// projection it renders (the definition lives with the store that owns the
// payload).
export { accountRows };

// ---------------------------------------------------------------------------
// DOM section.
// ---------------------------------------------------------------------------

const state = {
    store: claudexorStatus,
    loginCard: null,
    disposers: [],
    initialized: false,
};

export function renderHarnessAccountsSection() {
    return `
        <div class="form-section" id="harness-accounts-section">
            <h3>Harness Accounts</h3>
            <div class="settings-section-copy">
                Coding-agent subscriptions (Codex CLI, Claude Code, Cursor) used by delegated
                subagents and reviewer slots. Accounts live in Ouroboros's own agent home —
                your personal logins are never read or imported. Claudexor is installed,
                repaired, and updated automatically when you connect or start delegated work;
                login and verification remain the agent daemon's own flows.
            </div>
            <div id="harness-daemon-status" class="settings-inline-status">Checking daemon…</div>
            <div id="harness-accounts-rows" class="settings-secret-list"></div>
            <div id="harness-login-card"></div>
            <div class="settings-toolbar">
                <button type="button" class="settings-ghost-btn" id="btn-harness-refresh">Refresh</button>
            </div>
        </div>
    `;
}

function rowHtml(row, payload) {
    const badge = verificationBadge(row);
    const quota = quotaSummary(payload?.quota || [], row.harness, row.profile_id);
    const identity = [row.identity?.email, row.identity?.plan].filter(Boolean).join(' · ');
    const name = row.kind === 'native'
        ? `${row.harness} — default account`
        : `${row.harness} — ${row.profile_id}`;
    return `
        <div class="harness-account-row${quota.exhausted ? ' harness-exhausted' : ''}" data-harness="${escapeHtml(row.harness)}" data-profile="${escapeHtml(row.profile_id)}">
            <div class="harness-account-main">
                <span class="harness-chip" data-harness-chip="${escapeHtml(row.harness)}">${escapeHtml(row.harness)}</span>
                <strong>${escapeHtml(name)}</strong>
                ${identity ? `<span class="muted">${escapeHtml(identity)}</span>` : ''}
                <span class="settings-inline-status" data-tone="${badge.tone}">${escapeHtml(badge.label)}</span>
                ${quota.label ? `<span class="settings-inline-status" data-tone="${quota.exhausted ? 'warn' : 'muted'}" ${quota.exhausted && quota.resetsAt ? `data-resets-at="${escapeHtml(quota.resetsAt)}"` : ''}>${escapeHtml(quota.label)}</span>` : ''}
            </div>
            <div class="harness-account-actions">
                <button type="button" class="settings-ghost-btn" data-harness-login>${runtimeActionLabel(payload)}</button>
                ${row.kind === 'native' ? '<button type="button" class="settings-ghost-btn" data-harness-add-profile title="Register one more account for this agent (rotation uses every enabled account)">Add account…</button>' : ''}
            </div>
        </div>
    `;
}

function harnessesWithoutRows(payload, rows) {
    const covered = new Set(rows.map((row) => row.harness));
    const discovered = (payload?.harnesses || [])
        .map((h) => String(h.id || ''))
        .filter(Boolean);
    // When the daemon has discovered nothing yet (fresh install / not
    // provisioned), fall back to the bootstrap families so the first login is
    // reachable — otherwise the whole onboarding path is a dead end.
    const source = discovered.length ? discovered : BOOTSTRAP_HARNESSES;
    return source.filter((id) => !covered.has(id));
}

export function sectionStatusLine(store) {
    // ONE line above the rows. A transport failure is NOT a daemon verdict —
    // the payload in hand describes a past read, never the current one — so it
    // gets the shared "nobody could be asked" sentence instead of a daemon-state
    // claim the read never established. Same for a facet a RUNNING daemon
    // REFUSED: the lifecycle line would print "Claudexor ready" over a list
    // that was never delivered. Every other state is the existing lifecycle
    // line, which already names each not-running daemon state precisely.
    const accounts = store.facet(FACET_ACCOUNTS);
    if (accounts === READ_TRANSPORT || accounts === READ_FAILED) {
        return store.unavailableNote(FACET_ACCOUNTS);
    }
    return daemonStatusLine(store.snapshot || {}, {
        checking: store.loading && !store.everSettled,
    });
}

export function bareRowStatusText(accountsRead) {
    // The verdict beside a harness with NO row. "no account connected" is a
    // claim about the ACCOUNT STORE, and it may only be made once that store
    // was actually read: an idle daemon is never asked, so the emptiness says
    // nothing (BIBLE P1 — a gap is not a zero). The Connect button stays in
    // every case; onboarding must remain reachable.
    if (accountsRead === READ_OK) return 'no account connected';
    if (accountsRead === READ_UNREAD) return 'checking…';
    if (accountsRead === READ_TRANSPORT) return 'not checked — the status request did not complete';
    if (accountsRead === READ_FAILED) return 'not checked — the daemon did not answer this read';
    return 'not checked — the agent daemon is not running';
}

function renderRows() {
    const host = document.getElementById('harness-accounts-rows');
    const statusEl = document.getElementById('harness-daemon-status');
    if (!host || !statusEl) return;
    const payload = state.store.snapshot || {};
    const line = sectionStatusLine(state.store);
    statusEl.textContent = line.text;
    statusEl.dataset.tone = line.tone;
    const rows = accountRows(payload);
    const bare = harnessesWithoutRows(payload, rows);
    const bareStatus = bareRowStatusText(state.store.facet(FACET_ACCOUNTS));
    const parts = rows.map((row) => rowHtml(row, payload));
    for (const harness of bare) {
        parts.push(`
            <div class="harness-account-row" data-harness="${escapeHtml(harness)}" data-profile="">
                <div class="harness-account-main">
                    <span class="harness-chip" data-harness-chip="${escapeHtml(harness)}">${escapeHtml(harness)}</span>
                    <strong>${escapeHtml(harness)}</strong>
                    <span class="settings-inline-status" data-tone="muted">${escapeHtml(bareStatus)}</span>
                </div>
                <div class="harness-account-actions">
                    <button type="button" class="settings-ghost-btn" data-harness-login>${runtimeActionLabel(payload)}</button>
                </div>
            </div>
        `);
    }
    host.innerHTML = parts.join('')
        || '<div class="muted">Connect a coding-agent account to run delegated work on subscriptions.</div>';
    host.querySelectorAll('[data-harness-login]').forEach((button) => {
        button.addEventListener('click', () => {
            const row = button.closest('[data-harness]');
            startLogin(row?.dataset.harness, row?.dataset.profile);
        });
    });
    host.querySelectorAll('[data-harness-add-profile]').forEach((button) => {
        button.addEventListener('click', async () => {
            // Captured before the await: the status poll replaces the rows
            // while the dialog is open, detaching this button's row.
            const harness = button.closest('[data-harness]')?.dataset.harness;
            const profile = await promptProfileName();
            if (profile) startLogin(harness, profile);
        });
    });
    state.loginCard?.render();
}

function ensureLoginCard() {
    if (state.loginCard) return state.loginCard;
    state.loginCard = createLoginCardController({
        host: () => document.getElementById('harness-login-card'),
        store: state.store,
        // The Settings face is the FULL card: paste-code entry, engine detail,
        // the collapsed Advanced terminal fallback, Close.
        mode: 'full',
        onSettled: () => renderRows(),
    });
    return state.loginCard;
}

/**
 * Start (or restart) a login for one account row. Exported because the account
 * rows, the Add-account dialog and the browser smoke tests all drive it.
 */
export async function startLogin(harness, profile) {
    if (!harness) return;
    await ensureLoginCard().start(harness, profile);
}

/** Read the shared status once (the Refresh button, and the first paint). */
export function refreshHarnessStatus() {
    return state.store.refresh();
}

export function initHarnessAccounts({ store = claudexorStatus } = {}) {
    destroyHarnessAccounts();
    state.store = store;
    ensureLoginCard();
    document.getElementById('btn-harness-refresh')
        ?.addEventListener('click', () => state.store.refresh());
    // The store polls only while THIS surface is on screen: `.page` is
    // display:none when inactive, which is exactly what offsetParent reports.
    state.disposers.push(state.store.subscribe(() => renderRows(), {
        visible: () => document.getElementById('harness-accounts-rows')?.offsetParent != null,
    }));
    // Reaching the panel is not a visibility CHANGE the store can observe, so
    // the two activation events stay here — and are released with everything
    // else (DEVELOPMENT.md «UI resources carry a disposer»).
    const onPageShown = (event) => {
        if (event?.detail?.page === 'settings') state.store.refresh();
    };
    const onSubtabShown = (event) => {
        if (event?.detail?.tab === 'providers') state.store.refresh();
    };
    window.addEventListener('ouro:page-shown', onPageShown);
    window.addEventListener('ouro:settings-subtab-shown', onSubtabShown);
    state.disposers.push(() => window.removeEventListener('ouro:page-shown', onPageShown));
    state.disposers.push(() => window.removeEventListener('ouro:settings-subtab-shown', onSubtabShown));
    state.initialized = true;
    // The first read must not wait for the poll interval: init runs while the
    // page may not be visible yet, and the panel would sit on "Checking
    // daemon…" until the first tick (#125).
    state.store.refresh();
    renderRows();
}

export function destroyHarnessAccounts() {
    for (const dispose of state.disposers.splice(0)) {
        try { dispose(); } catch (err) { /* a broken disposer must not block the rest */ }
    }
    state.loginCard?.dispose();
    state.loginCard = null;
    state.initialized = false;
}
