import json
import os
import re
from types import SimpleNamespace

from ouroboros.task_tree_ledger import tree_ledger_append
from ouroboros.tools.project_journal import (
    _journal_read,
    journal_tail_digest,
)
from ouroboros.tools.recent_tasks import _handle_recent_tasks
from ouroboros.tools.task_tree import _tree_read
from ouroboros.utils import append_jsonl


def _snapshot(text: str) -> str:
    match = re.search(r"snapshot=['\"]?([0-9a-f]{64})", text)
    assert match is not None, text
    return match.group(1)


def _journal_rows(root, project_id: str, count: int) -> None:
    path = root / "projects" / project_id / "journal.jsonl"
    for index in range(count):
        append_jsonl(path, {
            "ts": f"2026-08-21T00:{index // 60:02d}:{index % 60:02d}Z",
            "kind": "note",
            "text": f"journal-{index:03d}",
            "task_id": f"task-{index:03d}",
        })


def test_journal_read_pages_205_rows_and_digest_pointer_is_executable(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    _journal_rows(tmp_path, "mine", 205)
    ctx = SimpleNamespace(
        drive_root=tmp_path / "fork",
        budget_drive_root=str(tmp_path),
        project_id="mine",
        task_metadata={"budget_drive_root": str(tmp_path)},
    )

    first = _journal_read(ctx, "other", limit=200)
    assert "total=205" in first and "remaining=5" in first
    assert "journal-005" in first and "journal-204" in first
    assert "journal-004" not in first
    second = _journal_read(
        ctx, "other", limit=200, offset=200, snapshot=_snapshot(first),
    )
    assert "total=205" in second and "remaining=0" in second
    assert "journal-000" in second and "journal-004" in second
    assert "journal-005" not in second

    digest = journal_tail_digest("mine", limit=40)
    assert "journal_read(project_id='mine', limit=40, offset=40, snapshot=" in digest
    pointed = _journal_read(
        ctx, "", limit=40, offset=40, snapshot=_snapshot(digest),
    )
    assert "journal-164" in pointed and "journal-165" not in pointed


def test_journal_read_refuses_append_between_pages(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    _journal_rows(tmp_path, "mine", 3)
    ctx = SimpleNamespace(drive_root=tmp_path / "fork", project_id="mine")
    first = _journal_read(ctx, limit=2)
    _journal_rows(tmp_path, "mine", 1)

    changed = _journal_read(ctx, limit=2, offset=2, snapshot=_snapshot(first))
    assert changed.startswith("JOURNAL_READ_SNAPSHOT_CHANGED:")
    assert "journal-000" not in changed


def _tree_rows(root, root_id: str, count: int) -> None:
    for index in range(count):
        result = tree_ledger_append(
            root_id,
            "note",
            f"tree-{index:03d}",
            task_id=f"child-{index:03d}",
            data_root=root,
        )
        assert result.startswith("OK:")


def test_tree_read_pages_500_rows_from_fork_and_digest_pointer_is_executable(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    _tree_rows(tmp_path, "root-task", 500)
    assert tree_ledger_append(
        "other-root", "note", "foreign-tree", task_id="foreign", data_root=tmp_path,
    ).startswith("OK:")
    ctx = SimpleNamespace(
        task_id="child-task",
        drive_root=tmp_path / "fork",
        budget_drive_root=str(tmp_path),
        task_metadata={
            "root_task_id": "root-task",
            "budget_drive_root": str(tmp_path),
        },
    )

    pages = []
    offset = 0
    snapshot = ""
    while True:
        page = _tree_read(ctx, limit=200, offset=offset, snapshot=snapshot)
        pages.append(page)
        snapshot = snapshot or _snapshot(page)
        remaining = int(re.search(r"remaining=(\d+)", page).group(1))
        if remaining == 0:
            break
        offset += 200
    joined = "\n".join(pages)
    assert len(pages) == 3
    assert "tree-300" in pages[0] and "tree-499" in pages[0]
    assert "tree-299" not in pages[0]
    assert "foreign-tree" not in joined
    assert all(f"tree-{index:03d}" in joined for index in range(500))

    from ouroboros.task_tree_ledger import tree_ledger_tail_digest

    digest = tree_ledger_tail_digest("root-task", limit=40, data_root=tmp_path)
    assert "tree_read(limit=40, offset=40, snapshot=" in digest
    pointed = _tree_read(ctx, limit=40, offset=40, snapshot=_snapshot(digest))
    assert "tree-420" in pointed and "tree-419" not in pointed


def test_tree_read_refuses_append_between_pages(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    _tree_rows(tmp_path, "root-task", 3)
    ctx = SimpleNamespace(
        task_id="child",
        drive_root=tmp_path / "fork",
        budget_drive_root=str(tmp_path),
        task_metadata={"root_task_id": "root-task"},
    )
    first = _tree_read(ctx, limit=2)
    _tree_rows(tmp_path, "root-task", 1)

    changed = _tree_read(ctx, limit=2, offset=2, snapshot=_snapshot(first))
    assert changed.startswith("TREE_READ_SNAPSHOT_CHANGED:")
    assert "tree-000" not in changed


def _write_task(root, index: int, *, project_id: str = "mine") -> None:
    path = root / "task_results" / f"task-{index:03d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "task_id": f"task-{index:03d}",
        "ts": f"2026-08-21T00:{index // 60:02d}:{index % 60:02d}Z",
        "status": "completed",
        "description": f"task {index}",
        "result": f"result {index}",
        "project_id": project_id,
    }), encoding="utf-8")
    os.utime(path, ns=(index + 1, index + 1))


def test_recent_tasks_pages_364_and_finds_21st_without_known_id(tmp_path):
    canonical = tmp_path / "canonical"
    for index in range(364):
        _write_task(canonical, index)
    ctx = SimpleNamespace(
        drive_root=tmp_path / "fork",
        budget_drive_root=str(canonical),
        project_id="mine",
        task_metadata={"budget_drive_root": str(canonical)},
    )

    first = json.loads(_handle_recent_tasks(ctx, limit=20))
    assert first["total"] == 364
    assert first["remaining"] == 344
    assert first["tasks"][0]["task_id"] == "task-363"
    second = json.loads(_handle_recent_tasks(
        ctx, limit=20, offset=20, snapshot=first["snapshot"],
    ))
    assert second["tasks"][0]["task_id"] == "task-343"
    assert second["remaining"] == 324

    seen = first["tasks"] + second["tasks"]
    assert seen[20]["task_id"] == "task-343"
def test_recent_tasks_refuses_append_between_pages(tmp_path):
    canonical = tmp_path / "canonical"
    for index in range(3):
        _write_task(canonical, index)
    ctx = SimpleNamespace(drive_root=tmp_path / "fork", budget_drive_root=str(canonical))
    first = json.loads(_handle_recent_tasks(ctx, limit=2))
    _write_task(canonical, 3)

    changed = json.loads(_handle_recent_tasks(
        ctx, limit=2, offset=2, snapshot=first["snapshot"],
    ))
    assert changed["error"]["code"] == "RECENT_TASKS_SNAPSHOT_CHANGED"
    assert changed["tasks"] == []
