/**
 * Skill Review chat cards (node-tested).
 *
 * Since a776639f the producer writes ONE compact reference line per review
 * terminal into chat.jsonl; the full evidence lives in the skill's durable
 * review_history.jsonl. Rows carrying the exact-job reference (skill + job_id)
 * lazily fetch the server-rendered block from
 * GET /api/skills/{skill}/review-history/{job_id}; rows without it (legacy
 * full-text rows) keep local expansion of their own text.
 */

import { apiFetch } from './api_client.js';
import { escapeHtmlAttr, renderMarkdown } from './utils.js';

// escapeHtmlAttr (pure string, node-safe) is used for text content too:
// over-escaping quotes renders identically in the browser.

export function summarizeSkillReviewMessage(text) {
    const raw = String(text || '');
    const lines = raw.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    const headline = lines[0] || 'Skill review';
    const hashLine = lines.find((line) => line.startsWith('content_hash=')) || '';
    const reviewersLine = lines.find((line) => line.startsWith('Reviewers:')) || '';
    const findingsLine = lines.find((line) => /^##\s+Findings/.test(line)) || '';
    const meta = [hashLine, reviewersLine.replace(/^Reviewers:\s*/, ''), findingsLine.replace(/^##\s*/, '')]
        .filter(Boolean)
        .map((line) => escapeHtmlAttr(line.length > 140 ? `${line.slice(0, 137)}...` : line))
        .join(' · ');
    return {
        headline: escapeHtmlAttr(headline.replace(/^#+\s*/, '')),
        meta,
    };
}

export function renderSkillReviewDisclosure(text, ref = null, deps = {}) {
    const render = deps.render || renderMarkdown;
    const summary = summarizeSkillReviewMessage(text);
    const jobRef = ref && ref.skill && ref.jobId ? ref : null;
    const refAttrs = jobRef
        ? ` data-skill-review-skill="${escapeHtmlAttr(jobRef.skill)}" data-skill-review-job="${escapeHtmlAttr(jobRef.jobId)}"`
        : '';
    return `
        <div class="skill-review-disclosure" data-skill-review-disclosure data-expanded="0"${refAttrs}>
            <button type="button" class="skill-review-summary-button" data-skill-review-toggle aria-expanded="false">
                <span class="skill-review-summary-main">${summary.headline}</span>
                <span class="skill-review-summary-side">
                    <span class="skill-review-meta">${summary.meta}</span>
                    <span class="skill-review-toggle-label">Show review</span>
                </span>
            </button>
            <div class="skill-review-full" data-skill-review-full hidden>${jobRef ? '' : render(text)}</div>
        </div>
    `;
}

/**
 * Fill a reference row's detail container from
 * GET /api/skills/{skill}/review-history/{job_id}. States ride
 * `full.dataset.state`: '' (idle) → 'loading' → 'loaded' | 'error'; an
 * in-flight or already-loaded container is never refetched (Retry clears the
 * state first). Errors degrade honestly inline and keep the card usable.
 */
export async function loadSkillReviewDetail(full, ref, deps = {}) {
    const fetchImpl = deps.fetchImpl || apiFetch;
    const render = deps.render || renderMarkdown;
    if (!full || !ref || !ref.skill || !ref.jobId) return '';
    if (full.dataset.state === 'loading' || full.dataset.state === 'loaded') {
        return full.dataset.state;
    }
    full.dataset.state = 'loading';
    full.innerHTML = '<div class="skill-review-loading">Loading review details…</div>';
    try {
        const url = `/api/skills/${encodeURIComponent(ref.skill)}/review-history/${encodeURIComponent(ref.jobId)}`;
        const resp = await fetchImpl(url, { cache: 'no-store' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const markdown = String(data?.markdown || '');
        if (!markdown) throw new Error('empty review detail');
        full.innerHTML = render(markdown);
        full.dataset.state = 'loaded';
    } catch (err) {
        full.dataset.state = 'error';
        full.innerHTML = `<div class="skill-review-error">Review details unavailable (${escapeHtmlAttr(String(err?.message || err))}). `
            + '<button type="button" class="skill-review-retry" data-skill-review-retry>Retry</button></div>';
    }
    return full.dataset.state;
}

/**
 * Wire one rendered skill-review bubble: expand/collapse toggle, first-expand
 * lazy fetch for reference rows, and the error-state Retry. `bubble` is the
 * chat bubble element containing a `renderSkillReviewDisclosure` result;
 * `onLayoutChange` runs after any content/height change.
 */
export function wireSkillReviewDisclosure(bubble, onLayoutChange, deps = {}) {
    const toggle = bubble.querySelector('[data-skill-review-toggle]');
    if (!toggle) return false;
    const disclosure = bubble.querySelector('[data-skill-review-disclosure]');
    const full = bubble.querySelector('[data-skill-review-full]');
    const reviewRef = disclosure?.dataset.skillReviewSkill && disclosure?.dataset.skillReviewJob
        ? { skill: disclosure.dataset.skillReviewSkill, jobId: disclosure.dataset.skillReviewJob }
        : null;
    toggle.addEventListener('click', () => {
        const label = bubble.querySelector('.skill-review-toggle-label');
        const expanded = disclosure?.dataset.expanded === '1';
        if (!disclosure || !full) return;
        disclosure.dataset.expanded = expanded ? '0' : '1';
        full.hidden = expanded;
        toggle.setAttribute('aria-expanded', expanded ? 'false' : 'true');
        if (label) label.textContent = expanded ? 'Show review' : 'Hide review';
        // Reference rows fetch the rendered review once on first expand;
        // collapse/re-expand keeps whatever state the container reached.
        if (!expanded && reviewRef && !full.dataset.state) {
            loadSkillReviewDetail(full, reviewRef, deps).then(onLayoutChange);
        }
        onLayoutChange();
    });
    if (reviewRef && full) {
        full.addEventListener('click', (ev) => {
            if (!ev.target?.closest?.('[data-skill-review-retry]')) return;
            full.dataset.state = '';
            loadSkillReviewDetail(full, reviewRef, deps).then(onLayoutChange);
        });
    }
    return true;
}
