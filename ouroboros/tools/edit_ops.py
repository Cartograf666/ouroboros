"""Advanced repo editing tools: apply_patch and edit_batch.

Two editing primitives beyond exact-match ``edit_text`` and full-file
``write_file``:

- ``apply_patch``  — context-anchored multi-file diff (V4A-style, no line
  numbers): hunks locate themselves by surrounding lines plus optional ``@@``
  anchors. Atomic across all files/hunks: any unmatched hunk aborts the whole
  patch with per-hunk diagnostics.
- ``edit_batch``   — atomic batch of COUNTED exact replacements. Each edit
  declares how many occurrences it expects (default 1); a count mismatch
  aborts the whole batch. This is the safe form of "replace all".

Both target the repo lanes only (active_workspace / system_repo) and reuse the
same guard chain as ``edit_text``: root access, protected artifact paths,
project-room write guard, protected runtime paths.

(An ``edit_sketch`` fast-apply tool — strong-model sketch merged by the cheap
LIGHT model — lived here through the editbench evaluation and was removed: the
sketch/apply split never beat the direct tools on either cost or robustness;
see devtools/benchmarks/editbench/README.md. Its useful rails — unified diff
in the result and a pre-write syntax check — moved into write_file.)

``_syntax_check`` and ``_unified_diff`` are shared helpers, also used by the
repo write path (git._repo_write).
"""

from __future__ import annotations

import difflib
import json
import logging
import pathlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.config import get_runtime_mode
from ouroboros.runtime_mode_policy import (
    is_protected_runtime_path,
    mode_allows_protected_write,
    normalize_repo_path,
    protected_write_block_message,
)
from ouroboros.tools.registry import ToolContext, ToolEntry, active_repo_dir_for
from ouroboros.utils import safe_relpath, write_text

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared target resolution (mirrors the edit_text guard chain)
# ---------------------------------------------------------------------------

def _resolve_edit_target(
    ctx: ToolContext, path: str, root: str, *, error_tag: str
) -> Tuple[Optional[pathlib.Path], str]:
    """Resolve ``path`` under ``root`` with the same guards as edit_text.

    Returns ``(target, "")`` on success or ``(None, error_message)``.
    """
    from ouroboros.tools.core import (
        _access_or_block,
        _protected_artifact_write_block,
        project_room_lens_dir,
    )

    if not path or not str(path).strip():
        return None, f"⚠️ {error_tag}: path is required."
    normalized, block = _access_or_block(ctx, root, "edit")
    if block:
        return None, block
    if normalized not in {"active_workspace", "system_repo"}:
        return None, (
            f"⚠️ {error_tag}: root={normalized!r} is not supported; "
            "these tools edit repo lanes only (active_workspace / system_repo). "
            "Use write_file/edit_text for data-plane roots."
        )
    if normalized == "system_repo":
        try:
            from ouroboros.tool_access import resource_root_path

            active_root = resource_root_path(ctx, "active_workspace")
            system_root = resource_root_path(ctx, "system_repo")
            if active_root.resolve(strict=False) != system_root.resolve(strict=False):
                return None, (
                    f"⚠️ {error_tag}: root=system_repo edits require the active "
                    "workspace to be the system repo."
                )
        except Exception as exc:  # noqa: BLE001 - validation must fail closed
            return None, f"⚠️ {error_tag}: could not validate system_repo root: {type(exc).__name__}: {exc}"
    protected_block = _protected_artifact_write_block(
        ctx, normalized, [path], prefix=error_tag
    )
    if protected_block:
        return None, protected_block
    if normalized == "active_workspace" and project_room_lens_dir(ctx) is not None:
        return None, (
            "⚠️ ROOM_WRITE_VIA_TASK: this room's files are edited by PROMOTED tasks — "
            "call promote_chat_to_task for real work there. For a deliberate edit of "
            'the Ouroboros system repo, pass root="system_repo" explicitly.'
        )
    norm = normalize_repo_path(path)
    if (
        not ctx.is_workspace_mode()
        and is_protected_runtime_path(norm)
        and not mode_allows_protected_write(_runtime_mode())
    ):
        return None, protected_write_block_message(
            path=norm, runtime_mode=_runtime_mode(), action="edit"
        )
    try:
        target = ctx.repo_path(path)
    except ValueError as e:
        return None, f"⚠️ PATH_ERROR: {e}"
    return target, ""


