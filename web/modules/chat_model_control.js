import { apiFetch } from './api_client.js';
import {
    FACET_QUOTA,
    READ_OK,
    claudexorStatus,
    familyLabel,
} from './claudexor_status_store.js';
import { quotaSummary } from './harness_accounts.js';

const LOCAL_VALUE = '__local__';
const SUCCESS_EVENT_TYPES = new Set(['llm_round']);
const ERROR_EVENT_TYPES = new Set(['llm_api_error']);
const READ_TIMEOUT_MS = 5000;
const QUOTA_READ_TIMEOUT_MS = 1500;

export function normalizeModelIdentity(value) {
    return String(value || '').trim()
        .replace('::', '/')
        .replace(/\s+\(local\)$/i, '')
        .replace(/^([^/]+)\/models\//i, '$1/');
}

function basenameWithoutGguf(value) {
    const leaf = String(value || '').split(/[\\/]/).pop() || 'Local model';
    return leaf.replace(/\.gguf$/i, '');
}

export function parseQuotaError(error) {
    const text = String(error || '');
    const metricMatch = text.match(/Quota exceeded for metric:\s*([^,\s]+)/i);
    const limitMatch = text.match(/\blimit:\s*([0-9]+(?:\.[0-9]+)?)/i);
    const retryMatch = text.match(/Please retry in\s*([0-9]+(?:\.[0-9]+)?)s/i);
    const metricPath = metricMatch ? metricMatch[1] : '';
    return {
        limit: limitMatch ? Number(limitMatch[1]) : null,
        metric: metricPath ? metricPath.split('/').pop() : '',
        retryAfterSec: retryMatch ? Number(retryMatch[1]) : null,
    };
}

function eventEpoch(event) {
    const value = Date.parse(String(event?.ts || ''));
    return Number.isFinite(value) ? value : 0;
}

function modelEvents(modelValue, events) {
    const identity = normalizeModelIdentity(modelValue);
    return (events || [])
        .filter((row) => normalizeModelIdentity(row?.model) === identity)
        .filter((row) => SUCCESS_EVENT_TYPES.has(String(row?.type || ''))
            || ERROR_EVENT_TYPES.has(String(row?.type || '')))
        .sort((a, b) => eventEpoch(a) - eventEpoch(b));
}

function clockLabel(epoch) {
    if (!Number.isFinite(epoch) || epoch <= 0) return '';
    return new Date(epoch).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function deriveModelStatus(modelValue, events, { now = Date.now() } = {}) {
    const rows = modelEvents(modelValue, events);
    const latest = rows.at(-1);
    if (!latest) {
        return {
            state: 'unknown', detail: 'Not checked yet', remaining: null,
            resetAt: '', limit: null, metric: '',
        };
    }
    const at = eventEpoch(latest);
    if (SUCCESS_EVENT_TYPES.has(String(latest.type || ''))) {
        return {
            state: 'available',
            detail: `Last request succeeded${at ? ` at ${clockLabel(at)}` : ''}; exact quota remaining is not exposed`,
            remaining: null, resetAt: '', limit: null, metric: '',
        };
    }

    const errorText = `${latest.error || ''} ${latest.provider_message || ''}`;
    const quota = parseQuotaError(errorText);
    const code = Number(latest.status_code || 0);
    const kind = String(latest.error_kind || '');
    if (code === 429 || kind === 'rate_limit' || kind === 'quota_exhausted'
        || kind === 'subscription_window_exhausted') {
        const resetEpoch = quota.retryAfterSec != null && at
            ? at + quota.retryAfterSec * 1000 : NaN;
        const resetAt = Number.isFinite(resetEpoch) ? new Date(resetEpoch).toISOString() : '';
        if (kind === 'quota_exhausted' || kind === 'subscription_window_exhausted') {
            return {
                state: 'exhausted', detail: 'Provider reported the quota exhausted',
                remaining: null, resetAt, limit: quota.limit, metric: quota.metric,
            };
        }
        if (Number.isFinite(resetEpoch) && resetEpoch > now) {
            return {
                state: 'limited', detail: `Rate limited until ${clockLabel(resetEpoch)}`,
                remaining: null, resetAt, limit: quota.limit, metric: quota.metric,
            };
        }
        return {
            state: 'unknown',
            detail: 'A limit was hit; its retry window may have reset, but no later success is recorded',
            remaining: null, resetAt, limit: quota.limit, metric: quota.metric,
        };
    }
    if (kind === 'auth_error' || code === 401 || code === 403) {
        return {
            state: 'unavailable', detail: 'Authorization failed', remaining: null,
            resetAt: '', limit: null, metric: '',
        };
    }
    return {
        state: 'unavailable', detail: 'The last request failed', remaining: null,
        resetAt: '', limit: null, metric: '',
    };
}

function configuredValues(settings) {
    const values = [
        settings?.OUROBOROS_MODEL,
        settings?.OUROBOROS_MODEL_HEAVY,
        settings?.OUROBOROS_MODEL_LIGHT,
        ...String(settings?.OUROBOROS_MODEL_FALLBACKS || '').split(/[\s,]+/),
    ];
    return values.map((value) => String(value || '').trim()).filter(Boolean);
}

const NON_CHAT_MODEL_HINT = /(?:^|[-_/])(embedding|imagen|veo|lyria|tts|audio|image|robotics|translate)(?:$|[-_/])/i;

function chatCatalogItem(item) {
    const identity = normalizeModelIdentity(item?.value);
    return Boolean(identity) && !NON_CHAT_MODEL_HINT.test(identity)
        && !/(?:^|\/)aqa$/i.test(identity)
        && !/(deep-research|computer-use|video|nano-banana|antigravity|live)/i.test(identity);
}

function prettyModelName(value) {
    const raw = normalizeModelIdentity(value).split('/').slice(1).join('/') || String(value || '');
    return raw
        .replace(/^models\//i, '')
        .split('-')
        .map((part) => /^(?:[0-9]+(?:\.[0-9]+)?|[a-z]{1,3})$/i.test(part)
            ? part.toUpperCase() : `${part.slice(0, 1).toUpperCase()}${part.slice(1)}`)
        .join(' ');
}

function choiceLabel(item, settings) {
    const value = String(item?.value || '');
    const googleCompatible = /generativelanguage\.googleapis\.com/i.test(
        String(settings?.OPENAI_COMPATIBLE_BASE_URL || settings?.OPENAI_BASE_URL || ''),
    );
    if (googleCompatible && normalizeModelIdentity(value).startsWith('openai-compatible/')) {
        return `Google · ${prettyModelName(value)}`;
    }
    return String(item?.label || value);
}

export function buildModelChoices({ settings = {}, catalogItems = [], localStatus = {}, events = [] } = {}) {
    const choices = [];
    const seen = new Set();
    const localReady = String(localStatus?.status || '') === 'ready';
    if (localStatus?.model_name || localStatus?.model_path || localReady) {
        choices.push({
            value: LOCAL_VALUE,
            label: `Local · ${basenameWithoutGguf(localStatus.model_name || localStatus.model_path)}`,
            status: {
                state: localReady ? 'available' : 'unavailable',
                detail: localReady ? 'Running locally; no provider quota' : 'Local model server is not ready',
                remaining: null, resetAt: '', limit: null, metric: '',
            },
        });
        seen.add(LOCAL_VALUE);
    }
    if (Array.isArray(localStatus?.discovered_models)) {
        for (const dm of localStatus.discovered_models) {
            const dmName = dm.name || basenameWithoutGguf(dm.filename || dm.path);
            const isCurrent = (localStatus?.model_name && dmName.toLowerCase().includes(localStatus.model_name.toLowerCase())) ||
                              (localStatus?.model_path && localStatus.model_path === dm.path);
            const val = `local_discovered::${dm.path}::${dm.source || ''}::${dm.filename || ''}`;
            if (!isCurrent && !seen.has(val)) {
                choices.push({
                    value: val,
                    label: `Local · ${dmName.replace('-Q4_K_M', '')}`,
                    status: {
                        state: 'available',
                        detail: `${dm.size_gb ? dm.size_gb + ' GB · ' : ''}Local GGUF model on disk; click to start`,
                        remaining: null, resetAt: '', limit: null, metric: '',
                    },
                });
                seen.add(val);
            }
        }
    }
    const fallbackIsLocal = ['1', 'true', 'yes', 'on'].includes(
        String(settings?.USE_LOCAL_FALLBACK || '').toLowerCase(),
    );
    const catalog = (catalogItems || []).filter(chatCatalogItem);
    const catalogByIdentity = new Map(catalog.map((item) => [normalizeModelIdentity(item.value), item]));
    const configured = configuredValues(settings).map((value) => ({
        ...(catalogByIdentity.get(normalizeModelIdentity(value)) || {}),
        value,
    }));
    const entries = [...configured, ...catalog];
    for (const item of entries) {
        const value = String(item?.value || '').trim();
        const identity = normalizeModelIdentity(value);
        if (!value || seen.has(identity)) continue;
        if (fallbackIsLocal && normalizeModelIdentity(value) === 'local-model') continue;
        seen.add(identity);
        choices.push({
            value,
            label: choiceLabel(item, settings),
            status: deriveModelStatus(value, events),
        });
    }
    return choices;
}

export function modelSelectionPayload(value) {
    if (value === LOCAL_VALUE) return { USE_LOCAL_MAIN: true, OUROBOROS_MODEL: 'local-model' };
    if (typeof value === 'string' && value.startsWith('local_discovered::')) {
        const parts = value.split('::');
        const path = parts[1] || '';
        const source = parts[2] || path;
        const filename = parts[3] || '';
        return {
            USE_LOCAL_MAIN: true,
            OUROBOROS_MODEL: 'local-model',
            LOCAL_MODEL_SOURCE: source,
            LOCAL_MODEL_FILENAME: filename,
            LOCAL_MODEL_CONTEXT_LENGTH: 131072,
        };
    }
    return { OUROBOROS_MODEL: String(value || ''), USE_LOCAL_MAIN: false };
}

function stateGlyph(state) {
    return { available: '●', limited: '◐', exhausted: '×', unavailable: '×', unknown: '?' }[state] || '?';
}

function stateLabel(state) {
    return {
        available: 'Available', limited: 'Rate limited', exhausted: 'Quota exhausted',
        unavailable: 'Unavailable', unknown: 'Unknown',
    }[state] || 'Unknown';
}

function quotaDetail(status) {
    const observed = status.limit != null
        ? `Observed limit: ${Number(status.limit).toLocaleString()}${status.metric ? ` · ${status.metric}` : ''}. `
        : '';
    return `${observed}${status.detail}`;
}

function harnessQuotaLines(payload, quotaRead) {
    const subjects = new Map();
    for (const snapshot of payload?.quota || []) {
        const harness = String(snapshot?.subject?.harness || '');
        const subjectId = String(snapshot?.subject?.subject_id || '');
        if (!harness) continue;
        subjects.set(`${harness}\u0000${subjectId}`, { harness, subjectId });
    }
    if (!subjects.size) return ['Claude Code / Codex · subscription limits unavailable'];
    return [...subjects.values()].map(({ harness, subjectId }) => {
        const summary = quotaSummary(payload.quota || [], harness, subjectId, { quotaRead });
        const suffix = subjectId ? ` (${subjectId})` : '';
        return `${familyLabel(harness, payload)}${suffix} · ${summary.label}`;
    });
}

async function responseJson(response, fallback = {}, timeoutMs = READ_TIMEOUT_MS) {
    if (!response?.ok) return fallback;
    try {
        const payload = await timedPromise(response.json(), timeoutMs);
        return payload == null ? fallback : payload;
    } catch { return fallback; }
}

function timedFetch(path, options = {}, timeoutMs = READ_TIMEOUT_MS) {
    const request = apiFetch(path, options).catch(() => null);
    const timeout = new Promise((resolve) => setTimeout(() => resolve(null), timeoutMs));
    return Promise.race([request, timeout]);
}

function timedPromise(promise, timeoutMs = READ_TIMEOUT_MS) {
    const timeout = new Promise((resolve) => setTimeout(() => resolve(null), timeoutMs));
    return Promise.race([Promise.resolve(promise).catch(() => null), timeout]);
}

export function initChatModelControl({ root, showToast = () => {} } = {}) {
    if (!root) return () => {};
    const select = root.querySelector('[data-model-select]');
    const summary = root.querySelector('[data-model-summary]');
    const status = root.querySelector('[data-model-status]');
    const detail = root.querySelector('[data-model-detail]');
    const harness = root.querySelector('[data-harness-quota]');
    const refresh = root.querySelector('[data-model-refresh]');
    if (!select || !status || !detail || !refresh) return () => {};

    let disposed = false;
    let settings = {};
    let choices = [];
    let loadInFlight = null;

    function activeValue() {
        const local = ['1', 'true', 'yes', 'on'].includes(String(settings.USE_LOCAL_MAIN || '').toLowerCase());
        return local ? LOCAL_VALUE : String(settings.OUROBOROS_MODEL || '');
    }

    function render() {
        if (disposed) return;
        const selected = activeValue();
        select.replaceChildren(...choices.map((choice) => {
            const option = document.createElement('option');
            option.value = choice.value;
            option.textContent = `${stateGlyph(choice.status.state)} ${choice.label}`;
            return option;
        }));
        if (choices.some((choice) => choice.value === selected)) select.value = selected;
        const current = choices.find((choice) => choice.value === select.value) || choices[0];
        if (!current) {
            status.dataset.state = 'unknown';
            status.textContent = 'No models';
            detail.textContent = 'Configure a remote provider or start a local model.';
            return;
        }
        status.dataset.state = current.status.state;
        status.textContent = stateLabel(current.status.state);
        if (summary) summary.textContent = `${current.label} · ${stateLabel(current.status.state)}`;
        detail.textContent = quotaDetail(current.status);
    }

    async function load() {
        if (disposed) return;
        if (loadInFlight) return loadInFlight;
        loadInFlight = (async () => {
            refresh.disabled = true;
            status.dataset.state = 'loading';
            status.textContent = 'Checking…';
            try {
                // A reconnect can race the server's restart window. Retry the
                // read-only discovery calls briefly so the selector does not
                // stay empty just because the first request met a dead socket.
                let responses = [];
                for (let attempt = 0; attempt < 3; attempt += 1) {
                    const requests = [
                        timedFetch('/api/settings', { cache: 'no-store' }),
                        timedFetch('/api/model-catalog', { cache: 'no-store' }),
                        timedFetch('/api/local-model/status', { cache: 'no-store' }),
                        timedFetch('/api/logs/events?limit=2000', { cache: 'no-store' }),
                    ];

                    // Settings contain the configured route, so render those
                    // choices as soon as their response arrives. A slow remote
                    // catalog must not hide a model the owner already selected.
                    const settingsResp = await requests[0];
                    settings = await responseJson(settingsResp);
                    choices = buildModelChoices({ settings, events: [] });
                    if (choices.length) render();

                    responses = await Promise.all(requests);
                    // A local-status response alone is not enough evidence that
                    // the server is ready to provide the model/settings data.
                    if (responses[0] || responses[1] || attempt === 2) break;
                    await new Promise((resolve) => setTimeout(resolve, 350 * (attempt + 1)));
                }
                const [, catalogResp, localResp, eventsResp] = responses;
                if (!responses.some(Boolean)) {
                    throw new Error('Ouroboros is restarting; press Refresh in a moment.');
                }
                // `settingsResp` was consumed by the early render above. Do
                // not read that one-shot Response body a second time: a failed
                // second read would erase the configured models with `{}`.
                const [catalog, local, eventRows] = await Promise.all([
                    responseJson(catalogResp, { items: [] }),
                    responseJson(localResp),
                    responseJson(eventsResp, { entries: [] }),
                ]);
                choices = buildModelChoices({
                    settings,
                    catalogItems: catalog.items || [],
                    localStatus: local,
                    events: eventRows.entries || [],
                });
                render();
                // Subscription discovery is informative and must never block the
                // main-model control. Claudexor can be stale or offline while the
                // local/Gemini choices remain perfectly usable.
                if (harness) void timedPromise(claudexorStatus.refresh(), QUOTA_READ_TIMEOUT_MS)
                    .then(() => {
                        if (disposed) return;
                        const payload = claudexorStatus.snapshot || {};
                        const lines = harnessQuotaLines(payload, claudexorStatus.facet(FACET_QUOTA));
                        harness.textContent = lines.join('\n');
                    });
            } catch (error) {
                status.dataset.state = 'unavailable';
                status.textContent = 'Status unavailable';
                detail.textContent = String(error?.message || error);
            } finally {
                refresh.disabled = false;
            }
        })();
        try {
            await loadInFlight;
        } finally {
            loadInFlight = null;
        }
    }

    async function saveSelection() {
        const previous = activeValue();
        const next = select.value;
        if (!next || next === previous) {
            render();
            return;
        }
        select.disabled = true;
        try {
            const payloadBody = modelSelectionPayload(next);
            const response = await apiFetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payloadBody),
            });
            const payload = await responseJson(response);
            if (!response.ok) throw new Error(payload.error || 'Could not save the model');
            settings = { ...settings, ...payloadBody };

            if (typeof next === 'string' && next.startsWith('local_discovered::')) {
                const parts = next.split('::');
                const path = parts[1] || '';
                const source = parts[2] || path;
                const filename = parts[3] || '';
                try {
                    await apiFetch('/api/local-model/stop', { method: 'POST' });
                } catch {}
                try {
                    await apiFetch('/api/local-model/start', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            source,
                            filename,
                            n_ctx: 131072,
                            n_gpu_layers: -1,
                        }),
                    });
                } catch (e) {
                    console.warn('Auto-start local server note:', e);
                }
            }

            render();
            showToast('Model changed for new tasks.', 'success');
        } catch (error) {
            select.value = previous;
            render();
            showToast(`Could not change model: ${error?.message || error}`, 'error');
        } finally {
            select.disabled = false;
        }
    }

    const onSelect = () => saveSelection();
    const onRefresh = () => load();
    select.addEventListener('change', onSelect);
    refresh.addEventListener('click', onRefresh);
    load();
    const dispose = () => {
        disposed = true;
        select.removeEventListener('change', onSelect);
        refresh.removeEventListener('click', onRefresh);
    };
    // The chat controller calls this after a websocket reconnect. Keeping the
    // same single-flight loader also makes manual Refresh safe during reloads.
    dispose.refresh = () => load();
    return dispose;
}
