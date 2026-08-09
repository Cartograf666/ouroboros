"""Durable registry of owner projects (multi-project, v6.32.0).

A project is a durable context the single agent works in: id + name +
per-project memory (``data/projects/<id>/``) + chat thread (its own positive
``chat_id``) + an OPTIONAL working folder (invisible auto-git under the
durable projects root). File-less research projects are valid. Projects are
NEVER age-pruned; the owner curates by archive/delete.

State lives in ``data/state/projects.json`` via the canonical durable-JSON
pattern (mirrors ``subagent_worktrees.py``). Deletion keeps a durable tombstone
so chat history, bindings, memory and the owner folder remain addressable and a
boot reconcile cannot resurrect the room. The registry is data-plane
bookkeeping only — identity, constitution, and evolution stay unified in the
one agent (BIBLE P1).
"""

from __future__ import annotations

import logging
import pathlib
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from ouroboros.contracts.chat_id_policy import MAIN_THREAD_ID, project_chat_id, thread_chat_id
from ouroboros.contracts.schema_versions import with_schema_version
from ouroboros.project_facts import sanitize_project_id
from ouroboros.utils import atomic_write_json, iter_jsonl_objects, read_json_dict, utc_now_iso

log = logging.getLogger(__name__)

_REGISTRY_NAME = "projects.json"
_BINDINGS_NAME = "project_task_bindings.json"
# v6.58.0 (slice 0): projects.json carries an opt-in _schema_version so future
# additive fields (git provenance, trusted_at) migrate deliberately. Old rows read
# as version 0; new fields must stay additive with safe-empty defaults because
# reconcile_projects mints rows that will lack them.
_REGISTRY_SCHEMA_VERSION = 2
# v6.73.0: project_task_bindings.json gains source_text / origin_absent fields.
_BINDINGS_SCHEMA_VERSION = 1
_LOCK = threading.RLock()

PROJECT_NAME_MAX = 80
PROJECT_ACTIVE = "active"
PROJECT_DELETING = "deleting"
PROJECT_TOMBSTONED = "tombstoned"
PROJECT_LIFECYCLES = frozenset({PROJECT_ACTIVE, PROJECT_DELETING, PROJECT_TOMBSTONED})
_DEPRECATED_CHAT_IDS_EVENTS: set[str] = set()

# Threads (project-threads T0). A project row carries an ADDITIVE ``threads: []``
# list of EXTRA threads; thread #0 is never stored — it is projected at read time
# from the project's own chat_id/name/timestamps/revision, and the top-level
# ``chat_id`` stays its compatibility alias. Nothing is rewritten on disk, so a
# legacy row (and any row minted by reconcile) reads as a one-thread project.
THREAD_NAME_MAX = PROJECT_NAME_MAX
# Bound the retry walk when a minted thread chat id is already reserved. Each
# step is a fresh deterministic pre-image, so exhausting this many is a
# registry-wide alarm, not a routine outcome.
_THREAD_ID_MINT_ATTEMPTS = 64
# Registry VERSIONS (path, mtime_ns, size) whose duplicate-chat-id scan already
# ran — the scan is an alarm, not a per-read log flood, but keying it on the
# file version means a collision hand-edited in later is still reported. Bounded
# so a long-lived writer process cannot accumulate one entry per write.
_DUPLICATE_CHAT_ID_REPORTED: set = set()
_DUPLICATE_MEMO_MAX = 64


@contextmanager
def _file_write_lock(target_path: pathlib.Path) -> Iterator[None]:
    """Cross-process exclusive lock for a registry/bindings read-modify-write.

    The registry is written from BOTH the server process (project create/bind,
    digest touch) AND worker processes (``project_journal`` touch_project), so a
    process-local ``threading.Lock`` cannot prevent lost updates. Flock a sidecar
    so the load→modify→atomic-write sequence is exclusive across processes; the
    in-process ``_LOCK`` is nested inside for thread-level serialization too.
    """
    from ouroboros.platform_layer import (
        acquire_exclusive_file_lock,
        release_exclusive_file_lock,
    )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target_path.with_name(target_path.name + ".lock")
    fd = acquire_exclusive_file_lock(lock_path, timeout_sec=4.0)
    if fd is None:
        raise TimeoutError(f"projects_registry: could not lock {lock_path} in time")
    try:
        with _LOCK:
            yield
    finally:
        release_exclusive_file_lock(lock_path, fd)


def _registry_path(drive_root: Any) -> pathlib.Path:
    return pathlib.Path(drive_root) / "state" / _REGISTRY_NAME


def _bindings_path(drive_root: Any) -> pathlib.Path:
    return pathlib.Path(drive_root) / "state" / _BINDINGS_NAME


def _load(drive_root: Any) -> Dict[str, Any]:
    data = read_json_dict(_registry_path(drive_root))
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        return {"projects": []}
    data["projects"] = [
        _normalize_project_row(p)
        for p in data["projects"]
        if isinstance(p, dict) and p.get("id")
    ]
    _report_duplicate_chat_ids(drive_root, data["projects"])
    return data


def _normalize_project_row(value: Dict[str, Any]) -> Dict[str, Any]:
    """Add safe lifecycle/read-cursor defaults without rewriting on read."""
    row = dict(value)
    lifecycle = str(row.get("lifecycle") or PROJECT_ACTIVE).strip().lower()
    row["lifecycle"] = lifecycle if lifecycle in PROJECT_LIFECYCLES else PROJECT_ACTIVE
    for field in ("routing_generation", "visible_revision"):
        try:
            row[field] = max(0, int(row.get(field) or 0))
        except (TypeError, ValueError):
            row[field] = 0
    row["delete_error"] = str(row.get("delete_error") or "")
    row["threads"] = _normalize_thread_rows(row.get("threads"))
    return row


