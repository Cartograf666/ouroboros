"""Multi-project lease + registry + chat-id policy (v6.32.0)."""

from __future__ import annotations

import os

from ouroboros.contracts.chat_id_policy import (
    PROJECT_CHAT_ID_MIN,
    WEB_UI_CHAT_ID,
    is_a2a_chat_id,
    is_project_chat_id,
    project_chat_id,
)
from ouroboros.project_lease import (
    candidate_is_leasable,
    running_project_ids,
    running_project_lanes,
)


def _task(project_id="", role="", tid="t1", workspace_root=""):
    task = {"id": tid, "type": "task"}
    if project_id:
        task["project_id"] = project_id
    if role:
        task["delegation_role"] = role
    if workspace_root:
        task["workspace_root"] = workspace_root
    return task


def _meta(task):
    """Production RUNNING value shape: meta dict wrapping the task."""
    return {"task": task, "worker_id": 0, "last_heartbeat_at": 1.0}


def test_running_project_lanes_counts_top_level_scoped_tasks_only():
    # Mix the PRODUCTION meta shape (workers.py RUNNING values) with bare task
    # dicts — the lane query must unwrap meta and still count both.
    running = [
        _meta(_task("alpha")),               # production shape
        _task("beta"),                       # bare task dict
        _meta(_task("", tid="plain")),       # unscoped: no lane
        _meta(_task("gamma", role="subagent")),  # swarm member: no lease of its own
        "garbage",
        None,
    ]
    assert running_project_lanes(running) == {("alpha", ""), ("beta", "")}
    # The project-WIDE ACTIVITY query (merge/remove preconditions) is a SEPARATE
    # answer, deliberately not the lease key — and it counts the SUBAGENT the
    # lane exempts (T3R-14). That exemption is a SCHEDULING rule, so a swarm
    # cannot deadlock against its own parent; a subagent still runs commands and
    # writes files, so "is anything happening in this project" must see it.
    # Exempting it here let a merge rewrite the folder mid-swarm-write.
    assert running_project_ids(running) == {"alpha", "beta", "gamma"}


def test_the_activity_query_counts_QUEUED_work_too():
    """T3R-14, stated plainly rather than left to be inferred: a PENDING task for
    this project can be assigned at ANY instant, including the one right after
    this answer is read, and a merge holds no lock against the scheduler.

    Counting it costs the owner a wait. Not counting it costs a folder rewritten
    under a task that has just started.
    """
    running = [_meta(_task("alpha"))]
    pending = [_task("beta", tid="queued"), _task("", tid="unscoped")]

    assert running_project_ids(running, pending) == {"alpha", "beta"}
    # Omitted, the answer is about running work alone — a caller that can only
    # see one collection still gets a true answer about that one.
    assert running_project_ids(running) == {"alpha"}


def test_running_project_lanes_unwraps_production_meta_shape():
    """Regression for the inert-lease bug: RUNNING.values() are meta dicts."""
    running = {"t1": _meta(_task("racer"))}.values()
    lanes = running_project_lanes(running)
    assert lanes == {("racer", "")}
    assert candidate_is_leasable(_task("racer", tid="t2"), lanes) is False


def test_candidate_is_leasable_matrix():
    leased = {("alpha", "")}
    # Unscoped tasks never serialize.
    assert candidate_is_leasable(_task(""), leased) is True
    # A second writer for a leased project's own folder waits.
    assert candidate_is_leasable(_task("alpha"), leased) is False
    # A different project proceeds in parallel.
    assert candidate_is_leasable(_task("beta"), leased) is True
    # The leased project's OWN subagents must not deadlock the swarm.
    assert candidate_is_leasable(_task("alpha", role="subagent"), leased) is True


def test_lane_is_keyed_on_the_folder_whenever_a_task_names_one():
    """The precondition that makes "branch off for parallel work" real.

    Two threads of ONE project in the SAME folder must still serialize; a
    thread branched off into its own git worktree gets its own lane and runs
    concurrently. Keying the lane on project_id alone made branching a promise
    the queue could not keep.
    """
    main_folder = _task("alpha", tid="t1", workspace_root="/w/alpha")
    same_folder = _task("alpha", tid="t2", workspace_root="/w/alpha")
    branched = _task("alpha", tid="t3", workspace_root="/w/alpha-thread-2")

    lanes = running_project_lanes([_meta(main_folder)])

    assert candidate_is_leasable(same_folder, lanes) is False
    assert candidate_is_leasable(branched, lanes) is True
    # ...and the branched worktree then holds a lane of its own.
    both = running_project_lanes([_meta(main_folder), _meta(branched)])
    assert len(both) == 2
    assert candidate_is_leasable(_task("alpha", tid="t4", workspace_root="/w/alpha-thread-2"), both) is False
    # The project-wide activity query still sees ONE busy project.
    assert running_project_ids([_meta(main_folder), _meta(branched)]) == {"alpha"}


def test_two_projects_on_one_folder_share_the_lane():
    """T0R2-5, a DELIBERATE reversal of T0's `(project_id, workspace_root)` key.

    Folder exclusivity used to hold only WITHIN one project: two projects
    attached to the same folder each got their own lane and ran at the same
    time, while the lane docstring and ARCHITECTURE both stated the invariant as
    "one folder is one writer lane". A named folder is now the lane key on its
    own, so the claim and the behaviour finally agree.
    """
    alpha = _task("alpha", tid="t1", workspace_root="/w/shared")
    beta = _task("beta", tid="t2", workspace_root="/w/shared")

    lanes = running_project_lanes([_meta(alpha)])

    assert lanes == {("", os.path.normcase("/w/shared"))}
    assert candidate_is_leasable(beta, lanes) is False
    # A task that names NO folder is narrower on purpose: nothing at this layer
    # may read the registry, so it can only serialize within its own project.
    assert candidate_is_leasable(_task("beta", tid="t3"), lanes) is True
    assert running_project_lanes([_meta(_task("beta", tid="t3"))]) == {("beta", "")}


def test_lane_reads_the_metadata_mirror_and_normalizes_the_path():
    """workspace_root rides both the task record and its metadata mirror; the
    comparison is pure normpath/normcase (no filesystem access under the queue
    lock), so a trailing slash or a redundant segment is the SAME lane."""
    mirrored = {"id": "t1", "project_id": "alpha", "metadata": {"workspace_root": "/w/alpha"}}
    noisy = _task("alpha", tid="t2", workspace_root="/w/alpha/./")

    lanes = running_project_lanes([_meta(mirrored)])
    assert lanes == {("", os.path.normcase("/w/alpha"))}
    assert candidate_is_leasable(noisy, lanes) is False


def test_lane_folds_case_on_a_case_insensitive_filesystem():
    """T0R2-4: `normcase` lowercases on win32 ONLY, so on macOS `/Users/x/Repo`
    and `/Users/x/repo` — the SAME folder — produced two lanes and admitted two
    writers onto it. The docstring claimed a case-insensitive lane it did not
    deliver; now it delivers one wherever the platform's filesystem is."""
    import sys

    upper = _task("alpha", tid="t1", workspace_root="/W/Alpha")
    lower = _task("beta", tid="t2", workspace_root="/w/alpha")

    lanes = running_project_lanes([_meta(upper)])

    if sys.platform in ("darwin", "win32"):
        assert candidate_is_leasable(lower, lanes) is False
    else:
        # A case-SENSITIVE filesystem genuinely has two folders here, and
        # folding them together would serialize two unrelated writers.
        assert candidate_is_leasable(lower, lanes) is True


def test_a_running_lane_is_pinned_and_cannot_drift():
    """T0R2-7: a live writer must not be moved off the folder it is writing in.

    The lane used to be recomputed from the task record on every assignment
    pass, so a mid-run mutation (the post-hoc project conversion is the live
    one) released the lane the task actually held and let a second writer into
    the same folder.
    """
    from ouroboros.project_lease import LANE_PIN_FIELD, pin_task_lane

    running = _task("alpha", tid="t1", workspace_root="/w/alpha")
    assert pin_task_lane(running) == ("", os.path.normcase("/w/alpha"))
    assert running[LANE_PIN_FIELD] == ["", os.path.normcase("/w/alpha")]

    # Something edits the live record underneath the writer.
    running["workspace_root"] = "/w/somewhere-else"
    running["project_id"] = "beta"

    lanes = running_project_lanes([_meta(running)])
    assert lanes == {("", os.path.normcase("/w/alpha"))}
    assert candidate_is_leasable(
        _task("alpha", tid="t2", workspace_root="/w/alpha"), lanes
    ) is False
    # The pin is write-once: re-pinning never overwrites a held lane.
    assert pin_task_lane(running) == ("", os.path.normcase("/w/alpha"))


def test_post_hoc_conversion_pins_the_lane_it_acquires():
    """A RUNNING task that was never a lane occupant ACQUIRES one when the owner
    converts it into a project. That is a deliberate take, not drift, so it pins
    on the spot — otherwise a later registry edit could move a live writer."""
    from ouroboros.project_lease import LANE_PIN_FIELD, mark_task_project

    unscoped = {"id": "t1", "workspace_root": "/w/alpha"}
    running = {"t1": {"task": unscoped}}
    assert running_project_lanes(running.values()) == set()

    assert mark_task_project(running, [], "t1", "alpha") is True

    assert unscoped[LANE_PIN_FIELD] == ["", os.path.normcase("/w/alpha")]
    assert running_project_lanes(running.values()) == {("", os.path.normcase("/w/alpha"))}


