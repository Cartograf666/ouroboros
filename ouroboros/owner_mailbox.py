"""Per-task owner-message mailboxes for running worker tasks."""
import json
import logging
import pathlib
import uuid
from typing import Any, Dict, List, Optional

from ouroboros.task_results import validate_task_id
from ouroboros.utils import append_jsonl, utc_now_iso

log = logging.getLogger(__name__)

_MAILBOX_DIR = "memory/owner_mailbox"

# Typed mailbox entry kinds. KIND_OWNER_TEXT entries are injected verbatim as
# owner dialogue; control kinds carry supervisor->worker protocol signals and
# are routed structurally (never shown as user prose).
KIND_OWNER_TEXT = "owner_text"
KIND_TASK_MESSAGE = "task_message"
KIND_FINALIZE_NOW = "finalize_now"
# Owner "hurry" control (HQ1, 2026-08-15): a task-local typed acceleration
# directive — NEVER owner dialogue and NEVER revoked after drain (restart
# re-drain must restore the attempt latch; only terminal cleanup removes it).
# Its ``text`` is the parser-required internal reason ("owner_hurry"), not prose.
KIND_HURRY = "hurry"
# The mailbox is append-only, so a sender that changes its mind cannot delete the
# control it already wrote — it appends this retraction naming the target msg_id.
# Revocations are resolved by the READER over the whole mailbox, so a control that
# was revoked before anyone drained it is never delivered at all. Its payload IS
# the revoked msg_id (same convention as finalize_now, whose payload is a reason).
KIND_CONTROL_REVOKED = "control_revoked"


def _mailbox_path(drive_root: pathlib.Path, task_id: str) -> pathlib.Path:
    return pathlib.Path(drive_root) / _MAILBOX_DIR / f"{validate_task_id(task_id)}.jsonl"


def _ack_path(drive_root: pathlib.Path, task_id: str) -> pathlib.Path:
    return pathlib.Path(drive_root) / _MAILBOX_DIR / f"{validate_task_id(task_id)}.acks.jsonl"


def acknowledged_task_message_ids(drive_root: pathlib.Path, task_id: str) -> set[str]:
    """Read the shared durable acknowledgements for transcript-delivered messages."""

    path = _ack_path(drive_root, task_id)
    if not path.exists():
        return set()
    found: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(row, dict) and str(row.get("msg_id") or ""):
                found.add(str(row["msg_id"]))
    except OSError:
        log.warning("Failed to read task-message acknowledgements for %s", task_id, exc_info=True)
    return found


