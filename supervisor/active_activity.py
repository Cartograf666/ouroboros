"""In-memory, process-local registry for active direct and ephemeral chat activities.

Tracks in-flight direct conversational turns and ephemeral decision turns so
the Gateway (/api/state) and WebSocket activity pipeline have authoritative,
thread-safe visibility into in-progress work without creating spurious queue records.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

log = logging.getLogger(__name__)


@dataclass
class DirectActivityEntry:
    activity_id: str
    chat_id: int
    project_id: str = ""
    client_message_id: str = ""
    kind: str = "direct_chat"  # "direct_chat" | "ephemeral_decision"
    phase: str = "thinking"
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "activity_id": self.activity_id,
            "chat_id": self.chat_id,
            "project_id": self.project_id,
            "client_message_id": self.client_message_id,
            "kind": self.kind,
            "phase": self.phase,
            "started_at": self.started_at,
        }


class DirectActivityRegistry:
    """Thread-safe registry for active direct-chat and ephemeral-decision turns."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._activities: Dict[str, DirectActivityEntry] = {}

    def register(
        self,
        activity_id: str,
        chat_id: int,
        *,
        client_message_id: str = "",
        project_id: str = "",
        kind: str = "direct_chat",
        phase: str = "thinking",
    ) -> DirectActivityEntry:
        aid = str(activity_id or "").strip()
        if not aid:
            raise ValueError("activity_id cannot be empty")
        entry = DirectActivityEntry(
            activity_id=aid,
            chat_id=int(chat_id or 0),
            project_id=str(project_id or ""),
            client_message_id=str(client_message_id or ""),
            kind=str(kind or "direct_chat"),
            phase=str(phase or "thinking"),
            started_at=time.time(),
        )
        with self._lock:
            self._activities[aid] = entry
        log.debug("Registered direct activity: %s (chat_id=%s, kind=%s)", aid, chat_id, kind)
        return entry

    def unregister(self, activity_id: str) -> Optional[DirectActivityEntry]:
        aid = str(activity_id or "").strip()
        with self._lock:
            entry = self._activities.pop(aid, None)
        if entry:
            log.debug("Unregistered direct activity: %s (chat_id=%s)", aid, entry.chat_id)
        return entry

    def snapshot(self, chat_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if chat_id is not None:
                cid = int(chat_id)
                return [e.to_dict() for e in self._activities.values() if e.chat_id == cid]
            return [e.to_dict() for e in self._activities.values()]

    def get(self, activity_id: str) -> Optional[DirectActivityEntry]:
        aid = str(activity_id or "").strip()
        with self._lock:
            return self._activities.get(aid)

    def clear(self) -> None:
        """Clear registry — primarily for tests and process resets."""
        with self._lock:
            self._activities.clear()


# Global process-local singleton
_DIRECT_ACTIVITY_REGISTRY = DirectActivityRegistry()


def get_direct_activity_registry() -> DirectActivityRegistry:
    return _DIRECT_ACTIVITY_REGISTRY


@contextmanager
def track_direct_activity(
    activity_id: str,
    chat_id: int,
    *,
    client_message_id: str = "",
    project_id: str = "",
    kind: str = "direct_chat",
    phase: str = "thinking",
) -> Iterator[DirectActivityEntry]:
    registry = get_direct_activity_registry()
    entry = registry.register(
        activity_id,
        chat_id,
        client_message_id=client_message_id,
        project_id=project_id,
        kind=kind,
        phase=phase,
    )
    try:
        yield entry
    finally:
        registry.unregister(activity_id)
