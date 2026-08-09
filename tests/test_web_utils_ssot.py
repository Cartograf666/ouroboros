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


def test_onboarding_wizard_remains_inline_iife_without_imports():
    """The onboarding wizard is inlined into a classic script, not loaded as an ES module."""
    src = _read("onboarding_wizard.js")
    assert src.startswith("(() => {")
    assert "\nimport " not in src


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


def test_onboarding_escape_mirrors_utils():
    """``onboarding_wizard.js`` is an IIFE bundle and cannot ``import`` from
    ``utils.js`` without a bootstrap rewrite. Its local ``escapeHtml`` MUST
    therefore mirror the same chain of HTML-escape ``replace`` calls
    ``escapeHtmlAttr`` performs so the security contract (replace ``&``
    first, then the five HTML metas, then backtick) cannot drift between
    the wizard and the SPA. We compare on the normalized list of
    ``.replace(<from>, <to>)`` calls — indentation differs (IIFE has one
    extra wrap level) but the actual escape sequence must be identical.
    """
    import re
    onboarding = _read("onboarding_wizard.js")
    utils = _read("utils.js")
    wizard_body = onboarding.split("function escapeHtml(value) {", 1)[1].split("}", 1)[0]
    utils_body = utils.split("export function escapeHtmlAttr(value) {", 1)[1].split("}", 1)[0]
    pattern = re.compile(r"\.replace\(([^)]+)\)")
    wizard_replaces = pattern.findall(wizard_body)
    utils_replaces = pattern.findall(utils_body)
    assert wizard_replaces == utils_replaces, (
        "onboarding_wizard.escapeHtml drifted from utils.escapeHtmlAttr "
        f"— wizard chain: {wizard_replaces}, utils chain: {utils_replaces}"
    )
    # Smoke-check that the chain still escapes ampersand FIRST (any other
    # order would double-encode the entity numbers later in the chain).
    assert wizard_replaces[0].startswith("/&/g"), (
        "ampersand replacement must be first in the chain"
    )


def test_onboarding_account_truth_mirrors_the_panel_rules():
    """The wizard is an IIFE and cannot import, so it restates TWO rules the
    accounts panel owns. Both are the owner-visible bug from 2026-08-08 —
    "No accounts connected yet" on an idle machine, and a native codex login
    counted as zero — so a silent revert of either mirror must go RED here.

    This is a STRUCTURAL pin, not a behavioural one: the wizard is an IIFE
    inside a page module, so it cannot be imported and executed here. Each rule
    is extracted from both sources and compared clause by clause, including the
    ORDER of the failed-closed check against the legacy fallback — a rewrite
    that keeps every clause but changes the meaning can still slip through.
    """
    import re

    wizard = _read("onboarding_wizard.js")
    panel = _read("harness_accounts.js")

    # 1. The read-state rule: three states plus the legacy fallback. The wizard
    #    must key on `reads.accounts`, must NOT collapse it to a boolean, and
    #    must fall back to daemon.state for a payload that predates the block.
    body = wizard.split("function claudexorAccountsRead(payload) {", 1)[1].split("\n    }", 1)[0]
    assert re.search(r"reads\??\.accounts", body), (
        "the wizard must read the per-facet read state"
    )
    assert "'not_read'" in body and "'failed'" in body, (
        "the wizard collapsed the three read states into a boolean; a refused read "
        "would then be announced as 'the daemon is not running'"
    )
    assert "=== 'running'" in body, "the legacy fallback (older backend) is missing"
    # The panel's own reader declares the same vocabulary — if it ever gains a
    # fourth state the mirror has to learn it too.
    panel_reader = panel.split("export function facetReadState(", 1)[1].split("\n}", 1)[0]
    for state in ("'ok'", "'not_read'", "'failed'"):
        assert state in panel_reader and state in body, f"read state {state} drifted"
    # A read block that IS present but carries a value neither side knows must
    # fail closed, and it must do so BEFORE the legacy daemon-state fallback —
    # otherwise an unknown value falls through to "running => ok" and the false
    # absence is back on the surface where the owner first saw it.
    for where, src in (("wizard", body), ("panel", panel_reader)):
        assert "return 'failed'" in src, (
            f"{where}: an unknown facet value is not failed closed"
        )
        assert src.index("return 'failed'") < src.index("=== 'running'"), (
            f"{where}: the legacy fallback is reached before the unknown-value "
            "check, so an unknown read state becomes authoritative again"
        )

    # The REASON the wizard gives for an unread facet must not contradict the
    # daemon it just read. `not_read` normally means the daemon never ran, but a
    # discovery/handshake failure before the fan-out leaves every facet untouched
    # while the daemon reports `unreachable` — blaming a running daemon there is
    # a second false statement, which is what the panel's own note already
    # avoids (`unknownAccountsNote`).
    def code_only(src: str) -> str:
        """Drop // comments — a rule that lives only in prose is not a rule, and
        a pin that reads prose passes on a revert that kept the comment."""
        return "\n".join(
            line for line in src.splitlines() if not line.strip().startswith("//")
        )

    line_body = code_only(
        wizard.split("async function refreshHarnessAccountsLine", 1)[1].split("\n    }", 1)[0]
    )
    assert "unreachable" in line_body, (
        "the wizard blames a non-running daemon even when the payload says the "
        "daemon answered nothing because it was unreachable"
    )
    panel_note = code_only(
        panel.split("export function unknownAccountsNote", 1)[1].split("\n}", 1)[0]
    )
    assert "unreachable" in panel_note, (
        "the panel's own note lost the distinction the wizard mirrors"
    )

    # 2. The connected-row rule: a NATIVE login counts (the wizard used to count
    #    only named profiles, so a native codex session read as "0 connected"),
    #    and a named profile counts only when its verification passed.
    count_body = wizard.split("function countConnectedAccounts(payload) {", 1)[1].split("\n    }", 1)[0]
    assert "harnessAccounts" in count_body, (
        "native logins are not counted; a native-only machine reads as 0 accounts"
    )
    assert "native_login_detected" in count_body
    assert re.search(r"verification[^\n]*'passed'", count_body), (
        "a signed-out named profile would be counted as connected"
    )
    # The panel derives native rows from the same field, with the same meaning.
    assert "native_login_detected" in panel


