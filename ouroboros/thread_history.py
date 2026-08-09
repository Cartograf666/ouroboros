"""ONE thread-ancestry lens shared by the UI history and the agent's context.

A forked thread stores only a CURSOR — ``{fork_of_chat_id, fork_before_ts}`` —
and NEVER a copy of its parent's rows. Reading a fork therefore means reading
its own chat PLUS a bounded slice of every ancestor chat. Two independent
readers need that answer: ``gateway/history.py`` (what the owner sees) and
``ouroboros/context.py`` (what the agent sees). A cursor written into only one
of them would hand the UI and the agent DIFFERENT histories of the same thread,
so both consume the lens built here and nothing else.

Semantics pinned by this module:

* **Inclusive boundary.** An ancestor row is in scope when
  ``row_ts <= fork_before_ts``. ``fork_before_ts`` is stamped at the fork
  moment, so a row bearing exactly that timestamp existed BEFORE the fork.
  Comparison is lexicographic over ISO-8601 UTC strings — the same convention
  the history window already uses for its recency floor. A row with no
  timestamp sorts as oldest and is therefore admitted.
* **Intersected cutoffs for a fork of a fork.** Following the chain, each
  ancestor's effective cutoff is the MOST RESTRICTIVE bound on the path to it:
  a grandchild can never see more of a grandparent than its own parent could.
* **Lifecycle-blind ancestry.** Ancestors resolve whether they are active,
  archived, deleting or tombstoned. Filtering the chain by liveness would
  silently orphan every fork of a deleted thread.
* **Bounded and disclosed.** The walk stops at ``MAX_ANCESTRY_DEPTH`` or on a
  cycle and sets ``truncated`` — the caller discloses it rather than quietly
  serving a short history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# A fork chain deeper than this is pathological (each link is an owner action).
# Hitting it is disclosed, never silently truncated.
MAX_ANCESTRY_DEPTH = 32


def _min_cutoff(left: str, right: str) -> str:
    """Intersect two cutoffs; ``""`` means "unbounded" and always loses."""
    if not left:
        return right
    if not right:
        return left
    return left if left <= right else right


@dataclass(frozen=True)
class ThreadLens:
    """Every chat one thread reads, with each chat's effective cutoff."""

    chat_id: int
    project_id: str = ""
    thread_id: int = 0
    # chat_id -> "" (the whole chat) or the INCLUSIVE ts upper bound.
    cutoffs: Dict[int, str] = field(default_factory=dict)
    # Self first, then ancestors nearest-first (render/disclosure order).
    order: List[int] = field(default_factory=list)
    # Binding-held canonical owner-row refs, bucketed by the LENS chat that owns
    # them: a converted project's start message lives in Main, so a fork of that
    # project's thread would lose it without carrying the ancestor's refs too.
    source_refs: Dict[int, List[Dict[str, Any]]] = field(default_factory=dict)
    truncated: bool = False

    @property
    def chat_ids(self) -> set:
        return set(self.cutoffs)

    @property
    def is_project_thread(self) -> bool:
        return bool(self.project_id)

    @property
    def has_ancestors(self) -> bool:
        return len(self.cutoffs) > 1

    def admits(self, entry_chat: Any, ts: Any = "") -> bool:
        """True when a row of ``entry_chat`` stamped ``ts`` belongs to this thread."""
        try:
            chat = int(entry_chat or 0)
        except (TypeError, ValueError):
            return False
        if chat not in self.cutoffs:
            return False
        cutoff = self.cutoffs[chat]
        return not cutoff or str(ts or "") <= cutoff

    def admits_source_ref(self, entry: Dict[str, Any]) -> bool:
        """True when ``entry`` IS a canonical owner row referenced by a binding
        of this thread or of an in-scope ancestor (and is within that
        ancestor's cutoff)."""
        if not self.source_refs or not isinstance(entry, dict):
            return False
        try:
            from ouroboros.project_dialogue import entry_matches_source_ref
        except Exception:  # pragma: no cover - import guard
            return False
        ts = str(entry.get("ts") or "")
        for owner_chat, refs in self.source_refs.items():
            cutoff = self.cutoffs.get(owner_chat, "")
            if cutoff and ts > cutoff:
                continue
            try:
                if entry_matches_source_ref(entry, refs):
                    return True
            except Exception:
                log.debug("Thread source-ref classification failed", exc_info=True)
        return False


