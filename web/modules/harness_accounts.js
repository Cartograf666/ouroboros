// Harness Accounts (D30) — the Providers-tab section over the owned Claudexor
// daemon's account surface, MCP-card style: one row per account, two HONEST
// verification statuses, quota with a resets_at timer, login cards.
//
// «Красота-сначала»: a structural device-code card wherever the engine hosts
// the flow itself; the FALLBACK is a copy-paste `claudexor setup attach …`
// command the user runs in the USER'S OWN terminal outside this UI, with the
// card polling the job. No terminal panel exists in this UI, and none may be
// added. No harness-name branches either: the in-app flow is ASKED FOR
// (login_flow=device_auth) and a typed refusal flips the card to the
// terminal-command path — capability is discovered, never hardcoded.
//
// Pure helpers up top are node-tested without a DOM.

import { apiFetch } from './api_client.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';

const POLL_MS = 5000;
const JOB_POLL_MS = 3000;

// ---------------------------------------------------------------------------
// Pure helpers.
// ---------------------------------------------------------------------------

export function verificationBadge(profile) {
    // Q2-а (recommendation A, pending owner confirmation): both statuses are
    // shown honestly — vendor-verified is trusted, local-store presence is
    // labeled "not verified", because the local status has lied before.
    const status = profile?.status || profile || {};
    const source = String(status.verification_source || '');
    const verification = String(status.verification || '');
    const at = String(status.last_verified_at || '');
    if (source === 'vendor' && verification === 'passed') {
        return { tone: 'ok', label: `verified live${at ? ` ${at}` : ''}` };
    }
    if (verification === 'passed') {
        return { tone: 'warn', label: 'logged in locally — not verified' };
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
        return !subjectId || String(subject.subject_id || '') === String(subjectId);
    });
    let worst = null;
    for (const snap of rows) {
        for (const constraint of snap.constraints || []) {
            const used = Number(constraint.used_ratio);
            if (!Number.isFinite(used)) continue;
            if (!worst || used > worst.used) {
                worst = {
                    used,
                    resetsAt: String(constraint.resets_at || constraint.cooldown_until || ''),
                    exhausted: used >= 1.0 || Boolean(constraint.cooldown_until),
                };
            }
        }
    }
    if (!worst) return { label: '', exhausted: false, resetsAt: '' };
    const percent = Math.min(100, Math.round(worst.used * 100));
    return {
        exhausted: worst.exhausted,
        resetsAt: worst.resetsAt,
        label: worst.exhausted
            ? `window exhausted — resets ${worst.resetsAt || 'soon'}`
            : `${percent}% of window used`,
    };
}

export function deviceCodeDisclosure(job) {
    // The transient read-time projection (never journaled by the daemon):
    // {flow, verificationUrl, userCode} wherever the flow surfaces one.
    //
    // The FLOW is the discriminator, not field truthiness. Claudexor's
    // SetupDeviceCodeDisclosure (packages/schema/src/setup.ts) says the code is
    // EMPTY for the browser-callback (`chatgpt`) and `oauth_url` flows —
    // `oauth_url` being the sign-in link a TERMINAL-mode claude/cursor login
    // prints. Demanding a userCode meant those URL-only disclosures matched
    // nothing, so the engine published a link the card never showed.
    const seen = new Set();
    const stack = [job];
    while (stack.length) {
        const node = stack.pop();
        if (!node || typeof node !== 'object' || seen.has(node)) continue;
        seen.add(node);
        if (node.flow && node.verificationUrl) {
            return { url: String(node.verificationUrl), code: String(node.userCode || '') };
        }
        for (const value of Object.values(node)) {
            if (value && typeof value === 'object') stack.push(value);
        }
    }
    return null;
}

// Which face the login card shows, in priority order. STRUCTURED WINS: when
// the engine surfaces an OAuth device/link disclosure on ANY job — including a
// sealed client_pty job once the upstream extension discloses claude/cursor
// links structurally — the card renders it, and the copy-paste command is only
// the fallback for a job with nothing structured yet. Pure for node tests.
export function loginCardFace(active) {
    if (!active) return 'none';
    if (active.error) return 'error';
    if (deviceCodeDisclosure(active.job || {})) return 'device';
    if (active.mode === 'attach' && active.attachCommand) return 'attach';
    return 'progress';
}

export function jobStateSummary(job) {
    const state = String(job?.state || job?.status || '');
    const phase = String(job?.phase || '');
    const terminal = ['succeeded', 'failed', 'cancelled', 'timed_out',
        'interrupted_unknown', 'not_supported'].includes(state);
    return { state, phase, terminal, succeeded: state === 'succeeded' };
}

