import assert from 'node:assert/strict';
import test from 'node:test';

import {
    CUSTOM_API_CHOICE,
    ROUTE_KIND_API,
    ROUTE_KIND_SESSION,
    buildReviewerSlotsSetting,
    capabilityBadge,
    composeSessionTarget,
    decodeRouteChoice,
    describeLastExecution,
    encodeRouteChoice,
    mintSlotId,
    profileOptionsFor,
    routeChoiceGroups,
    splitSessionTarget,
} from '../modules/reviewer_slots.js';

test('a saved account pin survives a discovery list that no longer contains it', () => {
    // The select's value must EXIST as an option or the browser silently selects the
    // first one — "automatic rotation" — so a row pinned to one account redrew as
    // unpinned whenever the daemon was down or that account was signed out. Nothing
    // looked wrong, and saving the panel made the widening real.
    const discovered = profileOptionsFor(['koshak', 'valentine'], 'koshak');
    assert.deepEqual(discovered.map((o) => o.value), ['', 'koshak', 'valentine']);

    const undiscovered = profileOptionsFor(['valentine'], 'koshak');
    assert.deepEqual(undiscovered.map((o) => o.value), ['', 'valentine', 'koshak']);
    assert.match(undiscovered[2].label, /not in discovery/);

    // Discovery empty entirely (daemon down) is the SAME case, not a special one.
    assert.deepEqual(profileOptionsFor([], 'koshak').map((o) => o.value), ['', 'koshak']);
    // No pin: nothing invented, and the rotation entry stays the only default.
    assert.deepEqual(profileOptionsFor([], '').map((o) => o.value), ['']);
    assert.deepEqual(profileOptionsFor(null, '').map((o) => o.value), ['']);
});

test('the provider shown for a delegated row is the harness name, never Claudexor', () => {
    const groups = routeChoiceGroups({
        catalogModels: ['openai/gpt-5.6-luna'],
        harnesses: [{ id: 'codex', display_name: 'Codex CLI', status: 'ok', enabled: true }],
    });
    const flat = JSON.stringify(groups);
    assert.ok(flat.includes('Codex CLI'));
    assert.ok(!/claudexor/i.test(flat), 'the aggregator brand must not appear as a provider');
    // One grouped combobox encodes both kind and target.
    assert.equal(groups[0].label, 'API models');
    assert.equal(groups[1].options[0].value, 'session:codex');
});

test('no :: syntax anywhere in encoded choices or composed targets', () => {
    const groups = routeChoiceGroups({
        catalogModels: ['openai/gpt-5.6-luna', 'anthropic/claude-sonnet-5'],
        harnesses: [{ id: 'codex' }, { id: 'claude' }],
    });
    for (const group of groups) {
        for (const option of group.options) {
            assert.ok(!option.value.includes('::'), option.value);
        }
    }
    assert.equal(composeSessionTarget('codex', 'gpt-5.6-sol'), 'codex=gpt-5.6-sol');
    assert.equal(composeSessionTarget('codex', ''), 'codex');
});

test('route choice round-trips through encode/decode', () => {
    const apiRow = { route: { kind: ROUTE_KIND_API, target_id: 'openai/gpt-5.6-luna' } };
    assert.deepEqual(decodeRouteChoice(encodeRouteChoice(apiRow)),
        { kind: ROUTE_KIND_API, target: 'openai/gpt-5.6-luna' });
    const sessionRow = { route: { kind: ROUTE_KIND_SESSION, target_id: 'codex=gpt-5.6-sol' } };
    assert.deepEqual(decodeRouteChoice(encodeRouteChoice(sessionRow)),
        { kind: ROUTE_KIND_SESSION, harness: 'codex' });
    assert.deepEqual(decodeRouteChoice(CUSTOM_API_CHOICE), { kind: ROUTE_KIND_API, custom: true });
    assert.deepEqual(splitSessionTarget('codex=gpt-5.6-sol'),
        { harness: 'codex', model: 'gpt-5.6-sol' });
});

