"""Git command classifiers for shell-tool safety guards."""

from __future__ import annotations

import pathlib
import os
from typing import Any

from ouroboros.shell_parse import (
    collect_leading_env,
    shell_argv,
    shell_command_string,
    shell_segments,
    strip_leading_env_assignments,
    unwrap_env_argv,
)
from ouroboros.utils import safe_relpath

GIT_READONLY_SUBCOMMANDS = frozenset([
    "status", "diff", "log", "show", "ls-files", "describe", "rev-parse",
    "cat-file", "shortlog", "version", "help", "blame", "grep", "reflog",
    "for-each-ref", "rev-list", "show-ref",
])
# Subcommands that reach the network (gated by allowed_resources.network in
# external workspaces, where local git is otherwise unrestricted).
GIT_NETWORK_SUBCOMMANDS = frozenset([
    "clone", "fetch", "pull", "push", "ls-remote", "submodule",
    "remote", "archive", "lfs",
])
_SHELL_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")"})
_BRANCH_MUTATING_FLAGS = frozenset({
    "-d", "-D", "-m", "-M", "-c", "-C", "-f", "-u",
    "--delete", "--move", "--copy", "--force", "--set-upstream-to",
    "--unset-upstream", "--edit-description", "--track", "--no-track",
})
_BRANCH_READONLY_FLAGS = frozenset({
    "-l", "--list", "-a", "--all", "-r", "--remotes", "-v", "-vv",
    "--verbose", "--show-current", "--contains", "--merged", "--no-merged",
    "--points-at", "--format", "--sort", "--color", "--no-color",
    "--column", "--no-column", "--abbrev", "--no-abbrev", "--ignore-case",
})
_TAG_MUTATING_FLAGS = frozenset({
    "-a", "-s", "-u", "-d", "-v", "-f", "-m", "-F",
    "--annotate", "--sign", "--local-user", "--delete", "--verify",
    "--force", "--message", "--file", "--cleanup", "--create-reflog",
})
_TAG_READONLY_FLAGS = frozenset({
    "-l", "--list", "-n", "--sort", "--format", "--points-at",
    "--contains", "--merged", "--no-merged", "--column", "--no-column",
    "--ignore-case", "--color", "--no-color",
})


def _git_subcommand_and_args(cmd_parts: list[str]) -> tuple[str, list[str]]:
    parts = strip_leading_env_assignments([str(p) for p in cmd_parts])
    if not parts or pathlib.PurePath(parts[0]).name.lower() != "git":
        return "", []
    i = 1
    while i < len(parts):
        part = parts[i]
        if part in _SHELL_SEPARATORS:
            # `git --version; echo done` — the git invocation ends at the
            # separator; what follows is a different command, not a subcommand.
            return "", []
        if part.startswith("-"):
            i += 2 if part in ("-C", "-c", "--git-dir", "--work-tree") else 1
            continue
        return part.lower(), parts[i + 1:]
    return "", []


def _git_option_value_flags(args: list[str]) -> set[int]:
    value_taking_flags = {
        "--contains", "--merged", "--no-merged", "--points-at", "--format",
        "--sort", "--color", "--column", "--abbrev", "-n", "-m", "-F", "-u",
        "--message", "--file", "--local-user", "--set-upstream-to",
    }
    return {idx + 1 for idx, arg in enumerate(args[:-1]) if arg in value_taking_flags}


def _short_flag_chars(arg: str) -> set[str]:
    text = str(arg or "")
    return set(text[1:]) if text.startswith("-") and not text.startswith("--") else set()


