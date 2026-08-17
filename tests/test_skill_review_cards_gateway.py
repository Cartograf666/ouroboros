"""S4 skill-review cards fix (Q15, design b): reference rows + lazy detail route.

Producer stays untouched: since a776639f every ``skill_review`` chat row already
carries the exact-job reference (skill/job_id/content_hash/...). These tests pin
the two READ-side halves added here:

- ``gateway/history.py`` passes the reference fields through on
  ``system_type == "skill_review"`` rows (legacy rows without them unchanged);
- ``GET /api/skills/{skill}/review-history/{job_id}`` serves the server-rendered
  normalized review block for the exact record, with typed 404s (never a 500)
  for missing skill / missing job / malformed history, and raw reviewer text
  kept private (degraded reviewers disclosed by model + status only).
"""
from __future__ import annotations

import json
import pathlib

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros.utils import append_jsonl


def _detail_client(tmp_path: pathlib.Path) -> tuple[TestClient, pathlib.Path]:
    from ouroboros.gateway.extensions import api_skill_review_history_detail

    app = Starlette(routes=[
        Route(
            "/api/skills/{skill}/review-history/{job_id}",
            endpoint=api_skill_review_history_detail,
            methods=["GET"],
        ),
    ])
    drive_root = tmp_path / "drive"
    drive_root.mkdir(exist_ok=True)
    app.state.drive_root = drive_root
    return TestClient(app), drive_root


def _history_path(drive_root: pathlib.Path, skill: str) -> pathlib.Path:
    path = drive_root / "state" / "skills" / skill / "review_history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _terminal_record(**overrides) -> dict:
    record = {
        "ts": "2026-08-15T00:00:00+00:00",
        "status": "clean",
        "job_status": "succeeded",
        "terminal_reason": "succeeded",
        "content_hash": "abc123def4567890",
        "failure_signature": [],
        "fail_findings": [],
        "job_id": "skill-job-1",
        "group_id": "manual:alpha",
        "review_round": 2,
        "snapshot_attempt": 1,
        "snapshot_revised": False,
        "source": "skills",
        "raw_actor_records": [
            {
                "slot": 0,
                "slot_id": "s0",
                "model_id": "openai/gpt-x",
                "status": "responded",
                "raw_text": "RAW-REVIEWER-TEXT-MUST-STAY-PRIVATE",
                "parsed_items": [
                    {
                        "item": "manifest_schema",
                        "verdict": "PASS",
                        "severity": "advisory",
                        "reason": "manifest ok",
                        "model": "openai/gpt-x",
                    },
                    {
                        "item": "secrets_hygiene",
                        "verdict": "FAIL",
                        "severity": "major",
                        "reason": "token appears in plugin.py",
                        "model": "openai/gpt-x",
                    },
                ],
            },
            {
                "slot": 1,
                "slot_id": "s1",
                "model_id": "anthropic/claude-y",
                "status": "empty_response",
                "raw_text": "DEGRADED-RAW-BODY-MUST-STAY-PRIVATE",
                "parsed_items": [],
            },
        ],
    }
    record.update(overrides)
    return record


def test_review_history_detail_found_record_renders_private_markdown(tmp_path):
    client, drive_root = _detail_client(tmp_path)
    append_jsonl(_history_path(drive_root, "alpha"), _terminal_record())

    resp = client.get("/api/skills/alpha/review-history/skill-job-1")

    assert resp.status_code == 200
    payload = resp.json()
    assert set(payload) == {"markdown", "status", "content_hash", "job_status"}
    assert payload["status"] == "clean"
    assert payload["content_hash"] == "abc123def4567890"
    assert payload["job_status"] == "succeeded"
    markdown = payload["markdown"]
    # Normalized verdict rows from parsed_items are rendered per reviewer.
    assert "Skill review round 2 — snapshot abc123def456 (attempt 1)" in markdown
    assert "`alpha` — status=clean" in markdown
    assert "### Reviewer: openai/gpt-x" in markdown
    assert "[PASS] manifest_schema: manifest ok" in markdown
    assert "[FAIL major] secrets_hygiene: token appears in plugin.py" in markdown
    # Raw reviewer bodies never leave the history file.
    assert "RAW-REVIEWER-TEXT-MUST-STAY-PRIVATE" not in markdown
    assert "DEGRADED-RAW-BODY-MUST-STAY-PRIVATE" not in markdown
    # Degraded reviewers are disclosed honestly by model + status.
    assert "anthropic/claude-y (empty_response)" in markdown
    assert "withheld from chat" in markdown


