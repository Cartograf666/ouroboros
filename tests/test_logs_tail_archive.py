"""api_logs_tail bounded-tail + archive-backfill tests (v6.90.x P2)."""

from __future__ import annotations

import json

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros.gateway.logs import api_logs_tail


def _app(data):
    app = Starlette(routes=[Route("/api/logs/{name}", endpoint=api_logs_tail, methods=["GET"])])
    app.state.drive_root = data
    return app


def test_logs_tail_backfills_from_rotated_progress_archive(tmp_path):
    data = tmp_path / "data"
    logs = data / "logs"
    logs.mkdir(parents=True)
    archive = data / "archive"
    archive.mkdir()
    (archive / "progress_20260101T000100.jsonl").write_text(
        "\n".join(
            json.dumps({"ts": f"2026-01-01T00:00:{i:02d}Z", "content": f"archived-{i}", "task_id": "t1"})
            for i in range(3)
        ) + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text(
        json.dumps({"ts": "2026-01-01T00:02:00Z", "content": "live-row", "task_id": "t1"}) + "\n",
        encoding="utf-8",
    )

    payload = TestClient(_app(data)).get("/api/logs/progress?limit=10").json()

    contents = [row["content"] for row in payload["entries"]]
    assert contents == ["archived-0", "archived-1", "archived-2", "live-row"]


def test_logs_tail_skips_archives_when_live_satisfies_limit(tmp_path):
    data = tmp_path / "data"
    logs = data / "logs"
    logs.mkdir(parents=True)
    archive = data / "archive"
    archive.mkdir()
    (archive / "progress_20260101T000100.jsonl").write_text(
        json.dumps({"ts": "2026-01-01T00:00:00Z", "content": "archived", "task_id": "t1"}) + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text(
        "\n".join(
            json.dumps({"ts": f"2026-01-01T00:02:{i:02d}Z", "content": f"live-{i}", "task_id": "t1"})
            for i in range(5)
        ) + "\n",
        encoding="utf-8",
    )

    payload = TestClient(_app(data)).get("/api/logs/progress?limit=3").json()

    contents = [row["content"] for row in payload["entries"]]
    assert contents == ["live-2", "live-3", "live-4"]  # newest live tail, no archive read


def test_logs_tail_task_filter_counts_toward_backfill_quota(tmp_path):
    """The archive is consulted when the LIVE file lacks enough rows MATCHING the
    task filter, even if it holds plenty of unrelated rows."""
    data = tmp_path / "data"
    logs = data / "logs"
    logs.mkdir(parents=True)
    archive = data / "archive"
    archive.mkdir()
    (archive / "progress_20260101T000100.jsonl").write_text(
        json.dumps({"ts": "2026-01-01T00:00:00Z", "content": "wanted-archived", "task_id": "target"}) + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text(
        "\n".join(
            json.dumps({"ts": f"2026-01-01T00:02:{i:02d}Z", "content": f"noise-{i}", "task_id": "other"})
            for i in range(5)
        ) + "\n",
        encoding="utf-8",
    )
    (data / "task_results").mkdir()
    (data / "task_results" / "target.json").write_text(
        json.dumps({"task_id": "target", "status": "completed", "result": "done"}),
        encoding="utf-8",
    )
    (data / "state").mkdir()
    (data / "state" / "queue_snapshot.json").write_text('{"pending": [], "running": []}', encoding="utf-8")

    payload = TestClient(_app(data)).get("/api/logs/progress?task_id=target&limit=2").json()

    contents = [row["content"] for row in payload["entries"]]
    assert contents == ["wanted-archived"]