def _runtime_mode() -> str:
    try:
        return get_runtime_mode()
    except Exception:
        return "advanced"


def _finish_mutation(ctx: ToolContext, changed_paths: List[str], source_tool: str) -> str:
    """Advisory invalidation + the standard commit/patch-artifact footer."""
    from ouroboros.tools.commit_gate import _invalidate_advisory

    try:
        _invalidate_advisory(
            ctx,
            changed_paths=changed_paths,
            mutation_root=active_repo_dir_for(ctx),
            source_tool=source_tool,
        )
    except Exception:
        log.debug("%s: advisory invalidation failed (non-critical)", source_tool, exc_info=True)
    if ctx.is_workspace_mode():
        return "Files are on disk but NOT committed. Do not commit; the headless runner will emit a patch artifact."
    return (
        "Files are on disk but NOT committed. Run commit_reviewed when ready.\n"
        "⚠️ Advisory pre-review is now stale — run advisory_review before commit_reviewed."
    )


def _line_positions(text: str, needle: str, limit: int = 5) -> List[str]:
    positions: List[str] = []
    start = 0
    for _ in range(limit):
        idx = text.find(needle, start)
        if idx < 0:
            break
        positions.append(f"line {text[:idx].count(chr(10)) + 1}")
        start = idx + 1
    return positions


# ---------------------------------------------------------------------------
# apply_patch
# ---------------------------------------------------------------------------

_PATCH_BEGIN = "*** Begin Patch"
_PATCH_END = "*** End Patch"
_UPDATE_HDR = "*** Update File:"
_ADD_HDR = "*** Add File:"
_DELETE_HDR = "*** Delete File:"


@dataclass
class _Hunk:
    anchor: str = ""
    lines: List[Tuple[str, str]] = field(default_factory=list)  # (prefix, text)


@dataclass
class _FileOp:
    kind: str  # update | add | delete
    path: str
    hunks: List[_Hunk] = field(default_factory=list)
    add_lines: List[str] = field(default_factory=list)


def _strip_directive_tail(text: str) -> str:
    """Drop decorative trailing asterisks models like to add: '... ***'."""
    return text.strip().rstrip("*").strip()