def _git_branch_readonly(args: list[str]) -> bool:
    value_indexes = _git_option_value_flags(args)
    read_hint = not args
    explicit_list = False
    positionals = []
    for idx, arg in enumerate(args):
        if idx in value_indexes:
            continue
        if arg in _BRANCH_MUTATING_FLAGS or _short_flag_chars(arg) & set("dDmMcCfFu"):
            return False
        if arg.startswith("--") and "=" in arg:
            flag = arg.split("=", 1)[0]
            if flag in _BRANCH_MUTATING_FLAGS:
                return False
            explicit_list = explicit_list or flag == "--list"
            read_hint = read_hint or flag in _BRANCH_READONLY_FLAGS
            continue
        if arg.startswith("-"):
            chars = _short_flag_chars(arg)
            if arg == "--list" or "l" in chars:
                explicit_list = True
            if arg in _BRANCH_READONLY_FLAGS or chars <= set("alrv"):
                read_hint = True
                continue
            return False
        positionals.append(arg)
    return bool(read_hint and (not positionals or explicit_list))


def _git_tag_readonly(args: list[str]) -> bool:
    value_indexes = _git_option_value_flags(args)
    read_hint = not args
    positionals = []
    for idx, arg in enumerate(args):
        if idx in value_indexes:
            continue
        if arg in _TAG_MUTATING_FLAGS or _short_flag_chars(arg) & set("asudvfmF"):
            return False
        if arg.startswith("--") and "=" in arg:
            flag = arg.split("=", 1)[0]
            if flag in _TAG_MUTATING_FLAGS:
                return False
            read_hint = read_hint or flag in _TAG_READONLY_FLAGS
            continue
        if arg.startswith("-"):
            chars = _short_flag_chars(arg)
            if arg in _TAG_READONLY_FLAGS or chars <= set("ln"):
                read_hint = True
                continue
            return False
        positionals.append(arg)
    return read_hint or not positionals


def _git_invocation_block_reason(parts: list[str], *, allow_network: bool = True) -> str:
    subcmd, args = _git_subcommand_and_args(parts)
    if not subcmd or subcmd in GIT_READONLY_SUBCOMMANDS:
        return ""
    if subcmd == "branch" and _git_branch_readonly(args):
        return ""
    if subcmd == "tag" and _git_tag_readonly(args):
        return ""
    if subcmd == "ls-remote":
        return "" if allow_network else "task_contract.allowed_resources.network=false blocks git ls-remote"
    return f"git {subcmd}"


def run_shell_git_block_reason(raw_cmd: Any, *, allow_network: bool = True) -> str:
    argv = strip_leading_env_assignments(unwrap_env_argv(shell_argv(raw_cmd)))
    if not argv:
        return ""
    first = pathlib.PurePath(argv[0]).name.lower()
    if first in {"bash", "sh", "zsh"}:
        inline = shell_command_string(argv)
        return run_shell_git_block_reason(inline, allow_network=allow_network) if inline else ""
    for idx, token in enumerate(argv):
        if pathlib.PurePath(str(token)).name.lower() == "git":
            reason = _git_invocation_block_reason(argv[idx:], allow_network=allow_network)
            if reason:
                return reason
    return ""


def _resolve_workspace_shell_cwd(active_root: pathlib.Path, cwd: str = "") -> pathlib.Path:
    root = pathlib.Path(active_root).resolve(strict=False)
    if cwd and str(cwd).strip() not in ("", ".", "./"):
        raw = pathlib.Path(str(cwd)).expanduser()
        return raw.resolve(strict=False) if raw.is_absolute() else (root / safe_relpath(str(cwd))).resolve(strict=False)
    return root


def _casefold_relative_to(target: pathlib.Path, root: pathlib.Path) -> bool:
    """Symlink-resolved, case-insensitive "target is under root" (prefix compare
    on resolved parts). Casefold is unconditional — the same conservative trade
    the admission/user_files guards make (``tool_access.paths_overlap_casefold``):
    APFS/NTFS are case-insensitive, so ``/users/anton/ouroboros/REPO`` and the
    real repo are ONE directory there, and a case-sensitive compare is a bypass."""
    try:
        target_parts = pathlib.Path(target).resolve(strict=False).parts
        root_parts = pathlib.Path(root).resolve(strict=False).parts
    except (OSError, ValueError):
        return False
    if len(target_parts) < len(root_parts):
        return False
    return tuple(part.casefold() for part in target_parts[: len(root_parts)]) == tuple(
        part.casefold() for part in root_parts
    )


