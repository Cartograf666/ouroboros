import test from 'node:test';
import assert from 'node:assert/strict';

import {
    WIDGET_FRAME_DEFAULT_HEIGHT,
    WIDGET_FRAME_MAX_HEIGHT,
} from '../modules/widgets.js';
import {
    classifyWidgetJobStatus,
    isRetryableWidgetError,
    readWidgetJobStatus,
    withWidgetRequestTimeout,
} from '../modules/widget_job.js';

test('widget frame contract keeps the bounded host geometry', () => {
    assert.equal(WIDGET_FRAME_DEFAULT_HEIGHT, 320);
    assert.equal(WIDGET_FRAME_MAX_HEIGHT, 8192);
});

test('widget job retry classification distinguishes transport from terminal errors', () => {
    assert.equal(isRetryableWidgetError({ status: 408 }), true);
    assert.equal(isRetryableWidgetError({ status: 429 }), true);
    assert.equal(isRetryableWidgetError({ status: 503 }), true);
    assert.equal(isRetryableWidgetError({ name: 'TypeError' }), true);
    assert.equal(isRetryableWidgetError({ status: 400 }), false);
    assert.equal(isRetryableWidgetError({ status: 404 }), false);
    assert.equal(isRetryableWidgetError({ status: 200, retryable: false }), false);
    assert.equal(isRetryableWidgetError({ name: 'AbortError', retryable: true }), false);
});

test('widget jobs bound unknown status and reject a missing status', () => {
    assert.equal(classifyWidgetJobStatus('queued'), 'pending');
    assert.equal(classifyWidgetJobStatus('running'), 'pending');
    assert.equal(classifyWidgetJobStatus('done'), 'success');
    assert.equal(classifyWidgetJobStatus('failed'), 'failure');
    assert.equal(classifyWidgetJobStatus(''), 'invalid');
    assert.equal(classifyWidgetJobStatus(123), 'invalid');
    assert.equal(classifyWidgetJobStatus({}), 'invalid');
    assert.equal(classifyWidgetJobStatus([]), 'invalid');
    assert.equal(classifyWidgetJobStatus('mystery'), 'pending');
});

test('widget job status selection preserves explicit falsy status values', () => {
    assert.equal(readWidgetJobStatus({ status: 0, state: 'running' }), 0);
    assert.equal(readWidgetJobStatus({ status: false, state: 'running' }), false);
    assert.equal(readWidgetJobStatus({ status: '', state: 'running' }), '');
    assert.equal(readWidgetJobStatus({ state: 'running' }), 'running');
});

test('widget request timeout aborts the request and remains retryable', async () => {
    const controller = new AbortController();
    await assert.rejects(
        withWidgetRequestTimeout(
            (signal) => new Promise((_, reject) => {
                signal.addEventListener('abort', () => {
                    const error = new Error('aborted');
                    error.name = 'AbortError';
                    reject(error);
                }, { once: true });
            }),
            controller,
            5,
        ),
        (error) => error.code === 'WIDGET_REQUEST_TIMEOUT' && error.retryable === true,
    );
    assert.equal(controller.signal.aborted, true);
});

test('widget request timeout stays terminal when the task swallows abort', async () => {
    const controller = new AbortController();
    await assert.rejects(
        withWidgetRequestTimeout(
            () => new Promise((resolve) => setTimeout(() => resolve('late result'), 20)),
            controller,
            5,
        ),
        (error) => error.code === 'WIDGET_REQUEST_TIMEOUT' && error.retryable === true,
    );
    assert.equal(controller.signal.aborted, true);
});
