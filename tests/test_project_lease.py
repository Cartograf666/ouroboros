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
    WILDCARD_WORKSPACE,
    candidate_is_leasable,
    mark_task_project,
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
    # No task carries a workspace_root and no project folder map was supplied,
    # so each lane's folder is UNKNOWN -> the wildcard, never a lane of its own.
    assert running_project_lanes(running) == {
        ("alpha", WILDCARD_WORKSPACE), ("beta", WILDCARD_WORKSPACE)
    }
    # The project-WIDE activity query (merge/remove preconditions) is a
    # SEPARATE answer and is deliberately not the lease key.
    assert running_project_ids(running) == {"alpha", "beta"}


def test_running_project_lanes_unwraps_production_meta_shape():
    """Regression for the inert-lease bug: RUNNING.values() are meta dicts."""
    running = {"t1": _meta(_task("racer"))}.values()
    lanes = running_project_lanes(running)
    assert lanes == {("racer", WILDCARD_WORKSPACE)}
    assert candidate_is_leasable(_task("racer", tid="t2"), lanes) is False


def test_candidate_is_leasable_matrix():
    leased = {("alpha", WILDCARD_WORKSPACE)}
    # Unscoped tasks never serialize.
    assert candidate_is_leasable(_task(""), leased) is True
    # A second writer for a leased project's own folder waits.
    assert candidate_is_leasable(_task("alpha"), leased) is False
    # A different project proceeds in parallel.
    assert candidate_is_leasable(_task("beta"), leased) is True
    # The leased project's OWN subagents must not deadlock the swarm.
    assert candidate_is_leasable(_task("alpha", role="subagent"), leased) is True


def test_lane_is_keyed_on_project_AND_workspace_root():
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


def test_lane_reads_the_metadata_mirror_and_normalizes_the_path():
    """workspace_root rides both the task record and its metadata mirror; the
    comparison is pure normpath/normcase (no filesystem access under the queue
    lock), so a trailing slash or a redundant segment is the SAME lane."""
    mirrored = {"id": "t1", "project_id": "alpha", "metadata": {"workspace_root": "/w/alpha"}}
    noisy = _task("alpha", tid="t2", workspace_root="/w/alpha/./")

    lanes = running_project_lanes([_meta(mirrored)])
    assert lanes == {("alpha", os.path.normcase("/w/alpha"))}
    assert candidate_is_leasable(noisy, lanes) is False


def test_a_post_hoc_scoped_task_shares_the_project_folder_lane():
    """The regression this decision closes: only the promote/room path stamps
    workspace_root. A task scoped POST-HOC through mark_task_project carries the
    project id alone, so comparing the raw field split ONE folder into two lanes
    — ("alpha", "/w/alpha") and ("alpha", "") — and let TWO top-level writers
    into it, which is strictly worse than the project-wide lease this key
    replaced."""
    room_task = _task("alpha", tid="t1", workspace_root="/w/alpha")
    converted = {"id": "t2", "type": "task"}
    assert mark_task_project({}, [converted], "t2", "alpha") is True
    assert "workspace_root" not in converted        # the SSOT stamps only the id

    folders = {"alpha": "/w/alpha"}
    lanes = running_project_lanes([_meta(room_task)], folders)
    assert lanes == {("alpha", os.path.normcase("/w/alpha"))}
    # The empty workspace resolves to the project's registered working_dir.
    assert candidate_is_leasable(converted, lanes, folders) is False
    # A thread branched into its OWN worktree still runs concurrently.
    branched = _task("alpha", tid="t3", workspace_root="/w/alpha-thread-2")
    assert candidate_is_leasable(branched, lanes, folders) is True


def test_an_unknown_project_folder_is_a_wildcard_lane():
    """Fail-safe: when neither the task record nor the registry can name the
    folder, the lane conflicts with EVERY lane of its project. Never
    parallel-by-accident."""
    room_task = _task("alpha", tid="t1", workspace_root="/w/alpha")
    unknown = {"id": "t2", "type": "task", "project_id": "alpha"}

    # No folder map at all (unreadable registry / file-less project).
    lanes = running_project_lanes([_meta(room_task)], {})
    assert candidate_is_leasable(unknown, lanes, {}) is False
    # ...and symmetrically: a wildcard HOLDER blocks a folder-bearing candidate.
    held_wild = running_project_lanes([_meta(unknown)], {})
    assert held_wild == {("alpha", WILDCARD_WORKSPACE)}
    assert candidate_is_leasable(room_task, held_wild, {}) is False
    # A different project is untouched by either.
    assert candidate_is_leasable(_task("beta", tid="t9"), held_wild, {}) is True


