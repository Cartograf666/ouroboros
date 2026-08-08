"""SSE /api/tasks/{id}/events follow-phase tests (v6.90.x P2).

The stream's initial replay is a full archive-aware merge; the follow phase
reads only appended bytes per (root, source) log, re-discovers late-spawned
child roots each tick, and heals mid-stream rotation by reading the newest
archive's unconsumed suffix. seq stays monotonic in-stream and the
cross-reconnect cursor contract (ouroboros/cli.py::_watch_task) is preserved.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

from ouroboros.gateway.tasks import api_task_events, iter_task_events
from ouroboros.task_results import write_task_result

OLD_TS = "2026-01-01T00:00:00Z"


def _request(data, task_id, *, cursor=0, wait=8):
    return SimpleNamespace(
        path_params={"task_id": task_id},
        query_params={"cursor": str(cursor), "wait": str(wait)},
        app=SimpleNamespace(state=SimpleNamespace(drive_root=data)),
    )


def _parse_frame(frame):
    if not isinstance(frame, str) or frame.startswith(":"):
        return None
    for line in frame.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])
    return None


async def _consume(response, on_event=None):
    events = []
    async for frame in response.body_iterator:
        event = _parse_frame(frame)
        if event is None:
            continue
        events.append(event)
        if on_event is not None:
            on_event(event, events)
    return events


def _seed_running_task(tmp_path, task_id="t1", progress_rows=2):
    data = tmp_path / "data"
    logs = data / "logs"
    logs.mkdir(parents=True)
    lines = [
        json.dumps({
            "ts": f"2026-01-01T00:01:{i:02d}Z",
            "content": f"step-{i}",
            "task_id": task_id,
        })
        for i in range(progress_rows)
    ]
    (logs / "progress.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_task_result(data, task_id, "running", result="working", ts=OLD_TS)
    (data / "state").mkdir(parents=True, exist_ok=True)
    (data / "state" / "queue_snapshot.json").write_text('{"pending": [], "running": []}', encoding="utf-8")
    return data


def _append_progress(data, task_id, content, ts):
    with (data / "logs" / "progress.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": ts, "content": content, "task_id": task_id}) + "\n")


def _finalize(data, task_id):
    # ts sorts after every progress row used in these tests, so the terminal
    # task_result row is the LAST merged event (a terminal row sorting earlier
    # is also valid — the stream then synthesizes finality for a late cursor).
    write_task_result(data, task_id, "completed", result="done", ts="2026-01-01T00:10:00Z")
    # The follow loop recomputes the terminal projection only when log offsets
    # advanced or the queue snapshot moved — production terminalization always
    # does one of the two; tests bump the snapshot explicitly.
    (data / "state" / "queue_snapshot.json").write_text('{"pending": [], "running": []} ', encoding="utf-8")


def test_sse_incremental_follow_appends_with_monotonic_seq(tmp_path):
    data = _seed_running_task(tmp_path)
    fired = {"appended": False, "finalized": False}

    def on_event(event, events):
        if not fired["appended"] and len(events) >= 3:  # 2 progress + running task_result
            _append_progress(data, "t1", "late-step", "2026-01-01T00:02:00Z")
            fired["appended"] = True
        if fired["appended"] and not fired["finalized"] and any(
            (e.get("data") or {}).get("content") == "late-step" for e in events
        ):
            _finalize(data, "t1")
            fired["finalized"] = True

    response = asyncio.run(api_task_events(_request(data, "t1")))
    events = asyncio.run(_consume(response, on_event))

    contents = [(e.get("data") or {}).get("content") for e in events if e["type"] == "progress"]
    assert contents == ["step-0", "step-1", "late-step"]
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    final = events[-1]
    assert final["type"] == "task_result"
    assert final["data"]["status"] == "completed"


def test_sse_cursor_resume_matches_initial_replay_positions(tmp_path):
    """Reconnecting with cursor=N replays exactly the events after position N —
    the CLI's cross-reconnect contract."""
    data = _seed_running_task(tmp_path, progress_rows=4)
    _finalize(data, "t1")

    first = asyncio.run(_consume(asyncio.run(api_task_events(_request(data, "t1", wait=0)))))
    assert len(first) >= 5  # 4 progress + terminal task_result

    resumed = asyncio.run(
        _consume(asyncio.run(api_task_events(_request(data, "t1", cursor=2, wait=0))))
    )

    assert [e["seq"] for e in resumed] == [e["seq"] for e in first[2:]]
    assert [(e["type"], (e.get("data") or {}).get("content")) for e in resumed] == [
        (e["type"], (e.get("data") or {}).get("content")) for e in first[2:]
    ]


