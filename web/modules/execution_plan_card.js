// The owner's answer to «which agent runs this» — one editable row per piece of
// work, on the running task's own card.
//
// A row names a CONFIGURED SUBAGENT by id, never a route. Which agents exist is
// the owner's standing catalog in Settings (Available subagents); this card only
// decides which of them each piece of THIS task gets. So the dropdown shows the
// row's own name and what it is recommended for — the words the owner already
// wrote — and no route vocabulary is restated here at all.

import { apiFetch } from './api_client.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';

// What the owner is choosing between: the catalog, in its own words. A row's
// `recommended_use` is the owner's own note about when to reach for it, so it
// rides into the option text rather than being summarized away.
export function subagentChoices(catalog) {
    return (catalog?.subagents || [])
        .filter((row) => row && row.subagent_id)
        .map((row) => ({
            value: String(row.subagent_id),
            label: String(row.name || row.subagent_id),
            hint: String(row.recommended_use || ''),
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
    const items = (proposal?.items || []).map((item) => ({
        item_id: String(item.item_id || ''),
        title: String(item.title || ''),
        // An untouched row keeps what was proposed; the select only ever holds
        // a catalog id, so there is nothing to decode and nothing to carry over.
        subagent_id: String(selections?.[item.item_id] || item.subagent_id || ''),
    }));
    return {
        task_id: String(proposal?.task_id || ''),
        plan: {
            version: 2,
            root_task_id: String(proposal?.root_task_id || ''),
            items,
        },
    };
}

function optionsHtml(choices, selected) {
    return choices.map((choice) => {
        const hint = choice.hint ? ` — ${choice.hint}` : '';
        const isSelected = choice.value === selected ? ' selected' : '';
        return `<option value="${escapeHtml(choice.value)}"${isSelected}>`
            + `${escapeHtml(choice.label + hint)}</option>`;
    }).join('');
}

function rowHtml(item, choices) {
    const selected = String(item.subagent_id || '');
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
                ${known ? '' : `<option value="${escapeHtml(selected)}" selected>${escapeHtml(selected + ' — no longer in the catalog')}</option>`}
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
    const choices = subagentChoices(catalog);
    const items = proposal?.items || [];
    host.innerHTML = `
        <div class="exec-plan-head">
            <span class="exec-plan-title">Which agent runs each part?</span>
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
