import assert from 'node:assert/strict';
import test from 'node:test';

import {
    computeDerivedChatStatus,
    computeHydratedDirectActivities,
} from '../modules/chat.js';

test('computeDerivedChatStatus: offline state when ws is disconnected', () => {
    const status = computeDerivedChatStatus({
        isConnected: false,
        hasActiveLiveCard: true,
        activeDirectCount: 1,
        pendingSubmissionsCount: 1,
    });
    assert.deepEqual(status, {
        kind: 'offline',
        text: 'Reconnecting...',
        showDots: false,
    });
});

test('computeDerivedChatStatus: working state when active live card is present', () => {
    const status = computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: true,
        activeDirectCount: 0,
        pendingSubmissionsCount: 0,
    });
    assert.deepEqual(status, {
        kind: 'thinking',
        text: 'Working...',
        showDots: false,
    });
});

test('computeDerivedChatStatus: thinking state with dots when direct activities are active', () => {
    const status = computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 1,
        pendingSubmissionsCount: 0,
    });
    assert.deepEqual(status, {
        kind: 'thinking',
        text: 'Thinking...',
        showDots: true,
    });
});

test('computeDerivedChatStatus: sending state with dots when local submissions are pending', () => {
    const status = computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 1,
    });
    assert.deepEqual(status, {
        kind: 'thinking',
        text: 'Sending...',
        showDots: true,
    });
});

test('computeDerivedChatStatus: attention state when terminal phase failed', () => {
    const status = computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 0,
        lastTerminalAttention: true,
    });
    assert.deepEqual(status, {
        kind: 'error',
        text: 'Attention',
        showDots: false,
    });
});

test('computeDerivedChatStatus: online idle state by default', () => {
    const status = computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 0,
        lastTerminalAttention: false,
    });
    assert.deepEqual(status, {
        kind: 'online',
        text: 'Online',
        showDots: false,
    });
});

