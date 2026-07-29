import assert from 'node:assert/strict';
import test from 'node:test';

import {
    bindSwipe,
    classifySwipe,
    hasSameAxisScrollableAncestor,
    swipeStartRefused,
} from '../modules/gestures.js';

// ---------------------------------------------------------------------------
// classifySwipe — pure threshold/dominance/fling decisions
// ---------------------------------------------------------------------------

test('deliberate horizontal distance commits left/right', () => {
    assert.equal(classifySwipe({ dx: -60, dy: 4, elapsedMs: 300 }), 'left');
    assert.equal(classifySwipe({ dx: 60, dy: -4, elapsedMs: 300 }), 'right');
});

test('deliberate vertical distance commits up/down', () => {
    assert.equal(classifySwipe({ dx: 3, dy: -70, elapsedMs: 300 }), 'up');
    assert.equal(classifySwipe({ dx: -3, dy: 70, elapsedMs: 300 }), 'down');
});

test('a slow drag below min distance does not commit', () => {
    // 40px < 52px and 40/500 = 0.08 px/ms < 0.45 → not a fling either.
    assert.equal(classifySwipe({ dx: -40, dy: 0, elapsedMs: 500 }), null);
});

test('a short fast fling commits below the distance threshold', () => {
    // 30px >= 24px and 30/50 = 0.6 px/ms >= 0.45.
    assert.equal(classifySwipe({ dx: -30, dy: 2, elapsedMs: 50 }), 'left');
    assert.equal(classifySwipe({ dx: 1, dy: 30, elapsedMs: 50 }), 'down');
});

test('a fling below the fling distance floor does not commit', () => {
    // Fast but only 20px < 24px.
    assert.equal(classifySwipe({ dx: -20, dy: 0, elapsedMs: 20 }), null);
});

test('diagonal movement without axis dominance is null', () => {
    // |dx|/|dy| = 60/55 ≈ 1.09 < 1.25 dominance.
    assert.equal(classifySwipe({ dx: -60, dy: -55, elapsedMs: 100 }), null);
    assert.equal(classifySwipe({ dx: 55, dy: 60, elapsedMs: 100 }), null);
});

test('dominance is exactly at the boundary → the axis wins', () => {
    // |dx| = 1.25 * |dy| satisfies "≥ dominance".
    assert.equal(classifySwipe({ dx: -55, dy: 44, elapsedMs: 100 }), 'left');
});

test('a stationary tap is null', () => {
    assert.equal(classifySwipe({ dx: 0, dy: 0, elapsedMs: 80 }), null);
});

test('non-finite or negative inputs are null', () => {
    assert.equal(classifySwipe({ dx: NaN, dy: 0, elapsedMs: 100 }), null);
    assert.equal(classifySwipe({ dx: -80, dy: 0, elapsedMs: NaN }), null);
    assert.equal(classifySwipe({ dx: -80, dy: 0, elapsedMs: -5 }), null);
    assert.equal(classifySwipe({}), null);
    assert.equal(classifySwipe(), null);
});

test('same-tick synthetic dispatch (elapsedMs 0) still honors the distance path', () => {
    assert.equal(classifySwipe({ dx: -60, dy: 0, elapsedMs: 0 }), 'left');
});

test('custom thresholds are honored', () => {
    const opts = { minDistance: 100, minFlingDistance: 90, minVelocity: 2 };
    assert.equal(classifySwipe({ dx: -95, dy: 0, elapsedMs: 100 }, opts), null);
    assert.equal(classifySwipe({ dx: -120, dy: 0, elapsedMs: 400 }, opts), 'left');
});

// ---------------------------------------------------------------------------
// swipeStartRefused — editing-surface and selection guards (stub targets,
// live_line_disclosure.test.js style: just enough of closest()).
// ---------------------------------------------------------------------------

function makeTarget({ editableSelectorHit = false } = {}) {
    return {
        closest(selector) {
            assert.equal(selector, 'input, textarea, select, [contenteditable]');
            return editableSelectorHit ? {} : null;
        },
    };
}

test('a plain target with a collapsed selection is allowed', () => {
    assert.equal(swipeStartRefused(makeTarget(), { isCollapsed: true }), false);
    assert.equal(swipeStartRefused(makeTarget(), null), false);
});

test('a start inside input/textarea/select/[contenteditable] is refused', () => {
    assert.equal(swipeStartRefused(makeTarget({ editableSelectorHit: true }), null), true);
});

test('a non-collapsed selection refuses the start', () => {
    assert.equal(swipeStartRefused(makeTarget(), { isCollapsed: false }), true);
});

test('a missing target is refused', () => {
    assert.equal(swipeStartRefused(null, null), true);
});

// ---------------------------------------------------------------------------
// hasSameAxisScrollableAncestor — conservative scroller yield (stub chain)
// ---------------------------------------------------------------------------

