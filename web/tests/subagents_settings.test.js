import assert from 'node:assert/strict';
import test from 'node:test';

import { createClaudexorStatusStore } from '../modules/claudexor_status_store.js';
import { accountRows } from '../modules/harness_accounts.js';
import {
    DELEGATION_OFF,
    applySubagentsSettings,
    collectSubagentsSettings,
    composeSubagentRoute,
    composeSubagentTail,
    connectedHarnesses,
    delegationView,
    destroySubagentsSection,
    initSubagentsSection,
    lastDelegationLine,
    parseSubagentRoute,
    reloadSubagentsSection,
    renderSignature,
    splitSubagentTail,
} from '../modules/subagents_settings.js';

// The wire body the Harness Accounts panel really consumes: `profiles` is an
// array of {profile, status, identity} wrappers and `harnessAccounts` an array
// of per-harness authority rows (Claudexor credential-profile.ts).
function statusPayload({ native = [], profiles = [], harnesses = [], quota = [] } = {}) {
    // Shaped like the producer's own answer — daemon/harnesses/profiles/quota
    // are unconditional there, and the store now requires all four before it
    // will derive a facet from a 2xx body.
    return {
        daemon: { state: 'running' },
        config_dir: '/home/agent',
        harnesses,
        profiles: { harnessAccounts: native, profiles },
        quota,
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

test('a FAILED verification is not connected — the same red the accounts panel shows', () => {
    // The accounts panel renders verification!=='passed' as an error badge
    // (accountLoginConfirmed === false). Counting the same row as "connected"
    // here printed "a subscription is connected" over a red account and let the
    // default-on rule auto-pin a route whose every dispatch falls back to native.
    const failed = statusPayload({
        harnesses: [{ id: 'claude', display_name: 'Claude Code' }],
        profiles: [{ profile: { harness_id: 'claude', profile_id: 'valentine' },
                     status: { verification: 'failed', verification_source: 'vendor' } }],
    });
    assert.deepEqual(connectedHarnesses(failed), []);
    const view = delegationView({ saved: '', payload: failed });
    assert.equal(view.state, 'no_subscription');
    assert.equal(view.enabled, false);
});

test('subscription-first: a local session is enough to turn delegation on by default', () => {
    // OWNER DECISION 2026-08-09 (guard, not a characterization). A review lens
    // recommended requiring vendor-level verification before the default-on
    // rule fires, because a `local_store` session is only detected on disk, not
    // re-proved against the vendor. The owner DECLINED: Ouroboros prefers
    // subscriptions over the API budget, and that gate would leave delegation
    // off on exactly the machines that have a subscription sitting right there.
    // This test fails if a future change narrows the rule.
    const nativeOnly = {
        reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' },
        daemon: { state: 'running' },
        harnesses: [{ id: 'codex', display_name: 'Codex CLI' }],
        profiles: { profiles: [], harnessAccounts: [{ harness_id: 'codex', native_login_detected: true }] },
        quota: [],
    };
    const connected = connectedHarnesses(nativeOnly);
    assert.deepEqual(connected.map((c) => c.id), ['codex'],
        'a detected local session counts as a connected subscription');

    const view = delegationView({ saved: '', payload: nativeOnly });
    assert.equal(view.state, 'default_on', 'delegation defaults ON from a local session');
    assert.equal(view.harness, 'codex');

    // The same holds for a NAMED profile whose verification came from the local
    // store rather than a vendor probe.
    const localNamed = {
        reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' },
        daemon: { state: 'running' },
        harnesses: [{ id: 'claude', display_name: 'Claude Code' }],
        profiles: {
            profiles: [{
                profile: { harness_id: 'claude', profile_id: 'mironov', kind: 'config_dir_login' },
                status: { verification: 'passed', verification_source: 'local_store' },
                identity: {},
            }],
            harnessAccounts: [],
        },
        quota: [],
    };
    assert.deepEqual(connectedHarnesses(localNamed).map((c) => c.id), ['claude']);
    assert.equal(delegationView({ saved: '', payload: localNamed }).state, 'default_on');
});

test('with no subscription connected the section explains instead of offering a dead toggle', () => {
    const view = delegationView({ saved: '', payload: statusPayload() });
    assert.equal(view.state, 'no_subscription');
    assert.equal(view.enabled, false);
    assert.deepEqual(view.options, []);
    // The pointer now names a place on the SAME tab. It used to send the owner
    // to the accounts section under the Providers tab, two tabs away from the
    // control it explained; accounts and delegation live together in Agents (D-10).
    assert.match(view.note, /Accounts above/);
    assert.doesNotMatch(view.note, /Providers/);
});

test('a failed accounts read is not the same sentence as "nothing connected"', () => {
    // Blaming the owner's accounts for this page failing to ask is the kind of
    // lie the reviewer-slot banner separation already fixed once.
    const view = delegationView({ saved: '', payload: null, statusError: 'HTTP 503' });
    assert.equal(view.state, 'unknown');
    assert.equal(view.enabled, false);
    assert.match(view.note, /HTTP 503/);
    assert.doesNotMatch(view.note, /No agent subscription/);
});

test('a failed accounts read suppresses the controls even over a saved harness', () => {
    // With saved='codex' + statusError the view used to fall through to state
    // 'on' with live controls: the owner could flip delegation Off, press Save,
    // read "Settings saved" — while collect() returned {} and nothing moved.
    // The read failure decides the WHOLE view, and the note stops at the error:
    // this page failing to read the accounts says nothing about the daemon-side
    // account, so no fallback claim is earned.
    const view = delegationView({ saved: 'codex', payload: null, statusError: 'daemon down' });
    assert.equal(view.state, 'unknown');
    assert.equal(view.enabled, false);
    assert.deepEqual(view.options, []);
    assert.match(view.note, /daemon down/);
    assert.doesNotMatch(view.note, /ordinary subagent on the API/);
    assert.doesNotMatch(view.note, /no account connected/);
});

test('a decided "off" is never promised to turn itself back on', () => {
    // `off` sets decided:true, so defaultOn stays false and a newly connected
    // subscription will NOT re-enable delegation — the sentence must not claim
    // the opposite (connect → turn Off → Save → sign out → reopen Settings).
    const view = delegationView({ saved: DELEGATION_OFF, payload: statusPayload() });
    assert.equal(view.state, 'no_subscription');
    assert.doesNotMatch(view.note, /turns on by itself/);
    assert.match(view.note, /you turned it off/);
});

test('before the accounts have been read the section says it is reading, not "no subscription"', () => {
    // applySubagentsSettings renders immediately while the accounts arrive later:
    // while !loaded the view cannot tell "not read yet" from "read and found
    // nothing", so a connected owner briefly saw "No agent subscription".
    const view = delegationView({ saved: '', payload: null, loaded: false });
    assert.equal(view.state, 'loading');
    assert.equal(view.enabled, false);
    assert.match(view.note, /Reading your agent accounts/);
    assert.doesNotMatch(view.note, /No agent subscription/);
});

test('the money copy never claims delegation moves the subagent itself off the API', () => {
    // A delegated subagent is a NANNY: the coding rides the subscription while
    // the subagent's own rounds stay metered. "spend a connected subscription
    // instead" overstated the saving on the very control that authors it.
    const payload = statusPayload({
        harnesses: [{ id: 'codex', display_name: 'Codex CLI' }],
        native: [{ harness_id: 'codex', native_login_detected: true }],
    });
    const off = delegationView({ saved: DELEGATION_OFF, payload });
    assert.doesNotMatch(off.note, /instead/);
    assert.match(off.note, /send their work to a connected subscription/);
    const on = delegationView({ saved: 'codex', payload });
    assert.match(on.note, /still runs on the API/);
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

test('an untouched save still carries a hand-written model/effort tail verbatim', () => {
    // The model select now AUTHORS the `=model` half of the tail, but an
    // untouched section must still recompose the saved value byte-identically
    // — including the hand-written `:effort` remainder, which has no control
    // here and rides through verbatim.
    assert.deepEqual(parseSubagentRoute('codex=gpt-5.6:high'),
        { harness: 'codex', suffix: '=gpt-5.6:high', decided: true });
    assert.equal(composeSubagentRoute('codex', '=gpt-5.6:high'), 'codex=gpt-5.6:high');
    assert.deepEqual(splitSubagentTail('=gpt-5.6:high'), { model: 'gpt-5.6', effortSuffix: ':high' });
    assert.equal(composeSubagentTail('gpt-5.6', ':high'), '=gpt-5.6:high');
    // Degenerate tails round-trip too: effort with no model, model alone, nothing.
    assert.deepEqual(splitSubagentTail('=:high'), { model: '', effortSuffix: ':high' });
    assert.equal(composeSubagentTail('', ':high'), '=:high');
    assert.equal(composeSubagentTail('', ''), '');

    const payload = statusPayload({
        harnesses: [{ id: 'codex', display_name: 'Codex CLI' },
                    { id: 'claude', display_name: 'Claude Code' }],
        native: [{ harness_id: 'codex', native_login_detected: true },
                 { harness_id: 'claude', native_login_detected: true }],
    });
    const view = delegationView({ saved: 'codex=gpt-5.6:high', payload });
    assert.equal(view.harness, 'codex');
    assert.equal(view.model, 'gpt-5.6');
    assert.equal(view.suffix, '=gpt-5.6:high');
    assert.equal(composeSubagentRoute(view.harness, view.suffix), 'codex=gpt-5.6:high');

    // ...and is DROPPED when the harness changes: the tail was written for the
    // harness it named, and carrying it over would pin a model the other
    // harness may not serve (the guarded branch at the suffix computation).
    const switched = delegationView({
        saved: 'codex=gpt-5.6:high', payload, edit: { enabled: true, harness: 'claude' },
    });
    assert.equal(switched.harness, 'claude');
    assert.equal(switched.suffix, '');
    assert.equal(composeSubagentRoute(switched.harness, switched.suffix), 'claude');
});

test('the model select edits the =model tail and keeps the effort remainder', () => {
    const payload = statusPayload({
        harnesses: [{ id: 'codex', display_name: 'Codex CLI',
                      models: [{ id: 'gpt-5.6' }, { id: 'gpt-5.6-luna' }] }],
        native: [{ harness_id: 'codex', native_login_detected: true }],
    });
    // Picking a model composes it into the tail; the hand-written :high stays.
    const picked = delegationView({ saved: 'codex=gpt-5.6:high', payload,
        edit: { enabled: true, harness: 'codex', model: 'gpt-5.6-luna' } });
    assert.equal(picked.suffix, '=gpt-5.6-luna:high');
    assert.equal(composeSubagentRoute(picked.harness, picked.suffix), 'codex=gpt-5.6-luna:high');
    // Choosing "Engine default model" ('') drops the model but not the effort.
    const cleared = delegationView({ saved: 'codex=gpt-5.6:high', payload,
        edit: { enabled: true, harness: 'codex', model: '' } });
    assert.equal(cleared.suffix, '=:high');
    // The options come from discovery with the engine default first.
    assert.deepEqual(picked.modelOptions.map((o) => o.value), ['', 'gpt-5.6', 'gpt-5.6-luna']);
});

test('a saved model missing from discovery keeps a not-in-discovery option', () => {
    // Save with the daemon down (or the model delisted) must not silently
    // erase the owner's pin: the select's value must EXIST as an option.
    const payload = statusPayload({
        harnesses: [{ id: 'codex', display_name: 'Codex CLI', models: [{ id: 'other' }] }],
        native: [{ harness_id: 'codex', native_login_detected: true }],
    });
    const view = delegationView({ saved: 'codex=gpt-5.6', payload });
    assert.deepEqual(view.modelOptions.map((o) => o.value), ['', 'other', 'gpt-5.6']);
    assert.match(view.modelOptions[2].label, /not in discovery/);
    assert.equal(view.suffix, '=gpt-5.6');
});

test('a saved delegation model is not called undiscovered before the catalog was read', () => {
    // Rebased provenance pin (invariant 5): the model select rides the CATALOG
    // facet, independently of the accounts facet that decides the view. With
    // the accounts read (so the section is live and shows its controls) and the
    // catalog unread, the saved pin keeps its option labelled "(not checked)" —
    // an accusation about a catalog nobody opened stays unearned.
    const saved = 'codex=gpt-5.6-sol';
    const unread = delegationView({
        saved,
        payload: statusPayload({ native: [{ harness_id: 'codex', native_login_detected: true }] }),
        accountsRead: 'ok', catalogRead: 'not_read',
    });
    assert.equal(unread.state, 'on', 'the accounts facet still decides the view');
    const unreadPin = (unread.modelOptions || []).find((o) => o.value === 'gpt-5.6-sol');
    assert.ok(unreadPin, 'the saved model lost its option');
    assert.match(unreadPin.label, /not checked/);
    assert.doesNotMatch(unreadPin.label, /not in discovery/);
    // …and the catalog gap itself is named in the note (subject, not enum).
    assert.match(unread.note, /Agents were not read/);

    // Read catalog, model genuinely absent → the honest accusation is allowed.
    const read = delegationView({
        saved,
        payload: statusPayload({
            harnesses: [{ id: 'codex', models: [{ id: 'other-model' }] }],
            native: [{ harness_id: 'codex', native_login_detected: true }],
        }),
        accountsRead: 'ok', catalogRead: 'ok',
    });
    const readPin = (read.modelOptions || []).find((o) => o.value === 'gpt-5.6-sol');
    assert.ok(readPin && /not in discovery/.test(readPin.label),
        'a read catalog must still be able to say a model is missing');
});

test('switching the harness resets the model to a visible Engine default', () => {
    // Accepted residual (owner spec): the tail belongs to the harness it was
    // written for, so a switch DROPS it — visibly, before Save, as the
    // "Engine default model" selection — rather than pinning a model the new
    // harness may not serve. No per-harness memory is built.
    const payload = statusPayload({
        harnesses: [{ id: 'codex', display_name: 'Codex CLI' },
                    { id: 'claude', display_name: 'Claude Code', models: [{ id: 'sonnet' }] }],
        native: [{ harness_id: 'codex', native_login_detected: true },
                 { harness_id: 'claude', native_login_detected: true }],
    });
    const switched = delegationView({ saved: 'codex=gpt-5.6:high', payload,
        edit: { enabled: true, harness: 'claude', model: null } });
    assert.equal(switched.harness, 'claude');
    assert.equal(switched.model, '');
    assert.equal(switched.suffix, '');
    assert.equal(composeSubagentRoute(switched.harness, switched.suffix), 'claude');
    // Switching back restores the SAVED tail (it was never deleted un-saved).
    const back = delegationView({ saved: 'codex=gpt-5.6:high', payload,
        edit: { enabled: true, harness: 'codex', model: null } });
    assert.equal(back.suffix, '=gpt-5.6:high');
});

test('the last-delegated-run line discloses mismatch and shows absence as absence', () => {
    // No record (or a recordless {}), no line — never a dressed-up default.
    assert.equal(lastDelegationLine(null), '');
    assert.equal(lastDelegationLine({}), '');
    // The plain receipt: route · applied model · age.
    const line = lastDelegationLine({ ts: new Date().toISOString(), route: 'claude',
        requested_model: 'sonnet', applied_model: 'sonnet', run_id: 'r1' });
    assert.match(line, /^Last delegated run: claude · sonnet · /);
    // requested≠applied is disclosed loudly (advisory, not a failure)…
    const mismatch = lastDelegationLine({ ts: new Date().toISOString(), route: 'claude',
        requested_model: 'sonnet', applied_model: 'claude-opus-5', run_id: 'r2' });
    assert.ok(mismatch.includes('requested sonnet → ran claude-opus-5'));
    // …and an undisclosed applied model says so instead of echoing the request.
    const bare = lastDelegationLine({ ts: new Date().toISOString(), route: 'claude',
        requested_model: 'sonnet', applied_model: '', run_id: 'r3' });
    assert.ok(bare.includes('model not disclosed'));
    assert.ok(!bare.includes('sonnet'));
});

// ---------------------------------------------------------------------------
// Phase 2: "we could not ask" and "we asked and the daemon is down" are two
// different sentences, and NEITHER earns a row-level claim about the owner's
// saved harness. Before the shared store, a stopped daemon reached this
// section as a successful read with no harnesses — so a saved route was
// labeled "(no account connected)" and the note announced an API fallback
// nobody had established.
// ---------------------------------------------------------------------------

test('a stopped daemon explains itself instead of accusing the saved harness', () => {
    const view = delegationView({ saved: 'codex', payload: { daemon: { state: 'stale' }, harnesses: [] },
        accountsRead: 'not_read' });
    assert.equal(view.state, 'unknown');
    assert.equal(view.enabled, false);
    assert.deepEqual(view.options, []);
    // The shared sentence states what the read state establishes — nobody
    // asked — and never diagnoses a daemon this section cannot see.
    assert.match(view.note, /daemon was not asked/);
    assert.doesNotMatch(view.note, /is not running/);
    assert.match(view.note, /saved choices are unchanged/);
    assert.doesNotMatch(view.note, /no account connected/);
    assert.doesNotMatch(view.note, /ordinary subagent on the API/);
});

test('a refused accounts read never claims the daemon is not running', () => {
    // Rebased provenance pin: a per-facet `failed` (or a global `unreachable`)
    // is a read that did not land — the daemon may well be running, and one
    // endpoint died. Saying "not running" would be a second lie; saying
    // "was not asked" would be a third (somebody did ask).
    const view = delegationView({ saved: '', payload: statusPayload(), accountsRead: 'failed' });
    assert.equal(view.state, 'unknown');
    assert.match(view.note, /could not be read/);
    assert.doesNotMatch(view.note, /not running/);
    assert.doesNotMatch(view.note, /was not asked/);
});

test('a fresh read with a genuinely absent harness still says so', () => {
    // The row-level claim is not deleted — it is now EARNED. A daemon that
    // answered and simply does not have this account keeps the honest label.
    const view = delegationView({ saved: 'codex', payload: statusPayload(), accountsRead: 'ok' });
    assert.match(view.note, /No connected account for codex/);
    assert.ok(view.options.some((o) => /no account connected/.test(o.label)),
        'the saved harness keeps its option so a Save cannot re-point delegation');
});

test('a transport failure and a stopped daemon are not the same sentence', () => {
    const dead = delegationView({ saved: '', payload: null, statusError: 'HTTP 503' });
    const down = delegationView({ saved: '', payload: null, accountsRead: 'not_read' });
    assert.equal(dead.state, 'unknown');
    assert.equal(down.state, 'unknown');
    assert.match(dead.note, /HTTP 503/);
    assert.doesNotMatch(down.note, /HTTP 503/);
    assert.notEqual(dead.note, down.note);
});

test('a CATALOG gap is named too, instead of being dropped behind the accounts sentence', () => {
    // This section renders two facets: the accounts decide the view, the model
    // select rides the catalog. With the accounts read fine and the catalog
    // refused, the model list quietly narrowed to whatever the last read held
    // and the note said nothing at all about it.
    const payload = statusPayload({
        harnesses: [{ id: 'codex', display_name: 'Codex CLI', models: [{ id: 'gpt-5.6' }] }],
        native: [{ harness_id: 'codex', native_login_detected: true }],
    });
    const gap = delegationView({ saved: 'codex', payload, accountsRead: 'ok', catalogRead: 'failed' });
    assert.equal(gap.state, 'on', 'the accounts facet still decides the view');
    assert.match(gap.note, /Agents were not read/);
    assert.match(gap.note, /last known/);
    // The saved model pin keeps its option and loses only the earned claim.
    assert.doesNotMatch(JSON.stringify(gap.modelOptions), /not in discovery/);

    // A healthy catalog adds nothing…
    const clean = delegationView({ saved: 'codex', payload, accountsRead: 'ok', catalogRead: 'ok' });
    assert.doesNotMatch(clean.note, /were not read/);
    // …and a catalog in the SAME state as the accounts facet is STILL named:
    // equal state is not equal subject, and the accounts sentence says nothing
    // about agent discovery. Dropping it left the model select unexplained.
    const both = delegationView({ saved: 'codex', payload: null, accountsRead: 'not_read', catalogRead: 'not_read' });
    assert.match(both.note, /daemon was not asked/);
    assert.match(both.note, /Agents were not read/);
    assert.equal(both.note.match(/could not be read/g), null);
});

// ---------------------------------------------------------------------------
// The live subscription: what makes the section repaint, and whether it can
// keep the shared poll armed at all.
// ---------------------------------------------------------------------------

function fakeSurface() {
    // The minimum this section's renderRows() touches, plus the offsetParent
    // that answers "is this surface on screen".
    const host = { innerHTML: '', offsetParent: {}, querySelector: () => null };
    const doc = {
        hidden: false,
        getElementById: (id) => (id === 'subagents-rows' ? host : null),
        addEventListener() {}, removeEventListener() {},
    };
    const win = { addEventListener() {}, removeEventListener() {} };
    return { host, doc, win };
}

test('the repaint signature is keyed on model IDENTITY, not on how many there are', () => {
    // Pure form of the defect: same count, different model.
    const store = (models) => ({
        reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' },
        error: '',
        snapshot: {
            daemon: { state: 'running' },
            harnesses: [{ id: 'codex', display_name: 'Codex CLI', models }],
            profiles: { harnessAccounts: [{ harness_id: 'codex', native_login_detected: true }], profiles: [] },
        },
    });
    const before = JSON.stringify(renderSignature(store([{ id: 'old-model' }])));
    const after = JSON.stringify(renderSignature(store([{ id: 'new-model' }])));
    assert.notEqual(before, after, 'a model swapped at equal count must change the signature');
    assert.equal(before, JSON.stringify(renderSignature(store([{ id: 'old-model' }]))), 'and be stable otherwise');
});

test('a model swapped at EQUAL count really reaches the pixels', async () => {
    // The subscriber skipped the repaint because the signature counted models
    // instead of naming them: the section kept offering the model that no
    // longer exists and never exposed the one that does — and the select is
    // built from exactly these ids.
    const { host, doc, win } = fakeSurface();
    const priorDoc = globalThis.document;
    const priorWin = globalThis.window;
    globalThis.document = doc;
    globalThis.window = win;
    let models = [{ id: 'old-model' }];
    let modelsError = '';
    const store = createClaudexorStatusStore({
        fetchImpl: async () => ({
            ok: true,
            status: 200,
            json: async () => statusPayload({
                harnesses: [{
                    id: 'codex', display_name: 'Codex CLI', models,
                    ...(modelsError ? { models_error: modelsError } : {}),
                }],
                native: [{ harness_id: 'codex', native_login_detected: true }],
            }),
        }),
        doc,
        pollMs: 5000,
    });
    try {
        initSubagentsSection({ store });
        applySubagentsSettings({ OUROBOROS_SUBAGENT_HARNESS: '' });
        await reloadSubagentsSection();
        assert.ok(host.innerHTML.includes('old-model'), host.innerHTML);
        // The section's own binding is what keeps the shared read alive: a
        // subscriber with no visibility predicate could never arm the poll.
        assert.equal(store.polling, true, 'a visible section keeps the shared poll armed');

        models = [{ id: 'new-model' }];
        await store.refresh();
        assert.ok(host.innerHTML.includes('new-model'), `the swap must reach the DOM: ${host.innerHTML}`);
        assert.ok(!host.innerHTML.includes('old-model'), `and the dead model must go: ${host.innerHTML}`);

        // An unchanged payload still does not repaint (the caret guard).
        host.innerHTML = 'SENTINEL';
        await store.refresh();
        assert.equal(host.innerHTML, 'SENTINEL', 'no repaint when nothing this section renders changed');

        // The typed model-read gap is rendered too, so its ARRIVAL and its
        // clearing must both repaint — the list itself can stay identical
        // across a refused probe, and the signature saw only the list.
        modelsError = 'models_probe_failed';
        await store.refresh();
        assert.ok(host.innerHTML.includes('could not be read'),
            `the model-read gap must reach the DOM: ${host.innerHTML}`);
        modelsError = '';
        await store.refresh();
        assert.ok(!host.innerHTML.includes('could not be read'),
            `and a later successful probe must clear it: ${host.innerHTML}`);
    } finally {
        destroySubagentsSection();
        store.dispose();
        globalThis.document = priorDoc;
        globalThis.window = priorWin;
    }
});

test('the settings collector itself refuses to author from an unknown read', async () => {
    // Rebased provenance pin: the pure-view tests above pass even if the GUARD
    // inside collectSubagentsSettings is deleted — this drives the REAL
    // store-backed reload + collect path, which is what a Save actually calls.
    // An unread account facet (idle lazy daemon) yields an empty harness list
    // on a 2xx; authoring from it would write delegation OFF behind the
    // owner's back on an unrelated Save.
    const { doc, win } = fakeSurface();
    const priorDoc = globalThis.document;
    const priorWin = globalThis.window;
    globalThis.document = doc;
    globalThis.window = win;
    let payload = {
        reads: { catalog: 'not_read', accounts: 'not_read', quota: 'not_read' },
        daemon: { state: 'stale' }, harnesses: [], profiles: {}, quota: [],
    };
    const store = createClaudexorStatusStore({
        fetchImpl: async () => ({ ok: true, status: 200, json: async () => payload }),
        doc,
    });
    try {
        initSubagentsSection({ store });
        applySubagentsSettings({ OUROBOROS_SUBAGENT_HARNESS: '' });
        await reloadSubagentsSection();
        assert.deepEqual(collectSubagentsSettings(), {},
            'an unread account store must never author the delegation route');

        payload = statusPayload({
            harnesses: [{ id: 'codex', display_name: 'Codex' }],
            native: [{ harness_id: 'codex', native_login_detected: true }],
        });
        await reloadSubagentsSection();
        assert.ok('OUROBOROS_SUBAGENT_HARNESS' in collectSubagentsSettings(),
            'a genuinely read store authors normally');
    } finally {
        destroySubagentsSection();
        store.dispose();
        globalThis.document = priorDoc;
        globalThis.window = priorWin;
    }
});
