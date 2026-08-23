// The owner's answer to «where should this run» — one editable row per piece of
// work, on the running task's own card.
//
// The route VOCABULARY is imported from reviewer_slots, not restated: an install
// with two spellings of `agent_session` would eventually disagree with itself
// about the same route. What differs here is the CHOICE value. A reviewer row
// keeps the route and the model in separate controls, so its api entry is a
// single `api`; a plan row's api target IS the model, so a choice has to carry
// it. That is why this file encodes `api:<model>` rather than reusing
// reviewer_slots' encodeRouteChoice, and the difference is deliberate.

import { apiFetch } from './api_client.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';
import { ROUTE_KIND_API, ROUTE_KIND_SESSION } from './reviewer_slots.js';

export { ROUTE_KIND_API, ROUTE_KIND_SESSION };

export function encodeTargetChoice(route) {
    const kind = String(route?.kind || '');
    const target = String(route?.target_id || '');
    if (!kind || !target) return '';
    return `${kind}:${target}`;
}

export function decodeTargetChoice(value) {
    const raw = String(value || '');
    const cut = raw.indexOf(':');
    if (cut <= 0) return null;
    const kind = raw.slice(0, cut);
    const target = raw.slice(cut + 1);
    if (!target) return null;
    if (kind !== ROUTE_KIND_API && kind !== ROUTE_KIND_SESSION) return null;
    return { kind, target_id: target };
}

// What the owner is choosing between, in one flat list. Unavailable targets are
// KEPT and marked rather than filtered out: «codex — subscription window spent,
// resets 14:20» is the fact behind the recommendation, and hiding the row would
// leave the owner wondering why their usual choice vanished.
export function targetChoices(catalog) {
    const rows = [
        ...(catalog?.api_chat || []),
        ...(catalog?.agent_session || []),
    ];
    return rows
        .filter((row) => row && row.target_id)
        .map((row) => ({
            value: encodeTargetChoice(row),
            label: String(row.label || row.target_id),
            available: row.available !== false,
            reason: String(row.unavailable_reason || ''),
            kind: String(row.kind || ''),
        }));
}

// The estimate line, or an honest silence. A row whose basis is unknown says so
// — the one thing it must never do is print a confident number nobody measured.
export function estimateLine(estimate) {
    if (!estimate || typeof estimate !== 'object') return '';
    const parts = [];
    const seconds = Number(estimate.duration_sec);
    if (Number.isFinite(seconds) && seconds > 0) {
        parts.push(seconds < 90 ? `~${Math.round(seconds)}s` : `~${Math.round(seconds / 60)}m`);
    }
    const cost = Number(estimate.cost_usd);
    if (Number.isFinite(cost)) parts.push(`$${cost.toFixed(2)}`);
    const basis = String(estimate.basis || '').trim();
    if (basis) parts.push(basis);
    return parts.join(' · ');
}

// The payload the decision endpoint takes. Pure, so what gets POSTed is testable
// without a browser — and so the shape has exactly one author.
export function buildDecisionPayload(proposal, selections) {
    const items = (proposal?.items || []).map((item) => {
        const chosen = decodeTargetChoice(selections?.[item.item_id])
            || {
                kind: String(item?.recommended_route?.kind || ''),
                target_id: String(item?.recommended_route?.target_id || ''),
            };
        const route = { kind: chosen.kind, target_id: chosen.target_id };
        const model = String(item?.recommended_route?.model || '');
        // The proposed harness model survives only while the harness itself does.
        // Carried onto a target the owner switched to, it would pin one engine's
        // model id on another.
        if (model && chosen.kind === ROUTE_KIND_SESSION
            && chosen.target_id === String(item?.recommended_route?.target_id || '')) {
            route.model = model;
        }
        return {
            item_id: String(item.item_id || ''),
            title: String(item.title || ''),
            route,
        };
    });
    return {
        task_id: String(proposal?.task_id || ''),
        plan: {
            version: 1,
            root_task_id: String(proposal?.root_task_id || ''),
            items,
        },
    };
}

function optionsHtml(choices, selected) {
    return choices.map((choice) => {
        const suffix = choice.available ? '' : ` — unavailable (${choice.reason || 'not reachable'})`;
        const disabled = choice.available ? '' : ' disabled';
        const isSelected = choice.value === selected ? ' selected' : '';
        return `<option value="${escapeHtml(choice.value)}"${disabled}${isSelected}>`
            + `${escapeHtml(choice.label + suffix)}</option>`;
    }).join('');
}

