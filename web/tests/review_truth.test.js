import assert from 'node:assert/strict';
import test from 'node:test';

import {
    executorChip,
    formatReviewProjection,
    summarizeChatLiveEvent,
    taskOutcomeSeverity,
} from '../modules/log_events.js';

test('shared task severity never paints degraded or best-effort outcomes green', () => {
    const clean = {
        outcome_axes: {
            lifecycle: { status: 'completed' }, execution: { status: 'ok' },
            objective: { status: 'pass' }, review: { status: 'pass' },
            artifacts: { status: 'ready' },
        },
    };
    assert.equal(taskOutcomeSeverity(clean), 'done');
    assert.equal(taskOutcomeSeverity({
        ...clean, outcome_axes: { ...clean.outcome_axes, objective: { status: 'best_effort' } },
    }), 'warn');
    assert.equal(taskOutcomeSeverity({
        ...clean, outcome_axes: { ...clean.outcome_axes, objective: { status: 'degraded' } },
    }), 'warn');
    assert.equal(taskOutcomeSeverity({
        ...clean, outcome_axes: { ...clean.outcome_axes, review: { status: 'degraded' } },
    }), 'warn');
    assert.equal(taskOutcomeSeverity({
        ...clean, outcome_axes: { ...clean.outcome_axes, review: { status: 'fail' } },
    }), 'error');
    assert.equal(taskOutcomeSeverity({
        ...clean,
        outcome_axes: {
            ...clean.outcome_axes,
            execution: { status: 'degraded', reason_code: 'review_skipped_deadline_reserve' },
            objective: { status: 'degraded', source: 'task_acceptance_deadline_reserve' },
            review: {
                status: 'skipped', eligibility: 'eligible', run_count: 0,
                // v6.78.0: canonical host status + typed reason (the reason literal
                // is what execution.reason_code / objective.source key off).
                acceptance_decision: {
                    status: 'finalized_unaccepted',
                    reason: 'review_skipped_deadline_reserve',
                },
            },
        },
    }), 'warn');
});

test('review projection exposes panel binding and every actor without raw output', () => {
    const text = formatReviewProjection({ panels: [{
        panel_id: 'panel_abc', surface: 'task_acceptance', authority: 'host_root',
        aggregate_signal: 'DEGRADED', transport_status: 'partial', parse_status: 'malformed',
        quorum: { required: 2, contributed: 1, configured: 3 },
        enforcement_impact: 'degrades_completion', reason: 'one actor malformed',
        candidate_hash: 'candidate', evidence_revision: 'evidence', fence_hash: 'fence-hash',
        actors: [
            {
                slot_id: 'slot_1', actor_role: 'task acceptance', provider: 'anthropic',
                model: 'anthropic/claude-fable-5', transport_status: 'success',
                parse_status: 'valid', semantic_verdict: 'DEGRADED',
                outcome_tier: 'best_effort',
                quorum_contribution: false, enforcement_impact: 'abstains', reason: 'uncertain',
            },
            {
                slot_id: 'slot_2', actor_role: 'task acceptance', provider: 'openai',
                model: 'openai/gpt-5.6-sol', transport_status: 'timeout',
                parse_status: 'malformed', semantic_verdict: '',
                quorum_contribution: false, enforcement_impact: 'abstains', reason: 'timeout',
            },
        ],
    }] });
    assert.match(text, /panel_abc/);
    assert.match(text, /candidate_hash=candidate/);
    assert.match(text, /fence_hash=fence-hash/);
    assert.match(text, /slot_1.*claude-fable-5.*verdict=DEGRADED/);
    assert.match(text, /slot_1.*outcome_tier=best_effort/);
    assert.match(text, /slot_2.*transport=timeout.*parse=malformed/);
    assert.doesNotMatch(text, /raw_text/);
});

test('mixed panel renders quorum participation separately from pass support and veto', () => {
    const text = formatReviewProjection({ panels: [{
        panel_id: 'panel_mixed', surface: 'task_acceptance', authority: 'host_root',
        aggregate_signal: 'FAIL', transport_status: 'success', parse_status: 'valid',
        quorum: { required: 2, contributed: 3, configured: 3 },
        enforcement_impact: 'requires_revision', reason: 'one valid reviewer vetoed',
        actors: [
            {
                slot_id: 'slot_1', actor_role: 'task acceptance', provider: 'anthropic',
                model: 'anthropic/claude-fable-5', transport_status: 'success',
                parse_status: 'valid', semantic_verdict: 'PASS',
                quorum_contribution: true, enforcement_impact: 'supports_pass', reason: 'ready',
            },
            {
                slot_id: 'slot_2', actor_role: 'task acceptance', provider: 'openai',
                model: 'openai/gpt-5.6-sol', transport_status: 'success',
                parse_status: 'valid', semantic_verdict: 'PASS',
                quorum_contribution: true, enforcement_impact: 'supports_pass', reason: 'ready',
            },
            {
                slot_id: 'slot_3', actor_role: 'task acceptance', provider: 'google',
                model: 'google/gemini-3.5-flash', transport_status: 'success',
                parse_status: 'valid', semantic_verdict: 'FAIL',
                quorum_contribution: true, enforcement_impact: 'veto', reason: 'gap',
            },
        ],
    }] });
    assert.match(text, /panel_mixed.*verdict=FAIL.*quorum=3\/3 \(required 2\)/);
    assert.match(text, /slot_1.*verdict=PASS.*quorum=contributes.*enforcement=supports_pass/);
    assert.match(text, /slot_3.*verdict=FAIL.*quorum=contributes.*enforcement=veto/);
});