test('computeHydratedDirectActivities: filters turns by chatId', () => {
    const turns = [
        { activity_id: 'act-main-1', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
        { activity_id: 'act-proj-2', chat_id: 2, kind: 'ephemeral_decision', phase: 'thinking' },
        { activity_id: 'act-main-2', chat_id: 1, kind: 'ephemeral_decision', phase: 'thinking' },
    ];

    const mapChat1 = computeHydratedDirectActivities(new Map(), turns, 1);
    assert.equal(mapChat1.size, 2);
    assert.ok(mapChat1.has('act-main-1'));
    assert.ok(mapChat1.has('act-main-2'));
    assert.ok(!mapChat1.has('act-proj-2'));

    const mapChat2 = computeHydratedDirectActivities(new Map(), turns, 2);
    assert.equal(mapChat2.size, 1);
    assert.ok(mapChat2.has('act-proj-2'));
});

test('computeHydratedDirectActivities: removes completed activities not in snapshot', () => {
    const initialMap = new Map([
        ['act-old', { activityId: 'act-old', kind: 'direct_chat', phase: 'thinking' }],
        ['act-keep', { activityId: 'act-keep', kind: 'direct_chat', phase: 'thinking' }],
    ]);

    const turns = [
        { activity_id: 'act-keep', chat_id: 1, kind: 'direct_chat', phase: 'working' },
        { activity_id: 'act-new', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
    ];

    const updated = computeHydratedDirectActivities(initialMap, turns, 1);
    assert.equal(updated.size, 2);
    assert.ok(!updated.has('act-old'));
    assert.ok(updated.has('act-keep'));
    assert.equal(updated.get('act-keep').phase, 'working');
    assert.ok(updated.has('act-new'));
});

test('computeHydratedDirectActivities: a stale snapshot never wipes an activity registered after the snapshot was requested', () => {
    const snapshotRequestedAt = 1_000_000;
    const initialMap = new Map([
        // Registered by a WS typing frame AFTER the /api/state request went out:
        // the (stale) empty snapshot has no authority over it.
        ['act-fresh', { activityId: 'act-fresh', kind: 'direct_chat', phase: 'thinking', startedAt: snapshotRequestedAt + 50 }],
        // Existed before the snapshot was requested and is absent from it: done.
        ['act-stale', { activityId: 'act-stale', kind: 'direct_chat', phase: 'thinking', startedAt: snapshotRequestedAt - 50 }],
    ]);

    const updated = computeHydratedDirectActivities(initialMap, [], 1, snapshotRequestedAt);
    assert.equal(updated.size, 1);
    assert.ok(updated.has('act-fresh'));
    assert.ok(!updated.has('act-stale'));

    // Without a barrier (default Infinity) the snapshot wipes both — legacy behavior.
    const legacy = computeHydratedDirectActivities(initialMap, [], 1);
    assert.equal(legacy.size, 0);
});

test('computeHydratedDirectActivities: startedAt stays client-clock (snapshot server time never enters the barrier)', () => {
    const clientObservedAt = 5_000;
    const initialMap = new Map([
        ['act-1', { activityId: 'act-1', kind: 'direct_chat', phase: 'thinking', startedAt: clientObservedAt }],
    ]);
    // Snapshot lists the same activity with a (skewed) server-clock started_at.
    const turns = [
        { activity_id: 'act-1', chat_id: 1, kind: 'direct_chat', phase: 'working', started_at: 99_999_999 },
    ];
    const updated = computeHydratedDirectActivities(initialMap, turns, 1, 10_000);
    assert.equal(updated.get('act-1').startedAt, clientObservedAt);
    assert.equal(updated.get('act-1').phase, 'working');
});

test('computeHydratedDirectActivities: correctly handles chat_id=0 without coercing to 1', () => {
    const turns = [
        { activity_id: 'act-sys-0', chat_id: 0, kind: 'direct_chat', phase: 'thinking' },
        { activity_id: 'act-main-1', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
    ];

    const mapChat0 = computeHydratedDirectActivities(new Map(), turns, 0);
    assert.equal(mapChat0.size, 1);
    assert.ok(mapChat0.has('act-sys-0'));

    const mapChat1 = computeHydratedDirectActivities(new Map(), turns, 1);
    assert.equal(mapChat1.size, 1);
    assert.ok(mapChat1.has('act-main-1'));
});

test('computeHydratedDirectActivities: snapshot has no deletion authority over managed-task typing entries (no kind stamp)', () => {
    const initialMap = new Map([
        // Queued managed task's typing frame carries no kind: the direct
        // registry does not track it, so an (empty) snapshot must not end it.
        ['managed-1', { activityId: 'managed-1', kind: '', phase: 'thinking', startedAt: 100 }],
        // Registry-tracked direct turn absent from the snapshot: concluded.
        ['direct-1', { activityId: 'direct-1', kind: 'direct_chat', phase: 'thinking', startedAt: 100 }],
        ['eph-1', { activityId: 'eph-1', kind: 'ephemeral_decision', phase: 'thinking', startedAt: 100 }],
    ]);

    const updated = computeHydratedDirectActivities(initialMap, [], 1, 1_000);
    assert.equal(updated.size, 1);
    assert.ok(updated.has('managed-1'));
    assert.ok(!updated.has('direct-1'));
    assert.ok(!updated.has('eph-1'));
});

test('computeHydratedDirectActivities: a concluded turn is never resurrected by a snapshot captured while it still ran', () => {
    const concluded = new Map([['act-done', 12345]]);
    // Snapshot was requested mid-turn, its response arrived AFTER the turn's
    // keyed final concluded the activity client-side (project panels hydrate
    // once at creation, so without the ledger this insert would be permanent).
    const turns = [
        { activity_id: 'act-done', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
        { activity_id: 'act-live', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
    ];
    const updated = computeHydratedDirectActivities(new Map(), turns, 1, Infinity, concluded);
    assert.equal(updated.size, 1);
    assert.ok(!updated.has('act-done'));
    assert.ok(updated.has('act-live'));
});

test('computeDerivedChatStatus: priority order is preserved (offline > live card > direct thinking > sending > attention > online)', () => {
    // 1. Disconnected beats everything
    assert.equal(computeDerivedChatStatus({
        isConnected: false,
        hasActiveLiveCard: true,
        activeDirectCount: 5,
        pendingSubmissionsCount: 3,
        lastTerminalAttention: true,
    }).text, 'Reconnecting...');

    // 2. Active live card beats direct thinking & sending & attention
    assert.equal(computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: true,
        activeDirectCount: 5,
        pendingSubmissionsCount: 3,
        lastTerminalAttention: true,
    }).text, 'Working...');

    // 3. Direct thinking beats local pending submissions & attention
    assert.equal(computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 2,
        pendingSubmissionsCount: 3,
        lastTerminalAttention: true,
    }).text, 'Thinking...');

    // 4. Local pending submissions beat terminal attention
    assert.equal(computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 1,
        lastTerminalAttention: true,
    }).text, 'Sending...');

    // 5. Terminal attention beats default online
    assert.equal(computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 0,
        lastTerminalAttention: true,
    }).text, 'Attention');

    // 6. Clean idle state
    assert.equal(computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 0,
        lastTerminalAttention: false,
    }).text, 'Online');
});
