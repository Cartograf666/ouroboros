import assert from 'node:assert/strict';
import test from 'node:test';

import { accountRows } from '../modules/harness_accounts.js';
import {
    DELEGATION_OFF,
    composeSubagentRoute,
    connectedHarnesses,
    delegationView,
    parseSubagentRoute,
} from '../modules/subagents_settings.js';

// The wire body the Harness Accounts panel really consumes: `profiles` is an
// array of {profile, status, identity} wrappers and `harnessAccounts` an array
// of per-harness authority rows (Claudexor credential-profile.ts).
function statusPayload({ native = [], profiles = [], harnesses = [] } = {}) {
    return {
        daemon: { state: 'running' },
        harnesses,
        profiles: { harnessAccounts: native, profiles },
    };
}

test('connected means the accounts panel already says connected — not merely discovered', () => {
    // A harness the daemon DISCOVERED has no account behind it: offering it as a
    // delegation target produces a route whose every dispatch silently falls back
    // to a native child. The section must read the same fact the accounts rows do.
    const payload = statusPayload({
        harnesses: [
            { id: 'codex', display_name: 'Codex CLI' },
            { id: 'cursor', display_name: 'Cursor' },
        ],
        native: [
            { harness_id: 'codex', native_login_detected: true },
            { harness_id: 'cursor', native_login_detected: false },
        ],
    });
    assert.deepEqual(connectedHarnesses(payload), [{ id: 'codex', label: 'Codex CLI' }]);
    // Same source, same answer: cursor has a row in the accounts panel too, it
    // just is not logged in.
    assert.equal(accountRows(payload).length, 2);

    // A named credential profile counts, and one harness is listed once.
    const withProfile = statusPayload({
        harnesses: [{ id: 'claude', display_name: 'Claude Code' }],
        native: [{ harness_id: 'claude', native_login_detected: true }],
        profiles: [{ profile: { harness_id: 'claude', profile_id: 'valentine' },
                     status: { verification: 'passed', verification_source: 'vendor' } }],
    });
    assert.deepEqual(connectedHarnesses(withProfile).map((h) => h.id), ['claude']);

    // Nothing readable at all is not a connection.
    assert.deepEqual(connectedHarnesses(null), []);
    assert.deepEqual(connectedHarnesses(statusPayload()), []);
});

test('with no subscription connected the section explains instead of offering a dead toggle', () => {
    const view = delegationView({ saved: '', payload: statusPayload() });
    assert.equal(view.state, 'no_subscription');
    assert.equal(view.enabled, false);
    assert.deepEqual(view.options, []);
    assert.match(view.note, /Harness Accounts/);
});

test('a failed accounts read is not the same sentence as "nothing connected"', () => {
    // Blaming the owner's accounts for this page failing to ask is the kind of
    // lie the reviewer-slot banner separation already fixed once.
    const view = delegationView({ saved: '', payload: null, statusError: 'HTTP 503' });
    assert.equal(view.state, 'unknown');
    assert.equal(view.enabled, false);
    assert.match(view.note, /HTTP 503/);
    assert.doesNotMatch(view.note, /No coding-agent subscription/);
});

test('one connected subscription turns delegation on by default, and says it is not saved yet', () => {
    const payload = statusPayload({
        harnesses: [{ id: 'codex', display_name: 'Codex CLI' }],
        native: [{ harness_id: 'codex', native_login_detected: true }],
    });
    const view = delegationView({ saved: '', payload });
    assert.equal(view.state, 'default_on');
    assert.equal(view.enabled, true);
    assert.equal(view.harness, 'codex');
    // The runtime default is still OFF until the value is saved; saying "on"
    // without saying that would misreport where subagents actually run.
    assert.match(view.note, /Save Settings/);
});

