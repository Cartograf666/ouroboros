/**
 * Project threads (T1): the sidebar thread list, the owner's manual order, the
 * per-thread unread arithmetic, the thread row menu, and the CENTRE stage a
 * thread's chat mounts into.
 *
 * Why this is its own module (R8): `web/app.js` is under a hard 1600-line module
 * gate and is already the navigation/state-machine owner. Thread UI lands here;
 * app.js keeps only the instance lifecycle (single live instance, scroll stash,
 * paint ACK) it already owned and calls into these seams.
 *
 * WHY THE CENTRE, not the right panel: a project chat used to open as a right
 * split panel, which on a phone became a full-screen overlay stacked ON TOP of
 * the content area — two competing full-screen surfaces with two close
 * affordances. A thread is a conversation, i.e. the primary thing you look at,
 * so it takes over the centre exactly like Main Chat does. The right panel is
 * now the task inspector's alone.
 *
 * The pure half (ordering, unread aggregation, cursor normalization) is exported
 * separately from the DOM half and is covered by `web/tests/project_threads.test.js`.
 */

import { openConfirmDialog } from './confirm_dialog.js';
import { openRowMenu } from './project_create.js';
import { renderMobileNavToggle } from './page_header.js';

/** Thread #0 IS the project's original chat — mirrors `contracts/chat_id_policy.py`. */
export const MAIN_THREAD_ID = 0;
/** Mirrored from the frozen backend THREAD_NAME_MAX contract. */
export const THREAD_NAME_MAX = 80;

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

/** The instance/stash/cursor key for one thread. Project ids never contain '#'. */
export function threadKey(projectId, threadId) {
    return `${String(projectId || '')}#${Number(threadId) || 0}`;
}

/**
 * Browser mirror of `gateway/ui_preferences.py::_normalize_seen_revision`.
 *
 * The read cursor is NESTED per thread (`{project: {thread: revision}}`). A FLAT
 * `{project: revision}` entry is what every pre-T1 runtime stored and is the one
 * compatibility spelling of thread #0's cursor, so it normalizes to
 * `{project: {"0": revision}}` — the same rule the server applies, kept here so
 * a browser that read preferences written by an older server agrees with it
 * instead of treating every project as unread.
 *
 * @param {Object|null|undefined} raw
 * @returns {Object.<string, Object.<string, number>>}
 */
export function normalizeSeenRevision(raw) {
    const out = {};
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out;
    for (const [pid, value] of Object.entries(raw)) {
        const key = String(pid || '').trim();
        if (!key) continue;
        if (value && typeof value === 'object' && !Array.isArray(value)) {
            const lane = {};
            for (const [tid, revision] of Object.entries(value)) {
                const thread = Number(tid);
                if (!Number.isFinite(thread)) continue;
                lane[String(Math.trunc(thread))] = Math.max(0, Number(revision) || 0);
            }
            out[key] = lane;
        } else {
            out[key] = { [String(MAIN_THREAD_ID)]: Math.max(0, Number(value) || 0) };
        }
    }
    return out;
}

/** The acknowledged revision of one thread (0 when never acknowledged). */
export function seenRevisionFor(cursor, projectId, threadId) {
    const lane = (cursor || {})[String(projectId || '')];
    if (!lane) return 0;
    return Math.max(0, Number(lane[String(Number(threadId) || 0)]) || 0);
}

/** Record an acknowledgement in the local mirror, monotonically. */
export function rememberSeenRevision(cursor, projectId, threadId, revision) {
    const store = cursor || {};
    const pid = String(projectId || '');
    const tid = String(Number(threadId) || 0);
    const lane = store[pid] || (store[pid] = {});
    lane[tid] = Math.max(Number(lane[tid]) || 0, Math.max(0, Number(revision) || 0));
    return store;
}

/**
 * The canonical thread rows of a sidebar project entry, thread #0 first.
 *
 * `/api/state` already ships `ProjectEntry.threads` from the server's canonical
 * projection. The fallback synthesizes thread #0 from the project's own
 * `chat_id`/`name`/`visible_revision` so a browser talking to a server that
 * predates the projection still renders exactly one (correct) thread rather
 * than an empty list.
 */
export function projectThreadRows(project) {
    const rows = Array.isArray(project?.threads) ? project.threads : null;
    if (rows && rows.length) {
        return rows
            .filter((thread) => thread && Number.isFinite(Number(thread.id)))
            .map((thread) => ({
                ...thread,
                id: Number(thread.id) || 0,
                chat_id: Number(thread.chat_id) || 0,
                name: String(thread.name || '') || `Thread ${Number(thread.id) || 0}`,
                visible_revision: Math.max(0, Number(thread.visible_revision) || 0),
            }));
    }
    return [{
        id: MAIN_THREAD_ID,
        chat_id: Number(project?.chat_id) || 0,
        name: String(project?.name || project?.id || ''),
        visible_revision: Math.max(0, Number(project?.visible_revision) || 0),
    }];
}

/** Every thread EXCEPT #0 — the ones the sidebar lists under the project row. */
export function extraThreadRows(project) {
    return projectThreadRows(project).filter((thread) => thread.id !== MAIN_THREAD_ID);
}

/** A thread is unread when its OWN revision exceeds its OWN acknowledged cursor. */
export function isThreadUnread(thread, cursor, projectId) {
    return Math.max(0, Number(thread?.visible_revision) || 0)
        > seenRevisionFor(cursor, projectId, thread?.id);
}

/**
 * How many of a project's threads are unread — the project ROW's aggregate.
 * The row owns no unread number of its own; it is a grouping over its threads,
 * so a sibling thread's message can never mark the project's main thread read.
 */
export function unreadThreadCount(project, cursor) {
    if (String(project?.lifecycle || 'active') !== 'active') return 0;
    const pid = String(project?.id || '');
    return projectThreadRows(project).filter((thread) => isThreadUnread(thread, cursor, pid)).length;
}

/**
 * Apply an owner's manual order (D3) as an explicit PREFIX: everything the owner
 * has placed keeps that order, everything else falls in behind it in the
 * caller's default order. A stale id in the stored order is ignored, so
 * deleting a project or thread never scrambles the rest of the list.
 *
 * @param {Array} rows          default-ordered rows
 * @param {string[]} manual     owner-ordered ids
 * @param {(row:any)=>string} idOf
 */
export function applyManualOrder(rows, manual, idOf) {
    const list = Array.isArray(rows) ? [...rows] : [];
    const order = Array.isArray(manual) ? manual.map(String) : [];
    if (!order.length) return list;
    const rank = new Map(order.map((id, index) => [id, index]));
    const placed = [];
    const rest = [];
    for (const row of list) {
        (rank.has(idOf(row)) ? placed : rest).push(row);
    }
    placed.sort((a, b) => rank.get(idOf(a)) - rank.get(idOf(b)));
    return [...placed, ...rest];
}

/**
 * D3 project order: newest on top by default (recency), owner's manual order
 * first. Deliberately NO unread hoist — "no clever logic" is the owner's rule,
 * and a list that reshuffles itself when a message arrives is exactly the
 * jumping-target problem drag-and-drop ordering exists to end.
 */
export function orderProjectRows(rows, manualOrder) {
    const recency = (p) => String(p?.last_active_at || p?.updated_at || p?.created_at || '');
    const byRecency = [...(rows || [])].sort((a, b) => recency(b).localeCompare(recency(a)));
    return applyManualOrder(byRecency, manualOrder, (row) => String(row.id));
}

/**
 * D3 thread order within a project: a NEW thread on top (ids are monotonic, so
 * descending id is "newest first" without reading a clock), owner's manual
 * order first.
 */
export function orderThreadRows(threads, manualOrder) {
    const byNewest = [...(threads || [])].sort((a, b) => (Number(b.id) || 0) - (Number(a.id) || 0));
    return applyManualOrder(byNewest, manualOrder, (row) => String(row.id));
}

/**
 * The new explicit order after dropping `draggedId` onto `targetId`.
 * Returns the FULL id list so what is persisted is what is displayed — a
 * partial prefix would let the default order re-sort the tail under the owner.
 *
 * @param {string[]} ids       currently displayed ids, in display order
 * @param {string} draggedId
 * @param {string} targetId
 * @param {boolean} placeAfter drop below the target rather than above it
 */
export function reorderIds(ids, draggedId, targetId, placeAfter = false) {
    const list = (ids || []).map(String);
    const dragged = String(draggedId);
    const target = String(targetId);
    if (dragged === target || !list.includes(dragged) || !list.includes(target)) return list;
    const without = list.filter((id) => id !== dragged);
    const at = without.indexOf(target);
    without.splice(at + (placeAfter ? 1 : 0), 0, dragged);
    return without;
}

// ---------------------------------------------------------------------------
// Sidebar: the thread list under a project row
// ---------------------------------------------------------------------------

/**
 * Build the sibling container listing a project's extra threads.
 *
 * SIBLING, not a child of the project row: the pinned markup contract keeps the
 * project row a single `<button>` with one trailing action slot
 * (`item.append(btn, trailing)`), and interactive UI must never be nested inside
 * a button. Returns `null` when the project has no extra threads, so a project
 * that never used threads renders exactly the sidebar it always did.
 *
 * @returns {HTMLElement|null}
 */
export function renderThreadList(project, {
    cursor = {},
    manualOrder = [],
    activeThreadKey = '',
    onOpen,
    onMenu,
    onReorder,
} = {}) {
    const pid = String(project?.id || '');
    const threads = orderThreadRows(extraThreadRows(project), manualOrder);
    if (!threads.length) return null;
    const list = document.createElement('div');
    list.className = 'nav-thread-list';
    list.dataset.threadsFor = pid;
    list.setAttribute('role', 'group');
    list.setAttribute('aria-label', `Threads in ${project.name || pid}`);

    const displayedIds = () => Array.from(list.querySelectorAll('[data-thread-id]'))
        .map((el) => el.dataset.threadId);

    for (const thread of threads) {
        const key = threadKey(pid, thread.id);
        const item = document.createElement('div');
        item.className = 'nav-thread-item';
        item.dataset.threadKey = key;
        item.dataset.threadId = String(thread.id);
        item.draggable = true;

        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'nav-row nav-thread-row';
        // NOT `data-project-id`: that attribute is the project-row active-state
        // selector, and reusing it here would light up every thread of the
        // active project as if it were the open one.
        row.dataset.threadProjectId = pid;
        row.dataset.threadId = String(thread.id);
        row.dataset.threadKey = key;
        row.title = thread.name;
        const label = document.createElement('span');
        label.className = 'nav-row-label nav-thread-label';
        label.textContent = thread.name;
        row.appendChild(label);
        if (isThreadUnread(thread, cursor, pid)) {
            const dot = document.createElement('span');
            dot.className = 'nav-unread-dot';
            dot.title = 'New activity';
            row.appendChild(dot);
            row.classList.add('has-unread');
        }
        if (key === activeThreadKey) {
            row.classList.add('active');
            row.setAttribute('aria-current', 'page');
        }
        row.addEventListener('click', () => onOpen?.(project, thread));

        const kebab = document.createElement('button');
        kebab.type = 'button';
        kebab.className = 'nav-project-kebab nav-thread-kebab';
        kebab.textContent = '⋯';
        kebab.title = 'Thread actions';
        kebab.setAttribute('aria-label', `Actions for thread ${thread.name}`);
        kebab.addEventListener('click', (event) => {
            event.stopPropagation();
            onMenu?.(project, thread, kebab);
        });

        item.append(row, kebab);
        list.appendChild(item);
    }

    attachReorder(list, '[data-thread-id]', (ids) => onReorder?.(pid, ids), displayedIds);
    return list;
}

/**
 * Pointer drag-and-drop reordering over a container of rows (D3).
 *
 * Uses the native HTML drag events rather than a pointer-move reimplementation:
 * the browser then owns the drag image, the escape-to-cancel behaviour and the
 * accessibility semantics. `onCommit(ids)` receives the FULL new id order and is
 * called once, on drop.
 */
