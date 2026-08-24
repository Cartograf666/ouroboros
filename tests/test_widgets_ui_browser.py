from __future__ import annotations

import json
import os
import pathlib
import textwrap

import pytest

from tests.test_ui_smoke_playwright import direct_server_with_data as _direct_server_with_data

direct_server_with_data = _direct_server_with_data


def _write_module_widget_smoke_extension(data_dir: pathlib.Path) -> str:
    """Install reviewed module tabs that exercise host geometry and teardown."""
    from ouroboros.skill_loader import SkillReviewState, compute_content_hash, save_review_state

    name = "module_widget_smoke"
    skill_dir = data_dir / "skills" / "external" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            f"""\
            ---
            name: {name}
            description: Isolated module widget geometry smoke.
            version: 0.1.0
            type: extension
            entry: plugin.py
            permissions: ["route", "widget"]
            ---
            # Module widget geometry smoke
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "plugin.py").write_text(
        textwrap.dedent(
            """\
            async def ping(_request):
                return {"ok": True}


            def register(api):
                api.register_route("ping", ping, methods=("GET",))
                api.register_ui_tab("auto", "Auto module", render={"kind": "module", "entry": "widget.js"})
                api.register_ui_tab("fixed", "Fixed module", render={"kind": "module", "entry": "widget.js", "height": 480})
                api.register_ui_tab("capped", "Capped module", render={"kind": "module", "entry": "widget.js", "max_height": 640})
                api.register_ui_tab("small", "Small module", render={"kind": "module", "entry": "small.js"})
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "widget.js").write_text(
        textwrap.dedent(
            """\
            (() => {
                const root = document.getElementById('root');
                const style = document.createElement('style');
                style.textContent = 'body{margin:0;padding:12px;height:100vh;box-sizing:border-box;overflow-y:auto;font:14px sans-serif;color:#e8ecf3;background:#111}#root{display:block}.row{padding:8px 0;border-bottom:1px solid #445}button{margin:8px 0;padding:6px 10px}';
                root.appendChild(style);
                const heading = document.createElement('h2');
                heading.textContent = 'Module geometry probe';
                root.appendChild(heading);
                const addRows = (count) => {
                    for (let index = 0; index < count; index += 1) {
                        const row = document.createElement('div');
                        row.className = 'row';
                        row.textContent = `Measured row ${index + 1}`;
                        root.appendChild(row);
                    }
                };
                addRows(28);
                const button = document.createElement('button');
                button.textContent = 'Add rows';
                button.addEventListener('click', () => addRows(18));
                root.appendChild(button);
                fetch('/api/extensions/module_widget_smoke/ping').then((response) => {
                    if (!response.ok) throw new Error('ping failed');
                }).catch(() => {});
            })();
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "small.js").write_text(
        "document.getElementById('root').textContent = 'Small content';\n",
        encoding="utf-8",
    )
    content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
    save_review_state(data_dir, name, SkillReviewState(status="pass", content_hash=content_hash))
    return name


def _write_job_widget_smoke_extension(data_dir: pathlib.Path) -> str:
    """Install a tiny declarative job widget for retry-preservation E2E."""
    from ouroboros.skill_loader import SkillReviewState, compute_content_hash, save_review_state

    name = "job_widget_smoke"
    skill_dir = data_dir / "skills" / "external" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            f"""\
            ---
            name: {name}
            description: Isolated declarative job retry smoke.
            version: 0.1.0
            type: extension
            entry: plugin.py
            permissions: ["route", "widget"]
            ---
            # Job retry smoke
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "plugin.py").write_text(
        textwrap.dedent(
            """\
            async def start(_request):
                return {"job_id": "retry-job"}


            async def status(_request):
                return {"status": "queued", "progress": 10}


            def register(api):
                api.register_route("start", start, methods=("POST",))
                api.register_route("status", status, methods=("GET",))
                api.register_ui_tab(
                    "main",
                    "Job retry",
                    render={
                        "kind": "declarative",
                        "schema_version": 1,
                        "components": [
                            {"type": "action", "id": "job-action", "label": "Start job", "route": "start", "method": "POST", "job": True, "status_route": "status"},
                            {"type": "status", "id": "job-status", "target": "result", "loading": "Waiting", "success": "Done", "error": "Failed"},
                        ],
                    },
                )
            """
        ),
        encoding="utf-8",
    )
    content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
    save_review_state(data_dir, name, SkillReviewState(status="pass", content_hash=content_hash))
    return name


@pytest.mark.ui_browser
def test_ui_smoke_job_poll_preserves_id_after_transient_failure(direct_server_with_data):
    """Prove a retryable status failure keeps the same job id and resumes."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    skill = _write_job_widget_smoke_extension(direct_server_with_data["data_dir"])
    status_urls = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                toggled = page.evaluate(
                    """async (skill) => {
                        const response = await fetch(`/api/skills/${encodeURIComponent(skill)}/toggle`, {
                            method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({enabled: true}),
                        });
                        return {status: response.status, body: await response.json()};
                    }""",
                    skill,
                )
                assert toggled["status"] == 200, toggled

                def fulfill_status(route):
                    status_urls.append(route.request.url)
                    if len(status_urls) == 1:
                        route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "temporary"}))
                    elif len(status_urls) == 2:
                        route.fulfill(status=200, content_type="application/json", body=json.dumps({"status": "queued", "progress": 20}))
                    else:
                        route.fulfill(status=200, content_type="application/json", body=json.dumps({"status": "done", "result": {"message": "finished"}}))

                page.route(f"**/api/extensions/{skill}/status**", fulfill_status)
                page.click('[data-nav-page="widgets"]')
                card = page.locator(f'[data-widget-key="{skill}:main"]')
                card.wait_for(state="visible", timeout=30_000)
                card.locator('[data-widget-action="id:job-action"]').click()
                for _ in range(100):
                    if status_urls:
                        break
                    page.wait_for_timeout(50)
                assert len(status_urls) == 1, status_urls
                page.evaluate(
                    """() => {
                        const button = [...document.querySelectorAll('[data-nav-page="dashboard"]')]
                            .find((item) => getComputedStyle(item).display !== 'none');
                        button?.click();
                    }"""
                )
                # The declarative card has no iframe. Waiting beyond the
                # existing 2s interval proves its disposed poller did not
                # issue the queued retry while Widgets was hidden.
                page.wait_for_timeout(2_200)
                assert len(status_urls) == 1, status_urls
                page.evaluate(
                    """() => {
                        const button = [...document.querySelectorAll('[data-nav-page="widgets"]')]
                            .find((item) => getComputedStyle(item).display !== 'none');
                        button?.click();
                    }"""
                )
                card.wait_for(state="visible", timeout=10_000)
                page.wait_for_function(
                    "() => document.querySelector('.widget-status[data-state=\"success\"]')",
                    timeout=10_000,
                )
                assert len(status_urls) >= 3, status_urls
                job_ids = {url.split("job_id=", 1)[1].split("&", 1)[0] for url in status_urls}
                assert job_ids == {"retry-job"}, status_urls
                assert card.locator('.widget-status[data-state="error"]').count() == 0
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise


@pytest.mark.ui_browser
def test_ui_smoke_module_widgets_geometry_lifecycle(direct_server_with_data):
    """Prove module auto-height, fixed/capped contracts, and framed teardown."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    data_dir = direct_server_with_data["data_dir"]
    skill = _write_module_widget_smoke_extension(data_dir)
    evidence_dir = pathlib.Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", str(data_dir.parent)))
    evidence_dir.mkdir(parents=True, exist_ok=True)

    def card_selector(tab_id: str) -> str:
        return f'[data-widget-key="{skill}:{tab_id}"]'

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                toggled = page.evaluate(
                    """async (skill) => {
                        const response = await fetch(`/api/skills/${encodeURIComponent(skill)}/toggle`, {
                            method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({enabled: true}),
                        });
                        return {status: response.status, body: await response.json()};
                    }""",
                    skill,
                )
                assert toggled["status"] == 200, toggled
                page.click('[data-nav-page="widgets"]')
                auto = page.locator(card_selector("auto"))
                auto.wait_for(state="visible", timeout=30_000)
                auto_frame = page.frame_locator(f'{card_selector("auto")} iframe')
                auto_frame.locator("#root").wait_for(state="visible", timeout=10_000)

                page.wait_for_function(
                    """(selector) => {
                        const frame = document.querySelector(`${selector} iframe`);
                        return frame && frame.getBoundingClientRect().height > 320;
                    }""",
                    arg=card_selector("auto"),
                    timeout=10_000,
                )
                auto_metrics = page.locator(f'{card_selector("auto")} iframe').evaluate(
                    """frame => ({height: frame.getBoundingClientRect().height, client: frame.clientHeight})"""
                )
                child_metrics = auto_frame.locator("body").evaluate(
                    """body => {
                        const root = document.querySelector('#root');
                        const style = getComputedStyle(body);
                        return {
                            client: body.clientHeight,
                            scroll: body.scrollHeight,
                            root: root?.scrollHeight || 0,
                            cssHeight: style.height,
                            padding: `${style.paddingTop}/${style.paddingBottom}`,
                            marginBottom: style.marginBottom,
                            bodyRect: body.getBoundingClientRect().toJSON(),
                            rootRect: root?.getBoundingClientRect().height || 0,
                            rootBottom: root?.getBoundingClientRect().bottom || 0,
                            scrollTop: body.scrollTop,
                        };
                    }"""
                )
                assert auto_metrics["height"] > 320, auto_metrics
                # Chromium can retain a two-pixel fractional/border rounding delta;
                # the host's own resize reserve is tested separately below.
                assert child_metrics["scroll"] <= child_metrics["client"] + 3, child_metrics
                assert child_metrics["root"] <= auto_metrics["client"] + 3, child_metrics

                first_height = auto_metrics["height"]
                page.wait_for_timeout(250)
                second_height = page.locator(f'{card_selector("auto")} iframe').evaluate(
                    "frame => frame.getBoundingClientRect().height"
                )
                assert abs(first_height - second_height) <= 1
                auto_frame.get_by_role("button", name="Add rows").click()
                page.wait_for_function(
                    """([selector, previous]) => document.querySelector(`${selector} iframe`)?.getBoundingClientRect().height > previous + 100""",
                    arg=[card_selector("auto"), second_height],
                    timeout=10_000,
                )

                fixed_frame = page.locator(f'{card_selector("fixed")} iframe')
                capped_frame = page.locator(f'{card_selector("capped")} iframe')
                small_frame = page.locator(f'{card_selector("small")} iframe')
                page.wait_for_function(
                    """(selectors) => selectors.every((selector) => document.querySelector(`${selector} iframe`))""",
                    arg=[card_selector("fixed"), card_selector("capped"), card_selector("small")],
                    timeout=10_000,
                )
                assert fixed_frame.evaluate("frame => frame.getBoundingClientRect().height") == 480
                capped_height = capped_frame.evaluate("frame => frame.getBoundingClientRect().height")
                assert capped_height == 640, capped_height
                small_height = small_frame.evaluate("frame => frame.getBoundingClientRect().height")
                assert 320 <= small_height < 700, small_height

                page.screenshot(path=str(evidence_dir / "module-widgets-desktop.png"), full_page=True)
                page.set_viewport_size({"width": 430, "height": 932})
                page.wait_for_timeout(300)
                assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
                narrow_height = page.locator(f'{card_selector("auto")} iframe').evaluate(
                    "frame => frame.getBoundingClientRect().height"
                )
                assert narrow_height > 320
                page.screenshot(path=str(evidence_dir / "module-widgets-narrow.png"), full_page=True)

                page.evaluate(
                    """() => {
                        const button = [...document.querySelectorAll('[data-nav-page="dashboard"]')]
                            .find((item) => getComputedStyle(item).display !== 'none');
                        button?.click();
                    }"""
                )
                page.wait_for_function(
                    "(selector) => document.querySelector(`${selector} iframe`) === null",
                    arg=card_selector("auto"),
                    timeout=10_000,
                )
                page.evaluate(
                    """() => {
                        const button = [...document.querySelectorAll('[data-nav-page="widgets"]')]
                            .find((item) => getComputedStyle(item).display !== 'none');
                        button?.click();
                    }"""
                )
                page.locator(card_selector("auto")).locator("iframe").wait_for(state="attached", timeout=10_000)
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise
