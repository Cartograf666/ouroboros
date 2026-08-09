"""Static guard: the Agents tab exists, and every control it absorbed moved
ONCE.

D-10 gave the agent surfaces their own Settings tab because they were scattered
across three others: accounts sat in Providers between API-key cards, reviewer
slots and subagent routing sat in Models beside model-id fields, and the two
counts that bound subagents (children per root, nesting depth) sat in Advanced →
Runtime Limits next to process worker counts they have nothing to do with.

The failure this file exists to prevent is not "a control is missing" — that one
is loud. It is the SILENT one DEVELOPMENT.md names: "One capability, one
section. Never render a second control over the same settings key — two controls
carry two drafts, and the last one collected wins." A move that leaves the old
markup behind produces two inputs with the same id, and `settings.js` collects
whichever `getElementById` happens to reach first.

So each moved id is asserted to appear EXACTLY ONCE across the whole rendered
settings page, and to still be mapped to its settings key.

Pattern follows tests/test_web_dialogs_static.py and
tests/test_web_typography_static.py: read the sources, assert the structural
fact, no browser needed.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULES = REPO_ROOT / "web" / "modules"

# The panels that together produce the settings page markup. `settings_ui.js`
# renders the shell and inlines the three section renderers.
PAGE_SOURCES = (
    "settings_ui.js",
    "harness_accounts.js",
    "reviewer_slots.js",
    "subagents_settings.js",
)

# Every control the Agents tab absorbed, with the settings key it round-trips
# to. The key mapping lives in settings.js (id → key tables); the id lives in
# whichever module renders it now.
MOVED_CONTROLS = {
    "s-allow-mutative-subagents": "OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS",
    "s-active-subagents": "OUROBOROS_MAX_ACTIVE_SUBAGENTS_PER_ROOT",
    "s-subagent-depth": "OUROBOROS_MAX_SUBAGENT_DEPTH",
    "s-subagent-worktree-root": "OUROBOROS_SUBAGENT_WORKTREE_ROOT",
    "s-subagent-projects-root": "OUROBOROS_SUBAGENT_PROJECTS_ROOT",
}

# Deliberately NOT moved: worker processes are runtime capacity, not an agent
# setting, and GC retention governs every disposable artifact.
STAYED_IN_ADVANCED = ("s-workers", "s-gc-retention-days")


def _read(name: str) -> str:
    return (MODULES / name).read_text(encoding="utf-8")


def _page_markup() -> str:
    return "\n".join(_read(name) for name in PAGE_SOURCES)


def _panel(name: str) -> str:
    """The markup of one settings panel, by its data-settings-panel value."""
    css = _read("settings_ui.js")
    start = css.index(f'data-settings-panel="{name}"')
    end = css.find('<section class="settings-panel', start)
    return css[start:end if end != -1 else len(css)]


# ---------------------------------------------------------------------------
# The tab
# ---------------------------------------------------------------------------


def test_agents_tab_sits_between_models_and_behavior() -> None:
    """Reads as a sequence: keys → secrets → which API models → who among the
    agents does what → behavior → technical."""
    source = _read("settings_ui.js")
    start = source.index("const SETTINGS_TABS = [")
    strip = source[start:source.index("];", start)]
    labels = re.findall(r"\{ value: '([a-z]+)', label: '([A-Za-z]+)' \}", strip)
    values = [value for value, _ in labels]
    assert values == [
        "providers", "secrets", "models", "agents", "behavior", "advanced", "about",
    ], f"unexpected settings tab order: {values}"
    assert ("agents", "Agents") in labels, (
        "the tab is named 'Agents', not 'Coding agents' (D-10, owner verbatim: "
        "«эти агенты не только для кода используются»)"
    )
    assert '<section class="settings-panel" data-settings-panel="agents">' in source


def test_the_agents_panel_carries_one_banner_and_the_three_sections() -> None:
    panel = _panel("agents")
    for fragment in (
        "renderAgentsServiceBanner()",
        "renderAgentAccountsSection()",
        "renderReviewerSlotsSection()",
        "renderSubagentsSection()",
    ):
        assert fragment in panel, f"the Agents panel does not render {fragment}"
    # Order is the specification's: service banner, accounts, review lanes,
    # delegation.
    order = [panel.index(f) for f in (
        "renderAgentsServiceBanner()", "renderAgentAccountsSection()",
        "renderReviewerSlotsSection()", "renderSubagentsSection()",
    )]
    assert order == sorted(order), "the Agents panel sections are out of order"
    # Exactly ONE service banner element on the whole page.
    assert _page_markup().count('id="agents-service-banner"') == 1


def test_the_vacated_tabs_no_longer_render_the_moved_sections() -> None:
    source = _read("settings_ui.js")
    for renderer in ("renderAgentAccountsSection()", "renderReviewerSlotsSection()",
                     "renderSubagentsSection()"):
        assert source.count(renderer) == 1, (
            f"{renderer} is rendered more than once — a section mounted in two "
            "panels would carry two drafts of the same settings keys"
        )
    assert "renderAgentAccountsSection()" not in _panel("providers")
    models = _panel("models")
    assert "renderReviewerSlotsSection()" not in models
    assert "renderSubagentsSection()" not in models


# ---------------------------------------------------------------------------
# The moved controls
# ---------------------------------------------------------------------------


def test_every_moved_control_appears_exactly_once() -> None:
    markup = _page_markup()
    for control_id in MOVED_CONTROLS:
        found = re.findall(rf'id="{re.escape(control_id)}"', markup)
        assert len(found) == 1, (
            f"#{control_id} is rendered {len(found)} times. DEVELOPMENT.md: "
            "'One capability, one section… two controls carry two drafts, and "
            "the last one collected wins.'"
        )


def test_every_moved_control_now_lives_in_the_delegation_section() -> None:
    delegation = _read("subagents_settings.js")
    for control_id in MOVED_CONTROLS:
        assert f'id="{control_id}"' in delegation, (
            f"#{control_id} was moved out of its old panel but did not land in "
            "Agents → Delegation"
        )
    advanced = _panel("advanced")
    for control_id in MOVED_CONTROLS:
        assert f'id="{control_id}"' not in advanced


def test_every_moved_control_still_round_trips_to_its_settings_key() -> None:
    """Moving markup must not detach a control from its key: settings.js maps
    id → key, so the pair has to survive the move intact."""
    settings_js = _read("settings.js")
    for control_id, key in MOVED_CONTROLS.items():
        if control_id == "s-allow-mutative-subagents":
            # This one is not in a table: it is collected explicitly because
            # Auto saves the EMPTY value (SC-4).
            assert f"byId('{control_id}')" in settings_js
            assert key in settings_js
            continue
        assert re.search(rf"\['{re.escape(control_id)}', '{re.escape(key)}'", settings_js), (
            f"#{control_id} no longer maps to {key} in settings.js"
        )


def test_process_capacity_stayed_in_advanced() -> None:
    """Max Workers is runtime worker processes, not an agent setting; GC
    retention governs every disposable artifact, not just subagent worktrees."""
    advanced = _panel("advanced")
    for control_id in STAYED_IN_ADVANCED:
        assert f'id="{control_id}"' in advanced
    assert 's-workers' not in _read("subagents_settings.js")


def test_the_two_path_roots_are_behind_a_collapsed_disclosure() -> None:
    """Owner-facing scannability: two paths nobody edits in a normal week must
    not push the delegation controls below the fold."""
    delegation = _read("subagents_settings.js")
    start = delegation.index('<details class="settings-subsection"')
    end = delegation.index("</details>", start)
    disclosure = delegation[start:end]
    assert 'id="s-subagent-worktree-root"' in disclosure
    assert 'id="s-subagent-projects-root"' in disclosure
    assert " open" not in delegation[start:delegation.index(">", start)], (
        "the Advanced disclosure ships collapsed"
    )


# ---------------------------------------------------------------------------
# The naming sweep
# ---------------------------------------------------------------------------


def test_no_owner_facing_coding_agent_copy_remains_on_the_agents_surfaces() -> None:
    """D-10, owner verbatim: «мне не нравится "coding" потому что эти агенты не
    только для кода используются, например для создания презентаций и любые
    tasks». Product names (Claude Code, Codex, Cursor) are unaffected — they are
    trademarks, not the generic label.

    Scoped to OWNER-FACING copy: comments are skipped, because a comment that
    explains why the word was retired has to be able to spell it."""
    offenders: list[str] = []
    sources = [(f"web/modules/{n}", _read(n), "//") for n in PAGE_SOURCES]
    sources.append(("web/modules/claudexor_status_store.js",
                    _read("claudexor_status_store.js"), "//"))
    sources.append(("ouroboros/reviewer_slot_config.py",
                    (REPO_ROOT / "ouroboros" / "reviewer_slot_config.py")
                    .read_text(encoding="utf-8"), "#"))
    for label, text, comment in sources:
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith(comment):
                continue
            if re.search(r"coding[- ]agent", line, re.IGNORECASE):
                offenders.append(f"{label}:{lineno}: {line.strip()}")
    assert not offenders, (
        "owner-facing copy still says \"coding agent\" (D-10)\n" + "\n".join(offenders))


def test_a_spent_window_is_emphasised_rather_than_dimmed() -> None:
    """Caught by looking at the rendered tab: `.harness-exhausted` dimmed the whole
    row to 0.62 opacity, which greyed out the very sentence reporting the limit
    and made its still-clickable Remove/Sign-in buttons read as disabled
    (docs/DESIGN.md: "a control rendered at secondary ink reads as disabled")."""
    css = (REPO_ROOT / "web" / "style.css").read_text(encoding="utf-8")
    start = css.index("design-system:migrated-begin")
    end = css.index("design-system:migrated-end", start)
    region = css[start:end]
    rule = re.search(r"\.harness-exhausted\s*\{([^}]*)\}", region)
    assert rule, ".harness-exhausted missing from the migrated region"
    assert "opacity" not in rule.group(1), (
        "a spent window is emphasis, not de-emphasis — dimming hides the limit "
        "sentence and disables-looking buttons that still work"
    )
    assert "var(--status-warn-" in rule.group(1)


def test_the_reviewer_disclosure_stopped_advising_against_the_default() -> None:
    """The sentence used to end "keep at least one API reviewer row to avoid the
    fallback", which asks the owner to undo the ratified default (D-3: with a
    subscription connected, everything that can run on one does, and a triad is
    never half API and half subscription). It is a routing DISCLOSURE now."""
    config = (REPO_ROOT / "ouroboros" / "reviewer_slot_config.py").read_text(encoding="utf-8")
    assert "Keep at least one API reviewer row" not in config
    assert "never fall back" in config
    lanes = _read("reviewer_slots.js")
    assert "keep at least one API row" not in lanes
    assert "never fall back to API spend" in lanes