def _parse_patch(patch: str) -> Tuple[List[_FileOp], str]:
    """Parse the V4A-style patch envelope. Returns (ops, error)."""
    lines = patch.splitlines()
    ops: List[_FileOp] = []
    current: Optional[_FileOp] = None
    seen_end = False
    for lineno, raw in enumerate(lines, 1):
        if seen_end:
            if raw.strip():
                return [], f"⚠️ APPLY_PATCH_ERROR: content after '{_PATCH_END}' (line {lineno})."
            continue
        # Envelope/headers tolerate decorative trailing '***' ("*** Begin Patch ***").
        directive = _strip_directive_tail(raw) if raw.lstrip().startswith("***") else raw.strip()
        if directive == _strip_directive_tail(_PATCH_BEGIN) and raw.lstrip().startswith("***"):
            continue
        if directive == _strip_directive_tail(_PATCH_END) and raw.lstrip().startswith("***"):
            seen_end = True
            continue
        if raw.startswith(_UPDATE_HDR):
            current = _FileOp("update", _strip_directive_tail(raw[len(_UPDATE_HDR):]))
            ops.append(current)
            continue
        if raw.startswith(_ADD_HDR):
            current = _FileOp("add", _strip_directive_tail(raw[len(_ADD_HDR):]))
            ops.append(current)
            continue
        if raw.startswith(_DELETE_HDR):
            current = _FileOp("delete", _strip_directive_tail(raw[len(_DELETE_HDR):]))
            ops.append(current)
            continue
        if raw.startswith("***"):
            return [], f"⚠️ APPLY_PATCH_ERROR: unrecognized directive at line {lineno}: {raw.strip()!r}."
        if current is None:
            if raw.strip():
                return [], (
                    f"⚠️ APPLY_PATCH_ERROR: content before the first file header "
                    f"(line {lineno}). Start with '{_UPDATE_HDR} <path>'."
                )
            continue
        if current.kind == "add":
            if raw.startswith("+"):
                current.add_lines.append(raw[1:])
            elif not raw.strip():
                current.add_lines.append("")
            else:
                return [], (
                    f"⚠️ APPLY_PATCH_ERROR: Add File body lines must start with '+' "
                    f"(line {lineno}: {raw[:60]!r})."
                )
            continue
        if current.kind == "delete":
            if raw.strip():
                return [], f"⚠️ APPLY_PATCH_ERROR: Delete File takes no body (line {lineno})."
            continue
        # update
        if raw.startswith("@@"):
            current.hunks.append(_Hunk(anchor=raw[2:].strip()))
            continue
        if raw.startswith(("+", "-", " ")) or raw == "":
            if not current.hunks:
                current.hunks.append(_Hunk())
            prefix = raw[:1] if raw else " "
            current.hunks[-1].lines.append((prefix, raw[1:] if raw else ""))
            continue
        return [], (
            f"⚠️ APPLY_PATCH_ERROR: unrecognized hunk line at {lineno}: {raw[:60]!r}. "
            "Hunk lines must start with ' ', '-', '+' or '@@'."
        )
    if not ops:
        return [], (
            "⚠️ APPLY_PATCH_ERROR: no file operations found. Expected headers like "
            f"'{_UPDATE_HDR} <path>' with hunks of ' '/'-'/'+' lines."
        )
    for op in ops:
        if not op.path:
            return [], f"⚠️ APPLY_PATCH_ERROR: {op.kind} header is missing a file path."
        if op.kind == "update" and not any(h.lines for h in op.hunks):
            return [], f"⚠️ APPLY_PATCH_ERROR: Update File {op.path}: no hunk lines."
    return ops, ""


def _find_sequence(
    file_lines: List[str], seq: List[str], start: int, *, fuzzy: bool
) -> List[int]:
    """Indices >= start where ``seq`` matches ``file_lines`` (cap 5)."""
    if not seq:
        return []
    matches: List[int] = []
    if fuzzy:
        hay = [l.rstrip() for l in file_lines]
        needle = [l.rstrip() for l in seq]
    else:
        hay = file_lines
        needle = seq
    n = len(needle)
    for i in range(start, len(hay) - n + 1):
        if hay[i:i + n] == needle:
            matches.append(i)
            if len(matches) >= 5:
                break
    return matches


