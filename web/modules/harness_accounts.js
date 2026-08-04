// Harness Accounts (D30) — the Providers-tab section over the owned Claudexor
// daemon's account surface, MCP-card style: one row per account, two HONEST
// verification statuses, quota with a resets_at timer, login cards.
//
// «Красота-сначала»: the login card is LINK-FIRST — the engine disclosures the
// sign-in URL (and a one-time code for the codex device flow) through the job
// snapshot's transient overlay, and the card renders "Open sign-in link" +
// copy affordances + a live state line. A job that also accepts user input
// (the claude OAuth paste-code, disclosure flow `oauth_url_input`) shows an
// ALWAYS-VISIBLE code entry: claude's CLI only asks for the code when the
// browser cannot reach its localhost callback (e.g. another device), so the
// field cannot depend on a prompt this UI cannot observe — the input is
// OPTIONAL by design (the callback may complete on its own). The copy-paste
// `claudexor setup attach …` command survives ONLY as a collapsed Advanced
// affordance for engines that predate the disclosure modes or jobs whose link
// never arrives — never the primary UI. No terminal panel exists in this UI,
// and none may be added. Shape selection keys on the disclosure's typed FLOW
// (url-only `oauth_url`/`chatgpt` vs `oauth_url_input`; 3.3.7 final
// contract) — no harness-name branches, no boolean sidecar.
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
    for (const snap of rows) {
        for (const constraint of snap.constraints || []) {
            const used = Number(constraint.used_ratio);
            const spent = Boolean(constraint.cooldown_until) || (Number.isFinite(used) && used >= 1.0);
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
    if (!worst && !exhausted) return { label: '', exhausted: false, resetsAt: '' };
    const percent = worst ? Math.min(100, Math.round(worst.used * 100)) : 100;
    const resetsAt = exhausted ? (exhaustedResetsAt || worst?.resetsAt || '') : (worst?.resetsAt || '');
    return {
        exhausted,
        resetsAt,
        label: exhausted
            ? `window exhausted — resets ${resetsAt || 'soon'}`
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
            return {
                url: String(node.verificationUrl),
                code: String(node.userCode || ''),
                flow: String(node.flow),
            };
        }
        for (const value of Object.values(node)) {
            if (value && typeof value === 'object') stack.push(value);
        }
    }
    return null;
}

// Which face the login card shows, in priority order. STRUCTURED WINS: when
// the engine surfaces an OAuth device/link disclosure on ANY job the card
// renders it (link-first). There is deliberately NO attach face any more: the
// copy-paste command is a demoted, collapsed Advanced affordance under the
// waiting face (attachFallbackDue below), never a primary card body — the
// owner rejected terminal-first login outright. Pure for node tests.
export function loginCardFace(active) {
    if (!active) return 'none';
    if (active.error) return 'error';
    if (deviceCodeDisclosure(active.job || {})) return 'device';
    return 'progress';
}

// How long the card waits for the engine to disclose a sign-in link before
// offering the collapsed Advanced attach fallback.
export const ATTACH_FALLBACK_MS = 20000;

export function attachFallbackDue(active, nowMs) {
    // The demoted fallback: only for a job that HAS a copy-paste command (the
    // create answer issues one exactly for client_pty jobs — the POLL route's
    // per-job attach_command is deliberately never read), and only when the
    // primary path has nothing better: the engine predates the disclosure
    // modes (engineDegraded — the create answer said so, or the input route
    // 404'd), or no disclosure arrived within the window. A rendered
    // disclosure keeps the fallback hidden unless the engine is degraded:
    // link-first, always.
    if (!active || !active.attachCommand) return false;
    if (active.engineDegraded) return true;
    if (deviceCodeDisclosure(active.job || {})) return false;
    const started = Number(active.startedAtMs) || 0;
    return started > 0 && (Number(nowMs) - started) >= ATTACH_FALLBACK_MS;
}