def test_sse_survives_mid_stream_rotation_without_loss_or_duplicates(tmp_path):
    data = _seed_running_task(tmp_path)
    live = data / "logs" / "progress.jsonl"
    fired = {"rotated": False, "finalized": False}

    def on_event(event, events):
        if not fired["rotated"] and len(events) >= 3:
            # An unconsumed row lands just before the rotation…
            _append_progress(data, "t1", "pre-rotation", "2026-01-01T00:02:00Z")
            archive_dir = data / "archive"
            archive_dir.mkdir(exist_ok=True)
            os.replace(live, archive_dir / "progress_20260101T000200.jsonl")
            live.touch()
            # …and a fresh row starts the new live file.
            _append_progress(data, "t1", "post-rotation", "2026-01-01T00:02:01Z")
            fired["rotated"] = True
        if fired["rotated"] and not fired["finalized"] and any(
            (e.get("data") or {}).get("content") == "post-rotation" for e in events
        ):
            _finalize(data, "t1")
            fired["finalized"] = True

    response = asyncio.run(api_task_events(_request(data, "t1")))
    events = asyncio.run(_consume(response, on_event))

    contents = [(e.get("data") or {}).get("content") for e in events if e["type"] == "progress"]
    # The archive suffix (pre-rotation) and the new live row both arrive, exactly
    # once each, and the pre-rotation history is not re-emitted.
    assert contents == ["step-0", "step-1", "pre-rotation", "post-rotation"]
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert events[-1]["data"]["status"] == "completed"


def test_sse_discovers_late_spawned_child_root(tmp_path):
    data = _seed_running_task(tmp_path, task_id="p1", progress_rows=1)
    child_drive = tmp_path / "childdrive"
    fired = {"spawned": False, "finalized": False}

    def on_event(event, events):
        if not fired["spawned"] and len(events) >= 2:
            (child_drive / "logs").mkdir(parents=True, exist_ok=True)
            (child_drive / "logs" / "progress.jsonl").write_text(
                json.dumps({
                    "ts": "2026-01-01T00:03:00Z",
                    "content": "child-step",
                    "task_id": "c1",
                }) + "\n",
                encoding="utf-8",
            )
            write_task_result(
                data, "c1", "running",
                delegation_role="subagent",
                parent_task_id="p1",
                root_task_id="p1",
                child_drive_root=str(child_drive),
                ts=OLD_TS,
            )
            fired["spawned"] = True
        if fired["spawned"] and not fired["finalized"] and any(
            (e.get("data") or {}).get("content") == "child-step" for e in events
        ):
            _finalize(data, "p1")
            fired["finalized"] = True

    response = asyncio.run(api_task_events(_request(data, "p1")))
    events = asyncio.run(_consume(response, on_event))

    child_rows = [e for e in events if (e.get("data") or {}).get("content") == "child-step"]
    assert len(child_rows) == 1  # the late child's log joined at offset 0
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert events[-1]["data"]["status"] == "completed"


def test_iter_task_events_reads_progress_archive_chain(tmp_path):
    """The initial replay is archive-aware: rotated progress rows precede live
    rows in the merged, seq-numbered order."""
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "archive").mkdir()
    (data / "archive" / "progress_20260101T000100.jsonl").write_text(
        json.dumps({"ts": "2026-01-01T00:00:30Z", "content": "archived", "task_id": "t1"}) + "\n",
        encoding="utf-8",
    )
    (data / "logs" / "progress.jsonl").write_text(
        json.dumps({"ts": "2026-01-01T00:01:30Z", "content": "live", "task_id": "t1"}) + "\n",
        encoding="utf-8",
    )
    write_task_result(data, "t1", "completed", result="done", ts="2026-01-01T00:02:00Z")

    events = iter_task_events(data, "t1")

    progress = [e for e in events if e["type"] == "progress"]
    assert [(e["data"]["content"]) for e in progress] == ["archived", "live"]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[-1]["type"] == "task_result"
