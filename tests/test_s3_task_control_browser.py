"""S3 (Q1/Q2/HQ1) focused browser proofs for the shared task control dropdown.

A NEW marker-gated module (the giant smoke file is byte-pinned): real server +
real bundles in Chromium, with the mutating control endpoints stubbed at the
`window.fetch` seam so the proofs stay deterministic and the no-chat contract
is asserted against the LIVE chat DOM, not a unit model.

Covered here (both owner surfaces):
  * Chat live card: trigger label, the exact frozen three-action dropdown,
    dismiss-continues-the-run, "Hurry up" = local toast + stable request_id
    idempotent retry + ZERO new chat bubbles, "Wrap up" = "Finalizing…"
    card with the pending menu collapsed to "Stop now" only.
  * Activity tab: the SAME shared dropdown (product-wide parity) with the same
    labels and the same no-chat hurry acknowledgement.

Run: OUROBOROS_RUN_UI_SMOKE=1 python -m pytest -o addopts="" -m ui_browser \
         tests/test_s3_task_control_browser.py
"""
from __future__ import annotations

import json

import pytest

from tests.test_ui_smoke_playwright import (
    direct_server_with_data as _direct_server_with_data,
)

# Re-export the shared server fixture under its canonical name so the tests
# below can request it (aliased import keeps ruff F811 clean).
direct_server_with_data = _direct_server_with_data

# The frozen owner wording (Q2/HQ1) — asserted verbatim, in order.
EXPECTED_ACTIONS = [
    ("finalize", "Wrap up"),
    ("hurry", "Hurry up"),
    ("stop_now", "Stop now"),
]

# One fetch-seam stub shared by both surfaces: captures every mutating control
# call for later assertion and answers the S3 contract shapes without touching
# the real queue (GET traffic falls through to the real server).
_FETCH_STUB_JS = """
(taskId) => {
    const real = window.fetch.bind(window);
    window.__s3Calls = [];
    const jsonResp = (obj, status = 200) => Promise.resolve(new Response(
        JSON.stringify(obj), { status, headers: { 'Content-Type': 'application/json' } },
    ));
    window.__s3PendingCancel = false;
    window.fetch = (input, init = {}) => {
        const url = String(input && input.url ? input.url : input);
        const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
        const record = (kind) => window.__s3Calls.push({
            kind, url, method, body: init && init.body ? JSON.parse(init.body) : null,
        });
        if (url.includes(`/api/tasks/${taskId}/hurry`)) {
            record('hurry');
            const dup = window.__s3Calls.filter((c) => c.kind === 'hurry').length > 1;
            return jsonResp({
                ok: true, task_id: taskId,
                request_id: (init && init.body) ? JSON.parse(init.body).request_id : '',
                state: 'requested', attempt_key: 1, duplicate: dup,
            });
        }
        if (url.includes(`/api/tasks/${taskId}/cancel`)) {
            record('cancel');
            window.__s3PendingCancel = true;
            return jsonResp({
                ok: true, task_id: taskId, cancelled: [], cascade: true,
                cancel_state: 'pending', stop_policy: 'finalize_then_cancel',
            }, 202);
        }
        if (method === 'GET' && url.endsWith(`/api/tasks/${taskId}`) && window.__s3PendingCancel) {
            return jsonResp({
                ok: true, task_id: taskId, status: 'running',
                cancel_state: 'pending', stop_policy: 'finalize_then_cancel',
            });
        }
        return real(input, init);
    };
}
"""


def _menu_labels(menu) -> list[tuple[str, str]]:
    items = menu.locator(".task-control-item")
    return [
        (
            items.nth(i).get_attribute("data-task-control") or "",
            (items.nth(i).inner_text() or "").strip(),
        )
        for i in range(items.count())
    ]


def _chat_bubble_count(page) -> int:
    return page.locator(".chat-bubble").count()