export function loginInputSupport(job) {
    // Card shape 2 discriminator: can the user paste the browser's code back
    // into this job? The engine's typed disclosure FLOW is the structural
    // marker (3.3.7 final contract): `oauth_url_input` = sign-in link plus an
    // optional paste-code the daemon forwards (claude's manual-callback
    // path); `oauth_url`/`chatgpt` stay link-only. No boolean sidecar and no
    // harness-name fallback — the enum decides. The input is OPTIONAL by
    // design: claude's localhost callback may complete the login with no code
    // ever shown, so the field's presence never implies obligation.
    return deviceCodeDisclosure(job)?.flow === 'oauth_url_input';
}

// Job-failure reasons a stale post-login verification read can fabricate:
// codex clears its auth store when a login STARTS, so a probe in that window
// reads "not logged in" while the vendor login is succeeding — the owner's
// codex login SUCCEEDED while the card said "Login failed · completed". These
// verdicts are re-checked against live account status before the card is
// allowed to say "failed".
export const VERIFY_RACE_REASONS = ['capability_verification_failed', 'auth_not_ready'];

export function loginVerdict(job) {
    // State and verdict must never contradict: a non-terminal job has NO
    // verdict, a succeeded job is success, and a failed job is only a final
    // failure when its typed outcome reason is one no verification race can
    // fabricate. Everything else is 'recheck' — judged by live account
    // status, not by one stale read.
    const summary = jobStateSummary(job);
    if (!summary.terminal) return { kind: 'pending', reason: '' };
    if (summary.succeeded) return { kind: 'success', reason: '' };
    const outcome = job?.outcome || job?.job?.outcome || {};
    const reason = String(outcome.reason || '') || summary.state;
    if (summary.state === 'failed'
        && (!outcome.reason || VERIFY_RACE_REASONS.includes(String(outcome.reason)))) {
        return { kind: 'recheck', reason };
    }
    return { kind: 'failure', reason };
}

export function failureText(reason) {
    return `Sign-in failed — ${String(reason || 'unknown reason').replace(/_/g, ' ')}.`;
}

export function loginStatusLine(job) {
    // The live state line for a non-terminal job — plain words mapped from
    // the typed state/phase, never raw enum spellings glued together (the old
    // "Login failed · completed" contradiction is structurally impossible
    // now: a terminal job renders a verdict, never this line).
    const summary = jobStateSummary(job);
    if (summary.terminal) return '';
    if (summary.phase === 'verifying') return 'Checking the sign-in…';
    if (deviceCodeDisclosure(job)) return 'Waiting for you to finish signing in in the browser…';
    if (summary.phase === 'awaiting_user' || summary.state === 'waiting_for_input') {
        return 'Waiting for the sign-in link…';
    }
    return 'Starting the sign-in…';
}

export function accountLoginConfirmed(payload, harness, profileId = '') {
    // The account-status truth the verdict re-check reads: the row for THIS
    // harness+profile shows a login — vendor-verified or the daemon's own
    // local-store detection (accountRows already projects both as 'passed').
    const row = accountRows(payload).find((r) =>
        r.harness === String(harness || '')
        && String(r.profile_id || '') === String(profileId || ''));
    return String(row?.status?.verification || '') === 'passed';
}