def _source_refs_by_chat(drive_root: Any, chats: set) -> Dict[int, List[Dict[str, Any]]]:
    """Bucket binding-held source refs by project chat, in ONE bindings read.

    ``project_dialogue.source_refs_for_project`` re-reads the bindings file per
    chat; a fork chain would pay that once per ancestor.
    """
    out: Dict[int, List[Dict[str, Any]]] = {}
    if not chats:
        return out
    try:
        from ouroboros.projects_registry import project_task_bindings

        for row in project_task_bindings(drive_root).values():
            ref = row.get("source_ref")
            if not isinstance(ref, dict) or not ref:
                continue
            try:
                owner = int(row.get("project_chat_id") or 0)
            except (TypeError, ValueError):
                continue
            if owner in chats:
                out.setdefault(owner, []).append(dict(ref))
    except Exception:
        log.debug("Failed to bucket project source refs", exc_info=True)
    return out


def thread_ancestry_lens(
    drive_root: Any,
    chat_id: Any,
    *,
    with_source_refs: bool = True,
) -> ThreadLens:
    """Build the lens for ``chat_id`` (see the module docstring for semantics).

    A non-project chat (Main, an external transport) yields a degenerate lens
    over itself alone, so callers can use one code path.
    """
    try:
        cid = int(chat_id or 0)
    except (TypeError, ValueError):
        cid = 0
    try:
        from ouroboros.projects_registry import resolve_chat_binding

        binding = resolve_chat_binding(drive_root, cid)
    except Exception:
        log.debug("thread_ancestry_lens binding lookup failed", exc_info=True)
        binding = {}
    if not binding:
        return ThreadLens(chat_id=cid, cutoffs={cid: ""} if cid else {}, order=[cid] if cid else [])

    cutoffs: Dict[int, str] = {cid: ""}
    order: List[int] = [cid]
    truncated = False
    current: Optional[Dict[str, Any]] = binding
    effective = ""
    depth = 0
    while current is not None:
        thread = _thread_row(current)
        parent_chat, fork_before = _fork_cursor(thread)
        if not parent_chat or not fork_before:
            break
        depth += 1
        if depth > MAX_ANCESTRY_DEPTH:
            truncated = True
            log.warning(
                "Thread ancestry for chat %s exceeds depth %s — older ancestors omitted",
                cid, MAX_ANCESTRY_DEPTH,
            )
            break
        # Intersection: a descendant can never see more of an ancestor than the
        # link it inherited the view through.
        effective = _min_cutoff(effective, fork_before)
        if parent_chat in cutoffs:
            # A cycle (only reachable through hand-edited state): tighten the
            # existing bound and stop rather than loop forever.
            cutoffs[parent_chat] = _min_cutoff(cutoffs[parent_chat], effective)
            truncated = True
            log.warning("Thread ancestry cycle at chat %s — walk stopped", parent_chat)
            break
        cutoffs[parent_chat] = effective
        order.append(parent_chat)
        try:
            from ouroboros.projects_registry import resolve_chat_binding

            # Lifecycle-blind by construction: resolve_chat_binding answers for
            # deleting/tombstoned rows too, so a fork of a deleted thread keeps
            # reading its shared past (A3a).
            current = resolve_chat_binding(drive_root, parent_chat) or None
        except Exception:
            log.debug("thread_ancestry_lens ancestor lookup failed", exc_info=True)
            current = None

    source_refs = (
        _source_refs_by_chat(drive_root, set(cutoffs)) if with_source_refs else {}
    )
    return ThreadLens(
        chat_id=cid,
        project_id=str(binding.get("project_id") or ""),
        thread_id=int(binding.get("thread_id") or 0),
        cutoffs=cutoffs,
        order=order,
        source_refs=source_refs,
        truncated=truncated,
    )


def _thread_row(binding: Dict[str, Any]) -> Dict[str, Any]:
    """The stored thread row behind a binding (``{}`` for thread #0)."""
    project = binding.get("project")
    if not isinstance(project, dict):
        return {}
    try:
        want = int(binding.get("thread_id") or 0)
    except (TypeError, ValueError):
        return {}
    for row in project.get("threads") or ():
        if isinstance(row, dict):
            try:
                if int(row.get("id")) == want:
                    return row
            except (TypeError, ValueError):
                continue
    return {}


def _fork_cursor(thread: Dict[str, Any]) -> tuple:
    try:
        parent = int(thread.get("fork_of_chat_id") or 0)
    except (TypeError, ValueError):
        parent = 0
    return parent, str(thread.get("fork_before_ts") or "")


__all__ = ["MAX_ANCESTRY_DEPTH", "ThreadLens", "thread_ancestry_lens"]
