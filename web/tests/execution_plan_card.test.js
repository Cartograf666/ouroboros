import assert from 'node:assert/strict';
import test from 'node:test';

import {
    ROUTE_KIND_API,
    ROUTE_KIND_SESSION,
    buildDecisionPayload,
    decodeTargetChoice,
    encodeTargetChoice,
    estimateLine,
    targetChoices,
} from '../modules/execution_plan_card.js';

test('a choice round-trips through both kinds', () => {
    for (const route of [
        { kind: ROUTE_KIND_SESSION, target_id: 'codex' },
        { kind: ROUTE_KIND_API, target_id: 'google/gemini-3-pro' },
        // A model id containing a colon must survive: only the FIRST colon
        // separates the kind, or every provider-prefixed model would decode
        // to a truncated target.
        { kind: ROUTE_KIND_API, target_id: 'openai::gpt-5' },
    ]) {
        assert.deepEqual(decodeTargetChoice(encodeTargetChoice(route)), route);
    }
});

test('an unknown or malformed choice decodes to null, never a guess', () => {
    for (const value of ['', 'codex', 'bogus:codex', ':codex', 'api_chat:']) {
        assert.equal(decodeTargetChoice(value), null);
    }
});

test('unavailable targets stay in the list, marked', () => {
    const choices = targetChoices({
        api_chat: [{ kind: ROUTE_KIND_API, target_id: 'm1', label: 'Main · m1' }],
        agent_session: [{
            kind: ROUTE_KIND_SESSION,
            target_id: 'codex',
            label: 'Codex',
            available: false,
            unavailable_reason: 'subscription_window_exhausted',
        }],
    });
    assert.equal(choices.length, 2);
    const codex = choices.find((row) => row.value === 'agent_session:codex');
    assert.equal(codex.available, false);
    assert.equal(codex.reason, 'subscription_window_exhausted');
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
        {
            item_id: 'frontend',
            title: 'Frontend',
            recommended_route: { kind: ROUTE_KIND_SESSION, target_id: 'codex', model: 'gpt-5' },
        },
        {
            item_id: 'tests',
            title: 'Tests',
            recommended_route: { kind: ROUTE_KIND_API, target_id: 'local-model' },
        },
    ],
};

test('an unedited proposal is submitted exactly as proposed', () => {
    const payload = buildDecisionPayload(PROPOSAL, {});
    assert.equal(payload.task_id, 't-1');
    assert.equal(payload.plan.version, 1);
    assert.equal(payload.plan.root_task_id, 'r-1');
    assert.deepEqual(payload.plan.items[0].route,
        { kind: ROUTE_KIND_SESSION, target_id: 'codex', model: 'gpt-5' });
    assert.deepEqual(payload.plan.items[1].route,
        { kind: ROUTE_KIND_API, target_id: 'local-model' });
});

test('switching a row away from its harness drops that harness model', () => {
    const payload = buildDecisionPayload(PROPOSAL, { frontend: 'agent_session:claude' });
    // `gpt-5` is codex's model id; carried onto claude it would pin one engine's
    // model on another.
    assert.deepEqual(payload.plan.items[0].route,
        { kind: ROUTE_KIND_SESSION, target_id: 'claude' });
});

test('switching a row to an api model keeps the model as the target', () => {
    const payload = buildDecisionPayload(PROPOSAL, { tests: 'api_chat:google/gemini-3-pro' });
    assert.deepEqual(payload.plan.items[1].route,
        { kind: ROUTE_KIND_API, target_id: 'google/gemini-3-pro' });
});
