// One timeline line of a live card, rendered as HTML.
//
// Pure: it reads the line and the card's expanded-key set and returns a string.
// It touches no DOM and closes over no instance state, so the same line renders
// identically whether it is painted in bulk on a history load or patched in on
// its own when a single event arrives — one renderer, not two that drift.

import { escapeHtmlAttr, escapeHtmlText as escapeHtml, renderMarkdown } from './utils.js';

export function isLiveLineExpandable(item) {
    return Boolean(
        (item.fullHeadline && item.fullHeadline !== item.headline)
        || (item.fullBody && item.fullBody !== item.body)
        // P3: even when the preview equals the capped body, a server-truncated line
        // with a fetch ref has MORE to show (the genuinely-full output on demand).
        || (item.truncated && item.fullRef)
    );
}


export function buildTimelineItemHtml(item, record) {
    const expandable = isLiveLineExpandable(item);
    const expanded = expandable && record.expandedLineKeys.has(item.lineKey);
    const displayHeadline = expanded && item.fullHeadline ? item.fullHeadline : item.headline;
    // P3: when expanded, prefer the genuinely-full fetched output, then the capped
    // fullBody, then the preview body. A server-truncated line shows the fetched full
    // text in a bounded-scroll box so a huge research output never grows the chat.
    const displayBody = expanded ? (item.fetchedFull || item.fullBody || item.body) : item.body;
    const showingFetched = expanded && Boolean(item.fetchedFull);
    const loadingFull = expanded && Boolean(item.truncated && item.fullRef && !item.fetchedFull);
    const isProgressLine = item.phase === 'working' || item.phase === 'thinking';
    const bodyId = `chat-live-line-body-${String(record.groupId || 'task').replace(/[^A-Za-z0-9_-]/g, '-')}-${String(item.lineKey || '').replace(/[^A-Za-z0-9_-]/g, '-')}`;
    const headContent = `
        <span class="chat-live-line-title">${isProgressLine ? renderMarkdown(displayHeadline) : escapeHtml(displayHeadline)}</span>
        <span class="chat-live-line-repeat" ${item.count > 1 ? '' : 'hidden'}>${item.count > 1 ? `${item.count}x` : ''}</span>
        ${item.ts ? `<span class="chat-live-line-time">${escapeHtml(item.ts)}</span>` : ''}
    `;
    const headHtml = expandable
        ? `
            <button
                type="button"
                class="chat-live-line-toggle"
                data-live-line-toggle="${escapeHtmlAttr(item.lineKey)}"
                aria-expanded="${expanded ? 'true' : 'false'}"
                ${displayBody ? `aria-controls="${escapeHtmlAttr(bodyId)}"` : ''}
            >
                <span class="chat-live-line-head">${headContent}</span>
                <span class="chat-live-line-expand-label">${expanded ? 'Collapse' : ((item.truncated && item.fullRef) ? 'Show full' : 'Expand')}</span>
            </button>
        `
        : `<div class="chat-live-line-head">${headContent}</div>`;
    return `
        <div
            class="chat-live-line ${item.phase || 'working'}${expandable ? ' expandable' : ''}"
            data-live-line-key="${escapeHtmlAttr(item.lineKey || '')}"
            data-expanded="${expanded ? '1' : '0'}"
        >
            ${headHtml}
            ${displayBody ? `<div class="chat-live-line-body${showingFetched ? ' chat-live-line-body-full' : ''}" id="${escapeHtmlAttr(bodyId)}">${renderMarkdown(displayBody)}${loadingFull ? '<div class="chat-live-line-loading">Loading full output…</div>' : ''}</div>` : ''}
        </div>
    `;
}
