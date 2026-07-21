// Desktop Remote connection UI (owner-only). Talks ONLY to the launcher's
// pywebview bridge — never to /api/settings — because the connection profiles
// are launcher-owned owner state the self-modifying agent must not reach (D13).
//
// The whole surface is inert unless the native bridge is present (desktop app),
// so a plain browser / the remote server's own SPA simply never renders it,
// except the connection pill's remote_status/remote_disconnect, which the
// launcher deliberately answers from any page so a remote page can show the
// pill and offer "Back to local".

/** @returns {object|null} the launcher bridge, or null in a plain browser. */
export function remoteBridge() {
    const api = (typeof window !== 'undefined' && window.pywebview && window.pywebview.api) || null;
    return api && typeof api.remote_status === 'function' ? api : null;
}

/** Pure: short human label for a status dict (used by the pill + tests). */
export function pillLabel(status) {
    const state = (status && status.state) || 'disconnected';
    const name = (status && (status.profile_name || status.profile_id)) || 'remote';
    switch (state) {
        case 'connected': return `Remote: ${name}`;
        case 'connecting': return `Connecting: ${name}…`;
        case 'reconnecting': return `Reconnecting: ${name}…`;
        case 'gave_up': return `Remote lost: ${name}`;
        case 'error': return `Remote error: ${name}`;
        default: return '';
    }
}

/** Pure: is the pill visible for this status? (hidden when local/disconnected). */
export function pillVisible(status) {
    const state = (status && status.state) || 'disconnected';
    return state !== 'disconnected';
}

function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, (ch) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
    ));
}

// ---- Connection pill (chat header) -----------------------------------------

let _pillTimer = null;

/** Mount the connection pill into `slot`; polls the bridge while connected. */
export function mountConnectionPill(slot) {
    const bridge = remoteBridge();
    if (!slot || !bridge) return;
    const render = async () => {
        let status = {};
        try { status = (await bridge.remote_status()) || {}; } catch (_e) { status = {}; }
        if (!pillVisible(status)) {
            slot.innerHTML = '';
            return;
        }
        const disconnecting = status.state === 'connected' || status.state === 'reconnecting';
        slot.innerHTML = `
            <span class="remote-pill remote-pill-${esc(status.state)}" title="${esc(status.error || pillLabel(status))}">
                <span class="remote-pill-label">${esc(pillLabel(status))}</span>
                <button class="remote-pill-back btn btn-ghost btn-xs" type="button" data-remote-back="1">
                    ${disconnecting ? 'Back to local' : 'Dismiss'}
                </button>
            </span>`;
        const back = slot.querySelector('[data-remote-back]');
        if (back) {
            back.addEventListener('click', async () => {
                try { await bridge.remote_disconnect(); } catch (_e) { /* best-effort */ }
                await render();
            });
        }
    };
    if (_pillTimer) clearInterval(_pillTimer);
    _pillTimer = setInterval(render, 3000);
    render();
}

// ---- Settings → Remote section ---------------------------------------------

