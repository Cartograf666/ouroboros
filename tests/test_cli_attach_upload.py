"""CLI --attach content-upload flow + typed skipped_missing staging disclosure.

Before remote v1 the CLI passed raw LOCAL paths to /api/tasks and the server
silently dropped any path that did not exist on ITS filesystem — silent data
loss whenever client and server are different machines. Now the CLI uploads
content via POST /api/chat/upload and references the returned server path;
the server manifest discloses missing path-based sources instead of skipping.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import urllib.request

import pytest

from ouroboros import cli
from ouroboros.cli import (
    CLIError,
    _cleanup_uploads,
    _upload_attachments,
    _validate_attach_paths,
)


class FakeClient:
    def __init__(self, *, fail_upload_at: int = -1, fail_task_create: bool = False):
        self.requests = []
        self.uploads = []
        self.fail_upload_at = fail_upload_at
        self.fail_task_create = fail_task_create
        self.base_url = "http://127.0.0.1:9999"

    def post_multipart_file(self, path, file_path, *, field="file", timeout=None):
        index = len(self.uploads)
        self.uploads.append((path, str(file_path)))
        if index == self.fail_upload_at:
            raise CLIError("HTTP 413: too large")
        name = f"srv-{index}_{file_path.name}"
        return {"ok": True, "filename": name, "path": f"/srv/uploads/{name}"}

    def request(self, method, path, body=None, **_kwargs):
        self.requests.append((method, path, body))
        if method == "POST" and path == "/api/tasks":
            if self.fail_task_create:
                raise CLIError("HTTP 400: workspace_root is not a directory")
            return {"task_id": "task-123"}
        if method == "DELETE" and path == "/api/chat/upload":
            return {"ok": True}
        return {}


def test_validate_attach_paths_is_loud_on_missing_file(tmp_path):
    with pytest.raises(CLIError, match="not found"):
        _validate_attach_paths([str(tmp_path / "nope.txt")])
    # A directory is not a regular file either.
    with pytest.raises(CLIError, match="not found"):
        _validate_attach_paths([str(tmp_path)])


def test_validate_attach_paths_rejects_over_limit_batch(tmp_path):
    # P1 no-silent-loss: reject an over-cap batch up front (shared server cap)
    # rather than uploading files the server would silently drop.
    from ouroboros.artifacts import _MAX_STAGED_ATTACHMENTS as cap

    files = []
    for i in range(cap + 1):
        f = tmp_path / f"f{i}.txt"
        f.write_text("x")
        files.append(str(f))
    with pytest.raises(CLIError, match="at most"):
        _validate_attach_paths(files)


def test_validate_attach_paths_rejects_oversized(tmp_path):
    # R9C2: the per-file bound is the SHARED server authority, not a CLI-local
    # literal — sparse-truncate one byte over it.
    from ouroboros.artifacts import _MAX_STAGED_ATTACHMENT_BYTES as per_file

    big = tmp_path / "big.bin"
    with big.open("wb") as fh:
        fh.truncate(per_file + 1)
    with pytest.raises(CLIError, match="per-file"):
        _validate_attach_paths([str(big)])


def test_validate_attach_paths_rejects_over_total_batch(tmp_path):
    # R9C2: individually-valid files whose COMBINED size exceeds the shared
    # per-task total must be rejected up front (no ~1.25 GB upload-then-drop).
    from ouroboros.artifacts import (
        _MAX_STAGED_ATTACHMENT_BYTES as per_file,
        _MAX_STAGED_TOTAL_BYTES as total,
    )

    # Each file is just under the per-file cap; enough of them to cross the total.
    each = per_file - 1
    n = (total // each) + 1
    files = []
    for i in range(int(n)):
        f = tmp_path / f"f{i}.bin"
        with f.open("wb") as fh:
            fh.truncate(each)
        files.append(str(f))
    # Stay within the count cap so it's the TOTAL bound that trips (not count).
    from ouroboros.artifacts import _MAX_STAGED_ATTACHMENTS as cap
    assert len(files) <= cap, "test wants the total bound to trip, not the count cap"
    with pytest.raises(CLIError, match="total size"):
        _validate_attach_paths(files)


def test_multipart_and_request_share_base_headers(monkeypatch):
    # R9C1: post_multipart_file must build its headers from the SAME authority as
    # request() (a single place to add auth later — deferred D9), so the two can
    # never diverge. Capture every urllib Request and assert both carry the full
    # base-header set (title-cased by urllib).
    client = cli.OuroborosHTTPClient("http://127.0.0.1:1")
    captured = []

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def _fake_urlopen(req, timeout=None):
        captured.append(dict(req.headers))
        return _FakeResp()

    monkeypatch.setattr(cli.urllib.request, "urlopen", _fake_urlopen)
    client.request("POST", "/api/x", body={"a": 1})
    client.post_multipart_file("/api/chat/upload", pathlib.Path(__file__))
    assert len(captured) == 2
    expected = {k.title() for k in client._base_headers()}  # {"Accept"} today
    for headers in captured:
        present = {k.title() for k in headers}
        assert expected.issubset(present), (expected, present)


def test_upload_attachments_returns_server_paths(tmp_path):
    files = []
    for name in ("a.txt", "b.txt"):
        f = tmp_path / name
        f.write_text(name)
        files.append(f)
    client = FakeClient()
    attachments, names = _upload_attachments(client, files)
    assert attachments == [
        {"path": "/srv/uploads/srv-0_a.txt", "label": "a.txt"},
        {"path": "/srv/uploads/srv-1_b.txt", "label": "b.txt"},
    ]
    assert names == ["srv-0_a.txt", "srv-1_b.txt"]


def test_upload_attachments_cleans_up_partial_batch(tmp_path):
    files = []
    for name in ("a.txt", "b.txt"):
        f = tmp_path / name
        f.write_text(name)
        files.append(f)
    client = FakeClient(fail_upload_at=1)
    with pytest.raises(CLIError):
        _upload_attachments(client, files)
    deletes = [r for r in client.requests if r[0] == "DELETE"]
    assert deletes == [("DELETE", "/api/chat/upload", {"filename": "srv-0_a.txt"})]


def test_cleanup_uploads_swallows_delete_failures():
    class _Angry(FakeClient):
        def request(self, method, path, body=None, **_kwargs):
            raise CLIError("HTTP 404: File not found")

    _cleanup_uploads(_Angry(), ["x", "y"])  # must not raise


def test_run_command_uploads_and_references_server_paths(tmp_path, monkeypatch, capsys):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF")
    client = FakeClient()
    monkeypatch.setattr(cli, "_client", lambda args, start=False: client)
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--attach", str(f), "--detach", "hello"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.strip() == "task-123"
    method, path, body = next(r for r in client.requests if r[1] == "/api/tasks")
    assert body["attachments"] == [
        {"path": "/srv/uploads/srv-0_doc.pdf", "label": "doc.pdf"}
    ]


def test_run_command_cleans_uploads_when_task_create_fails(tmp_path, monkeypatch):
    f = tmp_path / "doc.txt"
    f.write_text("hi")
    client = FakeClient(fail_task_create=True)
    monkeypatch.setattr(cli, "_client", lambda args, start=False: client)
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--attach", str(f), "--detach", "hello"])
    with pytest.raises(CLIError):
        args.func(args)
    deletes = [r for r in client.requests if r[0] == "DELETE"]
    assert deletes == [("DELETE", "/api/chat/upload", {"filename": "srv-0_doc.txt"})]


def test_run_command_validates_attachments_before_contacting_server(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(cli, "_client", lambda args, start=False: called.append(True))
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--attach", str(tmp_path / "missing.txt"), "hi"])
    with pytest.raises(CLIError, match="not found"):
        args.func(args)
    assert called == []


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _capture_multipart(monkeypatch, path):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["content_type"] = req.get_header("Content-type")
        captured["body"] = req.data
        return _Resp(json.dumps({"ok": True, "filename": "n", "path": "/p"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = cli.OuroborosHTTPClient("http://127.0.0.1:1").post_multipart_file("/api/chat/upload", path)
    return result, captured


def test_post_multipart_file_encodes_wellformed_body(tmp_path, monkeypatch):
    # A space + non-ascii name is valid on every OS (no '"', which is illegal on
    # NTFS); still exercises disposition encoding cross-platform.
    f = tmp_path / "тест file.txt"
    f.write_bytes(b"BYTES")
    result, captured = _capture_multipart(monkeypatch, f)
    assert result["ok"] is True
    assert captured["url"].endswith("/api/chat/upload")
    assert "multipart/form-data; boundary=" in captured["content_type"]
    boundary = captured["content_type"].split("boundary=", 1)[1]
    body = captured["body"]
    assert body.startswith(f"--{boundary}".encode())
    assert body.rstrip().endswith(f"--{boundary}--".encode())
    assert b"BYTES" in body
    assert b'name="file"' in body


@pytest.mark.skipif(os.name == "nt", reason="'\"' is an illegal NTFS filename char")
def test_post_multipart_file_strips_quote_from_disposition(tmp_path, monkeypatch):
    # A '"' in the on-disk name is only creatable on POSIX; verify the encoder
    # strips it from the Content-Disposition filename (header-injection guard).
    f = tmp_path / 'a"b.txt'
    f.write_bytes(b"X")
    _result, captured = _capture_multipart(monkeypatch, f)
    disposition = captured["body"].split(b"\r\n\r\n", 1)[0]
    filename_field = disposition.split(b'filename="', 1)[1].split(b"\r\n", 1)[0][:-1]
    assert b'"' not in filename_field


def test_stage_task_attachments_discloses_missing_sources(tmp_path):
    from ouroboros.artifacts import stage_task_attachments

    real = tmp_path / "real.txt"
    real.write_text("data")
    manifest = stage_task_attachments(
        tmp_path / "drive",
        "task-abc",
        [
            {"path": str(real)},
            {"path": str(tmp_path / "gone-from-this-machine.txt")},
        ],
    )
    statuses = [m.get("status") for m in manifest]
    assert "skipped_missing" in statuses
    staged = [m for m in manifest if m.get("relpath")]
    assert len(staged) == 1 and staged[0]["label"] == "real.txt"
    skipped = next(m for m in manifest if m.get("status") == "skipped_missing")
    assert skipped["label"] == "gone-from-this-machine.txt"
    assert "relpath" not in skipped


def test_stage_task_attachments_discloses_over_limit_omission(tmp_path):
    from ouroboros.artifacts import _MAX_STAGED_ATTACHMENTS, stage_task_attachments

    srcs = []
    for i in range(_MAX_STAGED_ATTACHMENTS + 3):
        f = tmp_path / f"f{i}.txt"
        f.write_text("x")
        srcs.append({"path": str(f)})
    manifest = stage_task_attachments(tmp_path / "drive", "task-lim", srcs)
    over = [m for m in manifest if m.get("status") == "skipped_over_limit"]
    assert len(over) == 1  # exactly one typed omission entry, not a silent break
    assert over[0]["limit"] == _MAX_STAGED_ATTACHMENTS
    staged = [m for m in manifest if m.get("relpath")]
    assert len(staged) == _MAX_STAGED_ATTACHMENTS


def test_render_attachment_lines_discloses_over_limit(tmp_path):
    from ouroboros.gateway.tasks import _render_attachment_lines

    rendered = _render_attachment_lines(
        [{"label": "5 more attachment(s)", "status": "skipped_over_limit", "limit": 25}]
    )
    assert "NOT STAGED" in rendered and "limit" in rendered and "read_file" not in rendered


def test_stage_task_attachments_discloses_over_total_omission(tmp_path):
    # R9C2: files that individually pass the per-file cap but together exceed the
    # per-task TOTAL must stop with one typed omission entry (no silent break).
    from ouroboros.artifacts import (
        _MAX_STAGED_ATTACHMENT_BYTES,
        _MAX_STAGED_TOTAL_BYTES,
        stage_task_attachments,
    )

    each = _MAX_STAGED_ATTACHMENT_BYTES  # exactly the per-file cap (allowed)
    n = (_MAX_STAGED_TOTAL_BYTES // each) + 2
    srcs = []
    for i in range(int(n)):
        f = tmp_path / f"big{i}.bin"
        with f.open("wb") as fh:
            fh.truncate(each)
        srcs.append({"path": str(f)})
    manifest = stage_task_attachments(tmp_path / "drive", "task-total", srcs)
    over = [m for m in manifest if m.get("status") == "skipped_over_total"]
    assert len(over) == 1
    assert over[0]["limit_bytes"] == _MAX_STAGED_TOTAL_BYTES
    staged = [m for m in manifest if m.get("relpath")]
    # Total bound stops staging before the byte ceiling is crossed.
    assert sum(pathlib.Path(m["abs_path"]).stat().st_size for m in staged) <= _MAX_STAGED_TOTAL_BYTES


def test_render_attachment_lines_discloses_over_total(tmp_path):
    from ouroboros.gateway.tasks import _render_attachment_lines

    rendered = _render_attachment_lines(
        [{"label": "3 more attachment(s)", "status": "skipped_over_total",
          "limit_bytes": 200 * 1024 * 1024}]
    )
    assert "NOT STAGED" in rendered and "MB" in rendered and "read_file" not in rendered


def test_render_attachment_lines_discloses_skipped_without_fake_read(tmp_path):
    from ouroboros.gateway.tasks import _render_attachment_lines

    rendered = _render_attachment_lines(
        [
            {"label": "ok.txt", "root": "artifact_store", "relpath": "attachments/ok.txt"},
            {"label": "lost.txt", "status": "skipped_missing"},
        ]
    )
    lines = rendered.splitlines()
    assert any("read_file" in line and "ok.txt" in line for line in lines)
    lost_line = next(line for line in lines if "lost.txt" in line)
    assert "NOT STAGED" in lost_line
    assert "read_file" not in lost_line
