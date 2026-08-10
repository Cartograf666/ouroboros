"""Thread archive and delete (D4) with the project pattern's admission fencing (X10).

The claims worth breaking a build over:

* archive HIDES and nothing else — history, fork cursors and worktree intact, and
  `restore` puts it back;
* an ARCHIVED thread whose task is still running stays VISIBLE until that task is
  terminal, the explicitly decided reading of X10 (the alternative — hiding it
  immediately — leaves live output arriving in a room nobody can open);
* delete FENCES routing FIRST, cancels by EXACT thread chat id (never the whole
  project's queue), and tombstones only after quiescence;
* a tombstoned id and chat id are reserved forever — with 28-bit chat ids, reuse
  would merge a dead thread's history into a live conversation;
* deleting a forked-FROM thread does not break its children (A3a).
"""

from __future__ import annotations

import pytest

from ouroboros.contracts.chat_id_policy import MAIN_THREAD_ID
from ouroboros.projects_registry import (
    THREAD_ACTIVE,
    THREAD_ARCHIVED,
    THREAD_DELETING,
    THREAD_TOMBSTONED,
    ThreadLifecycleError,
    archive_thread,
    begin_thread_deletion,
    complete_thread_deletion,
    create_project,
    create_thread,
    fork_thread,
    get_thread,
    project_threads,
    projects_summary,
    reserved_project_chat_ids,
    resolve_chat_binding,
    restore_thread,
)


@pytest.fixture()
def project(tmp_path):
    create_project(tmp_path, "racer", name="Cyber Racer")
    return tmp_path


def _visible_ids(drive_root, live_chat_ids=None):
    rows = projects_summary(drive_root, live_chat_ids=live_chat_ids)
    return [int(thread["id"]) for thread in rows[0]["threads"]]


def test_archive_hides_and_restore_brings_it_back_unchanged(project):
    thread = create_thread(project, "racer", name="Side quest")
    tid = int(thread["id"])
    assert _visible_ids(project) == [MAIN_THREAD_ID, tid]

    archived = archive_thread(project, "racer", tid)

    assert archived["lifecycle"] == THREAD_ARCHIVED
    assert archived["archived_at"]
    assert _visible_ids(project) == [MAIN_THREAD_ID]
    # Nothing was removed: the row, its name and its chat id are all still there.
    row = get_thread(project, "racer", tid)
    assert row["name"] == "Side quest"
    assert row["chat_id"] == thread["chat_id"]
    assert row["chat_id"] in reserved_project_chat_ids(project)

    restored = restore_thread(project, "racer", tid)

    assert restored["lifecycle"] == THREAD_ACTIVE
    assert restored["archived_at"] == ""
    assert _visible_ids(project) == [MAIN_THREAD_ID, tid]


def test_an_archived_thread_with_live_work_stays_visible_until_terminal(project):
    """X10, decided explicitly: keep it visible rather than hide live output."""
    thread = create_thread(project, "racer", name="Side quest")
    tid = int(thread["id"])
    archive_thread(project, "racer", tid)

    # While its task runs, the thread is still on screen...
    assert _visible_ids(project, live_chat_ids={int(thread["chat_id"])}) == [MAIN_THREAD_ID, tid]
    # ...and the moment that task is terminal, the archive takes effect.
    assert _visible_ids(project, live_chat_ids=set()) == [MAIN_THREAD_ID]


def test_thread_zero_is_the_project_and_refuses_its_own_lifecycle(project):
    """Thread #0 IS the project row. A second lifecycle over the same thing could
    only ever disagree with it, so both operations are refused by name."""
    with pytest.raises(ThreadLifecycleError) as archived:
        archive_thread(project, "racer", MAIN_THREAD_ID)
    assert archived.value.reason == "thread_zero_is_the_project"

    with pytest.raises(ThreadLifecycleError):
        begin_thread_deletion(project, "racer", MAIN_THREAD_ID)


def test_delete_fences_routing_before_anything_is_cancelled(project):
    """The fence is step ONE: a message must not land in a room on its way out."""
    thread = create_thread(project, "racer", name="Doomed")
    tid, chat_id = int(thread["id"]), int(thread["chat_id"])
    assert resolve_chat_binding(project, chat_id)["thread_lifecycle"] == THREAD_ACTIVE

    fenced = begin_thread_deletion(project, "racer", tid)

    assert fenced["lifecycle"] == THREAD_DELETING
    assert resolve_chat_binding(project, chat_id)["thread_lifecycle"] == THREAD_DELETING
    # Still VISIBLE while it quiesces — exactly as a deleting PROJECT is.
    assert tid in _visible_ids(project)

    tombstoned = complete_thread_deletion(project, "racer", tid)

    assert tombstoned["lifecycle"] == THREAD_TOMBSTONED
    assert tid not in _visible_ids(project)


