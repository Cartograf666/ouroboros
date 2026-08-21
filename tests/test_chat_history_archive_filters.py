"""Focused coverage for archive-aware, exact-provenance chat recall."""

from __future__ import annotations

import json

from ouroboros.memory import Memory
from ouroboros.tools.control import get_tools


def _row(ts: str, text: str, *, actor: str = "u-1", **transport: str) -> dict:
    return {
        "ts": ts,
        "direction": "in",
        "text": text,
        "sender_label": "Alex",
        "sender_session_id": actor,
        "transport": {
            "provider": transport.get("provider", "telegram"),
            "account_id": transport.get("account_id", "bot-1"),
            "conversation_id": transport.get("conversation_id", "room-1"),
            "thread_id": transport.get("thread_id", "topic-1"),
            "actor": {"platform_actor_id": actor, "username": "alex"},
        },
    }


def _write(path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_chat_history_combines_archive_and_live_with_shared_provenance(tmp_path):
    _write(tmp_path / "archive" / "chat_20260820T000000.jsonl", [
        _row("2026-08-20T09:00:00Z", "archived hello"),
    ])
    _write(tmp_path / "logs" / "chat.jsonl", [
        _row("2026-08-21T09:00:00Z", "live hello"),
    ])

    history = Memory(tmp_path).chat_history(count=2)

    assert history.index("archived hello") < history.index("live hello")
    assert "Alex [provider=telegram; account=bot-1; conversation=room-1; thread=topic-1]" in history


def test_chat_history_exact_provenance_and_inclusive_date_filters(tmp_path):
    _write(tmp_path / "archive" / "chat_20260820T000000.jsonl", [
        _row("2026-08-20T08:59:59Z", "too early"),
        _row("2026-08-20T09:00:00Z", "matching archived"),
    ])
    _write(tmp_path / "logs" / "chat.jsonl", [
        _row("2026-08-20T10:00:00+00:00", "matching live"),
        _row("2026-08-20T10:00:00Z", "wrong actor", actor="u-2"),
        _row("2026-08-20T10:00:00Z", "wrong room", conversation_id="room-2"),
        _row("2026-08-20T10:00:01Z", "too late"),
    ])

    history = Memory(tmp_path).chat_history(
        count=20,
        provider="telegram",
        account_id="bot-1",
        conversation_id="room-1",
        thread_id="topic-1",
        actor_id="u-1",
        date_from="2026-08-20T09:00:00Z",
        date_to="2026-08-20T10:00:00Z",
    )

    assert "matching archived" in history and "matching live" in history
    assert "too early" not in history and "too late" not in history
    assert "wrong actor" not in history and "wrong room" not in history


def test_chat_history_old_call_keeps_search_offset_count_result(tmp_path):
    _write(tmp_path / "logs" / "chat.jsonl", [
        {"ts": "2026-08-21T09:00:00Z", "direction": "in", "text": "match one"},
        {"ts": "2026-08-21T09:01:00Z", "direction": "in", "text": "ignore"},
        {"ts": "2026-08-21T09:02:00Z", "direction": "in", "text": "match two"},
        {"ts": "2026-08-21T09:03:00Z", "direction": "in", "text": "match three"},
    ])

    assert Memory(tmp_path).chat_history(count=1, offset=1, search="MATCH") == (
        "Showing 1 messages:\n\n← [2026-08-21T09:02] [User] match two"
    )


def test_chat_history_tool_exposes_only_exact_filter_fields():
    tool = next(entry for entry in get_tools() if entry.name == "chat_history")
    assert set(tool.schema["parameters"]["properties"]) == {
        "count", "offset", "search", "provider", "account_id", "conversation_id",
        "thread_id", "actor_id", "date_from", "date_to",
    }
    assert tool.schema["parameters"]["additionalProperties"] is False
