/**
 * Minimal mobile swipe gestures (v6.82.0 P3): one pure classifier + one binder.
 *
 * Deliberately NOT a gesture framework. The binder recognizes a single
 * release-triggered swipe direction per bound surface and calls an existing
 * state API through `onCommit`; it never toggles classes, `hidden`, ARIA, or
 * any other DOM state itself, and it never touches `element.style`.
 * Touch-action arbitration is owned by stylesheet classes in web/style.css.
 *
 * Out of scope by owner decision: drag-follow/tracking, tab swipes,
 * pull-to-refresh, toast/card swipes, edge-swipe open, multi-touch, mouse
 * gestures, third-party libs.
 */

/**
 * Classify a completed pointer displacement as a swipe.
 *
 * Pure: no DOM access, exported for node tests. A gesture commits either on a
 * deliberate distance (`minDistance`) or on a shorter, fast fling
 * (`minFlingDistance` + `minVelocity` px/ms). The winning axis must dominate
 * the other by `dominance` so diagonal scribbles stay null.
 *
 * @returns {'left'|'right'|'up'|'down'|null}
 */
export function classifySwipe({ dx, dy, elapsedMs } = {}, {
    minDistance = 52,
    minFlingDistance = 24,
    minVelocity = 0.45,
    dominance = 1.25,
} = {}) {
    const x = Number(dx);
    const y = Number(dy);
    const elapsed = Number(elapsedMs);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    if (!Number.isFinite(elapsed) || elapsed < 0) return null;
    const adx = Math.abs(x);
    const ady = Math.abs(y);
    let axis = null;
    if (adx >= ady * dominance && adx > 0) axis = 'x';
    else if (ady >= adx * dominance && ady > 0) axis = 'y';
    if (!axis) return null;
    const distance = axis === 'x' ? adx : ady;
    // Distance path is time-independent; the fling path uses velocity with the
    // elapsed time floored at 1ms so synthetic same-tick sequences stay sane.
    const velocity = distance / Math.max(elapsed, 1);
    const committed = distance >= minDistance
        || (distance >= minFlingDistance && velocity >= minVelocity);
    if (!committed) return null;
    if (axis === 'x') return x < 0 ? 'left' : 'right';
    return y < 0 ? 'up' : 'down';
}

/**
 * True when a swipe must not START on `target` (pure, exported for tests).
 *
 * Refuses form/editing surfaces (their touch semantics — caret placement,
 * selection handles — must win) and any non-collapsed text selection
 * (same philosophy as chat.js::liveLineRowToggleKey: an active selection is
 * an in-progress interaction a gesture must never steal).
 */
export function swipeStartRefused(target, selection = null) {
    if (!target) return true;
    if (target.closest?.('input, textarea, select, [contenteditable]')) return true;
    if (selection && !selection.isCollapsed) return true;
    return false;
}

/**
 * True when `target` has an ancestor INSIDE the bound surface (root excluded)
 * that natively scrolls on the gesture axis. Pure walk, exported for tests:
 * `getStyle` defaults to live computed style and is injectable for stubs.
 *
 * This is a conservative yield — native scrolling wins every ambiguous
 * contest. It complements (never replaces) the stylesheet `touch-action`
 * boundary, which the browser resolves before JS sees the gesture.
 */
export function hasSameAxisScrollableAncestor(target, root, axis, getStyle = (el) => getComputedStyle(el)) {
    let el = target;
    while (el && el !== root) {
        const style = typeof el.scrollWidth === 'number' || typeof el.scrollHeight === 'number'
            ? getStyle(el)
            : null;
        if (style) {
            if (axis === 'x') {
                const overflow = style.overflowX;
                if ((overflow === 'auto' || overflow === 'scroll') && el.scrollWidth > el.clientWidth) return true;
            } else {
                const overflow = style.overflowY;
                if ((overflow === 'auto' || overflow === 'scroll') && el.scrollHeight > el.clientHeight) return true;
            }
        }
        el = el.parentElement;
    }
    return false;
}

const AXIS_BY_DIRECTION = { left: 'x', right: 'x', up: 'y', down: 'y' };