def _apply_hunks_to_text(
    content: str, hunks: List[_Hunk], path: str
) -> Tuple[Optional[str], List[str], str]:
    """Apply hunks in order. Returns (new_content, notes, error)."""
    file_lines = content.split("\n")
    notes: List[str] = []
    cursor = 0
    for hi, hunk in enumerate(hunks, 1):
        old = [t for p, t in hunk.lines if p in (" ", "-")]
        new = [t for p, t in hunk.lines if p in (" ", "+")]
        start = cursor
        if hunk.anchor:
            anchor_hits = [
                i for i in range(start, len(file_lines)) if hunk.anchor in file_lines[i]
            ]
            if not anchor_hits:
                return None, notes, (
                    f"hunk {hi}: @@ anchor {hunk.anchor!r} not found in {path} "
                    f"after line {start + 1}"
                )
            start = anchor_hits[0]
        if not old:
            if not hunk.anchor:
                return None, notes, (
                    f"hunk {hi}: pure insertion needs an @@ anchor or context lines"
                )
            pos = start + 1
            file_lines[pos:pos] = new
            cursor = pos + len(new)
            continue
        matches = _find_sequence(file_lines, old, start, fuzzy=False)
        fuzzy_used = False
        if not matches:
            matches = _find_sequence(file_lines, old, start, fuzzy=True)
            fuzzy_used = bool(matches)
        if not matches:
            preview = "\n".join("    " + l for l in old[:6])
            return None, notes, (
                f"hunk {hi}: context not found in {path} (searched from line {start + 1}). "
                f"Hunk expects these consecutive lines:\n{preview}\n"
                "Copy the exact lines from the file (read_file) into the hunk context."
            )
        if len(matches) > 1:
            where = ", ".join(f"line {m + 1}" for m in matches)
            return None, notes, (
                f"hunk {hi}: context is ambiguous in {path} — matches at {where}. "
                "Add an @@ anchor (e.g. '@@ def name') or more context lines."
            )
        pos = matches[0]
        file_lines[pos:pos + len(old)] = new
        cursor = pos + len(new)
        if fuzzy_used:
            notes.append(f"hunk {hi}: matched after trailing-whitespace normalization")
    return "\n".join(file_lines), notes, ""


def _apply_patch(ctx: ToolContext, patch: str, root: str = "active_workspace") -> str:
    if not patch or not patch.strip():
        return "⚠️ APPLY_PATCH_ERROR: patch is required."
    ops, err = _parse_patch(patch)
    if err:
        return err

    # Phase 1: resolve + validate everything BEFORE any write (atomicity).
    planned_writes: List[Tuple[pathlib.Path, str, str]] = []  # (target, rel_path, content)
    planned_deletes: List[Tuple[pathlib.Path, str]] = []
    summaries: List[str] = []
    all_notes: List[str] = []
    seen: Dict[str, str] = {}  # rel path -> pending content (chained updates)
    for op in ops:
        target, terr = _resolve_edit_target(ctx, op.path, root, error_tag="APPLY_PATCH_BLOCKED")
        if terr:
            return terr
        rel = safe_relpath(op.path)
        if op.kind == "add":
            if rel in seen or target.exists():
                return (
                    f"⚠️ APPLY_PATCH_ERROR: Add File {op.path}: file already exists. "
                    "Use '*** Update File:' to modify it."
                )
            content = "\n".join(op.add_lines)
            if content and not content.endswith("\n"):
                content += "\n"
            planned_writes.append((target, rel, content))
            seen[rel] = content
            summaries.append(f"✅ Added {rel} ({len(op.add_lines)} lines)")
            continue
        if op.kind == "delete":
            if not target.exists():
                return f"⚠️ APPLY_PATCH_ERROR: Delete File {op.path}: file not found."
            planned_deletes.append((target, rel))
            summaries.append(f"✅ Deleted {rel}")
            continue
        # update
        if rel in seen:
            content = seen[rel]
        else:
            if not target.exists():
                return f"⚠️ APPLY_PATCH_ERROR: Update File {op.path}: file not found."
            try:
                content = target.read_text(encoding="utf-8")
            except Exception as e:  # noqa: BLE001 - report unreadable target
                return f"⚠️ APPLY_PATCH_ERROR: cannot read {op.path}: {e}"
        new_content, notes, herr = _apply_hunks_to_text(content, op.hunks, rel)
        if herr:
            return f"⚠️ APPLY_PATCH_ERROR: {herr}\nNothing was applied (the patch is atomic)."
        seen[rel] = new_content
        planned_writes.append((target, rel, new_content))
        added = sum(1 for h in op.hunks for p, _ in h.lines if p == "+")
        removed = sum(1 for h in op.hunks for p, _ in h.lines if p == "-")
        summaries.append(f"✅ Updated {rel} ({len(op.hunks)} hunk(s), +{added}/-{removed} lines)")
        all_notes.extend(f"{rel}: {n}" for n in notes)

    # Phase 2: write. Dedup chained updates so each file is written once (final content).
    final_content: Dict[str, Tuple[pathlib.Path, str]] = {}
    for target, rel, content in planned_writes:
        final_content[rel] = (target, content)
    changed_paths: List[str] = []
    for rel, (target, content) in final_content.items():
        try:
            write_text(target, content)
        except Exception as e:  # noqa: BLE001 - surface the failed path
            return f"⚠️ APPLY_PATCH_ERROR: write failed for {rel}: {e} (earlier files in this patch WERE written)"
        changed_paths.append(rel)
    for target, rel in planned_deletes:
        try:
            target.unlink()
        except Exception as e:  # noqa: BLE001 - surface the failed path
            return f"⚠️ APPLY_PATCH_ERROR: delete failed for {rel}: {e}"
        changed_paths.append(rel)

    footer = _finish_mutation(ctx, changed_paths, "apply_patch")
    body = "\n".join(summaries)
    if all_notes:
        body += "\nNotes:\n" + "\n".join("  " + n for n in all_notes)
    return f"{body}\n{footer}"