function makeChain(root, nodes) {
    // nodes: innermost-first list of {overflowX, overflowY, scrollWidth,
    // clientWidth, scrollHeight, clientHeight}; parents link toward root.
    let parent = root;
    let innermost = null;
    for (let i = nodes.length - 1; i >= 0; i -= 1) {
        const el = { ...nodes[i], parentElement: parent };
        parent = el;
        innermost = el;
    }
    return innermost;
}

const plain = {
    overflowX: 'visible', overflowY: 'visible',
    scrollWidth: 100, clientWidth: 100, scrollHeight: 100, clientHeight: 100,
};
const getStyle = (el) => ({ overflowX: el.overflowX, overflowY: el.overflowY });

test('an x-overflowing auto scroller between target and root yields the x axis', () => {
    const root = { ...plain };
    const target = makeChain(root, [
        { ...plain, overflowX: 'auto', scrollWidth: 400, clientWidth: 200 },
        { ...plain },
    ]);
    assert.equal(hasSameAxisScrollableAncestor(target, root, 'x', getStyle), true);
    // ...but does not block the ORTHOGONAL axis.
    assert.equal(hasSameAxisScrollableAncestor(target, root, 'y', getStyle), false);
});

test('a y scroller yields the y axis only', () => {
    const root = { ...plain };
    const target = makeChain(root, [
        { ...plain, overflowY: 'scroll', scrollHeight: 900, clientHeight: 300 },
    ]);
    assert.equal(hasSameAxisScrollableAncestor(target, root, 'y', getStyle), true);
    assert.equal(hasSameAxisScrollableAncestor(target, root, 'x', getStyle), false);
});

test('overflow visible/hidden never yields even when content overflows', () => {
    const root = { ...plain };
    const target = makeChain(root, [
        { ...plain, overflowX: 'visible', scrollWidth: 500, clientWidth: 100 },
        { ...plain, overflowX: 'hidden', scrollWidth: 500, clientWidth: 100 },
    ]);
    assert.equal(hasSameAxisScrollableAncestor(target, root, 'x', getStyle), false);
});

test('a scroller with no actual overflow does not yield', () => {
    const root = { ...plain };
    const target = makeChain(root, [
        { ...plain, overflowX: 'auto', scrollWidth: 200, clientWidth: 200 },
    ]);
    assert.equal(hasSameAxisScrollableAncestor(target, root, 'x', getStyle), false);
});

test('the walk stops at the bound root (root itself is not inspected)', () => {
    const root = { ...plain, overflowX: 'auto', scrollWidth: 999, clientWidth: 1 };
    const target = makeChain(root, [{ ...plain }]);
    assert.equal(hasSameAxisScrollableAncestor(target, root, 'x', getStyle), false);
});

test('target === root walks nothing', () => {
    const root = { ...plain, overflowX: 'auto', scrollWidth: 999, clientWidth: 1 };
    assert.equal(hasSameAxisScrollableAncestor(root, root, 'x', getStyle), false);
});

// ---------------------------------------------------------------------------
// bindSwipe — binder wiring against a minimal EventTarget stub (no jsdom).
// Verifies: touch-only, decide-on-pointerup, enabled() re-check, cancel reset,
// scoped click suppression, unbind.
// ---------------------------------------------------------------------------

function makeStubDoc() {
    const listeners = [];
    return {
        listeners,
        addEventListener(type, fn, capture) { listeners.push({ type, fn, capture }); },
        removeEventListener(type, fn) {
            const idx = listeners.findIndex((l) => l.type === type && l.fn === fn);
            if (idx >= 0) listeners.splice(idx, 1);
        },
        defaultView: { getSelection: () => ({ isCollapsed: true }) },
    };
}

function makeStubRoot() {
    const handlers = new Map();
    const doc = makeStubDoc();
    const captured = new Set();
    const root = {
        ownerDocument: doc,
        doc,
        handlers,
        captured,
        addEventListener(type, fn) { handlers.set(type, fn); },
        removeEventListener(type) { handlers.delete(type); },
        dispatch(type, event) { handlers.get(type)?.(event); },
        contains(node) { return Boolean(node && node._insideRoot); },
        setPointerCapture(pointerId) { captured.add(pointerId); },
        releasePointerCapture(pointerId) { captured.delete(pointerId); },
    };
    return root;
}

const targetOutsideScrollers = { closest: () => null, parentElement: null };

function touchEvent(overrides = {}) {
    return {
        pointerType: 'touch',
        isPrimary: true,
        pointerId: 7,
        clientX: 300,
        clientY: 400,
        target: targetOutsideScrollers,
        ...overrides,
    };
}

