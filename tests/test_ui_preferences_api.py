from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from starlette.applications import Starlette

from ouroboros.gateway.router import collect_routes


def test_ui_preferences_round_trip_and_normalization(tmp_path):
    from starlette.testclient import TestClient

    from ouroboros.projects_registry import create_project, increment_project_visible_revision

    app = Starlette(routes=collect_routes(data_dir=tmp_path))
    app.state.drive_root = tmp_path
    with TestClient(app) as client:
        initial = client.get("/api/ui/preferences")
        assert initial.status_code == 200
        assert initial.json() == {
            "widget_order": [],
            "nested_subagents_expanded": False,
            "sidebar_width": 0,
            "project_panel_width": 0,
            "project_seen_revision": {},
            "project_order": [],
            "project_thread_order": {},
            "project_last_viewed": {},
            "project_hidden": {},
        }

        create_project(tmp_path, "racer", name="Racer")
        create_project(tmp_path, "site", name="Site")
        increment_project_visible_revision(tmp_path, project_id="racer")
        increment_project_visible_revision(tmp_path, project_id="racer")
        increment_project_visible_revision(tmp_path, project_id="site")

        # Paint ACKs merge monotonically. A future value is clamped to the current
        # visible revision; stale tabs cannot move a cursor backwards. Since T1 the
        # cursor is NESTED per thread; a FLAT number is the one-minor compatibility
        # spelling of thread #0's cursor and reads back nested.
        a = client.post("/api/ui/preferences", json={"project_seen_revision": {"racer": 1}})
        assert a.status_code == 200
        assert a.json()["project_seen_revision"] == {"racer": {"0": 1}}
        b = client.post("/api/ui/preferences", json={"project_seen_revision": {"site": 999}})
        assert b.json()["project_seen_revision"] == {"racer": {"0": 1}, "site": {"0": 1}}
        stale = client.post("/api/ui/preferences", json={"project_seen_revision": {"racer": 0}})
        assert stale.json()["project_seen_revision"]["racer"] == {"0": 1}
        # The nested spelling is the native one and reaches the same lane.
        future = client.post(
            "/api/ui/preferences", json={"project_seen_revision": {"racer": {"0": 999}}}
        )
        assert future.json()["project_seen_revision"]["racer"] == {"0": 2}
        unknown = client.post("/api/ui/preferences", json={"project_seen_revision": {"missing": 8}})
        assert "missing" not in unknown.json()["project_seen_revision"]
        assert client.get("/api/ui/preferences").json()["project_seen_revision"]["racer"] == {"0": 2}

        # Manual drag-and-drop order (D3) rides the same preferences surface as
        # widget_order: dedup + unknown-tolerant, an explicit prefix only.
        order = client.post(
            "/api/ui/preferences",
            json={
                "project_order": ["site", "racer", "site", ""],
                "project_thread_order": {"racer": [3, "1", 3]},
            },
        )
        assert order.status_code == 200
        assert order.json()["project_order"] == ["site", "racer"]
        assert order.json()["project_thread_order"] == {"racer": ["3", "1"]}
        assert client.get("/api/ui/preferences").json()["project_order"] == ["site", "racer"]

        # One-minor aliases remain accepted but are loud no-ops.
        legacy = client.post(
            "/api/ui/preferences",
            json={
                "project_hidden": {"racer": True},
                "project_last_viewed": {"racer": "2026-06-15T01:00:00Z"},
            },
        )
        assert legacy.status_code == 200
        assert legacy.json()["project_hidden"] == {}
        assert legacy.json()["project_last_viewed"] == {}
        assert legacy.json()["warnings"][0]["type"] == "deprecated_ui_preferences_ignored"

        # Resizable side-section widths round-trip and clamp (v6.33.0).
        widths = client.post(
            "/api/ui/preferences",
            json={"sidebar_width": 99999, "project_panel_width": 10},
        )
        assert widths.status_code == 200
        assert widths.json()["sidebar_width"] == 560  # clamped to max
        assert widths.json()["project_panel_width"] == 320  # clamped to min
        zero = client.post("/api/ui/preferences", json={"sidebar_width": 0})
        assert zero.status_code == 200
        assert zero.json()["sidebar_width"] == 0

        response = client.post(
            "/api/ui/preferences",
            json={
                "widget_order": ["skill:two", "skill:one", "skill:two", ""],
                "nested_subagents_expanded": False,
            },
        )
        assert response.status_code == 200
        assert response.json()["widget_order"] == ["skill:two", "skill:one"]
        assert response.json()["nested_subagents_expanded"] is False

        persisted = client.get("/api/ui/preferences")
        assert persisted.status_code == 200
        assert persisted.json()["widget_order"] == ["skill:two", "skill:one"]
        assert persisted.json()["nested_subagents_expanded"] is False

        partial_order = client.post(
            "/api/ui/preferences",
            json={"widget_order": ["skill:three"]},
        )
        assert partial_order.status_code == 200
        assert partial_order.json()["widget_order"] == ["skill:three"]
        assert partial_order.json()["nested_subagents_expanded"] is False

        partial_nested = client.post(
            "/api/ui/preferences",
            json={"nested_subagents_expanded": True},
        )
        assert partial_nested.status_code == 200
        assert partial_nested.json()["widget_order"] == ["skill:three"]
        assert partial_nested.json()["nested_subagents_expanded"] is True

        assert client.post("/api/ui/preferences", json=[]).status_code == 400
        assert client.post("/api/ui/preferences", json={"widget_order": "bad"}).status_code == 400
        assert client.post("/api/ui/preferences", json={"project_seen_revision": {"racer": "bad"}}).status_code == 400
        assert client.post(
            "/api/ui/preferences", json={"project_seen_revision": {"racer": {"0": "bad"}}}
        ).status_code == 400
        assert client.post(
            "/api/ui/preferences", json={"project_seen_revision": {"racer": {"main": 1}}}
        ).status_code == 400
        assert client.post("/api/ui/preferences", json={"project_order": "bad"}).status_code == 400
        assert client.post(
            "/api/ui/preferences", json={"project_thread_order": {"racer": "bad"}}
        ).status_code == 400
        assert client.post("/api/ui/preferences", json={"unknown": True}).status_code == 400


