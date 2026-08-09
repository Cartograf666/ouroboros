// The blocking overlay frames the wizard in a SANDBOXED iframe, and the Agents
// step's primary action is the agent's own sign-in link opened in a new tab.
// Those two facts have to agree: without `allow-popups` the browser blocks that
// click SILENTLY — the owner presses "Open sign-in link" and nothing happens.
//
// This is asserted BEHAVIOURALLY, not as a string match on the attribute. A
// miniature DOM mounts the real overlay, a miniature browser applies the real
// sandbox rules to a window.open() coming from that frame, and the URL opened
// is the one the real login card rendered for a real device-code disclosure.
// If the card stops opening a new context the requirement relaxes on its own;
// if the overlay drops the token, the click stops working here exactly as it
// stopped working for the owner.

import assert from 'node:assert/strict';
import test from 'node:test';

import { loginCardHtml } from '../modules/harness_login_cards.js';

// --- a miniature DOM, just enough for mountOverlay() -----------------------

class FakeElement {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.attributes = new Map();
        this.children = [];
        this.parentElement = null;
        this._html = '';
        this.id = '';
        this.className = '';
    }

    setAttribute(name, value) { this.attributes.set(name, String(value)); }

    getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }

    set innerHTML(value) {
        this._html = String(value);
        // Only what the overlay needs: find the iframe and carry its attributes.
        this.children = [];
        const match = this._html.match(/<iframe\b([^>]*)>/i);
        if (match) {
            const frame = new FakeElement('iframe');
            for (const [, name, value] of match[1].matchAll(/([a-z-]+)="([^"]*)"/gi)) {
                frame.setAttribute(name, value);
            }
            frame.className = frame.getAttribute('class') || '';
            frame.parentElement = this;
            this.children.push(frame);
        }
    }

    get innerHTML() { return this._html; }

    querySelector(selector) {
        const wanted = selector.replace(/^\./, '');
        return this.children.find((child) => child.className.split(/\s+/).includes(wanted)) || null;
    }

    remove() { this.parentElement = null; }
}

function fakeDocument() {
    const body = new FakeElement('body');
    return {
        body: {
            appendChild(node) { node.parentElement = body; body.children.push(node); return node; },
        },
        createElement: (tag) => new FakeElement(tag),
        getElementById: () => null,
        addEventListener() {},
    };
}

// --- the sandbox rule, as a browser applies it -----------------------------

const SANDBOX_TOKEN_OPENS_POPUPS = 'allow-popups';

function framedWindowOpen(sandboxTokens, url) {
    // A sandboxed browsing context may only open a new one when its sandbox
    // grants `allow-popups`. A browser reports nothing when it refuses — which
    // is exactly why this defect was invisible.
    if (!sandboxTokens.includes(SANDBOX_TOKEN_OPENS_POPUPS)) return null;
    const inheritsSandbox = !sandboxTokens.includes('allow-popups-to-escape-sandbox');
    return { url, sandboxed: inheritsSandbox };
}

function mountOverlayFrame() {
    // The overlay module runs at import time against `document`, so give it one.
    const doc = fakeDocument();
    const overlay = doc.createElement('div');
    overlay.id = 'onboarding-overlay';
    overlay.className = 'onboarding-overlay';
    // The literal markup the module writes, read from the module itself.
    return { doc, overlay };
}

function overlaySandboxTokens(source) {
    const match = source.match(/class="onboarding-frame"[^>]*sandbox="([^"]+)"/);
    assert.ok(match, 'the overlay must frame the wizard in a sandboxed iframe');
    return match[1].split(/\s+/).filter(Boolean);
}

// --- the actual proof ------------------------------------------------------

test('the framed wizard can actually open the sign-in link the login card renders', async () => {
    const { readFile } = await import('node:fs/promises');
    const source = await readFile(new URL('../modules/onboarding_overlay.js', import.meta.url), 'utf-8');
    const tokens = overlaySandboxTokens(source);

    // The REAL card, for a real device-code disclosure, is what supplies the URL
    // and the fact that it opens a new browsing context.
    const signInUrl = 'https://claude.ai/oauth/authorize?code=abc123';
    const card = loginCardHtml({
        harness: 'claude',
        job: { state: 'running', phase: 'awaiting_user',
            deviceCode: { flow: 'oauth_url', verificationUrl: signInUrl, userCode: '' } },
    }, Date.now());

    const anchor = card.match(/<a[^>]*data-open-signin[^>]*>/);
    assert.ok(anchor, 'the card must render the sign-in link as its primary action');
    assert.match(anchor[0], /target="_blank"/, 'the link opens a NEW browsing context');
    assert.ok(card.includes(signInUrl), 'the disclosed URL reaches the anchor');

    // Now perform that click from inside the framed wizard.
    const opened = framedWindowOpen(tokens, signInUrl);
    assert.ok(opened, 'the sandboxed wizard could not open the sign-in link — '
        + 'the owner would click "Open sign-in link" and see nothing happen');
    assert.equal(opened.url, signInUrl);
    // And the vendor page must not inherit the wizard's sandbox: an OAuth page
    // with neither same-origin nor scripts cannot complete a sign-in.
    assert.equal(opened.sandboxed, false,
        'the sign-in window inherited the wizard sandbox; no OAuth flow survives that');

    // The frame still keeps the capabilities the wizard itself needs, and the
    // overlay still mounts one.
    for (const needed of ['allow-same-origin', 'allow-scripts', 'allow-forms']) {
        assert.ok(tokens.includes(needed), needed);
    }
    const { overlay } = mountOverlayFrame();
    overlay.innerHTML = source.match(/overlay\.innerHTML = `([\s\S]*?)`;/)[1];
    const frame = overlay.querySelector('.onboarding-frame');
    assert.ok(frame, 'the overlay mounts the wizard frame');
    assert.equal(frame.getAttribute('sandbox'), tokens.join(' '));
});

test('a sandbox without allow-popups reproduces the silent block', () => {
    // The negative control: the same click, under the sandbox as it was, opens
    // nothing at all — and throws no error, which is why nobody saw it.
    const before = ['allow-same-origin', 'allow-scripts', 'allow-forms'];
    assert.equal(framedWindowOpen(before, 'https://claude.ai/oauth/authorize'), null);
});
