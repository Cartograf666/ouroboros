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
    successText,
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

test('a thread STUCK in deleting can still be told to try again (T3R2-H7)', () => {
    // `deleting` is the deliberate end state of `fail_thread_deletion`, and
    // `thread_is_visible` keeps it on screen precisely so the owner can act on it.
    // Collapsing it into one `terminal` flag with `tombstoned` disabled every row,
    // while the server accepts a delete retry idempotently — the thread's
    // committed work was unreachable and the only escape was a server restart.
    const deleting = byId(
        { id: 2, lifecycle: 'deleting', delete_error: 'RuntimeError: did not quiesce (t1)' },
        IN_WORKTREE,
    );
    for (const row of Object.values(deleting)) {
        if (row.id === 'delete') continue;
        assert.equal(row.available, false, `${row.id} must not be offered while deleting`);
        assert.match(row.disabledReason, /being deleted/);
    }
    assert.equal(deleting.delete.available, true);
    assert.equal(deleting.delete.label, 'Retry delete');
    // `delete_error` is normalised onto every row and nothing read it. It is the
    // reason the retry is OFFERED, not a reason something is disabled.
    assert.match(deleting.delete.reason, /did not quiesce/);
    assert.equal(deleting.delete.disabledReason, '');

    // A deleting thread with no recorded error still offers the retry, with a
    // sentence rather than a blank.
    const quiet = byId({ id: 2, lifecycle: 'deleting' }, IN_FOLDER);
    assert.equal(quiet.delete.available, true);
    assert.match(quiet.delete.reason, /did not finish/);
});

test('thread #0 of a DELETING project is never offered a retry it cannot win', () => {
    // Thread #0 mirrors the PROJECT's lifecycle, so a deleting project makes it
    // read `deleting` — and the delete route refuses thread #0 by name.
    const zero = byId({ id: 0, lifecycle: 'deleting' }, IN_FOLDER);
    assert.equal(zero.delete.label, 'Delete…');
    assert.equal(zero.delete.available, false);
    assert.match(zero.delete.disabledReason, /being deleted/);
});

test('a TOMBSTONED thread really is terminal', () => {
    const gone = byId({ id: 2, lifecycle: 'tombstoned' }, IN_FOLDER);
    for (const row of Object.values(gone)) {
        assert.equal(row.available, false, `${row.id} must not be offered once tombstoned`);
        assert.match(row.disabledReason, /deleted/);
    }
});

test('a refusal the owner can ANSWER says so, and carries the object that answers it', () => {
    // T3R2-H6/L3. `acknowledge_checkout_dirty` is the server's only escape from
    // `checkout_dirty`, and it had NO producer in any client code: one stray
    // build.log made merge-back permanently unreachable. `decision` is T2's typed
    // git_init_required offer, whose yes is apiClient.projectInitGit.
    const dirty = describeOutcome({
        ok: false,
        reason: 'checkout_dirty',
        message: 'This thread\'s checkout has changes that were never committed.',
        dirty_files: ['?? build.log'],
        acknowledgeable: true,
    });
    assert.equal(dirty.acknowledgeable, true);
    assert.deepEqual(dirty.evidence, ['?? build.log']);

    // The branch being WRONG is deliberately not acknowledgeable, and neither is
    // a project folder standing on no branch at all.
    for (const reason of ['checkout_head_off_branch', 'project_head_detached']) {
        assert.equal(describeOutcome({ ok: false, reason }).acknowledgeable, false);
    }

    const offer = describeOutcome({
        ok: false,
        reason: 'git_init_required',
        message: 'This folder is not tracked by git yet.',
        decision: { decision: 'git_init_required', offer: 'init_git', enables: ['branching'] },
    });
    assert.equal(offer.decision.offer, 'init_git');
    assert.equal(describeOutcome({ ok: false, reason: 'branch_failed' }).decision, null);
});

test('merge-back carries the owner\'s acknowledgement as its own argument', async () => {
    // Mirrors removeWorktree: a separate argument so nobody passes it by accident.
    const { threadOps } = await import('../modules/project_thread_actions.js');
    const { apiClient } = await import('../modules/api_client.js');
    const seen = [];
    const original = apiClient.threadMergeBack;
    apiClient.threadMergeBack = (...args) => { seen.push(args); return Promise.resolve({}); };
    try {
        await threadOps.mergeBack('racer', 2);
        await threadOps.mergeBack('racer', 2, true);
    } finally {
        apiClient.threadMergeBack = original;
    }
    assert.deepEqual(seen, [['racer', 2, false], ['racer', 2, true]]);
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

test('a removal says whether the branch went with it (T3R-5)', () => {
    // The disclosure has to REACH the owner: a branch that survived is exactly
    // what the next branch-off refuses on, and meeting that later as a bare
    // "branch already exists" is what this sentence prevents.
    assert.match(
        successText({ removed: true, branch: 'thread/racer__1', branch_removed: true }),
        /branch thread\/racer__1 with it — this thread can branch off again/,
    );
    const kept = successText({
        removed: true,
        branch: 'thread/racer__1',
        branch_removed: false,
        branch_kept_reason: 'the checkout held unmerged work, so its branch keeps the commits',
    });
    assert.match(kept, /was kept — the checkout held unmerged work/);
    assert.match(kept, /Branching off again will refuse/);
    // Nothing known about the branch: no invented claim either way.
    assert.equal(successText({ removed: true }), 'Checkout removed.');
});

test('an acknowledged merge still says what stayed in the checkout', () => {
    // Acknowledging that work is left behind is not the same as forgetting it was.
    const base = successText({ merged: true, worktree_kept: true });
    assert.ok(!/stayed in the checkout/.test(base));

    const withLeftovers = successText({
        merged: true,
        worktree_kept: true,
        checkout_left_behind: ['?? scratch.log', ' M feature.txt'],
    });
    assert.match(withLeftovers, /keeps its checkout until you remove it/);
    assert.match(withLeftovers, /2 uncommitted changes stayed in the checkout/);
    assert.match(withLeftovers, /a merge brings commits only/);

    assert.match(
        successText({ merged: true, checkout_left_behind: ['?? one.log'] }),
        /1 uncommitted change stayed/,
    );
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

test('a snapshot receipt does not call an ALREADY TRACKED file untracked (T3R-1)', () => {
    // The old wording said every credential-shaped file was "still untracked".
    // For a file git already tracked that was true only because the snapshot had
    // just untracked it — by committing a DELETION on the owner's branch. Now the
    // file is snapshotted like any other tracked file, so the receipt says so.
    const text = snapshotReceipt({
        snapshot_commit: {
            created: true,
            sha: 'abc',
            skipped_sensitive: ['.env'],
            tracked_sensitive: ['tests/fixtures/token.json'],
        },
    });
    assert.match(text, /still in your folder, still untracked\): \.env/);
    assert.match(text, /Already tracked by git, so committed with everything else: tests\/fixtures\/token\.json/);
    // The two facts are never merged into one claim about all of them.
    assert.ok(!/still untracked\): [^.]*token\.json/.test(text));

    // A snapshot with only tracked ones says nothing about untracked files.
    const trackedOnly = snapshotReceipt({
        snapshot_commit: { created: true, sha: 'abc', tracked_sensitive: ['secrets.yml'] },
    });
    assert.ok(!/still untracked/.test(trackedOnly));
    assert.match(trackedOnly, /secrets\.yml/);
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