def _overlaps_protected(target: pathlib.Path, root: pathlib.Path) -> bool:
    """BIDIRECTIONAL containment: the target inside the protected root, OR the
    protected root inside the target. One direction alone lets an ANCESTOR
    target through — ``git -C ~/Ouroboros init`` puts repo/ and data/ inside a
    task-created work tree even though the target contains (rather than is
    contained by) every protected root."""
    return _casefold_relative_to(target, root) or _casefold_relative_to(root, target)


def _shell_path(text: str) -> pathlib.Path:
    return pathlib.Path(os.path.expandvars(str(text or ""))).expanduser()


def external_workspace_git_violation(
    raw_cmd: Any,
    *,
    active_root: pathlib.Path,
    cwd: str = "",
    protected_roots: list[pathlib.Path] | None = None,
    allow_network: bool = True,
    inherited_env: "dict[str, str] | None" = None,
) -> str:
    """Target-aware git policy: full git is legitimate task work.

    Tasks routinely need `git clone`, `git checkout`, `git commit`, even a real
    `git push` to a task-local remote. Since the Q4=A unwind (2026-08-08) this is
    the ONE resolver for external workspaces AND the default (non-workspace)
    shell lane — direct chat, light mode, self_modification-profile tasks — so
    the deterministic guard only protects what actually needs protecting:

    - no MUTATING git invocation may target the Ouroboros runtime (system repo
      or any data drive) via cwd, `-C`, `--git-dir`, `--work-tree`, `GIT_DIR`/
      `GIT_WORK_TREE` env, or a path argument. Containment is bidirectional
      (an ancestor target such as ``git -C ~/Ouroboros init`` is a violation
      too), casefold, and symlink-resolved;
    - READ-ONLY git (status/log/diff/show/rev-parse/branch- and tag-listing)
      stays allowed EVERYWHERE, including at a runtime target — blocking it is
      the recorded false-block class (v4.5.1, f14baf8f);
    - network-reaching subcommands respect ``allowed_resources.network`` in
      every lane, read-only or not.

    Everything else stays allowed here; the LLM safety layer still reviews the
    command for genuinely dangerous intent. Disclosed residuals (deliberately
    NOT chased — no shell-parser arms race): git launched through a transparent
    wrapper (``nice``/``xargs``/``time``) or from inside interpreter code is not
    a per-segment ``git`` command and is not classified here (the pre-flip text
    classifier never saw the interpreter form either); `-c alias.*=...`/
    ``include.path`` config indirection is not parsed — only the explicit
    cwd/flag/env/argument target vectors above are resolved.
    """
    roots = [pathlib.Path(p) for p in (protected_roots or [])]
    base = _resolve_workspace_shell_cwd(pathlib.Path(active_root), cwd)
    segments = shell_segments(raw_cmd)
    if not segments:
        return ""

    def _protected_label(target: pathlib.Path) -> str:
        for root in roots:
            if _overlaps_protected(target, root):
                return str(root)
        return ""

    def _resolve(value: str, base_dir: pathlib.Path) -> pathlib.Path:
        target = _shell_path(value)
        if not target.is_absolute():
            target = base_dir / target
        return target.resolve(strict=False)

    current_base = base
    # GIT_DIR/GIT_WORK_TREE exported in EARLIER segments (or inherited from an
    # enclosing shell that carried them into this `sh -c ...`).
    session_env: dict[str, str] = {
        k: v for k, v in (inherited_env or {}).items() if k in ("GIT_DIR", "GIT_WORK_TREE")
    }
    for segment in segments:
        if not segment:
            continue
        # Peel leading env assignments (VAR=val / env VAR=val) FIRST so a prefix
        # like `GIT_DIR=x bash -c '...'` is captured before the shell recursion.
        env_assigns, command = collect_leading_env(segment)
        if not command:
            # A pure-assignment segment (`GIT_DIR=... ` alone) exports into the
            # shell session and applies to LATER git segments. Carry it forward.
            for var in ("GIT_DIR", "GIT_WORK_TREE"):
                if var in env_assigns:
                    session_env[var] = env_assigns[var]
            continue
        cmd_name = pathlib.PurePath(str(command[0]).strip("`'\"")).name.lower()
        # Recurse into nested shells (sh -c "..."), carrying any GIT_DIR/
        # GIT_WORK_TREE env (segment-local + session) INTO the nested inspection
        # so `GIT_DIR=<runtime> bash -c 'git reset'` cannot retarget the repo.
        if cmd_name in {"bash", "sh", "zsh"}:
            inline = shell_command_string(command)
            if inline:
                nested = external_workspace_git_violation(
                    inline,
                    active_root=active_root,
                    cwd=str(current_base),
                    protected_roots=roots,
                    allow_network=allow_network,
                    inherited_env={**session_env, **env_assigns},
                )
                if nested:
                    return nested
            continue
        if cmd_name in {"export", "declare", "typeset"}:
            # `export GIT_DIR=...`, and bash `declare -x` / zsh-ksh `typeset -x`,
            # all export into the environment git honours. Capture GIT_* either
            # way (flags like -x are ignored; only VAR=val tokens are read).
            for token in command[1:]:
                text = str(token)
                if "=" in text and not text.startswith(("-", "=")):
                    key, _, value = text.partition("=")
                    if key in ("GIT_DIR", "GIT_WORK_TREE"):
                        session_env[key] = value
            continue
        if cmd_name == "cd" and len(command) >= 2:
            target = _shell_path(str(command[1]))
            current_base = target if target.is_absolute() else (current_base / target)
            current_base = current_base.resolve(strict=False)
            continue
        if cmd_name != "git":
            continue
        invocation = command
        # READ-ONLY git is never target-checked: `git status`/`log`/`diff` at the
        # system repo is the vcs_status-equivalent inspection lane, and blocking
        # it is the recorded false-block class (v4.5.1; f14baf8f). Classification
        # deliberately probes with allow_network=True so it answers ONLY "does
        # this invocation mutate?" — the contract's network fence is enforced
        # separately at the tail, for read-only and mutating git alike.
        if _git_invocation_block_reason(invocation, allow_network=True):
            # Global `-C <path>` flags (before the subcommand) CHDIR sequentially
            # BEFORE git does anything: the EFFECTIVE working directory — not the
            # shell cwd — is what a mutating invocation targets. Chain them so
            # `git -C /tmp/proj commit` from a runtime cwd (the default lane's
            # default cwd IS the system repo) stays allowed, while
            # `git -C <runtime> commit` from anywhere hits the same containment
            # check. `-C` AFTER the subcommand is subcommand syntax (e.g.
            # `git commit -C <commit>` reuses a message) and is not a path.
            effective_base = current_base
            j = 1
            while j < len(invocation):
                part = str(invocation[j])
                if part == "-C" and j + 1 < len(invocation):
                    effective_base = _resolve(str(invocation[j + 1]), effective_base)
                    j += 2
                    continue
                if part.startswith("-"):
                    j += 2 if part in ("-c", "--git-dir", "--work-tree") else 1
                    continue
                break  # the subcommand
            for root in roots:
                if _overlaps_protected(effective_base, root):
                    return "git working directory targets the Ouroboros runtime"
            # Git is legitimate task work in host scratch (a repo under /tmp, a
            # /build tree, a sibling checkout), so the cwd is NOT confined to the
            # declared active workspace — only the Ouroboros runtime roots above
            # are protected (per this function's contract).
            # GIT_DIR / GIT_WORK_TREE environment retargeting (this segment runs
            # git). Merge env exported in earlier segments; segment-local wins.
            # Relative values resolve against the post-`-C` effective base — the
            # cwd git actually sees when it reads the environment.
            effective_env = {**session_env, **env_assigns}
            for var in ("GIT_DIR", "GIT_WORK_TREE"):
                val = effective_env.get(var)
                if not val:
                    continue
                target = _resolve(val, effective_base)
                if _protected_label(target):
                    return f"git invocation targets the Ouroboros runtime via {var}"
            j = 1
            while j < len(invocation):
                part = str(invocation[j])
                value = ""
                if part in {"--git-dir", "--work-tree"} and j + 1 < len(invocation):
                    value = str(invocation[j + 1])
                    j += 2
                elif part.startswith("--git-dir=") or part.startswith("--work-tree="):
                    value = part.split("=", 1)[1]
                    j += 1
                elif part in {"-c", "-C"}:
                    j += 2
                else:
                    j += 1
                if not value:
                    continue
                target = _resolve(value, effective_base)
                if _protected_label(target):
                    return "git invocation targets the Ouroboros runtime"
            for arg in invocation[1:]:
                text = str(arg)
                if "=" in text and text.startswith("--"):
                    text = text.split("=", 1)[1]
                candidate = _shell_path(text)
                if not candidate.is_absolute():
                    # Relative args resolve under the effective base; a
                    # non-runtime base can only escape toward the runtime by
                    # ".."-climbing (a plain descend from a base that passed the
                    # bidirectional check cannot reach a protected root).
                    if ".." not in candidate.parts:
                        continue
                    candidate = (effective_base / candidate).resolve(strict=False)
                if _protected_label(candidate):
                    return "git invocation targets the Ouroboros runtime"
        if not allow_network:
            subcmd, _ = _git_subcommand_and_args(invocation)
            if subcmd in GIT_NETWORK_SUBCOMMANDS:
                return f"task_contract.allowed_resources.network=false blocks git {subcmd}"
    return ""


