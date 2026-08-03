"""Presentation labels never create a second managed-update route."""

from supervisor.update_merge_policy import classify_conflicts, is_document_path, is_hot_code


def test_clean_when_no_conflicts():
    assert classify_conflicts([]) == {
        "kind": "clean",
        "doc_conflict_paths": [],
        "code_conflict_paths": [],
        "hot_code_paths": [],
    }


def test_every_filename_uses_the_same_conflicting_route():
    paths = ["README.md", "BIBLE.md", "docs/CHECKLISTS.md", "prompts/SAFETY.md", "ouroboros/loop.py"]
    result = classify_conflicts(paths)
    assert result["kind"] == "conflicting"
    assert set(result["doc_conflict_paths"] + result["code_conflict_paths"]) == set(paths)
    assert "protected_conflict_paths" not in result
    assert result["hot_code_paths"] == ["ouroboros/loop.py"]


def test_labels_normalize_paths_without_changing_route():
    assert is_document_path("docs\\guide.md")
    assert not is_document_path("docs/notes.txt")
    assert is_hot_code("./supervisor/queue.py")