def test_ui_preferences_nested_cursor_is_per_thread_and_migrates_flat(tmp_path):
    """The T1 migration: a stored FLAT cursor reads as thread #0, and a sibling
    thread's ACK can never mark thread #0 read (they clamp on separate ceilings)."""
    from starlette.testclient import TestClient

    from ouroboros.projects_registry import (
        create_project,
        create_thread,
        increment_project_visible_revision,
    )
    from ouroboros.utils import atomic_write_json

    app = Starlette(routes=collect_routes(data_dir=tmp_path))
    app.state.drive_root = tmp_path
    create_project(tmp_path, "twin", name="Twin")
    thread = create_thread(tmp_path, "twin", name="Side")
    for _ in range(3):
        increment_project_visible_revision(tmp_path, project_id="twin")  # thread #0
    increment_project_visible_revision(tmp_path, chat_id=int(thread["chat_id"]))  # the sibling

    # A cursor written BEFORE this release is flat on disk.
    atomic_write_json(
        tmp_path / "state" / "ui_preferences.json",
        {"project_seen_revision": {"twin": 3}},
        trailing_newline=True,
    )
    with TestClient(app) as client:
        stored = client.get("/api/ui/preferences").json()["project_seen_revision"]
        assert stored == {"twin": {"0": 3}}, "flat -> nested normalization maps to thread #0"

        tid = str(thread["id"])
        # The sibling's ceiling is its OWN revision (1), not the project aggregate (4).
        acked = client.post(
            "/api/ui/preferences", json={"project_seen_revision": {"twin": {tid: 999}}}
        ).json()["project_seen_revision"]
        assert acked["twin"] == {"0": 3, tid: 1}

        # Thread #0's cursor is untouched by the sibling and clamps on its own ceiling.
        increment_project_visible_revision(tmp_path, chat_id=int(thread["chat_id"]))
        bumped = client.post(
            "/api/ui/preferences", json={"project_seen_revision": {"twin": {"0": 999}}}
        ).json()["project_seen_revision"]
        assert bumped["twin"]["0"] == 3, "thread #0 clamps on thread0_visible_revision"

        # An unknown thread id is never newly admitted.
        assert "99" not in client.post(
            "/api/ui/preferences", json={"project_seen_revision": {"twin": {"99": 1}}}
        ).json()["project_seen_revision"]["twin"]


def test_ui_preferences_prunes_dead_lanes_and_never_evicts_a_live_one(tmp_path):
    """A merge drops lanes for threads that no longer exist — and ONLY those.

    The regression this pins is silent: bounding a lane by insertion order drops
    whichever key was written FIRST, which on a project with more threads than
    the bound is routinely thread #0 — the project's main chat, re-marked unread
    with nothing on screen to explain it. Existence, not arrival order, decides.
    """
    from starlette.testclient import TestClient

    from ouroboros.projects_registry import (
        create_project,
        create_thread,
        increment_project_visible_revision,
    )
    from ouroboros.utils import atomic_write_json

    app = Starlette(routes=collect_routes(data_dir=tmp_path))
    app.state.drive_root = tmp_path
    create_project(tmp_path, "pruned", name="Pruned")
    thread = create_thread(tmp_path, "pruned", name="Side")
    increment_project_visible_revision(tmp_path, project_id="pruned")          # thread #0
    increment_project_visible_revision(tmp_path, chat_id=int(thread["chat_id"]))
    tid = str(thread["id"])

    # On disk: thread #0's lane (oldest), a live sibling, and two lanes whose
    # threads are gone. A stored cursor is not re-validated on read, so they can
    # only ever be cleaned up here, at merge time.
    atomic_write_json(
        tmp_path / "state" / "ui_preferences.json",
        {"project_seen_revision": {"pruned": {"0": 1, tid: 1, "77": 5, "88": 5}}},
        trailing_newline=True,
    )
    with TestClient(app) as client:
        merged = client.post(
            "/api/ui/preferences", json={"project_seen_revision": {"pruned": {tid: 1}}}
        ).json()["project_seen_revision"]["pruned"]
        assert merged == {"0": 1, tid: 1}, "dead lanes dropped, live lanes kept"
        # Thread #0 survived a merge it was not itself part of.
        assert merged["0"] == 1


def test_ui_preferences_concurrent_paint_acks_are_monotonic(tmp_path):
    from ouroboros.gateway.ui_preferences import api_ui_preferences_post
    from ouroboros.projects_registry import create_project, increment_project_visible_revision

    create_project(tmp_path, "race", name="Race")
    for _ in range(5):
        increment_project_visible_revision(tmp_path, project_id="race")
    barrier = threading.Barrier(2)

    def _post(revision: int) -> int:
        async def _json():
            barrier.wait(timeout=5)
            return {"project_seen_revision": {"race": revision}}

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(drive_root=tmp_path)),
            json=_json,
        )
        return asyncio.run(api_ui_preferences_post(request)).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(_post, (2, 5)))
    assert statuses == [200, 200]
    stored = json.loads((tmp_path / "state" / "ui_preferences.json").read_text(encoding="utf-8"))
    assert stored["project_seen_revision"]["race"] == {"0": 5}