function rowHtml(item, choices) {
    const selected = encodeTargetChoice(item.recommended_route);
    const estimate = estimateLine(item.estimate);
    const known = choices.some((choice) => choice.value === selected);
    return `
        <div class="exec-plan-row" data-exec-plan-item="${escapeHtml(item.item_id)}">
            <div class="exec-plan-row-head">
                <span class="exec-plan-row-title">${escapeHtml(item.title || item.item_id)}</span>
                ${estimate ? `<span class="exec-plan-row-estimate">${escapeHtml(estimate)}</span>` : ''}
            </div>
            ${item.why ? `<div class="exec-plan-row-why">${escapeHtml(item.why)}</div>` : ''}
            <select class="exec-plan-select" data-exec-plan-select aria-label="Where ${escapeHtml(item.title || item.item_id)} runs">
                ${known ? '' : `<option value="${escapeHtml(selected)}" selected>${escapeHtml(String(item.recommended_route?.target_id || '') + ' — not in the current catalog')}</option>`}
                ${optionsHtml(choices, selected)}
            </select>
        </div>`;
}

// Renders the card and wires Approve. `submit` receives the decision payload and
// resolves to '' on success or a message to show. The card stays on screen until
// the answer is accepted — a failed POST must not look like an approval.
export function renderExecutionPlanCard(proposal, catalog, submit) {
    const host = document.createElement('div');
    host.className = 'chat-bubble system exec-plan-card';
    host.dataset.execPlanTask = String(proposal?.task_id || '');
    const choices = targetChoices(catalog);
    const items = proposal?.items || [];
    host.innerHTML = `
        <div class="exec-plan-head">
            <span class="exec-plan-title">Where should this run?</span>
            ${proposal?.headline ? `<span class="exec-plan-headline">${escapeHtml(proposal.headline)}</span>` : ''}
        </div>
        <div class="exec-plan-rows">${items.map((item) => rowHtml(item, choices)).join('')}</div>
        <div class="exec-plan-actions">
            <button type="button" class="btn btn-primary btn-sm" data-exec-plan-approve>Approve</button>
            <span class="exec-plan-note" data-exec-plan-note></span>
        </div>`;
    const note = host.querySelector('[data-exec-plan-note]');
    const button = host.querySelector('[data-exec-plan-approve]');
    button?.addEventListener('click', async () => {
        const selections = {};
        for (const row of host.querySelectorAll('[data-exec-plan-item]')) {
            const select = row.querySelector('[data-exec-plan-select]');
            selections[row.dataset.execPlanItem] = select ? select.value : '';
        }
        button.disabled = true;
        if (note) note.textContent = 'Sending…';
        let problem = '';
        try {
            problem = await submit(buildDecisionPayload(proposal, selections));
        } catch (err) {
            problem = String(err?.message || err || 'the decision could not be delivered');
        }
        if (problem) {
            button.disabled = false;
            if (note) note.textContent = problem;
            return;
        }
        host.dataset.execPlanApproved = '1';
        button.remove();
        if (note) note.textContent = 'Approved — the task is scheduling the work.';
    });
    return host;
}


// Proposals already on screen, so a re-delivered frame (a reconnect replaying
// the persisted event) updates nothing instead of stacking a second copy of the
// same unanswered question. Process-local by design: the durable copy is the
// task's own event stream.
const shown = new Set();

// Mount one proposal. Lives HERE rather than in chat.js because the card owns
// its own catalog read, its own submit and its own dedupe — the chat module
// only knows a frame arrived, and hands it over.
export async function mountExecutionPlanProposal(proposal, { insert, stamp }) {
    const taskId = String(proposal?.task_id || '');
    const key = `${taskId}:${String(proposal?.ts || '')}`;
    if (!taskId || shown.has(key)) return;
    shown.add(key);
    const catalog = await apiFetch('/api/execution-targets?include=models', { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : {}))
        .catch(() => ({}));
    const card = renderExecutionPlanCard(proposal, catalog, async (payload) => {
        const resp = await apiFetch('/api/execution-plan/decision', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (resp.ok) return '';
        const body = await resp.json().catch(() => ({}));
        return String(body.error || `the decision was refused (${resp.status})`);
    });
    stamp(card, proposal?.ts || new Date().toISOString());
    insert(card, { forceStick: true });
}
