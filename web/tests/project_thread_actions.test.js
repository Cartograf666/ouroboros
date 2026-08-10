import assert from 'node:assert/strict';
import test from 'node:test';

import {
    describeOutcome,
    isBranched,
    openThreadChanges,
    queueNoticeOffersBranching,
    queueNoticeText,
    removalPrompt,
    snapshotReceipt,
    threadActions,
} from '../modules/project_thread_actions.js';

const IN_FOLDER = { where: 'project_folder' };
const IN_WORKTREE = { where: 'worktree', branch: 'thread/racer__2', path: '/w/t2' };

function byId(thread, location) {
    return Object.fromEntries(threadActions(thread, location).map((row) => [row.id, row]));
}

test('a thread\'s location is DERIVED from the worktree existing, never stored', () => {
    assert.equal(isBranched(IN_WORKTREE), true);
    assert.equal(isBranched(IN_FOLDER), false);
    assert.equal(isBranched(null), false);
    assert.equal(isBranched({ where: '' }), false);
});

test('branch off and merge back are OPPOSITE offers, never both available', () => {
    const inFolder = byId({ id: 2 }, IN_FOLDER);
    assert.equal(inFolder.branch_off.available, true);
    assert.equal(inFolder.merge_back.available, false);
    assert.match(inFolder.merge_back.disabledReason, /works in the project folder/);

    const branched = byId({ id: 2 }, IN_WORKTREE);
    assert.equal(branched.branch_off.available, false);
    assert.match(branched.branch_off.disabledReason, /already works in its own branch/);
    assert.equal(branched.merge_back.available, true);
});

test('the checkout diff and its removal are offered only where a checkout exists', () => {
    const inFolder = byId({ id: 2 }, IN_FOLDER);
    assert.equal(inFolder.show_changes.available, false);
    assert.equal(inFolder.remove_worktree.available, false);

    const branched = byId({ id: 2 }, IN_WORKTREE);
    assert.equal(branched.show_changes.available, true);
    assert.equal(branched.remove_worktree.available, true);
});

test('thread #0 is the project, so its own lifecycle is disabled WITH a reason', () => {
    // Omitting the items would teach nothing; a disabled item with a reason says
    // where the operation actually lives.
    const main = byId({ id: 0 }, IN_FOLDER);
    assert.equal(main.archive.available, false);
    assert.match(main.archive.disabledReason, /is the project itself/);
    assert.equal(main.delete.available, false);
});

test('archive flips to restore once the thread is archived', () => {
    const archived = byId({ id: 2, lifecycle: 'archived' }, IN_FOLDER);
    assert.ok(archived.restore);
    assert.equal(archived.restore.available, true);
    assert.equal(archived.archive, undefined);
});

test('a thread already being deleted offers nothing runnable', () => {
    const deleting = byId({ id: 2, lifecycle: 'deleting' }, IN_WORKTREE);
    for (const row of Object.values(deleting)) {
        assert.equal(row.available, false, `${row.id} must not be offered while deleting`);
        assert.match(row.disabledReason, /being deleted/);
    }
});

test('a merge conflict is SHOWN with its paths, in the server\'s own words', () => {
    const outcome = describeOutcome({
        ok: false,
        reason: 'merge_conflict',
        message: 'These files changed on both sides, so the merge was stopped.',
        conflicts: ['app.txt', 'src/main.py'],
    });

    assert.equal(outcome.tone, 'conflict');
    assert.equal(outcome.text, 'These files changed on both sides, so the merge was stopped.');
    assert.deepEqual(outcome.evidence, ['app.txt', 'src/main.py']);
});

test('a refusal with no sentence is still NAMED, never rendered blank', () => {
    const outcome = describeOutcome({ ok: false, reason: 'branch_failed' });
    assert.match(outcome.text, /branch_failed/);
});

test('a successful merge says the checkout survives (A10)', () => {
    const outcome = describeOutcome({ ok: true, merged: true, worktree_kept: true });
    assert.equal(outcome.tone, 'ok');
    assert.match(outcome.text, /keeps its checkout until you remove it/);
});

test('a snapshot receipt names the credential files it left out', () => {
    // They are still in the folder and still untracked. An owner who is not told
    // will believe their .env came along.
    const text = snapshotReceipt({
        snapshot_commit: { created: true, sha: 'abc', skipped_sensitive: ['.env', 'id_rsa'] },
    });
    assert.match(text, /\.env, id_rsa/);
    assert.match(text, /still untracked/);

    assert.match(
        snapshotReceipt({ snapshot_commit: { created: false, skipped_sensitive: [] } }),
        /no uncommitted changes/,
    );
    assert.equal(snapshotReceipt({}), '');
});

test('a clean checkout removes without an acknowledgement; unmerged work does not', () => {
    const clean = removalPrompt({ dirty_files: [], unmerged_commits: 0 });
    assert.equal(clean.needsAcknowledgement, false);

    const risky = removalPrompt({ dirty_files: ['a.txt', 'b.txt'], unmerged_commits: 3 });
    assert.equal(risky.needsAcknowledgement, true);
    assert.match(risky.text, /3 commits the project folder never received/);
    assert.match(risky.text, /2 uncommitted file changes/);
    assert.deepEqual(risky.evidence, ['a.txt', 'b.txt']);
});

test('an unreadable checkout is UNSAFE — "cannot tell" is never "nothing to lose"', () => {
    const prompt = removalPrompt({ error: 'not a git repository', dirty_files: [], unmerged_commits: 0 });
    assert.equal(prompt.needsAcknowledgement, true);
    assert.match(prompt.text, /could not be read/);
    assert.deepEqual(prompt.evidence, ['not a git repository']);
});

test('the queue notice is the server\'s sentence, and it offers branching', () => {
    const notice = {
        queued: true,
        remedy: 'branch_off',
        message: 'A task you start here will be QUEUED behind it and will run as soon as that one finishes.',
    };

    assert.equal(queueNoticeText(notice), notice.message);
    assert.equal(queueNoticeOffersBranching(notice), true);
    // Nothing waiting means nothing said.
    assert.equal(queueNoticeText({ queued: false, message: 'x' }), '');
    assert.equal(queueNoticeOffersBranching({ queued: false, remedy: 'branch_off' }), false);
    // A thread waiting on its OWN checkout is not offered a second branch-off:
    // that advice would not work.
    assert.equal(queueNoticeOffersBranching({ queued: true, remedy: '' }), false);
});

test('opening a thread checkout goes through the SAME event seam the inspector uses', () => {
    // The menu must not need a handle on the Changes controller: one page owns
    // Changes, and both ways in land on its two source-mode entry points.
    const seen = [];
    const original = globalThis.window;
    globalThis.window = { dispatchEvent: (event) => seen.push(event) };
    try {
        assert.equal(openThreadChanges({ projectId: 'racer', threadId: 2, branch: 'thread/racer__2' }), true);
        assert.equal(seen.length, 1);
        assert.equal(seen[0].type, 'ouro:open-thread-changes');
        assert.deepEqual(seen[0].detail, {
            projectId: 'racer', threadId: '2', label: '', branch: 'thread/racer__2', filePath: '',
        });
        // Thread 0 is a legitimate id; only a MISSING one is refused.
        assert.equal(openThreadChanges({ projectId: 'racer', threadId: 0 }), true);
        assert.equal(openThreadChanges({ projectId: '', threadId: 2 }), false);
        assert.equal(openThreadChanges({ projectId: 'racer' }), false);
    } finally {
        globalThis.window = original;
    }
});