test('the composed setting carries stable ids, per-row routes/efforts and the optional pin', () => {
    const setting = JSON.parse(buildReviewerSlotsSetting({
        triad: [
            { slot_id: 't_api', route: { kind: ROUTE_KIND_API, target_id: 'openai/gpt-5.6-luna' }, effort: 'high' },
            { slot_id: 't_sess', route: { kind: ROUTE_KIND_SESSION, target_id: 'codex=gpt-5.6-sol', profile_id: 'koshak' }, effort: '' },
        ],
        scope: [{ slot_id: 's_1', route: { kind: ROUTE_KIND_API, target_id: 'openai/gpt-5.6-terra' }, effort: 'xhigh' }],
        advisory: { enabled: false, route: { kind: ROUTE_KIND_SESSION, target_id: 'codex' }, effort: 'low' },
    }));
    assert.deepEqual(setting.triad[0], {
        slot_id: 't_api',
        route: { kind: 'api_chat', target_id: 'openai/gpt-5.6-luna' },
        effort: 'high',
    });
    // '' effort means "surface default" and is OMITTED, never written as ''.
    assert.equal('effort' in setting.triad[1], false);
    // The optional manual pin (Q2-в) rides only when set; rotation is default.
    assert.equal(setting.triad[1].route.profile_id, 'koshak');
    assert.equal('profile_id' in setting.scope[0].route, false);
    assert.equal(setting.advisory.enabled, false);
    assert.equal(setting.advisory.effort, 'low');
});

test('minted slot ids are prefixed, unique, and never an array index', () => {
    const taken = ['triad_abc123'];
    const minted = mintSlotId('triad', taken);
    assert.match(minted, /^triad_[a-z0-9]{4,}$/);
    assert.ok(!taken.includes(minted));
    assert.notEqual(mintSlotId('scope', []), mintSlotId('scope', []));
});

test('the runs-as line is the capability_delta projection, compact and honest', () => {
    const line = describeLastExecution({
        ts: '2026-08-03T10:00:00Z',
        effective: { route: 'agent_session:codex', model: 'gpt-5.6-sol', effort: 'xhigh',
                     verdict_method: 'light_model_extraction' },
        capability_delta: [{ reason: 'extraction_instead_of_schema' }],
    });
    assert.ok(line.includes('runs as agent_session:codex'));
    assert.ok(line.includes('model gpt-5.6-sol'));
    assert.ok(line.includes('verdict via light model extraction'));
    assert.ok(line.includes('1 capability delta disclosed'));
    assert.equal(describeLastExecution(null), '');
});

test('capability badges display facts and never configure', () => {
    const sessionRow = { route: { kind: ROUTE_KIND_SESSION, target_id: 'codex' } };
    assert.ok(capabilityBadge(sessionRow, { codex: { status: 'ok' } }).includes('route ok'));
    assert.ok(capabilityBadge(sessionRow, {}).includes('not discovered'));
    const apiRow = { route: { kind: ROUTE_KIND_API, target_id: 'openai/gpt-5.6-luna' } };
    assert.equal(capabilityBadge(apiRow, {}), 'api pack delivery');
});

test('the runs-as line shows APPLIED account/access and honest absence for an undisclosed model', () => {
    const applied = describeLastExecution({
        effective: { route: 'agent_session:codex', model: 'gpt-5.6-sol',
                     profile_id: 'koshak', access: 'readonly', effort: 'xhigh' },
    });
    assert.ok(applied.includes('account koshak'));
    assert.ok(applied.includes('access readonly'));
    // Old telemetry: a session with NO resolved model says so — the requested
    // model never masquerades as the applied one.
    const bare = describeLastExecution({ effective: { route: 'agent_session:codex' } });
    assert.ok(bare.includes('model not disclosed'));
    assert.ok(!bare.includes('account'));
    // An api row keeps its sent-model-is-applied-model reading with no noise.
    const api = describeLastExecution({ effective: { route: 'api_chat', model: 'openai/x' } });
    assert.ok(api.includes('model openai/x') && !api.includes('not disclosed'));
});