# ---------------------------------------------------------------------------
# edit_batch
# ---------------------------------------------------------------------------

def _edit_batch(ctx: ToolContext, edits: List[Dict[str, Any]], root: str = "active_workspace") -> str:
    if not edits or not isinstance(edits, list):
        return "⚠️ EDIT_BATCH_ERROR: edits must be a non-empty array."
    contents: Dict[str, str] = {}
    targets: Dict[str, pathlib.Path] = {}
    applied: List[str] = []
    errors: List[str] = []
    for idx, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            errors.append(f"edit {idx}: must be an object")
            continue
        path = str(edit.get("path", "") or "")
        old_str = edit.get("old_str", "")
        new_str = edit.get("new_str", "")
        if not isinstance(old_str, str) or not old_str:
            errors.append(f"edit {idx} ({path or '?'}): old_str is required (non-empty string)")
            continue
        if not isinstance(new_str, str):
            errors.append(f"edit {idx} ({path or '?'}): new_str must be a string")
            continue
        try:
            count = int(edit.get("count", 1))
        except (TypeError, ValueError):
            errors.append(f"edit {idx} ({path or '?'}): count must be an integer")
            continue
        if count < 1:
            errors.append(f"edit {idx} ({path or '?'}): count must be >= 1")
            continue
        rel = safe_relpath(path) if path else ""
        if rel not in contents:
            target, terr = _resolve_edit_target(ctx, path, root, error_tag="EDIT_BATCH_BLOCKED")
            if terr:
                errors.append(f"edit {idx}: {terr.lstrip('⚠️ ')}")
                continue
            if not target.exists():
                errors.append(f"edit {idx} ({rel}): file not found")
                continue
            try:
                contents[rel] = target.read_text(encoding="utf-8")
            except Exception as e:  # noqa: BLE001 - report unreadable target
                errors.append(f"edit {idx} ({rel}): cannot read: {e}")
                continue
            targets[rel] = target
        text = contents[rel]
        occurrences = text.count(old_str)
        if occurrences != count:
            positions = _line_positions(text, old_str)
            where = f" (at: {', '.join(positions)})" if positions else ""
            errors.append(
                f"edit {idx} ({rel}): old_str occurs {occurrences} time(s), expected {count}{where}. "
                "Re-read the file and set count to the exact number of occurrences you intend to replace."
            )
            continue
        contents[rel] = text.replace(old_str, new_str)
        applied.append(f"edit {idx} ({rel}): replaced {count} occurrence(s)")
    if errors:
        return (
            "⚠️ EDIT_BATCH_ERROR: batch aborted, NOTHING was written (atomic). Problems:\n"
            + "\n".join("  - " + e for e in errors)
        )
    changed: List[str] = []
    for rel, text in contents.items():
        try:
            write_text(targets[rel], text)
        except Exception as e:  # noqa: BLE001 - surface the failed path
            return f"⚠️ EDIT_BATCH_ERROR: write failed for {rel}: {e} (earlier files WERE written)"
        changed.append(rel)
    footer = _finish_mutation(ctx, changed, "edit_batch")
    return (
        f"✅ edit_batch applied {len(applied)} edit(s) across {len(changed)} file(s):\n"
        + "\n".join("  " + a for a in applied)
        + f"\n{footer}"
    )


