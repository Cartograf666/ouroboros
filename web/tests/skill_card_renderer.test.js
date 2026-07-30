import assert from 'node:assert/strict';
import test from 'node:test';

import { renderInstalledSkillCard } from '../modules/skill_card_renderer.js';

function skill(overrides = {}) {
    return {
        name: 'telegram',
        type: 'extension',
        version: '1.0.0',
        description: 'Telegram',
        enabled: false,
        source: 'native',
        review_status: 'clean',
        review_stale: false,
        review_gate: { executable_review: true },
        executable_review: true,
        grants: { all_granted: true, missing_keys: [], missing_permissions: [] },
        permissions: [],
        ...overrides,
    };
}

test('extension registration status says Loaded, not Active', () => {
    const html = renderInstalledSkillCard(skill({
        enabled: true,
        live_loaded: true,
        dispatch_live: true,
    }));

    assert.match(html, />Loaded</);
    assert.doesNotMatch(html, />Active</);
});

test('disabled conflicting skill is explained and cannot be enabled', () => {
    const html = renderInstalledSkillCard(skill({
        conflict: { code: 'skill_conflict', skills: ['telegram-bridge'], omitted: 0 },
    }));

    assert.match(html, /Conflicts with telegram-bridge/);
    assert.match(html, /Locked: conflicts with telegram-bridge/);
    assert.match(html, /class="skills-toggle"[^>]*disabled/);
});

test('enabled conflicting skill can still be disabled', () => {
    const html = renderInstalledSkillCard(skill({
        enabled: true,
        conflict: { code: 'skill_conflict', skills: ['telegram-bridge'], omitted: 0 },
    }));

    assert.match(html, /Conflicts with telegram-bridge/);
    assert.doesNotMatch(html, /class="skills-toggle"[^>]*disabled/);
});

test('conflict support preserves the existing Grant access action', () => {
    const html = renderInstalledSkillCard(skill({
        grants: {
            all_granted: false,
            requested_keys: ['TELEGRAM_BOT_TOKEN'],
            missing_keys: ['TELEGRAM_BOT_TOKEN'],
            missing_permissions: [],
        },
    }));

    assert.match(html, />Grant access</);
});
