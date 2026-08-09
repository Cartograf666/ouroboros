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
import { openConfirmDialog } from './confirm_dialog.js';
import { escapeHtmlAttr as escapeHtml, safeExternalHrefAttr } from './utils.js';

const POLL_MS = 5000;
const JOB_POLL_MS = 3000;
export const JOB_POLL_MAX_DELAY_MS = 30000;
export const JOB_POLL_GIVE_UP_FAILURES = 10;

// ---------------------------------------------------------------------------
// Pure helpers.
// ---------------------------------------------------------------------------

// #125: the status refresh is visibility-gated (each tick fans out to four
// daemon round-trips), but a FORCED refresh — first load, the Refresh button,
// the poll give-up — always runs. Pure so the gate is node-tested.
export function shouldSkipStatusRefresh({ hostVisible = false, hidden = false, force = false } = {}) {
    if (force) return false;
    return !hostVisible || Boolean(hidden);
}

// #125: job-poll pacing over CONSECUTIVE failures (a successful read — any
// parsed snapshot, pending included — resets the counter): 3s while healthy,
// exponential backoff 6/12/24/30…s capped at 30s while the daemon is not
// answering, and after 10 consecutive failures the chain gives up (~4 min)
// instead of hammering a dead daemon forever.
export function nextJobPollDelay(consecutiveFailures) {
    const failures = Math.max(0, Number(consecutiveFailures) || 0);
    if (failures >= JOB_POLL_GIVE_UP_FAILURES) return { delayMs: 0, giveUp: true };
    return {
        delayMs: Math.min(JOB_POLL_MS * 2 ** failures, JOB_POLL_MAX_DELAY_MS),
        giveUp: false,
    };
}

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

// A bounded re-check that RAN OUT is not a failure. The verdict it re-checks
// was already judged unproven (a verification race), and the account row it
// waits for routinely lands a tick after the window closes — so the honest
// answer is "not confirmed yet", pointing at the two places the truth appears,
// never a hard "Sign-in failed" for a login that may well have succeeded.
export const UNCONFIRMED_TEXT =
    'Could not confirm the sign-in yet — check the account row above, or press Refresh.';