def test_reviewer_rows_label_each_pin_from_its_own_facet():
    """The read states are per-facet and INDEPENDENT — the backend pins that
    explicitly (`test_status_payload_classifies_each_fanned_out_facet_independently`).
    The row builders therefore have to label a saved ACCOUNT pin from the
    accounts read and a saved MODEL from the catalog read. Threading the wrong
    facet is invisible in the helper tests (they take the state as a parameter),
    and it re-creates the owner-visible lie in miniature: a profile nobody read
    gets told it is "not in discovery".

    Structural pin: each call site in the module source is inspected.
    """
    import re

    src = _read("reviewer_slots.js")
    def call_sites(name):
        """Every call of `name` with its full argument list, definitions aside."""
        found = []
        for match in re.finditer(re.escape(name) + r"\(", src):
            if src[:match.start()].rstrip().endswith("function"):
                continue
            depth, i = 0, match.end() - 1
            while i < len(src):
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            found.append(src[match.start():i + 1])
        return found

    profile_calls = call_sites("profileOptionsFor")
    # Two builders (the triad/scope row and the advisory row) plus nothing else.
    assert len(profile_calls) >= 2, "the profile-option call sites moved"
    for call in profile_calls:
        assert "accountsRead: state.accountsRead" in call, (
            f"a saved account pin is labelled from the wrong read facet: {call!r}"
        )
        assert "state.catalogRead" not in call, (
            "the catalog read is standing in for the accounts read; a failed "
            f"accounts read would then be announced as discovery truth: {call!r}"
        )
    model_calls = call_sites("sessionModelOptions")
    assert len(model_calls) >= 2, "the model-option call sites moved"
    for call in model_calls:
        assert "modelsRead" in call, (
            "a saved model is labelled without the catalog read state, so an "
            f"unread catalog says 'not in discovery' about it: {call!r}"
        )
        # ...and from the CATALOG read specifically. Asserting only that the
        # option exists lets the facets be swapped — the very defect this test
        # was written for, one field over.
        assert "catalogRead" in call, (
            "a saved model is labelled from a facet that does not stamp the "
            f"model list; models come from the catalog: {call!r}"
        )
        assert "accountsRead" not in call, (
            f"the accounts read is standing in for the catalog read: {call!r}"
        )
    # An unread facet must never default to the authoritative state on the way
    # in. (The helpers keep `'ok'` as their own parameter default, which is the
    # discovery-truth case they were written for and is pinned separately.)
    assert not re.search(r"state\.\w*Read \|\| 'ok'", src), (
        "a missing read state falls back to 'ok'; unknown must never be "
        "authoritative — that is the original bug's shape"
    )
