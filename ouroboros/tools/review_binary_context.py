"""Exact, bounded prompt metadata for staged binary files."""

from __future__ import annotations

import os
import pathlib
import subprocess


def _git_bytes(repo_dir: pathlib.Path, args: list[str]) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return b""
    return result.stdout if result.returncode == 0 else b""


def _tree_entry(repo_dir: pathlib.Path, ref: str, rel: str) -> tuple[str, str]:
    record = _git_bytes(repo_dir, ["ls-tree", "-z", ref, "--", rel]).split(b"\0", 1)[0]
    fields = record.partition(b"\t")[0].split()
    if len(fields) < 3:
        return "", ""
    return fields[0].decode("ascii"), fields[2].decode("ascii")


def _object_size(repo_dir: pathlib.Path, blob_oid: str) -> str:
    value = _git_bytes(repo_dir, ["cat-file", "-s", blob_oid]).strip()
    return value.decode("ascii") if value.isdigit() else ""


def staged_path_is_binary(repo_dir: pathlib.Path, rel: str) -> bool:
    """Detect staged binary content from Git numstat, independent of filename."""
    expected_path = os.fsencode(rel)
    raw = _git_bytes(
        repo_dir, ["diff", "--cached", "--numstat", "-z", "--", rel]
    )
    for record in raw.split(b"\0"):
        fields = record.split(b"\t", 2)
        if len(fields) == 3 and fields[2] == expected_path:
            return fields[0] == b"-" and fields[1] == b"-"
    return False


def render_staged_binary_metadata(repo_dir: pathlib.Path, rel: str) -> str | None:
    """Render stage-0 identity, or fail closed when the index object is unbound."""
    raw = _git_bytes(repo_dir, ["ls-files", "--stage", "-z", "--", rel])
    expected_path = os.fsencode(rel)
    for record in raw.split(b"\0"):
        prefix, separator, path = record.partition(b"\t")
        fields = prefix.split()
        if separator and path == expected_path and len(fields) == 3 and fields[2] == b"0":
            mode = fields[0].decode("ascii")
            blob_oid = fields[1].decode("ascii")
            object_size = _object_size(repo_dir, blob_oid)
            if not object_size:
                return None
            _head_mode, head_blob = _tree_entry(repo_dir, "HEAD", rel)
            _merge_mode, merge_blob = _tree_entry(repo_dir, "MERGE_HEAD", rel)
            return (
                "*(binary bytes are represented by exact Git metadata for this review; "
                "they are not rendered as text)*\n\n"
                f"- staged blob: `{blob_oid}`\n"
                f"- staged mode: `{mode}`\n"
                f"- staged object size: `{object_size}` bytes\n"
                f"- pre-merge HEAD blob: `{head_blob or 'absent'}`\n"
                f"- official MERGE_HEAD blob: `{merge_blob or 'absent'}`\n"
            )
    deleted = _git_bytes(
        repo_dir, ["diff", "--cached", "--name-only", "--diff-filter=D", "-z", "--", rel]
    ).split(b"\0")
    if expected_path not in deleted:
        return None
    head_mode, head_blob = _tree_entry(repo_dir, "HEAD", rel)
    merge_mode, merge_blob = _tree_entry(repo_dir, "MERGE_HEAD", rel)
    parent_blob = head_blob or merge_blob
    object_size = _object_size(repo_dir, parent_blob) if parent_blob else ""
    if not parent_blob or not object_size:
        return None
    return (
        "*(binary deletion is represented by exact Git metadata for this review; "
        "the deleted bytes are not rendered as text)*\n\n"
        "- staged blob: `absent (deletion)`\n"
        "- staged mode: `absent`\n"
        f"- deleted object size: `{object_size}` bytes\n"
        f"- pre-merge HEAD: `{head_mode or 'absent'} {head_blob or 'absent'}`\n"
        f"- official MERGE_HEAD: `{merge_mode or 'absent'} {merge_blob or 'absent'}`\n"
    )