@pytest.mark.ui_browser
def test_s3_chat_card_dropdown_hurry_and_soft_stop(direct_server_with_data):
    """Chat surface: frozen dropdown, no-chat hurry with idempotent request_id
    retry, and the soft stop collapsing the pending menu to the escalation."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    data_dir = direct_server_with_data["data_dir"]
    logs_dir = data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "chat.jsonl").write_text("", encoding="utf-8")
    (logs_dir / "progress.jsonl").write_text(
        json.dumps({
            "ts": "2026-08-15T10:00:00+00:00", "chat_id": 1, "task_id": "live-root",
            "content": "Working on the big thing", "cancelable": True,
        }) + "\n",
        encoding="utf-8",
    )

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                live = page.locator('.chat-live-card[data-task-id="live-root"]')
                live.wait_for(state="attached", timeout=30_000)
                trigger = live.locator("[data-cancel-run]")
                trigger.wait_for(state="attached", timeout=30_000)
                assert trigger.inner_text().strip() == "Stop…"
                page.evaluate(_FETCH_STUB_JS, "live-root")
                bubbles_before = _chat_bubble_count(page)

                # 1) The dropdown offers EXACTLY the three frozen actions, in
                #    order and verbatim; Escape dismisses = the run continues.
                trigger.click()
                menu = live.locator(".task-control-menu")
                menu.wait_for(state="visible", timeout=10_000)
                assert _menu_labels(menu) == EXPECTED_ACTIONS
                page.keyboard.press("Escape")
                menu.wait_for(state="detached", timeout=10_000)
                assert trigger.is_enabled()
                assert page.evaluate("() => window.__s3Calls.length") == 0

                # 2) "Hurry up": typed control + LOCAL toast; the chat DOM
                #    gains ZERO bubbles (HQ1's no-chat contract, live DOM).
                trigger.click()
                menu.wait_for(state="visible", timeout=10_000)
                menu.locator('[data-task-control="hurry"]').click()
                toast = page.locator(".toast", has_text="Hurry up: accepted")
                toast.wait_for(state="visible", timeout=10_000)
                calls = page.evaluate("() => window.__s3Calls")
                assert [c["kind"] for c in calls] == ["hurry"]
                first_body = calls[0]["body"]
                assert set(first_body.keys()) == {"request_id"}, first_body
                assert first_body["request_id"].startswith("hurry-")
                assert _chat_bubble_count(page) == bubbles_before

                # 3) Retry of the SAME owner intent reuses the SAME request_id
                #    (idempotent endpoint ack -> the duplicate toast wording).
                trigger.click()
                menu.wait_for(state="visible", timeout=10_000)
                menu.locator('[data-task-control="hurry"]').click()
                page.locator(".toast", has_text="already accepted").wait_for(
                    state="visible", timeout=10_000,
                )
                calls = page.evaluate("() => window.__s3Calls")
                assert [c["kind"] for c in calls] == ["hurry", "hurry"]
                assert calls[1]["body"]["request_id"] == first_body["request_id"]
                assert _chat_bubble_count(page) == bubbles_before

                # 4) "Wrap up": 202 keeps the card honestly LIVE as
                #    "Finalizing…", the trigger stays reachable, and the
                #    pending menu offers ONLY "Stop now".
                trigger.click()
                menu.wait_for(state="visible", timeout=10_000)
                menu.locator('[data-task-control="finalize"]').click()
                page.wait_for_function(
                    "() => document.querySelector('.chat-live-card[data-task-id=\"live-root\"]"
                    " [data-live-phase]')?.textContent === 'Finalizing…'",
                    timeout=10_000,
                )
                calls = page.evaluate("() => window.__s3Calls")
                assert calls[-1]["kind"] == "cancel"
                assert calls[-1]["body"] == {"cascade": True, "stop_policy": "finalize_then_cancel"}
                trigger.wait_for(state="attached", timeout=10_000)
                page.wait_for_function(
                    "() => !document.querySelector('.chat-live-card[data-task-id=\"live-root\"]"
                    " [data-cancel-run]')?.disabled",
                    timeout=10_000,
                )
                trigger.click()
                menu.wait_for(state="visible", timeout=10_000)
                assert _menu_labels(menu) == [("stop_now", "Stop now")]
                page.keyboard.press("Escape")
                menu.wait_for(state="detached", timeout=10_000)
                assert _chat_bubble_count(page) == bubbles_before
                page.screenshot(
                    path=str(data_dir.parent / "s3-chat-control.png"), full_page=True,
                )
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise


@pytest.mark.ui_browser
def test_s3_activity_tab_dropdown_parity_and_no_chat_hurry(direct_server_with_data):
    """Activity surface: the SAME shared dropdown (labels, order, hurry flow)
    and the same no-chat acknowledgement — product-wide parity (Q2/HQ1)."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    data_dir = direct_server_with_data["data_dir"]
    logs_dir = data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "chat.jsonl").write_text("", encoding="utf-8")
    (logs_dir / "progress.jsonl").write_text("", encoding="utf-8")

    # The activity queue view is fed by GET /api/tasks?queue_only=1; stub it at
    # the same fetch seam (no real queue mutation from a UI test).
    activity_stub = """
    (taskId) => {
        const real = window.fetch.bind(window);
        window.fetch = (input, init = {}) => {
            const url = String(input && input.url ? input.url : input);
            const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
            if (method === 'GET' && url.includes('/api/tasks?queue_only=1')) {
                return Promise.resolve(new Response(JSON.stringify({
                    ok: true,
                    queue: {
                        running: [{ id: taskId, type: 'task', runtime_sec: 42,
                                    task: { id: taskId, title: 'Active task' } }],
                        pending: [],
                    },
                }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
            }
            if (method === 'GET' && url.endsWith(`/api/tasks/${taskId}`)) {
                return Promise.resolve(new Response(JSON.stringify({
                    ok: true, task_id: taskId, status: 'running',
                }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
            }
            return real(input, init);
        };
    }
    """

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_selector("#page-chat", timeout=30_000)
                page.evaluate(activity_stub, "act-1")
                page.evaluate(_FETCH_STUB_JS.replace(
                    "const real = window.fetch.bind(window);",
                    "const real = window.fetch;",
                ), "act-1")

                page.click('[data-nav-page="dashboard"]')
                page.click('[data-dashboard-tab="activity"]')
                row_btn = page.locator('#dashboard-panel-activity [data-act="task-control"][data-id="act-1"]')
                row_btn.wait_for(state="visible", timeout=30_000)
                assert row_btn.inner_text().strip() == "Stop…"

                # Parity: the same shared menu with the exact frozen actions.
                row_btn.click()
                menu = page.locator("#dashboard-panel-activity .task-control-menu")
                menu.wait_for(state="visible", timeout=10_000)
                assert _menu_labels(menu) == EXPECTED_ACTIONS

                # "Hurry up" from Activity: local toast only, no chat bubble.
                # Baseline captured HERE (post-navigation) so async startup
                # bubbles (awaken banner, history replay) are already settled.
                bubbles_before = _chat_bubble_count(page)
                menu.locator('[data-task-control="hurry"]').click()
                page.locator(".toast", has_text="Hurry up: accepted").wait_for(
                    state="visible", timeout=10_000,
                )
                calls = page.evaluate("() => window.__s3Calls")
                assert [c["kind"] for c in calls] == ["hurry"]
                assert set(calls[0]["body"].keys()) == {"request_id"}
                assert _chat_bubble_count(page) == bubbles_before
                # Belt-and-suspenders: no bubble anywhere mentions the control.
                assert page.locator(".chat-bubble", has_text="Hurry up").count() == 0
                page.screenshot(
                    path=str(data_dir.parent / "s3-activity-control.png"), full_page=True,
                )
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise
