import assert from 'node:assert/strict';
import test from 'node:test';

import {
    ROUTE_KIND_AGENT_SESSION,
    ROUTE_KIND_API_MODEL,
    normalizeRouteSpec,
    serializeRouteSpec,
} from '../modules/route_editor_primitives.js';
import {
    MAX_AVAILABLE_SUBAGENTS,
    availableSubagentRowMarkup,
    availableSubagentsSavePayload,
    buildAvailableSubagentsSetting,
    createAvailableSubagentsEditor,
    generatedPreviewCanReplace,
    parseAvailableSubagentsSetting,
    renderSubagentsSection,
    subagentSettingsFingerprint,
    validateAvailableSubagentsSetting,
} from '../modules/subagents_settings.js';
import { buildReviewerSlotsSetting } from '../modules/reviewer_slots.js';

function apiRow(overrides = {}) {
    return {
        subagent_id: 'api_scout',
        name: 'API scout',
        recommended_use: 'Fast independent research and verification.',
        route: { kind: ROUTE_KIND_API_MODEL, target_id: 'openai/gpt-5.6-luna' },
        effort: 'high',
        ...overrides,
    };
}

function sessionRow(overrides = {}) {
    return {
        subagent_id: 'codex_builder',
        name: 'Codex builder',
        recommended_use: 'Implementation in a real workspace.',
        route: {
            kind: ROUTE_KIND_AGENT_SESSION,
            target_id: 'codex=gpt-5.6-sol-high',
            credential_profile_id: 'koshak',
        },
        ...overrides,
    };
}

function setting(items = [apiRow(), sessionRow()]) {
    return { enabled: true, items };
}

test('canonical parser accepts object or JSON and refuses unknown saved fields', () => {
    const objectResult = parseAvailableSubagentsSetting(setting());
    assert.equal(objectResult.error, '');
    assert.deepEqual(objectResult.setting, setting());

    const textResult = parseAvailableSubagentsSetting(JSON.stringify(setting([apiRow()])));
    assert.equal(textResult.setting.items[0].subagent_id, 'api_scout');

    const unknown = parseAvailableSubagentsSetting({ ...setting(), surprise: true });
    assert.equal(unknown.setting, null);
    assert.match(unknown.error, /unknown field: surprise/);

    const rowUnknown = parseAvailableSubagentsSetting(setting([{ ...apiRow(), role: 'scout' }]));
    assert.equal(rowUnknown.setting, null);
    assert.match(rowUnknown.error, /unknown field: role/);

    const routeUnknown = parseAvailableSubagentsSetting(setting([
        apiRow({ route: { ...apiRow().route, base_url: 'https://example.test' } }),
    ]));
    assert.equal(routeUnknown.setting, null);
    assert.match(routeUnknown.error, /route has unknown field: base_url/);

    const badKind = parseAvailableSubagentsSetting(setting([
        apiRow({ route: { kind: 'api_chat', target_id: 'openai/gpt-5.6-luna' } }),
    ]));
    assert.equal(badKind.setting, null);
    assert.match(badKind.error, /unsupported route kind/);

    const apiPin = parseAvailableSubagentsSetting(setting([
        apiRow({ route: {
            kind: 'api_model', target_id: 'openai/gpt-5.6-luna',
            credential_profile_id: 'must-not-ride-api',
        } }),
    ]));
    assert.equal(apiPin.setting, null);
    assert.match(apiPin.error, /account pin on an API route/);
});

test('an unloaded or malformed view cannot replace the owner setting', () => {
    assert.deepEqual(availableSubagentsSavePayload({ loaded: false, setting: setting() }), {});
    assert.deepEqual(availableSubagentsSavePayload({
        loaded: false,
        parseError: 'invalid JSON',
        setting: setting(),
    }), {});
    assert.deepEqual(availableSubagentsSavePayload({ loaded: true, setting: setting([apiRow()]) }), {
        OUROBOROS_SUBAGENTS: setting([apiRow()]),
    });
});

