// Live-status projection for one saved Agent-session actor. Dispatch remains
// authoritative; this module only decides which positive/negative facts the
// Settings row may honestly claim from the shared Claudexor snapshot.

import { accountRows, nextUpAccount } from './claudexor_status_store.js';
import { harnessModelsKnown, splitSessionTarget } from './route_editor_primitives.js';

function harnessMap(snapshot) {
    return Object.fromEntries((snapshot?.harnesses || [])
        .filter((harness) => harness?.id)
        .map((harness) => [String(harness.id), harness]));
}

function modelScopeMatches(model, aliases) {
    const routeModel = String(model || '').trim().toLowerCase();
    const scopes = (Array.isArray(aliases) ? aliases : [])
        .map((value) => String(value || '').trim().toLowerCase()).filter(Boolean);
    if (!scopes.length || !routeModel) return true;
    return scopes.some((scope) => scope === routeModel
        || routeModel.includes(scope) || scope.includes(routeModel));
}

function cooldownActive(value, nowMs = Date.now()) {
    const text = String(value || '').trim();
    if (!text) return false;
    const instant = Date.parse(text);
    return !Number.isFinite(instant) || instant > nowMs;
}

/** UI projection of the same positive quota facts dispatch checks again. */
function routeQuotaFact(snapshot, harness, model, profileId = '', nowMs = Date.now()) {
    const pin = String(profileId || '');
    let observed = false;
    let usable = false;
    let spent = false;
    for (const row of snapshot?.quota || []) {
        const subject = row?.subject || {};
        if (String(subject.harness || '') !== String(harness || '')) continue;
        if (pin && String(subject.subject_id || '') !== pin) continue;
        if (String(row?.freshness || '') !== 'fresh') continue;
        observed = true;
        const spentHere = (row?.constraints || []).some((constraint) => {
            const used = Number(constraint?.used_ratio);
            return modelScopeMatches(model, constraint?.applies_to_models)
                && (cooldownActive(constraint?.cooldown_until, nowMs)
                    || (Number.isFinite(used) && used >= 1));
        });
        if (spentHere) spent = true;
        else usable = true;
    }
    return { known: observed, exhausted: spent && !usable };
}

function modelIsPresent(harness, model) {
    if (!model) return true;
    return (harness?.models || []).some((entry) =>
        String(entry?.id || entry?.value || entry || '') === String(model));
}

export function sessionRouteAvailability(row, state, nowMs = Date.now()) {
    const { harness, model } = splitSessionTarget(row?.route?.target_id);
    if (!state?.catalogKnown || !state?.accountsKnown) {
        return 'Agent session · live availability not checked';
    }
    const harnessEntry = harnessMap(state.snapshot)[harness];
    if (!harnessEntry) return `${harness} · currently unavailable`;
    if (!harnessModelsKnown(harnessEntry, state.catalogKnown)) {
        return `${harness} · model availability not checked`;
    }
    if (!modelIsPresent(harnessEntry, model)) {
        return `${harness} · selected model ${model} currently unavailable`;
    }

    const rows = accountRows(state.snapshot).filter((account) => account.harness === harness);
    const pin = String(row?.route?.credential_profile_id || '');
    if (pin) {
        const account = rows.find((candidate) => String(candidate.profile_id || '') === pin);
        if (!account || account.enabled === false
            || String(account?.status?.verification || '') !== 'passed') {
            return `${harness} · pinned account ${pin} currently unavailable`;
        }
        if (!state.quotaKnown) return `${harness} · pinned account ready; quota not checked`;
        const quota = routeQuotaFact(state.snapshot, harness, model, pin, nowMs);
        if (quota.exhausted) return `${harness} · pinned account ${pin} limit reached`;
        if (!quota.known) return `${harness} · pinned account ready; quota availability not proven`;
        return `${harness} · available now`;
    }

    if (harnessEntry.enabled === false
        || (harnessEntry.status && String(harnessEntry.status) !== 'ok')) {
        return `${harness} · currently unavailable`;
    }
    if (!rows.some((account) => account.enabled !== false
        && String(account?.status?.verification || '') === 'passed')) {
        return `${harness} · no usable account currently`;
    }
    if (!state.quotaKnown) return `${harness} · account ready; quota not checked`;
    const pool = nextUpAccount(state.snapshot, harness);
    if (pool?.kind === 'none' || pool?.kind === 'api_key_route') {
        return `${harness} · no usable subscription account currently`;
    }
    if (pool?.kind === 'profile' || pool?.kind === 'native') {
        return `${harness} · compatible account selected; exact model quota checked at start`;
    }
    const quota = routeQuotaFact(state.snapshot, harness, model, '', nowMs);
    if (quota.exhausted) return `${harness} · all known accounts reached a limit`;
    if (quota.known) return `${harness} · available now`;
    return `${harness} · live availability not checked`;
}
