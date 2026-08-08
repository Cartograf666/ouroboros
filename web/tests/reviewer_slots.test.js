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
    sessionModelOptions,
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
    assert.ok(line.includes('codex session'));
    assert.ok(line.includes('gpt-5.6-sol'));
    assert.ok(line.includes('verdict via light model extraction'));
    assert.ok(line.includes('1 capability delta disclosed'));
    // The timestamp is humanized for the visible line; the raw ISO instant
    // stays recoverable in the row tooltip, never inline.
    assert.ok(!line.includes('2026-08-03T10:00:00Z'));
    assert.equal(describeLastExecution(null), '');
});

test('capability badges display facts and never configure', () => {
    const sessionRow = { route: { kind: ROUTE_KIND_SESSION, target_id: 'codex' } };
    assert.ok(capabilityBadge(sessionRow, { codex: { status: 'ok' } }).includes('route ok'));
    assert.ok(capabilityBadge(sessionRow, {}).includes('not discovered'));
    const apiRow = { route: { kind: ROUTE_KIND_API, target_id: 'openai/gpt-5.6-luna' } };
    assert.equal(capabilityBadge(apiRow, {}), 'API delivery');
});

test('the session model-options fragment guards a saved model discovery no longer lists', () => {
    // ONE fragment for every session model select (triad, scope, advisory,
    // Subagents): "Engine default model" first, discovery next, and a saved
    // model the daemon cannot see right now keeps an option — otherwise the
    // browser silently redraws the select as "Engine default" and the next
    // Save really erases the pin (REG:1190 #7 class).
    const listed = sessionModelOptions({ models: [{ id: 'sonnet' }, { id: 'claude-opus-5' }] }, 'sonnet');
    assert.deepEqual(listed.map((o) => o.value), ['', 'sonnet', 'claude-opus-5']);
    assert.equal(listed[0].label, 'Engine default model');

    const unlisted = sessionModelOptions({ models: [{ id: 'claude-opus-5' }] }, 'sonnet');
    assert.deepEqual(unlisted.map((o) => o.value), ['', 'claude-opus-5', 'sonnet']);
    assert.match(unlisted[2].label, /not in discovery/);

    // Daemon down (no harness at all) is the SAME case, not a special one.
    assert.deepEqual(sessionModelOptions(null, 'sonnet').map((o) => o.value), ['', 'sonnet']);
    assert.deepEqual(sessionModelOptions(null, '').map((o) => o.value), ['']);
});

test('an advisory session model composes into the target and survives save-load', () => {
    // Compose exactly as the data-advisory-model handler does…
    const target = composeSessionTarget('codex', 'gpt-5.6-luna');
    const setting = JSON.parse(buildReviewerSlotsSetting({
        triad: [{ slot_id: 't1', route: { kind: ROUTE_KIND_API, target_id: 'openai/x' }, effort: '' }],
        scope: [{ slot_id: 's1', route: { kind: ROUTE_KIND_API, target_id: 'openai/y' }, effort: '' }],
        advisory: { enabled: true, route: { kind: ROUTE_KIND_SESSION, target_id: target, profile_id: 'koshak' }, effort: 'low' },
    }));
    // …and the saved value round-trips model AND profile pin intact.
    assert.equal(setting.advisory.route.kind, ROUTE_KIND_SESSION);
    assert.equal(setting.advisory.route.target_id, 'codex=gpt-5.6-luna');
    assert.equal(setting.advisory.route.profile_id, 'koshak');
    assert.ok(!setting.advisory.route.target_id.includes('::'));
    assert.deepEqual(splitSessionTarget(setting.advisory.route.target_id),
        { harness: 'codex', model: 'gpt-5.6-luna' });
});

test('switching the advisory harness resets the model to the bare harness', () => {
    // The model belongs to the harness it was picked for (same rule as the
    // triad rows and the Subagents tail): A→B lands on B's engine default,
    // spelled as the bare harness — never A's model carried across.
    const saved = { kind: ROUTE_KIND_SESSION, target_id: 'claude=claude-opus-5' };
    const out = advisoryRouteTransition(saved, { kind: ROUTE_KIND_SESSION, harness: 'codex' },
        { api: null, session: { ...saved } });
    assert.deepEqual(out.route, { kind: ROUTE_KIND_SESSION, target_id: 'codex' });
});

test('the advisory api-route model input round-trips as the raw target', () => {
    // kind stays the advisory's own 'api' (NOT api_chat — the branch trap),
    // and the free-text SDK spelling rides target_id verbatim; no profile pin
    // is emitted on the api kind.
    const setting = JSON.parse(buildReviewerSlotsSetting({
        triad: [{ slot_id: 't1', route: { kind: ROUTE_KIND_API, target_id: 'openai/x' }, effort: '' }],
        scope: [{ slot_id: 's1', route: { kind: ROUTE_KIND_API, target_id: 'openai/y' }, effort: '' }],
        advisory: { enabled: true, route: { kind: 'api', target_id: 'sonnet', profile_id: 'koshak' }, effort: 'low' },
    }));
    assert.equal(setting.advisory.route.kind, 'api');
    assert.equal(setting.advisory.route.target_id, 'sonnet');
    assert.equal('profile_id' in setting.advisory.route, false);
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
    assert.ok(api.includes('openai/x') && !api.includes('not disclosed'));
});

test('an unread catalog is not "None available", and the reason is not invented', () => {
    // The subscriptions group used to announce absence whenever the catalog was
    // empty — including the ordinary idle case, sending the owner to sign in for
    // accounts already in the agent home.
    const read = routeChoiceGroups({ harnesses: [], catalogRead: 'ok' });
    const subs = (g) => g.find((x) => x.label.startsWith('Coding agents')).options[0].label;
    assert.match(subs(read), /None available/, 'a READ catalog may state absence');

    assert.match(subs(routeChoiceGroups({ harnesses: [], catalogRead: 'not_read' })), /Not checked yet/);
    assert.match(subs(routeChoiceGroups({ harnesses: [], catalogRead: 'not_read' })), /not running/);

    // A refused or failed read is NOT "the daemon is not running" — it was
    // running and this read died; saying otherwise is a second lie.
    for (const state of ['failed', 'transport']) {
        const label = subs(routeChoiceGroups({ harnesses: [], catalogRead: state }));
        assert.match(label, /Not checked/);
        assert.ok(!/not running/.test(label), `${state} must not claim the daemon is down`);
    }
});