export async function submitLoginInput(jobId, value, { fetchImpl = apiFetch } = {}) {
    // ONE code line to the engine via the gateway proxy. Typed non-2xx
    // answers keep their meaning: 404 is a capability gap (`degraded` — the
    // engine predates the route, or reaped the job) and 409 is a typed input
    // CONFLICT (`conflict` carries the engine's code:
    // setup_input_not_applicable = the callback already completed and no code
    // is needed; setup_input_already_submitted = a repeat the server, being
    // authoritative over our double-submit guard, refused). Neither is a raw
    // error — the card maps both to friendly copy.
    try {
        const resp = await fetchImpl(`/api/claudexor/login/${encodeURIComponent(jobId)}/input`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ value }),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) return { ok: true, degraded: false, conflict: '', error: '' };
        if (resp.status === 404) {
            return { ok: false, degraded: true, conflict: '', error: String(data.error || 'input not supported') };
        }
        if (resp.status === 409) {
            return { ok: false, degraded: false, conflict: String(data.code || 'conflict'), error: String(data.error || 'input conflict') };
        }
        return { ok: false, degraded: false, conflict: '', error: String(data.error || `HTTP ${resp.status}`) };
    } catch (error) {
        return { ok: false, degraded: false, conflict: '', error: String(error?.message || error) };
    }
}

export const CONFIRM_POLL_MS = 2500;
export const CONFIRM_ATTEMPTS = 4;

export async function confirmLoginLive(harness, profileId, {
    fetchImpl = apiFetch, attempts = CONFIRM_ATTEMPTS, delayMs = CONFIRM_POLL_MS,
    sleepImpl = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    isStale = () => false,
} = {}) {
    // The verify-race re-check: briefly re-poll live account status and let
    // IT decide, instead of declaring failure off the job's one stale
    // verification read. Bounded; answers with the last payload so the caller
    // can refresh the rows from the same reads.
    let payload = null;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
        if (attempt > 0) await sleepImpl(delayMs);
        if (isStale()) return { confirmed: false, stale: true, payload };
        try {
            const resp = await fetchImpl('/api/claudexor/status', { cache: 'no-store' });
            const data = await resp.json().catch(() => ({}));
            if (resp.ok) {
                payload = data;
                if (accountLoginConfirmed(data, harness, profileId)) {
                    return { confirmed: true, stale: false, payload };
                }
            }
        } catch (err) { /* transient; the next attempt retries */ }
    }
    return { confirmed: false, stale: false, payload };
}