export function jobDetail(job) {
    // The engine's own SENTENCE about a settled job, beside the typed reason:
    // `message` on the job (dual-level, because the POLL route answers the
    // envelope while CREATE answers the bare job — the same shape every other
    // field here is read through). The card used to drop it entirely: a codex
    // login that ended `auth_not_ready` rendered only the fixed
    // UNCONFIRMED_TEXT, while the daemon had already said exactly what was
    // wrong ("native Codex session is not logged in") and that text was
    // sitting in the snapshot the card was holding. A typed reason names a
    // CATEGORY; this names the thing that happened, so it is the half the
    // owner needs and the half that reached no reader.
    for (const value of [job?.message, job?.job?.message]) {
        if (typeof value === 'string' && value.trim()) return value.trim();
    }
    return '';
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

export function pollResponseApplies(captured, current) {
    // Whether a poll answer may still be written onto the card. It belongs to
    // the job it was captured for (`captured === current`: the card may have
    // been closed, or reopened for another account, while the request was in
    // flight) and only while that job is UNSETTLED — a verdict, or the live
    // re-check that decides one, has already taken the card, and a snapshot
    // captured before the job settled would put a pending state back under it.
    // Pure so the ordering rule is asserted without a DOM or a timer.
    return Boolean(captured) && captured === current
        && !captured.verdict && !captured.confirming;
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

// ---------------------------------------------------------------------------
// DOM section.
// ---------------------------------------------------------------------------

const state = {
    payload: null,
    // A status read is in flight. Only reads BEFORE the first settle repaint on
    // it (see refreshStatus): once anything has been said, a silent refresh
    // beats churn — and beats flickering muted↔error against a failing endpoint.
    statusChecking: false,
    statusEverSettled: false,
    // The live read, shared by every caller (single-flight; see refreshStatus).
    statusInFlight: null,
    // A request that did not complete (network death, non-OK). Kept OUT of the
    // wire payload: staleness is a property of THIS view, not of the daemon's
    // answer. While set, the panel renders its last-known reading as exactly
    // that instead of presenting it as current truth.
    statusTransportError: '',
    // The owner asked to wake the daemon (see wakeDaemon). Single-flighted like
    // the status read: a cold runtime install takes real time and a second
    // click must not start a second provisioning.
    wakeInFlight: null,
    wakeError: '',
    wakeBusy: false,
    // Status reads and the wake share ONE generation counter: a GET issued
    // before a wake must never commit its (older) payload afterwards, or the
    // panel would flip back to "not running" right after starting the daemon.
    readGeneration: 0,
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

// The ONE place that decides what an empty collection MEANS. Every ES-module
// consumer of /api/claudexor/status imports this instead of re-deriving the
// rule; the onboarding wizard is a plain IIFE and cannot import, so it mirrors
// the rule under a structural pin (tests/test_web_utils_ssot.py) — the
// wizard proved a payload field alone is not enough: it already read
// daemon.state and still printed "No accounts connected yet" (owner report,
// 2026-08-08, three harnesses declared empty while two claude profiles, a
// cursor profile and two native sessions sat in the agent home).
//
// `facet` is 'catalog' | 'accounts' | 'quota' — they are INDEPENDENT: one
// fanned-out read can fail while its siblings land, so reviewer slots (which
// reads the CATALOG) must not be gated on the accounts facet.
export function facetReadState(payload, facet, { transportError = '' } = {}) {
    // A request that never arrived outranks whatever the last payload said.
    if (transportError) return 'transport';
    // PRESENT-but-unusable is not ABSENT. `reads: null`, a string, a number —
    // anything a drifting backend might emit — used to fall through to the
    // legacy branch and become `ok` on a running daemon: unknown, authoritative
    // again. Only a payload with no `reads` property at all is legacy.
    const hasBlock = payload !== null && typeof payload === 'object'
        && Object.prototype.hasOwnProperty.call(payload, 'reads');
    if (hasBlock) {
        const reads = payload.reads;
        if (!reads || typeof reads !== 'object') return 'failed';
        const state = String(reads[facet] || '');
        if (state === 'ok' || state === 'not_read' || state === 'failed') return state;
        // The block EXISTS but this facet is missing or carries a value this
        // build does not know (a newer backend). Falling back to the daemon
        // state here would make an unknown authoritative again — the exact bug.
        return 'failed';
    }
    // LEGACY payload — the block is entirely ABSENT (older backend, or a fixture
    // predating it). There the daemon state was the only signal and it carried
    // exactly this fact: running meant every facet was read, anything else none.
    return String(payload?.daemon?.state || '') === 'running' ? 'ok' : 'not_read';
}

// Sugar for the common question. NOT the only reader — see facetReadState.
export function accountsKnown(payload, options) {
    return facetReadState(payload, 'accounts', options) === 'ok';
}

// Why the truth is unavailable, in the words the surfaces render. Distinguishes
// "nobody asked" from "asked and refused": telling an owner the daemon is not
// running when it IS running and one endpoint died would be a second lie.
export function unknownAccountsNote(payload, { transportError = '', checking = false } = {}) {
    const state = facetReadState(payload, 'accounts', { transportError });
    if (state === 'ok') return '';
    // Nothing has answered YET. The first read fans out to four daemon
    // round-trips that probe real CLIs, so this window lasts tens of seconds —
    // long enough for "the agent daemon is not running" to be read as a verdict
    // about a daemon that is, in fact, running.
    if (checking && payload === null) return 'accounts not checked yet — reading the daemon status';
    if (state === 'transport') return 'accounts not checked — the last status request did not complete';
    if (state === 'failed') return 'accounts not checked — the daemon did not answer this read';
    // `not_read` normally means the daemon never ran — but a discovery/handshake
    // failure BEFORE the fan-out also leaves the facets untouched while the
    // daemon reports `unreachable`. Saying "not running" there would contradict
    // the status line one row above it.
    return String(payload?.daemon?.state || '') === 'unreachable'
        ? 'accounts not checked — the daemon did not answer'
        : 'accounts not checked — the agent daemon is not running';
}

// The Refresh button's honest label. It only ever RE-READS while the daemon is
// alive, but with a sleeping daemon a plain re-read returns the same nothing
// forever — so there it becomes an explicit owner action that STARTS the daemon,
// and the label says so rather than hiding the side effect.
export function refreshActionKind(payload) {
    // ONE predicate behind BOTH the label and the click. They were written
    // separately and drifted: the label promised a plain re-read on an
    // `unreachable` daemon while the handler called wake, so the button did
    // more than it said — and a comment claimed it "does not resurrect
    // anything" while the code resurrected. Live, the button re-reads. In every
    // other state — asleep, never installed, or answering nothing — pressing it
    // starts/repairs the daemon, which is what the owner is asking for.
    return daemonAnswered(payload) ? 'refresh' : 'wake';
}

export function refreshActionLabel(payload) {
    return refreshActionKind(payload) === 'refresh'
        ? 'Refresh'
        : 'Check accounts (starts the agent daemon)';
}

export function runtimeActionLabel(payload) {
    const state = String(payload?.daemon?.runtime?.state || '');
    if (state === 'error') return 'Fix & connect';
    if (state === 'missing') return 'Install & connect';
    if (state === 'update_available') return 'Update & connect';
    return 'Connect';
}

export function daemonStatusLine(payload, { checking = false, transportError = '', wakeError = '' } = {}) {
    const daemon = payload?.daemon || {};
    const runtime = daemon.runtime || {};
    const runtimeState = String(runtime.state || '');
    const status = String(daemon.state || 'unknown');
    // A request that did not complete: whatever is on screen is a LAST-KNOWN
    // reading, and saying so is the whole point — a swallowed failure used to
    // leave "Claudexor ready" and green badges standing indefinitely.
    if (wakeError) {
        // The owner PRESSED the button and it did not work — silence here is the
        // same class of dishonesty this change exists to remove (a typed 503 from
        // a missing binary or foreign home, a 404 from an older backend, a dead
        // network). Say what failed; the rows and Connect stay put.
        return { tone: 'error', text: `Could not start the agent daemon: ${wakeError}` };
    }
    if (transportError) {
        // With a previous reading on screen, say it is the LAST one rather than
        // the current one. With none — the very first request died, page load
        // against a restarting backend — there is no reading to qualify, and
        // the fallback at the bottom would announce a verdict about the daemon
        // built from zero data ("Daemon unknown", error tone) while the row
        // beside it correctly says nothing was checked.
        return daemon.state
            ? { tone: 'warn', text: `Showing the last reading — the status request did not complete (${transportError}). Retrying.` }
            : { tone: 'warn', text: `The status request did not complete (${transportError}). Retrying — nothing has been read yet.` };
    }
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
    // A facet can fail WITHOUT the aggregate hearing about it: an envelope that
    // arrived in the wrong shape is a failed read, not an exception, so the
    // daemon still reports `running`. The panel then said "Claudexor ready" in
    // green while the row underneath said "accounts not checked" — the two
    // halves of one screen contradicting each other, with the reassuring half
    // on top. The status line asks the facets, not the aggregate.
    //
    // Computed BEFORE the runtime branches, which used to return above it and
    // hide the unread facets entirely — and `update_staged` did worse than hide
    // them: it says the engine "keeps running", a positive claim about a daemon
    // that in this window answered nothing, over a button offering to START it.
    const unread = unreadFacets(payload);
    const unreadTail = unread.length
        ? ` ${unread.join(' and ')} ${unread.length > 1 ? 'were' : 'was'} not read.`
        : '';
    if (runtimeState === 'installing') {
        const version = runtime.target_version ? ` ${runtime.target_version}` : '';
        return { tone: 'muted', text: `Installing or checking Claudexor${version}…${unreadTail}` };
    }
    if (runtimeState === 'error') {
        const detail = runtime.last_error ? `: ${runtime.last_error}.` : '.';
        return { tone: 'error', text: `Claudexor needs repair${detail} Connect retries automatically.${unreadTail}` };
    }
    // The staged-update line asserts the current engine is still serving. Only
    // say that when this reading actually saw it serve; otherwise the facets own
    // the line and the staged update is a footnote.
    if (runtimeState === 'update_staged' && !unread.length) {
        const target = runtime.staged_version || runtime.target_version || '?';
        const current = daemon.engine_version || runtime.version || '?';
        return { tone: 'warn', text: `Claudexor ${target} is ready and will activate after the daemon next restarts. Engine ${current} keeps running until then.` };
    }
    if (runtimeState === 'update_staged') {
        const target = runtime.staged_version || runtime.target_version || '?';
        return { tone: 'warn', text: `${unread.join(' and ')} ${unread.length > 1 ? 'were' : 'was'} not read${daemon.last_error ? `: ${daemon.last_error}` : ''}. Claudexor ${target} is staged and activates after the daemon next restarts.` };
    }
    if (status === 'running' && !unread.length) {
        return { tone: 'ok', text: `Claudexor ready (engine ${daemon.engine_version || '?'}) · home ${payload.config_dir || ''}` };
    }
    if (status === 'running') {
        return { tone: 'warn', text: `Claudexor is running, but ${unread.join(' and ')} ${unread.length > 1 ? 'were' : 'was'} not read${daemon.last_error ? `: ${daemon.last_error}` : ''}. What those cover is unknown.` };
    }
    if (status === 'not_provisioned') {
        // Neither branch may claim there are no accounts: with no daemon the
        // account store was never read, so `not_provisioned + accounts ok` is
        // unreachable by contract. Say what IS known — the runtime state — and
        // what the next step is.
        if (runtimeState === 'ready') {
            const version = runtime.version ? ` ${runtime.version}` : '';
            return { tone: 'muted', text: `Claudexor${version} is ready. Connect starts Ouroboros’s own agent daemon and reads your accounts.` };
        }
        return { tone: 'muted', text: 'Connect installs Claudexor and starts Ouroboros’s own agent daemon; your accounts are read once it runs.' };
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
    if (daemonAnswered(payload) && unread.length) {
        // A PARTIAL refusal: the aggregate says `unreachable` because one read
        // died, but the others landed and their rows are on screen right now.
        // Announcing a dead daemon above accounts it just handed over is the
        // same false verdict as an unread store rendered empty. NAME the facets
        // that did not answer — "what is shown below was read" was itself an
        // overclaim when the accounts facet is the one that failed.
        return { tone: 'warn', text: `${unread.join(' and ')} ${unread.length > 1 ? 'were' : 'was'} not read${daemon.last_error ? `: ${daemon.last_error}` : ''}. What those cover is unknown.` };
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
                <button type="button" class="settings-ghost-btn" data-harness-login>${runtimeActionLabel(payload)}</button>
                ${row.kind === 'native' ? '<button type="button" class="settings-ghost-btn" data-harness-add-profile title="Register one more account for this agent (rotation uses every enabled account)">Add account…</button>' : ''}
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
    const line = daemonStatusLine(payload, {
        checking: state.statusChecking && !state.statusEverSettled,
        transportError: state.statusTransportError,
        wakeError: state.wakeError,
    });
    statusEl.textContent = line.text;
    statusEl.dataset.tone = line.tone;
    const rows = accountRows(payload);
    const bare = harnessesWithoutRows(payload, rows);
    // The bootstrap rows and their Connect buttons STAY whatever we know —
    // onboarding must stay reachable — but the verdict beside them may only
    // assert absence when the account store was actually read.
    // RAW state, not the `{}` normalized above: "nothing has answered yet" is
    // exactly the absence of a payload, and handing the placeholder over made
    // the branch that says so unreachable — so the bootstrap rows spent the
    // whole first read printing "the agent daemon is not running" about a
    // daemon that was answering, which is the verdict this file exists to stop.
    const unknownNote = unknownAccountsNote(state.payload, {
        transportError: state.statusTransportError, checking: state.statusChecking });
    const bareStatus = unknownNote || 'no account connected';
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
    const refreshEl = document.getElementById('btn-harness-refresh');
    if (refreshEl) {
        refreshEl.textContent = state.wakeBusy ? 'Starting the agent daemon…' : refreshActionLabel(payload);
        refreshEl.disabled = Boolean(state.wakeBusy);
    }
    host.querySelectorAll('[data-harness-login]').forEach((button) => {
        button.addEventListener('click', () => {
            const row = button.closest('[data-harness]');
            startLogin(row?.dataset.harness, row?.dataset.profile);
        });
    });
    host.querySelectorAll('[data-harness-add-profile]').forEach((button) => {
        button.addEventListener('click', async () => {
            // Captured before the await: the 5-second status poll replaces the
            // rows while the dialog is open, detaching this button's row.
            const harness = button.closest('[data-harness]')?.dataset.harness;
            const profile = await promptProfileName();
            if (profile) startLogin(harness, profile);
        });
    });
    renderLoginCard();
}

export function loginCardHtml(active, nowMs = Date.now()) {
    // The card's whole body as a STRING — pure, so every face is asserted in
    // node without a DOM: the unsafe-URL refusal, the unconfirmed re-check,
    // and the rule that a verdict silences the live status line.
    if (!active) return '';
    const summary = jobStateSummary(active.job || {});
    const disclosure = deviceCodeDisclosure(active.job || {});
    const face = loginCardFace(active);
    // NOT .panel-card: that class lives in onboarding.css, which the settings
    // pages never load — the card rendered with no frame at all (finding #4).
    const bits = [`<div class="harness-login-card" data-login-card>`,
        `<h4>Connect ${escapeHtml(active.harness)}${active.profile ? ` (${escapeHtml(active.profile)})` : ''}</h4>`];
    if (face === 'error') {
        bits.push(`<div class="settings-inline-note" data-tone="error">${escapeHtml(active.error)}</div>`);
        bits.push('<button type="button" class="btn btn-primary" data-login-retry>Try again</button>');
    } else if (face === 'device') {
        // LINK-FIRST: the prominent action is opening the disclosed sign-in
        // URL, with an explicit copy affordance (the macOS-app card pattern).
        // That link is now a PRIMARY click target built from engine-supplied
        // text, so it goes through the house helper: http/https only, escaped
        // by the helper itself. Anything else — javascript:, data:, a
        // malformed string — yields '' and renders NO clickable link at all.
        const href = safeExternalHrefAttr(disclosure.url);
        bits.push(href ? `
            <div class="harness-signin-actions">
                <a class="btn btn-primary" data-open-signin href="${href}" target="_blank" rel="noopener">Open sign-in link</a>
                <button type="button" class="settings-ghost-btn" data-copy-signin-link>Copy link</button>
            </div>
        ` : `
            <div class="settings-inline-note" data-tone="error" data-unsafe-signin-link>
                The engine disclosed a sign-in link this app will not open (only http and https links are opened).
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
    // card can never say two contradicting things at once. The verdict slot
    // ALSO silences it explicitly: once a verdict (or its re-check) owns the
    // card, a snapshot that still reads pending — a poll response captured
    // before the job settled — must not print "Waiting for the sign-in link…"
    // underneath "Connected.".
    if (face !== 'error' && !active.verdict && !active.confirming) {
        const line = active.preparingRuntime
            ? 'Installing or checking Claudexor…'
            : loginStatusLine(active.job || {});
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
    } else if (active.verdict?.kind === 'unconfirmed') {
        // The re-check window closed without the row appearing. Unknown, not
        // failed — worded so the owner looks where the answer actually lands.
        bits.push(`<div class="settings-inline-status" data-tone="warn" data-login-verdict>${escapeHtml(UNCONFIRMED_TEXT)}</div>`);
    }
    // …and beside that verdict, the engine's own explanation. The two verdict
    // texts above are FIXED constants (deliberately: one is a category, the
    // other an honest "unknown"), so without this line a settled login says
    // nothing about WHY — the daemon's sentence lived in the snapshot and was
    // rendered nowhere. Only for a settled non-success verdict: beside
    // "Connected." a stale message would contradict the outcome, and while the
    // job is pending the live status line already owns the card.
    if (active.verdict?.kind === 'failure' || active.verdict?.kind === 'unconfirmed') {
        // #125: a verdict minted client-side (the poll give-up) carries its own
        // sentence; a daemon-settled verdict keeps the engine's own message.
        const detail = String(active.verdict?.detail || '').trim() || jobDetail(active.job || {});
        if (detail) {
            bits.push(`<div class="settings-inline-note" data-login-detail>${escapeHtml(detail)}</div>`);
        }
    }
    // The demoted attach fallback: a collapsed Advanced affordance, only when
    // due (engine predates the disclosure modes, or no link within the
    // window) and only while the job can still be attached.
    if (!summary.terminal && attachFallbackDue(active, nowMs)) {
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
    return bits.join('');
}

export function preserveCardFocus(host, swap, doc = typeof document === 'undefined' ? null : document) {
    // The card re-renders on EVERY 3-second poll tick by replacing its whole
    // DOM, and the replacement dropped the caret out of the paste-code field
    // mid-typing — a sign-in code cannot be typed into a field that dies every
    // three seconds. The focused entry and its selection are captured across
    // the swap and restored onto the element that replaced it.
    const prior = doc ? doc.activeElement : null;
    const keep = Boolean(prior && host.contains?.(prior)
        && prior.hasAttribute?.('data-login-code-input'));
    const start = keep ? prior.selectionStart : null;
    const end = keep ? prior.selectionEnd : null;
    swap();
    if (!keep) return;
    const next = host.querySelector?.('[data-login-code-input]');
    if (!next || next.disabled) return;
    next.focus?.();
    if (typeof next.setSelectionRange === 'function' && start !== null) {
        next.setSelectionRange(start, end === null ? start : end);
    }
}

function renderLoginCard() {
    const host = document.getElementById('harness-login-card');
    if (!host) return;
    const active = state.activeJob;
    if (!active) { host.innerHTML = ''; return; }
    preserveCardFocus(host, () => { host.innerHTML = loginCardHtml(active, Date.now()); });
    wireLoginCard(host, active, jobStateSummary(active.job || {}));
}

function wireLoginCard(host, active, summary) {
    host.querySelector('[data-login-retry]')?.addEventListener('click', () => {
        // NO preemptive stopJobPolling() here: if the C7 guard inside
        // startLogin refuses (cancel unproven, job still live), the old poll
        // must stay armed — it is the recovery path that clears the transient
        // note and settles the real verdict. _startLoginLocked stops polling
        // itself once the previous job is terminal or provably cancelled.
        startLogin(active.harness, active.profile);
    });
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
    host.querySelector('[data-login-dismiss]')?.addEventListener('click', () => {
        withLoginTransition(async () => {
            // Stale wiring: a rebuilt card (or a completed transition) may
            // have replaced the job this handler captured.
            if (state.activeJob !== active) return;
            if (active.jobId && !jobStateSummary(active.job || {}).terminal) {
                // C7: the job id of a possibly-live login is never dropped on
                // an UNPROVEN cancel — clearing it would let the next start
                // bypass the serialization guard. The card stays with an
                // honest error until the daemon confirms the job is gone.
                const gone = await cancelLoginJob(active.jobId);
                // Belt: the lock already excludes concurrent transitions, but
                // an identity drift across the await must never clear a job
                // this handler does not own.
                if (state.activeJob !== active) return;
                // A poll tick may have settled the job while the DELETE was in
                // flight: a terminal job needs no cancel, the user asked the
                // card to close — fall through to the clear instead of
                // freezing a settled card on a stale cancel error.
                const settledMeanwhile = loginSettleProven(active);
                if (!gone && !settledMeanwhile) {
                    active.error = 'Could not cancel this sign-in — it may still be running. '
                        + 'Try dismissing again once the daemon is reachable.';
                    renderLoginCard();
                    return;
                }
            }
            stopJobPolling();
            state.activeJob = null;
            renderLoginCard();
            refreshStatus();
        });
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

/**
 * Cancel a login job and REPORT whether the daemon no longer runs it (C7):
 * ok/404/410 mean the job is gone; anything else (5xx, network death) means
 * it may still be live — the caller must not start a second job beside it.
 */
export async function cancelLoginJob(jobId, fetchImpl = apiFetch) {
    if (!jobId) return true;
    try {
        const resp = await fetchImpl(`/api/claudexor/login/${encodeURIComponent(jobId)}`, { method: 'DELETE' });
        return !!resp && (resp.ok || resp.status === 404 || resp.status === 410);
    } catch (err) {
        return false;
    }
}

/**
 * Whether the login job PROVABLY settled: only a TERMINAL job snapshot counts.
 * A verdict alone is NOT proof — the give-up path issues {kind:'unconfirmed'}
 * on LOST CONTACT, where the job may still be live server-side; the
 * terminal-derived kinds (success/failure/recheck) always ride a terminal
 * snapshot anyway, so the snapshot is the one honest witness.
 */
export function loginSettleProven(active) {
    return jobStateSummary(active?.job || {}).terminal;
}

// ONE lock for EVERY login lifecycle transition (C7): start AND dismiss.
// Each transition is an async section that reads and replaces
// state.activeJob across awaits (create POST while jobId is still '';
// cancel DELETE while a start could run) — serializing only the starts
// left the dismiss-vs-start overlap able to drop custody of a live job.
let loginTransitionBusy = false;

async function withLoginTransition(fn) {
    if (loginTransitionBusy) return false;
    loginTransitionBusy = true;
    try {
        await fn();
        return true;
    } finally {
        loginTransitionBusy = false;
    }
}

export async function startLogin(harness, profile) {
    if (!harness) return;
    await withLoginTransition(() => _startLoginLocked(harness, profile));
}

async function _startLoginLocked(harness, profile) {
    // C7 (plan roast, accepted): a NEW login may start only once the previous
    // job is terminal or PROVABLY cancelled — a second job beside a live one
    // races the daemon and orphans the old job server-side. Centralized HERE
    // so every entry point (Connect, Add account, Retry) inherits the guard.
    const prev = state.activeJob;
    if (prev && prev.jobId && !jobStateSummary(prev.job || {}).terminal) {
        const cancelled = await cancelLoginJob(prev.jobId);
        // Re-read the world after the await: a poll tick may have settled the
        // job (succeeded/failed) while the DELETE was in flight — a terminal
        // job needs no cancellation, and writing a cancel error AFTER the
        // settle would freeze the card on a stale face (the terminal tick
        // schedules no further poll to clear it).
        const settledMeanwhile = state.activeJob !== prev || loginSettleProven(prev);
        if (!cancelled && !settledMeanwhile) {
            prev.error = 'Could not cancel the active sign-in — it may still be running. '
                + 'Retry from its card, or dismiss it first.';
            renderLoginCard();
            return;
        }
    }
    stopJobPolling();
    // One start shape for every harness: the in-app flow is asked for and the
    // server/engine decide the hosting (loginFlow rides only for codex).
    const body = { harness, login_flow: 'device_auth' };
    if (profile) body.profile_id = profile;
    state.activeJob = {
        harness, profile, jobId: '', job: null, attachCommand: '', error: '',
        startedAtMs: Date.now(), engineDegraded: false,
        inputValue: '', inputBusy: false, inputSent: false, inputError: '', inputNote: '',
        verdict: null, confirming: false, advancedOpen: false, preparingRuntime: true,
    };
    const active = state.activeJob;
    renderLoginCard();
    try {
        const resp = await apiFetch('/api/claudexor/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json().catch(() => ({}));
        if (state.activeJob !== active) return;
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        active.preparingRuntime = false;
        active.jobId = String(data.job_id || '');
        active.job = data.job || {};
        active.attachCommand = String(data.attach_command || '');
        // An engine that predates the disclosure modes never sends a link;
        // the demoted Advanced fallback may show right away (still collapsed).
        active.engineDegraded = data.disclosure_native === false
            && !!active.attachCommand;
        startJobPolling();
    } catch (error) {
        if (state.activeJob !== active) return;
        active.preparingRuntime = false;
        active.error = String(error.message || error);
    }
    renderLoginCard();
}

function startJobPolling() {
    stopJobPolling();
    // #125: a setTimeout CHAIN, not an interval — the next tick is armed only
    // after the previous round-trip settled, so overlapping polls (and the
    // out-of-order snapshot that overwrote a terminal one) are structurally
    // impossible; the pollResponseApplies ordering guard still stands for a
    // card closed/reopened mid-flight. Failures back off exponentially and
    // after 10 CONSECUTIVE failures the chain stops with an honest
    // "unconfirmed" verdict instead of hammering a dead daemon forever.
    let consecutiveFailures = 0;
    const schedule = (delayMs) => { state.jobTimer = setTimeout(tick, delayMs); };
    const tick = async () => {
        state.jobTimer = 0;
        const active = state.activeJob;
        if (!active?.jobId) return;
        let snapshot = null;
        let readOk = false;
        try {
            const resp = await apiFetch(`/api/claudexor/login/${encodeURIComponent(active.jobId)}`, { cache: 'no-store' });
            const data = await resp.json().catch(() => ({}));
            // The CREATE answer is the only authority on whether this job has a
            // copy-paste command: it issues one exactly for a client_pty job,
            // while the poll route hands one back for every job including the
            // daemon-transport codex device flow, which must never show it.
            if (resp.ok) {
                snapshot = data.job || null;
                readOk = true;
            }
        } catch (err) { /* transient poll failure; the chain retries with backoff */ }
        if (!pollResponseApplies(active, state.activeJob)) return;
        // ANY parsed answer (a pending snapshot included) resets the failure
        // streak — a legitimate multi-minute sign-in must never trip give-up.
        consecutiveFailures = readOk ? 0 : consecutiveFailures + 1;
        if (snapshot) active.job = snapshot;
        if (readOk && active.error) {
            // A successful job read re-establishes the true state: the
            // cancel-failure note (a Dismiss whose DELETE the daemon never
            // confirmed) is TRANSIENT and must not mask a live/terminal face
            // forever — a recovered daemon + succeeded login used to leave the
            // card stuck on the cancel-error face while the account row said
            // connected. Fatal START errors never reach this line: a job that
            // failed to create has no jobId and is never polled.
            active.error = '';
        }
        const verdict = loginVerdict(active.job || {});
        if (verdict.kind !== 'pending') {
            settleVerdict(active, verdict);
            return;
        }
        const next = nextJobPollDelay(consecutiveFailures);
        if (next.giveUp) {
            // Contact with the job is lost, NOT the login: the sign-in may
            // well have completed. The EXISTING typed unconfirmed verdict path
            // owns this face. Deliberately NOT active.error — its Try-again
            // would start a SECOND login while the old jobId may still be
            // live; a fresh attempt goes through Close (which DELETEs a
            // non-terminal job) or the account row once the daemon answers.
            active.verdict = {
                kind: 'unconfirmed',
                reason: 'job_poll_lost_contact',
                detail: 'Lost contact with the sign-in job — the sign-in may '
                    + 'still have completed. Press Refresh to re-check the account.',
            };
            renderLoginCard();
            refreshStatus({ force: true });
            return;
        }
        renderLoginCard();
        schedule(next.delayMs);
    };
    schedule(nextJobPollDelay(0).delayMs);
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
    // The same join the poll takes: the confirmation reads the status endpoint
    // directly, and running it beside an in-flight wake would make it the
    // THIRD writer in the race the wake exists to settle. Wait the wake out,
    // then confirm against the reading it produced.
    if (state.wakeInFlight) await state.wakeInFlight;
    const generation = state.readGeneration;
    const check = await confirmLoginLive(active.harness, active.profile || '', {
        isStale: () => state.activeJob !== active,
    });
    if (state.activeJob !== active) return;
    active.confirming = false;
    // GENERATION, not just job identity. This read goes to the status endpoint
    // directly, outside `refreshStatus`, so any commit that landed while it was
    // in flight has already published a newer payload — and `readGeneration`
    // promises that a read issued before a commit never lands on top of it.
    const superseded = generation !== state.readGeneration;
    if (check.payload && !superseded) commitStatusPayload(check.payload);
    // A positive confirmation does not expire. The generation is captured once
    // for a SERIES of attempts, so a commit landing between attempt 1 and
    // attempt 2 marked the whole series stale — including the later attempt
    // that watched the account come online. Seeing the login is monotone
    // evidence: believe it whenever it happened, and otherwise judge the newest
    // reading with the same predicate rather than the one this series began on.
    const confirmed = check.confirmed
        || accountLoginConfirmed(state.payload, active.harness, active.profile || '');
    // An exhausted window is not a failure verdict: the job's own read was
    // already judged unproven, and the account row often appears a tick after
    // the bounded re-poll gives up. Say that, and say where to look.
    active.verdict = confirmed
        ? { kind: 'success', reason: '' }
        : { kind: 'unconfirmed', reason: verdict.reason };
    renderRows();
}

function stopJobPolling() {
    if (state.jobTimer) clearTimeout(state.jobTimer);
    state.jobTimer = 0;
}

// The ONE place a fresh status payload is committed. Three paths produce one
// (the poll, the wake, and the login confirmation), and they disagreed: a
// transport error was retired by two of them and a wake error by one, so a
// daemon that came up still rendered "Could not start the agent daemon" until
// the next tick — and a wake error was ALSO cleared by a 200 that reported the
// daemon still down, wiping the reason before the owner could read it. A fresh
// answer always retires staleness, because the answer IS current. It retires
// the wake error only on proven recovery: its basis is a daemon that would not
// answer, so only a daemon that ANSWERED expires it — `daemonAnswered`, which
// is deliberately not the literal `running`.
// The facets the status contract declares. `daemonAnswered` iterates this list,
// so a facet added to the contract and not here would be invisible to it —
// tests/test_gateway_parity.py reads this literal out of the source and
// compares it with `ClaudexorStatusReads`.
export const READ_FACETS = ['catalog', 'accounts', 'quota'];

export function unreadFacets(payload) {
    // Which facets did NOT answer, in contract order. Empty means everything
    // this payload promises was actually read.
    return READ_FACETS.filter((facet) => facetReadState(payload, facet) !== 'ok');
}

export function daemonAnswered(payload) {
    // Did the daemon ANSWER? A disjunction whose halves prove different things.
    // An authenticated `running` is positive evidence on its own — the
    // handshake happened. Anything else is NOT evidence of silence: a PARTIAL
    // refusal (quota times out while the catalog and the account store land) is
    // reported as `daemon.state = 'unreachable'`, so a predicate written on the
    // literal `running` called a daemon dead while its own accounts were on
    // screen — it kept a failed wake's error standing over them and made
    // Refresh offer to start something already answering. There a facet's own
    // `ok` is the evidence. What the aggregate can never be is the NEGATIVE
    // answer.
    //
    // The facet states come from `facetReadState`, never from the raw block:
    // reading `Object.values(reads)` here was a SECOND reader of the same
    // field, and the two disagreed — an ARRAY is `typeof 'object'`, so
    // `reads: ['ok']` counted as an answered facet here while `facetReadState`
    // correctly called every facet `failed`.
    if (String(payload?.daemon?.state || '') === 'running') return true;
    return READ_FACETS.some((facet) => facetReadState(payload, facet) === 'ok');
}

export function commitStatusPayload(data) {
    state.payload = data;
    state.statusTransportError = '';
    if (daemonAnswered(data)) state.wakeError = '';
    // The ONE owner of the read version. It used to be bumped only by the wake,
    // so two readers of the same generation could still land out of order: a
    // login confirmation that began before an ordinary poll committed AFTER it
    // and put the older snapshot back. Every accepted payload retires every
    // read that began before it, whichever path produced it.
    state.readGeneration += 1;
}

// OWNER action behind the Refresh button when the daemon is asleep: start it,
// then take the fresh reading. Never called by the poll — the status GET stays
// side-effect-free, which is what makes an automatic 5s wake impossible.
function wakeErrorIfStillNeeded(message) {
    // A start that failed is only news while the daemon is still not answering.
    // An ordinary poll can commit a live reading WHILE the wake is in flight —
    // and then a refusal that arrived afterwards would plant "Could not start
    // the agent daemon" on top of a panel already listing that daemon's
    // accounts. The failure is real; it just stopped mattering.
    return daemonAnswered(state.payload) ? '' : message;
}

export async function wakeDaemon({ fetchImpl = apiFetch } = {}) {
    if (state.wakeInFlight) return state.wakeInFlight;
    // CAUSAL ordering with a GET already in flight: its read may land daemon-side
    // before or after ours — the client cannot know — but if we START after its
    // COMMIT, our reading is later than its by construction, and the
    // unconditional commit below is correct. So wait it out before posting.
    // (A GET started after us joins us instead — the check at the top of
    // refreshStatus — so the two writers never overlap in either order.)
    const pendingRead = state.statusInFlight;
    // NOTE: no generation bump here. An opening bump used to guard "a read
    // started before the press must not undo it", but the bump taken at COMMIT
    // time already stales every read that started before the commit — which
    // includes those. On the failing path there is no commit to protect, and
    // the refusal survives on its own (commitStatusPayload retires a wake error
    // only on proven recovery). Two interleavings DO tell the two apart, and
    // both are benign: a read released while the wake is still pending repaints
    // from its own answer instead of being discarded, and a read that answers
    // for a daemon that came up anyway retires the error — which is exactly the
    // recovery rule this module declares. So the bump is not here claiming an
    // invariant it does not hold alone.
    state.wakeError = '';
    state.wakeBusy = true;
    renderRows();
    state.wakeInFlight = (async () => {
        try {
            if (pendingRead) await pendingRead.catch(() => {});
            const resp = await fetchImpl('/api/claudexor/wake', { method: 'POST' });
            const data = await resp.json().catch(() => ({}));
            if (resp.ok) {
                // The bump lives in commitStatusPayload now — one owner for the
                // read version, so this path does not need its own.
                commitStatusPayload(data);
                state.statusEverSettled = true;
            } else {
                state.wakeError = wakeErrorIfStillNeeded(String(data?.error || `HTTP ${resp.status}`));
            }
        } catch (err) {
            state.wakeError = wakeErrorIfStillNeeded(String(err?.message || err || 'request failed'));
        } finally {
            state.wakeBusy = false;
            state.wakeInFlight = null;
        }
        renderRows();
    })();
    return state.wakeInFlight;
}

export async function refreshStatus({ force = false, fetchImpl = apiFetch } = {}) {
    // Poll only while the section is actually on screen. Each tick fans out to four
    // daemon round-trips, and the interval started at app load and never stopped —
    // so every page in the app paid for them (`.page` is display:none when inactive,
    // which is exactly what offsetParent reports). #125: the SAME gate used to
    // suppress the very first fetch too (init runs before the page is shown), so
    // the section sat on "Checking daemon…" until the 5s interval; a FORCED
    // refresh (first load, the Refresh button, the poll give-up) skips the gate.
    const host = document.getElementById('harness-accounts-rows');
    if (!host) return;
    if (shouldSkipStatusRefresh({
        hostVisible: host.offsetParent !== null,
        hidden: document.hidden,
        force,
    })) return;
    // A wake OWNS the reading while it runs. It ensures the daemon and then
    // reads, so its answer is the one worth having — and a poll running beside
    // it is a second writer whose order against it cannot be established from
    // the client at all: neither request knows when the other's read actually
    // happened daemon-side. Comparing generations does not settle it either
    // (either direction discards a genuinely newer reading). Removing the
    // other writers does: the poll joins here, and the login confirmation
    // waits the wake out in settleVerdict the same way.
    if (state.wakeInFlight) return state.wakeInFlight;
    // SINGLE-FLIGHT. The 5s interval outlives a read that takes tens of seconds,
    // and each read fans out to four CLI-probing daemon GETs — unguarded, a
    // visible panel stacks ~6 reads (~24 concurrent probes) against the very
    // daemon whose slowness this is meant to relieve, and the first response to
    // land clears the flag while later ones are still running. Callers share the
    // live read; the interval's next tick starts the next one.
    if (state.statusInFlight) return state.statusInFlight;
    const generation = state.readGeneration;
    state.statusChecking = true;
    // Repaint BEFORE the request only until the first read SETTLES (success or
    // failure) — with anything already said, a per-poll repaint is churn, and
    // against a persistently failing endpoint it flickers muted↔error every tick.
    if (!state.statusEverSettled) renderRows();
    state.statusInFlight = (async () => {
        try {
            const resp = await fetchImpl('/api/claudexor/status', { cache: 'no-store' });
            const data = await resp.json().catch(() => ({}));
            if (generation !== state.readGeneration) return;  // a newer read won
            if (resp.ok) {
                commitStatusPayload(data);
            } else {
                // The MIRROR of the false absence: a swallowed failure used to
                // leave the last good payload rendering "Claudexor ready" with
                // green badges forever. Keep the snapshot — it is the best we
                // have — but stop presenting it as current.
                state.statusTransportError = String(data?.error || `HTTP ${resp.status}`);
            }
        } catch (err) {
            if (generation !== state.readGeneration) return;  // a newer read won
            state.statusTransportError = String(err?.message || err || 'request failed');
        } finally {
            state.statusChecking = false;
            state.statusEverSettled = true;
            state.statusInFlight = null;
        }
        renderRows();
    })();
    return state.statusInFlight;
}

// #125: wake the visibility-gated refresh the moment the section can actually
// be seen again — browser tab restored, Settings page shown, Providers subtab
// activated. Registered once per app lifetime (module flag); each handler
// calls the UNFORCED refresh, so the gate still filters invisible states.
let activationHandlersRegistered = false;

function registerActivationHandlers() {
    if (activationHandlersRegistered) return;
    activationHandlersRegistered = true;
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) refreshStatus();
    });
    window.addEventListener('ouro:page-shown', (event) => {
        // The boot default subtab does not dispatch a subtab event; page-shown
        // covers reaching Settings with Providers already active.
        if (event?.detail?.page === 'settings') refreshStatus();
    });
    window.addEventListener('ouro:settings-subtab-shown', (event) => {
        if (event?.detail?.tab === 'providers') refreshStatus();
    });
}

export function initHarnessAccounts() {
    const refreshBtn = document.getElementById('btn-harness-refresh');
    refreshBtn?.addEventListener('click', () => {
        // A sleeping daemon cannot be re-read into existence: there the button
        // is the owner's explicit start. Live, it stays a plain re-read. Same
        // predicate the LABEL uses, so the two cannot disagree again.
        return refreshActionKind(state.payload) === 'refresh'
            ? refreshStatus({ force: true })
            : wakeDaemon();
    });
    registerActivationHandlers();
    // Forced: init runs while the page may not be visible yet, and the first
    // daemon read must not wait 5 seconds for the interval (#125).
    refreshStatus({ force: true });
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(refreshStatus, POLL_MS);
}