test('subagent terminal projection keeps review complete without using it as activity', () => {
    const summary = summarizeChatLiveEvent({
        type: 'send_message',
        is_progress: true,
        delegation_role: 'subagent',
        parent_task_id: 'parent',
        subagent_task_id: 'child',
        subagent_role: 'reviewer',
        subagent_event: 'completed',
        result: 'Concrete child result',
        write_surface: 'none',
        status: 'completed',
        outcome_axes: {
            lifecycle: { status: 'completed' },
            execution: { status: 'ok' },
            objective: { status: 'best_effort' },
            review: { status: 'degraded' },
            artifacts: { status: 'ready' },
        },
        review_projection: { panels: [{
            panel_id: 'panel_child', surface: 'task_acceptance', authority: 'host_root',
            aggregate_signal: 'DEGRADED', transport_status: 'success', parse_status: 'valid',
            quorum: { required: 1, contributed: 1, configured: 1 },
            enforcement_impact: 'degrades_completion', reason: 'needs owner context',
            actors: [],
        }] },
    });
    assert.equal(summary.terminal, true);
    assert.equal(summary.phase, 'warn');
    assert.equal(summary.expandByDefault, true);
    assert.equal(summary.activityPreview, 'Concrete child result');
    assert.match(summary.body, /panel_child/);
    assert.match(summary.fullBody, /^\[REVIEW\][\s\S]*panel_child/);
    assert.match(summary.fullBody, /\[RESULT\]\nConcrete child result/);
    assert.deepEqual(summary.meta, ['write=none', 'status=completed']);
});

test('terminal subagent activity prefers the authoritative result over emitter boilerplate', () => {
    const summary = summarizeChatLiveEvent({
        type: 'send_message',
        is_progress: true,
        delegation_role: 'subagent',
        parent_task_id: 'parent',
        subagent_task_id: 'child',
        subagent_event: 'completed',
        content: 'Subagent child completed (researcher).',
        result: 'The evidence-backed conclusion and its concrete next step.',
        status: 'completed',
    });
    assert.equal(summary.activityPreview, 'The evidence-backed conclusion and its concrete next step.');
    assert.match(summary.fullBody, /Subagent child completed/);
    assert.match(summary.fullBody, /\[RESULT\]\nThe evidence-backed conclusion/);
});

test('review-only subagent projection explicitly has no collapsed activity', () => {
    const summary = summarizeChatLiveEvent({
        type: 'send_message',
        is_progress: true,
        delegation_role: 'subagent',
        parent_task_id: 'parent',
        subagent_task_id: 'child',
        subagent_event: 'completed',
        review_projection: { panels: [{
            panel_id: 'panel_review_only', surface: 'task_acceptance', authority: 'host_root',
            aggregate_signal: 'PASS', transport_status: 'success', parse_status: 'valid',
            quorum: { required: 1, contributed: 1, configured: 1 },
            enforcement_impact: 'supports_pass', reason: 'review only', actors: [],
        }] },
    });
    assert.equal(summary.activityPreview, '');
    assert.match(summary.body, /panel_review_only/);
    assert.equal(summary.expandByDefault, true);
});

// ---------------------------------------------------------------------------
// Phase 6, owner directive #1: the per-bubble / per-subagent executor chip.
// «бейдж точно нужен, но не рекламный … что ТУТ бабл \ субагент на codex»
// ---------------------------------------------------------------------------

test('a delegated frame yields a small harness chip; an ordinary frame yields none', () => {
    const chip = executorChip({ executor_route: 'codex' });
    assert.equal(chip.harness, 'codex');
    assert.equal(chip.label, 'codex');           // short harness name, not a slogan
    assert.ok(chip.icon);                        // icon, Claudexor-style
    assert.match(chip.title, /codex/);
    // WHERE it ran is all the chip can see. It has no access to the run's spend, so
    // it must not describe the billing: "on your codex subscription" told the owner
    // the work was covered, which the ledger alone can say (and only when the harness
    // says it — a zero there may mean unknown pricing, never "free").
    assert.ok(!/subscription/i.test(chip.title), chip.title);
    // The opaque `harness=model` spelling prints the HARNESS part only.
    assert.equal(executorChip({ executor_route: 'codex=gpt-5.6-sol' }).label, 'codex');
    // An unknown harness still gets a chip (a generic mark), never a crash.
    assert.equal(executorChip({ executor_route: 'opencode' }).label, 'opencode');
    // ABSENT FACT -> no chip at all: no placeholder, no "api" noise on every
    // ordinary bubble (the native path is the unremarkable case).
    assert.equal(executorChip({}), null);
    assert.equal(executorChip({ executor_route: '' }), null);
    assert.equal(executorChip(null), null);
});

test('the chip rides both projections: an ordinary progress bubble and a subagent row', () => {
    const bubble = summarizeChatLiveEvent({
        is_progress: true, content: 'working on the thing', task_id: 't1',
        executor_route: 'codex',
    });
    assert.equal(bubble.executorChip.harness, 'codex');

    const subagentRow = summarizeChatLiveEvent({
        is_progress: true, content: 'child progress',
        delegation_role: 'subagent', subagent_task_id: 'child-1',
        subagent_event: 'running', subagent_role: 'implementer',
        executor_route: 'claude=sonnet-5',
    });
    assert.equal(subagentRow.executorChip.harness, 'claude');

    // Neither projection invents a chip when the fact is absent.
    assert.equal('executorChip' in summarizeChatLiveEvent({
        is_progress: true, content: 'plain', task_id: 't2',
    }), false);
    assert.equal('executorChip' in summarizeChatLiveEvent({
        is_progress: true, content: 'plain child', delegation_role: 'subagent',
        subagent_task_id: 'child-2', subagent_event: 'running',
    }), false);
});