def test_review_history_detail_missing_skill_is_typed_404(tmp_path):
    client, _drive_root = _detail_client(tmp_path)

    resp = client.get("/api/skills/ghost/review-history/skill-job-1")

    assert resp.status_code == 404
    assert resp.json()["error"] == "no review history for skill"


def test_review_history_detail_missing_job_is_typed_404(tmp_path):
    client, drive_root = _detail_client(tmp_path)
    append_jsonl(_history_path(drive_root, "alpha"), _terminal_record())

    resp = client.get("/api/skills/alpha/review-history/skill-job-unknown")

    assert resp.status_code == 404
    assert resp.json()["error"] == "review record not found"


def test_review_history_detail_malformed_lines_degrade_to_typed_404(tmp_path):
    client, drive_root = _detail_client(tmp_path)
    path = _history_path(drive_root, "alpha")
    path.write_text("{not valid json\n", encoding="utf-8")

    resp = client.get("/api/skills/alpha/review-history/skill-job-1")

    assert resp.status_code == 404
    assert "error" in resp.json()

    # A malformed line does NOT poison neighbouring valid records.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_terminal_record()) + "\n")
    resp2 = client.get("/api/skills/alpha/review-history/skill-job-1")
    assert resp2.status_code == 200
    assert "status=clean" in resp2.json()["markdown"]


def test_review_history_detail_interrupted_record_renders_honest_minimum(tmp_path):
    client, drive_root = _detail_client(tmp_path)
    append_jsonl(
        _history_path(drive_root, "alpha"),
        _terminal_record(
            job_id="skill-job-int",
            status="interrupted",
            job_status="interrupted",
            terminal_reason="owner_process_exited",
            raw_actor_records=[],
        ),
    )

    resp = client.get("/api/skills/alpha/review-history/skill-job-int")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "interrupted"
    assert payload["job_status"] == "interrupted"
    assert "status=interrupted" in payload["markdown"]
    assert "Error: owner_process_exited" in payload["markdown"]
    assert "no parsed findings" in payload["markdown"]


def test_review_history_detail_rejects_non_canonical_skill_names(tmp_path):
    client, _drive_root = _detail_client(tmp_path)

    resp = client.get("/api/skills/../review-history/skill-job-1")

    assert resp.status_code == 404


def test_chat_history_passes_skill_review_reference_fields(tmp_path):
    from ouroboros.gateway.history import _collect_chat_rows

    chat_path = tmp_path / "chat.jsonl"
    archive_dir = tmp_path / "archive"
    append_jsonl(chat_path, {
        "ts": "2026-08-15T00:00:00+00:00",
        "direction": "system",
        "type": "skill_review",
        "chat_id": 1,
        "skill": "alpha",
        "status": "clean",
        "content_hash": "abc123def4567890",
        "job_id": "skill-job-1",
        "review_round": 2,
        "snapshot_attempt": 1,
        "source": "skills",
        "format": "markdown",
        "text": "Skill review round 2 — snapshot abc123def456 (attempt 1): `alpha` — status=clean, source=skills",
    })
    append_jsonl(chat_path, {
        "ts": "2026-08-15T00:00:01+00:00",
        "direction": "system",
        "type": "skill_review",
        "chat_id": 1,
        "format": "markdown",
        "text": "legacy full-text review row without reference fields",
    })
    append_jsonl(chat_path, {
        "ts": "2026-08-15T00:00:02+00:00",
        "direction": "out",
        "chat_id": 1,
        "text": "plain assistant row",
    })

    rows, _quota = _collect_chat_rows(
        chat_path, archive_dir, 10, lambda _chat, _entry: True, {},
    )

    assert len(rows) == 3
    reference, legacy, plain = rows
    assert reference["system_type"] == "skill_review"
    assert reference["skill"] == "alpha"
    assert reference["job_id"] == "skill-job-1"
    assert reference["status"] == "clean"
    assert reference["content_hash"] == "abc123def4567890"
    assert reference["review_round"] == 2
    assert reference["snapshot_attempt"] == 1
    assert reference["source"] == "skills"
    # Legacy skill_review rows expose empty reference fields (frontend keeps
    # exactly today's local expansion for them).
    assert legacy["skill"] == "" and legacy["job_id"] == ""
    # Non-skill-review rows do not grow the new keys at all.
    assert "skill" not in plain and "job_id" not in plain
