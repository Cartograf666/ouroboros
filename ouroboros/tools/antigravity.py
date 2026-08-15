"""Official Antigravity CLI consultation tool.

This is deliberately a *delegated consultation* surface, not an OpenAI-compatible
provider.  ``agy`` owns the Google OAuth session in the operator's account/keyring;
Ouroboros only starts the documented CLI in the active workspace and returns its
bounded answer.  Keeping the transport at this boundary means we do not copy OAuth
tokens into settings, environment variables, Telegram, or the Claudexor daemon.

The first version is read-only by contract (``--mode plan --sandbox``).  The CLI can
therefore be used for a second opinion, code review, prompt refinement, or planning
without silently mutating the live repository.  A future full delegated-run adapter
can build on this seam once durable start/wait/cancel custody is defined for the CLI.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import signal
import subprocess
from typing import Any, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry, active_repo_dir_for
from ouroboros.utils import truncate_within_limit


_CLI_CANDIDATES = (
    "agy",
    "/Users/alex/.local/bin/agy",
    "~/.local/bin/agy",
)
_DEFAULT_TIMEOUT_SEC = 300
_MAX_TIMEOUT_SEC = 900
_MAX_PROMPT_CHARS = 20_000
_MAX_RESULT_CHARS = 30_000


def find_antigravity_cli() -> Optional[str]:
    """Resolve the official ``agy`` executable without invoking a shell."""

    for candidate in _CLI_CANDIDATES:
        expanded = os.path.expanduser(candidate)
        if os.path.isabs(expanded):
            path = pathlib.Path(expanded)
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
            continue
        resolved = shutil.which(expanded)
        if resolved:
            return resolved
    return None


def _int_bound(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(15, min(parsed, maximum))


def _clean_output(stdout: str, stderr: str) -> str:
    answer = str(stdout or "").strip()
    if answer:
        return truncate_within_limit(answer, _MAX_RESULT_CHARS)
    detail = str(stderr or "").strip()
    if detail:
        return truncate_within_limit(detail, _MAX_RESULT_CHARS)
    return "(Antigravity не вернул текстовый ответ.)"


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Best-effort cleanup for the CLI and its language-server child."""

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            proc.terminate()
        except (OSError, ProcessLookupError):
            pass


def _ask_antigravity(
    ctx: ToolContext,
    prompt: str,
    model: str = "",
    effort: str = "",
    timeout_seconds: int = _DEFAULT_TIMEOUT_SEC,
) -> str:
    """Ask the operator's authenticated Antigravity CLI for a read-only answer."""

    text = str(prompt or "").strip()
    if not text:
        return "⚠️ ANTIGRAVITY_EMPTY_PROMPT: prompt is required."
    if len(text) > _MAX_PROMPT_CHARS:
        return (
            "⚠️ ANTIGRAVITY_PROMPT_TOO_LARGE: prompt exceeds "
            f"{_MAX_PROMPT_CHARS} characters. Shorten it or use a task artifact."
        )

    binary = find_antigravity_cli()
    if not binary:
        return (
            "⚠️ ANTIGRAVITY_UNAVAILABLE: official `agy` CLI was not found. "
            "Install it with the Antigravity installer, then restart Ouroboros."
        )

    root = pathlib.Path(active_repo_dir_for(ctx)).resolve(strict=False)
    if not root.is_dir():
        return f"⚠️ ANTIGRAVITY_WORKSPACE_INVALID: active workspace does not exist: {root}"

    timeout = _int_bound(timeout_seconds, _DEFAULT_TIMEOUT_SEC, _MAX_TIMEOUT_SEC)
    command: List[str] = [
        binary,
        "--output-format",
        "text",
        "--mode",
        "plan",
        "--sandbox",
        "--disable-slash-commands",
        "--print-timeout",
        f"{timeout}s",
    ]
    selected_model = str(model or "").strip()
    if selected_model:
        command.extend(["--model", selected_model])
    selected_effort = str(effort or "").strip().lower()
    if selected_effort:
        if selected_effort not in {"low", "medium", "high"}:
            return "⚠️ ANTIGRAVITY_INVALID_EFFORT: use low, medium, or high."
        command.extend(["--effort", selected_effort])

    guarded_prompt = (
        "You are being consulted by Ouroboros. Work in read-only planning mode: "
        "do not edit files, run destructive commands, commit, push, or change "
        "settings. Inspect the active workspace only when useful. Return a clear, "
        "actionable answer for the host agent.\n\nUser request:\n"
        + text
    )
    command.extend(["--print", guarded_prompt])

    try:
        proc: subprocess.Popen[str] = subprocess.Popen(
            command,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=None,  # inherit OAuth/keyring environment; never serialize it
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout + 15)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            stdout, stderr = proc.communicate(timeout=5)
            return (
                f"⚠️ ANTIGRAVITY_TIMEOUT: CLI exceeded {timeout} seconds.\n"
                + _clean_output(stdout, stderr)
            )
    except FileNotFoundError:
        return "⚠️ ANTIGRAVITY_UNAVAILABLE: `agy` disappeared before launch."
    except OSError as exc:
        return f"⚠️ ANTIGRAVITY_LAUNCH_FAILED: {type(exc).__name__}: {exc}"

    output = _clean_output(stdout, stderr)
    if proc.returncode != 0:
        return (
            f"⚠️ ANTIGRAVITY_FAILED: official CLI exited with code {proc.returncode}.\n"
            + output
        )
    return "Antigravity (official OAuth CLI, read-only plan mode):\n" + output


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            "antigravity_ask",
            {
                "name": "antigravity_ask",
                "description": (
                    "Ask the operator's authenticated official Antigravity CLI for a "
                    "read-only second opinion, code review, plan, or prompt refinement. "
                    "This uses the existing Google OAuth session and never exposes its token. "
                    "It does not edit the workspace."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "The question or review request for Antigravity.",
                            "minLength": 1,
                            "maxLength": _MAX_PROMPT_CHARS,
                        },
                        "model": {
                            "type": "string",
                            "description": "Optional model id from `agy models` (for example claude-sonnet-4-6).",
                        },
                        "effort": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                            "description": "Optional Antigravity reasoning effort.",
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "minimum": 15,
                            "maximum": _MAX_TIMEOUT_SEC,
                            "default": _DEFAULT_TIMEOUT_SEC,
                            "description": "Maximum CLI wait, in seconds.",
                        },
                    },
                    "required": ["prompt"],
                    "additionalProperties": False,
                },
            },
            _ask_antigravity,
            timeout_sec=_MAX_TIMEOUT_SEC + 30,
        )
    ]


__all__ = ["find_antigravity_cli", "get_tools"]