# ---------------------------------------------------------------------------
# shared verification helpers (also used by git._repo_write)
# ---------------------------------------------------------------------------

def _syntax_check(rel: str, content: str) -> str:
    """Cheap validity check for known formats. Returns error text or ''."""
    try:
        if rel.endswith(".py"):
            compile(content, rel, "exec")
        elif rel.endswith(".json"):
            json.loads(content)
    except SyntaxError as e:
        return f"content has a Python syntax error at line {e.lineno}: {e.msg}"
    except ValueError as e:
        return f"content is not valid JSON: {e}"
    except Exception:
        return ""
    return ""


def _unified_diff(rel: str, before: str, after: str, cap: int = 400) -> str:
    diff_lines = list(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="",
        )
    )
    if not diff_lines:
        return "(no textual changes)"
    clipped = diff_lines[:cap]
    if len(diff_lines) > cap:
        clipped.append(f"... diff truncated ({len(diff_lines) - cap} more lines)")
    return "\n".join(clipped)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("apply_patch", {
            "name": "apply_patch",
            "description": (
                "Apply a context-anchored multi-file patch (no line numbers). Atomic: "
                "any unmatched hunk aborts the whole patch. Format:\n"
                "*** Begin Patch\n"
                "*** Update File: relative/path.py\n"
                "@@ def nearest_function\n"
                " context line (starts with a space)\n"
                "-removed line\n"
                "+added line\n"
                "*** Add File: new/file.py\n"
                "+each line of the new file prefixed with +\n"
                "*** Delete File: old/file.py\n"
                "*** End Patch\n"
                "Hunks locate themselves by their exact context lines (copy them from "
                "read_file); the optional @@ anchor disambiguates repeated contexts. "
                "Prefer this over many edit_text calls for scattered multi-file changes. "
                "NOT for rewrites touching most of a file — there the patch grows as "
                "large as the file itself; use write_file instead."
            ),
            "parameters": {"type": "object", "properties": {
                "patch": {"type": "string", "description": "The full patch text (envelope lines optional)."},
                "root": {"type": "string", "enum": ["active_workspace", "system_repo"], "default": "active_workspace"},
            }, "required": ["patch"]},
        }, _apply_patch, is_code_tool=True, mutates_worktree=True),
        ToolEntry("edit_batch", {
            "name": "edit_batch",
            "description": (
                "Atomic batch of COUNTED exact replacements across one or more files. "
                "Each edit replaces ALL occurrences of old_str in its file and declares "
                "the exact number it expects via count (default 1). Any count mismatch "
                "aborts the WHOLE batch with per-edit diagnostics — read the file(s) "
                "first and state counts you verified. This is the safe 'replace all': "
                "use count>1 for identical repeated edits instead of many edit_text calls."
            ),
            "parameters": {"type": "object", "properties": {
                "edits": {"type": "array", "items": {"type": "object", "properties": {
                    "path": {"type": "string"},
                    "old_str": {"type": "string"},
                    "new_str": {"type": "string"},
                    "count": {"type": "integer", "default": 1,
                              "description": "Exact number of occurrences expected AND replaced."},
                }, "required": ["path", "old_str", "new_str"]}},
                "root": {"type": "string", "enum": ["active_workspace", "system_repo"], "default": "active_workspace"},
            }, "required": ["edits"]},
        }, _edit_batch, is_code_tool=True, mutates_worktree=True),
    ]
