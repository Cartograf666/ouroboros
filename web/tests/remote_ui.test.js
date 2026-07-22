import assert from 'node:assert/strict';
import test from 'node:test';

import { activeProfileId, pillLabel, pillVisible, remoteBridge } from '../modules/remote.js';

test('pillVisible hides the pill only when local/disconnected', () => {
    assert.equal(pillVisible({ state: 'disconnected' }), false);
    assert.equal(pillVisible({}), false);
    for (const state of ['connecting', 'connected', 'reconnecting', 'gave_up', 'error']) {
        assert.equal(pillVisible({ state }), true, state);
    }
});

test('pillLabel names the profile and reflects the connection state', () => {
    assert.equal(pillLabel({ state: 'connected', profile_name: 'prod' }), 'Remote: prod');
    assert.equal(pillLabel({ state: 'reconnecting', profile_name: 'prod' }), 'Reconnecting: prod…');
    assert.equal(pillLabel({ state: 'gave_up', profile_id: 'p1' }), 'Remote lost: p1');
    assert.equal(pillLabel({ state: 'disconnected' }), '');
});

test('activeProfileId is empty for gave_up/error so Settings keeps Connect', () => {
    // C4: pillVisible is true for gave_up/error (pill still shown), but the
    // Settings list must NOT treat those as the connected profile.
    for (const state of ['connecting', 'connected', 'reconnecting']) {
        assert.equal(activeProfileId({ state, profile_id: 'p1' }), 'p1', state);
    }
    for (const state of ['gave_up', 'error', 'disconnected']) {
        assert.equal(activeProfileId({ state, profile_id: 'p1' }), '', state);
        assert.equal(pillVisible({ state }), state !== 'disconnected', state); // still visible (except disconnected)
    }
});

test('remoteBridge is null without a pywebview bridge', () => {
    const priorWindow = globalThis.window;
    globalThis.window = {};
    assert.equal(remoteBridge(), null);
    globalThis.window = { pywebview: { api: {} } };
    assert.equal(remoteBridge(), null); // no remote_status → not a remote-capable bridge
    globalThis.window = { pywebview: { api: { remote_status() {} } } };
    assert.notEqual(remoteBridge(), null);
    globalThis.window = priorWindow;
});
