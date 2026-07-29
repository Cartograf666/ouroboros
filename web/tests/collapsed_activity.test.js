import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import { COLLAPSED_ACTIVITY_MAX, clearStickyCardState, projectCollapsedActivity } from '../modules/chat.js';

test('named root card shows the latest activity headline under the coined title', () => {
    assert.equal(projectCollapsedActivity({
        suggestedName: 'Data Analysis',
        headline: 'Analyzing the dataset',
    }), 'Analyzing the dataset');
});

test('unnamed root card suppresses the line (title already shows the activity)', () => {
    assert.equal(projectCollapsedActivity({
        suggestedName: '',
        headline: 'Analyzing the dataset',
    }), '');
    // Suppressed even when a previous activity was remembered.
    assert.equal(projectCollapsedActivity({
        suggestedName: '',
        headline: '',
        previous: 'Earlier step',
    }), '');
});

test('subagent card always feeds the line from the routed progress body', () => {
    assert.equal(projectCollapsedActivity({
        isSubagent: true,
        body: 'Running the migration script',
    }), 'Running the migration script');
    // No coined name is required — the subagent title keeps role·model·id.
    assert.equal(projectCollapsedActivity({
        isSubagent: true,
        suggestedName: '',
        body: 'Collecting evidence',
    }), 'Collecting evidence');
});

test('a frame without new activity keeps the previous text (Done never blanks it)', () => {
    assert.equal(projectCollapsedActivity({
        suggestedName: 'Data Analysis',
        headline: '',
        previous: 'Analyzing the dataset',
    }), 'Analyzing the dataset');
    assert.equal(projectCollapsedActivity({
        isSubagent: true,
        body: '',
        previous: 'Running the migration script',
    }), 'Running the migration script');
});

test('whitespace-only frames fall back to the previous activity', () => {
    assert.equal(projectCollapsedActivity({
        suggestedName: 'X',
        headline: '   ',
        previous: 'Real step',
    }), 'Real step');
});

test('clearStickyCardState resets the recycled record activity + cost (reusable slots)', () => {
    const record = {
        collapsedActivity: 'Old cycle activity',
        costMeta: { meta: ['cost=$1.00'], ts: 1, final: true },
        // Models the real element closely enough for attribute handling.
        activityEl: {
            textContent: 'Old cycle activity',
            title: '',
            removeAttribute(name) { if (name === 'title') this.title = ''; },
        },
    };
    record.latestActivityTs = '12:00:00';
    record.activityEl.title = 'Old cycle activity';
    clearStickyCardState(record);
    assert.equal(record.collapsedActivity, '');
    assert.equal(record.costMeta, null);
    assert.equal(record.activityEl.textContent, '');
    // The activity clock and the full-text reference are cycle state too.
    assert.equal(record.latestActivityTs, '');
    assert.equal(record.activityEl.title, '');
});

test('a terminal subagent keeps its last narration as collapsed activity (replay)', () => {
    // On history replay terminal children are pre-marked before pass 1, so the
    // card is never re-driven through a working frame; the projection must
    // still return the remembered narration for the collapsed line.
    assert.equal(projectCollapsedActivity({
        isSubagent: true, body: 'Collecting evidence', previous: 'Collecting evidence',
    }), 'Collecting evidence');
    // A later empty frame does not blank it.
    assert.equal(projectCollapsedActivity({
        isSubagent: true, body: '', previous: 'Collecting evidence',
    }), 'Collecting evidence');
});

test('the activity line is fed the FULL text, never the timeline preview', () => {
    // The projection itself never cuts: a long line survives verbatim.
    // Fed from the FULL source (never the timeline preview); the only clipping is
    // the line's own disclosed bound, exercised by the bounded-line test below.
    const mid = 'Reading '.repeat(20).trim();
    assert.equal(projectCollapsedActivity({ suggestedName: 'Data Analysis', headline: mid }), mid);
    assert.equal(projectCollapsedActivity({ isSubagent: true, body: mid }), mid);
    // ...and applyLiveCardState feeds it the uncut companions the summarizer
    // already carries, rather than the bounded `headline`/`body` previews. Pinned
    // at source because the wiring lives in a DOM-driven frame handler.
    const source = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');
    assert.match(source, /summary\.fullHeadline \|\| activeHeadline/);
    assert.match(source, /summary\.fullBody \|\| summary\.body/);
    // The routed child progress carries its uncut line alongside the preview.
    assert.match(source, /body: line\.slice\(0, 200\),[\s\S]{0,220}fullBody: line,/);
});

test('the line is BOUNDED and the bound is disclosed', () => {
    const long = 'x'.repeat(COLLAPSED_ACTIVITY_MAX + 250);
    const out = projectCollapsedActivity({ suggestedName: 'Data Analysis', headline: long });
    assert.equal(out.length, COLLAPSED_ACTIVITY_MAX);
    assert.ok(out.endsWith('…'), 'the cut is visible, never silent');
    // Text within the bound is untouched.
    const short = 'Reading the ledger';
    assert.equal(projectCollapsedActivity({ suggestedName: 'X', headline: short }), short);
    // ...and the complete narration survives on the element itself.
    const source = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');
    assert.match(source, /function renderCollapsedActivity\(record, text\)/);
});

test('a suppressed line does not leak its text through a tooltip', () => {
    // An unnamed root card deliberately shows no activity line; a title attribute
    // would disclose through hover exactly what the suppression withholds.
    const source = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');
    assert.match(source, /if \(text && full && full !== text\) record\.activityEl\.title = full/);
    // BOTH writers go through the one renderer — including the late task_named path.
});

test('every activity writer goes through the one renderer', () => {
    // Three writers exist: frame updates, the late task_named path, and terminal
    // subagent replay. A writer that bypasses the renderer clips to the bound
    // without leaving the complete narration anywhere (BIBLE P1).
    const source = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');
    assert.equal((source.match(/renderCollapsedActivity\(record/g) || []).length, 4);
    assert.doesNotMatch(source, /activityEl\.textContent = projectCollapsedActivity/);
});