test('binder commits a matching swipe on pointerup and arms a scoped click suppressor', () => {
    const root = makeStubRoot();
    let commits = 0;
    const unbind = bindSwipe(root, { direction: 'left', onCommit: () => { commits += 1; } });
    root.dispatch('pointerdown', touchEvent());
    assert.deepEqual([...root.captured], [7]);
    root.dispatch('pointerup', touchEvent({ clientX: 180, clientY: 404 }));
    assert.equal(commits, 1);
    assert.equal(root.captured.size, 0);
    // A capture-phase click suppressor is armed on the document...
    assert.equal(root.doc.listeners.length, 1);
    assert.equal(root.doc.listeners[0].type, 'click');
    assert.equal(root.doc.listeners[0].capture, true);
    // ...an unrelated click OUTSIDE the bound surface passes through untouched...
    let stopped = 0;
    root.doc.listeners[0].fn({ target: {}, stopPropagation: () => { stopped += 1; }, preventDefault: () => {} });
    assert.equal(stopped, 0);
    assert.equal(root.doc.listeners.length, 1);
    // ...while the gesture's own synthetic click (inside root) is swallowed exactly once, then disarmed.
    root.doc.listeners[0].fn({ target: { _insideRoot: true }, stopPropagation: () => { stopped += 1; }, preventDefault: () => {} });
    assert.equal(stopped, 1);
    assert.equal(root.doc.listeners.length, 0);
    unbind();
});

test('binder ignores mouse/pen and non-primary pointers', () => {
    const root = makeStubRoot();
    let commits = 0;
    bindSwipe(root, { direction: 'left', onCommit: () => { commits += 1; } });
    root.dispatch('pointerdown', touchEvent({ pointerType: 'mouse' }));
    root.dispatch('pointerup', touchEvent({ pointerType: 'mouse', clientX: 180 }));
    root.dispatch('pointerdown', touchEvent({ isPrimary: false }));
    root.dispatch('pointerup', touchEvent({ isPrimary: false, clientX: 180 }));
    assert.equal(commits, 0);
});

test('wrong direction and sub-threshold movement do not commit or suppress clicks', () => {
    const root = makeStubRoot();
    let commits = 0;
    bindSwipe(root, { direction: 'left', onCommit: () => { commits += 1; } });
    // Right swipe on a left binding.
    root.dispatch('pointerdown', touchEvent());
    root.dispatch('pointerup', touchEvent({ clientX: 420 }));
    // Short drag (tap-ish): the eventual click must stay allowed.
    root.dispatch('pointerdown', touchEvent());
    root.dispatch('pointerup', touchEvent({ clientX: 290 }));
    assert.equal(commits, 0);
    assert.equal(root.doc.listeners.length, 0);
});

test('pointercancel resets the pending start', () => {
    const root = makeStubRoot();
    let commits = 0;
    bindSwipe(root, { direction: 'left', onCommit: () => { commits += 1; } });
    root.dispatch('pointerdown', touchEvent());
    assert.deepEqual([...root.captured], [7]);
    root.dispatch('pointercancel', touchEvent());
    assert.equal(root.captured.size, 0);
    root.dispatch('pointerup', touchEvent({ clientX: 180 }));
    assert.equal(commits, 0);
});

test('enabled() gates the start and is re-checked before commit', () => {
    const root = makeStubRoot();
    let commits = 0;
    let enabled = true;
    bindSwipe(root, {
        direction: 'left',
        enabled: () => enabled,
        onCommit: () => { commits += 1; },
    });
    enabled = false;
    root.dispatch('pointerdown', touchEvent());
    root.dispatch('pointerup', touchEvent({ clientX: 180 }));
    assert.equal(commits, 0);
    // Disabled mid-gesture (e.g. keyboard opened): start allowed, commit refused.
    enabled = true;
    root.dispatch('pointerdown', touchEvent());
    enabled = false;
    root.dispatch('pointerup', touchEvent({ clientX: 180 }));
    assert.equal(commits, 0);
});

test('starts inside editing surfaces are refused by the binder', () => {
    const root = makeStubRoot();
    let commits = 0;
    bindSwipe(root, { direction: 'left', onCommit: () => { commits += 1; } });
    const editableTarget = { closest: () => ({}), parentElement: null };
    root.dispatch('pointerdown', touchEvent({ target: editableTarget }));
    root.dispatch('pointerup', touchEvent({ clientX: 180 }));
    assert.equal(commits, 0);
});

test('unbind removes every listener and future gestures are inert', () => {
    const root = makeStubRoot();
    let commits = 0;
    const unbind = bindSwipe(root, { direction: 'left', onCommit: () => { commits += 1; } });
    assert.equal(root.handlers.size, 4);
    unbind();
    assert.equal(root.handlers.size, 0);
    root.dispatch('pointerdown', touchEvent());
    root.dispatch('pointerup', touchEvent({ clientX: 180 }));
    assert.equal(commits, 0);
});

test('bindSwipe with a bad direction or missing onCommit is a safe no-op', () => {
    const root = makeStubRoot();
    const unbind = bindSwipe(root, { direction: 'diagonal', onCommit: () => {} });
    assert.equal(typeof unbind, 'function');
    assert.equal(root.handlers.size, 0);
    unbind();
    assert.equal(typeof bindSwipe(root, { direction: 'left' }), 'function');
});
