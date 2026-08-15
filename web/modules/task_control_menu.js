// S3 (Q2/HQ1): the shared three-action task stop/hurry control.
//
// One dropdown of exactly three owner-decided actions — "Wrap up"
// (soft finalize-then-stop), "Hurry up" (typed task-local hurry control,
// NO chat message ever), "Stop now" (hard stop). Dismissing the
// menu continues the run (the dismiss affordance replaced the old separate
// "keep running" confirm). While a cancel intent is already pending, the only
// offered action is "Stop now" — the monotonic escalation of the
// SAME durable stop intent; "Hurry up" is refused then and never offered.
//
// Chat live cards and the Activity tab consume the SAME module (owner
// product-wide parity), so eligibility gates differ per surface but the
// actions, endpoint bindings, request-id retry, and refusals do not.

import { cancelTask, hurryTask } from './api_client.js';
import { showToast } from './toast.js';

export const ACTION_FINALIZE = 'finalize';
export const ACTION_HURRY = 'hurry';
export const ACTION_STOP_NOW = 'stop_now';

// Logical slots that may host multiple independent cycles (v6.82: shared so
// control eligibility and the chat card layer read the same truth).
export const REUSABLE_TASK_IDS = new Set(['bg-consciousness', 'active']);

/**
 * v6.82 (P5): may this live card offer the stop/hurry control?
 * Card shape alone cannot answer it — an in-process direct-chat turn mints an
 * ordinary non-reusable, non-subagent card (supervisor/workers.py builds it a
 * real uuid task id) yet has no queue entry to cancel. So eligibility requires
 * the supervisor's host-attested `cancelable` progress-meta marker on top of
 * the structural gates: a ROOT (non-subagent) pooled card, not a reusable slot,
 * not finished, not converted into a project chip.
 */
export function cancelRunEligibility({
    groupId = '', isSubagent = false, finished = false, cancelable = false, converted = false,
} = {}) {
    return Boolean(cancelable)
        && !isSubagent
        && !finished
        && !converted
        && Boolean(String(groupId || '').trim())
        && !REUSABLE_TASK_IDS.has(String(groupId || ''));
}

// Frozen owner wording (Q2/HQ1) — exact strings, never localized/reworded here.
export const TASK_CONTROL_LABELS = Object.freeze({
    [ACTION_FINALIZE]: 'Wrap up',
    [ACTION_HURRY]: 'Hurry up',
    [ACTION_STOP_NOW]: 'Stop now',
});

/**
 * The action set for the current card state (pure — node-testable).
 * @param {{cancelPending?: boolean}} [state]
 * @returns {string[]} ordered action ids
 */
export function taskControlActions({ cancelPending = false } = {}) {
    // A pending cancel refuses hurry (HQ1) and a second soft stop is a no-op:
    // the single offered action is the hard escalation of the same intent.
    if (cancelPending) return [ACTION_STOP_NOW];
    return [ACTION_FINALIZE, ACTION_HURRY, ACTION_STOP_NOW];
}

/**
 * Map a stop action to the wire stop_policy (empty = not a stop action).
 * @param {string} action
 * @returns {string}
 */
export function stopPolicyFor(action) {
    if (action === ACTION_FINALIZE) return 'finalize_then_cancel';
    if (action === ACTION_STOP_NOW) return 'immediate';
    return '';
}

// Stable per-task hurry request id (HQ1): a retry of the SAME click reuses the
// id so the endpoint acknowledges idempotently instead of minting a second
// typed control. Page-session scoped — a reload is a new owner intent.
const hurryRequestIds = new Map();

