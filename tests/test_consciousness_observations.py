"""Production-shaped durability tests for background observations."""

from __future__ import annotations

import concurrent.futures
import json
import pathlib
import queue
from unittest.mock import MagicMock, patch

import pytest


def _make(tmp_path):
    from ouroboros.consciousness import BackgroundConsciousness

    drive = tmp_path / "drive"
    repo = tmp_path / "repo"
    (drive / "logs").mkdir(parents=True, exist_ok=True)
    repo.mkdir(exist_ok=True)
    with patch.object(BackgroundConsciousness, "_build_registry", return_value=MagicMock()):
        return BackgroundConsciousness(
            drive_root=drive,
            repo_dir=repo,
            event_queue=queue.Queue(),
            owner_chat_id_fn=lambda: None,
        ), drive


def test_observations_are_append_only_and_deduplicated_over_100_rows(tmp_path):
    bc, drive = _make(tmp_path)
    for index in range(125):
        assert bc.inject_observation(
            f"payload-{index}",
            observation_id=f"obs-{index}",
            source="test",
            kind="trace",
            ref={"path": "logs/events.jsonl", "line": index + 1},
        )
    assert not bc.inject_observation("replacement", observation_id="obs-7", source="other")
    pending = bc._snapshot_pending_observations()
    assert len(pending) == 125
    assert pending[7]["payload"] == "payload-7"
    store = drive / "state" / "consciousness_observations.jsonl"
    rows = [json.loads(line) for line in store.read_text().splitlines()]
    assert len(rows) == 125
    assert all(row["id"].startswith("obs-") for row in rows)
    assert all(set(("id", "source", "kind", "time", "payload", "ref")) <= row.keys() for row in rows)


def test_concurrent_enqueue_same_id_has_one_durable_row(tmp_path):
    bc, drive = _make(tmp_path)

    def enqueue(_):
        return bc.inject_observation("same", observation_id="same-id", source="thread")

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(enqueue, range(80)))
    assert sum(results) == 1
    rows = [json.loads(line) for line in (drive / "state" / "consciousness_observations.jsonl").read_text().splitlines()]
    assert [row["id"] for row in rows] == ["same-id"]


def test_context_snapshot_is_bounded_and_status_has_no_payload(tmp_path):
    bc, _ = _make(tmp_path)
    for index in range(15):
        bc.inject_observation("x" * 10_000, observation_id=f"obs-{index}", source="source-a")
    snapshot = bc._snapshot_pending_observations()
    rendered = bc._render_observations(snapshot)
    assert len(rendered) < 20_000
    assert "total=15" in rendered
    assert "read_file(root='runtime_data', path='state/consciousness_observations.jsonl')" in rendered
    assert "omitted=5" in rendered
    status = bc.status_snapshot()
    assert status["pending_observation_count"] == 15
    assert status["oldest_observation_at"]
    assert "payload" not in json.dumps(status)


def test_status_uses_cached_projection_after_initial_rebuild(tmp_path, monkeypatch):
    bc, _ = _make(tmp_path)
    bc.inject_observation("one", observation_id="one")
    assert bc.status_snapshot()["pending_observation_count"] == 1
    calls = {"read": 0}
    original = pathlib.Path.open

    def count_open(path, *args, **kwargs):
        if str(path).endswith("consciousness_observations.jsonl"):
            calls["read"] += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", count_open)
    assert bc.status_snapshot()["pending_observation_count"] == 1
    assert bc.status_snapshot()["pending_observation_count"] == 1
    assert calls["read"] == 0


def test_malformed_store_row_is_disclosed_and_blocks_ack(tmp_path):
    bc, drive = _make(tmp_path)
    bc.inject_observation("known", observation_id="known")
    store = drive / "state" / "consciousness_observations.jsonl"
    with store.open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    restarted, _ = _make(tmp_path)
    status = restarted.status_snapshot()
    assert status["observation_source_complete"] is False
    assert status["observation_gap_count"] == 1
    assert restarted._ack_observations(restarted._snapshot_pending_observations()) is False
    assert [row["id"] for row in restarted._snapshot_pending_observations()] == ["known"]


def test_success_acks_only_cycle_snapshot_and_later_rows_stay_pending(tmp_path, monkeypatch):
    bc, _ = _make(tmp_path)
    bc.inject_observation("first", observation_id="first")
    snapshot = bc._snapshot_pending_observations()
    bc.inject_observation("second", observation_id="second")
    assert bc._ack_observations(snapshot)
    assert [row["id"] for row in bc._snapshot_pending_observations()] == ["second"]


def test_successful_cognition_acks_after_thought_receipt(tmp_path, monkeypatch):
    bc, _ = _make(tmp_path)
    bc.inject_observation("settle me", observation_id="settled")
    monkeypatch.setattr(bc, "_build_context", lambda **_: "context")
    monkeypatch.setattr(bc, "_tool_schemas", lambda: [])
    monkeypatch.setattr(bc, "_check_budget", lambda: True)
    monkeypatch.setattr(
        "ouroboros.llm_observability.chat_observed",
        lambda *args, **kwargs: ({"content": "done"}, {"cost": None}),
    )
    assert bc._think_scoped() is True
    assert bc._snapshot_pending_observations() == []


def test_cancelled_cycle_keeps_snapshot_pending(tmp_path, monkeypatch):
    bc, _ = _make(tmp_path)
    bc.inject_observation("retry me", observation_id="cancelled")
    bc._stop_event.set()
    monkeypatch.setattr(bc, "_build_context", lambda **_: "context")
    monkeypatch.setattr(bc, "_tool_schemas", lambda: [])
    assert bc._think_scoped() is False
    assert [row["id"] for row in bc._snapshot_pending_observations()] == ["cancelled"]


@pytest.mark.parametrize("failure", [RuntimeError("provider"), OverflowError("context")])
def test_failed_cycle_keeps_observations_pending(tmp_path, monkeypatch, failure):
    bc, _ = _make(tmp_path)
    bc.inject_observation("must replay", observation_id="replay")
    if isinstance(failure, OverflowError):
        monkeypatch.setattr(bc, "_build_context", lambda *args, **kwargs: (_ for _ in ()).throw(failure))
    else:
        monkeypatch.setattr(bc, "_build_context", lambda *args, **kwargs: "context")
        monkeypatch.setattr("ouroboros.llm_observability.chat_observed", lambda *args, **kwargs: (_ for _ in ()).throw(failure))
    assert bc._think_scoped() is False
    assert [row["id"] for row in bc._snapshot_pending_observations()] == ["replay"]


def test_restart_replays_unacknowledged_observation_by_id(tmp_path):
    bc, _ = _make(tmp_path)
    bc.inject_observation("survive", observation_id="restart-id")
    restarted, _ = _make(tmp_path)
    assert [row["id"] for row in restarted._snapshot_pending_observations()] == ["restart-id"]
