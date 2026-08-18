import json

from ouroboros.code_intelligence import (
    build_code_inventory,
    inventory_cache_path,
    load_cached_inventory,
    render_codebase_digest,
)


def test_code_inventory_indexes_python_symbols_imports_and_no_raw_source_cache(tmp_path):
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "pkg" / "main.py").write_text(
        "import pkg.helper\n\n"
        "from .helper import VALUE\n\n"
        "from . import helper\n\n"
        "CONST = 'INVENTORY_RAW_SOURCE_SENTINEL'\n\n"
        "class Worker:\n"
        "    pass\n\n"
        "async def run():\n"
        "    return CONST\n",
        encoding="utf-8",
    )

    inventory = build_code_inventory(repo, drive_root=data, persist=True)
    files = {file.path: file for file in inventory.files}
    main = files["pkg/main.py"]

    assert main.sha256
    assert main.language == "python"
    assert {symbol.name for symbol in main.symbols} >= {"Worker", "run", "CONST"}
    assert "pkg.helper" in main.imports
    assert "pkg/helper.py" in main.resolved_import_paths
    assert any(call.name == "CONST" for call in main.references)

    digest = render_codebase_digest(inventory)
    assert "pkg/main.py" in digest
    assert "Worker" in digest

    cache_files = list((data / "state" / "code_intel").glob("*/inventory.json"))
    assert len(cache_files) == 1
    cached = json.loads(cache_files[0].read_text(encoding="utf-8"))
    rendered_cache = json.dumps(cached)
    assert "INVENTORY_RAW_SOURCE_SENTINEL" not in rendered_cache
    assert "return CONST" not in rendered_cache
    assert cached["schema_version"] == 2


def test_code_inventory_classifies_sensitive_and_symlink_escape(tmp_path):
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    outside = tmp_path / "outside.txt"
    repo.mkdir()
    outside.write_text("external", encoding="utf-8")
    (repo / ".env").write_text("OPENAI_API_KEY=thisisaverylongsecretvalue123456", encoding="utf-8")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "escape").symlink_to(outside)

    inventory = build_code_inventory(repo, drive_root=data, persist=True)
    files = {file.path: file for file in inventory.files}

    assert files[".env"].disposition == "sensitive"
    assert files[".env"].sha256 == ""
    assert files[".env"].size == 0
    assert files["escape"].disposition == "path_escape"

    cache_files = list((data / "state" / "code_intel").glob("*/inventory.json"))
    cached = json.loads(cache_files[0].read_text(encoding="utf-8"))
    rendered_cache = json.dumps(cached)
    assert "thisisaverylongsecretvalue" not in rendered_cache


def test_code_inventory_rebuilds_v1_cache(tmp_path):
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    repo.mkdir()
    (repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    build_code_inventory(repo, drive_root=data, persist=True)
    cache_file = next((data / "state" / "code_intel").glob("*/inventory.json"))
    cached = json.loads(cache_file.read_text(encoding="utf-8"))
    cached["schema_version"] = 1
    for file in cached["files"]:
        file.pop("call_sites", None)
        file.pop("references", None)
    cache_file.write_text(json.dumps(cached), encoding="utf-8")

    rebuilt = build_code_inventory(repo, drive_root=data, persist=True)
    assert rebuilt.schema_version == 2
    rebuilt_cache = json.loads(cache_file.read_text(encoding="utf-8"))
    assert rebuilt_cache["schema_version"] == 2
    app = {file.path: file for file in rebuilt.files}["app.py"]
    assert hasattr(app, "call_sites")


def test_code_inventory_unchanged_rebuild_does_not_rewrite_cache(tmp_path):
    """An unchanged tree must not re-emit the cache.

    Serializing and rewriting the inventory is the expensive half of a warm call
    (measured on the Ouroboros repo: ~1.9 s CPU and a 91 MB write, against ~0.56 s
    for the whole scan-and-load side), and `query_code`, the review-context atlas,
    and deep self-review all reach it per interactive call.
    """
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    repo.mkdir()
    (repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    first = build_code_inventory(repo, drive_root=data, persist=True)
    cache_file = inventory_cache_path(repo, data)
    stamp = cache_file.stat().st_mtime_ns

    second = build_code_inventory(repo, drive_root=data, persist=True)
    assert cache_file.stat().st_mtime_ns == stamp, "unchanged tree rewrote the cache"

    # Skipping the write must not change what the caller receives.
    assert second.files == first.files
    assert second.created_at == first.created_at, "created_at must be carried, not refreshed"
    assert second.git_head == first.git_head

    # A real change still rebuilds and persists.
    (repo / "app.py").write_text("def run():\n    return 1\n\n\ndef added():\n    pass\n", encoding="utf-8")
    third = build_code_inventory(repo, drive_root=data, persist=True)
    assert cache_file.stat().st_mtime_ns != stamp, "changed tree did not rewrite the cache"
    assert {symbol.name for symbol in {f.path: f for f in third.files}["app.py"].symbols} >= {"run", "added"}


def test_code_inventory_quarantines_one_malformed_row(tmp_path):
    """One unreadable row is dropped; the surviving rows stay usable.

    A whole-cache discard here cost a full rebuild for a single bad entry. The scope
    split matches the usage-ledger torn-tail quarantine: a validated prefix survives,
    and the dropped row is re-derived and healed on the next build.
    """
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    repo.mkdir()
    (repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / "other.py").write_text("def other():\n    return 2\n", encoding="utf-8")

    build_code_inventory(repo, drive_root=data, persist=True)
    cache_file = inventory_cache_path(repo, data)
    cached = json.loads(cache_file.read_text(encoding="utf-8"))
    total = len(cached["files"])
    victim = next(row for row in cached["files"] if row["path"] == "app.py")
    victim.pop("token_estimate")  # required key missing: the truncated-row shape
    cache_file.write_text(json.dumps(cached), encoding="utf-8")

    survivors = load_cached_inventory(repo, data)
    assert survivors is not None, "one bad row discarded the whole cache"
    assert len(survivors.files) == total - 1
    assert "app.py" not in {file.path for file in survivors.files}
    assert "other.py" in {file.path for file in survivors.files}

    stamp = cache_file.stat().st_mtime_ns
    healed = build_code_inventory(repo, drive_root=data, persist=True)
    assert {symbol.name for symbol in {f.path: f for f in healed.files}["app.py"].symbols} >= {"run"}
    assert cache_file.stat().st_mtime_ns != stamp, "healed cache was not persisted"
    assert load_cached_inventory(repo, data) is not None