export function jobStateSummary(job) {
    // The POLL returns ControlSetupJobSnapshot — the ENVELOPE {job, cursor,
    // sequence, deviceCode?} — while the CREATE returns a bare ControlSetupJob.
    // Reading only the top level found the state on the create response and
    // never on any poll, so `terminal` stayed false forever: no "Connected." /
    // "Login failed." banner, and the 3-second poll never stopped. `status` is
    // a field ControlSetupJob does not have and never had.
    const state = String(job?.state || job?.job?.state || '');
    const phase = String(job?.phase || job?.job?.phase || '');
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
            // Both engine schema versions declare this row
            // additionalProperties:false with NO status field, so presence
            // projects as local-store evidence — detected, liveness unproven.
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
    // { harness, profile, jobId, job, attachCommand, error, startedAtMs,
    //   engineDegraded, inputValue, inputBusy, inputSent, inputError,
    //   verdict, confirming, advancedOpen }
    activeJob: null,
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
        <div class="harness-account-row${quota.exhausted ? ' harness-exhausted' : ''}" data-harness="${escapeHtml(row.harness)}" data-profile="${escapeHtml(row.profile_id)}">
            <div class="harness-account-main">
                <span class="harness-chip" data-harness-chip="${escapeHtml(row.harness)}">${escapeHtml(row.harness)}</span>
                <strong>${escapeHtml(name)}</strong>
                ${identity ? `<span class="muted">${escapeHtml(identity)}</span>` : ''}
                <span class="settings-inline-status" data-tone="${badge.tone}">${escapeHtml(badge.label)}</span>
                ${quota.label ? `<span class="settings-inline-status" data-tone="${quota.exhausted ? 'warn' : 'muted'}" ${quota.exhausted && quota.resetsAt ? `data-resets-at="${escapeHtml(quota.resetsAt)}"` : ''}>${escapeHtml(quota.label)}</span>` : ''}
            </div>
            <div class="harness-account-actions">
                <button type="button" class="settings-ghost-btn" data-harness-login>${row.status?.verification ? 'Re-login' : 'Log in'}</button>
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
            <div class="harness-account-row" data-harness="${escapeHtml(harness)}" data-profile="">
                <div class="harness-account-main">
                    <span class="harness-chip" data-harness-chip="${escapeHtml(harness)}">${escapeHtml(harness)}</span>
                    <strong>${escapeHtml(harness)}</strong>
                    <span class="settings-inline-status" data-tone="muted">no account connected</span>
                </div>
                <div class="harness-account-actions">
                    <button type="button" class="settings-ghost-btn" data-harness-login>Connect</button>
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
        button.addEventListener('click', () => {
            const row = button.closest('[data-harness]');
            const profile = (window.prompt('Name for the additional account (e.g. work, backup):') || '')
                .trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-');
            if (profile) startLogin(row?.dataset.harness, profile);
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
    // NOT .panel-card: that class lives in onboarding.css, which the settings
    // pages never load — the card rendered with no frame at all (finding #4).
    const bits = [`<div class="harness-login-card" data-login-card>`,
        `<h4>Connect ${escapeHtml(active.harness)}${active.profile ? ` (${escapeHtml(active.profile)})` : ''}</h4>`];
    if (face === 'error') {
        bits.push(`<div class="settings-inline-note" data-tone="error">${escapeHtml(active.error)}</div>`);
    } else if (face === 'device') {
        // LINK-FIRST: the prominent action is opening the disclosed sign-in
        // URL, with an explicit copy affordance (the macOS-app card pattern).
        bits.push(`
            <div class="harness-signin-actions">
                <a class="btn btn-primary" data-open-signin href="${escapeHtml(disclosure.url)}" target="_blank" rel="noopener">Open sign-in link</a>
                <button type="button" class="settings-ghost-btn" data-copy-signin-link>Copy link</button>
            </div>
        `);
        if (disclosure.code) {
            // The codex device flow: big selectable one-time code, explicit
            // Copy (never auto-copied).
            bits.push(`
                <div class="harness-device-code">
                    <p>Enter this one-time code on that page:</p>
                    <div class="harness-code" data-device-code>${escapeHtml(disclosure.code)}</div>
                    <button type="button" class="settings-ghost-btn" data-copy-device-code>Copy code</button>
                </div>
            `);
        }
    }
    // The live state line and the verdict are mutually exclusive by
    // construction (loginStatusLine answers '' on a terminal job), so the
    // card can never say two contradicting things at once.
    if (face !== 'error') {
        const line = loginStatusLine(active.job || {});
        if (line) bits.push(`<div class="settings-inline-status" data-tone="muted" data-login-state>${escapeHtml(line)}</div>`);
    }
    // Shape 2: the ALWAYS-VISIBLE paste-code entry for a job whose disclosure
    // flow is `oauth_url_input`. Claude's CLI prompts for the code only when
    // the browser cannot reach its localhost callback (e.g. the browser is on
    // another device), and this UI cannot observe that prompt — so the field
    // is unconditional while the job awaits the user, with the hint carrying
    // both the "if" and the optionality (no code is needed when the callback
    // completes on its own).
    if (face === 'device' && loginInputSupport(active.job || {}) && !summary.terminal) {
        const busy = active.inputBusy || active.inputSent;
        bits.push(`
            <div class="harness-code-entry" data-code-entry>
                <label for="harness-code-input">If the browser shows a code instead of finishing, paste it here (otherwise the sign-in completes on its own):</label>
                <div class="harness-code-entry-row">
                    <input type="text" id="harness-code-input" data-login-code-input autocomplete="off" spellcheck="false"
                        placeholder="sign-in code" value="${escapeHtml(active.inputValue || '')}"${active.inputSent ? ' disabled' : ''}>
                    <button type="button" class="settings-ghost-btn" data-login-code-submit${busy ? ' disabled' : ''}>${active.inputBusy ? 'Sending…' : (active.inputSent ? 'Code sent' : 'Submit code')}</button>
                </div>
                ${active.inputError ? `<div class="settings-inline-note" data-tone="error">${escapeHtml(active.inputError)}</div>` : ''}
                ${active.inputSent ? `<div class="settings-inline-status" data-tone="muted">${escapeHtml(active.inputNote || 'Code sent — finishing the sign-in…')}</div>` : ''}
            </div>
        `);
    }
    // The verdict: success, a REAL failure with its typed reason, or the
    // in-between "confirming" state while live account status is re-checked.
    if (active.confirming) {
        bits.push('<div class="settings-inline-status" data-tone="muted" data-login-verdict>Confirming the sign-in…</div>');
    } else if (active.verdict?.kind === 'success') {
        bits.push('<div class="settings-inline-status" data-tone="ok" data-login-verdict>Connected.</div>');
    } else if (active.verdict?.kind === 'failure') {
        bits.push(`<div class="settings-inline-status" data-tone="error" data-login-verdict>${escapeHtml(failureText(active.verdict.reason))}</div>`);
    }
    // The demoted attach fallback: a collapsed Advanced affordance, only when
    // due (engine predates the disclosure modes, or no link within the
    // window) and only while the job can still be attached.
    if (!summary.terminal && attachFallbackDue(active, Date.now())) {
        bits.push(`
            <details class="harness-advanced" data-login-advanced${active.advancedOpen ? ' open' : ''}>
                <summary>Advanced: sign in from your own terminal</summary>
                <p>${active.engineDegraded
        ? 'This engine cannot host the sign-in in the app yet. Run this in your own terminal (outside Ouroboros); the card follows the progress automatically:'
        : 'No sign-in link arrived yet. You can run this in your own terminal (outside Ouroboros) instead; the card follows the progress automatically:'}</p>
                <pre class="harness-attach-command" data-attach-command>${escapeHtml(active.attachCommand)}</pre>
                <button type="button" class="settings-ghost-btn" data-copy-attach>Copy command</button>
            </details>
        `);
    }
    bits.push('<button type="button" class="settings-ghost-btn" data-login-dismiss>Close</button>', '</div>');
    host.innerHTML = bits.join('');
    wireLoginCard(host, active, summary);
}

function wireLoginCard(host, active, summary) {
    host.querySelector('[data-copy-signin-link]')?.addEventListener('click', () => {
        const disclosure = deviceCodeDisclosure(active.job || {});
        if (disclosure?.url) navigator.clipboard?.writeText(disclosure.url);
    });
    host.querySelector('[data-copy-device-code]')?.addEventListener('click', () => {
        const disclosure = deviceCodeDisclosure(active.job || {});
        if (disclosure?.code) navigator.clipboard?.writeText(disclosure.code);
    });
    host.querySelector('[data-copy-attach]')?.addEventListener('click', () => {
        navigator.clipboard?.writeText(active.attachCommand || '');
    });
    const advanced = host.querySelector('[data-login-advanced]');
    // Re-renders (every poll tick) must not slam the user's Advanced section
    // shut: the open state is carried on the job and re-applied.
    advanced?.addEventListener('toggle', () => { active.advancedOpen = advanced.open; });
    const codeInput = host.querySelector('[data-login-code-input]');
    codeInput?.addEventListener('input', () => { active.inputValue = codeInput.value; });
    codeInput?.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); submitCodeFromCard(active); }
    });
    host.querySelector('[data-login-code-submit]')?.addEventListener('click', () => submitCodeFromCard(active));
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

