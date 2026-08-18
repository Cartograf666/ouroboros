"""Browser smoke tests for the project chat continuity contract.

Lives in its own module (not test_ui_smoke_playwright.py) so the giant smoke
module stays under the size-ratchet byte gate. Reuses its server fixture.

Four observable scenarios:
1. A stale history rebuild (response assembled before the send was logged)
   must not erase the owner's optimistic bubble or its routing receipt.
2. A chat opened AFTER a managed task started shows Working... from the
   /api/state activity snapshot, and the snapshot's absence concludes it.
3. An early final answer holds the card on "Finalizing…" —
   task_cost_finalized does not resolve it, the settled task_done does.
4. A project panel (one-shot hydration, no /api/state poll) concludes its
   managed activity on the settled task_done: the header returns to Online
   even though the early final did not conclude the turn.
"""

from __future__ import annotations

import json

import pytest

from tests.test_ui_smoke_playwright import direct_server_with_data  # noqa: F401 - pytest fixture import


def _launch(pw):
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    return browser, page


def _wait_status(page, expected, timeout=10_000):
    page.wait_for_function(
        """(expected) => {
            const el = document.querySelector('#chat-status');
            return el && el.textContent.trim() === expected;
        }""",
        arg=expected,
        timeout=timeout,
    )


@pytest.mark.ui_browser
def test_ui_smoke_stale_history_rebuild_keeps_owner_bubble(direct_server_with_data):  # noqa: F811
    """Reconnect rebuild against a stale (empty) history keeps bubble + ack."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    try:
        with sync_playwright() as pw:
            browser, page = _launch(pw)
            try:
                # Every history fetch in this scenario returns a STALE empty
                # window — as if it were assembled before the send was logged.
                page.route(
                    "**/api/chat/history*",
                    lambda route: route.fulfill(
                        content_type="application/json",
                        body=json.dumps({"messages": []}),
                    ),
                )
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_selector("#chat-status", state="attached", timeout=30_000)
                _wait_status(page, "Online", timeout=30_000)

                page.fill("#chat-input", "please keep this message visible")
                page.click("#chat-send")
                bubble = page.locator(".chat-bubble.user[data-client-message-id]").last
                bubble.wait_for(state="visible", timeout=10_000)
                cmid = bubble.get_attribute("data-client-message-id")
                assert cmid

                # Typed routing receipt lands on the bubble.
                page.evaluate(
                    """(cmid) => {
                        window.__ouroWs.emit('message_annotation', {
                            type: 'message_annotation',
                            annotation_type: 'routing_ack',
                            chat_id: 1,
                            client_message_id: cmid,
                            action: 'mailbox_delivery',
                            target: 'root-1',
                            status: 'delivered',
                        });
                    }""",
                    cmid,
                )
                annotation = page.locator(
                    f'.chat-bubble.user[data-client-message-id="{cmid}"] .msg-routing-annotation'
                )
                annotation.wait_for(state="visible", timeout=5_000)
                assert "Delivered to task" in annotation.inner_text()

                # Reconnect-driven full rebuild replays the STALE response.
                page.evaluate(
                    "() => window.__ouroWs.emit('open', { previouslyConnected: true })"
                )
                page.wait_for_selector(
                    '.chat-bubble[data-system-type="reconnect"]', timeout=10_000
                )
                survivor = page.locator(
                    f'.chat-bubble.user[data-client-message-id="{cmid}"]'
                )
                assert survivor.count() == 1
                assert "please keep this message visible" in survivor.inner_text()
                assert "Delivered to task" in survivor.locator(
                    ".msg-routing-annotation"
                ).inner_text()
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise


@pytest.mark.ui_browser
def test_ui_smoke_late_open_chat_shows_managed_activity(direct_server_with_data):  # noqa: F811
    """A chat that missed every typing frame still shows queue-backed Working...;
    the next authoritative snapshot without the task concludes it."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    try:
        with sync_playwright() as pw:
            browser, page = _launch(pw)
            try:
                state = {"managed": True}

                def _inject(route):
                    response = route.fetch()
                    payload = response.json()
                    payload["active_chat_activities"] = (
                        [{
                            "activity_id": "managed-root-1",
                            "chat_id": 1,
                            "project_id": "",
                            "client_message_id": "",
                            "kind": "managed_task",
                            "phase": "working",
                            "started_at": 1.0,
                        }]
                        if state["managed"] else []
                    )
                    route.fulfill(
                        content_type="application/json",
                        body=json.dumps(payload),
                    )

                page.route("**/api/state*", _inject)
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                # The managed activity arrives purely from the snapshot: this
                # page never saw a typing frame or a progress card.
                _wait_status(page, "Working...", timeout=30_000)

                # Queue authority concludes it: the task left PENDING/RUNNING.
                state["managed"] = False
                _wait_status(page, "Online", timeout=15_000)
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise


@pytest.mark.ui_browser
def test_ui_smoke_early_final_holds_finalizing_until_task_done(direct_server_with_data):  # noqa: F811
    """Finalizing hold: early final -> Finalizing…, cost checkpoint does not
    resolve the card, task_done does."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    card = '.chat-live-card[data-task-id="fin-1"]'
    try:
        with sync_playwright() as pw:
            browser, page = _launch(pw)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_selector("#chat-status", state="attached", timeout=30_000)
                _wait_status(page, "Online", timeout=30_000)

                # A tool call makes the root card visible.
                page.evaluate(
                    """() => window.__ouroWs.emit('log', {
                        type: 'log', chat_id: 1,
                        data: { type: 'tool_call_started', task_id: 'fin-1',
                                tool: 'run_command', ts: '2026-08-17T10:00:00+00:00' },
                    })"""
                )
                page.wait_for_selector(card, timeout=10_000)

                # Early final answer: delivered while post-task still runs.
                page.evaluate(
                    """() => window.__ouroWs.emit('chat', {
                        type: 'chat', role: 'assistant', chat_id: 1,
                        content: 'Here is the delivered answer.',
                        task_id: 'fin-1', task_phase: 'finalizing',
                        ts: '2026-08-17T10:00:05+00:00',
                    })"""
                )
                page.wait_for_function(
                    """(sel) => {
                        const el = document.querySelector(sel + ' .chat-live-phase');
                        return el && el.textContent.trim() === 'Finalizing…';
                    }""",
                    arg=card,
                    timeout=5_000,
                )
                assert page.locator(card).get_attribute("data-finished") == "0"
                # The answer itself is visible as an ordinary bubble.
                assert page.locator(
                    ".chat-bubble.assistant", has_text="Here is the delivered answer."
                ).count() >= 1

                # The cost checkpoint is bookkeeping — the card stays open.
                page.evaluate(
                    """() => window.__ouroWs.emit('log', {
                        type: 'log', chat_id: 1,
                        data: { type: 'task_cost_finalized', task_id: 'fin-1',
                                post_task_status: 'completed',
                                cost_accounting_status: 'available',
                                accounted_upper_bound_usd: 0.42, cost_final: true,
                                ts: '2026-08-17T10:00:07+00:00' },
                    })"""
                )
                page.wait_for_timeout(300)
                assert page.locator(card).get_attribute("data-finished") == "0"
                assert page.locator(f"{card} .chat-live-phase").inner_text().strip() == "Finalizing…"

                # The settled task_done resolves the card.
                page.evaluate(
                    """() => window.__ouroWs.emit('log', {
                        type: 'log', chat_id: 1,
                        data: { type: 'task_done', task_id: 'fin-1', status: 'completed',
                                outcome_axes: { lifecycle: { status: 'completed' } },
                                ts: '2026-08-17T10:00:09+00:00' },
                    })"""
                )
                page.wait_for_function(
                    """(sel) => document.querySelector(sel)?.dataset.finished === '1'""",
                    arg=card,
                    timeout=5_000,
                )
                assert page.locator(f"{card} .chat-live-phase").inner_text().strip() == "Done"
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise


@pytest.mark.ui_browser
def test_ui_smoke_project_panel_concludes_activity_on_task_done(direct_server_with_data):  # noqa: F811
    """Panels never poll /api/state; the settled task_done must conclude the
    managed activity so the panel header does not stay Working... forever."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    from ouroboros.projects_registry import create_project

    url = direct_server_with_data["url"]
    data_dir = direct_server_with_data["data_dir"]
    project = create_project(data_dir, "cont-panel", name="Continuity panel")
    project_chat = int(project["chat_id"])
    status_sel = '[id="pchat-cont-panel-status"]'

    def _panel_status_is(page, expected, timeout=10_000):
        page.wait_for_function(
            """({ sel, expected }) => {
                const el = document.querySelector(sel);
                return el && el.textContent.trim() === expected;
            }""",
            arg={"sel": status_sel, "expected": expected},
            timeout=timeout,
        )

    try:
        with sync_playwright() as pw:
            browser, page = _launch(pw)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.click('.nav-project-row[data-project-id="cont-panel"]')
                page.wait_for_selector("#project-panel:not([hidden])", timeout=30_000)
                _panel_status_is(page, "Online", timeout=30_000)

                # Managed typing frame registers the activity in the panel.
                page.evaluate(
                    """(chatId) => window.__ouroWs.emit('typing', {
                        type: 'typing', chat_id: chatId, activity_id: 'panel-root-1',
                        phase: 'working', kind: 'managed_task',
                    })""",
                    project_chat,
                )
                _panel_status_is(page, "Working...")

                # The early final does NOT conclude the turn (post-task runs).
                page.evaluate(
                    """(chatId) => window.__ouroWs.emit('chat', {
                        type: 'chat', role: 'assistant', chat_id: chatId,
                        content: 'Early answer.', task_id: 'panel-root-1',
                        task_phase: 'finalizing', ts: '2026-08-17T11:00:00+00:00',
                    })""",
                    project_chat,
                )
                _panel_status_is(page, "Working...")

                # The settled task_done concludes the activity; header settles.
                page.evaluate(
                    """(chatId) => window.__ouroWs.emit('log', {
                        type: 'log', chat_id: chatId,
                        data: { type: 'task_done', task_id: 'panel-root-1',
                                status: 'completed',
                                outcome_axes: { lifecycle: { status: 'completed' } },
                                ts: '2026-08-17T11:00:05+00:00' },
                    })""",
                    project_chat,
                )
                _panel_status_is(page, "Online")
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise
