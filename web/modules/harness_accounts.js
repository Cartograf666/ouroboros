// Agent accounts (D30, regrouped in the Agents tab) — the owner-facing surface
// over the owned Claudexor daemon's account truth.
//
// The shape is the owner's (2026-08-08): "все акки клод кода должны быть
// эквивалентны", "позиции кнопок нелогичные… в каждой секции добавить кнопку",
// "текст про лимиты компактнее и понятнее". So:
//
//  * ONE CARD PER FAMILY (Claude Code / Codex / Cursor). The family name and
//    its aggregate status sit in the card header, and that card owns its own
//    Add-account button — the button used to hang off the native row, which is
//    why adding a Codex account meant hunting for a control under an unrelated
//    line.
//  * ROWS ARE EQUIVALENT. The default CLI login is a row like any other; being
//    native is a CAPTION on its metadata line, never a different layout, never
//    extra chrome. Rotation treats every connected account of a family the
//    same, and the UI now says so by looking the same.
//  * TWO LINES PER ROW. Line 1 is the one primary thing (the account) plus its
//    status; line 2 is muted metadata in human words — "38% used · resets in
//    2h", never a raw ISO instant.
//  * REMOVAL goes through the ENGINE's own contract (DELETE
//    /api/claudexor/credential-profiles/…). A native CLI login has no removal
//    button: that account belongs to the vendor's CLI, and a simulated
//    sign-out would claim an effect this app cannot have.
//
// The status payload comes from the SHARED store (`claudexor_status_store.js`)
// — this section owns no poll — and the login card is the SHARED controller
// (`harness_login_cards.js`), so the onboarding wizard mounts the same flow.
//
// PROVENANCE IS PER FACET (the store's rule) and it OUTRANKS the aggregate:
// `daemon.state` can say `unreachable` while two of the three fanned-out reads
// landed, so no sentence here judges the daemon off the aggregate alone —
// `daemonAnswered` below is the one predicate, and the status line asks the
// facets before it repeats any runtime claim ("keeps running", "ready").
//
// Pure helpers up top are node-tested without a DOM.

import { apiFetch } from './api_client.js';
import {
    FACET_ACCOUNTS,
    FACET_CATALOG,
    FACET_QUOTA,
    FACET_SUBJECT,
    READ_FAILED,
    READ_INDETERMINATE,
    READ_OK,
    READ_TRANSPORT,
    READ_UNREAD,
    STATUS_FACETS,
    accountRows,
    bindStatusSurface,
    claudexorStatus,
    facetReadState,
    familyLabel,
    readsFor,
} from './claudexor_status_store.js';
import { openConfirmDialog } from './confirm_dialog.js';
import { createLoginCardController } from './harness_login_cards.js';
import { formatRelativeAge } from './ui_helpers.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';

// ---------------------------------------------------------------------------
// Pure helpers.
// ---------------------------------------------------------------------------

export function verificationBadge(profile, { known = true } = {}) {
    // Q2-а: both statuses are shown honestly — vendor-verified is trusted,
    // local-store presence stays labeled "not verified live" in WORDS, but in
    // a NEUTRAL tone (owner finding #2): the engine has no vendor probe for
    // some harnesses (cursor), so a warning-toned "not verified" there is a
    // permanent alarm nothing can clear — noise, not honesty. "local session"
    // is the daemon's own name for the route (next_up.route).
    //
    // The instant moved OUT of this label (owner finding: a row must not lead
    // with a raw ISO timestamp) — `accountMetaLine` humanizes it below.
    const status = profile?.status || profile || {};
    const source = String(status.verification_source || '');
    const verification = String(status.verification || '');
    const badge = () => {
        if (source === 'vendor' && verification === 'passed') {
            return { tone: 'ok', label: 'Verified live' };
        }
        if (verification === 'passed') {
            // The claim is NARROWER than "signed in" and must stay narrower in
            // WORDS: local-store material has read `passed` a minute before a 401.
            return { tone: 'muted', label: 'Signed in — not verified live' };
        }
        if (verification) {
            return { tone: 'error', label: `Verification ${verification}` };
        }
        return { tone: 'muted', label: 'Not signed in' };
    };
    const value = badge();
    // `known` = the ACCOUNTS facet was really read. Otherwise this row is the
    // retained snapshot's memory of an account, and painting a green "Verified
    // live" over a read that never landed is the same lie as the banner's — the
    // panel used to say nothing could be listed while a stale row sat below it
    // dressed as verified. The row survives (it is the only Connect affordance
    // some harnesses have); only its claim is dated, and the green goes with it.
    if (known) return value;
    return { tone: 'muted', label: `${value.label} — last known` };
}

export function humanizeResetAt(resetsAt, nowMs = Date.now()) {
    // "resets in 2h", not "resets 2026-08-09T21:04:00Z". Absence is absence.
    const at = Date.parse(String(resetsAt || ''));
    if (!Number.isFinite(at)) return '';
    const minutes = Math.round((at - nowMs) / 60000);
    if (minutes <= 1) return 'in a moment';
    if (minutes < 60) return `in ${minutes}m`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `in ${hours}h`;
    return `in ${Math.round(hours / 24)}d`;
}

