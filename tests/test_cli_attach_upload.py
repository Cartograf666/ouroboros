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

    def post_multipart_file(self, path, file_path, *, field="file", timeout=None, max_bytes=None, revalidate_secret=False):
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


def test_post_multipart_file_bounds_read_against_grown_file(tmp_path):
    # R31C1: a file larger than max_bytes AT READ TIME (grown/swapped after
    # validation) must be refused, not read unbounded into memory.
    big = tmp_path / "grew.bin"
    with big.open("wb") as fh:
        fh.truncate(2048)
    client = cli.OuroborosHTTPClient("http://127.0.0.1:1")
    with pytest.raises(CLIError, match="upload budget at read time"):
        client.post_multipart_file("/api/chat/upload", big, max_bytes=1024)


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


def test_upload_attachments_cleans_up_on_non_clierror(tmp_path):
    # R23C2: a NON-CLIError mid-batch (e.g. the 2nd file's read raising OSError,
    # or a non-dict HTTP-200 making .get() raise) must still release earlier
    # uploads — not just a CLIError. Model it via a client whose 2nd upload
    # raises OSError; the 1st upload must be deleted and the error normalized.
    files = []
    for name in ("a.txt", "b.txt"):
        f = tmp_path / name
        f.write_text(name)
        files.append(f)

    class _RaceClient(FakeClient):
        def post_multipart_file(self, path, file_path, *, field="file", timeout=None, max_bytes=None, revalidate_secret=False):
            if len(self.uploads) == 1:  # 2nd file
                raise OSError("file vanished after validation")
            return super().post_multipart_file(path, file_path)

    client = _RaceClient()
    with pytest.raises(CLIError):  # OSError normalized to CLIError
        _upload_attachments(client, files)
    deletes = [r for r in client.requests if r[0] == "DELETE"]
    assert deletes == [("DELETE", "/api/chat/upload", {"filename": "srv-0_a.txt"})]


def test_upload_attachments_cleans_up_on_non_dict_response(tmp_path):
    # R23C2: a non-dict HTTP-200 body (post_multipart_file returns a str) must not
    # raise AttributeError uncaught — it releases prior uploads and normalizes.
    files = []
    for name in ("a.txt", "b.txt"):
        f = tmp_path / name
        f.write_text(name)
        files.append(f)

    class _BadBodyClient(FakeClient):
        def post_multipart_file(self, path, file_path, *, field="file", timeout=None, max_bytes=None, revalidate_secret=False):
            if len(self.uploads) == 1:  # 2nd file: server returns a bare string
                self.uploads.append((path, str(file_path)))
                return "OK (not json)"
            return super().post_multipart_file(path, file_path)

    client = _BadBodyClient()
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


def test_validate_attach_paths_drops_secret_sources_with_zero_uploads(tmp_path):
    # R32C1: a secret SOURCE (~/.ssh/id_rsa, credentials.json, *.pem, a secret-
    # DIRECTORY component) must be refused BEFORE any upload. Staging drops it,
    # but for a REMOTE server the CLI would otherwise already have transmitted
    # and persisted the secret bytes on another machine. Zero multipart requests.
    normal = tmp_path / "doc.txt"
    normal.write_text("data")
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    key = ssh_dir / "id_rsa"  # secret DIRECTORY component (.ssh) + secret name
    key.write_text("PRIVATE")
    creds = tmp_path / "credentials.json"  # secret NAME
    creds.write_text("{}")
    pem = tmp_path / "server.pem"  # secret EXTENSION
    pem.write_text("cert")

    kept = _validate_attach_paths([str(key), str(normal), str(creds), str(pem)])
    assert [p.name for p in kept] == ["doc.txt"]

    client = FakeClient()
    attachments, names = _upload_attachments(client, kept)
    assert client.uploads and all("doc.txt" in u[1] for u in client.uploads)
    assert len(client.uploads) == 1  # exactly the non-secret file — ZERO secret uploads


def test_validate_attach_paths_drops_missing_secret_without_confirming_existence(tmp_path):
    # A secret-SHAPED path that does not exist must be dropped SILENTLY — never a
    # loud "not found", which would confirm the secret file's (non-)existence and
    # leak its name. The secret check runs before the is_file() branch.
    kept = _validate_attach_paths([str(tmp_path / ".ssh" / "id_ed25519")])
    assert kept == []