/**
 * Bind one release-triggered swipe direction to `root`. Returns an unbind fn.
 *
 * Binder contract (design spec, all mandatory):
 * - Pointer Events only; primary touch pointers only (mouse/pen ignored).
 * - Start recorded on pointerdown; the DECISION happens only on pointerup.
 *   The accepted touch pointer is explicitly captured, so the matching
 *   pointerup reaches `root` even if the finger leaves the narrow surface.
 * - pointercancel / lostpointercapture reset the pending start, nothing else.
 * - `enabled()` gates both the start and (re-checked) the commit.
 * - Starts are refused on editing surfaces, over a non-collapsed selection,
 *   and when the start target sits in a same-axis scroller inside `root`.
 * - After a committed swipe a capture-phase click suppressor SCOPED to that
 *   gesture is armed on the document, removed by the matching click or a
 *   ~400ms timeout — never left standing (a swipe released over a drawer nav
 *   row must not fire that row's synthetic click).
 * - `onCommit` calls existing app state APIs; this module owns no DOM state.
 */
export function bindSwipe(root, { direction, enabled = () => true, onCommit } = {}) {
    const axis = AXIS_BY_DIRECTION[direction];
    if (!root || !axis || typeof onCommit !== 'function') return () => {};

    const doc = root.ownerDocument || null;
    const view = doc?.defaultView || null;
    const now = () => (typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now());

    let start = null;
    let disarmClickSuppressor = null;

    function currentSelection() {
        try {
            return view?.getSelection ? view.getSelection() : null;
        } catch {
            return null;
        }
    }

    function armClickSuppressor() {
        if (!doc) return;
        disarmClickSuppressor?.();
        let timer = 0;
        const onClick = (event) => {
            // Swallow only the committed gesture's own synthetic click (it
            // always targets a descendant of the bound surface); an unrelated
            // tap elsewhere during the timeout window passes through untouched.
            const inRoot = Boolean(event.target)
                && typeof root.contains === 'function'
                && root.contains(event.target);
            if (!inRoot) return;
            event.stopPropagation();
            event.preventDefault();
            disarm();
        };
        const disarm = () => {
            doc.removeEventListener('click', onClick, true);
            clearTimeout(timer);
            if (disarmClickSuppressor === disarm) disarmClickSuppressor = null;
        };
        doc.addEventListener('click', onClick, true);
        timer = setTimeout(disarm, 400);
        disarmClickSuppressor = disarm;
    }

    function onPointerDown(event) {
        // A new contact always invalidates any stale gesture state, even when
        // this pointerdown is refused below — otherwise a filtered start could
        // pair with a later pointerup (iOS reuses touch pointerIds) and
        // phantom-commit across two unrelated touches.
        start = null;
        if (event.pointerType !== 'touch' || !event.isPrimary) return;
        if (!enabled()) return;
        if (swipeStartRefused(event.target, currentSelection())) return;
        if (hasSameAxisScrollableAncestor(event.target, root, axis)) return;
        start = {
            pointerId: event.pointerId,
            x: event.clientX,
            y: event.clientY,
            t: now(),
        };
        // Do not rely only on the browser's implicit touch capture. Explicit
        // capture keeps release-triggered gestures deterministic on narrow
        // headers and in emulated/mobile WebViews where the pointer target may
        // otherwise move to the panel body before pointerup.
        try { root.setPointerCapture?.(event.pointerId); } catch {}
    }

    function onPointerUp(event) {
        if (!start || event.pointerId !== start.pointerId) return;
        const pointerId = start.pointerId;
        const gesture = {
            dx: event.clientX - start.x,
            dy: event.clientY - start.y,
            elapsedMs: now() - start.t,
        };
        start = null;
        try { root.releasePointerCapture?.(pointerId); } catch {}
        if (classifySwipe(gesture) !== direction) return;
        // Re-check at release: the surface may have been disabled (keyboard
        // opened, breakpoint crossed) and a drag can create a selection.
        if (!enabled()) return;
        const selection = currentSelection();
        if (selection && !selection.isCollapsed) return;
        armClickSuppressor();
        onCommit();
    }

    function onPointerReset(event) {
        if (!start || event.pointerId !== start.pointerId) return;
        const pointerId = start.pointerId;
        start = null;
        try { root.releasePointerCapture?.(pointerId); } catch {}
    }

    root.addEventListener('pointerdown', onPointerDown);
    root.addEventListener('pointerup', onPointerUp);
    root.addEventListener('pointercancel', onPointerReset);
    root.addEventListener('lostpointercapture', onPointerReset);

    return function unbind() {
        root.removeEventListener('pointerdown', onPointerDown);
        root.removeEventListener('pointerup', onPointerUp);
        root.removeEventListener('pointercancel', onPointerReset);
        root.removeEventListener('lostpointercapture', onPointerReset);
        const pointerId = start?.pointerId;
        start = null;
        if (pointerId != null) {
            try { root.releasePointerCapture?.(pointerId); } catch {}
        }
        disarmClickSuppressor?.();
    };
}
