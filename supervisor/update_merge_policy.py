"""Presentation-only labels for managed-update conflicts.

Git decides whether a merge is clean. Every conflict, regardless of pathname,
goes through the same reviewed assisted resolver. The doc/code/hot split only
helps the resolver and UI describe the plan; it grants or blocks nothing.
"""

from __future__ import annotations

import posixpath
from typing import Dict, List

DOCUMENT_EXACT = frozenset({"README.md"})
DOCUMENT_PREFIXES = ("docs/",)
HOT_CODE_PATHS = frozenset({
    "ouroboros/loop.py",
    "ouroboros/tools/control.py",
    "ouroboros/tools/registry.py",
    "ouroboros/config.py",
    "supervisor/queue.py",
    "supervisor/events.py",
})


def _norm(path: str) -> str:
    normalized = posixpath.normpath(str(path or "").replace("\\", "/"))
    return normalized[2:] if normalized.startswith("./") else normalized.lstrip("/")


def is_document_path(path: str) -> bool:
    p = _norm(path)
    if p in DOCUMENT_EXACT or posixpath.basename(p).upper().startswith("CHANGELOG"):
        return True
    return p.endswith(".md") and any(p.startswith(prefix) for prefix in DOCUMENT_PREFIXES)


def is_hot_code(path: str) -> bool:
    return _norm(path) in HOT_CODE_PATHS


def classify_conflicts(conflict_paths: List[str]) -> Dict[str, object]:
    """Return one route plus presentation labels; filenames never set policy."""
    paths = [str(path).strip() for path in (conflict_paths or []) if str(path).strip()]
    docs = [path for path in paths if is_document_path(path)]
    code = [path for path in paths if path not in docs]
    return {
        "kind": "conflicting" if paths else "clean",
        "doc_conflict_paths": docs,
        "code_conflict_paths": code,
        "hot_code_paths": [path for path in code if is_hot_code(path)],
    }
