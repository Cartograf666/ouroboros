"""Chat→owner resolution goes through ONE seam (R3).

``server.py`` used to compare an inbound chat id against ``project["chat_id"]``
directly, which sees thread #0 ONLY — every message to any other thread of a
project would have been classified as Main and scoped to no project. These
tests pin the seam and the behaviour that depends on it.
"""

from __future__ import annotations

import types

from ouroboros.projects_registry import (
    begin_project_deletion,
    create_project,
    create_thread,
)


def _ctx(tmp_path):
    return types.SimpleNamespace(DRIVE_ROOT=tmp_path)


def test_every_thread_of_a_project_classifies_as_that_project(tmp_path):
    import server

    project = create_project(tmp_path, "racer", name="Cyber Racer")
    thread = create_thread(tmp_path, "racer", name="Tuning")
    ctx = _ctx(tmp_path)

    assert server._project_id_for_registered_chat(ctx, project["chat_id"]) == "racer"
    assert server._project_id_for_registered_chat(ctx, thread["chat_id"]) == "racer"
    # Main and unknown transport ids stay unscoped.
    assert server._project_id_for_registered_chat(ctx, 1) == ""
    assert server._project_id_for_registered_chat(ctx, 987654321) == ""


def test_reserved_lookup_answers_for_a_thread_of_a_fenced_project(tmp_path):
    import server

    create_project(tmp_path, "racer")
    thread = create_thread(tmp_path, "racer", name="Tuning")
    begin_project_deletion(tmp_path, "racer")
    ctx = _ctx(tmp_path)

    # Fenced: ordinary routing must NOT resurrect the room...
    assert server._project_id_for_registered_chat(ctx, thread["chat_id"]) == ""
    # ...but the reserved lookup still identifies it, so the caller can emit the
    # typed "project_unavailable" receipt instead of silently answering as Main.
    reserved = server._reserved_project_for_chat(ctx, thread["chat_id"])
    assert reserved.get("id") == "racer"
    assert reserved.get("lifecycle") == "deleting"


def test_owner_notices_from_a_thread_still_bind_to_main(tmp_path):
    """A WEB owner message in ANY project thread keeps binding owner notices to
    Main (1) — the behaviour that broke when a thread misclassified as Main."""
    import server

    create_project(tmp_path, "racer")
    thread = create_thread(tmp_path, "racer", name="Tuning")
    ctx = _ctx(tmp_path)

    assert server._owner_binding_chat_id(ctx, thread["chat_id"], False) == 1
    assert server._owner_binding_chat_id(ctx, thread["chat_id"], True) == thread["chat_id"]