async function submitCodeFromCard(active) {
    // Double-submit guard: one POST in flight at a time, and an accepted code
    // is final — the button stays disabled after success.
    if (!active || active.inputBusy || active.inputSent) return;
    const value = String(active.inputValue || '').trim();
    if (!value) return;
    // The job may have finished while the user typed: render the verdict
    // instead of posting into a dead job.
    if (jobStateSummary(active.job || {}).terminal) { renderLoginCard(); return; }
    active.inputBusy = true;
    active.inputError = '';
    renderLoginCard();
    const result = await submitLoginInput(active.jobId, value);
    if (state.activeJob !== active) return;   // card closed while in flight
    active.inputBusy = false;
    if (result.ok) {
        active.inputSent = true;
    } else if (result.conflict === 'setup_input_not_applicable') {
        // Typed 409: the callback already completed (or the flow moved past
        // the code step) — no code is needed. An answer, not an error: quiet
        // note, and the job poll lands the verdict.
        active.inputSent = true;
        active.inputNote = 'No code needed — the sign-in is already completing.';
    } else if (result.conflict === 'setup_input_already_submitted') {
        // Typed 409: the server already has a code (it is authoritative over
        // our double-submit guard, e.g. a second browser tab).
        active.inputSent = true;
        active.inputNote = 'Code already received — finishing the sign-in.';
    } else if (result.degraded) {
        // The engine predates the input route, or reaped the job. Degrade to
        // the Advanced fallback when one exists; otherwise say what is known.
        if (!jobStateSummary(active.job || {}).terminal && active.attachCommand) {
            active.engineDegraded = true;
            active.inputError = 'This engine cannot accept the code here — see Advanced below.';
        } else {
            active.inputError = 'The engine did not accept the code (the sign-in may have already finished).';
        }
    } else {
        active.inputError = result.error || 'Code submission failed; try again.';
    }
    renderLoginCard();
}

