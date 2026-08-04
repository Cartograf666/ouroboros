import assert from 'node:assert/strict';
import test from 'node:test';

import {
    API_ROUTE_CHOICE,
    ROUTE_KIND_API,
    ROUTE_KIND_SESSION,
    advisoryRouteTransition,
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
        harnesses: [{ id: 'codex', display_name: 'Codex CLI', status: 'ok', enabled: true }],
    });
    const flat = JSON.stringify(groups);
    assert.ok(flat.includes('Codex CLI'));
    assert.ok(!/claudexor/i.test(flat), 'the aggregator brand must not appear as a provider');
    // The route select carries ROUTES only (finding #6): one API entry, one
    // entry per harness — never the flat model catalog. Both groups labeled.
    assert.equal(groups[0].label, 'API');
    assert.deepEqual(groups[0].options, [{ value: API_ROUTE_CHOICE, label: 'API model' }]);
    assert.equal(groups[1].options[0].value, 'session:codex');
});

test('a saved session route survives a discovery list that no longer contains its harness', () => {
    // Same rule as profileOptionsFor: the select's value must EXIST as an
    // option or the browser silently redraws the row as the first choice.
    const groups = routeChoiceGroups({ harnesses: [{ id: 'codex' }], currentChoice: 'session:claude' });
    const session = groups[1].options;
    assert.deepEqual(session.map((o) => o.value), ['session:codex', 'session:claude']);
    assert.match(session[1].label, /not in discovery/);
    // A choice discovery DOES list gains no duplicate.
    const listed = routeChoiceGroups({ harnesses: [{ id: 'codex' }], currentChoice: 'session:codex' });
    assert.deepEqual(listed[1].options.map((o) => o.value), ['session:codex']);
});

test('no :: syntax anywhere in encoded choices or composed targets', () => {
    const groups = routeChoiceGroups({
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
    // The API choice no longer carries the model id — the free-text input
    // does — so encode collapses every api row to the ONE api option, and a
    // fresh row (target '') displays exactly that option, not the first
    // catalog model (finding #6c).
    const apiRow = { route: { kind: ROUTE_KIND_API, target_id: 'openai/gpt-5.6-luna' } };
    assert.equal(encodeRouteChoice(apiRow), API_ROUTE_CHOICE);
    assert.equal(encodeRouteChoice({ route: { kind: ROUTE_KIND_API, target_id: '' } }), API_ROUTE_CHOICE);
    assert.deepEqual(decodeRouteChoice(encodeRouteChoice(apiRow)), { kind: ROUTE_KIND_API });
    const sessionRow = { route: { kind: ROUTE_KIND_SESSION, target_id: 'codex=gpt-5.6-sol' } };
    assert.deepEqual(decodeRouteChoice(encodeRouteChoice(sessionRow)),
        { kind: ROUTE_KIND_SESSION, harness: 'codex' });
    assert.deepEqual(splitSessionTarget('codex=gpt-5.6-sol'),
        { harness: 'codex', model: 'gpt-5.6-sol' });
});

test('advisory route switching never wipes a stored target (finding #7c)', () => {
    // Saved: api with an explicit target. Flip to a session and back: the
    // saved api target is restored, not written to ''.
    const savedApi = { kind: 'api', target_id: 'anthropic/claude-opus-5' };
    let memory = { api: { ...savedApi }, session: null };
    const toSession = advisoryRouteTransition(savedApi, { kind: ROUTE_KIND_SESSION, harness: 'codex' }, memory);
    assert.deepEqual(toSession.route, { kind: ROUTE_KIND_SESSION, target_id: 'codex' });
    const back = advisoryRouteTransition(toSession.route, { kind: ROUTE_KIND_API }, toSession.memory);
    assert.deepEqual(back.route, savedApi);

    // Saved: session with a model spec. Kind round-trip restores the FULL
    // spec (harness=model), not the bare harness.
    const savedSession = { kind: ROUTE_KIND_SESSION, target_id: 'claude=claude-opus-5' };
    memory = { api: null, session: { ...savedSession } };
    const toApi = advisoryRouteTransition(savedSession, { kind: ROUTE_KIND_API }, memory);
    assert.deepEqual(toApi.route, { kind: 'api', target_id: '' });
    const restored = advisoryRouteTransition(toApi.route, { kind: ROUTE_KIND_SESSION, harness: 'claude' }, toApi.memory);
    assert.deepEqual(restored.route, savedSession);

    // Re-selecting the CURRENT kind/harness is a no-op, never a reset.
    const noop = advisoryRouteTransition(savedSession, { kind: ROUTE_KIND_SESSION, harness: 'claude' }, memory);
    assert.deepEqual(noop.route, savedSession);
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