def test_project_chat_id_policy():
    assert is_project_chat_id(WEB_UI_CHAT_ID) is False
    assert is_project_chat_id(-5) is False
    cid = project_chat_id("my-game")
    assert cid >= PROJECT_CHAT_ID_MIN
    assert is_project_chat_id(cid) is True
    assert is_a2a_chat_id(cid) is False
    # Deterministic and id-sensitive.
    assert project_chat_id("my-game") == cid
    assert project_chat_id("other") != cid
    # Empty scope falls back to the main chat.
    assert project_chat_id("") == WEB_UI_CHAT_ID


def test_registry_create_idempotent_and_summary(tmp_path):
    from ouroboros.projects_registry import (
        create_project,
        get_project,
        list_projects,
        projects_summary,
    )

    entry = create_project(tmp_path, "racer", name="Cyber Racer")
    assert entry["id"] == "racer"
    assert "status" not in entry  # statuses removed (v6.33.0)
    assert entry["chat_id"] == project_chat_id("racer")

    again = create_project(tmp_path, "racer", name="ignored on existing")
    assert again["name"] == "Cyber Racer"
    assert len(list_projects(tmp_path)) == 1

    rows = projects_summary(tmp_path)
    assert rows and rows[0]["id"] == "racer" and rows[0]["chat_id"] == entry["chat_id"]
    assert "status" not in rows[0]
    assert get_project(tmp_path, "missing") is None


def test_registry_reconcile_registers_existing_stores_never_prunes(tmp_path):
    from ouroboros.projects_registry import create_project, list_projects, reconcile_projects

    create_project(tmp_path, "kept")
    (tmp_path / "projects" / "legacy-store" / "knowledge").mkdir(parents=True)

    added = reconcile_projects(tmp_path)

    assert added == 1
    ids = {p["id"] for p in list_projects(tmp_path)}
    assert ids == {"kept", "legacy-store"}
    # Second run is a no-op (idempotent) and nothing is pruned.
    assert reconcile_projects(tmp_path) == 0
    assert {p["id"] for p in list_projects(tmp_path)} == ids


def test_journal_and_workpad_roundtrip(tmp_path, monkeypatch):
    import types

    # Scope the project store to tmp_path WITHOUT importlib.reload(config): a
    # reload permanently rebinds ouroboros.config.DATA_DIR for the rest of the
    # pytest process (monkeypatch restores only the env var, not the reloaded
    # module), polluting later tests. project_facts reads config.DATA_DIR at call
    # time, so monkeypatch.setattr (auto-restored) is sufficient and isolated.
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    from ouroboros.tools import project_journal as pj

    ctx = types.SimpleNamespace(project_id="racer", task_id="t-9", drive_root=tmp_path)
    tools = {t.name: t for t in pj.get_tools()}

    out = tools["journal_write"].handler(ctx, kind="start", text="Bootstrapping the racer")
    assert out.startswith("OK:")
    out = tools["journal_write"].handler(ctx, kind="bogus", text="x")
    assert "TOOL_ARG_ERROR" in out
    listing = tools["journal_read"].handler(ctx)
    assert "Bootstrapping the racer" in listing and "START" in listing

    assert tools["workpad_write"].handler(ctx, content="## plan\n- wheels").startswith("OK:")
    assert "wheels" in tools["workpad_read"].handler(ctx)

    digest = pj.journal_tail_digest("racer")
    assert "Bootstrapping the racer" in digest

    # Unscoped ctx without explicit id refuses honestly.
    bare = types.SimpleNamespace(project_id="", task_id="t", drive_root=tmp_path)
    assert "no project scope" in tools["journal_write"].handler(bare, kind="note", text="x")


def test_a_candidate_is_compared_by_what_it_DESCRIBES_never_by_a_stale_pin():
    """Only OCCUPANCY reads the pin. A pending candidate holds nothing.

    A crash retry re-enqueues the very dict the pin was stamped on, so if a
    candidate were compared by its pin it would be matched against a lane it no
    longer holds — over-serializing at best, and under-serializing the moment
    the record legitimately named a different folder.
    """
    from ouroboros.project_lease import pin_task_lane

    retried = _task("alpha", tid="t1", workspace_root="/w/alpha")
    pin_task_lane(retried)
    # The retry will write in a DIFFERENT folder than the run that was pinned.
    retried["workspace_root"] = "/w/beta"

    busy_beta = running_project_lanes([_meta(_task("alpha", tid="t9", workspace_root="/w/beta"))])

    assert candidate_is_leasable(retried, busy_beta) is False
    stale_alpha = running_project_lanes([_meta(_task("alpha", tid="t9", workspace_root="/w/alpha"))])
    assert candidate_is_leasable(retried, stale_alpha) is True