def workspace_git_safety_violation(
    raw_cmd: Any,
    *,
    active_root: pathlib.Path,
    cwd: str = "",
    allow_network: bool = True,
) -> str:
    root = pathlib.Path(active_root).resolve(strict=False)
    base = _resolve_workspace_shell_cwd(root, cwd)
    try:
        base.relative_to(root)
        base_inside_root = True
    except Exception:
        base_inside_root = False
    argv = strip_leading_env_assignments(unwrap_env_argv(shell_argv(raw_cmd)))
    if not argv:
        return ""
    first = pathlib.PurePath(argv[0]).name.lower()
    if first in {"bash", "sh", "zsh"}:
        inline = shell_command_string(argv)
        return workspace_git_safety_violation(
            inline,
            active_root=root,
            cwd=str(base) if inline else "",
            allow_network=allow_network,
        ) if inline else ""
    for idx, token in enumerate(argv):
        if pathlib.PurePath(str(token)).name.lower() != "git":
            continue
        parts = argv[idx:]
        saw_root_selector = False
        j = 1
        while j < len(parts):
            part = parts[j]
            if part in {"-C", "--git-dir", "--work-tree"} and j + 1 < len(parts):
                saw_root_selector = True
                try:
                    target = pathlib.Path(parts[j + 1])
                    if not target.is_absolute():
                        target = base / target
                    target.resolve(strict=False).relative_to(root)
                except Exception:
                    return f"git {part} escapes the active workspace"
                j += 2
                continue
            if part.startswith("--git-dir=") or part.startswith("--work-tree="):
                saw_root_selector = True
                value = part.split("=", 1)[1]
                try:
                    target = pathlib.Path(value)
                    if not target.is_absolute():
                        target = base / target
                    target.resolve(strict=False).relative_to(root)
                except Exception:
                    return "git root selector escapes the active workspace"
                j += 1
                continue
            if part == "-c":
                j += 2
                continue
            if part.startswith("-"):
                j += 1
                continue
            break
        if not base_inside_root and not saw_root_selector:
            return "git cwd escapes the active workspace"
        reason = _git_invocation_block_reason(parts, allow_network=allow_network)
        if reason:
            return reason
    return ""