export function accountRows(payload) {
    // Consume the REAL ControlCredentialProfilesResponse shape (Claudexor
    // packages/schema/src/credential-profile.ts): `profiles` is an ARRAY of
    // wrapper objects `{profile, status, identity}` with snake_case fields, and
    // `harnessAccounts` is an ARRAY of per-harness authority rows — not the flat
    // camelCase maps an earlier draft invented. Fields are read exactly as the
    // Zod schema names them so the golden fixture and the wire agree.
    const rows = [];
    const profiles = payload?.profiles?.profiles || [];
    const harnessAccounts = payload?.profiles?.harnessAccounts || [];
    for (const native of Array.isArray(harnessAccounts) ? harnessAccounts : []) {
        rows.push({
            harness: String(native?.harness_id || ''),
            profile_id: '',
            kind: 'native',
            identity: native?.identity || {},
            // The native/CLI login is local-store evidence at best: presence is
            // detected, liveness is not (verification_source stays local_store).
            status: {
                verification: native?.native_login_detected ? 'passed' : '',
                verification_source: 'local_store',
            },
        });
    }
    for (const wrapper of Array.isArray(profiles) ? profiles : []) {
        const profile = wrapper?.profile || {};
        rows.push({
            harness: String(profile.harness_id || ''),
            profile_id: String(profile.profile_id || ''),
            display_name: String(profile.display_name || ''),
            kind: 'profile',
            identity: wrapper?.identity || {},
            status: wrapper?.status || {},
        });
    }
    return rows.filter((row) => row.harness);
}

// ---------------------------------------------------------------------------
// DOM section.
// ---------------------------------------------------------------------------

const state = {
    payload: null,
    activeJob: null, // { harness, jobId, mode: 'device'|'attach', job, attachCommand, error }
    pollTimer: 0,
    jobTimer: 0,
};