test('an owner who turns delegation off stays off — empty and off are different answers', () => {
    // Empty is "never decided", so the connected-subscription default may fill
    // it. `off` is a decision, and re-defaulting over it would make the owner's
    // Off un-saveable: it would come back On on every reload.
    const payload = statusPayload({
        harnesses: [{ id: 'codex', display_name: 'Codex CLI' }],
        native: [{ harness_id: 'codex', native_login_detected: true }],
    });
    const view = delegationView({ saved: DELEGATION_OFF, payload });
    assert.equal(view.state, 'off');
    assert.equal(view.enabled, false);
    assert.equal(composeSubagentRoute('', ''), DELEGATION_OFF);
    assert.deepEqual(parseSubagentRoute(DELEGATION_OFF), { harness: '', suffix: '', decided: true });
    assert.deepEqual(parseSubagentRoute(''), { harness: '', suffix: '', decided: false });
});

test('a saved route survives a discovery list that no longer contains its harness', () => {
    // Same rule as the reviewer rows: redrawing the row as the first connected
    // entry would make the next Save re-point delegation at an account the owner
    // never chose — and it would do it silently, while the daemon is down.
    const payload = statusPayload({
        harnesses: [{ id: 'codex', display_name: 'Codex CLI' }],
        native: [{ harness_id: 'codex', native_login_detected: true }],
    });
    const view = delegationView({ saved: 'claude', payload });
    assert.equal(view.state, 'on');
    assert.equal(view.harness, 'claude');
    assert.deepEqual(view.options.map((o) => o.id), ['codex', 'claude']);
    assert.match(view.options[1].label, /no account connected/);
    assert.match(view.note, /ordinary subagent on the API/);
});

test('the muted sentence follows the owner edit, never the value underneath it', () => {
    // Caught live on the stand: switching the select to "Subagents run on the
    // API" left "On by default … Save Settings to apply it" under it, because
    // the note was computed from the SAVED value while the controls showed the
    // edit. The edit goes through the same view for exactly this reason.
    const payload = statusPayload({
        harnesses: [{ id: 'codex', display_name: 'Codex CLI' },
                    { id: 'claude', display_name: 'Claude Code' }],
        native: [{ harness_id: 'codex', native_login_detected: true },
                 { harness_id: 'claude', native_login_detected: true }],
    });
    const turnedOff = delegationView({ saved: '', payload, edit: { enabled: false, harness: '' } });
    assert.equal(turnedOff.state, 'off');
    assert.equal(turnedOff.harness, '');
    assert.doesNotMatch(turnedOff.note, /by default/);

    // Turning it back on resolves the route again without the caller storing one.
    const backOn = delegationView({ saved: '', payload, edit: { enabled: true, harness: '' } });
    assert.equal(backOn.harness, 'codex');

    // Picking the other connected subscription is honoured over the default.
    const picked = delegationView({ saved: '', payload, edit: { enabled: true, harness: 'claude' } });
    assert.equal(picked.harness, 'claude');
    assert.equal(picked.state, 'default_on');
});

test('a hand-written model/effort tail rides through untouched', () => {
    // The section authors the HARNESS only (model and effort come from each
    // call's own axes), so it must not silently delete a tail the owner wrote.
    assert.deepEqual(parseSubagentRoute('codex=gpt-5.6:high'),
        { harness: 'codex', suffix: '=gpt-5.6:high', decided: true });
    assert.equal(composeSubagentRoute('codex', '=gpt-5.6:high'), 'codex=gpt-5.6:high');

    const payload = statusPayload({
        harnesses: [{ id: 'codex', display_name: 'Codex CLI' }],
        native: [{ harness_id: 'codex', native_login_detected: true }],
    });
    const view = delegationView({ saved: 'codex=gpt-5.6:high', payload });
    assert.equal(view.harness, 'codex');
    assert.equal(view.suffix, '=gpt-5.6:high');
    assert.equal(composeSubagentRoute(view.harness, view.suffix), 'codex=gpt-5.6:high');
});
