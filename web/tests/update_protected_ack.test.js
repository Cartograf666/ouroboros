import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

// `applyUpdate` is closure-scoped inside `initUpdates`, which builds real DOM via innerHTML +
// querySelector, and this repo has no DOM harness. So the protected-acknowledgement handshake is
// pinned at SOURCE, the way cancel_run.test.js pins the cancel branches: `node --check` accepts an
// inverted condition or a deleted branch, these assertions do not.
const source = readFileSync(new URL('../modules/updates.js', import.meta.url), 'utf8');

const firstDisclosure = source.indexOf("let data = await postApply({ strategy });");
const reAck = source.indexOf('acknowledged_protected_paths: data.protected_paths');
const genericManual = source.indexOf('Update needs manual handling');

test('the update apply handshake keeps all three branches in order', () => {
    assert.ok(firstDisclosure > 0, 'applyUpdate must POST the apply once before any disclosure');
    assert.ok(reAck > firstDisclosure, 'the bound acknowledgement re-POST follows the disclosure');
    assert.ok(genericManual > reAck, 'the generic manual toast is the LAST resort, after both');
});

test('a moved release on the acknowledged re-POST reports the race, not a dead end', () => {
    // The backend refuses a now-stale echo by answering with a FRESH disclosure (status manual +
    // requires_acknowledgement + new SHAs). Falling through to the generic branch would tell the
    // owner the update "needs manual handling", which is false: the next step is one more click.
    const postAck = source.slice(reAck, genericManual);
    assert.match(postAck, /if \(data\.status === 'manual' && data\.requires_acknowledgement\)/);
    assert.match(postAck, /release moved since you confirmed/);
    assert.match(postAck, /Click Update again/);
    // The branch must hand the button back and stop, so the click is genuinely re-armed.
    assert.match(postAck, /restoreBtn\(\);\s*return;/);
});

test('the fresh disclosure is re-prompted by a new click, never auto-looped in place', () => {
    // One dialog per disclosure is what keeps the acknowledgement honest: re-opening confirm()
    // inside the same handler would let a moved release be acknowledged from a stale reading.
    const postAck = source.slice(reAck, genericManual);
    assert.doesNotMatch(postAck, /confirmProtectedAck/);
    assert.doesNotMatch(postAck, /await postApply/);
});

test('the disclosure check is applied to BOTH responses, not just the first', () => {
    const applyUpdate = source.slice(firstDisclosure, genericManual);
    const checks = applyUpdate.match(
        /data\.status === 'manual' && data\.requires_acknowledgement/g,
    ) || [];
    assert.equal(checks.length, 2);
});

test('a declined disclosure is reported as cancelled, distinctly from the moved-release race', () => {
    // Three outcomes, three messages: the owner must be able to tell "you said no" from
    // "the release moved" from "this genuinely needs manual handling".
    assert.match(source, /Update cancelled — protected changes were not acknowledged\./);
    assert.match(source, /the official delta could not be verified/);
});
