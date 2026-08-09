import { apiFetch } from './api_client.js';
function removeOverlay() {
    document.getElementById('onboarding-overlay')?.remove();
}

function mountOverlay() {
    removeOverlay();
    const overlay = document.createElement('div');
    overlay.id = 'onboarding-overlay';
    overlay.className = 'onboarding-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', 'Ouroboros setup');
    // `allow-popups` + `allow-popups-to-escape-sandbox` are LOAD-BEARING, not
    // hygiene: the Agents step's primary action is the agent's own sign-in link,
    // opened with target="_blank". Without them the browser blocks that click
    // SILENTLY — the owner presses "Open sign-in link" and nothing happens, and
    // copying the URL by hand is the only way through. The escape variant is
    // required too: a popup that inherits this sandbox lands on the vendor's
    // OAuth page without same-origin or scripts, which no sign-in survives.
    // Both apply only to windows the framed page opens, never to the frame's own
    // authority over this document.
    overlay.innerHTML = `
        <div class="onboarding-overlay-backdrop"></div>
        <iframe class="onboarding-frame" title="Ouroboros Setup" sandbox="allow-same-origin allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox"></iframe>
    `;
    const frame = overlay.querySelector('.onboarding-frame');
    // ONE onboarding host: frame the real /onboarding page rather than an
    // inlined srcdoc document. A srcdoc string cannot import web/modules/*,
    // and the wizard's steps need those ordinary ES modules.
    if (frame) frame.src = '/onboarding';
    document.body.appendChild(overlay);
}

function escapeHtml(value) {
    // Backtick escaped too (defense-in-depth parity with utils.escapeHtmlAttr).
    return String(value ?? '').replace(/[&<>"'`]/g, (ch) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
        '`': '&#96;',
    }[ch]));
}

function showRestartRequiredOverlay(runtimeMode) {
    const mode = escapeHtml(runtimeMode || 'advanced');
    const overlay = document.getElementById('onboarding-overlay') || document.createElement('div');
    overlay.id = 'onboarding-overlay';
    overlay.className = 'onboarding-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', 'Ouroboros restart required');
    overlay.innerHTML = `
        <div class="onboarding-overlay-backdrop"></div>
        <section class="onboarding-restart-card">
            <h2>Restart Required</h2>
            <p>Runtime mode was saved as <code>${mode}</code> for the next boot. Restart Ouroboros to apply it before continuing in that mode.</p>
            <button type="button" class="btn btn-primary" data-onboarding-continue>Continue in current mode</button>
        </section>
    `;
    if (!overlay.parentElement) document.body.appendChild(overlay);
    overlay.querySelector('[data-onboarding-continue]')?.addEventListener('click', () => {
        removeOverlay();
        window.location.reload();
    });
}

export async function initOnboardingOverlay() {
    function handleMessage(event) {
        // Same-origin only: any web page can postMessage into this window;
        // without the origin check a foreign page could dismiss onboarding or
        // spoof restart prompts.
        if (event.origin !== window.location.origin) return;
        if (event?.data?.type !== 'ouroboros:onboarding-complete') return;
        if (event.data.restart_required) {
            showRestartRequiredOverlay(event.data.runtime_mode);
            return;
        }
        removeOverlay();
        window.location.reload();
    }

    window.addEventListener('message', handleMessage);

    try {
        // Readiness probe only: 204 means the install is structurally configured
        // and no blocking overlay is due. The wizard itself is served by the
        // /onboarding page the frame loads.
        const response = await apiFetch('/api/onboarding', { cache: 'no-store' });
        if (response.status === 204) return;
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        mountOverlay();
    } catch (error) {
        console.error('Failed to load onboarding overlay:', error);
    }
}
