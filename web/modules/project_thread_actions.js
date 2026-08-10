/**
 * Thread branch / merge / checkout / lifecycle actions, as small pure helpers.
 *
 * This module owns the DECISIONS behind those owner gestures — what a menu may
 * offer for a given thread, what each answer means, and the exact words shown
 * when something refuses. It deliberately renders no DOM: the thread menus live
 * in `project_threads.js` (phase T1) and this file is the seam they call, so the
 * two phases could be built in parallel and joined without either one guessing
 * at the other's internals.
 *
 * Three rules the helpers exist to keep:
 *
 *   1. A thread's LOCATION is derived, never stored (A7). Every helper reads it
 *      from `location.where` — no caller keeps a boolean about it.
 *   2. A refusal is SHOWN, never smoothed over (A9/A10). `describeOutcome` turns
 *      every typed reason into the sentence the server already wrote, and
 *      `conflicts`/`dirty_files`/`inspection` ride along as evidence rather than
 *      being flattened into "something went wrong".
 *   3. Removing a checkout is always a separate, confirmed act (A10). There is
 *      no path here that removes one as a side effect of anything else, and the
 *      acknowledgement is a distinct second call the owner has to reach.
 */

import { apiClient } from './api_client.js';

/** A thread works in the project folder unless a worktree exists for it (A7). */
export function isBranched(location) {
    return String(location?.where || '') === 'worktree';
}

/**
 * Which actions a menu may offer for one thread, and which are disabled.
 *
 * Returned in menu order with a stable `id` per action, an owner-facing `label`,
 * and — when it cannot be run — a `disabledReason` sentence rather than a silent
 * omission. A missing item teaches nothing; a greyed one with a reason teaches
 * what to do first.
 *
 * @param {{id?: number, lifecycle?: string}} thread
 * @param {{where?: string, branch?: string}} location
 */
export function threadActions(thread, location) {
    const isMain = Number(thread?.id ?? 0) === 0;
    const lifecycle = String(thread?.lifecycle || 'active');
    const branched = isBranched(location);
    const terminal = lifecycle === 'deleting' || lifecycle === 'tombstoned';
    // Thread #0 IS the project. Offering it a lifecycle of its own would promise
    // an operation the server refuses by name, so it is disabled with the reason.
    const projectItself = isMain ? 'This thread is the project itself.' : '';
    // A thread on its way out overrides every other explanation: "already works
    // in its own branch" is true but useless when the thread is being deleted.
    const settling = terminal ? 'This thread is being deleted.' : '';
    const row = (id, label, allowed, reason) => ({
        id,
        label,
        available: allowed && !terminal,
        disabledReason: terminal ? settling : (allowed ? '' : reason),
    });
    const noCheckout = 'This thread works in the project folder.';
    return [
        row('branch_off', 'Branch off…', !branched, 'This thread already works in its own branch.'),
        row('merge_back', 'Merge back', branched, noCheckout),
        row('show_changes', 'Show changes', branched, noCheckout),
        row('remove_worktree', 'Remove checkout…', branched, 'This thread has no checkout.'),
        row(
            lifecycle === 'archived' ? 'restore' : 'archive',
            lifecycle === 'archived' ? 'Restore' : 'Archive',
            !isMain,
            projectItself,
        ),
        row('delete', 'Delete…', !isMain, projectItself),
    ];
}

/**
 * The owner-facing reading of any branch/merge/remove/lifecycle answer.
 *
 * `tone` drives a CSS token only. `text` is the server's own sentence wherever it
 * wrote one — the copy for a refusal belongs beside the rule that produced it,
 * and re-authoring it here is how two surfaces end up explaining the same
 * refusal differently. `evidence` is the list the owner needs to act: the
 * conflicting paths, the files blocking a merge, what a removal would destroy.
 */
export function describeOutcome(outcome) {
    const reason = String(outcome?.reason || '');
    const server = String(outcome?.message || '').trim();
    if (outcome?.ok) {
        return { tone: 'ok', text: server || successText(outcome), evidence: [] };
    }
    const evidence = []
        .concat(Array.isArray(outcome?.conflicts) ? outcome.conflicts : [])
        .concat(Array.isArray(outcome?.dirty_files) ? outcome.dirty_files : [])
        .concat(Array.isArray(outcome?.inspection?.dirty_files) ? outcome.inspection.dirty_files : []);
    return {
        tone: reason === 'merge_conflict' ? 'conflict' : 'blocked',
        // A refusal with no sentence at all is still named, never rendered blank.
        text: server || `This could not be done (${reason || 'unknown reason'}).`,
        evidence,
        reason,
    };
}