def test_upload_attachments_enforces_aggregate_budget_at_read_time(tmp_path, monkeypatch):
    # R32C3: validation checks the 200 MB aggregate from pre-upload stat values,
    # but files can GROW (each staying under the per-file cap) between validation
    # and the read. The read-time budget must still bound the TOTAL transferred —
    # not just each file. Model it with tiny caps and files that grew after
    # validation; the batch must abort with a budget error and clean up.
    import ouroboros.artifacts as art

    monkeypatch.setattr(art, "_MAX_STAGED_ATTACHMENT_BYTES", 100, raising=True)
    monkeypatch.setattr(art, "_MAX_STAGED_TOTAL_BYTES", 150, raising=True)

    f0 = tmp_path / "f0.bin"
    f1 = tmp_path / "f1.bin"
    f0.write_bytes(b"a" * 10)
    f1.write_bytes(b"b" * 10)
    validated = _validate_attach_paths([str(f0), str(f1)])  # 20 B total, passes
    # Both grow to 80 B: each still < 100 B per-file, but 160 B > 150 B aggregate.
    f0.write_bytes(b"a" * 80)
    f1.write_bytes(b"b" * 80)

    uploaded = []

    class _RealBytesClient(FakeClient):
        # Exercise the REAL post_multipart_file read path so the budget is
        # enforced against actual bytes, not the fake's canned response.
        def post_multipart_file(self, path, file_path, *, field="file", timeout=None, max_bytes=None, revalidate_secret=False):
            content = pathlib.Path(file_path).read_bytes()
            if max_bytes is not None and len(content) > max_bytes:
                from ouroboros.cli import CLIError as _E
                self._last_upload_bytes = 0
                raise _E("exceeds the remaining upload budget at read time")
            self._last_upload_bytes = len(content)
            index = len(self.uploads)
            self.uploads.append((path, str(file_path)))
            uploaded.append(file_path.name)
            name = f"srv-{index}_{file_path.name}"
            return {"ok": True, "filename": name, "path": f"/srv/uploads/{name}"}

    client = _RealBytesClient()
    with pytest.raises(CLIError, match="budget"):
        _upload_attachments(client, validated)
    # f0 (80 B) fit; f1 (80 B) crossed the 150 B budget → aborted + f0 released.
    assert uploaded == ["f0.bin"]
    deletes = [r for r in client.requests if r[0] == "DELETE"]
    assert deletes == [("DELETE", "/api/chat/upload", {"filename": "srv-0_f0.bin"})]


def test_post_multipart_file_records_true_bytes_sent(tmp_path, monkeypatch):
    # R32C3: the running aggregate budget decrements by _last_upload_bytes, so the
    # real encoder must record the TRUE number of content bytes it transmitted.
    f = tmp_path / "payload.bin"
    f.write_bytes(b"z" * 37)
    client = cli.OuroborosHTTPClient("http://127.0.0.1:1")

    def fake_urlopen(req, timeout=None):
        return _Resp(json.dumps({"ok": True, "filename": "n", "path": "/p"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client.post_multipart_file("/api/chat/upload", f, max_bytes=1000)
    assert client._last_upload_bytes == 37


@pytest.mark.skipif(os.name == "nt", reason="symlink swap needs POSIX symlinks")
def test_upload_refuses_path_swapped_to_secret_before_open(tmp_path, monkeypatch):
    # R33C2: _validate_attach_paths cleared a NORMAL path, but the upload reopens
    # the original path. A symlink swapped in between (now pointing at a secret)
    # must be refused at open time — ZERO network requests, no credential bytes
    # ever leave the machine for the remote server.
    normal = tmp_path / "doc.txt"
    normal.write_text("data")
    validated = _validate_attach_paths([str(normal)])  # passes: not secret
    assert [p.name for p in validated] == ["doc.txt"]
    # Swap the validated path to a symlink at a secret-shaped target.
    normal.unlink()
    secret_target = tmp_path / "credentials.json"
    secret_target.write_text("SECRET")
    normal.symlink_to(secret_target)

    net = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: net.append(1))
    client = cli.OuroborosHTTPClient("http://127.0.0.1:1")
    with pytest.raises(CLIError, match="secret-shaped"):
        _upload_attachments(client, validated)
    assert net == []  # ZERO multipart requests — the secret never left the machine


@pytest.mark.skipif(os.name == "nt", reason="symlink identity test needs POSIX symlinks")
def test_reject_swapped_source_detects_inode_mismatch(tmp_path):
    # R33C2: the opened handle is bound to the checked identity by (dev, ino). If
    # the re-resolved path names a DIFFERENT file than the open handle (a swap in
    # the open->check window), refuse even when the new target is not itself secret.
    a = tmp_path / "a.txt"
    a.write_text("A")
    b = tmp_path / "b.txt"  # non-secret, but a DIFFERENT inode than the handle
    b.write_text("B")
    link = tmp_path / "link"
    link.symlink_to(b)
    client = cli.OuroborosHTTPClient("http://127.0.0.1:1")
    fh = a.open("rb")  # handle bound to a.txt's inode
    try:
        with pytest.raises(CLIError, match="changed identity"):
            client._reject_swapped_or_secret_source(link, fh)  # re-resolves to b.txt
    finally:
        fh.close()


def test_upload_normal_file_passes_revalidation(tmp_path, monkeypatch):
    # R33C2 (happy path): an ordinary, unchanged attachment must pass the open-time
    # secret + inode revalidation and upload normally — the guard must not break
    # the common case.
    f = tmp_path / "doc.txt"
    f.write_bytes(b"hello")
    validated = _validate_attach_paths([str(f)])

    def fake_urlopen(req, timeout=None):
        return _Resp(json.dumps({"ok": True, "filename": "n", "path": "/p"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = cli.OuroborosHTTPClient("http://127.0.0.1:1")
    attachments, names = _upload_attachments(client, validated)
    assert attachments == [{"path": "/p", "label": "doc.txt"}]
    assert names == ["n"]


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


def test_missing_secret_shaped_source_disclosed_generically(tmp_path):
    # R16C1: a MISSING secret-shaped path (e.g. ~/.ssh/id_rsa, credentials.json)
    # must disclose generically — echoing the basename would leak the secret
    # filename into the prompt, breaking the secret-silence contract.
    from ouroboros.artifacts import stage_task_attachments
    from ouroboros.gateway.tasks import _render_attachment_lines

    for secret_path in (str(tmp_path / ".ssh" / "id_rsa"), str(tmp_path / "credentials.json")):
        manifest = stage_task_attachments(
            tmp_path / "drive", "task-secret-missing", [{"path": secret_path}]
        )
        assert [m.get("status") for m in manifest] == ["skipped_missing"]
        label = manifest[0]["label"]
        assert "id_rsa" not in label and "credentials" not in label
        assert label == "a withheld attachment"
        rendered = _render_attachment_lines(manifest)
        assert "id_rsa" not in rendered and "credentials" not in rendered
    # A missing NON-secret path still discloses its name (unchanged).
    m2 = stage_task_attachments(tmp_path / "drive2", "t", [{"path": str(tmp_path / "report.pdf")}])
    assert m2[0]["label"] == "report.pdf"


def test_stage_task_attachments_skips_malformed_entries(tmp_path):
    # R15C2: a malformed attachment entry (string/None) must be skipped, not
    # abort the whole batch — a valid entry after it still stages.
    from ouroboros.artifacts import stage_task_attachments

    ok = tmp_path / "ok.txt"
    ok.write_text("hi")
    manifest = stage_task_attachments(
        tmp_path / "drive", "task-malformed",
        ["not-a-dict", None, 42, {"path": str(ok)}],
    )
    staged = [m for m in manifest if m.get("relpath")]
    assert len(staged) == 1 and staged[0]["label"] == "ok.txt"


def test_stage_task_attachments_bounds_manifest_for_all_missing(tmp_path):
    # R28C1: a POST with an arbitrarily long list of NONEXISTENT paths must not
    # produce an unbounded manifest (each would be a skipped_missing row expanded
    # into the prompt). The cap is on items PROCESSED, so the manifest is bounded
    # at ~_MAX_STAGED_ATTACHMENTS rows + one summarized remainder.
    from ouroboros.artifacts import _MAX_STAGED_ATTACHMENTS, stage_task_attachments

    srcs = [{"path": str(tmp_path / f"nope{i}.txt")} for i in range(_MAX_STAGED_ATTACHMENTS + 500)]
    manifest = stage_task_attachments(tmp_path / "drive", "task-flood", srcs)
    assert len(manifest) <= _MAX_STAGED_ATTACHMENTS + 1  # bounded, not 525 rows
    over = [m for m in manifest if m.get("status") == "skipped_over_limit"]
    assert len(over) == 1  # the summarized remainder
    missing = [m for m in manifest if m.get("status") == "skipped_missing"]
    assert len(missing) == _MAX_STAGED_ATTACHMENTS  # capped, each disclosed once


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