def _normalize_thread_rows(value: Any) -> List[Dict[str, Any]]:
    """Normalize the ADDITIVE extra-thread list of a project row (read-only).

    A legacy row has no ``threads`` key at all and normalizes to ``[]`` — i.e.
    exactly one (projected) thread. Rows are dropped rather than repaired when
    they carry no usable id/chat_id, and thread id ``0`` is never accepted from
    storage: thread #0 is synthesized from the project itself, so a stored
    duplicate would be a second, silently disagreeing truth.
    """
    out: List[Dict[str, Any]] = []
    seen_ids: set = set()
    if not isinstance(value, list):
        return out
    for raw in value:
        if not isinstance(raw, dict):
            continue
        try:
            thread_id = int(raw.get("id"))
            chat_id = int(raw.get("chat_id"))
        except (TypeError, ValueError):
            continue
        if thread_id <= MAIN_THREAD_ID or not chat_id or thread_id in seen_ids:
            continue
        seen_ids.add(thread_id)
        row: Dict[str, Any] = {
            "id": thread_id,
            "chat_id": chat_id,
            "name": str(raw.get("name") or "").strip() or f"Thread {thread_id}",
            "created_at": str(raw.get("created_at") or ""),
        }
        try:
            row["visible_revision"] = max(0, int(raw.get("visible_revision") or 0))
        except (TypeError, ValueError):
            row["visible_revision"] = 0
        # Fork cursor (A3): a pointer into the PARENT's rows, never a copy.
        try:
            fork_of = int(raw.get("fork_of_chat_id") or 0)
        except (TypeError, ValueError):
            fork_of = 0
        fork_before = str(raw.get("fork_before_ts") or "")
        if fork_of and fork_before:
            row["fork_of_chat_id"] = fork_of
            row["fork_before_ts"] = fork_before
        out.append(row)
    return sorted(out, key=lambda r: int(r["id"]))


def project_threads(project: Dict[str, Any]) -> List[Dict[str, Any]]:
    """CANONICAL thread projection of a project row — thread #0 first.

    Thread #0 is SYNTHESIZED from the project's own ``chat_id``/``name``/
    ``created_at``/``visible_revision`` (X7): nothing on disk is rewritten, and
    the top-level ``chat_id`` remains its compatibility alias. Every consumer
    that wants "the threads of this project" must read THIS, never the raw
    ``threads`` list, or it will silently lose the project's main thread.
    """
    if not isinstance(project, dict):
        return []
    pid = str(project.get("id") or "")
    try:
        chat_id = int(project.get("chat_id") or 0)
    except (TypeError, ValueError):
        chat_id = 0
    zero = {
        "id": MAIN_THREAD_ID,
        "chat_id": chat_id or project_chat_id(pid),
        "name": str(project.get("name") or pid),
        "created_at": str(project.get("created_at") or ""),
        "visible_revision": max(0, int(project.get("visible_revision") or 0)),
    }
    return [zero, *_normalize_thread_rows(project.get("threads"))]


def _row_chat_ids(project: Dict[str, Any]) -> List[int]:
    """Every chat id a single project row reserves (thread #0 included)."""
    return [int(thread["chat_id"]) for thread in project_threads(project)]


def _chat_id_owners(projects: List[Dict[str, Any]]) -> Dict[int, List[tuple]]:
    """chat_id -> [(project_id, thread_id), ...] across EVERY lifecycle state.

    Tombstoned rows keep reserving their ids on purpose: a reused chat id would
    silently merge a dead project's history into a live one.
    """
    owners: Dict[int, List[tuple]] = {}
    for project in projects:
        pid = str(project.get("id") or "")
        for thread in project_threads(project):
            owners.setdefault(int(thread["chat_id"]), []).append((pid, int(thread["id"])))
    return owners


def duplicate_chat_ids(drive_root: Any) -> Dict[int, List[tuple]]:
    """Pre-existing chat-id collisions in the registry (X1 load-time detection).

    Returns only ids claimed by more than one (project, thread) pair. A healthy
    registry returns ``{}``; anything else means two conversations would share
    one history stream and must be surfaced, never silently tolerated.
    """
    with _LOCK:
        projects = _load(drive_root)["projects"]
    return {cid: owners for cid, owners in _chat_id_owners(projects).items() if len(owners) > 1}


def _report_duplicate_chat_ids(drive_root: Any, projects: List[Dict[str, Any]]) -> None:
    """Loudly report duplicates once per registry VERSION per drive root.

    Called from ``_load`` so a corrupt registry cannot stay quiet; deliberately
    non-raising, because refusing to load the registry would take the whole
    server down over data that is still individually readable. The memo is
    keyed on the file's (mtime, size) rather than the root alone, so a
    hand-edited collision introduced AFTER the first load is still reported
    instead of hiding behind a once-per-process flag.
    """
    path = _registry_path(drive_root)
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (str(path), 0, 0)
    if key in _DUPLICATE_CHAT_ID_REPORTED:
        return
    clashes = {cid: owners for cid, owners in _chat_id_owners(projects).items() if len(owners) > 1}
    if len(_DUPLICATE_CHAT_ID_REPORTED) >= _DUPLICATE_MEMO_MAX:
        _DUPLICATE_CHAT_ID_REPORTED.clear()  # bounded: at worst one extra scan
    _DUPLICATE_CHAT_ID_REPORTED.add(key)
    if not clashes:
        return
    log.error(
        "Project registry chat-id COLLISION: %s — these conversations share one "
        "history stream; rename/recreate one of them",
        {cid: owners for cid, owners in sorted(clashes.items())},
    )
    try:
        from ouroboros.utils import append_jsonl

        append_jsonl(
            pathlib.Path(drive_root) / "logs" / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "project_chat_id_collision_detected",
                "collisions": {str(cid): owners for cid, owners in sorted(clashes.items())},
            },
        )
    except Exception:
        log.debug("Failed to record chat-id collision event", exc_info=True)


def _validated_name(value: Any, fallback: str = "") -> str:
    name = str(value or "").strip() or str(fallback or "").strip()
    if len(name) > PROJECT_NAME_MAX:
        raise ValueError(f"project name must be <= {PROJECT_NAME_MAX} characters")
    return name


