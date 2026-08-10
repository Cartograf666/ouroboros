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
    _registry_path,
    begin_project_deletion,
    bind_task_to_project,
    complete_project_deletion,
    create_project,
    create_thread,
    fork_thread,
)
from ouroboros.thread_history import MAX_ANCESTRY_DEPTH, thread_ancestry_lens
from ouroboros.utils import atomic_write_json, read_json_dict


def _rewrite_thread(tmp_path, project_id, thread_id, **fields):
    """Hand-edit a stored thread row (the only way these states are reachable)."""
    data = read_json_dict(_registry_path(tmp_path))
    for entry in data["projects"]:
        if entry.get("id") != project_id:
            continue
        for row in entry.get("threads") or []:
            if int(row["id"]) == int(thread_id):
                row.update(fields)
    atomic_write_json(_registry_path(tmp_path), data)


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


def _agent_chat_section(tmp_path, thread_chat_id):
    """The '## Recent chat' section the AGENT sees for one thread."""
    from ouroboros.context import build_recent_sections

    class _Memory:
        drive_root = tmp_path

        def read_jsonl_tail(self, name, limit):
            path = tmp_path / "logs" / name
            if not path.is_file():
                return []
            return [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ][-limit:]

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
        _Memory(), object(), task_id="", thread_chat_id=thread_chat_id
    )
    return next((s for s in sections if s.startswith("## Recent chat")), "")


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
    create_project(tmp_path, "racer")
    parent = create_thread(tmp_path, "racer", name="Parent")
    _rows(tmp_path, [_chat_row(parent["chat_id"], "2026-01-01T00:00:00+00:00", "shared-past")])
    fork = fork_thread(tmp_path, "racer", parent["id"])
    _rows(tmp_path, [
        _chat_row(parent["chat_id"], "2027-01-01T00:00:00+00:00", "parent-moved-on"),
        _chat_row(fork["chat_id"], "2027-01-02T00:00:00+00:00", "fork-own"),
    ])

    chat_section = _agent_chat_section(tmp_path, fork["chat_id"])

    assert "shared-past" in chat_section
    assert "fork-own" in chat_section
    assert "parent-moved-on" not in chat_section


# --------------------------------------------------------------------------- #
# T0 FIX round: the two surfaces must answer the SAME question
# --------------------------------------------------------------------------- #
def test_a_post_hoc_bound_task_lands_in_both_surfaces_identically(tmp_path):
    """The divergence that refuted this phase's core claim.

    ``context.py`` resolved a post-hoc binding with ``_bound.get(task) ==
    thread_chat_id`` — the thread's OWN chat, own task id only — while
    ``gateway/history.py`` routed the same binding through ``lens.admits`` by
    task LINEAGE. So a task bound to a PARENT thread appeared in the fork's UI
    history and was invisible to the agent working in that fork, and a subagent
    row (bound only through its root) was invisible to the agent everywhere.
    """
    from ouroboros.gateway.history import _assemble_history_response

    create_project(tmp_path, "racer")
    parent = create_thread(tmp_path, "racer", name="Parent")
    bind_task_to_project(
        tmp_path, "task-9", "racer", parent["chat_id"],
        origin={"absent": "post_hoc_unresolved"},
    )
    # The bound task's rows keep their ORIGINAL main chat_id (that is the whole
    # point of a post-hoc binding) — one of its own, one of a subagent.
    _rows(tmp_path, [
        _chat_row(1, "2026-01-01T00:00:00+00:00", "bound-row", task_id="task-9"),
        _chat_row(
            1, "2026-01-01T00:00:01+00:00", "bound-child",
            task_id="task-9-child", root_task_id="task-9",
        ),
    ])
    fork = fork_thread(tmp_path, "racer", parent["id"])
    _rows(tmp_path, [
        _chat_row(1, "2099-01-01T00:00:00+00:00", "bound-after-fork", task_id="task-9"),
        _chat_row(1, "2026-01-01T00:00:00+00:00", "unbound-main"),
    ])

    ui = [
        m["text"]
        for m in json.loads(_assemble_history_response(tmp_path, fork["chat_id"], 50, 10))["messages"]
    ]
    agent = _agent_chat_section(tmp_path, fork["chat_id"])

    for text in ("bound-row", "bound-child"):
        assert text in ui, f"{text} missing from the owner's history"
        assert text in agent, f"{text} missing from the agent's context"
    # ...and both stay bounded by the SAME cutoff and the same ownership rule.
    for text in ("bound-after-fork", "unbound-main"):
        assert text not in ui and text not in agent


