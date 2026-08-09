"""SSOT contract for shared web helpers (v5.8.3-rc.5 dedup pass).

``safeExternalHrefAttr``, ``renderMarkdownSafe``, ``boundedText``, and
``fetchJson`` previously had local copies in ``marketplace.js``,
``skills.js``, ``widgets.js``, and ``ouroboroshub.js``. They are now
owned by ``web/modules/utils.js`` for content helpers and
``web/modules/api_client.js`` for the gateway JSON-fetch helper, so the
URL/markdown/API error contracts cannot drift between modules.

These checks are static-text guards: they pin the helpers' presence in
``utils.js`` and the absence of duplicate function definitions in the
consumer modules. Any reintroduction of a local copy would be caught here
before the marketplace/skills/widgets surface diverged on a security
boundary (e.g. a publisher-supplied ``javascript:`` href slipping through
because one module's local helper was missing the protocol allowlist).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_MODULES = REPO_ROOT / "web" / "modules"


def _read(name: str) -> str:
    return (WEB_MODULES / name).read_text(encoding="utf-8")


def test_utils_exports_shared_helpers():
    """``utils.js`` keeps content helpers and re-exports gateway fetchJson."""
    src = _read("utils.js")
    for sig in (
        "export function safeExternalHrefAttr(",
        "export function renderMarkdownSafe(",
        "export function boundedText(",
        "export { fetchJson } from './api_client.js';",
    ):
        assert sig in src, f"utils.js must export {sig.strip().rstrip('(')}"
    assert "export async function fetchJson(" not in src
    assert "export async function fetchJson(" in _read("api_client.js")


def test_api_client_owns_extension_route_helpers():
    src = _read("api_client.js")
    for sig in (
        "export function cleanExtensionRoute(",
        "export function extensionRoutePrefix(",
        "export function extensionRoutePath(",
    ):
        assert sig in src
    assert "route.includes('\\\\')" in src
    assert "part === '..'" in src
    assert "encodeURIComponent(skill)" in src


def test_api_client_owns_json_post_ok_false_handling():
    api_src = _read("api_client.js")
    mcp_src = _read("mcp_settings.js")

    assert "export function jsonPost(" in api_src
    assert "rejectOkFalse" in api_src
    # mcp_settings must take its POST helper from api_client (it may co-import
    # apiFetch for the /api/mcp/status refresh) and must not roll its own postJson.
    assert "jsonPost" in mcp_src
    assert "} from './api_client.js';" in mcp_src
    assert "function postJson(" not in mcp_src
    assert "async function postJson(" not in mcp_src


def test_safe_external_href_attr_blocks_unsafe_schemes():
    """Schema allowlist must keep blocking javascript:/data:/vbscript:/mailto: hrefs."""
    src = _read("utils.js")
    block = src.split("export function safeExternalHrefAttr", 1)[1].split("export function", 1)[0]
    # Only http: and https: are allowed; mailto/javascript/data must NOT pass.
    assert "parsed.protocol === 'http:' || parsed.protocol === 'https:'" in block
    assert "escapeHtmlAttr(parsed.toString())" in block
    # Unparseable / unsafe → empty string (truthy gate at call sites).
    assert "return ''" in block


def test_render_markdown_safe_strips_dangerous_tags_and_attrs():
    """The DOMPurify allowlist must continue to ban script-bearing tags."""
    src = _read("utils.js")
    block = src.split("export function renderMarkdownSafe", 1)[1].split("export function", 1)[0]
    for forbidden_tag in ("script", "iframe", "object", "embed", "form", "input", "img"):
        assert f"'{forbidden_tag}'" in block, f"renderMarkdownSafe must FORBID_TAGS {forbidden_tag}"
    for forbidden_attr in ("style", "src", "srcset", "srcdoc"):
        assert f"'{forbidden_attr}'" in block, f"renderMarkdownSafe must FORBID_ATTR {forbidden_attr}"


def test_marketplace_does_not_redeclare_shared_helpers():
    """marketplace.js must import shared helpers, not redeclare them."""
    src = _read("marketplace.js")
    # Marketplace no longer renders package markdown after Details removal.
    assert "renderMarkdownSafe" not in src
    assert "safeExternalHrefAttr" in src
    assert "boundedText" in src
    assert "fetchJson" in src
    # No local function declarations of the SSOT helpers.
    assert "function boundedText(" not in src, "marketplace.js must use utils.boundedText"
    assert "async function fetchJson(" not in src, "marketplace.js must use utils.fetchJson"
    # ``safeExternalUrl`` may exist as a local *alias* (`const safeExternalUrl = safeExternalHrefAttr`)
    # but not as a function definition.
    assert "function safeExternalUrl(" not in src, "marketplace.js must alias to utils.safeExternalHrefAttr"


def test_skills_does_not_redeclare_shared_helpers():
    src = _read("skills.js")
    renderer = _read("skill_card_renderer.js")
    api_client = _read("api_client.js")
    assert "boundedText" in src
    assert "safeExternalHrefAttr as safeExternalUrl" in renderer
    assert "function boundedText(" not in src
    assert "function safeExternalUrl(" not in src + renderer
    assert "source === 'self_authored' || source === 'external'" in renderer
    assert "payloadRoot.startsWith('skills/external/')" in renderer
    assert "skills-delete-local" in renderer
    assert "apiClient.deleteSkill(name, payloadRoot)" in src
    assert "/api/skills/${encodeURIComponent(skill)}/delete" in api_client
    assert "payload_root: payloadRoot" in api_client
    assert "data/state/skills/${name}" in src


def test_widgets_uses_shared_render_markdown():
    src = _read("widgets.js")
    assert "renderMarkdownSafe" in src
    # Local declaration removed in v5.8.3-rc.5.
    assert "function renderMarkdownSafe(" not in src


def test_ouroboroshub_uses_shared_fetch_json():
    src = _read("ouroboroshub.js")
    assert "fetchJson" in src
    # Local async fetchJson removed in v5.8.3-rc.5.
    assert "async function fetchJson(" not in src


def test_onboarding_wizard_is_loaded_as_an_es_module():
    """The wizard is served from /static as a real ES module.

    It used to be inlined into a classic ``<script>`` inside a self-contained
    document, which is why it could never import ordinary ``web/modules/*``
    code. Any import it grows must resolve to a real sibling module — the page
    is served from ``/onboarding``, so a bare relative specifier resolves
    against the SCRIPT url, not the document.
    """
    import re

    template = (REPO_ROOT / "web" / "onboarding_template.html").read_text(encoding="utf-8")
    assert '<script type="module" src="/static/modules/onboarding_wizard.js"></script>' in template

    src = _read("onboarding_wizard.js")
    for specifier in re.findall(r"^\s*import\s.*?from\s+'([^']+)';", src, re.M):
        assert specifier.startswith("./"), f"non-relative wizard import: {specifier}"
        assert (REPO_ROOT / "web" / "modules" / specifier[2:]).is_file(), specifier


def test_accent_tokens_have_concrete_rgba_values():
    """The v5.8.3-rc.5 ``--accent-*`` numbered fade tokens added to
    ``web/style.css`` :root must each map to a concrete
    ``rgba(201, 53, 69, X)`` value — never to ``var(--accent-XX)`` (a
    self-reference forms an invalid CSS cycle and the entire crimson
    accent system silently fails to apply at computed-value time).

    Triad reviewers (gpt-5.5, gemini-3.5-flash, claude-opus-4.6) caught
    this exact regression in the first dry-run of v5.8.3-rc.5: a
    file-wide sed swap had also rewritten the :root definitions
    themselves, leaving every ``--accent-04: var(--accent-04);``-style
    cycle. This guard pins the fix and prevents the same regression
    class from returning silently.
    """
    import re
    src = (REPO_ROOT / "web" / "style.css").read_text(encoding="utf-8")
    root_match = re.search(r":root\s*\{([^}]+)\}", src, re.S)
    assert root_match, ":root block not found in web/style.css"
    root_body = root_match.group(1)

    expected = (
        "--accent-dim",
        "--accent-glow",
        "--accent-04",
        "--accent-05",
        "--accent-08",
        "--accent-10",
        "--accent-12",
        "--accent-18",
        "--accent-22",
        "--accent-25",
        "--accent-55",
    )
    for name in expected:
        decl_match = re.search(rf"{re.escape(name)}\s*:\s*([^;]+);", root_body)
        assert decl_match, f"{name} not declared in :root"
        value = decl_match.group(1).strip()
        # The value must be a literal rgba(...) anchored on the crimson
        # accent triple; never a ``var(<itself>)`` cycle, never a ``var()``
        # to a different accent token (that pattern works in CSS but
        # would silently break this token if its referent regresses).
        assert "var(" not in value, (
            f"{name} must not reference another CSS variable (cycle / "
            f"silent-drift risk); got: {value!r}"
        )
        assert "rgba(201, 53, 69," in value, (
            f"{name} must be defined on the crimson accent triple "
            f"rgba(201, 53, 69, X); got: {value!r}"
        )


@pytest.mark.parametrize("module_name", ["onboarding_wizard.js", "onboarding_overlay.js"])
def test_onboarding_uses_the_shared_escape_helper(module_name):
    """Both onboarding modules are real ES modules now, so escaping has ONE
    authority instead of a copy per module.

    The predecessor of this test compared the two ``replace`` chains character
    for character and explained the copy by saying the wizard "is an IIFE bundle
    and cannot import from utils.js". That stopped being true when the wizard
    became a linked ES module: the comparison then froze a duplicate the code
    was free to delete. Behaviour of the shared helper itself is pinned by
    web/tests/onboarding_overlay.test.js.
    """
    source = _read(module_name)

    assert "escapeHtmlAttr as escapeHtml" in source, (
        f"{module_name} must import the shared escape helper from utils.js"
    )
    assert "function escapeHtml(" not in source, (
        f"{module_name} redefines escapeHtml instead of using the shared helper"
    )


def test_onboarding_wizard_uses_the_shared_gateway_fetch_helper():
    """Same dedup, same reason: the wizard's private ``apiRequest`` was a second
    copy of ``api_client.fetchJson`` (identical throw-on-!ok, identical error
    message), and a second copy is a second place for the gateway error contract
    to drift."""
    source = _read("onboarding_wizard.js")

    assert "import { fetchJson } from './api_client.js';" in source
    assert "async function apiRequest(" not in source