/** The one sentence for a success the server did not narrate itself. */
export function successText(outcome) {
    if (outcome?.merged === false) return 'Nothing new to merge — the folder already has this work.';
    if (outcome?.merged) {
        // A10 is stated at the moment it matters: the checkout is still there.
        return 'Merged into the project folder. The thread keeps its checkout until you remove it.';
    }
    if (outcome?.removed) return 'Checkout removed.';
    if (outcome?.branch) return `Branched off into ${outcome.branch}.`;
    return 'Done.';
}

/**
 * What a snapshot base actually did, for the receipt after a branch-off.
 *
 * Returns '' when no snapshot was involved. A snapshot that EXCLUDED
 * credential-shaped files says so by name: those files are still in the folder
 * and still untracked, and an owner who is not told will believe their `.env`
 * came along.
 */
export function snapshotReceipt(outcome) {
    const snapshot = outcome?.snapshot_commit;
    if (!snapshot) return '';
    const skipped = Array.isArray(snapshot.skipped_sensitive) ? snapshot.skipped_sensitive.filter(Boolean) : [];
    if (!snapshot.created) return 'The folder had no uncommitted changes, so nothing new was committed.';
    const base = 'Your uncommitted changes were committed first, so the branch starts from exactly what was there.';
    return skipped.length
        ? `${base} Left out of that commit (still in your folder, still untracked): ${skipped.join(', ')}.`
        : base;
}

/**
 * The confirmation an owner must see BEFORE a checkout is removed (A10).
 *
 * Returns `{needsAcknowledgement, text, evidence}`. When the inspection could
 * not be read, this treats it as unsafe and says so — "cannot tell" must never
 * be rendered as "nothing to lose".
 */
export function removalPrompt(inspection) {
    const dirty = Array.isArray(inspection?.dirty_files) ? inspection.dirty_files.filter(Boolean) : [];
    const commits = Number(inspection?.unmerged_commits || 0);
    const error = String(inspection?.error || '').trim();
    if (error) {
        return {
            needsAcknowledgement: true,
            text: 'This checkout could not be read, so what removing it would destroy is unknown.',
            evidence: [error],
        };
    }
    if (!dirty.length && !commits) {
        return {
            needsAcknowledgement: false,
            text: 'This checkout has no unmerged work. Removing it deletes only the folder.',
            evidence: [],
        };
    }
    const parts = [];
    if (commits) parts.push(`${commits} commit${commits === 1 ? '' : 's'} the project folder never received`);
    if (dirty.length) parts.push(`${dirty.length} uncommitted file change${dirty.length === 1 ? '' : 's'}`);
    return {
        needsAcknowledgement: true,
        text: `Removing this checkout deletes ${parts.join(' and ')}.`,
        evidence: dirty,
    };
}

/**
 * The honest queue sentence (A14), or '' when nothing would wait.
 *
 * The copy itself is the SERVER's — one sentence, one place — so the UI can
 * never soften "queued behind" into "rejected" or the reverse.
 */
export function queueNoticeText(notice) {
    return notice?.queued ? String(notice.message || '') : '';
}

/** Does the queue notice offer branching as the way to run in parallel? */
export function queueNoticeOffersBranching(notice) {
    return Boolean(notice?.queued) && String(notice?.remedy || '') === 'branch_off';
}

// ---------------------------------------------------------------------------
// Thin call wrappers. They exist so the menu never hand-rolls a fetch and never
// has to remember which routes take a body.
// ---------------------------------------------------------------------------

export const threadOps = {
    bases: (projectId, threadId) => apiClient.threadBranchBases(projectId, threadId),
    branchOff: (projectId, threadId, baseRef) => apiClient.threadBranchOff(projectId, threadId, baseRef),
    mergeBack: (projectId, threadId) => apiClient.threadMergeBack(projectId, threadId),
    inspectWorktree: (projectId, threadId) => apiClient.threadWorktree(projectId, threadId),
    /**
     * Remove a checkout. `acknowledged` is the owner's answer to `removalPrompt`
     * and is the ONLY way past unmerged work — deliberately a separate argument
     * so no caller can pass it by accident.
     */
    removeWorktree: (projectId, threadId, acknowledged = false) => (
        apiClient.threadWorktreeRemove(projectId, threadId, acknowledged)
    ),
    archive: (projectId, threadId) => apiClient.threadArchive(projectId, threadId),
    restore: (projectId, threadId) => apiClient.threadRestore(projectId, threadId),
    delete: (projectId, threadId) => apiClient.threadDelete(projectId, threadId),
};
