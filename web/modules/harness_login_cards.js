// Agent login cards — the host-neutral controller + view (phase 2 seam).
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
// This machinery used to be welded into the Providers-tab section
// (`harness_accounts.js`). It is extracted so the onboarding wizard mounts the
// SAME flow instead of reimplementing device codes, backoff and the
// verify-race — and so both surfaces inherit every fix once.
//
// TWO render modes:
//   * `full`    — exactly what Settings renders today: link/device code,
//                 the optional paste-code entry, the live state line, the
//                 verdict + the engine's own sentence, the collapsed Advanced
//                 terminal fallback, and Close.
//   * `compact` — Connect + progress + verified state + retry only, for a
//                 wizard step. Disclosed residual: the paste-code entry and
//                 the Advanced terminal fallback are NOT rendered in compact,
//                 so a claude login whose localhost callback cannot complete
//                 (browser on another device) has no finishing path in that
//                 mode; the onboarding phase decides whether to keep it that
//                 way or opt into `full`.
//
// Pure helpers up top are node-tested without a DOM.

import { apiFetch } from './api_client.js';
import { accountLoginConfirmed, claudexorStatus } from './claudexor_status_store.js';
import { escapeHtmlAttr as escapeHtml, safeExternalHrefAttr } from './utils.js';

const JOB_POLL_MS = 3000;
export const JOB_POLL_MAX_DELAY_MS = 30000;
export const JOB_POLL_GIVE_UP_FAILURES = 10;

export const LOGIN_CARD_FULL = 'full';
export const LOGIN_CARD_COMPACT = 'compact';