function attachReorder(container, rowSelector, onCommit, displayedIds) {
    let draggedId = '';
    container.addEventListener('dragstart', (event) => {
        const row = event.target.closest(rowSelector);
        if (!row) return;
        draggedId = row.dataset.threadId || row.dataset.projectId || '';
        row.classList.add('is-dragging');
        try { event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', draggedId); } catch {}
    });
    container.addEventListener('dragend', () => {
        draggedId = '';
        container.querySelectorAll('.is-dragging, .drop-before, .drop-after')
            .forEach((el) => el.classList.remove('is-dragging', 'drop-before', 'drop-after'));
    });
    container.addEventListener('dragover', (event) => {
        const row = event.target.closest(rowSelector);
        if (!row || !draggedId) return;
        event.preventDefault();
        const box = row.getBoundingClientRect();
        const after = (event.clientY - box.top) > box.height / 2;
        container.querySelectorAll('.drop-before, .drop-after')
            .forEach((el) => el.classList.remove('drop-before', 'drop-after'));
        row.classList.add(after ? 'drop-after' : 'drop-before');
    });
    container.addEventListener('drop', (event) => {
        const row = event.target.closest(rowSelector);
        if (!row || !draggedId) return;
        event.preventDefault();
        const box = row.getBoundingClientRect();
        const after = (event.clientY - box.top) > box.height / 2;
        const targetId = row.dataset.threadId || row.dataset.projectId || '';
        const next = reorderIds(displayedIds(), draggedId, targetId, after);
        draggedId = '';
        onCommit?.(next);
    });
}

/** Drag-and-drop ordering for the PROJECT rows themselves (same D3 surface). */
export function attachProjectReorder(listEl, onCommit) {
    attachReorder(
        listEl,
        '.nav-project-item[data-project-id]',
        onCommit,
        () => Array.from(listEl.querySelectorAll('.nav-project-item[data-project-id]'))
            .map((el) => el.dataset.projectId),
    );
}

// ---------------------------------------------------------------------------
// Thread row menu: Rename… / Fork
// ---------------------------------------------------------------------------

/**
 * Per-thread actions, mounted through the SAME accessible row-menu shell the
 * project row uses (`project_create.js::openRowMenu`) so the keyboard model and
 * viewport-safe placement have exactly one implementation.
 *
 * Rename validates against the mirrored 80-char backend contract before the
 * request, so the owner gets the limit explained rather than a 400.
 */
export function openThreadRowMenu(project, thread, { apiClient, anchorEl, onChanged }) {
    openRowMenu({
        anchorEl,
        ariaLabel: `Actions for thread ${thread.name}`,
        itemsHtml: `
            <button type="button" role="menuitem" data-prm="rename">Rename…</button>
            <button type="button" role="menuitem" data-prm="fork">Fork</button>
        `,
        onSelect: async (action) => {
            if (action === 'rename') {
                const res = await openConfirmDialog({
                    title: 'Rename thread',
                    body: `New name for “${thread.name}”:`,
                    input: true,
                    initialValue: thread.name,
                    confirmLabel: 'Rename',
                });
                const newName = res?.confirmed ? String(res.value || '').trim() : '';
                if (newName.length > THREAD_NAME_MAX) {
                    await openConfirmDialog({
                        title: 'Rename thread',
                        body: `Thread names are limited to ${THREAD_NAME_MAX} characters.`,
                        alert: true,
                    });
                } else if (newName && newName !== thread.name) {
                    try {
                        await apiClient.projectThreadUpdate(project.id, thread.id, newName);
                        onChanged?.({ authoritative: true });
                    } catch (e) {
                        await openConfirmDialog({
                            title: 'Rename failed',
                            body: `Rename failed: ${e?.body?.error || e?.message || e}`,
                            alert: true,
                        });
                    }
                }
            } else if (action === 'fork') {
                // A cursor into this thread's rows, not a copy: the source thread
                // is untouched and the fork keeps resolving the shared past even
                // if the source is later archived or deleted (A3/A3a).
                try {
                    const payload = await apiClient.projectThreadFork(project.id, thread.id);
                    onChanged?.({ authoritative: true, thread: payload?.thread || null });
                } catch (e) {
                    await openConfirmDialog({
                        title: 'Fork failed',
                        body: `Fork failed: ${e?.body?.error || e?.message || e}`,
                        alert: true,
                    });
                }
            }
            if (anchorEl.isConnected) anchorEl.focus();
        },
    });
}

// ---------------------------------------------------------------------------
// The CENTRE stage
// ---------------------------------------------------------------------------

/**
 * Create the `#page-thread` centre page: an in-flow header bar (mobile nav
 * toggle, project/thread title, thread menu, close) above the mount point a
 * chat instance attaches to.
 *
 * ONE page element hosts every thread rather than one page per thread: `showPage`
 * keys on a stable page name, and the single-live-instance policy means at most
 * one instance is mounted here anyway.
 */
export function createThreadStage({ content, onClose, onMenu }) {
    const page = document.createElement('section');
    page.id = 'page-thread';
    page.className = 'page thread-stage';
    page.innerHTML = `
        <div class="thread-stage-bar project-panel-bar">
            <div class="app-page-leading">${renderMobileNavToggle()}</div>
            <div class="thread-stage-heading">
                <span class="thread-stage-project" id="thread-stage-project"></span>
                <h2 class="thread-stage-title project-panel-title app-page-title" id="thread-stage-title"></h2>
            </div>
            <button type="button" class="nav-project-kebab thread-stage-menu" id="thread-stage-menu" title="Thread actions" aria-label="Thread actions">⋯</button>
            <button type="button" class="project-panel-close thread-stage-close" id="thread-stage-close" title="Close thread" aria-label="Close thread">×</button>
        </div>
        <div class="thread-stage-body" id="thread-stage-body"></div>
    `;
    content.appendChild(page);
    const titleEl = page.querySelector('#thread-stage-title');
    const projectEl = page.querySelector('#thread-stage-project');
    const menuBtn = page.querySelector('#thread-stage-menu');
    page.querySelector('#thread-stage-close')?.addEventListener('click', () => onClose?.());
    menuBtn?.addEventListener('click', () => onMenu?.(menuBtn));
    return {
        page,
        body: page.querySelector('#thread-stage-body'),
        menuAnchor: menuBtn,
        setTitle(project, thread) {
            projectEl.textContent = project?.name || project?.id || '';
            titleEl.textContent = thread?.name || projectEl.textContent;
            // Thread #0 IS the project: showing the project name twice would read
            // as two different rooms with the same name.
            projectEl.hidden = Number(thread?.id) === MAIN_THREAD_ID;
        },
    };
}
