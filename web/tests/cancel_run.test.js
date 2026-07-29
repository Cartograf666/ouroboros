import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import {
    summarizeChatLiveEvent,
    summarizeLogEvent,
    taskOutcomeSeverity,
    taskTerminalPhase,
} from '../modules/log_events.js';
import { cancelRunEligibility, isTerminalTaskPhase } from '../modules/chat.js';

// --- cancelled severity reducer (added ONCE, consumed everywhere) ---

test('taskOutcomeSeverity classifies cancelled lifecycle as its own severity', () => {
    assert.equal(taskOutcomeSeverity({ status: 'cancelled' }), 'cancelled');
    assert.equal(taskOutcomeSeverity({ status: 'cancel_requested' }), 'cancelled');
    assert.equal(taskOutcomeSeverity({
        outcome_axes: { lifecycle: { status: 'cancelled' }, execution: { status: 'cancelled' } },
    }), 'cancelled');
});

test('cancellation wins over failure-shaped teardown side facts', () => {
    // A cancelled workspace task legitimately has artifacts=missing — that must
    // not relabel an owner-requested cancellation as "Failed".
    assert.equal(taskOutcomeSeverity({
        status: 'cancelled',
        artifact_status: 'missing',
        outcome_axes: { lifecycle: { status: 'cancelled' }, artifacts: { status: 'missing' } },
    }), 'cancelled');
});

test('non-cancelled severities are unchanged', () => {
    assert.equal(taskOutcomeSeverity({ status: 'done' }), 'done');
    assert.equal(taskOutcomeSeverity({ outcome_axes: { lifecycle: { status: 'failed' } } }), 'error');
    assert.equal(taskOutcomeSeverity({ outcome_axes: { execution: { status: 'degraded' } } }), 'warn');
});

test('taskTerminalPhase maps every severity to its card phase', () => {
    assert.equal(taskTerminalPhase({ status: 'cancelled' }), 'cancelled');
    assert.equal(taskTerminalPhase({ outcome_axes: { lifecycle: { status: 'failed' } } }), 'error');
    assert.equal(taskTerminalPhase({ outcome_axes: { execution: { status: 'degraded' } } }), 'warn');
    assert.equal(taskTerminalPhase({ status: 'done' }), 'done');
});

// --- both task_done summarizers ---

test('chat live task_done summarizer renders an honest Cancelled state', () => {
    const view = summarizeChatLiveEvent({ type: 'task_done', status: 'cancelled' });
    assert.equal(view.phase, 'cancelled');
    assert.equal(view.headline, 'Cancelled');
    assert.equal(view.terminal, true);
});

test('chat live task_done summarizer keeps the clean Done contract', () => {
    const view = summarizeChatLiveEvent({ type: 'task_done', status: 'done' });
    assert.equal(view.phase, 'done');
    assert.equal(view.headline, 'Done');
});

test('logs task_done summarizer labels cancellation as Cancelled', () => {
    const view = summarizeLogEvent({ type: 'task_done', status: 'cancelled' });
    assert.equal(view.phase, 'cancelled');
    assert.equal(view.headline, 'Cancelled');
});

// --- terminal phase + history replay fallback ---

test('cancelled is a terminal card phase (card resolves, never re-inflates)', () => {
    assert.equal(isTerminalTaskPhase('cancelled'), true);
    assert.equal(isTerminalTaskPhase('done'), true);
    assert.equal(isTerminalTaskPhase('working'), false);
});

test('history replay of a cancelled root resolves to Cancelled, not Done', () => {
    // The reload fallback builds {...row, status: task_terminal_status} and asks
    // taskTerminalPhase for the finishLiveCard phase (chat.js terminal fallback).
    const terminalRecord = { task_id: 'root1', status: 'cancelled' };
    assert.equal(taskTerminalPhase(terminalRecord), 'cancelled');
    assert.notEqual(taskTerminalPhase(terminalRecord), 'done');
});

// --- Cancel run eligibility (host-attested marker + structural gates) ---

