"""Phase 4 (v6.39) G: crash-safe atomic full-file overwrite."""

from __future__ import annotations

import pytest

from ouroboros import utils
from ouroboros.utils import atomic_write_json, write_bytes_atomic, write_text, write_text_atomic


def test_write_text_atomic_writes_content(tmp_path):
    target = tmp_path / "f.txt"
    write_text_atomic(target, "hello world")
    assert target.read_text(encoding="utf-8") == "hello world"


@pytest.mark.parametrize("fsync", [False, True])
def test_write_bytes_atomic_writes_exact_bytes(tmp_path, fsync):
    target = tmp_path / "media.bin"
    content = b"\x00\xff\x10media"
    write_bytes_atomic(target, content, fsync=fsync)
    assert target.read_bytes() == content


def test_write_text_atomic_preserves_old_file_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"
    target.write_text("OLD CONTENT", encoding="utf-8")

    def _boom(*a, **k):
        raise OSError("simulated crash during replace")

    # Fail the atomic swap AFTER the temp is written: the EXISTING file must stay fully
    # intact (never a truncated/partial file) and the orphan temp must be cleaned up.
    monkeypatch.setattr(utils.os, "replace", _boom)
    with pytest.raises(OSError):
        write_text_atomic(target, "NEW CONTENT THAT NEVER LANDS")

    assert target.read_text(encoding="utf-8") == "OLD CONTENT"
    assert not list(tmp_path.glob(".f.txt.tmp.*"))  # no orphaned temp left behind


@pytest.mark.skipif(__import__("sys").platform.startswith("win"),
                    reason="POSIX execute bits are not preserved/reported on Windows")
def test_write_text_atomic_preserves_full_mode(tmp_path):
    import os
    target = tmp_path / "script.sh"
    target.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    # setgid + rwxr-x--- exercises the FULL 0o7777 mask (special bits, not just rwx). Use
    # whatever the filesystem actually stored as the baseline so the test is fs-robust.
    os.chmod(target, 0o2750)
    expected = os.stat(target).st_mode & 0o7777
    # os.replace creates a new inode; the existing mode (incl any special bits) must survive.
    write_text_atomic(target, "#!/bin/sh\necho new\n")
    assert target.read_text(encoding="utf-8") == "#!/bin/sh\necho new\n"
    assert (os.stat(target).st_mode & 0o7777) == expected
    assert (os.stat(target).st_mode & 0o111)  # still executable


def test_write_text_helper_is_atomic(tmp_path):
    # utils.write_text (the shared overwrite helper used by git.py et al.) now routes
    # through the atomic primitive.
    target = tmp_path / "g.txt"
    target.write_text("OLD", encoding="utf-8")
    write_text(target, "NEW")
    assert target.read_text(encoding="utf-8") == "NEW"


def test_atomic_write_json_still_works(tmp_path):
    target = tmp_path / "d.json"
    atomic_write_json(target, {"a": 1, "b": [2, 3]})
    import json
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": [2, 3]}


def test_replace_retries_windows_sharing_violation_then_succeeds(tmp_path, monkeypatch):
    """Transient PermissionError (Windows winerror 5/32 sharing violation: a
    reader holds the destination open without FILE_SHARE_DELETE) must be
    absorbed by a bounded retry — the write lands and the file is intact."""
    target = tmp_path / "job.json"
    target.write_text("OLD", encoding="utf-8")
    real_replace = utils.os.replace
    calls = {"n": 0}

    def _flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise PermissionError(13, "The process cannot access the file")
        return real_replace(src, dst)

    monkeypatch.setattr(utils.os, "replace", _flaky_replace)
    monkeypatch.setattr(utils.time, "sleep", lambda _s: None)
    write_text_atomic(target, "NEW")
    assert calls["n"] == 4
    assert target.read_text(encoding="utf-8") == "NEW"
    assert not list(tmp_path.glob(".job.json.tmp.*"))


def test_replace_raises_permission_error_after_bounded_retries(tmp_path, monkeypatch):
    """A persistent PermissionError (a genuinely locked file) must surface
    honestly after the retry bound — never swallowed, never unbounded."""
    target = tmp_path / "job.json"
    target.write_text("OLD", encoding="utf-8")
    calls = {"n": 0}

    def _always_denied(src, dst):
        calls["n"] += 1
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(utils.os, "replace", _always_denied)
    monkeypatch.setattr(utils.time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        write_text_atomic(target, "NEW CONTENT THAT NEVER LANDS")
    assert calls["n"] == utils._REPLACE_RETRY_ATTEMPTS
    assert target.read_text(encoding="utf-8") == "OLD"
    assert not list(tmp_path.glob(".job.json.tmp.*"))


def test_replace_atomic_does_not_retry_other_oserrors(tmp_path, monkeypatch):
    """Only the Windows sharing-violation class retries; any other OSError
    (POSIX-visible failures included) propagates on the first attempt."""
    calls = {"n": 0}

    def _boom(src, dst):
        calls["n"] += 1
        raise OSError("disk detached")

    monkeypatch.setattr(utils.os, "replace", _boom)
    with pytest.raises(OSError):
        utils.replace_atomic(tmp_path / "a", tmp_path / "b")
    assert calls["n"] == 1
