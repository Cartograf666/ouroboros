import assert from 'node:assert/strict';
import test from 'node:test';

import {
    buildDecisionPayload,
    estimateLine,
    subagentChoices,
} from '../modules/execution_plan_card.js';

const CATALOG = {
    subagents: [
        { subagent_id: 'primary', name: 'Primary', recommended_use: 'Substantial implementation.' },
        { subagent_id: 'scout', name: 'Scout', recommended_use: 'Fast exploration.' },
        { subagent_id: '', name: 'broken' },
    ],
};

test('the choices are the catalog in the owner\'s own words', () => {
    // The row's `recommended_use` is a note the owner wrote about when to reach
    // for it; the card carries it instead of summarizing it away.
    const choices = subagentChoices(CATALOG);
    assert.deepEqual(choices.map((c) => c.value), ['primary', 'scout']);
    assert.equal(choices[0].label, 'Primary');
    assert.equal(choices[1].hint, 'Fast exploration.');
    assert.deepEqual(subagentChoices({}), []);
    assert.deepEqual(subagentChoices(null), []);
});

test('an estimate with no basis prints nothing rather than a fake zero', () => {
    assert.equal(estimateLine(null), '');
    assert.equal(estimateLine({}), '');
    assert.equal(estimateLine({ duration_sec: 240, cost_usd: 0 }), '~4m · $0.00');
    assert.equal(estimateLine({ basis: 'no evidence yet' }), 'no evidence yet');
});

const PROPOSAL = {
    task_id: 't-1',
    root_task_id: 'r-1',
    items: [
        { item_id: 'frontend', title: 'Frontend', subagent_id: 'primary' },
        { item_id: 'tests', title: 'Tests', subagent_id: 'scout' },
    ],
};

test('an unedited proposal is submitted exactly as proposed', () => {
    const payload = buildDecisionPayload(PROPOSAL, {});
    assert.equal(payload.task_id, 't-1');
    assert.equal(payload.plan.version, 2);
    assert.equal(payload.plan.root_task_id, 'r-1');
    assert.deepEqual(payload.plan.items.map((i) => i.subagent_id), ['primary', 'scout']);
});

test('an edited row carries the owner\'s pick, and only that row moves', () => {
    const payload = buildDecisionPayload(PROPOSAL, { frontend: 'scout' });
    assert.equal(payload.plan.items[0].subagent_id, 'scout');
    assert.equal(payload.plan.items[1].subagent_id, 'scout');
    assert.equal(payload.plan.items[0].item_id, 'frontend');
});

test('a row names a catalog id and never a route', () => {
    // The whole point of the port: which agents EXIST is the owner's standing
    // catalog, so a plan row references one instead of defining a destination.
    const payload = buildDecisionPayload(PROPOSAL, {});
    for (const item of payload.plan.items) {
        assert.deepEqual(Object.keys(item).sort(), ['item_id', 'subagent_id', 'title']);
    }
});