export function quotaSummary(snapshots, harnessId, subjectId = '',
                             { quotaRead = READ_OK, nowMs = Date.now() } = {}) {
    // The exhausted window is SHOWN with its reset time, never hidden (Q2-б):
    // hiding it would make the D28 fallback to API money unexplainable. What
    // CHANGED is only the wording — the owner asked for the limit text to be
    // compact and understandable, so "window exhausted — resets
    // 2026-08-09T21:04:00Z" became "Limit reached · resets in 2h".
    //
    // `quotaRead` is the QUOTA facet's own provenance. A refused quota read is
    // not a zero and not a full window: it licenses no usage claim at all,
    // while the catalogue and account facets beside it stay authoritative.
    if (quotaRead !== READ_OK) {
        return { label: 'Limits not checked', exhausted: false, resetsAt: '', tone: 'muted' };
    }
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
    const resetsAt = exhausted ? (exhaustedResetsAt || worst?.resetsAt || '') : (worst?.resetsAt || '');
    const resets = humanizeResetAt(resetsAt, nowMs);
    let base = '';
    if (exhausted) base = `Limit reached${resets ? ` · resets ${resets}` : ''}`;
    else if (worst) {
        base = `${Math.min(100, Math.round(worst.used * 100))}% used${resets ? ` · resets ${resets}` : ''}`;
    }
    // Read, and nothing to report about THIS account: say the usage is
    // unavailable rather than implying an empty window.
    if (!base && !note) return { label: 'Usage unavailable', exhausted: false, resetsAt: '', tone: 'muted' };
    return {
        exhausted,
        resetsAt,
        tone: exhausted ? 'warn' : 'muted',
        label: [base, note].filter(Boolean).join(' · '),
    };
}

export function normalizeProfileName(raw) {
    // The profile-id alphabet the login request accepts: lowercased, and every
    // character outside it becomes '-'.
    return String(raw || '').trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-');
}

