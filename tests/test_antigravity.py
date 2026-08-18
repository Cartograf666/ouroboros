from __future__ import annotations

import pathlib

from ouroboros.tools import antigravity
from ouroboros.tools.registry import ToolContext, ToolRegistry


def test_antigravity_cli_resolution_prefers_executable(monkeypatch, tmp_path):
    binary = tmp_path / "agy"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(antigravity, "_CLI_CANDIDATES", (str(binary),))
    assert antigravity.find_antigravity_cli() == str(binary)


def test_antigravity_tool_is_registered_and_core_visible(tmp_path):
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    assert "antigravity_ask" in registry.available_tools()
    assert "antigravity_ask" in {
        schema["function"]["name"] for schema in registry.schemas(core_only=True)
    }


def test_antigravity_ask_uses_official_readonly_flags(monkeypatch, tmp_path):
    class FakeProcess:
        pid = 123
        returncode = 0

        def communicate(self, timeout=None):
            return "answer", ""

        def terminate(self):
            pass

    calls = []
    monkeypatch.setattr(antigravity, "find_antigravity_cli", lambda: "/usr/local/bin/agy")
    monkeypatch.setattr(
        antigravity.subprocess,
        "Popen",
        lambda command, **kwargs: (calls.append((command, kwargs)) or FakeProcess()),
    )
    ctx = ToolContext(repo_dir=pathlib.Path(tmp_path), drive_root=pathlib.Path(tmp_path))
    result = antigravity._ask_antigravity(
        ctx,
        "review this plan",
        model="claude-sonnet-4-6",
        effort="high",
        timeout_seconds=30,
    )
    assert result.startswith("Antigravity (official OAuth CLI, read-only plan mode):")
    command, kwargs = calls[0]
    assert command[:3] == ["/usr/local/bin/agy", "--output-format", "text"]
    assert "--mode" in command and command[command.index("--mode") + 1] == "plan"
    assert "--sandbox" in command
    assert "--disable-slash-commands" in command
    assert command[command.index("--model") + 1] == "claude-sonnet-4-6"
    assert command[command.index("--effort") + 1] == "high"
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["start_new_session"] is True
