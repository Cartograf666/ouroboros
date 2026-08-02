// Main-screen Update affordance (P2): a compact pill that appears when a managed update
// is available (status is populated by the boot-time check-on-restart), opening a staged
// choice dialog (Auto-update / Ouroboros-assisted / Manual) backed by a fresh merge
// preflight. The full merge/smoke/rollback happens server-side; this is the thin,
// transparent control surface. Non-invasive: the detailed Dashboard -> Updates panel
// stays the place for recovery/details.

import { apiClient } from './api_client.js';

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

// Fail-soft wrapper around the api_client update helpers (the pill must never throw the app).
async function safe(fn) {
    try {
        return await fn();
    } catch {
        return null;
    }
}

export function initUpdateStatus({ showPage, openDashboardTab } = {}) {
    function ensurePill() {
        let pill = document.getElementById('update-pill');
        if (!pill) {
            pill = document.createElement('button');
            pill.id = 'update-pill';
            pill.type = 'button';
            pill.className = 'update-pill';
            pill.hidden = true;
            pill.addEventListener('click', openUpdateDialog);
            const anchor = document.getElementById('nav-version');
            if (anchor && anchor.parentNode) {
                anchor.parentNode.insertBefore(pill, anchor.nextSibling);
            } else {
                document.body.appendChild(pill);
            }
        }
        return pill;
    }

    function renderPill(status) {
        const pill = ensurePill();
        if (!status || !status.available) {
            pill.hidden = true;
            return;
        }
        const cur = status.current_version || (status.current_sha ? String(status.current_sha).slice(0, 8) : '');
        const next = status.latest_version || (status.latest_sha ? String(status.latest_sha).slice(0, 8) : '');
        pill.textContent = (cur && next) ? `Update ${cur} → ${next}` : 'Update available';
        pill.classList.toggle('has-local', Boolean(status.dirty || status.ahead));
        pill.hidden = false;
    }

    async function refresh() {
        renderPill(await safe(() => apiClient.updateStatus()));
    }

    // The dialog for a preflight whose answer cannot be trusted. Deliberately offers NO apply
    // button: the two things that are honestly available here are running the check again and
    // looking at the detailed panel, and an update the owner cannot start is a smaller harm than
    // one started against a checkout state nobody established.
    function renderUnverifiedPreflight(overlay, pre, plan) {
        const detail = !pre
            ? 'The update preflight could not be reached.'
            : (plan.kind === 'current'
                ? 'This checkout already matches the official release.'
                : (plan.error
                    ? `The update preflight did not complete: ${escapeHtml(plan.error)}`
                    : 'The update preflight did not complete.'));
        overlay.querySelector('.update-dialog').innerHTML = `
            <h3 class="update-dialog-title">Update status unconfirmed</h3>
            <div class="update-dialog-note">${detail}</div>
            <div class="update-dialog-note">Nothing about this checkout — local changes, conflicts, or whether the merge is clean — was established, so no update is offered from here.</div>
            <div class="update-dialog-actions">
                <button data-retry class="btn btn-primary">Retry</button>
                <button data-details class="btn btn-default">Open details</button>
                <button data-close class="btn btn-default">Cancel</button>
            </div>`;
        overlay.addEventListener('click', (event) => {
            const t = event.target;
            if (t === overlay || t.hasAttribute?.('data-close')) {
                overlay.remove();
                return;
            }
            if (t.hasAttribute?.('data-retry')) {
                overlay.remove();
                openUpdateDialog();
                return;
            }
            if (t.hasAttribute?.('data-details')) {
                overlay.remove();
                showPage?.('dashboard');
                openDashboardTab?.('updates');
            }
        });
    }

    async function openUpdateDialog() {
        const overlay = document.createElement('div');
        overlay.className = 'update-dialog-overlay';
        overlay.innerHTML = '<div class="update-dialog"><div class="update-dialog-status">Checking update…</div></div>';
        document.body.appendChild(overlay);

        const pre = await safe(() => apiClient.updatePreflight());
        const plan = (pre && pre.merge_plan) || {};
        const kind = plan.kind || 'unknown';
        // A preflight that never arrived (`safe` swallows the failure and answers null) or arrived
        // DEGRADED (`kind: 'unknown'` — the planner deliberately withholds a dirty count and a
        // conflict set it could not read) establishes NOTHING about this checkout. Every field this
        // dialog reads then falls back to the reassuring value — no conflicts, zero local changes,
        // "clean merge" — so the rendering below would describe a clean tree nobody verified and
        // offer to update it. The count is required as an INTEGER for the same reason the planner
        // only emits it from a `git status` that succeeded: `|| 0` turns a missing proof into one.
        const dirtyCount = Number.isInteger(plan.local_dirty_count) ? plan.local_dirty_count : null;
        const verified = Boolean(pre) && plan.available === true && kind !== 'unknown' && dirtyCount !== null;
        if (!verified) {
            renderUnverifiedPreflight(overlay, pre, plan);
            return;
        }
        const hot = new Set(plan.hot_code_paths || []);
        const conflicts = [
            ...((plan.protected_conflict_paths || []).map((p) => `Protected: ${p}`)),
            ...((plan.code_conflict_paths || []).map((p) => (hot.has(p) ? `Code (hot): ${p}` : `Code: ${p}`))),
            ...((plan.doc_conflict_paths || []).map((p) => `Docs: ${p}`)),
        ];
        const base = plan.base_sha ? String(plan.base_sha).slice(0, 8) : '';
        const target = plan.target_sha ? String(plan.target_sha).slice(0, 8) : '';
        // The backend evaluates its protected-path gate in the preflight for the strategy this
        // dialog would offer, so we never present an action it will then refuse (protected changes
        // to safety-critical files always need the owner's own eyes on the diff).
        const route = (pre && pre.protected_route) || {};
        const protectedPaths = route.protected_paths || [];
        // An unverifiable delta routes to manual with an EMPTY list (we could not read what the
        // release touches), so say that instead of rendering an empty "protected files" list.
        const protectedNote = !route.will_route_manual
            ? ''
            : (route.reason === 'protected_delta_unverifiable'
                ? '<div class="update-dialog-note">The official delta could not be verified, so this update needs manual review before it lands.</div>'
                : `<div class="update-dialog-note">This release changes protected files, so it needs your own review before it lands:</div><ul class="update-dialog-conflicts">${protectedPaths.map((p) => `<li>${escapeHtml(p)}</li>`).join('')}</ul>`);
        // Single source of truth for the offered action: the backend already chose it with its
        // strict fail-closed predicate (only a plan that PROVES an empty working tree unlocks the
        // unreviewed auto merge), so consume that answer rather than re-deriving it here — a
        // second, looser local rule would advertise Auto-update for plans the gate calls assisted.
        // The local computation survives only as the legacy fallback for an older backend that
        // sends no `protected_route`, and it consumes the count the `verified` guard above already
        // established as an integer — never a `|| 0` of its own, which is exactly the coercion that
        // turns a missing proof of a clean tree into a claim of one.
        const offered = (pre && pre.protected_route)
            ? route.offered_strategy
            : ((kind === 'clean' && dirtyCount === 0) ? 'auto_merge' : 'assisted');
        // A conflict-free plan that is still offered as assisted needs a word about WHY the primary
        // button is not Auto-update. The wording stays cause-neutral because this branch is reached
        // for several different reasons (uncommitted local work, a dirty count we may not trust, a
        // failed preflight) and the dialog cannot tell them apart — it must not blame the working
        // tree for a state the count beside it already reports.
        const assistedWithoutConflicts = offered !== 'auto_merge' && !route.will_route_manual && !conflicts.length;
        const primary = route.will_route_manual
            ? '<button data-strategy="manual" class="btn btn-primary">Review manually</button>'
            : (offered === 'auto_merge'
                ? '<button data-strategy="auto_merge" class="btn btn-primary">Auto-update</button>'
                : '<button data-strategy="assisted" class="btn btn-primary">Ouroboros-assisted update</button>');

        overlay.querySelector('.update-dialog').innerHTML = `
            <h3 class="update-dialog-title">Update ${escapeHtml(base)} → ${escapeHtml(target)}</h3>
            <div class="update-dialog-meta">${escapeHtml(dirtyCount)} local change(s)${conflicts.length ? ` · ${conflicts.length} conflict(s)` : (kind === 'clean' ? ' · clean merge' : '')}</div>
            ${conflicts.length ? `<ul class="update-dialog-conflicts">${conflicts.map((r) => `<li>${escapeHtml(r)}</li>`).join('')}</ul>` : ''}
            ${protectedNote}
            ${assistedWithoutConflicts ? '<div class="update-dialog-note">This update takes the reviewed Ouroboros-assisted path rather than an unreviewed auto-update.</div>' : ''}
            <div class="update-dialog-note">Your local work is preserved in a rescue snapshot first; a smoke test runs before the restart is accepted, and a failed update auto-rolls-back to the current version.</div>
            <div class="update-dialog-actions">
                ${primary}
                <button data-strategy="manual" class="btn btn-default">Open details</button>
                <button data-close class="btn btn-default">Cancel</button>
            </div>
            <div class="update-dialog-status" hidden></div>`;

        const statusEl = overlay.querySelector('.update-dialog-status');
        overlay.addEventListener('click', async (event) => {
            const t = event.target;
            if (t === overlay || t.hasAttribute?.('data-close')) {
                overlay.remove();
                return;
            }
            const strat = t.dataset?.strategy;
            if (!strat) return;
            if (strat === 'manual') {
                overlay.remove();
                showPage?.('dashboard');
                openDashboardTab?.('updates');
                return;
            }
            statusEl.hidden = false;
            statusEl.textContent = 'Applying update…';
            // A 409 is RAISED by jsonPost, but it can still carry a TYPED reason the owner can act
            // on. Collapsing every rejection into `{error}` reported the staged-path refusals as
            // plain failures; normalizing the reason here lets the branches below tell them apart
            // exactly as the detailed Updates panel's applyUpdate already does.
            const data = await apiClient.updateApply({ strategy: strat }).catch((e) => (
                (e && e.status === 409 && e.body && e.body.reason)
                    ? { reason: String(e.body.reason), error: String(e.message || e) }
                    : { error: String((e && e.message) || e) }
            ));
            if (data && data.status === 'ok') {
                // `status:'ok'` means the update LANDED — the commit is in the checkout and the
                // smoke test passed. `restarting` answers a SECOND, independent question: whether
                // the restart request was accepted. Gating success on both sent an applied update
                // to the generic failure text at the bottom of this chain, which is the one reading
                // that invites the owner to retry a commit already sitting in their tree.
                statusEl.textContent = data.restarting
                    ? 'Update applied; smoke-test passed; restarting…'
                    : `Update applied; smoke-test passed. ${data.warning || 'Restart the server manually to finish.'}`;
            } else if (data && data.status === 'assisted_started') {
                statusEl.textContent = 'Ouroboros is resolving the merge under review — watch progress in chat.';
            } else if (data && data.reason === 'update_lock_held') {
                // The boot check-on-restart thread holds the same exclusive update lock across its
                // fetch, so losing that race is routine and the very next click simply works.
                statusEl.textContent = 'An update check is already in progress. Try again in a moment.';
            } else if (data && data.reason === 'release_moved'
                       && !(Array.isArray(data.protected_paths) && data.protected_paths.length)) {
                // Both remaining drift windows land here: the fenced re-plan answers a 200 with this
                // reason, prepare's own fetch answers the typed 409 normalized above. Neither is a
                // failure and neither is manual handling — this dialog only builds its disclosure on
                // open, so the owner needs a fresh one.
                //
                // A drift whose fresh disclosure DOES name protected paths is excluded above and
                // falls through to the manual branch, which is the only place those paths get named
                // and the owner gets handed to the detailed panel. And this branch owns an exit of
                // its own: the disclosure is built once, on open, so the dialog cannot re-render
                // itself against the new release — leaving the overlay up stranded the owner with no
                // next step. Close it and refresh the pill, so the reopen it asks for is one click.
                statusEl.textContent = 'The official release changed while this update was being applied — reopen this dialog to see the current release.';
                setTimeout(() => { overlay.remove(); refresh(); }, 2500);
            } else if (data && data.status === 'manual') {
                // The backend routed this update to MANUAL — surface that handoff, don't show a
                // generic failure. Branch on the typed reason exactly as the preflight note above
                // does: an unverifiable delta arrives with an EMPTY protected_paths list, so the
                // protected-files wording would name a cause we never established.
                if (data.reason === 'protected_delta_unverifiable') {
                    statusEl.textContent = 'The official delta could not be verified, so this update needs manual review — opening the detailed Updates panel…';
                } else {
                    const prot = Array.isArray(data.protected_paths) && data.protected_paths.length
                        ? ` (protected: ${data.protected_paths.slice(0, 6).map(escapeHtml).join(', ')})`
                        : '';
                    statusEl.textContent = `This update needs manual handling${prot} — opening the detailed Updates panel…`;
                }
                setTimeout(() => { overlay.remove(); showPage?.('dashboard'); openDashboardTab?.('updates'); }, 1500);
            } else {
                statusEl.textContent = (data && data.error) ? `Did not complete: ${data.error}` : 'Update did not complete.';
            }
        });
    }

    refresh();
    window.addEventListener('ouro:page-shown', (event) => {
        if (event?.detail?.page === 'chat') refresh();
    });

    return { refresh };
}