def _save(drive_root: Any, data: Dict[str, Any]) -> None:
    path = _registry_path(drive_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Stamp the current schema version on every write (idempotent; old files that
    # never had it are treated as version 0 by read_schema_version).
    atomic_write_json(path, with_schema_version(dict(data), _REGISTRY_SCHEMA_VERSION))


def _load_bindings(drive_root: Any) -> Dict[str, Any]:
    data = read_json_dict(_bindings_path(drive_root))
    if not isinstance(data, dict) or not isinstance(data.get("bindings"), dict):
        return {"bindings": {}}
    return data


def _save_bindings(drive_root: Any, data: Dict[str, Any]) -> None:
    path = _bindings_path(drive_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # v6.73.0: bindings carry an opt-in _schema_version (legacy files read as 0).
    atomic_write_json(path, with_schema_version(dict(data), _BINDINGS_SCHEMA_VERSION))


# Closed enum of typed origin-absence reasons. ``producer_missing_ref`` is the
# truthful signal for a chat-born event whose producer failed to attach the ref
# (a grep-able producer bug, never a silent default). Upgrade-window note: tasks
# QUEUED before v6.73.0 predate the ingress capture, so their post-upgrade
# promotes legitimately land here until the pre-upgrade queue drains.
# NB: headless tasks are project-SCOPED but never project-BOUND (benchmark
# constraint), so the enum deliberately has no 'headless' member — it stays an
# honest map of reasons that actually have producers.
ORIGIN_ABSENT_REASONS = frozenset({
    "system",
    "mid_task_no_origin",
    "post_hoc_unresolved",
    "producer_missing_ref",
})

_ORIGIN_REF_KEYS = ("chat_id", "client_message_id", "ts", "text_sha256")


def _validated_origin(origin: Any, resolved_chat: int) -> Dict[str, Any]:
    """Validate the REQUIRED typed origin of a binding; raise ValueError otherwise.

    Content-derived identity lookups are forbidden (DEVELOPMENT.md anti-pattern):
    the caller must pass the origin ref BY VALUE (captured at chat ingress) or a
    typed absence reason. ``text`` is required exactly when the origin lives in a
    DIFFERENT chat than the project room (cross-thread projection needs the
    retention-proof copy); a same-room origin renders natively and stores no copy.
    """
    if not isinstance(origin, dict) or ("ref" in origin) == ("absent" in origin):
        raise ValueError(
            "bind_task_to_project requires origin={'ref': {...}, 'text': ...} for a "
            "chat-born binding or origin={'absent': <reason>} — exactly one of 'ref'/'absent'"
        )
    if "absent" in origin:
        reason = str(origin.get("absent") or "")
        if reason not in ORIGIN_ABSENT_REASONS:
            raise ValueError(
                f"invalid origin absence reason {reason!r}; expected one of {sorted(ORIGIN_ABSENT_REASONS)}"
            )
        return {"origin_absent": reason}
    ref = origin.get("ref")
    if not isinstance(ref, dict) or any(ref.get(key) in (None, "") for key in _ORIGIN_REF_KEYS):
        raise ValueError(f"origin['ref'] must carry non-empty {_ORIGIN_REF_KEYS}")
    clean_ref = {key: ref.get(key) for key in _ORIGIN_REF_KEYS}
    try:
        cross_thread = int(clean_ref.get("chat_id") or 0) != int(resolved_chat or 0)
    except (TypeError, ValueError):
        cross_thread = True
    text = origin.get("text")
    if not cross_thread:
        return {"source_ref": clean_ref}
    if not isinstance(text, str) or not text.strip():
        raise ValueError(
            "origin['text'] (the full original message) is required for a cross-thread "
            "origin — it is the retention-proof copy the Project lens projects"
        )
    from ouroboros.project_dialogue import _text_sha256

    if _text_sha256(text) != str(clean_ref.get("text_sha256") or ""):
        raise ValueError("origin['text'] does not match origin['ref']['text_sha256'] (integrity check)")
    return {"source_ref": clean_ref, "source_text": text}


def bind_task_to_project(
    drive_root: Any,
    task_id: str,
    project_id: str,
    chat_id: Any = None,
    *,
    origin: Dict[str, Any],
) -> Dict[str, Any]:
    """Durably bind an existing task/live card to a project thread.

    This is the post-hoc "Turn into project" bridge: old audit logs remain in
    their original files, while history/live routing can resolve the task's
    project chat from this lightweight binding.

    ``origin`` is REQUIRED and typed (see ``_validated_origin``): either the
    owner-message ref captured at chat ingress (+full text for a cross-thread
    origin) or a closed-enum absence reason. A same-task same-project re-bind
    that supplies a valid ref UPGRADES a ref-less existing row (one-way
    enrichment); an existing valid ref is never changed.
    """
    tid = str(task_id or "").strip()
    pid = sanitize_project_id(project_id)
    if not tid:
        raise ValueError("task_id is required")
    if not pid:
        raise ValueError(f"unusable project id: {project_id!r}")
    if get_reserved_project(drive_root, pid) is None:
        create_project(drive_root, pid)
    # Linearize admission with the lifecycle fence. Holding the registry lock
    # through the short bindings append means begin_project_deletion either lands
    # before this bind (which is refused) or after it (which cancellation sees).
    with _file_write_lock(_registry_path(drive_root)):
        project = next(
            (row for row in _load(drive_root)["projects"] if row.get("id") == pid),
            None,
        )
        if not isinstance(project, dict) or project.get("lifecycle") != PROJECT_ACTIVE:
            lifecycle = project.get("lifecycle") if isinstance(project, dict) else "missing"
            raise ValueError(f"project {pid!r} is {lifecycle}; it cannot accept bindings")
        try:
            resolved_chat = int(chat_id if chat_id is not None else project.get("chat_id"))
        except (TypeError, ValueError):
            resolved_chat = project_chat_id(pid)
        origin_fields = _validated_origin(origin, resolved_chat)
        row = {
            "task_id": tid,
            "project_id": pid,
            "project_chat_id": resolved_chat,
            "bound_at": utc_now_iso(),
            **origin_fields,
        }
        with _file_write_lock(_bindings_path(drive_root)):
            data = _load_bindings(drive_root)
            existing = data["bindings"].get(tid)
            if isinstance(existing, dict):
                existing_pid = str(existing.get("project_id") or "")
                if existing_pid == pid:
                    # One-way enrichment: fill a ref-less row when a valid ref
                    # arrives; a stored valid ref is immutable (never replaced).
                    if not isinstance(existing.get("source_ref"), dict) and "source_ref" in origin_fields:
                        enriched = {
                            key: value for key, value in existing.items() if key != "origin_absent"
                        }
                        enriched.update(origin_fields)
                        data["bindings"][tid] = enriched
                        _save_bindings(drive_root, data)
                        return dict(enriched)
                    return dict(existing)
                raise ValueError(
                    f"task {tid!r} is already bound to project {existing_pid!r}; "
                    "project binding is immutable"
                )
            data["bindings"][tid] = row
            _save_bindings(drive_root, data)
    touch_project(drive_root, pid)
    return dict(row)


def project_task_bindings(drive_root: Any) -> Dict[str, Dict[str, Any]]:
    """Copy of the immutable task-to-Project bindings for read models."""
    return {
        str(task_id): dict(row)
        for task_id, row in _load_bindings(drive_root).get("bindings", {}).items()
        if isinstance(row, dict)
    }


def all_task_bindings(drive_root: Any) -> Dict[str, int]:
    """Map task_id -> project chat_id for ALL post-hoc 'Turn into project' bindings.

    Cognition/history isolation consults this so a bound task's rows (which keep
    their ORIGINAL main chat_id) are still treated as project-owned. One bounded
    read; no per-row lock (atomic writes guarantee complete reads)."""
    out: Dict[str, int] = {}
    try:
        for tid, row in _load_bindings(drive_root).get("bindings", {}).items():
            if not isinstance(row, dict):
                continue
            try:
                cid = int(row.get("project_chat_id") or 0)
            except (TypeError, ValueError):
                continue
            if cid:
                out[str(tid)] = cid
    except Exception:
        log.debug("all_task_bindings failed", exc_info=True)
    return out


def all_task_project_bindings(drive_root: Any) -> Dict[str, Dict[str, Any]]:
    """Map task_id -> {project_id, chat_id} for ALL post-hoc 'Turn into project'
    bindings. Richer than all_task_bindings (chat-id only): the UI uses project_id
    to turn a bound main-chat card into a pointer that opens the project panel
    (F4), not merely to suppress the stray convert button (P2). Never raises."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        for tid, row in _load_bindings(drive_root).get("bindings", {}).items():
            if not isinstance(row, dict):
                continue
            pid = str(row.get("project_id") or "").strip()
            try:
                cid = int(row.get("project_chat_id") or 0)
            except (TypeError, ValueError):
                cid = 0
            if pid and cid:
                out[str(tid)] = {"project_id": pid, "chat_id": cid}
    except Exception:
        log.debug("all_task_project_bindings failed", exc_info=True)
    return out


def project_binding_for_task(drive_root: Any, task_id: str) -> Optional[Dict[str, Any]]:
    tid = str(task_id or "").strip()
    if not tid:
        return None
    # Read needs no lock: atomic_write_json renames into place, so a reader
    # always sees a complete (old or new) bindings file, never a torn one.
    row = _load_bindings(drive_root)["bindings"].get(tid)
    return dict(row) if isinstance(row, dict) else None


def project_chat_for_task(drive_root: Any, task_id: str) -> int:
    row = project_binding_for_task(drive_root, task_id)
    if not row:
        return 0
    try:
        return int(row.get("project_chat_id") or 0)
    except (TypeError, ValueError):
        return 0


def project_chat_for_task_tree(
    drive_root: Any, task_id: Any, parent_task_id: Any = "", root_task_id: Any = ""
) -> int:
    """Resolve the project chat for a task by its TASK TREE: the task's OWN binding
    wins; else inherit from its parent; else its root. A subagent is never bound
    itself, so this is how its live frames + history are recognized as belonging to
    its root's project and route to the project thread instead of staying in the main
    chat (the cyber-racing "subagents vanished from the project" gap). Membership is
    DERIVED from lineage — no per-child binding store, one SSOT."""
    for tid in (task_id, parent_task_id, root_task_id):
        tid = str(tid or "").strip()
        if not tid:
            continue
        chat = project_chat_for_task(drive_root, tid)
        if chat:
            return chat
    return 0


def list_reserved_projects(drive_root: Any) -> List[Dict[str, Any]]:
    """All Project ids, including deleting/tombstoned history reservations."""
    with _LOCK:
        projects = _load(drive_root)["projects"]
    return sorted(
        projects,
        key=lambda p: str(p.get("last_active_at") or p.get("updated_at") or p.get("created_at") or ""),
        reverse=True,
    )


def list_projects(drive_root: Any) -> List[Dict[str, Any]]:
    """Active, routable Projects (most recently active first)."""
    return [
        project for project in list_reserved_projects(drive_root)
        if project.get("lifecycle") == PROJECT_ACTIVE
    ]


def list_sidebar_projects(drive_root: Any) -> List[Dict[str, Any]]:
    """Projects visible while active or while deletion is quiescing."""
    return [
        project for project in list_reserved_projects(drive_root)
        if project.get("lifecycle") in {PROJECT_ACTIVE, PROJECT_DELETING}
    ]


def reserved_project_chat_ids(drive_root: Any) -> set:
    """The set of chat_ids reserved by every Project lifecycle state.

    The TRUTH source for "is this chat a project thread" — a bare numeric range
    cannot disambiguate from large external-transport (e.g. Telegram) chat ids,
    so routing/history/UI classify by registry membership instead.

    NOT an isolation boundary (full project awareness, v6.32.0): the one identity
    sees ALL threads in its unified memory. This classifier drives (a) the UI
    history/fan-out partition that organizes threads into panels, (b) message
    routing, and (c) the project TASK's FOCUSED passive context (build_recent_
    sections shows the task its own thread).

    Covers EVERY thread of every project (thread #0 included, via the canonical
    projection) — one widening makes threads visible to history, ``/api/state``
    and the agent's context at once.
    """
    out = set()
    try:
        for project in list_reserved_projects(drive_root):
            try:
                out.update(_row_chat_ids(project))
            except (TypeError, ValueError):
                continue
    except Exception:
        log.debug("reserved_project_chat_ids failed", exc_info=True)
    out.discard(0)
    return out


def registered_project_chat_ids(drive_root: Any) -> set:
    """One-minor compatibility alias for :func:`reserved_project_chat_ids`."""
    key = str(pathlib.Path(drive_root).resolve(strict=False))
    if key not in _DEPRECATED_CHAT_IDS_EVENTS:
        _DEPRECATED_CHAT_IDS_EVENTS.add(key)
        try:
            from ouroboros.utils import append_jsonl

            append_jsonl(
                pathlib.Path(drive_root) / "logs" / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "deprecated_project_chat_ids_alias_used",
                    "alias": "registered_project_chat_ids",
                    "replacement": "reserved_project_chat_ids",
                },
            )
        except Exception:
            log.debug("Failed to record Project chat-id alias use", exc_info=True)
    return reserved_project_chat_ids(drive_root)


def get_project(drive_root: Any, project_id: str) -> Optional[Dict[str, Any]]:
    pid = sanitize_project_id(project_id)
    if not pid:
        return None
    for project in list_projects(drive_root):
        if project.get("id") == pid:
            return dict(project)
    return None


def get_reserved_project(drive_root: Any, project_id: str) -> Optional[Dict[str, Any]]:
    """Lookup irrespective of lifecycle (history/recovery only)."""
    pid = sanitize_project_id(project_id)
    if not pid:
        return None
    for project in list_reserved_projects(drive_root):
        if project.get("id") == pid:
            return dict(project)
    return None


# chat_id -> binding index, memoized on the registry file's (mtime_ns, size).
# atomic_write_json renames into place, so any mutation changes at least one of
# those; a stale entry is therefore not reachable. C4: "which project/thread owns
# this chat" is asked once per inbound message and per history request, and with
# threads the naive scan is projects x threads.
_CHAT_BINDING_INDEX: Dict[str, tuple] = {}


def _chat_binding_index(drive_root: Any) -> Dict[int, Dict[str, Any]]:
    path = _registry_path(drive_root)
    try:
        stat = path.stat()
        stamp: tuple = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = (0, 0)
    key = str(path)
    cached = _CHAT_BINDING_INDEX.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    index: Dict[int, Dict[str, Any]] = {}
    for project in list_reserved_projects(drive_root):
        pid = str(project.get("id") or "")
        for thread in project_threads(project):
            index.setdefault(int(thread["chat_id"]), {
                "project_id": pid,
                "thread_id": int(thread["id"]),
                "chat_id": int(thread["chat_id"]),
                "lifecycle": str(project.get("lifecycle") or PROJECT_ACTIVE),
                "name": str(thread.get("name") or ""),
                "project": dict(project),
            })
    _CHAT_BINDING_INDEX[key] = (stamp, index)
    return index


def resolve_chat_binding(drive_root: Any, chat_id: Any) -> Dict[str, Any]:
    """THE canonical "who owns this chat id" lookup (R3).

    Returns ``{project_id, thread_id, chat_id, lifecycle, name, project}`` for
    ANY thread of ANY project in ANY lifecycle state, or ``{}`` for the main
    chat / an external transport id. Callers that must not resurrect a fenced
    room filter on ``lifecycle``; they must NOT compare a chat id against
    ``project["chat_id"]`` themselves — that comparison sees thread #0 only and
    misroutes every other thread to Main.
    """
    try:
        cid = int(chat_id or 0)
    except (TypeError, ValueError):
        return {}
    if not cid:
        return {}
    try:
        row = _chat_binding_index(drive_root).get(cid)
    except Exception:
        log.debug("resolve_chat_binding failed", exc_info=True)
        return {}
    return dict(row) if row else {}


def get_thread(drive_root: Any, project_id: str, thread_id: Any) -> Optional[Dict[str, Any]]:
    """One thread of a project by id (thread #0 included), else ``None``."""
    project = get_reserved_project(drive_root, project_id)
    if project is None:
        return None
    try:
        want = int(thread_id)
    except (TypeError, ValueError):
        return None
    for thread in project_threads(project):
        if int(thread["id"]) == want:
            return dict(thread)
    return None


def _mint_thread(data: Dict[str, Any], pid: str, existing: List[Dict[str, Any]]) -> tuple:
    """Pick the next free ``(thread_id, chat_id)`` pair for project ``pid``.

    Thread ids are opaque integers, so a chat-id collision is resolved by simply
    walking to the next id (X1's "retry thread ids on collision") — no allocator
    state, no widened hash. Raises when the walk is exhausted, which is a
    registry-wide alarm rather than a routine outcome.
    """
    reserved = _chat_id_owners(data["projects"])
    next_id = max((int(row["id"]) for row in existing), default=MAIN_THREAD_ID) + 1
    for candidate in range(next_id, next_id + _THREAD_ID_MINT_ATTEMPTS):
        chat_id = thread_chat_id(pid, candidate)
        if chat_id not in reserved:
            return candidate, chat_id
    raise ValueError(
        f"could not mint a free thread chat id for project {pid!r} after "
        f"{_THREAD_ID_MINT_ATTEMPTS} attempts — the registry has a chat-id collision storm"
    )


def _active_project_row(data: Dict[str, Any], pid: str) -> Dict[str, Any]:
    for entry in data["projects"]:
        if entry.get("id") == pid:
            if entry.get("lifecycle") != PROJECT_ACTIVE:
                raise ValueError(
                    f"project {pid!r} is {entry.get('lifecycle')}; it cannot accept thread changes"
                )
            return entry
    raise ValueError(f"unknown project: {pid!r}")


def create_thread(
    drive_root: Any,
    project_id: str,
    *,
    name: str = "",
    fork_of_chat_id: int = 0,
    fork_before_ts: str = "",
) -> Dict[str, Any]:
    """Append a NEW thread to a project and return its canonical row.

    A thread is an empty chat sharing the project's working folder (A2). The
    fork variant stores only a CURSOR ``{fork_of_chat_id, fork_before_ts}``
    (A3) — no history row is copied, so the parent keeps one row identity, one
    consolidation and one rotation cost. Prefer :func:`fork_thread` for forks;
    this is the primitive both paths share.
    """
    pid = sanitize_project_id(project_id)
    if not pid:
        raise ValueError(f"unusable project id: {project_id!r}")
    title = _validated_name(name, "New thread")
    with _file_write_lock(_registry_path(drive_root)):
        data = _load(drive_root)
        entry = _active_project_row(data, pid)
        threads = _normalize_thread_rows(entry.get("threads"))
        thread_id, chat_id = _mint_thread(data, pid, threads)
        row: Dict[str, Any] = {
            "id": thread_id,
            "chat_id": chat_id,
            "name": title,
            "created_at": utc_now_iso(),
            "visible_revision": 0,
        }
        if fork_of_chat_id and fork_before_ts:
            row["fork_of_chat_id"] = int(fork_of_chat_id)
            row["fork_before_ts"] = str(fork_before_ts)
        entry["threads"] = [*threads, row]
        _save(drive_root, data)
        log.info("Project thread created: %s#%s (chat_id=%s)", pid, thread_id, chat_id)
        return dict(row)


def rename_thread(drive_root: Any, project_id: str, thread_id: Any, name: str) -> Optional[Dict[str, Any]]:
    """Rename a thread. Thread #0 IS the project, so renaming it renames the
    project row itself — the projection would otherwise show a name the sidebar
    never persists."""
    pid = sanitize_project_id(project_id)
    if not pid:
        return None
    try:
        want = int(thread_id)
    except (TypeError, ValueError):
        return None
    title = _validated_name(name)
    if not title:
        raise ValueError("thread name is required")
    if want == MAIN_THREAD_ID:
        updated = update_project(drive_root, pid, name=title)
        return project_threads(updated)[0] if updated else None
    with _file_write_lock(_registry_path(drive_root)):
        data = _load(drive_root)
        entry = _active_project_row(data, pid)
        threads = _normalize_thread_rows(entry.get("threads"))
        for row in threads:
            if int(row["id"]) == want:
                row["name"] = title
                entry["threads"] = threads
                _save(drive_root, data)
                return dict(row)
    return None


def fork_thread(drive_root: Any, project_id: str, thread_id: Any) -> Dict[str, Any]:
    """Fork a thread: a new thread carrying a CURSOR into the source's rows.

    The source is untouched and keeps every row (A3). The cursor reads the
    parent's rows REGARDLESS of the parent later being archived or deleted
    (A3a), so a fork can never be orphaned. Auto-name is the plain English
    ``Copy of …`` with NO model call (D2).
    """
    source = get_thread(drive_root, project_id, thread_id)
    if source is None:
        raise ValueError(f"unknown thread {thread_id!r} in project {project_id!r}")
    label = str(source.get("name") or "").strip()
    auto = f"Copy of {label}" if label else "Copy of thread"
    return create_thread(
        drive_root,
        project_id,
        name=auto[:THREAD_NAME_MAX],
        fork_of_chat_id=int(source["chat_id"]),
        # The fork moment. History treats it INCLUSIVELY (``ts <= cutoff``):
        # a parent row stamped at exactly this instant existed before the fork.
        fork_before_ts=utc_now_iso(),
    )


def create_project(
    drive_root: Any,
    project_id: str,
    *,
    name: str = "",
    working_dir: str = "",
    origin: str = "owner",
) -> Dict[str, Any]:
    """Register (or idempotently return) a project entry.

    ``working_dir`` is optional — file-less projects (research, presentations
    drafted in chat) are first-class. The per-project chat id is derived
    deterministically from the id (one allocator-free SSOT).
    """
    pid = sanitize_project_id(project_id)
    if not pid:
        raise ValueError(f"unusable project id: {project_id!r}")
    with _file_write_lock(_registry_path(drive_root)):
        data = _load(drive_root)
        for existing in data["projects"]:
            if existing.get("id") == pid:
                if existing.get("lifecycle") != PROJECT_ACTIVE:
                    raise ValueError(
                        f"project id {pid!r} is permanently reserved by a "
                        f"{existing.get('lifecycle')} project"
                    )
                return dict(existing)
        # Registry-WIDE chat-id reservation (X1). A project's chat id is
        # deterministic from its id, so a collision cannot be retried away —
        # refuse loudly instead of silently merging two histories. Every
        # creation path funnels through here under the same file lock.
        chat_id = project_chat_id(pid)
        clash = _chat_id_owners(data["projects"]).get(chat_id)
        if clash:
            raise ValueError(
                f"chat id {chat_id} for project {pid!r} is already reserved by "
                f"{clash} — pick a different project id"
            )
        entry = {
            "id": pid,
            "name": _validated_name(name, pid),
            "chat_id": chat_id,
            "working_dir": str(working_dir or "").strip(),
            "origin": str(origin or "owner"),
            "created_at": utc_now_iso(),
            "last_active_at": utc_now_iso(),
            "lifecycle": PROJECT_ACTIVE,
            "routing_generation": 0,
            "visible_revision": 0,
            "delete_error": "",
        }
        data["projects"].append(entry)
        _save(drive_root, data)
        log.info("Project registered: %s (chat_id=%s)", pid, entry["chat_id"])
        return dict(entry)


def update_project(drive_root: Any, project_id: str, **updates: Any) -> Optional[Dict[str, Any]]:
    """Update mutable fields. v6.59.0 adds the additive source-provenance facts:
    ``provenance`` (attached|cloned|genesis|none — how the working_dir came to be),
    ``clone_url`` (historical fact; live git data is always read from .git), and
    ``trusted_at`` (stamped automatically on attach/clone — the notification trust
    model: attaching IS the owner's explicit grant, no second confirmation gate)."""
    pid = sanitize_project_id(project_id)
    if not pid:
        return None
    allowed = {
        "name", "working_dir", "last_active_at", "provenance", "clone_url", "trusted_at",
        # Write-once legacy-activity fact seeded by the boot-reconcile backfill
        # (_backfill_thread_activity); read by projects_summary's derivation.
        "thread_activity_seen",
    }
    with _file_write_lock(_registry_path(drive_root)):
        data = _load(drive_root)
        for entry in data["projects"]:
            if entry.get("id") != pid or entry.get("lifecycle") != PROJECT_ACTIVE:
                continue
            for key, value in updates.items():
                if key not in allowed:
                    continue
                if key == "name":
                    value = _validated_name(value, str(entry.get("id") or ""))
                entry[key] = value
            _save(drive_root, data)
            return dict(entry)
    return None


def begin_project_deletion(drive_root: Any, project_id: str) -> Optional[Dict[str, Any]]:
    """Close admission/routing before the supervisor cancels the live subtree."""
    pid = sanitize_project_id(project_id)
    if not pid:
        return None
    with _file_write_lock(_registry_path(drive_root)):
        data = _load(drive_root)
        for entry in data["projects"]:
            if entry.get("id") != pid:
                continue
            if entry.get("lifecycle") in {PROJECT_DELETING, PROJECT_TOMBSTONED}:
                return dict(entry)
            entry["lifecycle"] = PROJECT_DELETING
            entry["routing_generation"] = int(entry.get("routing_generation") or 0) + 1
            entry["admission_closed_at"] = utc_now_iso()
            entry["deleting_at"] = entry["admission_closed_at"]
            entry["delete_error"] = ""
            _save(drive_root, data)
            return dict(entry)
    return None


def fail_project_deletion(
    drive_root: Any, project_id: str, error: str
) -> Optional[Dict[str, Any]]:
    """Keep a fenced Project recoverably deleting while quiescence is pending."""
    pid = sanitize_project_id(project_id)
    if not pid:
        return None
    with _file_write_lock(_registry_path(drive_root)):
        data = _load(drive_root)
        for entry in data["projects"]:
            if entry.get("id") == pid and entry.get("lifecycle") == PROJECT_DELETING:
                entry["delete_error"] = str(error or "deletion did not quiesce")[:2000]
                _save(drive_root, data)
                return dict(entry)
    return None


def complete_project_deletion(drive_root: Any, project_id: str) -> Optional[Dict[str, Any]]:
    """Commit the tombstone after the supervisor proves subtree quiescence."""
    pid = sanitize_project_id(project_id)
    if not pid:
        return None
    with _file_write_lock(_registry_path(drive_root)):
        data = _load(drive_root)
        for entry in data["projects"]:
            if entry.get("id") != pid:
                continue
            if entry.get("lifecycle") == PROJECT_TOMBSTONED:
                return dict(entry)
            if entry.get("lifecycle") != PROJECT_DELETING:
                raise ValueError(f"project {pid!r} is not deleting")
            entry["lifecycle"] = PROJECT_TOMBSTONED
            entry["tombstoned_at"] = utc_now_iso()
            entry["delete_error"] = ""
            _save(drive_root, data)
            log.info(
                "Project tombstoned: %s (history, bindings, folder and memory preserved)",
                pid,
            )
            return dict(entry)
    return None


def delete_project(drive_root: Any, project_id: str) -> bool:
    """Compatibility completion; live deletion must first erect its queue fence."""
    row = get_reserved_project(drive_root, project_id)
    if row is None:
        return False
    if row.get("lifecycle") == PROJECT_TOMBSTONED:
        return True
    if row.get("lifecycle") != PROJECT_DELETING:
        raise RuntimeError("live Project deletion requires cancellation/quiescence first")
    complete_project_deletion(drive_root, project_id)
    return True


def increment_project_visible_revision(
    drive_root: Any,
    *,
    project_id: str = "",
    chat_id: Any = 0,
) -> Optional[Dict[str, Any]]:
    """Advance unread state for one newly-appended owner-visible canonical row.

    A row appended to a NON-primary thread advances that thread's own counter
    AND the project's aggregate: the project counter is what today's flat
    ``project_seen_revision`` cursor compares against, so leaving it untouched
    would make every non-primary thread's activity silently unread-invisible.
    (The per-thread counter is the number T1's nested cursor will read.)
    """
    pid = sanitize_project_id(project_id)
    try:
        cid = int(chat_id or 0)
    except (TypeError, ValueError):
        cid = 0
    if not pid and not cid:
        return None
    with _file_write_lock(_registry_path(drive_root)):
        data = _load(drive_root)
        for entry in data["projects"]:
            if entry.get("lifecycle") != PROJECT_ACTIVE:
                continue
            thread_hit = None
            if cid:
                thread_hit = next(
                    (t for t in project_threads(entry) if int(t["chat_id"]) == cid), None
                )
            if not ((pid and entry.get("id") == pid) or thread_hit is not None):
                continue
            entry["visible_revision"] = int(entry.get("visible_revision") or 0) + 1
            if thread_hit is not None and int(thread_hit["id"]) != MAIN_THREAD_ID:
                threads = _normalize_thread_rows(entry.get("threads"))
                for row in threads:
                    if int(row["id"]) == int(thread_hit["id"]):
                        row["visible_revision"] = int(row.get("visible_revision") or 0) + 1
                entry["threads"] = threads
            _save(drive_root, data)
            return dict(entry)
    return None


def touch_project(drive_root: Any, project_id: str) -> None:
    """Record activity (never raises)."""
    try:
        update_project(drive_root, project_id, last_active_at=utc_now_iso())
    except Exception:
        log.debug("touch_project failed for %s", project_id, exc_info=True)


def reconcile_projects(drive_root: Any) -> int:
    """Boot reconcile: register projects whose memory store exists but whose
    registry row is missing (e.g. created before the registry shipped, or a
    workspace-derived ``proj_<hash>`` store). NEVER prunes — durable project
    dirs outlive any registry accident.
    """
    added = 0
    try:
        projects_root = pathlib.Path(drive_root) / "projects"
        if projects_root.is_dir():
            with _file_write_lock(_registry_path(drive_root)):
                data = _load(drive_root)
                known = {p.get("id") for p in data["projects"]}
                reserved = _chat_id_owners(data["projects"])
                for entry in sorted(projects_root.iterdir()):
                    if not entry.is_dir() or entry.name.startswith("."):
                        continue
                    pid = sanitize_project_id(entry.name)
                    if not pid or pid in known:
                        continue
                    # Same registry-wide reservation invariant as create_project
                    # (X1): reconcile mints hashed ids too, so an unchecked
                    # append here could collide with an existing project OR
                    # thread. Skip loudly — a reconcile must never merge two
                    # histories, and the store stays on disk for the owner.
                    chat_id = project_chat_id(pid)
                    if chat_id in reserved:
                        log.error(
                            "Project reconcile SKIPPED %s: chat id %s already reserved by %s",
                            pid, chat_id, reserved[chat_id],
                        )
                        continue
                    reserved[chat_id] = [(pid, MAIN_THREAD_ID)]
                    data["projects"].append({
                        "id": pid,
                        "name": pid,
                        "chat_id": chat_id,
                        "working_dir": "",
                        "origin": "reconcile",
                        "created_at": utc_now_iso(),
                        "last_active_at": utc_now_iso(),
                        "lifecycle": PROJECT_ACTIVE,
                        "routing_generation": 0,
                        "visible_revision": 0,
                        "delete_error": "",
                    })
                    known.add(pid)
                    added += 1
                if added:
                    _save(drive_root, data)
                    log.info("Project registry reconcile: %d store(s) registered", added)
    except Exception:
        log.warning("Project registry reconcile failed", exc_info=True)
    _backfill_thread_activity(drive_root)
    return added


# Drive roots whose legacy thread-activity backfill already ran in this process.
# The scan is a boot-reconcile concern: once per process per root is enough,
# because everything AFTER the backfill is covered by the registry-facts
# derivation in projects_summary (visible_revision/bindings/origin).
_ACTIVITY_BACKFILL_DONE: set = set()


def _backfill_thread_activity(drive_root: Any) -> int:
    """One-time archive-aware seeding of the durable ``thread_activity_seen`` flag.

    ``projects_summary`` derives thread activity from registry facts alone
    (origin, bindings, ``visible_revision``) — but a legacy project whose
    activity predates the ``visible_revision`` counter would read inactive
    forever. So the boot reconcile scans live + archived chat/progress logs
    ONCE per process for a row carrying each such project's chat_id, and
    persists a write-once flag through the registry's own write path
    (``update_project``). This never runs on the GET path, never removes the
    flag, and a scan that finds nothing simply leaves the project inactive.
    The done-marker is set only AFTER a successful scan/write pass, so a
    transiently failed backfill retries on the next reconcile tick instead of
    silently waiting for a process restart.
    """
    key = str(pathlib.Path(drive_root).resolve(strict=False))
    if key in _ACTIVITY_BACKFILL_DONE:
        return 0
    flagged = 0
    try:
        bindings = _load_bindings(drive_root).get("bindings", {})
        bound_pids = {
            str(row.get("project_id") or "")
            for row in bindings.values()
            if isinstance(row, dict)
        }
        candidates: Dict[int, str] = {}
        with _LOCK:
            projects = _load(drive_root)["projects"]
        for project in projects:
            # update_project persists only ACTIVE rows; deleting/tombstoned
            # projects keep deriving from origin/bindings/visible_revision.
            if project.get("lifecycle") != PROJECT_ACTIVE:
                continue
            if project.get("thread_activity_seen"):
                continue
            pid = str(project.get("id") or "")
            if (
                not pid
                or str(project.get("origin") or "") == "owner_ui"
                or int(project.get("visible_revision") or 0) > 0
                or pid in bound_pids
            ):
                continue  # already active by derivation — no flag needed
            try:
                cid = int(project.get("chat_id") or 0)
            except (TypeError, ValueError):
                cid = 0
            if cid:
                candidates[cid] = pid
        if not candidates:
            _ACTIVITY_BACKFILL_DONE.add(key)
            return 0
        logs_dir = pathlib.Path(drive_root) / "logs"
        archive_dir = pathlib.Path(drive_root) / "archive"
        paths = [logs_dir / "chat.jsonl", logs_dir / "progress.jsonl"]
        if archive_dir.is_dir():
            paths.extend(sorted(archive_dir.glob("chat_*.jsonl"), reverse=True))
            paths.extend(sorted(archive_dir.glob("progress_*.jsonl"), reverse=True))
        seen: set = set()
        for path in paths:
            if len(seen) == len(candidates):
                break
            if not path.is_file():
                continue
            try:
                for row in iter_jsonl_objects(path):
                    try:
                        cid = int(row.get("chat_id") or 1)
                    except (TypeError, ValueError):
                        continue
                    if cid in candidates and cid not in seen:
                        seen.add(cid)
                        if len(seen) == len(candidates):
                            break
            except Exception:
                log.debug("thread-activity backfill scan failed for %s", path, exc_info=True)
        for cid in sorted(seen):
            if update_project(drive_root, candidates[cid], thread_activity_seen=True) is not None:
                flagged += 1
        if flagged:
            log.info("Thread-activity backfill: %d legacy project(s) flagged", flagged)
        _ACTIVITY_BACKFILL_DONE.add(key)
    except Exception:
        log.warning("Thread-activity backfill failed", exc_info=True)
    return flagged


def ensure_project_workspace(drive_root: Any, project_id: str, repo_dir: Any) -> str:
    """Provision (once) an invisible-git working folder for a project.

    Reuses the genesis-project machinery: a standalone git repo under the
    durable projects root (never GC-pruned, isolated from repo/ and data/).
    Returns the absolute path ("" when provisioning failed). File-less
    projects simply never call this.
    """
    entry = get_project(drive_root, project_id)
    if entry is None:
        entry = create_project(drive_root, project_id)
    existing = str(entry.get("working_dir") or "").strip()
    if existing and pathlib.Path(existing).is_dir():
        return existing
    try:
        from ouroboros.subagent_worktrees import provision_genesis_project

        handle = provision_genesis_project(
            repo_dir=repo_dir,
            task_id=f"project_{entry['id']}",
            data_dir=drive_root,
            # Name the genesis folder after the project so sibling builders land in a
            # recognizable shared root (binding identity stays the task_id). (I, v6.39)
            dir_name=str(entry.get("name") or ""),
        )
        update_project(drive_root, entry["id"], working_dir=str(handle.path))
        return str(handle.path)
    except Exception:
        log.warning("Project workspace provisioning failed for %s", project_id, exc_info=True)
        return ""


def projects_summary(drive_root: Any, *, limit: int = 50) -> List[Dict[str, Any]]:
    """Compact list for /api/state and the sidebar."""
    out: List[Dict[str, Any]] = []
    bindings = _load_bindings(drive_root).get("bindings", {})

    def _has_thread_activity(project: Dict[str, Any]) -> bool:
        # Registry-facts derivation ONLY — the GET path never scans logs and
        # never writes. Micro-delta vs the retired per-request log scan
        # (disclosed): a project whose thread carries ONLY telemetry rows (no
        # owner-visible canonical row, no binding) reads inactive until its
        # first visible row — exactly the junk-row shape this filter exists
        # to hide. Legacy projects whose activity predates the
        # visible_revision counter are covered by the write-once
        # `thread_activity_seen` flag seeded at boot reconcile
        # (_backfill_thread_activity).
        pid = str(project.get("id") or "")
        # v6.59.0: a project the OWNER explicitly created in the UI is always shown —
        # the activity filter exists to hide junk reconcile rows, not a fresh project
        # the owner just made (which has no chat rows yet by definition).
        if str(project.get("origin") or "") == "owner_ui":
            return True
        if any(isinstance(row, dict) and row.get("project_id") == pid for row in bindings.values()):
            return True
        if int(project.get("visible_revision") or 0) > 0:
            return True
        return bool(project.get("thread_activity_seen"))

    for project in list_sidebar_projects(drive_root)[: max(1, int(limit))]:
        out.append({
            "id": project.get("id"),
            "name": project.get("name"),
            "chat_id": project.get("chat_id"),
            "working_dir": project.get("working_dir") or "",
            "provenance": project.get("provenance") or "",
            "last_active_at": project.get("last_active_at") or "",
            "lifecycle": project.get("lifecycle") or PROJECT_ACTIVE,
            "routing_generation": int(project.get("routing_generation") or 0),
            "visible_revision": int(project.get("visible_revision") or 0),
            "delete_error": project.get("delete_error") or "",
            "has_thread_activity": _has_thread_activity(project),
            # Canonical projection, thread #0 first (X7). ``chat_id`` above stays
            # its compatibility alias, so a client that never learns about
            # threads keeps working unchanged.
            "threads": project_threads(project),
        })
    return out


__all__ = [
    "PROJECT_ACTIVE",
    "PROJECT_DELETING",
    "PROJECT_NAME_MAX",
    "PROJECT_TOMBSTONED",
    "THREAD_NAME_MAX",
    "all_task_bindings",
    "begin_project_deletion",
    "bind_task_to_project",
    "complete_project_deletion",
    "create_project",
    "create_thread",
    "delete_project",
    "duplicate_chat_ids",
    "ensure_project_workspace",
    "fail_project_deletion",
    "fork_thread",
    "get_project",
    "get_reserved_project",
    "get_thread",
    "project_threads",
    "rename_thread",
    "resolve_chat_binding",
    "increment_project_visible_revision",
    "list_projects",
    "list_reserved_projects",
    "list_sidebar_projects",
    "project_binding_for_task",
    "project_chat_for_task",
    "project_thread_note_for_task",
    "project_chat_for_task_tree",
    "project_task_bindings",
    "registered_project_chat_ids",
    "reserved_project_chat_ids",
    "projects_summary",
    "reconcile_projects",
    "touch_project",
    "update_project",
]


def project_thread_note_for_task(task: Any) -> str:
    """One-line pointer to the Project thread when a task is project-bound.

    The raw final answer of a bound task lives in the PROJECT room while the
    initiating (Main) chat receives only the task summary — twice in one night
    the owner read that silence as a hung agent. The pointer names where the
    full result lives (v6.70.0); an unbound task gets no extra text."""
    try:
        import pathlib as _pathlib

        from ouroboros.config import DATA_DIR

        chat_id = project_chat_for_task_tree(
            _pathlib.Path(DATA_DIR),
            str(task.get("id") or ""),
            str(task.get("parent_task_id") or ""),
            str(task.get("root_task_id") or ""),
        )
        if not chat_id or int(task.get("chat_id") or 0) == int(chat_id):
            return ""
        name = next(
            (
                str(project.get("name") or "").strip()
                for project in list_projects(_pathlib.Path(DATA_DIR))
                if int(project.get("chat_id") or 0) == int(chat_id)
            ),
            "",
        )
        return f" Full result in the '{name}' project thread." if name else " Full result in the project thread."
    except Exception:
        return ""