test('object and serialized settings compare as the same new-child-task intent', () => {
    assert.equal(
        subagentSettingsFingerprint(setting([apiRow()])),
        subagentSettingsFingerprint(JSON.stringify(setting([apiRow()]))),
    );
});

test('loaded saved rows remain collectible when live status is unavailable', () => {
    const store = {
        error: 'agent service offline',
        snapshot: null,
        facet: () => 'transport_error',
        subscribe: () => () => {},
        refresh: async () => {},
    };
    const editor = createAvailableSubagentsEditor({ store, doc: null, win: null });
    editor.load(setting([sessionRow()]), { source: 'configured' });
    assert.deepEqual(editor.collect(), {
        OUROBOROS_SUBAGENTS: setting([sessionRow()]),
    });
});

test('validation protects stable unique IDs, route shape, effort and ten-row limit', () => {
    assert.deepEqual(validateAvailableSubagentsSetting(setting()), []);
    assert.match(validateAvailableSubagentsSetting(setting([
        apiRow(), apiRow({ name: 'duplicate' }),
    ])).join(' '), /repeats stable ID/);
    assert.match(validateAvailableSubagentsSetting(setting([
        apiRow({ subagent_id: 'bad id' }),
    ])).join(' '), /stable ID/);
    assert.match(validateAvailableSubagentsSetting(setting([
        apiRow({ route: { kind: 'api_chat', target_id: 'x' } }),
    ])).join(' '), /API model or Agent session/);
    assert.match(validateAvailableSubagentsSetting(setting([
        apiRow({ effort: 'ultra' }),
    ])).join(' '), /unsupported reasoning effort/);
    const tooMany = Array.from({ length: MAX_AVAILABLE_SUBAGENTS + 1 }, (_, index) =>
        apiRow({ subagent_id: `actor_${index}` }));
    assert.match(validateAvailableSubagentsSetting(setting(tooMany)).join(' '), /at most 10/);
});

test('save materializes a readable name but never rewrites stable identity or purpose', () => {
    const built = buildAvailableSubagentsSetting(setting([
        apiRow({ subagent_id: 'fast_research', name: '', recommended_use: '  owner text  ' }),
    ]));
    assert.equal(built.items[0].subagent_id, 'fast_research');
    assert.equal(built.items[0].name, 'Fast Research');
    assert.equal(built.items[0].recommended_use, '  owner text  ');
});

test('API and session rows render different controls; account belongs only to session', () => {
    const state = {
        catalogKnown: true,
        accountsKnown: true,
        statusError: '',
        snapshot: {
            harnesses: [{
                id: 'codex', display_name: 'Codex', status: 'ok',
                models: [{ id: 'gpt-5.6-sol-high' }],
            }],
            profiles: {
                harnessAccounts: [],
                profiles: [{
                    profile: { harness_id: 'codex', profile_id: 'koshak', enabled: true },
                    status: { verification: 'passed' },
                }],
            },
        },
    };
    const apiHtml = availableSubagentRowMarkup(apiRow(), state);
    assert.match(apiHtml, /aria-label="API model"/);
    assert.doesNotMatch(apiHtml, /data-subagent-field="account"/);

    const sessionHtml = availableSubagentRowMarkup(sessionRow(), state);
    assert.match(sessionHtml, /aria-label="Agent session model"/);
    assert.match(sessionHtml, /data-subagent-field="account"/);
    assert.match(sessionHtml, /Account: koshak \(pinned\)/);
});

test('saved unavailable session route and account remain selectable', () => {
    const state = {
        catalogKnown: true,
        accountsKnown: true,
        statusError: '',
        snapshot: { harnesses: [], profiles: { harnessAccounts: [], profiles: [] } },
    };
    const html = availableSubagentRowMarkup(sessionRow(), state);
    assert.match(html, /codex \(not in discovery\)/);
    assert.match(html, /gpt-5.6-sol-high \(not in discovery\)/);
    assert.match(html, /Account: koshak \(not in discovery\)/);
    assert.match(html, /currently unavailable/);
});