/** Render + wire the Settings Remote section into `container` (local page only). */
export async function initRemoteSection(container) {
    const bridge = remoteBridge();
    if (!container) return;
    if (!bridge) {
        // Not the desktop app — the whole feature is unavailable here.
        container.innerHTML = '';
        return;
    }
    let status = {};
    try { status = (await bridge.remote_status()) || {}; } catch (_e) { status = {}; }
    if (status.local_origin === false) {
        // We are viewing a remote server: never manage the local profile list.
        container.innerHTML = '';
        return;
    }
    const sshMissing = status.ssh_available === false;
    let profiles = [];
    try {
        const listed = await bridge.remote_list();
        profiles = (listed && listed.profiles) || [];
    } catch (_e) { profiles = []; }

    const activeId = pillVisible(status) ? status.profile_id : '';
    const rows = profiles.map((p) => `
        <div class="remote-profile-row" data-remote-id="${esc(p.id)}">
            <div class="remote-profile-meta">
                <span class="remote-profile-name">${esc(p.name || p.id)}</span>
                <span class="remote-profile-target">ssh ${esc(p.ssh_target)}</span>
            </div>
            <div class="remote-profile-actions">
                ${activeId === p.id
                    ? '<span class="remote-profile-active">connected</span>'
                    : `<button class="btn btn-primary btn-sm" type="button" data-remote-connect="${esc(p.id)}" ${sshMissing ? 'disabled' : ''}>Connect</button>`}
                <button class="btn btn-default btn-sm" type="button" data-remote-delete="${esc(p.id)}">Delete</button>
            </div>
        </div>`).join('');

    container.innerHTML = `
        <div class="form-section">
            <h3>Remote connection</h3>
            <div class="settings-section-copy">
                Connect this desktop app to a headless Ouroboros running on your own
                server over an SSH tunnel. The remote server is a separate Ouroboros
                with its own identity, memory, and keys — connecting switches this
                window to it; the local agent keeps running. Set up key/agent SSH auth
                first (run <code>ssh &lt;target&gt; true</code> once in a terminal).
            </div>
            ${sshMissing ? '<div class="settings-inline-status">System <code>ssh</code> was not found — install an OpenSSH client to use Remote connection.</div>' : ''}
            <div class="remote-profile-list" id="remote-profile-list">${rows || '<div class="settings-inline-status">No saved connections yet.</div>'}</div>
            <div class="remote-profile-form form-grid two">
                <div class="form-field"><label>Name</label><input id="remote-new-name" placeholder="prod box"></div>
                <div class="form-field"><label>SSH target</label><input id="remote-new-target" placeholder="user@host or ~/.ssh/config alias"></div>
                <div class="form-field"><label>Remote data dir (optional)</label><input id="remote-new-datadir" placeholder="~/Ouroboros/data"></div>
                <div class="form-field"><label>Remote agent port (optional)</label><input id="remote-new-port" type="number" placeholder="auto-discovered"></div>
            </div>
            <div class="settings-toolbar">
                <button class="btn btn-default btn-sm" type="button" id="remote-add">Save connection</button>
            </div>
            <div class="settings-inline-status" id="remote-section-status"></div>
        </div>`;

    const statusEl = container.querySelector('#remote-section-status');
    const say = (msg, ok) => { if (statusEl) { statusEl.textContent = msg; statusEl.dataset.tone = ok ? 'ok' : 'error'; } };

    container.querySelector('#remote-add')?.addEventListener('click', async () => {
        const id = (container.querySelector('#remote-new-name')?.value || '')
            .toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64)
            || `conn-${Date.now().toString(36)}`;
        const profile = {
            id,
            name: container.querySelector('#remote-new-name')?.value || id,
            ssh_target: (container.querySelector('#remote-new-target')?.value || '').trim(),
        };
        const dir = (container.querySelector('#remote-new-datadir')?.value || '').trim();
        const port = (container.querySelector('#remote-new-port')?.value || '').trim();
        if (dir) profile.remote_data_dir = dir;
        if (port) profile.remote_agent_port = Number(port);
        let result;
        try { result = await bridge.remote_save(profile); } catch (e) { result = { ok: false, error: String(e) }; }
        if (result && result.ok) { say('Saved.', true); await initRemoteSection(container); }
        else { say(result?.error || 'Could not save connection.', false); }
    });

    container.querySelectorAll('[data-remote-connect]').forEach((btn) => {
        btn.addEventListener('click', async () => {
            say('Connecting…', true);
            let result;
            try { result = await bridge.remote_connect(btn.dataset.remoteConnect); }
            catch (e) { result = { ok: false, error: String(e) }; }
            if (!result || result.ok !== true) {
                const hint = result?.hint ? ` (${result.hint})` : '';
                say(`${result?.error || 'Connection failed.'}${hint}`, false);
            }
            // On success the launcher navigates the window to the remote page.
        });
    });

    container.querySelectorAll('[data-remote-delete]').forEach((btn) => {
        btn.addEventListener('click', async () => {
            try { await bridge.remote_delete(btn.dataset.remoteDelete); } catch (_e) { /* ignore */ }
            await initRemoteSection(container);
        });
    });
}