def acknowledge_task_messages(
    drive_root: pathlib.Path, task_id: str, msg_ids: List[str], *, wake_id: str,
) -> bool:
    """Acknowledge messages only after their full content entered a transcript."""

    path = _ack_path(drive_root, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = acknowledged_task_message_ids(drive_root, task_id)
    for msg_id in [str(item) for item in msg_ids if str(item) and str(item) not in existing]:
        if not append_jsonl(path, {
            "ts": utc_now_iso(), "type": "task_message_acknowledged",
            "task_id": str(task_id), "msg_id": msg_id, "wake_id": str(wake_id or ""),
        }):
            return False
    return True


def acknowledge_transcript_entry(
    drive_root: pathlib.Path, task_id: str, entry: Dict[str, Any], *, wake_id: str = "loop_delivery",
) -> None:
    """Durably acknowledge one mailbox entry after transcript injection."""
    msg_id = str(entry.get("msg_id") or "")
    if msg_id and not acknowledge_task_messages(
        drive_root, task_id, [msg_id], wake_id=wake_id,
    ):
        log.warning("Mailbox delivery acknowledgement failed for %s", msg_id)


def write_owner_message(
    drive_root: pathlib.Path,
    text: str,
    task_id: str,
    msg_id: Optional[str] = None,
    kind: str = KIND_OWNER_TEXT,
    client_surface: Optional[Dict[str, Any]] = None,
) -> bool:
    """Write an owner message or typed control entry to a task's mailbox."""
    path = _mailbox_path(drive_root, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "msg_id": msg_id or uuid.uuid4().hex,
        "ts": utc_now_iso(),
        "text": text,
        "kind": str(kind or KIND_OWNER_TEXT),
    }
    if isinstance(client_surface, dict) and client_surface:
        # Owner Surface Fact (additive, like ``ts``): which client surface sent
        # this follow-up, so the loop can note a mid-task device change.
        entry["client_surface"] = dict(client_surface)
    try:
        if not append_jsonl(path, entry):
            log.warning("Failed to durably append owner message for task %s", task_id)
            return False
        return True
    except Exception:
        log.warning("Failed to write owner message for task %s", task_id, exc_info=True)
        return False


def write_task_message(
    drive_root: pathlib.Path,
    text: str,
    task_id: str,
    *,
    source_task_id: str,
    provenance: str = "ancestor_task",
    relayed_from_task_id: str = "",
    msg_id: Optional[str] = None,
) -> bool:
    """Write an addressed task-tree message without forging owner provenance."""

    if provenance not in {"ancestor_task", "peer_via_ancestor", "system"}:
        return False
    path = _mailbox_path(drive_root, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "msg_id": msg_id or uuid.uuid4().hex,
        "ts": utc_now_iso(),
        "text": str(text or ""),
        "kind": KIND_TASK_MESSAGE,
        "provenance": provenance,
        "source_task_id": str(source_task_id or ""),
    }
    if relayed_from_task_id:
        entry["relayed_from_task_id"] = str(relayed_from_task_id)
    try:
        return bool(append_jsonl(path, entry))
    except Exception:
        log.warning("Failed to write task message for task %s", task_id, exc_info=True)
        return False


def deliver_task_message(
    entry: Dict[str, Any], task_id: str, event_queue: Any, append_message: Any,
) -> None:
    """Render typed provenance and publish the corresponding worker event."""

    provenance = str(entry.get("provenance") or "ancestor_task")
    source = str(entry.get("source_task_id") or "unknown")
    relayed = str(entry.get("relayed_from_task_id") or "")
    if provenance == "peer_via_ancestor" and relayed:
        prefix = f"[Message from task {relayed}, relayed by ancestor {source}]"
    elif provenance == "system":
        prefix = "[System task message]"
    else:
        prefix = f"[Message from ancestor task {source}]"
    append_message(f"{prefix}\n{entry.get('text') or ''}")
    if event_queue is not None:
        try:
            event_queue.put_nowait({
                "type": "task_message_injected", "task_id": task_id,
                "source_task_id": source, "provenance": provenance,
            })
        except Exception:
            pass


def revoke_owner_control(
    drive_root: pathlib.Path, task_id: str, control_msg_id: str,
) -> bool:
    """Retract an already-written control entry by its msg_id.

    The only way to un-send a mailbox control: the file is append-only, so this
    appends a revocation that ``drain_owner_entries`` resolves for every reader.
    Returns False when there is nothing to revoke or the append was not durable —
    the caller must then treat the control as STILL LIVE.
    """
    control_msg_id = str(control_msg_id or "").strip()
    if not control_msg_id:
        return False
    return write_owner_message(
        drive_root, control_msg_id, task_id, kind=KIND_CONTROL_REVOKED,
    )


def drain_owner_entries(
    drive_root: pathlib.Path,
    task_id: str,
    seen_ids: Optional[set] = None,
) -> List[dict]:
    """Read unseen mailbox entries without mutating the append-only mailbox.

    Revocations are resolved over the WHOLE mailbox before anything is yielded,
    so a control retracted by a later line is never delivered even if the reader
    had not drained it yet; the revocation lines themselves are protocol and are
    never returned as content.
    """
    path = _mailbox_path(drive_root, task_id)
    if not path.exists():
        return []
    if seen_ids is None:
        seen_ids = set()
    seen_ids.update(acknowledged_task_message_ids(drive_root, task_id))
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return []
        parsed: List[dict] = []
        revoked: set = set()
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                log.debug("Malformed mailbox line for task %s", task_id, exc_info=True)
                continue
            if not isinstance(entry, dict):
                continue
            if str(entry.get("kind") or KIND_OWNER_TEXT) == KIND_CONTROL_REVOKED:
                revoked.add(str(entry.get("text") or ""))
            parsed.append(entry)
        entries = []
        for entry in parsed:
            mid = entry.get("msg_id", "")
            if mid and mid in seen_ids:
                continue
            if mid:
                seen_ids.add(mid)
            kind = str(entry.get("kind") or KIND_OWNER_TEXT)
            if kind == KIND_CONTROL_REVOKED or (mid and mid in revoked):
                continue
            text = entry.get("text", "")
            if text:
                # ``ts`` is ADDITIVE (2026-08-15 Fable pin): typed controls such
                # as ``hurry`` carry their request time into the drained entry so
                # the attempt latch can preserve when the owner actually asked.
                drained = {
                    "msg_id": mid, "text": text, "kind": kind,
                    "ts": str(entry.get("ts") or ""),
                }
                # Owner Surface Fact: the drain projection must carry the field
                # explicitly or a written fact is never delivered (the exact
                # dead-wire class this sprint closes).
                if isinstance(entry.get("client_surface"), dict) and entry.get("client_surface"):
                    drained["client_surface"] = dict(entry["client_surface"])
                if kind == KIND_TASK_MESSAGE:
                    drained["provenance"] = str(entry.get("provenance") or "ancestor_task")
                    drained["source_task_id"] = str(entry.get("source_task_id") or "")
                    drained["relayed_from_task_id"] = str(entry.get("relayed_from_task_id") or "")
                entries.append(drained)
        return entries
    except Exception:
        log.debug("Failed to read mailbox for task %s", task_id, exc_info=True)
        return []


def drain_owner_messages(
    drive_root: pathlib.Path,
    task_id: str,
    seen_ids: Optional[set] = None,
) -> List[str]:
    """Read unseen owner-dialogue message texts (legacy text-only view)."""
    return [
        entry["text"]
        for entry in drain_owner_entries(drive_root, task_id, seen_ids=seen_ids)
        if entry.get("kind", KIND_OWNER_TEXT) == KIND_OWNER_TEXT
    ]


def cleanup_task_mailbox(drive_root: pathlib.Path, task_id: str) -> None:
    """Remove a task's mailbox file after task completes."""
    for path in (_mailbox_path(drive_root, task_id), _ack_path(drive_root, task_id)):
        try:
            if path.exists():
                path.unlink()
        except Exception:
            log.debug("Failed to cleanup mailbox for task %s", task_id, exc_info=True)