def test_workspace_none_task_still_serializes_against_its_project_folder():
    """`workspace="none"` is an explicit opt-out that yields NO workspace_root
    (workspace_admission.resolve_room_workspace). "I write nowhere" is not a
    claim the lease can verify, so it queues behind the folder's writer."""
    room_task = _task("alpha", tid="t1", workspace_root="/w/alpha")
    opted_out = {"id": "t2", "type": "task", "project_id": "alpha", "workspace": "none"}
    folders = {"alpha": "/w/alpha"}

    lanes = running_project_lanes([_meta(room_task)], folders)
    assert candidate_is_leasable(opted_out, lanes, folders) is False


def test_case_and_symlink_normalization_boundaries():
    """`normcase` is a NO-OP on POSIX (it matters for case-insensitive Windows/
    macOS spellings) and this module never touches the filesystem, so SYMLINK
    resolution is a RECORD-WRITE-time job: workspace_admission resolves a task's
    workspace_root and projects_registry resolves a project's working_dir before
    either is stored. Both therefore arrive here already realpath'd."""
    room = _task("alpha", tid="t1", workspace_root="/w/Alpha")
    lanes = running_project_lanes([_meta(room)])
    # Spelling equality is whatever normcase says on THIS platform — identity on
    # POSIX, case-folded on a case-insensitive one.
    same_case = _task("alpha", tid="t2", workspace_root="/w/Alpha/")
    assert candidate_is_leasable(same_case, lanes) is False
    other_case = _task("alpha", tid="t3", workspace_root="/w/alpha")
    expected = os.path.normcase("/w/Alpha") == os.path.normcase("/w/alpha")
    assert candidate_is_leasable(other_case, lanes) is not expected
    # An UNRESOLVED symlink spelling is a different string: the lease cannot
    # resolve it (no FS access under the queue lock), which is exactly why the
    # writers canonicalize before storing.
    via_link = _task("alpha", tid="t4", workspace_root="/link/to/alpha")
    assert candidate_is_leasable(via_link, lanes) is True


def test_lane_key_shape_is_validated_before_the_unscoped_short_circuit():
    """A caller passing bare project ids must be told IMMEDIATELY. Checking the
    shape after the unscoped short-circuit meant the misuse stayed silent for
    every unscoped candidate and surfaced only once a project task happened to
    be considered — by which time two writers could already be running."""
    import pytest

    with pytest.raises(TypeError, match="running_project_lanes"):
        candidate_is_leasable(_task(""), {"alpha"})
    with pytest.raises(TypeError):
        candidate_is_leasable(_task("alpha"), {("alpha", "", "extra")})


def test_project_working_dirs_feeds_the_lane_resolver(tmp_path):
    """The map the supervisor hands the lease comes from the registry, and the
    registry canonicalizes working_dir at WRITE time so the lease's pure
    comparison meets an already-resolved path."""
    from ouroboros.projects_registry import create_project, project_working_dirs

    folder = tmp_path / "alpha"
    folder.mkdir()
    create_project(tmp_path, "alpha", working_dir=str(folder))
    fileless = create_project(tmp_path, "notes")

    folders = project_working_dirs(tmp_path)
    assert folders["alpha"] == str(folder.resolve())
    assert "notes" not in folders            # file-less -> wildcard, not a lane
    assert fileless["working_dir"] == ""

    # A LEGACY row stored an unresolved spelling. Against a task's already
    # realpath'd workspace_root that is a different string — a second concrete
    # lane, exactly the split this map exists to close — so the read
    # canonicalizes too, not only the write.
    from ouroboros.projects_registry import _registry_path
    from ouroboros.utils import atomic_write_json, read_json_dict

    link = tmp_path / "alpha-link"
    link.symlink_to(folder, target_is_directory=True)
    data = read_json_dict(_registry_path(tmp_path))
    for entry in data["projects"]:
        if entry.get("id") == "alpha":
            entry["working_dir"] = str(link)
    atomic_write_json(_registry_path(tmp_path), data)
    assert project_working_dirs(tmp_path)["alpha"] == str(folder.resolve())

    room = _task("alpha", tid="t1", workspace_root=str(folder))
    converted = {"id": "t2", "type": "task", "project_id": "alpha"}
    lanes = running_project_lanes([_meta(room)], folders)
    assert candidate_is_leasable(converted, lanes, folders) is False


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