test('preview replaces only a clean generated baseline', () => {
    assert.equal(generatedPreviewCanReplace({ dirty: false, parsedSetting: setting() }), true);
    assert.equal(generatedPreviewCanReplace({ dirty: true, parsedSetting: setting() }), false);
    assert.equal(generatedPreviewCanReplace({ dirty: false, parsedSetting: null }), false);

    const editor = createAvailableSubagentsEditor({ doc: null, win: null });
    editor.load(setting([apiRow()]), { source: 'onboarding_default' });
    const result = editor.applyGeneratedPreview({
        available_subagents: setting([sessionRow()]),
        source: 'onboarding_default',
        diagnostics: [],
    });
    assert.equal(result.applied, true);
    assert.equal(editor.setting.items[0].subagent_id, 'codex_builder');
});

test('a typed preview refusal stays typed and cannot become an empty fictional draft', () => {
    const editor = createAvailableSubagentsEditor({ doc: null, win: null });
    editor.setPreviewFailure({
        message: 'preview refused',
        body: {
            code: 'subagent_preview_unavailable',
            diagnostics: { errors: [{ code: 'catalog_unread', message: 'Model catalog was not read.' }] },
        },
    });
    assert.equal(editor.loaded, false);
    assert.match(editor.parseError, /subagent_preview_unavailable: preview refused/);
    assert.match(editor.parseError, /catalog_unread: Model catalog was not read/);
    assert.deepEqual(editor.collect(), {});
});

test('shared route primitive preserves each semantic owner account spelling', () => {
    const normalizedReviewer = normalizeRouteSpec({
        kind: 'agent_session', target_id: 'codex=gpt-5.6-sol-high', profile_id: 'review-account',
    });
    assert.equal(normalizedReviewer.credential_pin, 'review-account');
    assert.deepEqual(serializeRouteSpec(normalizedReviewer, {
        apiKind: 'api_chat', credentialField: 'profile_id',
    }), {
        kind: 'agent_session',
        target_id: 'codex=gpt-5.6-sol-high',
        profile_id: 'review-account',
    });
    assert.deepEqual(serializeRouteSpec(sessionRow().route, {
        apiKind: ROUTE_KIND_API_MODEL,
        credentialField: 'credential_profile_id',
    }), sessionRow().route);
});

test('reviewer structured bytes keep api_chat and profile_id after extraction', () => {
    const reviewer = buildReviewerSlotsSetting({
        triad: [{
            slot_id: 'triad_1',
            route: {
                kind: 'agent_session',
                target_id: 'codex=gpt-5.6-sol-high',
                profile_id: 'koshak',
            },
            effort: 'high',
        }],
        scope: [{
            slot_id: 'scope_1',
            route: { kind: 'api_chat', target_id: 'openai/gpt-5.6-sol' },
        }],
        advisory: { enabled: true, route: { kind: 'api', target_id: '' }, effort: 'low' },
    });
    const parsed = JSON.parse(reviewer);
    assert.equal(parsed.triad[0].route.profile_id, 'koshak');
    assert.equal(parsed.triad[0].route.credential_profile_id, undefined);
    assert.equal(parsed.scope[0].route.kind, 'api_chat');
});

test('Settings section keeps global task-authority controls beside the actor list', () => {
    const html = renderSubagentsSection();
    assert.match(html, /<h3>Available subagents<\/h3>/);
    assert.match(html, /id="available-subagents-editor"/);
    assert.match(html, /id="s-allow-mutative-subagents"/);
    assert.match(html, /id="s-active-subagents"/);
    assert.match(html, /id="s-subagent-depth"/);
    assert.match(html, /id="s-subagent-worktree-root"/);
    assert.match(html, /id="s-subagent-projects-root"/);
});