test('Cancel run offered only on live, marker-attested root cards', () => {
    const eligible = {
        groupId: 'abc12345', isSubagent: false, finished: false, cancelable: true, converted: false,
    };
    assert.equal(cancelRunEligibility(eligible), true);
    // Subagent cards never offer it (the root cascade covers them).
    assert.equal(cancelRunEligibility({ ...eligible, isSubagent: true }), false);
    // Reusable slots (background consciousness / legacy active) never offer it.
    assert.equal(cancelRunEligibility({ ...eligible, groupId: 'bg-consciousness' }), false);
    assert.equal(cancelRunEligibility({ ...eligible, groupId: 'active' }), false);
    // Finished and converted cards have nothing live to cancel.
    assert.equal(cancelRunEligibility({ ...eligible, finished: true }), false);
    assert.equal(cancelRunEligibility({ ...eligible, converted: true }), false);
    // Without the host-attested marker (e.g. a direct-chat turn's card, which has
    // the same shape but no queue entry) the button must not appear.
    assert.equal(cancelRunEligibility({ ...eligible, cancelable: false }), false);
    assert.equal(cancelRunEligibility({ ...eligible, groupId: '' }), false);
});

test('both cancel surfaces report a refused cancellation', () => {
    // The endpoint answers only after the teardown, so success needs no extra
    // reporting — but a refusal must never read as a silent no-op click.
    const chat = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');
    const activity = readFileSync(new URL('../modules/activity.js', import.meta.url), 'utf8');
    for (const source of [chat, activity]) {
        assert.match(source, /await cancelTask\(/);
    }
    // ...and Activity no longer swallows a refused cancel (503) as a no-op click,
    // while keeping the documented 404 completion race graceful.
    assert.match(activity, /exc\?\.status !== 404/);
    assert.match(activity, /catch \(exc\)[\s\S]{0,400}showToast\(`Action failed/);
});

test('a timeout-retry root gains Cancel run: the host marker is the truth', () => {
    // A retry root's frame carries root_task_id naming the ORIGINAL task, so any
    // structural frameRoot===taskId gate would reject exactly the marker the
    // supervisor attested. Pinned at source: the handler trusts the marker alone.
    const chat = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');
    assert.match(chat, /msg\?\.cancelable === true && msg\?\.task_id\) markTaskCancelable/);
    assert.doesNotMatch(chat, /frameRoot === taskId\) *&&[\s\S]{0,80}markTaskCancelable/);
    // ...and the eligibility reducer still refuses subagent/finished/reusable cards,
    // so trusting the marker does not widen the button beyond live pooled roots.
    assert.equal(cancelRunEligibility({
        groupId: 'retry-1', isSubagent: false, finished: false, cancelable: true,
    }), true);
    assert.equal(cancelRunEligibility({
        groupId: 'child-1', isSubagent: true, finished: false, cancelable: true,
    }), false);
});

test('a 404 cancel reconciles the card from the durable record', () => {
    // 404 says "not live"; if the terminal frame was lost the card would sit
    // "Working" forever. The branch must fetch the durable record and resolve the
    // card through the SAME terminal seam replay uses — not merely hide a button.
    const chat = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');
    const branch = chat.slice(chat.indexOf('cancelableTaskIds.delete(taskId)'));
    assert.match(branch.slice(0, 1200), /apiFetch\(`\/api\/tasks\/\$\{encodeURIComponent\(taskId\)\}`\)/);
    assert.match(branch.slice(0, 1600), /finishLiveCard\(taskId, taskTerminalPhase\(stored\)\)/);
});

test('a successful cancel also reconciles when task_done publication is lost', () => {
    // Durable cancellation precedes fail-soft publication. A 200 with no WS frame
    // must therefore read the stored result before leaving the button disabled.
    const chat = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');
    const success = chat.slice(chat.indexOf('await cancelTask(taskId, { cascade: true })'));
    const beforeCatch = success.slice(0, success.indexOf('} catch (exc)'));
    assert.match(beforeCatch, /apiFetch\(`\/api\/tasks\/\$\{encodeURIComponent\(taskId\)\}`\)/);
    assert.match(beforeCatch, /finishLiveCard\(taskId, taskTerminalPhase\(stored\)\)/);
});