def test_both_surfaces_build_the_lens_with_source_refs(tmp_path):
    """A converted project's start message lives in Main and is reachable only
    through the binding's source ref. Building the agent's lens WITHOUT refs
    while the history endpoint built it WITH them was the second half of the
    same divergence."""
    from ouroboros.gateway.history import _assemble_history_response

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
    _rows(tmp_path, [{
        "chat_id": 1, "ts": "2026-01-01T00:00:00+00:00", "direction": "in",
        "text": text, "client_message_id": "cm-origin",
    }])
    fork = fork_thread(tmp_path, "conv", 0)

    ui = [
        m["text"]
        for m in json.loads(_assemble_history_response(tmp_path, fork["chat_id"], 50, 10))["messages"]
    ]
    assert text in ui
    assert text in _agent_chat_section(tmp_path, fork["chat_id"])


def test_an_unbound_ancestor_never_enters_the_lens(tmp_path):
    """A hand-written ``fork_of_chat_id: 1`` would pour the WHOLE Main chat into
    a project thread's history AND the agent's focused context, silently. The
    walk refuses an ancestor with no project binding BEFORE it enters the
    cutoffs, and discloses the refusal."""
    from ouroboros.gateway.history import _assemble_history_response

    create_project(tmp_path, "racer")
    thread = create_thread(tmp_path, "racer", name="T")
    _rewrite_thread(
        tmp_path, "racer", thread["id"],
        fork_of_chat_id=1, fork_before_ts="2030-01-01T00:00:00+00:00",
    )
    _rows(tmp_path, [
        _chat_row(1, "2026-01-01T00:00:00+00:00", "private-main-chat"),
        _chat_row(thread["chat_id"], "2026-01-02T00:00:00+00:00", "own"),
    ])

    lens = thread_ancestry_lens(tmp_path, thread["chat_id"])
    assert lens.cutoffs == {thread["chat_id"]: ""}
    assert lens.admits(1, "2026-01-01T00:00:00+00:00") is False
    assert lens.truncated is True          # refused, not silently dropped

    payload = json.loads(_assemble_history_response(tmp_path, thread["chat_id"], 50, 10))
    texts = [m["text"] for m in payload["messages"]]
    assert "own" in texts and "private-main-chat" not in texts
    assert "ancestry_depth" in payload["window"]["truncated_by"]

    agent = _agent_chat_section(tmp_path, thread["chat_id"])
    assert "own" in agent and "private-main-chat" not in agent


def test_an_ancestor_in_another_project_never_enters_the_lens(tmp_path):
    """An ancestor bound to a DIFFERENT project is just as foreign as an unbound
    one: a hand-written ``fork_of_chat_id`` pointing at project beta would pour
    beta's whole conversation into an alpha thread — on the owner's history AND
    in the agent's focused context — with ``truncated`` left False, so nothing
    even disclosed the crossing."""
    from ouroboros.gateway.history import _assemble_history_response

    create_project(tmp_path, "alpha")
    create_project(tmp_path, "beta")
    mine = create_thread(tmp_path, "alpha", name="Mine")
    theirs = create_thread(tmp_path, "beta", name="Theirs")
    _rewrite_thread(
        tmp_path, "alpha", mine["id"],
        fork_of_chat_id=theirs["chat_id"], fork_before_ts="2030-01-01T00:00:00+00:00",
    )
    _rows(tmp_path, [
        _chat_row(theirs["chat_id"], "2026-01-01T00:00:00+00:00", "beta-private"),
        _chat_row(mine["chat_id"], "2026-01-02T00:00:00+00:00", "alpha-own"),
    ])

    lens = thread_ancestry_lens(tmp_path, mine["chat_id"])
    assert lens.cutoffs == {mine["chat_id"]: ""}
    assert lens.admits(theirs["chat_id"], "2026-01-01T00:00:00+00:00") is False
    assert lens.truncated is True          # refused, not silently crossed

    payload = json.loads(
        _assemble_history_response(tmp_path, mine["chat_id"], 50, 10)
    )
    texts = [m["text"] for m in payload["messages"]]
    assert "alpha-own" in texts and "beta-private" not in texts
    assert "ancestry_depth" in payload["window"]["truncated_by"]

    agent = _agent_chat_section(tmp_path, mine["chat_id"])
    assert "alpha-own" in agent and "beta-private" not in agent