export function renderHarnessAccountsSection() {
    return `
        <div class="form-section" id="harness-accounts-section">
            <h3>Harness Accounts</h3>
            <div class="settings-section-copy">
                Coding-agent subscriptions (Codex CLI, Claude Code, Cursor) used by delegated
                subagents and reviewer slots. Accounts live in Ouroboros's own agent home —
                your personal logins are never read or imported. Login and verification are
                the agent daemon's own flows; Ouroboros only shows them.
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

function daemonStatusLine(payload) {
    const daemon = payload?.daemon || {};
    const status = String(daemon.state || 'unknown');
    if (status === 'running') {
        return { tone: 'ok', text: `Daemon running (engine ${daemon.engine_version || '?'}) · home ${payload.config_dir || ''}` };
    }
    if (status === 'not_provisioned') {
        return { tone: 'muted', text: 'No accounts connected yet. The first login starts Ouroboros’s own agent daemon.' };
    }
    if (status === 'stale') {
        return { tone: 'warn', text: 'Daemon home exists but the daemon is not answering; the next login restarts it.' };
    }
    if (status === 'foreign_daemon') {
        return { tone: 'warn', text: 'Another daemon answered on the stale port (not ours — left untouched). The next login restarts our own daemon on a fresh port.' };
    }
    if (daemon.ownership_problem) {
        return { tone: 'error', text: `This daemon home is not managed from here: ${daemon.ownership_problem}` };
    }
    return { tone: 'error', text: `Daemon ${status}${daemon.last_error ? `: ${daemon.last_error}` : ''}` };
}

function rowHtml(row, payload) {
    const badge = verificationBadge(row);
    const quota = quotaSummary(payload?.quota || [], row.harness, row.profile_id);
    const identity = [row.identity?.email, row.identity?.plan].filter(Boolean).join(' · ');
    const name = row.kind === 'native'
        ? `${row.harness} — default account`
        : `${row.harness} — ${row.profile_id}`;
    return `
        <div class="settings-custom-secret-row harness-account-row${quota.exhausted ? ' harness-exhausted' : ''}" data-harness="${escapeHtml(row.harness)}" data-profile="${escapeHtml(row.profile_id)}">
            <div class="harness-account-main">
                <span class="harness-chip" data-harness-chip="${escapeHtml(row.harness)}">${escapeHtml(row.harness)}</span>
                <strong>${escapeHtml(name)}</strong>
                ${identity ? `<span class="muted">${escapeHtml(identity)}</span>` : ''}
                <span class="settings-inline-status" data-tone="${badge.tone}">${escapeHtml(badge.label)}</span>
                ${quota.label ? `<span class="settings-inline-status" data-tone="${quota.exhausted ? 'warn' : 'muted'}" ${quota.exhausted && quota.resetsAt ? `data-resets-at="${escapeHtml(quota.resetsAt)}"` : ''}>${escapeHtml(quota.label)}</span>` : ''}
            </div>
            <div class="harness-account-actions">
                <button type="button" class="settings-ghost-btn" data-harness-login>${row.status?.verification ? 'Re-login' : 'Log in'}</button>
                <button type="button" class="settings-ghost-btn" data-harness-login-terminal title="Copy-paste command for your own terminal">Via your terminal</button>
                ${row.kind === 'native' ? '<button type="button" class="settings-ghost-btn" data-harness-add-profile title="Register one more account for this agent (D28 rotation uses every enabled account)">Add account…</button>' : ''}
            </div>
        </div>
    `;
}

// The coding-agent families a fresh install can connect BEFORE the daemon
// exists. Discovery needs a running daemon, and on first run there is none —
// so with nothing discovered the UI still offers a Connect affordance, and the
// first Connect is exactly what provisions the owned daemon (D30). Presentation
// only; the login flow itself stays harness-agnostic (asks device_auth, falls
// back to the terminal command).
export const BOOTSTRAP_HARNESSES = ['codex', 'claude', 'cursor'];

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

function renderRows() {
    const host = document.getElementById('harness-accounts-rows');
    const statusEl = document.getElementById('harness-daemon-status');
    if (!host || !statusEl) return;
    const payload = state.payload || {};
    const line = daemonStatusLine(payload);
    statusEl.textContent = line.text;
    statusEl.dataset.tone = line.tone;
    const rows = accountRows(payload);
    const bare = harnessesWithoutRows(payload, rows);
    const parts = rows.map((row) => rowHtml(row, payload));
    for (const harness of bare) {
        parts.push(`
            <div class="settings-custom-secret-row harness-account-row" data-harness="${escapeHtml(harness)}" data-profile="">
                <div class="harness-account-main">
                    <span class="harness-chip" data-harness-chip="${escapeHtml(harness)}">${escapeHtml(harness)}</span>
                    <strong>${escapeHtml(harness)}</strong>
                    <span class="settings-inline-status" data-tone="muted">no account connected</span>
                </div>
                <div class="harness-account-actions">
                    <button type="button" class="settings-ghost-btn" data-harness-login>Connect</button>
                    <button type="button" class="settings-ghost-btn" data-harness-login-terminal title="Copy-paste command for your own terminal">Via your terminal</button>
                </div>
            </div>
        `);
    }
    host.innerHTML = parts.join('')
        || '<div class="muted">Connect a coding-agent account to run delegated work on subscriptions.</div>';
    host.querySelectorAll('[data-harness-login]').forEach((button) => {
        button.addEventListener('click', () => {
            const row = button.closest('[data-harness]');
            startLogin(row?.dataset.harness, row?.dataset.profile, 'device');
        });
    });
    host.querySelectorAll('[data-harness-login-terminal]').forEach((button) => {
        button.addEventListener('click', () => {
            const row = button.closest('[data-harness]');
            startLogin(row?.dataset.harness, row?.dataset.profile, 'attach');
        });
    });
    host.querySelectorAll('[data-harness-add-profile]').forEach((button) => {
        button.addEventListener('click', () => {
            const row = button.closest('[data-harness]');
            const profile = (window.prompt('Name for the additional account (e.g. work, backup):') || '')
                .trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-');
            if (profile) startLogin(row?.dataset.harness, profile, 'device');
        });
    });
    renderLoginCard();
}

function renderLoginCard() {
    const host = document.getElementById('harness-login-card');
    if (!host) return;
    const active = state.activeJob;
    if (!active) { host.innerHTML = ''; return; }
    const summary = jobStateSummary(active.job || {});
    const disclosure = deviceCodeDisclosure(active.job || {});
    const face = loginCardFace(active);
    const bits = [`<div class="panel-card harness-login-card" data-login-card>`,
        `<h4>Connect ${escapeHtml(active.harness)}${active.profile ? ` (${escapeHtml(active.profile)})` : ''}</h4>`];
    if (face === 'error') {
        bits.push(`<div class="settings-inline-note" data-tone="error">${escapeHtml(active.error)}</div>`);
        if (active.mode === 'device') {
            bits.push(`<div class="settings-inline-note">The in-app code flow is not available for this agent yet — use
                “Via your terminal” below: it gives one command to paste into your own terminal, and this card
                tracks the login live.</div>`);
        }
    } else if (face === 'device') {
        // URL-only flows (`chatgpt`, `oauth_url`) carry no code: show the link
        // alone rather than numbered steps ending in an empty code box.
        bits.push(disclosure.code ? `
            <div class="harness-device-code">
                <p>1. Open <a href="${escapeHtml(disclosure.url)}" target="_blank" rel="noopener">${escapeHtml(disclosure.url)}</a></p>
                <p>2. Enter this one-time code:</p>
                <div class="harness-code" data-device-code>${escapeHtml(disclosure.code)}</div>
            </div>
        ` : `
            <div class="harness-device-code">
                <p>Open <a href="${escapeHtml(disclosure.url)}" target="_blank" rel="noopener">${escapeHtml(disclosure.url)}</a> to finish signing in.</p>
            </div>
        `);
    } else if (face === 'attach') {
        bits.push(`
            <p>Run this in <strong>your own terminal</strong> (outside Ouroboros), then finish the login there.
            This card follows the progress automatically:</p>
            <pre class="harness-attach-command" data-attach-command>${escapeHtml(active.attachCommand)}</pre>
            <button type="button" class="settings-ghost-btn" data-copy-attach>Copy command</button>
        `);
    } else {
        bits.push(`<div class="settings-inline-status" data-tone="muted">Login ${escapeHtml(summary.state || 'starting')}${summary.phase ? ` · ${escapeHtml(summary.phase)}` : ''}…</div>`);
    }
    if (summary.terminal) {
        bits.push(`<div class="settings-inline-status" data-tone="${summary.succeeded ? 'ok' : 'error'}">${summary.succeeded ? 'Connected.' : `Login ${escapeHtml(summary.state)}.`}</div>`);
    }
    bits.push('<button type="button" class="settings-ghost-btn" data-login-dismiss>Close</button>', '</div>');
    host.innerHTML = bits.join('');
    host.querySelector('[data-copy-attach]')?.addEventListener('click', () => {
        navigator.clipboard?.writeText(active.attachCommand || '');
    });
    host.querySelector('[data-login-dismiss]')?.addEventListener('click', async () => {
        if (active.jobId && !summary.terminal) {
            try { await apiFetch(`/api/claudexor/login/${encodeURIComponent(active.jobId)}`, { method: 'DELETE' }); } catch (err) { /* cancel is best-effort */ }
        }
        stopJobPolling();
        state.activeJob = null;
        renderLoginCard();
        refreshStatus();
    });
}

async function startLogin(harness, profile, mode) {
    if (!harness) return;
    const body = { harness };
    if (profile) body.profile_id = profile;
    if (mode === 'attach') body.transport = 'client_pty';
    else body.login_flow = 'device_auth';
    state.activeJob = { harness, profile, mode, job: null, jobId: '', attachCommand: '', error: '' };
    renderLoginCard();
    try {
        const resp = await apiFetch('/api/claudexor/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        state.activeJob.jobId = String(data.job_id || '');
        state.activeJob.job = data.job || {};
        state.activeJob.attachCommand = String(data.attach_command || '');
        startJobPolling();
    } catch (error) {
        state.activeJob.error = String(error.message || error);
    }
    renderLoginCard();
}

function startJobPolling() {
    stopJobPolling();
    state.jobTimer = setInterval(async () => {
        const active = state.activeJob;
        if (!active?.jobId) return stopJobPolling();
        try {
            const resp = await apiFetch(`/api/claudexor/login/${encodeURIComponent(active.jobId)}`, { cache: 'no-store' });
            const data = await resp.json().catch(() => ({}));
            if (resp.ok) {
                active.job = data.job || active.job;
                if (!active.attachCommand) active.attachCommand = String(data.attach_command || '');
            }
        } catch (err) { /* transient poll failure; the next tick retries */ }
        renderLoginCard();
        if (jobStateSummary(state.activeJob?.job || {}).terminal) {
            stopJobPolling();
            refreshStatus();
        }
    }, JOB_POLL_MS);
}

function stopJobPolling() {
    if (state.jobTimer) clearInterval(state.jobTimer);
    state.jobTimer = 0;
}

export async function refreshStatus() {
    // Poll only while the section is actually on screen. Each tick fans out to four
    // daemon round-trips, and the interval started at app load and never stopped —
    // so every page in the app paid for them (`.page` is display:none when inactive,
    // which is exactly what offsetParent reports).
    const host = document.getElementById('harness-accounts-rows');
    if (!host || host.offsetParent === null || document.hidden) return;
    try {
        const resp = await apiFetch('/api/claudexor/status', { cache: 'no-store' });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) state.payload = data;
    } catch (err) { /* transient; next poll retries */ }
    renderRows();
}

export function initHarnessAccounts() {
    document.getElementById('btn-harness-refresh')?.addEventListener('click', refreshStatus);
    refreshStatus();
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(refreshStatus, POLL_MS);
}