export function hurryRequestId(taskId) {
    const id = String(taskId || '').trim();
    if (!hurryRequestIds.has(id)) {
        const uuid = (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function')
            ? globalThis.crypto.randomUUID()
            : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
        hurryRequestIds.set(id, `hurry-${uuid}`);
    }
    return hurryRequestIds.get(id);
}

// One action in flight per task: the menu disables items while a request is
// awaiting, so a double click cannot race two controls.
const inFlight = new Set();

export function taskControlBusy(taskId) {
    return inFlight.has(String(taskId || '').trim());
}

/**
 * Submit the typed task-local hurry control (HQ1): body carries ONLY the
 * stable request_id — no text field exists and no chat message is created
 * anywhere on this path. Resolves with the acknowledgement (duplicate=true is
 * the idempotent success shape); a typed refusal rejects.
 * @param {string} taskId
 * @returns {Promise<import('./api_types.js').TaskHurryResponse>}
 */
export async function requestHurry(taskId) {
    const id = String(taskId || '').trim();
    inFlight.add(id);
    try {
        return await hurryTask(id, hurryRequestId(id));
    } finally {
        inFlight.delete(id);
    }
}

/**
 * The COMPLETE "Hurry up" flow both surfaces share (HQ1): submit the typed
 * control, acknowledge via LOCAL toast only — success, idempotent duplicate,
 * or a visible typed refusal (e.g. a pending cancel). Never a chat message.
 * @param {string} taskId
 * @returns {Promise<boolean>} whether the control was accepted
 */
export async function hurryTaskAction(taskId) {
    try {
        const ack = await requestHurry(taskId);
        showToast(ack?.duplicate
            ? 'Hurry up: already accepted for this task.'
            : 'Hurry up: accepted — the task will speed up at the next boundary.', 'ok');
        return true;
    } catch (exc) {
        showToast(`Hurry up: refused — ${exc?.message || exc}`, 'error');
        return false;
    }
}

/**
 * Submit a stop action ({@link ACTION_FINALIZE} or {@link ACTION_STOP_NOW}).
 * Both surfaces cancel the task AND its live subtree (v6.82 semantics kept).
 * @param {string} taskId
 * @param {string} action
 * @returns {Promise<import('./api_types.js').TaskCancelResponse>}
 */
export async function requestStop(taskId, action) {
    const id = String(taskId || '').trim();
    inFlight.add(id);
    try {
        return await cancelTask(id, { cascade: true, stopPolicy: stopPolicyFor(action) });
    } finally {
        inFlight.delete(id);
    }
}

// ---------------------------------------------------------------------------
// Dropdown DOM (shared by Chat live cards and the Activity tab)
// ---------------------------------------------------------------------------

let openMenu = null;
let openTrigger = null;

export function closeTaskControlMenu() {
    if (!openMenu) return;
    openMenu.remove();
    openMenu = null;
    openTrigger?.setAttribute?.('aria-expanded', 'false');
    openTrigger = null;
    document.removeEventListener('pointerdown', onOutsidePointer, true);
    document.removeEventListener('keydown', onMenuKeydown, true);
}

function onOutsidePointer(event) {
    if (openMenu && !openMenu.contains(event.target)) closeTaskControlMenu();
}

function onMenuKeydown(event) {
    // Dismiss = continue the run (Q2: the dismiss affordance replaced the old
    // explicit "keep running" item).
    if (event.key === 'Escape') closeTaskControlMenu();
}

/**
 * Open the three-action dropdown anchored inside `anchor`'s positioned parent.
 * Dismissing (outside click / Escape) continues the run. Selecting an item
 * closes the menu and invokes `onAction(actionId)`.
 * @param {HTMLElement} anchor trigger element; the menu mounts next to it
 * @param {{cancelPending?: boolean, busy?: boolean, onAction: (action: string) => void}} opts
 */
export function openTaskControlMenu(anchor, { cancelPending = false, busy = false, onAction } = {}) {
    closeTaskControlMenu();
    if (!anchor || !anchor.parentElement) return null;
    // A11y: the trigger owns a popup menu; expanded tracks the open state.
    anchor.setAttribute('aria-haspopup', 'menu');
    anchor.setAttribute('aria-expanded', 'true');
    const menu = document.createElement('div');
    menu.className = 'task-control-menu';
    menu.setAttribute('role', 'menu');
    for (const action of taskControlActions({ cancelPending })) {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = `task-control-item${action === ACTION_STOP_NOW ? ' danger' : ''}`;
        item.dataset.taskControl = action;
        item.setAttribute('role', 'menuitem');
        item.textContent = TASK_CONTROL_LABELS[action];
        if (busy) item.disabled = true;
        item.addEventListener('click', (event) => {
            event.stopPropagation();
            closeTaskControlMenu();
            onAction?.(action);
        });
        menu.appendChild(item);
    }
    anchor.parentElement.appendChild(menu);
    openMenu = menu;
    openTrigger = anchor;
    // A11y: keyboard users land on the first actionable item on open.
    menu.querySelector('button:not(:disabled)')?.focus?.();
    document.addEventListener('pointerdown', onOutsidePointer, true);
    document.addEventListener('keydown', onMenuKeydown, true);
    return menu;
}

// The trigger's constant label. The trigger always opens the dropdown; the
// pending-cancel state changes the OFFERED actions (escalation only), not the
// interaction shape — so an accidental click never hard-stops directly.
export const TASK_CONTROL_TRIGGER_LABEL = 'Stop…';