def test_a_tombstoned_thread_keeps_its_chat_id_reserved_forever(project):
    """28-bit chat ids: a reused one silently MERGES a dead thread's history into
    a live conversation. The row and its reservation therefore survive."""
    thread = create_thread(project, "racer", name="Doomed")
    tid, chat_id = int(thread["id"]), int(thread["chat_id"])
    begin_thread_deletion(project, "racer", tid)
    complete_thread_deletion(project, "racer", tid)

    assert chat_id in reserved_project_chat_ids(project)
    assert resolve_chat_binding(project, chat_id)["thread_id"] == tid
    # And the next thread never reuses the id, because the high-water mark is
    # persisted rather than derived from the live rows.
    fresh = create_thread(project, "racer", name="New")
    assert int(fresh["id"]) > tid
    assert int(fresh["chat_id"]) != chat_id


def test_deleting_a_forked_FROM_thread_does_not_break_its_children(project):
    """A3a: the fork cursor reads the parent's rows regardless of the parent
    being archived or deleted. A naive "hide deleted threads" filter in the
    ancestry read would silently orphan every fork."""
    from ouroboros.thread_history import thread_ancestry_lens

    parent = create_thread(project, "racer", name="Parent")
    child = fork_thread(project, "racer", parent["id"])
    grandchild = fork_thread(project, "racer", child["id"])

    before = thread_ancestry_lens(project, int(grandchild["chat_id"]))

    archive_thread(project, "racer", int(parent["id"]))
    begin_thread_deletion(project, "racer", int(parent["id"]))
    complete_thread_deletion(project, "racer", int(parent["id"]))

    after = thread_ancestry_lens(project, int(grandchild["chat_id"]))

    assert after == before, "a tombstoned parent must not change what its fork can read"
    assert int(parent["chat_id"]) in after.chat_ids
    assert int(child["chat_id"]) in after.chat_ids


def test_an_archived_thread_can_be_deleted_directly(project):
    """Filing something away and then discarding it is ONE flow, not two."""
    thread = create_thread(project, "racer", name="Side quest")
    archive_thread(project, "racer", thread["id"])

    fenced = begin_thread_deletion(project, "racer", thread["id"])

    assert fenced["lifecycle"] == THREAD_DELETING


def test_a_tombstone_is_terminal(project):
    thread = create_thread(project, "racer", name="Doomed")
    begin_thread_deletion(project, "racer", thread["id"])
    complete_thread_deletion(project, "racer", thread["id"])

    for operation in (archive_thread, restore_thread, begin_thread_deletion):
        with pytest.raises(ThreadLifecycleError) as raised:
            operation(project, "racer", thread["id"])
        assert raised.value.reason == "lifecycle_conflict"


def test_live_thread_task_ids_selects_by_EXACT_chat_id(monkeypatch):
    """A project can hold several threads. Cancelling the project's whole queue
    because ONE thread was deleted would kill work the owner never touched."""
    from supervisor import queue
    from supervisor.task_lifecycle import _live_thread_task_ids

    doomed = {"id": "t1", "project_id": "racer", "chat_id": 777}
    sibling = {"id": "t2", "project_id": "racer", "chat_id": 888}
    child = {"id": "t3", "project_id": "racer", "chat_id": 0, "parent_task_id": "t1", "root_task_id": "t1"}
    monkeypatch.setattr(queue, "PENDING", [sibling, child])
    monkeypatch.setattr(queue, "RUNNING", {"t1": {"task": doomed}})

    live = _live_thread_task_ids(777)

    assert set(live) == {"t1", "t3"}, "the sibling thread's task must survive"
    # Children first, so a cascade cancels from the leaves up.
    assert live[0] == "t3"


def test_the_scheduled_notice_says_QUEUED_not_rejected(monkeypatch):
    """A14 at the moment it becomes true.

    The earlier copy claimed a second thread's task was REJECTED. It never was —
    the writer lane serializes and has refused nothing — and telling an owner
    their work was thrown away when it is sitting in the queue is the kind of
    wrong that makes people stop trusting the queue entirely.
    """
    from supervisor.events import _project_lane_wait_suffix

    busy = {"t1": {"task": {"id": "t1", "project_id": "racer", "workspace_root": "/w/racer"}}}
    waiting = {"id": "t2", "project_id": "racer", "workspace_root": "/w/racer"}

    suffix = _project_lane_wait_suffix(waiting, busy)

    assert "QUEUED" in suffix
    assert "not rejected" in suffix
    assert "branching this thread off" in suffix.lower()
    # A thread with its OWN checkout is not waiting on that folder at all.
    branched = {"id": "t3", "project_id": "racer", "workspace_root": "/w/racer-thread-2"}
    assert _project_lane_wait_suffix(branched, busy) == ""
    # An unscoped task never serializes, so it is never warned.
    assert _project_lane_wait_suffix({"id": "t4", "workspace_root": "/w/racer"}, busy) == ""


def test_the_queue_warning_stays_silent_when_the_queue_cannot_be_read():
    """A false "your work will wait" costs trust; a missing one costs surprise."""
    from supervisor.events import _project_lane_wait_suffix

    class Unreadable:
        def values(self):
            raise RuntimeError("queue unavailable")

    assert _project_lane_wait_suffix(
        {"id": "t1", "project_id": "racer", "workspace_root": "/w/racer"}, Unreadable(),
    ) == ""