def test_a_cycle_never_narrows_the_requesting_threads_own_present(tmp_path):
    """A self-parent (or A->B->A) used to tighten the REQUESTING chat's cutoff,
    so a thread started rejecting the messages it had just sent."""
    create_project(tmp_path, "racer")
    solo = create_thread(tmp_path, "racer", name="Solo")
    _rewrite_thread(
        tmp_path, "racer", solo["id"],
        fork_of_chat_id=solo["chat_id"], fork_before_ts="2020-01-01T00:00:00+00:00",
    )

    lens = thread_ancestry_lens(tmp_path, solo["chat_id"])
    assert lens.cutoffs[solo["chat_id"]] == ""                    # unbounded
    assert lens.admits(solo["chat_id"], "2030-01-01T00:00:00+00:00") is True
    assert lens.truncated is True

    # ...and the two-hop variant A -> B -> A.
    a = create_thread(tmp_path, "racer", name="A")
    b = create_thread(tmp_path, "racer", name="B")
    _rewrite_thread(tmp_path, "racer", a["id"],
                    fork_of_chat_id=b["chat_id"], fork_before_ts="2026-01-01T00:00:00+00:00")
    _rewrite_thread(tmp_path, "racer", b["id"],
                    fork_of_chat_id=a["chat_id"], fork_before_ts="2020-01-01T00:00:00+00:00")

    looped = thread_ancestry_lens(tmp_path, a["chat_id"])
    assert looped.cutoffs[a["chat_id"]] == ""
    assert looped.admits(a["chat_id"], "2030-01-01T00:00:00+00:00") is True
    assert looped.cutoffs[b["chat_id"]] == "2026-01-01T00:00:00+00:00"
    assert looped.truncated is True


def test_a_bounded_ancestry_is_disclosed_end_to_end(tmp_path, monkeypatch):
    """ARCHITECTURE promises the ``truncated`` flag is DISCLOSED. It was set by
    the lens and consumed by nobody: the response still called itself complete
    while part of the shared past had not been read."""
    import ouroboros.thread_history as th
    from ouroboros.gateway.history import _assemble_history_response

    monkeypatch.setattr(th, "MAX_ANCESTRY_DEPTH", 2)
    create_project(tmp_path, "racer")
    tip = create_thread(tmp_path, "racer", name="root")
    for _ in range(4):
        tip = fork_thread(tmp_path, "racer", tip["id"])
    _rows(tmp_path, [_chat_row(tip["chat_id"], "2026-01-01T00:00:00+00:00", "own")])

    payload = json.loads(_assemble_history_response(tmp_path, tip["chat_id"], 50, 10))
    assert payload["window"]["complete"] is False
    assert "ancestry_depth" in payload["window"]["truncated_by"]

    # An ordinary thread discloses nothing extra.
    plain = create_thread(tmp_path, "racer", name="Plain")
    _rows(tmp_path, [_chat_row(plain["chat_id"], "2026-01-03T00:00:00+00:00", "plain")])
    ok = json.loads(_assemble_history_response(tmp_path, plain["chat_id"], 50, 10))
    assert "ancestry_depth" not in ok["window"]["truncated_by"]