export async function promptProfileName({ dialogImpl = openConfirmDialog, family = '' } = {}) {
    // pywebview's WKWebView implements no window.prompt — it answers null
    // silently, so the old prompt()-based Add-account flow was a dead button on
    // the desktop app. The in-house input dialog asks instead, and it loops
    // until the typed name already IS its normalized form: a name that
    // normalization would change ("Работа" → "------", "Work" → "work") is
    // shown back, editable, BEFORE any login starts — never rewritten silently.
    let initialValue = '';
    let body = `Name for the additional ${family || 'agent'} account (e.g. work, backup).`
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

// The facets the status contract declares, restated here for this module's
// own iteration order. The AUTHORITY is the store's `STATUS_FACETS` — that is
// the literal tests/test_gateway_parity.py greps and compares with
// `ClaudexorStatusReads` — and a node pin welds this spelling to the store's,
// so the two cannot drift and this list inherits the contract transitively.
export const READ_FACETS = ['catalog', 'accounts', 'quota'];

export function unreadFacets(payload) {
    // Which facets did NOT answer, in contract order. Empty means everything
    // this payload promises was actually read. Derived through the STORE's one
    // reader — a second parse of the `reads` block here is exactly the
    // two-readers-disagreeing bug the store was extracted to end.
    return STATUS_FACETS.filter((facet) => facetReadState(payload, facet) !== READ_OK);
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
    if (String(payload?.daemon?.state || '') === 'running') return true;
    return STATUS_FACETS.some((facet) => facetReadState(payload, facet) === READ_OK);
}

// The Refresh button's honest label. It only ever RE-READS while the daemon is
// alive, but with a sleeping daemon a plain re-read returns the same nothing
// forever — so there it becomes an explicit owner action that STARTS the
// daemon, and the label says so rather than hiding the side effect. ONE
// predicate behind BOTH the label and the click, so they cannot drift apart
// again (they were written separately once, and did).
export function refreshActionKind(payload) {
    return daemonAnswered(payload) ? 'refresh' : 'wake';
}

export function refreshActionLabel(payload) {
    return refreshActionKind(payload) === 'refresh'
        ? 'Refresh'
        : 'Check accounts (starts the agent daemon)';
}

const capitalize = (text) => (text ? `${text.charAt(0).toUpperCase()}${text.slice(1)}` : text);

function facetGapNames(reads) {
    // The unread facets a sentence may NAME, in the store's own subjects.
    // `indeterminate` is excluded on the store's rule — it is not a verdict
    // ABOUT a facet, so naming subjects under it would invent the per-facet
    // accusation the coarse state exists to avoid — and `unread` is just this
    // client's first read still in flight.
    const named = STATUS_FACETS
        .filter((facet) => reads[facet] !== READ_OK
            && reads[facet] !== READ_INDETERMINATE && reads[facet] !== READ_UNREAD)
        .map((facet) => FACET_SUBJECT[facet] || facet);
    return joinSubjects(named);
}

export function daemonStatusLine(payload, { checking = false, reads = null } = {}) {
    const daemon = payload?.daemon || {};
    const runtime = daemon.runtime || {};
    const runtimeState = String(runtime.state || '');
    const status = String(daemon.state || 'unknown');
    // Nothing read yet and a read in flight: SAY so, and say what it costs. The
    // daemon re-probes every agent CLI on each read, so first paint is tens of
    // seconds — and an unexplained silent panel reads as "broken", not as
    // "loading" (owner report, 2026-08-08).
    if (checking && !daemon.state) {
        return { tone: 'muted', text: 'Checking Claudexor… the first read probes each agent CLI and can take a minute or more.' };
    }
    if (daemon.ownership_problem) {
        return { tone: 'error', text: `This daemon home is not managed from here: ${daemon.ownership_problem}` };
    }
    // A facet can fail WITHOUT the aggregate hearing about it: an envelope that
    // arrived in the wrong shape is a failed read, not an exception, so the
    // daemon still reports `running`. The panel then said "Claudexor ready" in
    // green while the row underneath said the accounts were not checked — one
    // screen, two contradictory claims, the reassuring one on top. The status
    // line asks the FACETS, not the aggregate, and it does so BEFORE the
    // runtime branches, which used to return above the facet logic and hide the
    // gaps entirely.
    const facetReads = reads || readsFor(payload);
    const unread = STATUS_FACETS.filter((facet) => facetReads[facet] !== READ_OK);
    const gapNames = facetGapNames(facetReads);
    const unreadTail = gapNames ? ` ${capitalize(gapNames)} were not read.` : '';
    if (runtimeState === 'installing') {
        const version = runtime.target_version ? ` ${runtime.target_version}` : '';
        return { tone: 'muted', text: `Installing or checking Claudexor${version}…${unreadTail}` };
    }
    if (runtimeState === 'error') {
        const detail = runtime.last_error ? `: ${runtime.last_error}.` : '.';
        return { tone: 'error', text: `Claudexor needs repair${detail} Connect retries automatically.${unreadTail}` };
    }
    // The staged-update line asserts the current engine is still SERVING. Only
    // say that when this reading actually saw it serve; otherwise the facets
    // own the line and the staged update is a footnote — "Engine X keeps
    // running" was a positive claim about a daemon that, in that window,
    // answered nothing, printed over a button offering to START it.
    if (runtimeState === 'update_staged' && !unread.length) {
        const target = runtime.staged_version || runtime.target_version || '?';
        const current = daemon.engine_version || runtime.version || '?';
        return { tone: 'warn', text: `Claudexor ${target} is ready and will activate after the daemon next restarts. Engine ${current} keeps running until then.` };
    }
    if (runtimeState === 'update_staged') {
        const target = runtime.staged_version || runtime.target_version || '?';
        const gap = gapNames ? `${capitalize(gapNames)} were not read` : 'The status answer did not complete';
        return { tone: 'warn', text: `${gap}${daemon.last_error ? `: ${daemon.last_error}` : ''}. Claudexor ${target} is staged and activates after the daemon next restarts.` };
    }
    if (status === 'running') {
        // A REAL refusal (a read that was made and did not land) demotes the
        // green line: "ready" would be an overclaim about the facets. A gap
        // that is merely `not_read` keeps the ready line — the daemon itself
        // is proven up, and the tab's banner note (the store's own sentence)
        // explains a read nobody made.
        const refused = unread.filter((facet) =>
            facetReads[facet] === READ_FAILED || facetReads[facet] === READ_TRANSPORT);
        if (refused.length) {
            const names = joinSubjects(refused.map((facet) => FACET_SUBJECT[facet] || facet));
            return { tone: 'warn', text: `Claudexor is running, but ${names} were not read${daemon.last_error ? `: ${daemon.last_error}` : ''}. What those cover is unknown.` };
        }
        return { tone: 'ok', text: `Claudexor ready (engine ${daemon.engine_version || '?'}) · home ${payload.config_dir || ''}` };
    }
    if (status === 'not_provisioned') {
        if (runtimeState === 'ready') {
            const version = runtime.version ? ` ${runtime.version}` : '';
            return { tone: 'muted', explainsUnread: true, text: `Claudexor${version} is ready. Connect an account to start Ouroboros’s own agent daemon.` };
        }
        return { tone: 'muted', explainsUnread: true, text: 'No accounts connected yet. Connect installs Claudexor and starts Ouroboros’s own agent daemon automatically.' };
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
        return { tone: 'muted', explainsUnread: true, text: `Claudexor${version} is installed; the agent daemon is not running. It starts automatically on the next login or delegated run.` };
    }
    if (status === 'foreign_daemon') {
        return { tone: 'warn', text: 'Another daemon answered on the stale port (not ours — left untouched). The next login restarts our own daemon on a fresh port.' };
    }
    if (daemonAnswered(payload) && gapNames) {
        // A PARTIAL refusal: the aggregate says `unreachable` because one read
        // died, but the others landed and their rows are on screen right now.
        // Announcing a dead daemon above accounts it just handed over is the
        // same false verdict as an unread store rendered empty. NAME the facets
        // that did not answer — "what is shown below was read" was itself an
        // overclaim when the accounts facet is the one that failed.
        return { tone: 'warn', text: `${capitalize(gapNames)} were not read${daemon.last_error ? `: ${daemon.last_error}` : ''}. What those cover is unknown.` };
    }
    return { tone: 'error', text: `Daemon ${status}${daemon.last_error ? `: ${daemon.last_error}` : ''}` };
}

// The agent families a fresh install can connect BEFORE the daemon exists.
// Discovery needs a running daemon, and on first run there is none — so with
// nothing discovered the UI still offers a Connect affordance, and the first
// Connect is exactly what provisions the owned daemon (D30). Presentation
// only; the login flow itself stays harness-agnostic.
export const BOOTSTRAP_HARNESSES = ['codex', 'claude', 'cursor'];

// The display name comes from the store, which owns the payload it reads and is
// imported by BOTH this tab and the onboarding wizard. Re-exported so this
// module keeps its established import path.
export { familyLabel };

// Re-exported so the accounts surface keeps ONE import path for the payload
// projection it renders (the definition lives with the store that owns the
// payload).
export { accountRows };

export function bareRowStatusText(accountsRead) {
    // The verdict for a family with NO row. "no account connected" is a claim
    // about the ACCOUNT STORE, and it may only be made once that store was
    // actually read: an idle daemon is never asked, so the emptiness says
    // nothing (BIBLE P1 — a gap is not a zero). The Connect button stays in
    // every case; onboarding must remain reachable.
    if (accountsRead === READ_OK) return 'No account connected';
    if (accountsRead === READ_UNREAD) return 'Checking…';
    if (accountsRead === READ_TRANSPORT) return 'Not checked — the status request did not complete';
    if (accountsRead === READ_FAILED) return 'Not checked — the daemon did not answer this read';
    // The coarse state: the answer did not complete, and it does not say which
    // read was the one that failed — so the row claims nothing beyond that.
    // Without this branch a legacy payload's global refusal fell through to
    // "never asked", which is the opposite of what happened.
    if (accountsRead === READ_INDETERMINATE) return 'Not checked — the status answer did not complete';
    // NOT READ says nobody asked; it does NOT say why. "the agent daemon is not
    // running" named a cause this row cannot see — a runtime awaiting repair, a
    // foreign daemon on the stale port and an ownership problem all arrive here
    // as the same unread facet, and the tab's ONE banner is the place that
    // explains which of them it is.
    return 'Not checked — the agent daemon was never asked';
}

export function familyStatus(rows, { accountsRead = READ_OK } = {}) {
    // The aggregate lozenge in a card header. Every connected account of a
    // family is equivalent, so the header counts them and says they rotate —
    // it never singles one out as the real one.
    if (!rows.length) return { tone: 'muted', label: bareRowStatusText(accountsRead) };
    // …and the SAME provenance rule the rows obey. These rows are the retained
    // snapshot's memory when the accounts facet did not land, so a green
    // "Connected" over them is the row badge's lie one level up — and the two
    // would contradict each other inside one card, the header claiming fresh
    // while the badge under it says last known.
    const known = accountsRead === READ_OK;
    const verdict = (tone, label) => (known
        ? { tone, label }
        : { tone: tone === 'error' ? 'error' : 'muted', label: `${label} — last known` });
    const bad = rows.filter((row) => verificationBadge(row).tone === 'error').length;
    if (bad) {
        return verdict('error', `${bad} of ${rows.length} need attention`);
    }
    const live = rows.filter((row) => String(row?.status?.verification || '') === 'passed').length;
    if (!live) return verdict('muted', `${rows.length} account${rows.length === 1 ? '' : 's'} · not signed in`);
    // "N accounts · rotating" is a claim about what ROTATION will use, so it may
    // only count the accounts that are actually signed in. A family with one
    // live account and one cold row says exactly that instead of promising two.
    if (live < rows.length) return verdict('ok', `${live} of ${rows.length} connected`);
    if (live === 1) return verdict('ok', 'Connected');
    return verdict('ok', `${live} accounts · rotating`);
}

export function accountName(row) {
    if (row.kind === 'native') return 'Default CLI login';
    return String(row.display_name || '') || String(row.profile_id || '') || 'Account';
}

export function accountMetaLine(row, payload, { quotaRead = READ_OK, nowMs = Date.now() } = {}) {
    // Line 2: everything that is NOT the account itself, in human words and at
    // muted ink. Order is the owner's — how much of the window is left, which
    // plan, who it is, when we last checked.
    const parts = [];
    if (row.kind === 'native') {
        parts.push(`Managed by the ${familyLabel(row.harness, payload)} CLI`);
    }
    parts.push(quotaSummary(payload?.quota || [], row.harness, row.profile_id,
        { quotaRead, nowMs }).label);
    const identity = row.identity || {};
    if (identity.plan) parts.push(String(identity.plan));
    if (identity.email) parts.push(String(identity.email));
    const at = Date.parse(String(row?.status?.last_verified_at || ''));
    if (Number.isFinite(at)) {
        const age = formatRelativeAge(at, 'just now');
        if (age) parts.push(`checked ${age}`);
    }
    return parts.filter(Boolean).join(' · ');
}

export function accountGroups(payload, { accountsRead = READ_OK } = {}) {
    // One group per family, in a stable order: discovered families first (the
    // engine's own order), then any bootstrap family still missing, so a fresh
    // install shows all three cards and every one of them can be connected.
    const rows = accountRows(payload);
    const order = [];
    for (const harness of payload?.harnesses || []) {
        const id = String(harness?.id || '');
        if (id && !order.includes(id)) order.push(id);
    }
    for (const row of rows) if (!order.includes(row.harness)) order.push(row.harness);
    for (const id of BOOTSTRAP_HARNESSES) if (!order.includes(id)) order.push(id);
    return order.map((id) => {
        const own = rows.filter((row) => row.harness === id);
        return {
            harness: id,
            label: familyLabel(id, payload),
            rows: own,
            status: familyStatus(own, { accountsRead }),
        };
    });
}

export function familyActionLabel(group, payload) {
    // The card's OWN button, and the fix for "позиции кнопок нелогичные": the
    // add affordance lives in the family header instead of hanging off one
    // privileged row. An empty family connects its default CLI login first
    // (carrying the runtime's install/repair intent); once a family has any
    // account, the button adds a NAMED one — which is what makes the accounts
    // equivalent instead of one-default-plus-extras.
    //
    // DISCLOSED RESIDUAL (adversarial review, 2026-08-09): unlike `rowActionLabel`
    // this deliberately does NOT hand the label to a runtime that needs
    // installing or repairing. The button's own action is to ASK FOR A NAME and
    // then start a login — a header reading "Fix & connect" that opens a
    // name-the-account dialog would misdescribe what the click does, and
    // dropping the name step would remove the add intent this card exists for.
    // The repair is a PREREQUISITE, not the destination: the login card
    // performs it in the foreground and reports it there, and the tab's service
    // banner already names the fault above.
    return group.rows.length ? 'Add account' : runtimeActionLabel(payload);
}

export function rowActionLabel(row, payload) {
    // A runtime that needs installing, repairing or updating owns the label —
    // that work happens first whatever the row wants. Otherwise the row says
    // what it is really offering: an account that HAS a session signs in again,
    // one that does not simply signs in. ("Connect" belongs to a family with no
    // account yet, where it is the first step rather than a repeat.)
    const runtime = runtimeActionLabel(payload);
    if (runtime !== 'Connect') return runtime;
    return String(row?.status?.verification || '') === 'passed' ? 'Sign in again' : 'Sign in';
}

// "agents", "agents and limits", "agents, accounts and limits".
function joinSubjects(names) {
    const list = names.filter(Boolean);
    if (list.length <= 1) return list[0] || '';
    return `${list.slice(0, -1).join(', ')} and ${list[list.length - 1]}`;
}

const TONE_RANK = { ok: 0, muted: 0, warn: 1, error: 2 };

// A note whose only content is "we did not check" yields to a service line that
// EXPLAINS why nothing was read. A warn/error note reports a real read failure
// and keeps its place, because the service line cannot know which read died.
const GENERIC_FACET_NOTE_YIELDS = new Set(['muted']);

function faultOutranksReassurance(service, note) {
    // A MUTED note is a reassurance: "nothing below is missing or wrong". It
    // may not be the last word while the service line has a FAULT to report.
    // Every settled non-running state — runtime `error`, `foreign_daemon`, an
    // ownership problem, a recorded daemon `last_error` — leaves all three
    // facets unread, so the benign note used to be the ONLY sentence the owner
    // saw while the row buttons beside it offered "Fix & connect". The whole
    // error/warn vocabulary daemonStatusLine already speaks was unreachable
    // there. A warn/error note (a refused read, a dead request) is itself a
    // report and keeps its place.
    // Precedence is by SPECIFICITY, not by tone. A muted service line can still
    // be the more informative sentence: on a first run "No accounts connected
    // yet. Connect installs Claudexor…" is exactly what the owner needs, and it
    // was unreachable while only warn/error could win — every stopped state
    // leaves all three facets unread, so the generic note always spoke instead.
    // The generic note explains nothing the service line does not; it is the
    // FALLBACK for when the service line has nothing concrete to say.
    if (!note) return service;
    const serviceSpeaksFirst = Boolean(service) && (
        service.tone === 'error' || service.tone === 'warn' || service.explainsUnread === true
    );
    if (GENERIC_FACET_NOTE_YIELDS.has(note.tone) && serviceSpeaksFirst) {
        return { tone: service.tone, text: service.text };
    }
    return { tone: note.tone, text: note.text };
}

export function serviceBannerLine(store, { wakeError = '' } = {}) {
    // THE service banner: one place on the tab that explains a daemon/runtime
    // problem, replacing the scattering of "(not in discovery)" the owner
    // reported. Provenance is PER FACET, so this line never collapses three
    // independent reads into one verdict where the wire tells it apart: a
    // refused quota read leaves the catalogue and accounts authoritative and
    // says exactly that. The producer stamps `reads` on every answer
    // (`claudexor_accounts.py`), so this is the shape the line renders live;
    // the coarse all-indeterminate rendering remains only for a legacy payload
    // without the block.
    //
    // Deliberately NOT built on the store's `facetGapClause`, which exists for a
    // surface that LEADS with one facet and must still name the others (the
    // Delegation note does exactly that for its model select). This line leads
    // with none: it enumerates every facet it lost, in that facet's own state,
    // so the shared clause would add a second "could not be read" about facets
    // the sentence above it has already named. Same authority, one phrasing.
    //
    // A WAKE ERROR leads outright: the owner PRESSED the button and it did not
    // work — silence there is the same class of dishonesty this banner exists
    // to remove (a typed 503 from a missing binary or foreign home, a 404 from
    // an older backend, a dead network). The rows and Connect stay put; the
    // error's LIFECYCLE is the panel's (it expires only when the daemon
    // provably answers — `daemonAnswered`, deliberately not the literal
    // `running`).
    if (wakeError) {
        return { tone: 'error', text: `Could not start the agent daemon: ${wakeError}` };
    }
    const reads = store.reads || {};
    const facets = [FACET_CATALOG, FACET_ACCOUNTS, FACET_QUOTA];
    const bad = facets.filter((facet) => reads[facet] !== READ_OK);
    if (!bad.length) {
        return daemonStatusLine(store.snapshot || {}, {
            checking: store.loading && !store.everSettled,
            reads,
        });
    }
    // NOTHING READ YET is not a gap to report — it is the first read in flight,
    // and what the owner needs then is its COST: the daemon re-probes every
    // agent CLI, so first paint is tens of seconds and a silent panel reads as
    // broken rather than as loading (owner report, 2026-08-08). A bare
    // "Reading…" would have thrown that sentence away.
    if (!store.everSettled) {
        return daemonStatusLine(store.snapshot || {}, { checking: store.loading, reads });
    }
    // The service line asks the FACETS too (invariant: facets outrank both the
    // aggregate and the runtime branches), so when it wins below it already
    // names the unread facets and gates its own positive claims ("keeps
    // running", "ready") on a reading that actually saw them.
    const service = daemonStatusLine(store.snapshot || {}, { reads });
    // All three unread in the same way: ONE sentence about the service, from
    // the shared vocabulary, with the subject widened to the whole tab —
    // naming just the accounts would under-report a gap that also swallowed
    // the agent catalogue and the limits. A runtime fault outranks it.
    //
    // The sentence is asked of the STORE, never assembled here, because the
    // detail beside it is the store's to resolve: a transport error when the
    // request itself died, and otherwise — for a read that was made and did not
    // land — the daemon's OWN `last_error`. That string is the only explanation
    // an `unreachable` answer carries, and a banner that called the copy factory
    // directly printed "could not be read" and dropped it.
    const states = new Set(bad.map((facet) => reads[facet]));
    if (bad.length === 3 && states.size === 1) {
        return faultOutranksReassurance(service,
            store.unavailableNote(bad[0], { subject: 'agents, accounts and limits' }));
    }
    // A PARTIAL gap: name EVERY facet that could not be read — one sentence per
    // distinct way they failed — and let the closing reassurance cover only the
    // facets that genuinely read. Reporting `bad[0]` alone and appending
    // "everything else was read normally" told the owner two of three failures
    // had landed fine; the backend stamps `reads` per facet on every answer,
    // which is exactly what makes a mixed verdict possible.
    const sentences = [];
    let tone = 'muted';
    for (const readState of states) {
        const group = bad.filter((facet) => reads[facet] === readState);
        const subjects = group.map((facet) => FACET_SUBJECT[facet] || facet);
        // Any facet of the group answers for it — they share the read state,
        // and asking the store keeps the daemon's own reason attached.
        const note = store.unavailableNote(group[0], { subject: joinSubjects(subjects) });
        if (!note) continue;
        sentences.push(note.text);
        if (TONE_RANK[note.tone] > TONE_RANK[tone]) tone = note.tone;
    }
    if (!sentences.length) return service;
    const readOk = facets
        .filter((facet) => reads[facet] === READ_OK)
        .map((facet) => FACET_SUBJECT[facet] || facet);
    const tail = readOk.length ? ` Your ${joinSubjects(readOk)} were read normally.` : '';
    // The SAME precedence as the full-gap branch above — the two are one
    // decision, and fixing only one half of it is how this class survives. The
    // backend stamps `reads` per facet on every answer, so a mixed verdict is
    // an ordinary state — and a muted "some facets were never asked · the rest
    // read normally" must not swallow a runtime that needs repair.
    return faultOutranksReassurance(service, { tone, text: `${sentences.join(' ')}${tail}` });
}

export async function removeAccount(harness, profileId, { fetchImpl = apiFetch } = {}) {
    // The engine owns the account record, so removal is ITS contract. A failure
    // is reported as a failure — nothing here pretends an account is gone.
    const url = `/api/claudexor/credential-profiles/${encodeURIComponent(harness)}`
        + `/${encodeURIComponent(profileId)}`;
    const resp = await fetchImpl(url, { method: 'DELETE' });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(String(data?.error || `HTTP ${resp.status}`));
    return data;
}

export function removeAccountConfirmBody(name, family) {
    return `${family} will forget the account "${name}". Ouroboros deletes nothing on `
        + `the ${family} side — sign in again any time to bring it back. Reviewer rows `
        + 'pinned to this account stay visible and are shown as unavailable until you '
        + 'repoint them.';
}

// ---------------------------------------------------------------------------
// DOM section.
// ---------------------------------------------------------------------------

const state = {
    store: claudexorStatus,
    loginCard: null,
    disposers: [],
    removeError: '',
    initialized: false,
    // The owner asked to start the daemon and the wake refused. Rendered by the
    // service banner and expired ONLY when the daemon provably answers
    // (`daemonAnswered` over a fresh reading) — a 200 that reports the daemon
    // still down must NOT wipe the reason before the owner can read it, and a
    // daemon that came up on its own must not leave the error standing over
    // its accounts. The wake POST itself is the STORE's (single writer).
    wakeError: '',
    wakeBusy: false,
};

/** The tab's ONE service banner; rendered above every section. */
export function renderAgentsServiceBanner() {
    return `<div id="agents-service-banner" class="agents-service-banner settings-inline-status" data-tone="muted">Checking the agent service…</div>`;
}

export function renderAgentAccountsSection() {
    return `
        <div class="form-section" id="harness-accounts-section">
            <h3>Accounts</h3>
            <div class="settings-section-copy">
                Agent subscriptions (Claude Code, Codex, Cursor) used by delegated subagents and
                review lanes. Every account of a family is equivalent — work rotates across all of
                them. Accounts live in Ouroboros's own agent home; your personal logins are never
                read or imported.
            </div>
            <div id="harness-accounts-error" class="settings-inline-status" data-tone="error" hidden></div>
            <div id="harness-accounts-groups" class="agent-family-list"></div>
            <div id="harness-login-card"></div>
            <div class="settings-toolbar">
                <button type="button" class="settings-ghost-btn" id="btn-harness-refresh">Refresh</button>
            </div>
        </div>
    `;
}

export function accountRowFacts(row, payload,
                                { accountsRead = READ_OK, quotaRead = READ_OK, nowMs = Date.now() } = {}) {
    // Each projection is gated by ITS OWN facet: the identity claim is the
    // ACCOUNTS read, the window is the QUOTA read, and one lands while the
    // other refuses. The panel used to render both off the retained snapshot
    // regardless, so after a refused read the banner said nothing could be
    // listed while a stale row sat underneath it showing "Verified live" and a
    // red exhausted window. Pure, because that rule is the thing worth pinning.
    //
    // The two-line anatomy is the owner's (D-10): line 1 is the account and its
    // status, line 2 is muted metadata in human words. `quotaSummary` carries
    // the quota gap itself — an unread window says "Limits not checked" rather
    // than dressing a remembered percentage as current, and it never paints the
    // row red, because the exhausted styling is a claim about RIGHT NOW and the
    // reset it promises may already have happened.
    return {
        badge: verificationBadge(row, { known: accountsRead === READ_OK }),
        quota: quotaSummary(payload?.quota || [], row.harness, row.profile_id, { quotaRead, nowMs }),
        name: accountName(row),
        meta: accountMetaLine(row, payload, { quotaRead, nowMs }),
    };
}

function rowHtml(row, payload, facets = {}) {
    const { badge, quota, name, meta } = accountRowFacts(row, payload, facets);
    return `
        <div class="harness-account-row${quota.exhausted ? ' harness-exhausted' : ''}" data-harness="${escapeHtml(row.harness)}" data-profile="${escapeHtml(row.profile_id)}" data-kind="${escapeHtml(row.kind)}">
            <div class="harness-account-main">
                <strong>${escapeHtml(name)}</strong>
                <span class="ui-status" data-tone="${badge.tone}">${escapeHtml(badge.label)}</span>
            </div>
            <div class="harness-account-meta muted">${escapeHtml(meta)}</div>
            <div class="harness-account-actions">
                <button type="button" class="settings-ghost-btn" data-harness-login>${escapeHtml(rowActionLabel(row, payload))}</button>
                ${row.kind === 'native' ? '' : '<button type="button" class="settings-ghost-btn" data-harness-remove title="Ask the agent service to forget this account">Remove</button>'}
            </div>
        </div>
    `;
}

function groupHtml(group, payload, facets) {
    // An empty family is a ONE-LINE card: the header already carries the verdict
    // (familyStatus falls through to it), and printing the same sentence again
    // in the body just made the card twice as tall to say nothing new.
    const body = group.rows.map((row) => rowHtml(row, payload, facets)).join('');
    return `
        <section class="agent-family-card" data-family="${escapeHtml(group.harness)}">
            <div class="agent-family-head">
                <div class="agent-family-id">
                    <h4>${escapeHtml(group.label)}</h4>
                    <span class="ui-status" data-tone="${group.status.tone}">${escapeHtml(group.status.label)}</span>
                </div>
                <button type="button" class="settings-ghost-btn" data-family-add>${escapeHtml(familyActionLabel(group, payload))}</button>
            </div>
            <div class="agent-family-rows">${body}</div>
        </section>
    `;
}

function renderRows() {
    // The wake error expires HERE, on the one condition that makes it moot: a
    // FRESH reading (store error retired) in which the daemon provably
    // answered. A refusal was only ever news while nothing answered; keeping
    // it over accounts the daemon just handed over would be the stale-error
    // twin of the stale-absence lie.
    if (state.wakeError && !state.store.error && daemonAnswered(state.store.snapshot)) {
        state.wakeError = '';
    }
    const host = document.getElementById('harness-accounts-groups');
    const banner = document.getElementById('agents-service-banner');
    if (banner) {
        const line = serviceBannerLine(state.store, { wakeError: state.wakeError });
        banner.textContent = line.text;
        banner.dataset.tone = line.tone;
    }
    // The Refresh button says what pressing it does (one predicate feeds the
    // label AND the click — see initHarnessAccounts), and while a wake is in
    // flight it says that instead of inviting a second one.
    const refreshEl = document.getElementById('btn-harness-refresh');
    if (refreshEl) {
        refreshEl.textContent = state.wakeBusy
            ? 'Starting the agent daemon…'
            : refreshActionLabel(state.store.snapshot);
        refreshEl.disabled = Boolean(state.wakeBusy);
    }
    if (!host) return;
    const errorBox = document.getElementById('harness-accounts-error');
    if (errorBox) {
        errorBox.hidden = !state.removeError;
        errorBox.textContent = state.removeError;
    }
    const payload = state.store.snapshot || {};
    const accountsRead = state.store.facet(FACET_ACCOUNTS);
    const quotaRead = state.store.facet(FACET_QUOTA);
    host.innerHTML = accountGroups(payload, { accountsRead })
        .map((group) => groupHtml(group, payload, { accountsRead, quotaRead })).join('');
    host.querySelectorAll('[data-harness-login]').forEach((button) => {
        button.addEventListener('click', () => {
            if (!state.initialized) return;
            const row = button.closest('[data-harness]');
            startLogin(row?.dataset.harness, row?.dataset.profile);
        });
    });
    host.querySelectorAll('[data-harness-remove]').forEach((button) => {
        button.addEventListener('click', () => {
            const row = button.closest('[data-harness]');
            confirmRemoveAccount(row?.dataset.harness, row?.dataset.profile);
        });
    });
    host.querySelectorAll('[data-family-add]').forEach((button) => {
        button.addEventListener('click', async () => {
            if (!state.initialized) return;
            // Captured before the await: the status poll replaces the cards
            // while the dialog is open, detaching this button's section.
            const card = button.closest('[data-family]');
            const harness = card?.dataset.family;
            const hasRows = Boolean(card?.querySelector('[data-harness]'));
            if (!hasRows) { startLogin(harness, ''); return; }
            const profile = await promptProfileName({ family: familyLabel(harness, state.store.snapshot || {}) });
            if (profile) startLogin(harness, profile);
        });
    });
    state.loginCard?.render();
}

async function confirmRemoveAccount(harness, profileId) {
    if (!harness || !profileId) return;
    const family = familyLabel(harness, state.store.snapshot || {});
    const answer = await openConfirmDialog({
        title: 'Remove account',
        body: removeAccountConfirmBody(profileId, family),
        confirmLabel: 'Remove',
        danger: true,
    });
    if (!answer?.confirmed) return;
    state.removeError = '';
    try {
        await removeAccount(harness, profileId);
    } catch (error) {
        state.removeError = `Could not remove "${profileId}": ${error.message || error}. `
            + 'The account is unchanged.';
    }
    await state.store.refresh();
    renderRows();
}

/**
 * OWNER action behind the Refresh button when the daemon is asleep: start it,
 * then take the fresh reading. The POST and the commit belong to the STORE
 * (single writer — `store.wake()` serializes against the poll in both orders);
 * what lives here is the error's LIFECYCLE: shown only while it still matters
 * (a refusal is not news once the daemon answers — an ordinary poll can commit
 * a live reading while the wake is in flight), and expired only by a daemon
 * that provably answered (see renderRows).
 */
export async function wakeDaemon() {
    if (state.wakeBusy) return;
    state.wakeBusy = true;
    state.wakeError = '';
    renderRows();
    let result;
    try {
        result = await state.store.wake();
    } finally {
        state.wakeBusy = false;
    }
    if (!result?.ok) {
        state.wakeError = daemonAnswered(state.store.snapshot)
            ? ''
            : String(result?.error || 'request failed');
    }
    renderRows();
}

function ensureLoginCard() {
    if (state.loginCard && !state.loginCard.disposed) return state.loginCard;
    // `detach()` permanently fences one controller. Explicit Connect after a
    // destroy/re-init must therefore build a fresh controller instead of
    // reusing a cached disposed object whose start() correctly does nothing.
    state.loginCard = null;
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
    if (!harness || !state.initialized) return;
    await ensureLoginCard().start(harness, profile);
}

/** Read the shared status once (the Refresh button, and the first paint). */
export function refreshHarnessStatus() {
    return state.store.refresh();
}

/**
 * Mount the section. The exported destroy seam is an honest local detach, so
 * remount never waits on or invents daemon release proof.
 */
export function initHarnessAccounts({ store = claudexorStatus } = {}) {
    return _init(store);
}

async function _init(store) {
    _destroy();
    state.store = store;
    state.removeError = '';
    state.wakeError = '';
    state.wakeBusy = false;
    ensureLoginCard();
    document.getElementById('btn-harness-refresh')
        ?.addEventListener('click', () => {
            if (!state.initialized) return;
            // A sleeping daemon cannot be re-read into existence: there the
            // button is the owner's explicit start. Live, it stays a plain
            // re-read. SAME predicate the LABEL uses (renderRows), so the two
            // cannot disagree again.
            return refreshActionKind(state.store.snapshot) === 'refresh'
                ? state.store.refresh()
                : wakeDaemon();
        });
    // The SHARED surface binding: the visibility predicate that lets this
    // section keep the poll armed, and the catch-up read when the panel becomes
    // reachable — one implementation, released by one disposer. It carries no
    // tab NAME on purpose, and this section is the proof: it moved from
    // Providers to Agents in this very sprint, so a hardcoded tab name would
    // have gone quietly dead on arrival while its comment still promised that
    // a daemon coming up is picked up without a reload.
    state.disposers.push(bindStatusSurface(state.store, {
        listener: () => renderRows(),
        elementId: 'harness-accounts-groups',
    }));
    state.initialized = true;
    // The first read must not wait for the poll interval: init runs while the
    // page may not be visible yet, and the panel would sit on "Checking
    // daemon…" until the first tick (#125).
    state.store.refresh();
    renderRows();
    return true;
}

/**
 * Tear the exported Settings test/lifecycle seam down synchronously. This is a
 * local detach only: zero create/cancel/reconcile requests and no claim that a
 * daemon-owned process stopped. Production Settings remains mounted across
 * ordinary SPA navigation and never calls this as a leave hook.
 */
export function destroyHarnessAccounts() {
    return _destroy();
}

function _destroy() {
    for (const dispose of state.disposers.splice(0)) {
        try { dispose(); } catch (err) { /* a broken disposer must not block the rest */ }
    }
    state.initialized = false;
    const card = state.loginCard;
    if (!card) return true;
    card.detach();
    state.loginCard = null;
    return true;
}
