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


def test_validate_attach_paths_rejects_oversized(tmp_path):
    big = tmp_path / "big.bin"
    with big.open("wb") as fh:
        fh.truncate(cli._ATTACH_MAX_BYTES + 1)
    with pytest.raises(CLIError, match="50 MB"):
        _validate_attach_paths([str(big)])


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


def test_post_multipart_file_encodes_wellformed_body(tmp_path, monkeypatch):
    f = tmp_path / "тест file\".txt"
    f.write_bytes(b"BYTES")
    captured = {}

    class _Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["content_type"] = req.get_header("Content-type")
        captured["body"] = req.data
        return _Resp(json.dumps({"ok": True, "filename": "n", "path": "/p"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = cli.OuroborosHTTPClient("http://127.0.0.1:1")
    result = client.post_multipart_file("/api/chat/upload", f)
    assert result["ok"] is True
    assert captured["url"].endswith("/api/chat/upload")
    assert "multipart/form-data; boundary=" in captured["content_type"]
    boundary = captured["content_type"].split("boundary=", 1)[1]
    body = captured["body"]
    assert body.startswith(f"--{boundary}".encode())
    assert body.rstrip().endswith(f"--{boundary}--".encode())
    assert b"BYTES" in body
    assert b'name="file"' in body
    # The disposition filename is header-safe: no quotes/CR/LF survive.
    disposition = body.split(b"\r\n\r\n", 1)[0]
    assert b'"' not in disposition.split(b'filename="', 1)[1].split(b"\r\n", 1)[0][:-1]


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