def test_ancestor_origin_rows_come_from_one_bindings_read(tmp_path):
    """T0-12: the origin fallback asked project_origin_rows per ancestor, so a
    fork chain re-read state/project_task_bindings.json once per link and could
    synthesize ONE owner message several times."""
    import ouroboros.projects_registry as registry
    from ouroboros.gateway.history import _origin_fallback_rows

    project_chat = create_project(tmp_path, "conv")["chat_id"]
    text = "start the project"
    ref = {
        "chat_id": 1, "client_message_id": "cm-origin",
        "ts": "2026-01-01T00:00:00+00:00", "text_sha256": _text_sha256(text),
    }
    # Two bindings of the SAME owner message, on two chats of the ancestry.
    bind_task_to_project(tmp_path, "task-1", "conv", project_chat,
                         origin={"ref": ref, "text": text})
    child = create_thread(tmp_path, "conv", name="Child")
    bind_task_to_project(tmp_path, "task-2", "conv", child["chat_id"],
                         origin={"ref": ref, "text": text})
    fork = fork_thread(tmp_path, "conv", child["id"])
    _rewrite_thread(tmp_path, "conv", fork["id"], fork_of_chat_id=child["chat_id"])

    lens = thread_ancestry_lens(tmp_path, fork["chat_id"], with_source_refs=False)
    assert len(lens.order) >= 2                          # a real chain

    reads = {"n": 0}
    original = registry.project_task_bindings

    def _counting(*args, **kwargs):
        reads["n"] += 1
        return original(*args, **kwargs)

    registry.project_task_bindings = _counting
    try:
        synthesized = _origin_fallback_rows(tmp_path, lens, [])
    finally:
        registry.project_task_bindings = original

    assert reads["n"] == 1, "one bucketed bindings read for the whole chain"
    # ONE row despite two ancestor bindings holding the same origin identity.
    assert [row["text"] for row in synthesized] == [text]


# --------------------------------------------------------------------------- #
# P6 — "could not read the binding" is a THIRD state, and it is disclosed
# --------------------------------------------------------------------------- #

def test_an_unreadable_registry_is_disclosed_on_both_surfaces(tmp_path, monkeypatch):
    """P6: a fork could lose its entire ancestry and be told the window is complete.

    The reviewer blamed the outer `except` in `history.py` / `context.py`. Those
    are near-unreachable: `_chat_binding` ALREADY swallowed a registry failure to
    `{}`, and an empty binding for the REQUESTED chat took the degenerate
    own-thread early return with `truncated=False`. No exception was raised at all,
    so the silent path had nothing to catch. Reproduced: healthy lens sees the
    parent, registry made unreadable -> lens sees only itself, `truncated=False`,
    and `_window_metadata` answered `{'complete': True, 'truncated_by': []}`.
    """
    import ouroboros.projects_registry as reg
    from ouroboros.gateway import history as gw
    from ouroboros import thread_history

    folder = tmp_path / "folder"
    folder.mkdir()
    create_project(tmp_path, "alpha", name="Alpha", working_dir=str(folder))
    parent = create_thread(tmp_path, "alpha", name="Parent")
    forked = fork_thread(tmp_path, "alpha", parent["id"])
    fork_chat = int(forked["chat_id"])

    healthy = thread_ancestry_lens(tmp_path, fork_chat)
    assert healthy.has_ancestors is True
    assert healthy.truncated is False
    assert healthy.lens_unavailable is False

    def unreadable(*a, **k):
        raise OSError("Input/output error")

    monkeypatch.setattr(reg, "_chat_binding_index", unreadable)

    degraded = thread_ancestry_lens(tmp_path, fork_chat)
    assert degraded.has_ancestors is False, "the whole ancestry is gone"
    assert degraded.lens_unavailable is True, "and it says so"
    assert degraded.truncated is True, "so every existing consumer already reacts"

    meta = gw._window_metadata(
        chat_quota_rows=10, progress_quota_rows=10, n_human=10, n_progress=10,
        chat_path=tmp_path / "chat.jsonl", progress_path=tmp_path / "progress.jsonl",
        archive_dir=tmp_path / "archive", human_rows_dropped=False,
        lineage_truncated=False, lens=degraded,
    )
    assert meta["complete"] is False
    assert "lens_unavailable" in meta["truncated_by"]
    assert "ancestry_depth" in meta["truncated_by"]

    # `_chat_binding` now has THREE answers, and only the failure is `None`.
    assert thread_history._chat_binding(tmp_path, fork_chat) is None
    monkeypatch.undo()
    assert thread_history._chat_binding(tmp_path, 1) == {}