// ---------------------------------------------------------------------------
// Pure helpers.
// ---------------------------------------------------------------------------

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
    store = claudexorStatus, attempts = CONFIRM_ATTEMPTS, delayMs = CONFIRM_POLL_MS,
    sleepImpl = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    isStale = () => false,
    // The read seam. It goes through the SHARED store by default, so the
    // account rows this re-check consults are the very rows on screen — one
    // request, one truth. Tests inject a direct reader.
    readStatus = null,
} = {}) {
    // The verify-race re-check: briefly re-poll live account status and let
    // IT decide, instead of declaring failure off the job's one stale
    // verification read. Bounded; answers with the last payload so the caller
    // can refresh the rows from the same reads.
    const read = typeof readStatus === 'function'
        ? readStatus
        : async () => {
            await store.refresh();
            return store.error ? null : store.snapshot;
        };
    let payload = null;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
        if (attempt > 0) await sleepImpl(delayMs);
        if (isStale()) return { confirmed: false, stale: true, payload };
        try {
            const data = await read();
            if (data) {
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

export function loginCardHtml(active, nowMs = Date.now(), { mode = LOGIN_CARD_FULL } = {}) {
    // The card's whole body as a STRING — pure, so every face is asserted in
    // node without a DOM: the unsafe-URL refusal, the unconfirmed re-check,
    // and the rule that a verdict silences the live status line.
    if (!active) return '';
    const compact = mode === LOGIN_CARD_COMPACT;
    const summary = jobStateSummary(active.job || {});
    const disclosure = deviceCodeDisclosure(active.job || {});
    const face = loginCardFace(active);
    // NOT .panel-card: that class lives in onboarding.css, which the settings
    // pages never load — the card rendered with no frame at all (finding #4).
    const bits = [`<div class="harness-login-card" data-login-card data-login-mode="${escapeHtml(compact ? LOGIN_CARD_COMPACT : LOGIN_CARD_FULL)}">`,
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
    if (!compact && face === 'device' && loginInputSupport(active.job || {}) && !summary.terminal) {
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
    // A settled non-success verdict in COMPACT mode still offers the retry the
    // mode promises; in full mode the account row's own Connect button is the
    // retry, exactly as today.
    if (compact && (active.verdict?.kind === 'failure' || active.verdict?.kind === 'unconfirmed')) {
        bits.push('<button type="button" class="btn btn-primary" data-login-retry>Try again</button>');
    }
    // …and beside that verdict, the engine's own explanation. The two verdict
    // texts above are FIXED constants (deliberately: one is a category, the
    // other an honest "unknown"), so without this line a settled login says
    // nothing about WHY — the daemon's sentence lived in the snapshot and was
    // rendered nowhere. Only for a settled non-success verdict: beside
    // "Connected." a stale message would contradict the outcome, and while the
    // job is pending the live status line already owns the card.
    if (!compact && (active.verdict?.kind === 'failure' || active.verdict?.kind === 'unconfirmed')) {
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
    if (!compact && !summary.terminal && attachFallbackDue(active, nowMs)) {
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
    if (!compact) {
        bits.push('<button type="button" class="settings-ghost-btn" data-login-dismiss>Close</button>');
    }
    bits.push('</div>');
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

// ---------------------------------------------------------------------------
// The controller. Owns the job lifecycle; the caller owns the DOM host.
// ---------------------------------------------------------------------------

/**
 * @param {object} options
 * @param {Element|Function} options.host  where the card renders (element or getter)
 * @param {object} [options.store]         the shared Claudexor status store
 * @param {string} [options.mode]          'full' | 'compact'
 * @param {Function} [options.fetchImpl]   transport
 * @param {Function} [options.onSettled]   called after a verdict lands / the card closes
 * @returns {object} controller with start/close/render/dispose
 *
 * LIFECYCLE CONTRACT (public — two sibling branches mount this):
 *   * `close()` and `dispose()` both resolve to a BOOLEAN: whether custody of
 *     the login job was released (the daemon is provably no longer running it).
 *     `false` means the card kept the job id on purpose, because the cancel
 *     could not be proven and a second login must not be started beside it.
 *   * Neither is ever DROPPED. Transitions queue and run in order, so a close
 *     that arrives during the create POST is applied to the job that POST
 *     installs — it used to be answered `false` and forgotten, leaving a live
 *     server-side job nobody ever cancelled.
 *   * `dispose()` is RETRYABLE after a `false`: the job id is retained and a
 *     later call re-runs the same proven-cancel path. A host must therefore
 *     await the verdict and refuse to remount while it is `false` — the "one
 *     live login" invariant spans controller INSTANCES only if the host
 *     enforces it (a dropped verdict + a remount = two live logins).
 *   * `start()` COALESCES duplicate starts for the same harness/profile onto
 *     one promise, so a double-click cannot create-then-cancel-then-create.
 */
export function createLoginCardController({
    host,
    store = claudexorStatus,
    mode = LOGIN_CARD_FULL,
    fetchImpl = apiFetch,
    doc = () => (typeof document === 'undefined' ? null : document),
    now = () => Date.now(),
    onSettled = () => {},
} = {}) {
    const getHost = typeof host === 'function' ? host : () => host;
    const getDoc = typeof doc === 'function' ? doc : () => doc;
    const ctl = {
        active: null,
        jobTimer: 0,
        transitionChain: Promise.resolve(),
        pendingStart: null,
        releaseHold: null,
        disposed: false,
    };

    function holdStatusPolling() {
        // A live login job keeps the shared status poll awake even when the
        // surface would otherwise be considered idle — the account rows are
        // exactly what the login is about to change.
        if (ctl.releaseHold || !store?.holdPolling) return;
        ctl.releaseHold = store.holdPolling('login-job');
    }

    function releaseStatusPolling() {
        if (!ctl.releaseHold) return;
        ctl.releaseHold();
        ctl.releaseHold = null;
    }

    function stopJobPolling() {
        if (ctl.jobTimer) clearTimeout(ctl.jobTimer);
        ctl.jobTimer = 0;
    }

    function clearHost() {
        const hostEl = getHost();
        if (hostEl) hostEl.innerHTML = '';
    }

    function render() {
        const hostEl = getHost();
        if (!hostEl) return;
        const active = ctl.active;
        if (!active) { hostEl.innerHTML = ''; return; }
        preserveCardFocus(hostEl, () => {
            hostEl.innerHTML = loginCardHtml(active, now(), { mode });
        }, getDoc());
        wireLoginCard(hostEl, active);
    }

    function withLoginTransition(fn) {
        // ONE QUEUE for EVERY login lifecycle transition (C7): start, close and
        // shutdown. Each transition is an async section that reads and replaces
        // ctl.active across awaits (create POST while jobId is still '';
        // cancel DELETE while a start could run), so they must not interleave.
        //
        // It used to be a try-LOCK that DROPPED whatever arrived while another
        // transition ran, and dropping a close is not a small loss: a Close
        // pressed during the create POST answered "false" and vanished, the
        // POST then installed a live job, and no DELETE was ever issued — the
        // daemon kept running a sign-in for a card the owner had dismissed.
        // Queued instead, every transition runs in order and observes the world
        // the previous one left, so the close applies to the job it created.
        const run = ctl.transitionChain.then(() => fn(), () => fn());
        ctl.transitionChain = run.then(() => {}, () => {});
        return run;
    }

    function wireLoginCard(hostEl, active) {
        hostEl.querySelector('[data-login-retry]')?.addEventListener('click', () => {
            // NO preemptive stopJobPolling() here: if the C7 guard inside
            // start refuses (cancel unproven, job still live), the old poll
            // must stay armed — it is the recovery path that clears the
            // transient note and settles the real verdict. _startLocked stops
            // polling itself once the previous job is terminal or provably
            // cancelled.
            start(active.harness, active.profile);
        });
        hostEl.querySelector('[data-copy-signin-link]')?.addEventListener('click', () => {
            const disclosure = deviceCodeDisclosure(active.job || {});
            if (disclosure?.url) navigator.clipboard?.writeText(disclosure.url);
        });
        hostEl.querySelector('[data-copy-device-code]')?.addEventListener('click', () => {
            const disclosure = deviceCodeDisclosure(active.job || {});
            if (disclosure?.code) navigator.clipboard?.writeText(disclosure.code);
        });
        hostEl.querySelector('[data-copy-attach]')?.addEventListener('click', () => {
            navigator.clipboard?.writeText(active.attachCommand || '');
        });
        const advanced = hostEl.querySelector('[data-login-advanced]');
        // Re-renders (every poll tick) must not slam the user's Advanced section
        // shut: the open state is carried on the job and re-applied.
        advanced?.addEventListener('toggle', () => { active.advancedOpen = advanced.open; });
        const codeInput = hostEl.querySelector('[data-login-code-input]');
        codeInput?.addEventListener('input', () => { active.inputValue = codeInput.value; });
        codeInput?.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') { event.preventDefault(); submitCodeFromCard(active); }
        });
        hostEl.querySelector('[data-login-code-submit]')?.addEventListener('click', () => submitCodeFromCard(active));
        hostEl.querySelector('[data-login-dismiss]')?.addEventListener('click', () => close(active));
    }

    async function _closeLocked(expected, { shuttingDown = false } = {}) {
        // The ONE proven-cancel path. Close and shutdown differ in exactly two
        // places, both marked below; everything about custody is shared, so a
        // job can never be released down one path on evidence the other would
        // have refused.
        const active = ctl.active;
        if (!active) {
            if (shuttingDown) { stopJobPolling(); releaseStatusPolling(); clearHost(); }
            return true;
        }
        if (expected !== undefined && active !== expected) return false;
        if (active.jobId && !jobStateSummary(active.job || {}).terminal) {
            // C7: the job id of a possibly-live login is never dropped on an
            // UNPROVEN cancel — clearing it would let the next start bypass the
            // serialization guard. The card stays with an honest error until
            // the daemon confirms the job is gone.
            const gone = await cancelLoginJob(active.jobId, fetchImpl);
            // Belt: the queue already excludes concurrent transitions, but an
            // identity drift across the await must never clear a job this
            // handler does not own.
            if (ctl.active !== active) return false;
            // A poll tick may have settled the job while the DELETE was in
            // flight: a terminal job needs no cancel, the user asked the card
            // to close — fall through to the clear instead of freezing a
            // settled card on a stale cancel error.
            const settledMeanwhile = loginSettleProven(active);
            if (!gone && !settledMeanwhile) {
                active.error = 'Could not cancel this sign-in — it may still be running. '
                    + 'Try dismissing again once the daemon is reachable.';
                if (shuttingDown) {
                    // DIFFERENCE 1: the host is going away either way, so the
                    // error has no surface left to render on — but custody is
                    // RETAINED (ctl.active keeps the job id) and the caller is
                    // told `false`, because the daemon may still be running it.
                    stopJobPolling();
                    releaseStatusPolling();
                    clearHost();
                } else {
                    render();
                }
                return false;
            }
        }
        stopJobPolling();
        releaseStatusPolling();
        ctl.active = null;
        // DIFFERENCE 2: a shutdown has no card to repaint and no host to notify
        // — it clears the surface and stays silent.
        if (shuttingDown) { clearHost(); return true; }
        render();
        store?.refresh?.();
        onSettled();
        return true;
    }

    /**
     * Close the card, cancelling a still-live job first (C7).
     * @param {object} [expected] when given, a no-op unless it is still the
     *        active job — stale wiring from a rebuilt card must never clear a
     *        job it does not own.
     * @returns {Promise<boolean>} whether custody was released (see the
     *        lifecycle contract above): `false` means a live job is still held.
     */
    function close(expected = undefined) {
        return withLoginTransition(() => _closeLocked(expected));
    }

    async function submitCodeFromCard(active) {
        // Double-submit guard: one POST in flight at a time, and an accepted
        // code is final — the button stays disabled after success.
        if (!active || active.inputBusy || active.inputSent) return;
        const value = String(active.inputValue || '').trim();
        if (!value) return;
        // The job may have finished while the user typed: render the verdict
        // instead of posting into a dead job.
        if (jobStateSummary(active.job || {}).terminal) { render(); return; }
        active.inputBusy = true;
        active.inputError = '';
        render();
        const result = await submitLoginInput(active.jobId, value, { fetchImpl });
        if (ctl.active !== active) return;   // card closed while in flight
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
        render();
    }

    function start(harness, profile) {
        if (!harness || ctl.disposed) return Promise.resolve();
        // Duplicate starts are COALESCED, not merely serialized. The queue
        // guaranteed order, and order made an ordinary double-click into
        // create → cancel → create: the second start ran the C7 guard against
        // the job the first one had just installed, DELETEd it, and created
        // another — so the device link the owner was reading was invalidated
        // the moment it appeared. While a start for the same account is still
        // settling, every caller shares its promise.
        // The separator is spelled as a source escape rather than written as a
        // literal NUL byte: one raw NUL makes every ordinary search tool
        // classify this 53 KB module as binary, and `grep` then answers with
        // SILENCE instead of "no match", so a reviewer looking for a symbol
        // in here concludes it does not exist. Same key, same collisions.
        const key = `${harness}\u0000${profile || ''}`;
        if (ctl.pendingStart && ctl.pendingStart.key === key) return ctl.pendingStart.promise;
        const promise = withLoginTransition(() => _startLocked(harness, profile));
        const clear = () => {
            if (ctl.pendingStart && ctl.pendingStart.promise === promise) ctl.pendingStart = null;
        };
        // Both arms, and on the ORIGINAL promise: a derived one would be an
        // unhandled rejection for callers that fire and forget.
        promise.then(clear, clear);
        ctl.pendingStart = { key, promise };
        return promise;
    }

    async function _startLocked(harness, profile) {
        // Re-read after the queue wait: a shutdown may have been queued ahead of
        // this start, and a disposed controller must not create a job nobody
        // will ever poll or cancel.
        if (ctl.disposed) return;
        // C7 (plan roast, accepted): a NEW login may start only once the previous
        // job is terminal or PROVABLY cancelled — a second job beside a live one
        // races the daemon and orphans the old job server-side. Centralized HERE
        // so every entry point (Connect, Add account, Retry) inherits the guard.
        const prev = ctl.active;
        if (prev && prev.jobId && !jobStateSummary(prev.job || {}).terminal) {
            const cancelled = await cancelLoginJob(prev.jobId, fetchImpl);
            // Re-read the world after the await: a poll tick may have settled the
            // job (succeeded/failed) while the DELETE was in flight — a terminal
            // job needs no cancellation, and writing a cancel error AFTER the
            // settle would freeze the card on a stale face (the terminal tick
            // schedules no further poll to clear it).
            const settledMeanwhile = ctl.active !== prev || loginSettleProven(prev);
            if (!cancelled && !settledMeanwhile) {
                prev.error = 'Could not cancel the active sign-in — it may still be running. '
                    + 'Retry from its card, or dismiss it first.';
                render();
                return;
            }
        }
        stopJobPolling();
        holdStatusPolling();
        // One start shape for every harness: the in-app flow is asked for and the
        // server/engine decide the hosting (loginFlow rides only for codex).
        const body = { harness, login_flow: 'device_auth' };
        if (profile) body.profile_id = profile;
        ctl.active = {
            harness, profile, jobId: '', job: null, attachCommand: '', error: '',
            startedAtMs: now(), engineDegraded: false,
            inputValue: '', inputBusy: false, inputSent: false, inputError: '', inputNote: '',
            verdict: null, confirming: false, advancedOpen: false, preparingRuntime: true,
        };
        const active = ctl.active;
        render();
        try {
            const resp = await fetchImpl('/api/claudexor/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await resp.json().catch(() => null);
            if (ctl.active !== active) return;
            if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
            // A 2xx is not a created job. Without a job id there is nothing to
            // poll, nothing to cancel and nothing to report: the card used to
            // accept such an answer and sit "Starting the sign-in…" forever,
            // with no error and no way out. Fail here instead, where the error
            // face and its Try again already exist.
            const jobId = String(data?.job_id || '');
            if (!jobId) throw new Error('the sign-in service returned no job id');
            active.preparingRuntime = false;
            active.jobId = jobId;
            active.job = data.job && typeof data.job === 'object' ? data.job : {};
            active.attachCommand = String(data.attach_command || '');
            // An engine that predates the disclosure modes never sends a link;
            // the demoted Advanced fallback may show right away (still collapsed).
            active.engineDegraded = data.disclosure_native === false
                && !!active.attachCommand;
            startJobPolling();
        } catch (error) {
            if (ctl.active !== active) return;
            active.preparingRuntime = false;
            active.error = String(error.message || error);
            releaseStatusPolling();
        }
        render();
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
        const schedule = (delayMs) => { ctl.jobTimer = setTimeout(tick, delayMs); };
        const tick = async () => {
            ctl.jobTimer = 0;
            const active = ctl.active;
            if (!active?.jobId) return;
            let snapshot = null;
            let readOk = false;
            try {
                const resp = await fetchImpl(`/api/claudexor/login/${encodeURIComponent(active.jobId)}`, { cache: 'no-store' });
                const data = await resp.json().catch(() => null);
                // The CREATE answer is the only authority on whether this job has a
                // copy-paste command: it issues one exactly for a client_pty job,
                // while the poll route hands one back for every job including the
                // daemon-transport codex device flow, which must never show it.
                //
                // A poll answer without a `job` object is a PROTOCOL failure, not
                // a healthy read: the route answers {job, attach_command} on every
                // success, so a 2xx carrying no job tells us nothing about the
                // login. Counting it as a success reset the failure streak, and a
                // stream of such answers polled forever — the documented ten-failure
                // give-up was unreachable, and the card stayed pending for good.
                const job = data && typeof data.job === 'object' && data.job !== null
                    && !Array.isArray(data.job) ? data.job : null;
                // …and an EMPTY job object is no more an answer than a missing
                // one. The gateway normalizes a non-object engine reply to `{}`
                // (`gateways/claudexor.py::setup_job_call`), so `{job:{}}` is
                // reachable on the real wire, not hypothetical: twelve such
                // polls left the verdict null and armed another timer, because
                // each one reset the failure streak. A snapshot with no state
                // says nothing about this login, so it counts as a failure.
                if (resp.ok && job && jobStateSummary(job).state) {
                    snapshot = job;
                    readOk = true;
                }
            } catch (err) { /* transient poll failure; the chain retries with backoff */ }
            if (!pollResponseApplies(active, ctl.active)) return;
            // ANY answer that CARRIED A JOB (a pending snapshot included) resets the
            // failure streak — a legitimate multi-minute sign-in must never trip
            // give-up — and everything else counts toward the same bounded give-up.
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
                releaseStatusPolling();
                render();
                store?.refresh?.();
                onSettled();
                return;
            }
            render();
            schedule(next.delayMs);
        };
        schedule(nextJobPollDelay(0).delayMs);
    }

    async function settleVerdict(active, verdict) {
        if (!active) return;
        if (verdict.kind !== 'recheck') {
            active.verdict = verdict;
            releaseStatusPolling();
            render();
            store?.refresh?.();
            onSettled();
            return;
        }
        // The verify-race: never declare failure off one stale verification read
        // (the owner's codex login SUCCEEDED while the card said "Login failed ·
        // completed"). Live account status — the truth the rows themselves render
        // — briefly re-polled, is the judge.
        active.confirming = true;
        render();
        const check = await confirmLoginLive(active.harness, active.profile || '', {
            store,
            isStale: () => ctl.active !== active,
        });
        if (ctl.active !== active) return;
        active.confirming = false;
        // A positive confirmation is MONOTONE evidence: a login SEEN online is
        // never un-seen by bookkeeping. The bounded re-check can run out a tick
        // before the account row lands, while a reading that arrived meanwhile
        // (the held status poll, an owner wake) already shows the login — so
        // the newest committed snapshot is judged with the same predicate
        // before "unconfirmed" may be said.
        const confirmed = check.confirmed
            || accountLoginConfirmed(store?.snapshot, active.harness, active.profile || '');
        // An exhausted window is not a failure verdict: the job's own read was
        // already judged unproven, and the account row often appears a tick after
        // the bounded re-poll gives up. Say that, and say where to look.
        active.verdict = confirmed
            ? { kind: 'success', reason: '' }
            : { kind: 'unconfirmed', reason: verdict.reason };
        releaseStatusPolling();
        render();
        onSettled();
    }

    /**
     * Shut the controller down: wait for an in-flight transition (a create POST
     * owns the queue while its job id is still unknown), then run the SAME
     * proven-cancel path Close uses, and clear the host either way.
     *
     * It used to be synchronous and blind: it cleared `ctl.active`, released
     * the hold and returned — with no DELETE for a live job and the rendered
     * card still on screen. Both matter beyond Settings, because the onboarding
     * wizard mounts this controller on a step the owner can cancel mid-login.
     *
     * @returns {Promise<boolean>} whether custody was released. `false` = the
     *        cancel could not be proven, so the job id is deliberately retained
     *        (readable through `active`) rather than forgotten — and this
     *        disposer stays RETRYABLE: call it again and the same proven-cancel
     *        path runs against the same job. That is the whole recovery path
     *        for a host that must not remount while a login may still be live.
     */
    function dispose() {
        if (ctl.disposed && !ctl.active) {
            // Idempotent once custody is genuinely gone: no second DELETE, and
            // nothing to wait on.
            stopJobPolling();
            releaseStatusPolling();
            return Promise.resolve(true);
        }
        // Set FIRST: no new start may be queued behind the shutdown.
        ctl.disposed = true;
        ctl.pendingStart = null;
        return withLoginTransition(() => _closeLocked(undefined, { shuttingDown: true }));
    }

    return {
        start,
        close,
        render,
        dispose,
        get active() { return ctl.active; },
        get mode() { return mode; },
    };
}