async function startLogin(harness, profile) {
    if (!harness) return;
    // One start shape for every harness: the in-app flow is asked for and the
    // server/engine decide the hosting (loginFlow rides only for codex).
    const body = { harness, login_flow: 'device_auth' };
    if (profile) body.profile_id = profile;
    state.activeJob = {
        harness, profile, jobId: '', job: null, attachCommand: '', error: '',
        startedAtMs: Date.now(), engineDegraded: false,
        inputValue: '', inputBusy: false, inputSent: false, inputError: '', inputNote: '',
        verdict: null, confirming: false, advancedOpen: false,
    };
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
        // An engine that predates the disclosure modes never sends a link;
        // the demoted Advanced fallback may show right away (still collapsed).
        state.activeJob.engineDegraded = data.disclosure_native === false
            && !!state.activeJob.attachCommand;
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
            // The CREATE answer is the only authority on whether this job has a
            // copy-paste command: it issues one exactly for a client_pty job,
            // while the poll route hands one back for every job including the
            // daemon-transport codex device flow, which must never show it.
            if (resp.ok) active.job = data.job || active.job;
        } catch (err) { /* transient poll failure; the next tick retries */ }
        const verdict = loginVerdict(state.activeJob?.job || {});
        if (verdict.kind !== 'pending') {
            stopJobPolling();
            settleVerdict(state.activeJob, verdict);
            return;
        }
        renderLoginCard();
    }, JOB_POLL_MS);
}

async function settleVerdict(active, verdict) {
    if (!active) return;
    if (verdict.kind !== 'recheck') {
        active.verdict = verdict;
        renderLoginCard();
        refreshStatus();
        return;
    }
    // The verify-race: never declare failure off one stale verification read
    // (the owner's codex login SUCCEEDED while the card said "Login failed ·
    // completed"). Live account status — the truth the rows themselves render
    // — briefly re-polled, is the judge.
    active.confirming = true;
    renderLoginCard();
    const check = await confirmLoginLive(active.harness, active.profile || '', {
        isStale: () => state.activeJob !== active,
    });
    if (state.activeJob !== active) return;
    active.confirming = false;
    if (check.payload) state.payload = check.payload;
    active.verdict = check.confirmed
        ? { kind: 'success', reason: '' }
        : { kind: 'failure', reason: verdict.reason };
    renderRows();
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