def test_a_genuine_non_project_chat_is_still_complete(tmp_path):
    """The distinction has to cut both ways: Main and an external transport really
    have no ancestry, and marking THOSE truncated would cry wolf on every read."""
    from ouroboros.gateway import history as gw

    lens = thread_ancestry_lens(tmp_path, 1)
    assert lens.has_ancestors is False
    assert lens.truncated is False
    assert lens.lens_unavailable is False
    meta = gw._window_metadata(
        chat_quota_rows=10, progress_quota_rows=10, n_human=10, n_progress=10,
        chat_path=tmp_path / "chat.jsonl", progress_path=tmp_path / "progress.jsonl",
        archive_dir=tmp_path / "archive", human_rows_dropped=False,
        lineage_truncated=False, lens=lens,
    )
    assert meta == {"complete": True, "truncated_by": []}


def test_an_unreadable_ancestor_binding_is_unavailable_not_merely_refused(tmp_path, monkeypatch):
    """Mid-chain, the same two facts stay apart: an ancestor the registry REFUSES
    (unbound, or another project's) is `truncated` alone; an ancestor whose binding
    could not be READ is also `lens_unavailable`, because it may be recoverable."""
    from ouroboros import thread_history

    folder = tmp_path / "folder"
    folder.mkdir()
    create_project(tmp_path, "alpha", name="Alpha", working_dir=str(folder))
    parent = create_thread(tmp_path, "alpha", name="Parent")
    forked = fork_thread(tmp_path, "alpha", parent["id"])
    fork_chat = int(forked["chat_id"])
    parent_chat = int(parent["chat_id"])

    real = thread_history._chat_binding

    def only_the_parent_is_unreadable(drive_root, chat_id):
        if int(chat_id) == parent_chat:
            return None
        return real(drive_root, chat_id)

    monkeypatch.setattr(thread_history, "_chat_binding", only_the_parent_is_unreadable)

    lens = thread_history.thread_ancestry_lens(tmp_path, fork_chat)
    assert lens.has_ancestors is False
    assert lens.truncated is True
    assert lens.lens_unavailable is True


def test_the_agent_context_discloses_an_unavailable_lens(tmp_path, monkeypatch):
    """The context half. Degrading to own-thread rows is the right NARROWING; doing
    it silently handed the agent a view that looked complete."""
    from ouroboros import context as ctx
    from ouroboros.memory import Memory

    folder = tmp_path / "folder"
    folder.mkdir()
    create_project(tmp_path, "alpha", name="Alpha", working_dir=str(folder))
    parent = create_thread(tmp_path, "alpha", name="Parent")
    forked = fork_thread(tmp_path, "alpha", parent["id"])
    fork_chat = int(forked["chat_id"])

    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "chat.jsonl").write_text(
        json.dumps({"chat_id": fork_chat, "ts": "2026-01-02T00:00:00Z",
                    "role": "user", "content": "own turn"}) + "\n",
        encoding="utf-8",
    )

    import ouroboros.thread_history as th

    def boom(*a, **k):
        raise OSError("registry unreadable")

    monkeypatch.setattr(th, "thread_ancestry_lens", boom)

    sections = ctx.build_recent_sections(Memory(tmp_path), env=None, thread_chat_id=fork_chat)
    gaps = [s for s in sections if s.startswith("## Conversation gaps in this view")]
    assert gaps, sections
    assert "fork history could not be read" in gaps[0]
    # ...and the caveat is read BEFORE the conversation it qualifies.
    chat_index = next(
        (i for i, s in enumerate(sections) if s.startswith("## Recent chat")), len(sections)
    )
    assert sections.index(gaps[0]) < chat_index
