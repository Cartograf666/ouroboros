import { escapeHtmlAttr as escapeHtml } from './utils.js';
import { showToast } from './toast.js';
import { apiClient, apiFetch } from './api_client.js';

export function initUpdates({ mount, state }) {
    const page = document.createElement('div');
    page.id = 'page-updates';
    page.className = 'settings-embedded-content settings-updates-panel';
    // Keep the update action beside the status it refreshes.
    page.innerHTML = `
        <div class="updates-scroll">
            <section class="updates-card" id="updates-status-card">
                <div class="updates-card-head">
                    <div class="updates-card-head-main">
                        <div class="section-title">Official Updates</div>
                        <div class="updates-summary" id="updates-summary">Loading update status...</div>
                    </div>
                    <div class="updates-head-actions">
                        <span class="status-badge offline" id="updates-badge">Idle</span>
                        <button class="btn btn-default btn-sm" id="btn-update-check">Check for updates</button>
                    </div>
                </div>
                <div class="updates-meta" id="updates-meta"></div>
                <div class="updates-actions">
                    <button class="btn btn-primary" id="btn-update-apply" disabled>Update Now</button>
                </div>
            </section>
            <section class="updates-card">
                <div class="evo-versions-header">
                    <div id="updates-current" class="evo-versions-branch"></div>
                    <button class="btn btn-primary" id="updates-promote">Promote to Stable</button>
                </div>
                <div class="evo-versions-cols">
                    <div class="evo-versions-col">
                        <h3 class="section-title">Local Recovery: Recent Commits</h3>
                        <div id="updates-commits" class="log-scroll evo-versions-list"></div>
                    </div>
                    <div class="evo-versions-col">
                        <h3 class="section-title">Official Releases</h3>
                        <div id="updates-official-tags" class="log-scroll evo-versions-list"></div>
                    </div>
                    <div class="evo-versions-col">
                        <h3 class="section-title">Local Recovery: Local Tags</h3>
                        <div id="updates-tags" class="log-scroll evo-versions-list"></div>
                    </div>
                </div>
            </section>
        </div>
    `;
    mount.appendChild(page);

    const checkBtn = page.querySelector('#btn-update-check');
    const applyBtn = page.querySelector('#btn-update-apply');
    const badge = page.querySelector('#updates-badge');
    const summary = page.querySelector('#updates-summary');
    const meta = page.querySelector('#updates-meta');
    const current = page.querySelector('#updates-current');
    const commitsDiv = page.querySelector('#updates-commits');
    const officialTagsDiv = page.querySelector('#updates-official-tags');
    const tagsDiv = page.querySelector('#updates-tags');
    let latestStatus = null;

    function setBadge(kind, text) {
        badge.className = `status-badge ${kind}`;
        badge.textContent = text;
    }

    function divergenceText(data) {
        const parts = [];
        if (data.behind) parts.push(`${data.behind} incoming`);
        if (data.ahead) parts.push(`${data.ahead} local`);
        if (data.dirty_count) parts.push(`${data.dirty_count} dirty`);
        return parts.join(' / ') || 'clean';
    }

    function renderStatus(data) {
        latestStatus = data;
        const unmanaged = data.managed === false
            || (Array.isArray(data.warnings) && data.warnings.includes('managed_updates_unavailable'));
        if (unmanaged) {
            summary.textContent = 'Managed updates are unavailable for this checkout.';
            meta.innerHTML = `
                <span class="evo-runtime-chip"><strong>Mode:</strong> source checkout</span>
                <span class="evo-runtime-chip"><strong>Action:</strong> use git or install a launcher-managed build</span>
            `;
            applyBtn.disabled = true;
            applyBtn.dataset.safe = '0';
            applyBtn.textContent = 'Unavailable';
            setBadge('offline', 'Unavailable');
            return;
        }
        if (Array.isArray(data.warnings) && data.warnings.includes('official_status_requires_check')) {
            summary.textContent = 'Click Check for updates to refresh official update status.';
            meta.innerHTML = '<span class="evo-runtime-chip"><strong>Official repo:</strong> razzant/ouroboros</span>';
            applyBtn.disabled = true;
            applyBtn.dataset.safe = '0';
            applyBtn.textContent = 'Check Required';
            setBadge('offline', 'Not checked');
            return;
        }
        const currentVersion = data.current_version || 'unknown';
        const latestVersion = data.latest_version || 'unknown';
        const currentSha = data.current_short_sha || '?';
        const latestSha = data.latest_short_sha || '?';
        const latestMsg = data.latest_message || 'No remote message.';
        const canUpdate = Boolean(data.available);
        const safe = Boolean(data.safe_to_apply);
        summary.textContent = canUpdate
            ? `Update available: ${currentVersion} (${currentSha}) -> ${latestVersion} (${latestSha})`
            : `Ouroboros is up to date at ${currentVersion} (${currentSha}).`;
        meta.innerHTML = [
            `<span class="evo-runtime-chip"><strong>Official repo:</strong> razzant/ouroboros</span>`,
            `<span class="evo-runtime-chip"><strong>Remote ref:</strong> ${escapeHtml(data.remote || 'managed')}/${escapeHtml(data.remote_branch || '')}</span>`,
            `<span class="evo-runtime-chip"><strong>Divergence:</strong> ${escapeHtml(divergenceText(data))}</span>`,
            `<span class="evo-runtime-chip"><strong>Latest:</strong> ${escapeHtml(latestMsg)}</span>`,
        ].join('');
        applyBtn.disabled = !canUpdate;
        applyBtn.dataset.safe = safe ? '1' : '0';
        applyBtn.textContent = !canUpdate ? 'No Update Available' : (safe ? 'Update Now' : 'Update with Options');
        setBadge(canUpdate ? (safe ? 'online' : 'starting') : 'offline', canUpdate ? 'Available' : 'Current');
    }

    async function loadStatus({ fetchRemote = false } = {}) {
        checkBtn.disabled = true;
        setBadge('starting', fetchRemote ? 'Checking...' : 'Loading...');
        try {
            const resp = await apiFetch(fetchRemote ? '/api/update/check' : '/api/update/status', {
                method: fetchRemote ? 'POST' : 'GET',
                cache: 'no-store',
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            renderStatus(data);
            renderOfficialTags(data.official_tags || []);
        } catch (err) {
            summary.textContent = `Failed to load update status: ${err.message || err}`;
            meta.innerHTML = '';
            applyBtn.disabled = true;
            setBadge('error', 'Error');
        } finally {
            checkBtn.disabled = false;
        }
    }

    function renderVersionRow(item, labelText, targetId) {
        const row = document.createElement('div');
        row.className = 'log-entry evo-versions-row';
        const date = (item.date || '').slice(0, 16).replace('T', ' ');
        const msg = escapeHtml((item.message || '').slice(0, 72));
        row.innerHTML = `
            <span class="log-type tools evo-versions-row-label">${escapeHtml(labelText)}</span>
            <span class="log-ts">${escapeHtml(date)}</span>
            <span class="log-msg evo-versions-row-msg">${msg}</span>
            <button class="btn btn-danger btn-xs" data-target="${escapeHtml(targetId)}">Restore</button>
        `;
        row.querySelector('button').addEventListener('click', () => rollback(targetId));
        return row;
    }

    function renderOfficialTags(tags) {
        officialTagsDiv.innerHTML = '';
        (tags || []).forEach((tag) => {
            const row = document.createElement('div');
            row.className = 'log-entry evo-versions-row';
            row.innerHTML = `
                <span class="log-type tools evo-versions-row-label">${escapeHtml(tag.tag || '')}</span>
                <span class="log-msg evo-versions-row-msg">${escapeHtml((tag.sha || '').slice(0, 12))}</span>
            `;
            officialTagsDiv.appendChild(row);
        });
        if (!tags?.length) officialTagsDiv.innerHTML = '<div class="evo-empty">Check for updates to load official releases.</div>';
    }

    async function loadVersions() {
        try {
            const resp = await apiFetch('/api/git/log', { cache: 'no-store' });
            if (!resp.ok) throw new Error('Git log API error ' + resp.status);
            const data = await resp.json();
            current.textContent = `Branch: ${data.branch || '?'} @ ${data.sha || '?'}`;
            commitsDiv.innerHTML = '';
            (data.commits || []).forEach((commit) => {
                commitsDiv.appendChild(renderVersionRow(commit, commit.short_sha || commit.sha?.slice(0, 8), commit.sha));
            });
            if (!data.commits?.length) commitsDiv.innerHTML = '<div class="evo-empty">No commits found</div>';
            tagsDiv.innerHTML = '';
            (data.tags || []).forEach((tag) => {
                tagsDiv.appendChild(renderVersionRow(tag, tag.tag, tag.tag));
            });
            if (!data.tags?.length) tagsDiv.innerHTML = '<div class="evo-empty">No tags found</div>';
        } catch (err) {
            const msg = `<div class="evo-empty evo-empty-error">Failed to load: ${escapeHtml(err.message || err)}</div>`;
            commitsDiv.innerHTML = msg;
            tagsDiv.innerHTML = msg;
            current.textContent = 'Branch: unknown';
        }
    }

    async function rollback(target) {
        if (!confirm(`Roll back to ${target}?\n\nA rescue snapshot of the current state will be saved. The server will restart.`)) return;
        try {
            const resp = await apiFetch('/api/git/rollback', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ target }),
            });
            const data = await resp.json();
            if (data.status === 'ok') {
                showToast(`Rollback successful: ${data.message}. Server is restarting...`, 'success');
            } else {
                showToast(`Rollback failed: ${data.error || 'unknown error'}`, 'error');
            }
        } catch (err) {
            showToast('Rollback failed: ' + (err.message || err), 'error');
        }
    }

    // The apply POST is a declared contract on BOTH ends — the acknowledgement fields below are
    // part of `UpdateApplyRequest` — so it crosses the typed gateway boundary rather than a
    // hand-rolled fetch beside it. Naming it locally keeps the disclosure POST and the bound
    // acknowledgement re-POST on the same seam, which is what the source-pin fences.
    async function postApply(body) {
        try {
            return await apiClient.updateApply(body);
        } catch (err) {
            // The release can move at THREE points: between the disclosure and the bound re-POST,
            // between that and the fenced re-plan, and between the fence and `prepare_managed_
            // update`'s own fetch. The first two answer 200 responses `applyUpdate` reads
            // directly; only this last one is a typed 409, which `jsonPost` RAISES. All three
            // refuse the drifted release and all three want the same next action from the owner,
            // so this one is handed BACK as a normal response — one branch below reports all
            // three, instead of this window falling through to the generic 'Update failed' catch.
            if (err?.status === 409 && err?.body?.reason === 'release_moved') return err.body;
            throw err;
        }
    }

    // The exclusive update lock is also held by the boot check-on-restart thread while it fetches,
    // so "lock held" is routinely a transient state the owner did nothing to cause — and the next
    // click will simply work. Report it as retryable rather than as a failed update.
    const UPDATE_LOCK_HELD_MESSAGE = 'An update check is already in progress. '
        + 'Try again in a moment.';

    function isUpdateLockHeldError(err) {
        return err?.status === 409 && err?.body?.reason === 'update_lock_held';
    }

    // The backend gates a protected official change even on the replace family — a safety-critical
    // path OR a protected path whose tier it does not recognize — and answers
    // `{status: 'manual', requires_acknowledgement: true}` with the exact SHAs + paths it wants
    // acknowledged. Show that list, then re-POST the acknowledgement BOUND to it — an echo that
    // does not match exactly is refused, so nothing is ever acknowledged blind.
    function confirmProtectedAck(data) {
        const paths = (data.protected_paths || []).map((p) => `  • ${p}`).join('\n');
        const base = String(data.base_sha || '').slice(0, 8);
        const target = String(data.target_sha || '').slice(0, 8);
        return confirm(
            `This official update (${base} -> ${target}) changes protected files that must be `
            + `reviewed before a hard reset:\n\n${paths}\n\n`
            + 'These are safety-critical paths or protected paths of an unrecognized tier. '
            + 'Applying will hard-reset the checkout to the official version. Continue?',
        );
    }

    async function applyUpdate() {
        if (!latestStatus?.available) return;
        const safe = latestStatus.safe_to_apply;
        let strategy = 'replace';
        if (!safe) {
            const localBits = divergenceText(latestStatus);
            const proceed = confirm(
                `This update will replace the active managed checkout with the selected official version.\n\nLocal state: ${localBits}\n\nLocal commits will be preserved on a local-keep-* branch before the active branch moves. Dirty files will be saved in a rescue snapshot. Continue?`,
            );
            if (!proceed) return;
            strategy = latestStatus.ahead ? 'stash' : 'replace';
        }
        const restoreBtn = () => {
            applyBtn.disabled = false;
            applyBtn.textContent = safe ? 'Update Now' : 'Update with Options';
        };
        applyBtn.disabled = true;
        applyBtn.textContent = 'Preparing...';
        let releaseMoved = false;
        try {
            let data = await postApply({ strategy });
            if (data.status === 'manual' && data.requires_acknowledgement) {
                if (!confirmProtectedAck(data)) {
                    showToast('Update cancelled — protected changes were not acknowledged.', 'info');
                    restoreBtn();
                    return;
                }
                data = await postApply({
                    strategy,
                    acknowledge_protected: true,
                    acknowledged_base_sha: data.base_sha,
                    acknowledged_target_sha: data.target_sha,
                    acknowledged_protected_paths: data.protected_paths || [],
                });
                if (data.status === 'manual' && data.requires_acknowledgement) {
                    // The release moved between the disclosure and this echo, so the backend
                    // refused the now-stale acknowledgement and answered with a FRESH disclosure.
                    // Reported below, NOT re-prompted in place: one dialog per disclosure is what
                    // keeps the acknowledgement honest, so the owner comes back through a click.
                    releaseMoved = true;
                }
            }
            // Drift after the owner ACKNOWLEDGED protected changes: naming the confirmation and the
            // protected review is accurate only on this branch, because only this branch ran a
            // disclosure dialog.
            if (releaseMoved) {
                showToast(
                    'The official release moved since you confirmed. Click Update again to '
                    + 'review the new protected changes.',
                    'info',
                );
                restoreBtn();
                return;
            }
            // The other two drift windows — the fenced re-plan's typed reason and prepare's 409 that
            // `postApply` handed back as a response — can both fire for an UNPROTECTED update where
            // no acknowledgement dialog ever ran, so the wording stays neutral: claiming the owner
            // confirmed something, or that protected paths changed, would be false there.
            if (data.reason === 'release_moved') {
                showToast(
                    'The official release changed while this update was being applied. Click '
                    + 'Update again to recheck the changed release.',
                    'info',
                );
                restoreBtn();
                return;
            }
            if (data.status === 'manual') {
                // Not a success: the backend handed this update back for owner review.
                const why = data.reason === 'protected_delta_unverifiable'
                    ? 'the official delta could not be verified'
                    : 'it changes protected files';
                showToast(`Update needs manual handling — ${why}.`, 'error');
                restoreBtn();
                return;
            }
            const keep = data.keep_branch ? ` Local commits preserved as ${data.keep_branch}.` : '';
            // `status:'ok'` with `restarting:false` is the terminal frame for an update that LANDED
            // and then could not get its restart requested. "Server is restarting." tells the owner
            // to wait for a restart that is never coming, on a checkout that has already moved — and
            // leaving the button disabled for that wait strands them with no next action. Narrow to
            // this exact frame so every other terminal answer keeps its wording verbatim.
            if (data.status === 'ok' && !data.restarting) {
                const why = data.warning || 'restart the server manually to finish';
                showToast(`Update applied, but ${why}.${keep}`, 'warning');
                restoreBtn();
                return;
            }
            // Assisted staging starts a resolution task first — no restart is pending yet, so the
            // restart wording would promise something this path never does.
            if (data.status === 'assisted_started') {
                showToast(`Update fetched; assisted merge resolution started.${keep}`, 'success');
                return;
            }
            showToast(`Update prepared. Server is restarting.${keep}`, 'success');
        } catch (err) {
            // A held lock also arrives as a 409, which jsonPost RAISES — without the typed reason
            // it would read as a failed update rather than the transient state it is.
            const lockHeld = isUpdateLockHeldError(err);
            showToast(
                lockHeld ? UPDATE_LOCK_HELD_MESSAGE : `Update failed: ${err.message || err}`,
                lockHeld ? 'info' : 'error',
            );
            restoreBtn();
        }
    }

    checkBtn.addEventListener('click', () => {
        loadStatus({ fetchRemote: true });
        loadVersions();
    });
    applyBtn.addEventListener('click', applyUpdate);
    page.querySelector('#updates-promote').addEventListener('click', async () => {
        if (!confirm('Promote current ouroboros branch to ouroboros-stable?')) return;
        try {
            const resp = await apiFetch('/api/git/promote', { method: 'POST' });
            const data = await resp.json();
            if (data.status === 'ok') {
                showToast(data.message, 'success');
            } else {
                showToast('Error: ' + (data.error || 'unknown'), 'error');
            }
        } catch (err) {
            showToast('Failed: ' + (err.message || err), 'error');
        }
    });

    window.addEventListener('ouro:dashboard-subtab-shown', (event) => {
        if (event.detail?.tab !== 'updates' || state.activePage !== 'dashboard') return;
        loadStatus({ fetchRemote: false });
        loadVersions();
    });
}
