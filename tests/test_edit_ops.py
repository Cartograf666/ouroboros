"""Tests for ouroboros.tools.edit_ops (apply_patch / edit_batch)."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ouroboros.tools import edit_ops
from ouroboros.tools.edit_ops import (
    _apply_hunks_to_text,
    _find_sequence,
    _parse_patch,
    _syntax_check,
)


SAMPLE = "\n".join([
    "def ddd(x):",
    "    return x * 3",
    "",
    "",
    "def other(x):",
    "    return ddd(x)",
    "",
    "def caller():",
    "    return ddd(1) + ddd(2)",
    "",
])


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def test_parse_patch_update_add_delete():
    ops, err = _parse_patch(
        "*** Begin Patch\n"
        "*** Update File: a.py\n"
        "@@ def ddd\n"
        " def ddd(x):\n"
        "-    return x * 3\n"
        "+    return x * 4\n"
        "*** Add File: b.py\n"
        "+print('hi')\n"
        "*** Delete File: c.py\n"
        "*** End Patch\n"
    )
    assert err == ""
    assert [op.kind for op in ops] == ["update", "add", "delete"]
    assert ops[0].path == "a.py"
    assert ops[0].hunks[0].anchor == "def ddd"
    assert ops[1].add_lines == ["print('hi')"]


def test_parse_patch_tolerates_decorative_asterisks():
    ops, err = _parse_patch(
        "*** Begin Patch ***\n"
        "*** Update File: a.py ***\n"
        "-old\n"
        "+new\n"
        "*** End Patch ***\n"
    )
    assert err == ""
    assert ops[0].path == "a.py"


def test_parse_patch_envelope_optional():
    ops, err = _parse_patch(
        "*** Update File: a.py\n"
        " context\n"
        "-old\n"
        "+new\n"
    )
    assert err == ""
    assert len(ops) == 1


def test_parse_patch_rejects_stray_content():
    _, err = _parse_patch("hello\n*** Update File: a.py\n-x\n+y\n")
    assert "before the first file header" in err


def test_parse_patch_rejects_bad_add_body():
    _, err = _parse_patch("*** Add File: a.py\nno-plus-prefix\n")
    assert "must start with '+'" in err


def test_parse_patch_rejects_empty():
    _, err = _parse_patch("*** Begin Patch\n*** End Patch\n")
    assert "no file operations" in err


# ---------------------------------------------------------------------------
# hunk matching / application
# ---------------------------------------------------------------------------

def test_apply_single_hunk():
    ops, err = _parse_patch(
        "*** Update File: s.py\n"
        " def ddd(x):\n"
        "-    return x * 3\n"
        "+    return x * 30\n"
    )
    assert err == ""
    new, notes, herr = _apply_hunks_to_text(SAMPLE, ops[0].hunks, "s.py")
    assert herr == ""
    assert "x * 30" in new
    assert notes == []


def test_ambiguous_context_errors():
    content = "a\nb\na\nb\n"
    ops, _ = _parse_patch("*** Update File: s.py\n a\n-b\n+B\n")
    new, _, herr = _apply_hunks_to_text(content, ops[0].hunks, "s.py")
    assert new is None
    assert "ambiguous" in herr


def test_anchor_disambiguates():
    content = "def one():\n    x = 1\n\ndef two():\n    x = 1\n"
    ops, _ = _parse_patch(
        "*** Update File: s.py\n"
        "@@ def two\n"
        "-    x = 1\n"
        "+    x = 2\n"
    )
    new, _, herr = _apply_hunks_to_text(content, ops[0].hunks, "s.py")
    assert herr == ""
    assert new == "def one():\n    x = 1\n\ndef two():\n    x = 2\n"


def test_context_not_found_reports_lines():
    ops, _ = _parse_patch("*** Update File: s.py\n-does not exist\n+x\n")
    new, _, herr = _apply_hunks_to_text(SAMPLE, ops[0].hunks, "s.py")
    assert new is None
    assert "context not found" in herr


def test_fuzzy_trailing_whitespace_match():
    content = "line one   \nline two\n"
    ops, _ = _parse_patch("*** Update File: s.py\n-line one\n+line ONE\n")
    new, notes, herr = _apply_hunks_to_text(content, ops[0].hunks, "s.py")
    assert herr == ""
    assert new.startswith("line ONE")
    assert any("whitespace" in n for n in notes)


def test_pure_insertion_requires_anchor():
    ops, _ = _parse_patch("*** Update File: s.py\n+new line\n")
    new, _, herr = _apply_hunks_to_text(SAMPLE, ops[0].hunks, "s.py")
    assert new is None
    assert "anchor" in herr


def test_pure_insertion_with_anchor():
    ops, _ = _parse_patch("*** Update File: s.py\n@@ def other\n+    # inserted\n")
    new, _, herr = _apply_hunks_to_text(SAMPLE, ops[0].hunks, "s.py")
    assert herr == ""
    assert "def other(x):\n    # inserted\n    return ddd(x)" in new


def test_sequential_hunks_advance_cursor():
    ops, _ = _parse_patch(
        "*** Update File: s.py\n"
        "-    return ddd(x)\n"
        "+    return aaa(x)\n"
        "@@ def caller\n"
        "-    return ddd(1) + ddd(2)\n"
        "+    return aaa(1) + aaa(2)\n"
    )
    new, _, herr = _apply_hunks_to_text(SAMPLE, ops[0].hunks, "s.py")
    assert herr == ""
    assert "aaa(x)" in new and "aaa(1) + aaa(2)" in new
    assert "return ddd" not in new
    assert "def ddd(x):" in new  # the def line was not part of either hunk


def test_find_sequence_caps_matches():
    lines = ["x"] * 20
    assert len(_find_sequence(lines, ["x"], 0, fuzzy=False)) == 5


# ---------------------------------------------------------------------------
# shared verification helpers
# ---------------------------------------------------------------------------

def test_syntax_check():
    assert _syntax_check("x.py", "def f(:\n") != ""
    assert _syntax_check("x.py", "def f():\n    return 1\n") == ""
    assert _syntax_check("x.json", "{bad") != ""
    assert _syntax_check("x.json", '{"ok": 1}') == ""
    assert _syntax_check("x.txt", "anything") == ""


# ---------------------------------------------------------------------------
# end-to-end handler tests on a fake workspace ctx
# ---------------------------------------------------------------------------

class _FakeCtx:
    def __init__(self, repo: pathlib.Path):
        self._repo = repo
        self.repo_dir = repo
        self.drive_root = repo / ".drive"
        self.task_metadata = {}
        self.event_queue = None
        self.pending_events = []
        self.task_id = "test-task"

    def is_workspace_mode(self):
        return True

    def repo_path(self, rel):
        p = (self._repo / rel).resolve()
        if not str(p).startswith(str(self._repo.resolve())):
            raise ValueError(f"path escapes workspace: {rel}")
        return p


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    repo = tmp_path / "ws"
    repo.mkdir()
    (repo / "s.py").write_text(SAMPLE, encoding="utf-8")
    ctx = _FakeCtx(repo)
    # Route guard helpers around ToolContext specifics: keep the real access
    # logic out of scope — these tests exercise edit mechanics.
    monkeypatch.setattr(edit_ops, "_resolve_edit_target", _fake_resolver(ctx))
    monkeypatch.setattr(edit_ops, "_finish_mutation", lambda ctx_, paths, tool: "NOT committed.")
    return ctx


def _fake_resolver(ctx):
    def resolver(_ctx, path, _root, *, error_tag):
        if not path:
            return None, f"⚠️ {error_tag}: path is required."
        try:
            return ctx.repo_path(path), ""
        except ValueError as e:
            return None, f"⚠️ PATH_ERROR: {e}"
    return resolver


def test_apply_patch_end_to_end(ws):
    result = edit_ops._apply_patch(
        ws,
        "*** Begin Patch\n"
        "*** Update File: s.py\n"
        "-def ddd(x):\n"
        "+def aaa(x):\n"
        "@@ def other\n"
        "-    return ddd(x)\n"
        "+    return aaa(x)\n"
        "*** Add File: extra.py\n"
        "+VALUE = 1\n"
        "*** End Patch\n",
    )
    assert result.startswith("✅")
    text = (ws.repo_dir / "s.py").read_text()
    assert "def aaa(x):" in text and "return aaa(x)" in text
    assert (ws.repo_dir / "extra.py").read_text() == "VALUE = 1\n"


def test_apply_patch_atomic_on_bad_hunk(ws):
    before = (ws.repo_dir / "s.py").read_text()
    result = edit_ops._apply_patch(
        ws,
        "*** Update File: s.py\n"
        "-def ddd(x):\n"
        "+def aaa(x):\n"
        "@@ def nowhere\n"
        "-missing\n"
        "+present\n",
    )
    assert "APPLY_PATCH_ERROR" in result
    assert (ws.repo_dir / "s.py").read_text() == before


def test_apply_patch_add_existing_fails(ws):
    result = edit_ops._apply_patch(ws, "*** Add File: s.py\n+x\n")
    assert "already exists" in result


def test_apply_patch_delete(ws):
    (ws.repo_dir / "gone.py").write_text("x = 1\n")
    result = edit_ops._apply_patch(ws, "*** Delete File: gone.py\n")
    assert result.startswith("✅")
    assert not (ws.repo_dir / "gone.py").exists()


def test_edit_batch_counted_replace(ws):
    result = edit_ops._edit_batch(
        ws,
        [
            {"path": "s.py", "old_str": "ddd(", "new_str": "aaa(", "count": 4},
        ],
    )
    assert result.startswith("✅")
    text = (ws.repo_dir / "s.py").read_text()
    assert "ddd(" not in text
    assert text.count("aaa(") == 4


def test_edit_batch_count_mismatch_is_atomic(ws):
    before = (ws.repo_dir / "s.py").read_text()
    result = edit_ops._edit_batch(
        ws,
        [
            {"path": "s.py", "old_str": "def other", "new_str": "def another", "count": 1},
            {"path": "s.py", "old_str": "ddd(", "new_str": "aaa(", "count": 2},  # actually 4
        ],
    )
    assert "EDIT_BATCH_ERROR" in result
    assert "occurs 4 time(s), expected 2" in result
    assert (ws.repo_dir / "s.py").read_text() == before


def test_edit_batch_sequential_edits_see_prior_results(ws):
    result = edit_ops._edit_batch(
        ws,
        [
            {"path": "s.py", "old_str": "def ddd(x):", "new_str": "def aaa(x):", "count": 1},
            {"path": "s.py", "old_str": "def aaa(x):", "new_str": "def aaa(value):", "count": 1},
        ],
    )
    assert result.startswith("✅")
    assert "def aaa(value):" in (ws.repo_dir / "s.py").read_text()


def test_registry_registration():
    names = {e.name for e in edit_ops.get_tools()}
    assert names == {"apply_patch", "edit_batch"}
    for entry in edit_ops.get_tools():
        assert entry.is_code_tool
        assert entry.mutates_worktree


# ---------------------------------------------------------------------------
# write_file rails (git._repo_write): syntax guard + overwrite diff
# ---------------------------------------------------------------------------

def _ws_ctx(tmp_path):
    import subprocess

    from ouroboros.tools.registry import ToolContext

    ws = tmp_path / "extws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    (ws / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "seed"], cwd=ws, check=True)
    drive = tmp_path / "drive"
    drive.mkdir()
    return ToolContext(repo_dir=tmp_path / "repo", drive_root=drive,
                       workspace_root=str(ws), workspace_mode="external"), ws


def test_repo_write_blocks_broken_python(tmp_path):
    from ouroboros.tools.git import _repo_write

    ctx, ws = _ws_ctx(tmp_path)
    before = (ws / "mod.py").read_text()
    out = _repo_write(ctx, path="mod.py", content="def f(:\n    broken\n")
    assert "WRITE_BLOCKED_SYNTAX" in out
    assert (ws / "mod.py").read_text() == before


def test_repo_write_force_bypasses_syntax_guard(tmp_path):
    from ouroboros.tools.git import _repo_write

    ctx, ws = _ws_ctx(tmp_path)
    out = _repo_write(ctx, path="broken_fixture.py", content="def f(:\n", force=True)
    assert out.startswith("✅")
    assert (ws / "broken_fixture.py").exists()


def test_repo_write_overwrite_appends_diff(tmp_path):
    from ouroboros.tools.git import _repo_write

    ctx, ws = _ws_ctx(tmp_path)
    out = _repo_write(ctx, path="mod.py", content="def f():\n    return 2\n")
    assert out.startswith("✅")
    assert "Diff vs the previous version" in out
    assert "-    return 1" in out and "+    return 2" in out


def test_repo_write_new_file_has_no_diff_section(tmp_path):
    from ouroboros.tools.git import _repo_write

    ctx, ws = _ws_ctx(tmp_path)
    out = _repo_write(ctx, path="fresh.py", content="X = 1\n")
    assert out.startswith("✅")
    assert "Diff vs the previous version" not in out


# ---------------------------------------------------------------------------
# governance rails: envelopes, advisory staleness (P3), force disclosure
# ---------------------------------------------------------------------------

def test_capability_envelopes_pin_new_tools():
    # Write-capable lanes see the tools; the read-only subagent lane and the
    # heal-mode allowlist must NOT (P3: the read-only lane stays write-free,
    # and heal mode edits skill payloads, which these tools refuse).
    from ouroboros.tool_capabilities import (
        ACTING_SUBAGENT_TOOL_NAMES,
        CORE_TOOL_NAMES,
        LOCAL_READONLY_SUBAGENT_TOOL_NAMES,
    )
    from ouroboros.tools.registry import _HEAL_MODE_ALLOWED_TOOLS, _WORKSPACE_ALLOWED_TOOLS

    for name in ("apply_patch", "edit_batch"):
        assert name in CORE_TOOL_NAMES
        assert name in ACTING_SUBAGENT_TOOL_NAMES
        assert name in _WORKSPACE_ALLOWED_TOOLS
        assert name not in LOCAL_READONLY_SUBAGENT_TOOL_NAMES
        assert name not in _HEAL_MODE_ALLOWED_TOOLS


def test_tool_policy_and_smoke_registration():
    from ouroboros.safety import TOOL_POLICY

    assert TOOL_POLICY["apply_patch"] == TOOL_POLICY["edit_text"]
    assert TOOL_POLICY["edit_batch"] == TOOL_POLICY["edit_text"]


def test_mutations_invalidate_advisory(tmp_path, monkeypatch):
    # P3: every worktree-mutating tool must mark the advisory snapshot stale.
    calls = []
    from ouroboros.tools import commit_gate

    monkeypatch.setattr(
        commit_gate, "_invalidate_advisory",
        lambda ctx, **kw: calls.append(kw.get("source_tool")),
    )
    repo = tmp_path / "ws"
    repo.mkdir()
    (repo / "s.py").write_text(SAMPLE, encoding="utf-8")
    ctx = _FakeCtx(repo)
    monkeypatch.setattr(edit_ops, "_resolve_edit_target", _fake_resolver(ctx))
    out = edit_ops._apply_patch(ctx, "*** Update File: s.py\n-def ddd(x):\n+def aaa(x):\n")
    assert out.startswith("✅")
    out = edit_ops._edit_batch(ctx, [{"path": "s.py", "old_str": "aaa", "new_str": "bbb", "count": 1}])
    assert out.startswith("✅")
    assert calls == ["apply_patch", "edit_batch"]


def test_repo_write_force_bypass_is_disclosed(tmp_path):
    # P3: silent bypass is forbidden — a forced write of invalid content names it.
    from ouroboros.tools.git import _repo_write

    ctx, ws = _ws_ctx(tmp_path)
    out = _repo_write(ctx, path="fixture_broken.py", content="def f(:\n", force=True)
    assert out.startswith("✅")
    assert "SYNTAX_GUARD_BYPASSED" in out
