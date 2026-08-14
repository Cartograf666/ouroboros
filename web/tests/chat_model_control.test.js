import assert from 'node:assert/strict';
import test from 'node:test';

import {
    buildModelChoices,
    deriveModelStatus,
    modelSelectionPayload,
    normalizeModelIdentity,
    parseQuotaError,
} from '../modules/chat_model_control.js';

test('normalizes provider-tagged settings and runtime model identities', () => {
    assert.equal(
        normalizeModelIdentity('openai-compatible::gemini-3.5-flash'),
        'openai-compatible/gemini-3.5-flash',
    );
    assert.equal(
        normalizeModelIdentity('openai-compatible/gemini-3.5-flash'),
        'openai-compatible/gemini-3.5-flash',
    );
    assert.equal(
        normalizeModelIdentity('openai-compatible::models/gemini-3.5-flash'),
        'openai-compatible/gemini-3.5-flash',
    );
});

test('quota parser exposes the observed limit and retry window without inventing remaining quota', () => {
    const parsed = parseQuotaError(
        'Quota exceeded for metric: generativelanguage.googleapis.com/'
        + 'generate_content_free_tier_input_token_count, limit: 250000, '
        + 'model: gemini-3.5-flash Please retry in 29.296s.',
    );
    assert.deepEqual(parsed, {
        limit: 250000,
        metric: 'generate_content_free_tier_input_token_count',
        retryAfterSec: 29.296,
    });
});

test('a recent 429 is limited until its observed retry window expires', () => {
    const now = Date.parse('2026-08-14T20:07:50Z');
    const status = deriveModelStatus('openai-compatible::gemini-3.5-flash', [{
        ts: '2026-08-14T20:07:30Z',
        type: 'llm_api_error',
        model: 'openai-compatible::gemini-3.5-flash',
        status_code: 429,
        error_kind: 'provider_transient',
        error: 'Quota exceeded for metric: example/input_token_count, limit: 250000, '
            + 'model: gemini-3.5-flash Please retry in 29s.',
    }], { now });

    assert.equal(status.state, 'limited');
    assert.equal(status.limit, 250000);
    assert.equal(status.metric, 'input_token_count');
    assert.equal(status.resetAt, '2026-08-14T20:07:59.000Z');
});

test('a later successful round proves recovery but never claims numeric quota remaining', () => {
    const status = deriveModelStatus('openai-compatible::gemini-3.5-flash', [{
        ts: '2026-08-14T20:07:30Z', type: 'llm_api_error',
        model: 'openai-compatible::gemini-3.5-flash', status_code: 429,
        error: 'Please retry in 29s.',
    }, {
        ts: '2026-08-14T20:08:36Z', type: 'llm_round',
        model: 'openai-compatible/gemini-3.5-flash',
    }], { now: Date.parse('2026-08-14T20:09:00Z') });

    assert.equal(status.state, 'available');
    assert.equal(status.remaining, null);
    assert.match(status.detail, /last request succeeded/i);
});

test('an expired retry hint becomes unknown rather than a fabricated available state', () => {
    const status = deriveModelStatus('openai-compatible::gemini-3.5-flash', [{
        ts: '2026-08-14T20:07:30Z', type: 'llm_api_error',
        model: 'openai-compatible::gemini-3.5-flash', status_code: 429,
        error: 'Please retry in 10s.',
    }], { now: Date.parse('2026-08-14T20:08:00Z') });
    assert.equal(status.state, 'unknown');
    assert.match(status.detail, /may have reset/i);
});

test('model choices retain configured routes, dedupe the catalog, and add the running local model', () => {
    const choices = buildModelChoices({
        settings: {
            OUROBOROS_MODEL: 'openai-compatible::gemini-3.5-flash',
            OUROBOROS_MODEL_LIGHT: 'openai-compatible::gemini-3.1-flash-lite',
            OUROBOROS_MODEL_FALLBACKS: 'local-model',
            USE_LOCAL_MAIN: false,
            USE_LOCAL_FALLBACK: true,
            OPENAI_COMPATIBLE_BASE_URL: 'https://generativelanguage.googleapis.com/v1beta/openai/',
        },
        catalogItems: [
            {
                value: 'openai-compatible::models/gemini-3.5-flash',
                label: 'OpenAI Compatible · models/gemini-3.5-flash',
            },
            {
                value: 'openai-compatible::models/gemini-embedding-001',
                label: 'OpenAI Compatible · models/gemini-embedding-001',
            },
            {
                value: 'openai-compatible::models/gemini-2.5-computer-use-preview-10-2025',
                label: 'OpenAI Compatible · models/gemini-2.5-computer-use-preview-10-2025',
            },
        ],
        localStatus: {
            status: 'ready',
            model_name: '/models/Qwen3.8-27B-Q4_K_M.gguf',
        },
        events: [],
    });

    assert.deepEqual(choices.map((item) => item.value), [
        '__local__',
        'openai-compatible::gemini-3.5-flash',
        'openai-compatible::gemini-3.1-flash-lite',
    ]);
    assert.equal(choices[0].label, 'Local · Qwen3.8-27B-Q4_K_M');
    assert.equal(choices[0].status.state, 'available');
    assert.equal(choices[1].label, 'Google · Gemini 3.5 Flash');
});

test('selection payload switches the existing main route instead of creating a parallel setting', () => {
    assert.deepEqual(modelSelectionPayload('__local__'), { USE_LOCAL_MAIN: true });
    assert.deepEqual(modelSelectionPayload('openai-compatible::gemini-3.5-flash'), {
        OUROBOROS_MODEL: 'openai-compatible::gemini-3.5-flash',
        USE_LOCAL_MAIN: false,
    });
});
