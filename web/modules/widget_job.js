/* Shared declarative widget request and job-status policy. */

export const WIDGET_REQUEST_TIMEOUT_MS = 25000;

function widgetTimeoutError() {
    const error = new Error('widget request timed out');
    error.code = 'WIDGET_REQUEST_TIMEOUT';
    error.retryable = true;
    return error;
}

export function isRetryableWidgetError(error) {
    if (!error || error.name === 'AbortError') return false;
    if (error.retryable === true) return true;
    if (error.name === 'TypeError') return true;
    const status = Number(error.status);
    return status === 408 || status === 429 || (status >= 500 && status <= 599);
}

export async function withWidgetRequestTimeout(task, controller, timeoutMs = WIDGET_REQUEST_TIMEOUT_MS) {
    let timedOut = false;
    const timeout = setTimeout(() => {
        timedOut = true;
        controller.abort();
    }, timeoutMs);
    try {
        const result = await task(controller.signal);
        if (timedOut) throw widgetTimeoutError();
        return result;
    } catch (error) {
        if (timedOut) throw widgetTimeoutError();
        throw error;
    } finally {
        clearTimeout(timeout);
    }
}

export function readWidgetJobStatus(data) {
    if (!data || typeof data !== 'object' || Array.isArray(data)) return undefined;
    return Object.prototype.hasOwnProperty.call(data, 'status') ? data.status : data.state;
}

const WIDGET_JOB_STATUS_GROUPS = {
    pending: new Set(['queued', 'pending', 'running', 'processing', 'in_progress', 'started', 'working']),
    success: new Set(['done', 'succeeded', 'success', 'complete', 'completed']),
    failure: new Set(['error', 'failed', 'failure', 'cancelled', 'canceled', 'timeout', 'expired']),
};

export function classifyWidgetJobStatus(value) {
    if (typeof value !== 'string') return 'invalid';
    const status = value.trim().toLowerCase();
    if (!status) return 'invalid';
    for (const [kind, values] of Object.entries(WIDGET_JOB_STATUS_GROUPS)) {
        if (values.has(status)) return kind;
    }
    // Preserve richer producer-specific in-progress labels; max_ticks still
    // bounds an unknown non-empty status.
    return 'pending';
}
