"""The ONE thread-ancestry lens shared by the UI history and the agent context.

A fork stores a cursor, never copied rows, so "what does this thread read" has
exactly one definition. These tests pin that definition and then prove BOTH
consumers use it: ``gateway/history.py`` (owner) and ``ouroboros/context.py``
(agent). A cursor honoured by only one of them is the failure mode this file
exists to prevent.
"""

from __future__ import annotations

import json

from ouroboros.project_dialogue import _text_sha256
from ouroboros.projects_registry import (
    begin_project_deletion,
    bind_task_to_project,
    complete_project_deletion,
    create_project,
    create_thread,
    fork_thread,
)
from ouroboros.thread_history import MAX_ANCESTRY_DEPTH, thread_ancestry_lens


def _rows(tmp_path, rows):
    path = tmp_path / "logs" / "chat.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _chat_row(chat_id, ts, text, direction="in", **extra):
    return {
        "chat_id": chat_id, "ts": ts, "text": text, "direction": direction,
        "client_message_id": f"cm-{text}", **extra,
    }


def test_plain_thread_reads_only_itself(tmp_path):
    create_project(tmp_path, "racer")
    thread = create_thread(tmp_path, "racer", name="Tuning")

    lens = thread_ancestry_lens(tmp_path, thread["chat_id"])

    assert lens.project_id == "racer"
    assert lens.thread_id == thread["id"]
    assert lens.chat_ids == {thread["chat_id"]}
    assert lens.has_ancestors is False
    assert lens.admits(thread["chat_id"], "2026-01-01T00:00:00+00:00") is True
    assert lens.admits(999, "2026-01-01T00:00:00+00:00") is False


def test_non_project_chat_yields_a_degenerate_lens(tmp_path):
    lens = thread_ancestry_lens(tmp_path, 1)
    assert lens.is_project_thread is False
    assert lens.chat_ids == {1}
    assert lens.admits(1, "anything") is True


def test_fork_cutoff_is_inclusive_at_the_boundary(tmp_path):
    create_project(tmp_path, "racer")
    parent = create_thread(tmp_path, "racer", name="Parent")
    fork = fork_thread(tmp_path, "racer", parent["id"])
    cut = fork["fork_before_ts"]

    lens = thread_ancestry_lens(tmp_path, fork["chat_id"])

    assert lens.cutoffs[fork["chat_id"]] == ""          # own chat: unbounded
    assert lens.cutoffs[parent["chat_id"]] == cut
    # INCLUSIVE: a row stamped at exactly the fork instant existed before it.
    assert lens.admits(parent["chat_id"], cut) is True
    assert lens.admits(parent["chat_id"], cut + "1") is False
    # An unstamped row sorts as oldest and is admitted (never silently dropped).
    assert lens.admits(parent["chat_id"], "") is True
    # The fork's own future rows are always in scope.
    assert lens.admits(fork["chat_id"], "9999-01-01T00:00:00+00:00") is True


def test_fork_of_fork_intersects_cutoffs(tmp_path):
    create_project(tmp_path, "racer")
    grand = create_thread(tmp_path, "racer", name="Grandparent")
    parent = fork_thread(tmp_path, "racer", grand["id"])
    child = fork_thread(tmp_path, "racer", parent["id"])

    lens = thread_ancestry_lens(tmp_path, child["chat_id"])

    assert lens.order == [child["chat_id"], parent["chat_id"], grand["chat_id"]]
    assert lens.cutoffs[parent["chat_id"]] == child["fork_before_ts"]
    # The grandchild can never see MORE of the grandparent than its parent
    # could: the effective bound is the earlier (parent's own) fork moment.
    assert lens.cutoffs[grand["chat_id"]] == parent["fork_before_ts"]
    assert lens.cutoffs[grand["chat_id"]] <= lens.cutoffs[parent["chat_id"]]
    assert lens.truncated is False


def test_ancestry_survives_a_deleted_or_tombstoned_parent(tmp_path):
    """A3a: the cursor reads the parent's rows whether the parent is alive,
    archived or deleted. Filtering the chain by liveness would orphan forks."""
    create_project(tmp_path, "racer")
    parent = create_thread(tmp_path, "racer", name="Parent")
    fork = fork_thread(tmp_path, "racer", parent["id"])

    begin_project_deletion(tmp_path, "racer")
    fenced = thread_ancestry_lens(tmp_path, fork["chat_id"])
    assert fenced.cutoffs.get(parent["chat_id"]) == fork["fork_before_ts"]

    complete_project_deletion(tmp_path, "racer")
    dead = thread_ancestry_lens(tmp_path, fork["chat_id"])
    assert dead.cutoffs.get(parent["chat_id"]) == fork["fork_before_ts"]
    assert dead.project_id == "racer"


def test_fork_of_a_converted_project_carries_the_parent_source_refs(tmp_path):
    """X4: history loads source refs for the REQUESTED chat only. A fork of a
    CONVERTED project's thread must still see the Main-chat message that
    started the project — that row lives on the PARENT's binding."""
    create_project(tmp_path, "conv")
    project_chat = create_project(tmp_path, "conv")["chat_id"]
    text = "please turn this into a project"
    bind_task_to_project(
        tmp_path, "task-1", "conv", project_chat,
        origin={
            "ref": {
                "chat_id": 1,
                "client_message_id": "cm-origin",
                "ts": "2026-01-01T00:00:00+00:00",
                "text_sha256": _text_sha256(text),
            },
            "text": text,
        },
    )
    fork = fork_thread(tmp_path, "conv", 0)

    lens = thread_ancestry_lens(tmp_path, fork["chat_id"])

    assert project_chat in lens.source_refs
    origin_row = {
        "direction": "in", "chat_id": 1, "client_message_id": "cm-origin",
        "ts": "2026-01-01T00:00:00+00:00", "text": text,
    }
    assert lens.admits_source_ref(origin_row) is True
    # An origin stamped AFTER the fork is out of scope for the fork.
    late = dict(origin_row, ts="9999-01-01T00:00:00+00:00")
    assert lens.admits_source_ref(late) is False
    # with_source_refs=False keeps the agent-side build free of the extra read.
    assert thread_ancestry_lens(
        tmp_path, fork["chat_id"], with_source_refs=False
    ).source_refs == {}


def test_deep_chain_truncation_is_disclosed(tmp_path, monkeypatch):
    import ouroboros.thread_history as th

    monkeypatch.setattr(th, "MAX_ANCESTRY_DEPTH", 2)
    create_project(tmp_path, "racer")
    tip = create_thread(tmp_path, "racer", name="root")
    for _ in range(4):
        tip = fork_thread(tmp_path, "racer", tip["id"])

    lens = th.thread_ancestry_lens(tmp_path, tip["chat_id"])

    assert lens.truncated is True
    assert len(lens.cutoffs) == 3  # self + 2 ancestors, then stop
    assert MAX_ANCESTRY_DEPTH >= 2  # the module default is not what we pinned


def test_history_endpoint_serves_the_fork_its_shared_past(tmp_path):
    from ouroboros.gateway.history import _assemble_history_response

    create_project(tmp_path, "racer")
    parent = create_thread(tmp_path, "racer", name="Parent")
    _rows(tmp_path, [_chat_row(parent["chat_id"], "2026-01-01T00:00:00+00:00", "before")])
    fork = fork_thread(tmp_path, "racer", parent["id"])
    _rows(tmp_path, [
        _chat_row(parent["chat_id"], "2027-01-01T00:00:00+00:00", "after-in-parent"),
        _chat_row(fork["chat_id"], "2027-01-02T00:00:00+00:00", "own"),
        _chat_row(1, "2026-01-01T00:00:00+00:00", "main-chat"),
    ])

    payload = json.loads(_assemble_history_response(tmp_path, fork["chat_id"], 50, 10))
    texts = [m["text"] for m in payload["messages"]]

    assert "before" in texts          # shared past through the cursor
    assert "own" in texts             # its own conversation
    assert "after-in-parent" not in texts   # the parent moved on independently
    assert "main-chat" not in texts

    # The parent itself is untouched: it still sees everything it ever had.
    parent_payload = json.loads(
        _assemble_history_response(tmp_path, parent["chat_id"], 50, 10)
    )
    parent_texts = [m["text"] for m in parent_payload["messages"]]
    assert {"before", "after-in-parent"} <= set(parent_texts)
    assert "own" not in parent_texts


def test_agent_context_reads_the_same_shared_past(tmp_path, monkeypatch):
    """R4: context.py reads its own raw tail. If the cursor lived only in the
    history endpoint, the agent working IN the fork would see a different
    conversation than the owner reading it."""
    from ouroboros.context import build_recent_sections

    create_project(tmp_path, "racer")
    parent = create_thread(tmp_path, "racer", name="Parent")
    _rows(tmp_path, [_chat_row(parent["chat_id"], "2026-01-01T00:00:00+00:00", "shared-past")])
    fork = fork_thread(tmp_path, "racer", parent["id"])
    _rows(tmp_path, [
        _chat_row(parent["chat_id"], "2027-01-01T00:00:00+00:00", "parent-moved-on"),
        _chat_row(fork["chat_id"], "2027-01-02T00:00:00+00:00", "fork-own"),
    ])

    class _Memory:
        drive_root = tmp_path

        def read_jsonl_tail(self, name, limit):
            path = tmp_path / "logs" / name
            if not path.is_file():
                return []
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()][-limit:]

        def summarize_chat(self, entries, limit=0):
            return "\n".join(str(e.get("text") or "") for e in entries)

        def summarize_progress(self, rows, limit=0):
            return ""

        def summarize_tools(self, rows):
            return ""

        def summarize_events(self, rows):
            return ""

        def summarize_supervisor(self, rows):
            return ""

    sections = build_recent_sections(
        _Memory(), object(), task_id="", thread_chat_id=fork["chat_id"]
    )
    chat_section = next((s for s in sections if s.startswith("## Recent chat")), "")

    assert "shared-past" in chat_section
    assert "fork-own" in chat_section
    assert "parent-moved-on" not in chat_section
