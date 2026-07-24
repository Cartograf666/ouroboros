"""Command-line entrypoint for source and packaged runs."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, Iterator, List, Optional


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class CLIError(RuntimeError):
    pass


class PatchCLIError(CLIError):
    pass


class TaskTimeoutCLIError(CLIError):
    pass


class ConnectionCLIError(CLIError):
    pass


class OuroborosHTTPClient:
    def __init__(self, base_url: str = "", timeout: float = 30.0):
        self.base_url = (base_url or _default_base_url()).rstrip("/")
        self.timeout = timeout

    def _base_headers(self) -> Dict[str, str]:
        """Shared header authority for EVERY request this client makes.

        request() and post_multipart_file() both start from here so they can
        never diverge on transport-level headers — a single place to add auth
        (e.g. an OUROBOROS_NETWORK_PASSWORD header) if the CLI ever gains
        non-loopback support (deferred, plan D9: v1 is ssh-tunnel-only, where
        loopback needs no password). Today: Accept only.
        """
        return {"Accept": "application/json"}

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        data = None
        headers = self._base_headers()
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout if timeout is None else timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
                message = payload.get("error") or raw
            except Exception:
                message = raw or str(exc)
            raise CLIError(f"HTTP {exc.code}: {message}") from exc
        except urllib.error.URLError as exc:
            raise ConnectionCLIError(f"cannot reach Ouroboros server at {self.base_url}: {exc}") from exc
        except TimeoutError as exc:
            raise ConnectionCLIError(f"request to Ouroboros server timed out at {self.base_url}") from exc
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    @staticmethod
    def _reject_swapped_or_secret_source(source: "pathlib.Path", fh: Any) -> None:
        """Bind an already-opened attachment handle to a non-secret identity.

        TOCTOU hardening (R33C2): _validate_attach_paths applied the secret-source
        policy to the resolved path, but this method later REOPENS the original
        path — a symlink or path swapped in between could feed credential bytes to
        a remote server despite that pre-check. So, on the SAME handle we read
        from: (1) re-resolve and re-apply the secret policy (catches a swap BEFORE
        open), and (2) verify the opened fd's (st_dev, st_ino) still matches the
        re-resolved path (catches a swap in the open→check window, where the
        handle holds the old inode while the path now names a different file).
        """
        import os as _os

        fd_stat = _os.fstat(fh.fileno())
        real = pathlib.Path(_os.path.realpath(source))
        from ouroboros.artifacts import is_secret_attachment_source

        if is_secret_attachment_source(real) or is_secret_attachment_source(source):
            raise CLIError(
                f"refusing to upload a secret-shaped attachment source: {source.name}"
            )
        try:
            real_stat = real.stat()
        except OSError as exc:
            raise CLIError(
                f"attachment source vanished before upload: {source.name}"
            ) from exc
        if (fd_stat.st_dev, fd_stat.st_ino) != (real_stat.st_dev, real_stat.st_ino):
            raise CLIError(
                f"attachment source changed identity between validation and "
                f"upload: {source.name}"
            )

    def post_multipart_file(
        self,
        path: str,
        file_path: "pathlib.Path",
        *,
        field: str = "file",
        timeout: Optional[float] = None,
        max_bytes: Optional[int] = None,
        revalidate_secret: bool = False,
    ) -> Any:
        """POST one file as multipart/form-data (stdlib-only encoder).

        The file is read fully into memory (desktop CLI client, not a streaming
        service). ``max_bytes`` re-enforces a byte cap AT READ TIME (R31C1/R32C3):
        _validate_attach_paths checks sizes earlier, but a file that grows or is
        swapped between validation and upload would otherwise be read unbounded
        and bypass the validated cap — so read at most limit+1 and refuse if it
        grew. The caller passes ``min(per-file cap, remaining aggregate budget)``
        so a batch of individually-valid-but-grown files can't exceed the task's
        total-bytes limit either. The actual bytes read are recorded on
        ``self._last_upload_bytes`` so the caller can decrement its running
        aggregate budget by the TRUE transfer size (not the pre-stat estimate).
        """
        import mimetypes
        import uuid

        source = pathlib.Path(file_path)
        boundary = uuid.uuid4().hex
        # Header-safe filename: strip CR/LF and quotes from the disposition.
        safe_name = source.name.replace("\r", "").replace("\n", "").replace('"', "'")
        mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{safe_name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        if max_bytes is not None and max_bytes >= 0:
            with source.open("rb") as _fh:
                # Re-check secret policy + inode identity on the SAME handle we
                # read from (R33C2), so a post-validation path/symlink swap can't
                # transmit credential bytes to the remote server.
                if revalidate_secret:
                    self._reject_swapped_or_secret_source(source, _fh)
                content = _fh.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise CLIError(
                    f"attachment {source.name} exceeds the remaining "
                    f"{max_bytes // (1024 * 1024)} MB upload budget at read time "
                    "(file grew after validation, or the task's total attachment "
                    "size is exhausted)"
                )
        else:
            content = source.read_bytes()
        # TRUE transferred size, for the caller's running aggregate budget (R32C3).
        self._last_upload_bytes = len(content)
        body = head + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
        headers = self._base_headers()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        req = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                req, timeout=self.timeout if timeout is None else timeout
            ) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
                message = payload.get("error") or raw
            except Exception:
                message = raw or str(exc)
            raise CLIError(f"HTTP {exc.code}: {message}") from exc
        except urllib.error.URLError as exc:
            raise ConnectionCLIError(f"cannot reach Ouroboros server at {self.base_url}: {exc}") from exc
        except TimeoutError as exc:
            raise ConnectionCLIError(f"upload to Ouroboros server timed out at {self.base_url}") from exc
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return raw

    def get_bytes(self, path: str) -> bytes:
        req = urllib.request.Request(self.base_url + path, headers={"Accept": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
                message = payload.get("error") or raw
            except Exception:
                message = raw or str(exc)
            raise CLIError(f"HTTP {exc.code}: {message}") from exc
        except urllib.error.URLError as exc:
            raise ConnectionCLIError(f"cannot download from Ouroboros server at {self.base_url}: {exc}") from exc

    def stream_sse(self, path: str, timeout: float = 120.0) -> Iterator[Dict[str, Any]]:
        req = urllib.request.Request(self.base_url + path, headers={"Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                yield from _parse_sse_lines(resp)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise CLIError(f"HTTP {exc.code}: {raw}") from exc
        except urllib.error.URLError as exc:
            raise ConnectionCLIError(f"cannot stream from Ouroboros server at {self.base_url}: {exc}") from exc


def _server_command(args: argparse.Namespace) -> int:
    old_argv = sys.argv[:]
    try:
        import json
        import __main__

        # Preserve module-mode launches. When started via `python -m
        # ouroboros.cli server`, sys.argv[0] is the cli.py path (ends with
        # .py), so the old check re-exec'd a BARE SCRIPT (`python cli.py
        # server`) — which puts ouroboros/ (not the repo root) on sys.path[0]
        # and breaks every `from ouroboros...` import, so the self-restart
        # fails. __main__.__spec__.name carries the real `-m` module name.
        spec_name = getattr(getattr(__main__, "__spec__", None), "name", "") or ""
        if spec_name.startswith("ouroboros"):
            reexec_argv = ["-m", spec_name, *old_argv[1:]]
        elif str(old_argv[0]).endswith(".py"):
            reexec_argv = old_argv
        else:
            reexec_argv = ["-m", "ouroboros.cli", *old_argv[1:]]
        os.environ["OUROBOROS_SERVER_REEXEC_ARGV_JSON"] = json.dumps(reexec_argv)
    except Exception:
        pass
    if args.host:
        os.environ["OUROBOROS_SERVER_HOST"] = args.host
    if args.port:
        os.environ["OUROBOROS_SERVER_PORT"] = str(args.port)
    # The one-server-per-data-dir lock is acquired inside server.main() (the
    # universal startup boundary), so every entry path — not just this CLI
    # wrapper — enforces it and a self-reexec reacquires it there.
    import server

    try:
        sys.argv = [old_argv[0]]
        return int(server.main())
    finally:
        sys.argv = old_argv


def _status_command(args: argparse.Namespace) -> int:
    client = _client(args)
    health = client.request("GET", "/api/health")
    state = client.request("GET", "/api/state")
    if args.json:
        _print_json({"health": health, "state": state})
        return 0
    print(f"Ouroboros {health.get('version', '?')} at {client.base_url}")
    print(f"branch={state.get('branch', '?')} sha={state.get('sha', '?')} workers={state.get('workers_alive', 0)}/{state.get('workers_total', 0)}")
    print(f"pending={state.get('pending_count', 0)} running={state.get('running_count', 0)} runtime_mode={state.get('runtime_mode', '?')}")
    return 0


# CLI attachments are uploaded as CONTENT (POST /api/chat/upload) and the task
# references the returned server-side path. Passing the raw client path used to
# lose files silently whenever the CLI and the server are different machines.
# Attachment limits are NOT redefined here — the single authority is the server
# staging layer (ouroboros/artifacts.py); the CLI imports it so the up-front
# check and the server-side stage can never diverge.


def _validate_attach_paths(raw_paths: List[str]) -> List[pathlib.Path]:
    """Loudly validate every --attach path BEFORE contacting the server.

    Enforces the SAME per-task cap the server stages against, so an over-limit
    batch is rejected up front rather than uploading files the server would drop
    (P1 no-silent-loss: no orphaned uploads, no undisclosed incomplete input).
    """
    from ouroboros.artifacts import (
        _MAX_STAGED_ATTACHMENTS as _CAP,
        _MAX_STAGED_ATTACHMENT_BYTES as _PER_FILE,
        _MAX_STAGED_TOTAL_BYTES as _TOTAL,
        is_secret_attachment_source,
    )

    raw_paths = list(raw_paths or [])
    if len(raw_paths) > _CAP:
        raise CLIError(
            f"--attach accepts at most {_CAP} files per task; got {len(raw_paths)}. "
            "Bundle the rest (e.g. a zip) or split the task."
        )
    paths: List[pathlib.Path] = []
    total = 0
    for raw in raw_paths:
        path = pathlib.Path(raw).expanduser()
        # Secret-source refusal BEFORE any network contact (R32C1): staging drops
        # a credential source (~/.ssh/id_rsa, *.pem, credentials.json), but for a
        # REMOTE server the CLI would already have uploaded the bytes — they'd
        # land and persist on another machine. So refuse using the server's SAME
        # predicate. R34C1 (P1 no-silent-loss): fail LOUDLY, not silently — a
        # silent drop narrows an owner-requested task without disclosure (e.g. a
        # benign file with a secret-shaped name). The message is GENERIC (never
        # the filename) so confidentiality holds and existence is not confirmed,
        # yet the owner sees the omission before any task is created. Zero uploads.
        if is_secret_attachment_source(path.resolve()) or is_secret_attachment_source(path):
            raise CLIError(
                "one or more --attach files were withheld by secret-source policy "
                "(a path resolving under a credential location or with a "
                "credential-shaped name); remove them from --attach to proceed."
            )
        if not path.is_file():
            raise CLIError(f"--attach file not found (or not a regular file): {path}")
        size = path.stat().st_size
        if size > _PER_FILE:
            raise CLIError(
                f"--attach file exceeds the {_PER_FILE // (1024 * 1024)} MB per-file "
                f"upload limit: {path}"
            )
        total += size
        # Aggregate bound: reject the whole batch BEFORE uploading anything, so a
        # set of individually-valid files can't stream ~1.25 GB only to be
        # partly dropped at staging (same authority the server enforces).
        if total > _TOTAL:
            raise CLIError(
                f"--attach total size exceeds the {_TOTAL // (1024 * 1024)} MB "
                "per-task limit; bundle or split the inputs."
            )
        paths.append(path)
    return paths


def _upload_attachments(
    client: "OuroborosHTTPClient", paths: List[pathlib.Path]
) -> "tuple[List[Dict[str, Any]], List[str]]":
    """Upload attachment content; return (task attachments, uploaded names).

    On a mid-batch failure every already-uploaded temp file is best-effort
    deleted so partial batches never orphan files in the server's uploads dir.
    """
    from ouroboros.artifacts import (
        _MAX_STAGED_ATTACHMENT_BYTES as _PER_FILE,
        _MAX_STAGED_TOTAL_BYTES as _TOTAL,
    )

    attachments: List[Dict[str, Any]] = []
    uploaded_names: List[str] = []
    # Running aggregate budget re-enforced at READ TIME (R32C3): _validate_attach_
    # paths checks the 200 MB total from pre-upload stat values, but each file can
    # grow (staying under 50 MB) between validation and upload, so N files could
    # otherwise stream far past _TOTAL. Cap each read at the SMALLER of the per-
    # file limit and the budget still remaining, then decrement by the TRUE bytes
    # sent — the batch can never transmit more than _TOTAL.
    remaining = _TOTAL
    for path in paths:
        # Exception-SAFE (R23C2): ANY failure — a CLIError, an OSError from a file
        # that vanished after validation, or a non-dict HTTP-200 body making
        # .get() raise — must release every already-uploaded file, never orphan
        # them in the server's uploads dir. Non-CLIErrors normalize to CLIError.
        try:
            limit = min(_PER_FILE, remaining)
            result = client.post_multipart_file(
                "/api/chat/upload", path, max_bytes=limit, revalidate_secret=True
            )
            data = result if isinstance(result, dict) else {}
            server_path = str(data.get("path") or "")
            server_name = str(data.get("filename") or "")
            if not server_path or not server_name:
                raise CLIError(f"attachment upload failed for {path}: {result}")
            sent = int(getattr(client, "_last_upload_bytes", 0) or 0)
            remaining = max(0, remaining - sent)
        except Exception as exc:
            _cleanup_uploads(client, uploaded_names)
            if isinstance(exc, CLIError):
                raise
            raise CLIError(f"attachment upload failed for {path}: {exc}") from exc
        uploaded_names.append(server_name)
        attachments.append({"path": server_path, "label": path.name})
    return attachments, uploaded_names


def _cleanup_uploads(client: "OuroborosHTTPClient", uploaded_names: List[str]) -> None:
    for name in uploaded_names:
        try:
            client.request("DELETE", "/api/chat/upload", {"filename": name})
        except CLIError:
            pass  # best-effort cleanup; the server prunes uploads eventually


def _run_command(args: argparse.Namespace) -> int:
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        raise CLIError("run requires a prompt")
    if str(args.delegation_role or "root").strip().lower() != "root":
        raise CLIError("delegation_role=subagent is only allowed through the internal schedule_subagent tool")
    user_metadata: Dict[str, Any] = {}
    raw_metadata = str(getattr(args, "task_metadata_json", "") or "").strip()
    if raw_metadata:
        try:
            parsed_metadata = json.loads(raw_metadata)
        except json.JSONDecodeError as exc:
            raise CLIError(f"--task-metadata-json is not valid JSON: {exc}")
        if not isinstance(parsed_metadata, dict):
            raise CLIError("--task-metadata-json must be a JSON object")
        user_metadata = parsed_metadata
    attach_paths = _validate_attach_paths(args.attach)
    client = _client(args, start=args.start)
    attachments, uploaded_names = _upload_attachments(client, attach_paths)
    disabled_tools = []
    for raw in args.disable_tools or []:
        disabled_tools.extend(part.strip() for part in str(raw or "").split(",") if part.strip())
    body = {
        "description": prompt,
        "workspace_root": args.workspace or "",
        "workspace_mode": "external" if args.workspace else "",
        "project_id": getattr(args, "project_id", "") or "",
        "memory_mode": args.memory_mode or ("forked" if args.workspace else "shared"),
        "attachments": attachments,
        "actor_id": args.actor_id,
        # Host-owned service keys are spread LAST so --task-metadata-json can
        # never forge delegation_role/source (subagent forgery stays blocked).
        "metadata": {**user_metadata, "delegation_role": args.delegation_role, "source": "cli"},
        "source": "cli",
    }
    if disabled_tools:
        body["disabled_tools"] = list(dict.fromkeys(disabled_tools))
    if float(args.timeout or 0) > 0:
        body["timeout_sec"] = float(args.timeout or 0)
    try:
        created = client.request("POST", "/api/tasks", body)
        task_id = str(created.get("task_id") or "") if isinstance(created, dict) else ""
        if not task_id:
            raise CLIError(f"task creation did not return task_id: {created}")
    except Exception as exc:
        # Exception-SAFE (R23C2): release the uploaded files on ANY failure —
        # incl. a non-dict HTTP-200 body (created.get would raise) — until a
        # valid task_id takes ownership of them.
        _cleanup_uploads(client, uploaded_names)
        if isinstance(exc, CLIError):
            raise
        raise CLIError(f"task creation failed: {exc}") from exc
    if args.detach:
        if args.jsonl:
            print(json.dumps({"type": "task_created", "task_id": task_id, "data": created}, ensure_ascii=False))
        else:
            print(task_id)
        return 0
    timeout = float(args.timeout or 0)
    wait_timeout = _deadline_wait_timeout(timeout)
    if args.no_stream:
        if args.jsonl:
            print(json.dumps({"type": "task_created", "task_id": task_id, "data": created}, ensure_ascii=False))
        result = _wait_task(client, task_id, timeout_sec=wait_timeout)
    else:
        _watch_task(client, task_id, jsonl=args.jsonl, quiet=args.quiet, timeout_sec=wait_timeout)
        result = client.request("GET", f"/api/tasks/{urllib.parse.quote(task_id)}")
    result = _await_cost_finality(client, task_id, result)
    exit_code = 0 if _is_terminal_success(result) else 1
    if args.patch_out:
        patch = _patch_from_result(client, task_id, result, strict=True)
        pathlib.Path(args.patch_out).expanduser().write_text(patch, encoding="utf-8")
    if args.result_json_out:
        pathlib.Path(args.result_json_out).expanduser().write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.jsonl:
        print(json.dumps({"type": "final", "task_id": task_id, "result": result}, ensure_ascii=False))
    elif args.patch:
        patch = _patch_from_result(client, task_id, result, strict=True)
        print(patch, end="" if patch.endswith("\n") else "\n")
    else:
        print(str(result.get("result") or ""))
    return exit_code


def _tasks_list_command(args: argparse.Namespace) -> int:
    query = f"?limit={int(args.limit)}"
    if args.status:
        query += "&status=" + urllib.parse.quote(args.status)
    _print_json(_client(args).request("GET", "/api/tasks" + query))
    return 0


def _tasks_show_command(args: argparse.Namespace) -> int:
    _print_json(_client(args).request("GET", f"/api/tasks/{urllib.parse.quote(args.task_id)}"))
    return 0


def _tasks_cancel_command(args: argparse.Namespace) -> int:
    data = _client(args).request("POST", f"/api/tasks/{urllib.parse.quote(args.task_id)}/cancel", {})
    _print_json(data)
    return 0 if data.get("ok") else 1


def _tasks_watch_command(args: argparse.Namespace) -> int:
    _watch_task(_client(args), args.task_id, jsonl=args.jsonl, quiet=False, timeout_sec=0)
    return 0


def _chat_send_command(args: argparse.Namespace) -> int:
    text = " ".join(args.text).strip()
    if not text:
        raise CLIError("chat send requires text")
    _print_json(_client(args).request("POST", "/api/command", {"cmd": text}))
    return 0


def _chat_history_command(args: argparse.Namespace) -> int:
    _print_json(_client(args).request("GET", f"/api/chat/history?limit={int(args.limit)}"))
    return 0


def _logs_tail_command(args: argparse.Namespace) -> int:
    query = f"?limit={int(args.limit)}"
    if args.task_id:
        query += "&task_id=" + urllib.parse.quote(args.task_id)
    data = _client(args).request("GET", f"/api/logs/{urllib.parse.quote(args.name)}{query}")
    if args.json:
        _print_json(data)
    else:
        for entry in data.get("entries", []):
            print(json.dumps(entry, ensure_ascii=False))
    return 0


def _logs_follow_command(args: argparse.Namespace) -> int:
    seen = set()
    client = _client(args)
    while True:
        data = client.request("GET", f"/api/logs/{urllib.parse.quote(args.name)}?limit={int(args.limit)}")
        entries = list(data.get("entries") or [])
        for entry in entries:
            marker = (str(entry.get("_source_root") or ""), int(entry.get("_line") or 0), str(entry.get("ts") or ""))
            if marker in seen:
                continue
            seen.add(marker)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
        time.sleep(max(0.5, float(args.interval)))


def _evolve_command(args: argparse.Namespace) -> int:
    client = _client(args)
    if args.evolve_command == "status":
        _print_json(client.request("GET", "/api/state").get("evolution_state", {}))
    elif args.evolve_command == "start":
        # Evolution is hard-blocked in light mode; refuse synchronously here so the
        # CLI does not report success while the server silently rejects the start.
        runtime_mode = str(client.request("GET", "/api/state").get("runtime_mode", "") or "")
        if runtime_mode == "light":
            _print_json({
                "error": "evolution requires runtime_mode 'advanced' or 'pro'; refused in 'light' mode",
                "runtime_mode": runtime_mode,
            })
            return 1
        objective = " ".join(getattr(args, "objective", []) or []).strip()
        cmd = "/evolve on" + (f" {objective}" if objective else "")
        _print_json(client.request("POST", "/api/command", {"cmd": cmd}))
    elif args.evolve_command == "stop":
        _print_json(client.request("POST", "/api/command", {"cmd": "/evolve off"}))
    else:
        while True:
            _print_json(client.request("GET", "/api/state").get("evolution_state", {}))
            time.sleep(max(1.0, float(args.interval)))
    return 0


def _schedule_command(args: argparse.Namespace) -> int:
    client = _client(args)
    if args.schedule_command == "list":
        _print_json(client.request("GET", "/api/schedules"))
        return 0
    if args.schedule_command == "add":
        prompt = " ".join(args.prompt).strip() or args.name
        body = {
            "name": args.name,
            "description": prompt,
            "timezone": args.timezone or "",
            "trigger": {"type": "cron", "expr": args.cron},
            "task": {"type": "task", "text": prompt, "description": prompt},
        }
        _print_json(client.request("POST", "/api/schedules", body))
        return 0
    if args.schedule_command == "remove":
        _print_json(client.request("DELETE", f"/api/schedules/{urllib.parse.quote(args.schedule_id)}"))
        return 0
    raise CLIError(f"unknown schedule command: {args.schedule_command}")


def _settings_get_command(args: argparse.Namespace) -> int:
    data = _client(args).request("GET", "/api/settings")
    if args.key:
        print(json.dumps(data.get(args.key), ensure_ascii=False))
    else:
        _print_json(data)
    return 0


def _settings_set_command(args: argparse.Namespace) -> int:
    value: Any = args.value
    try:
        value = json.loads(args.value)
    except Exception:
        pass
    _print_json(_client(args).request("POST", "/api/settings", {args.key: value}))
    return 0


def _owner_runtime_mode_command(args: argparse.Namespace) -> int:
    _print_json(_client(args).request("POST", "/api/owner/runtime-mode", {"mode": args.mode}))
    return 0


def _owner_context_mode_command(args: argparse.Namespace) -> int:
    _print_json(_client(args).request("POST", "/api/owner/context-mode", {"mode": args.mode}))
    return 0


def _owner_auto_grant_command(args: argparse.Namespace) -> int:
    _print_json(_client(args).request("POST", "/api/owner/auto-grant", {"enabled": args.enabled == "on"}))
    return 0


def _skills_command(args: argparse.Namespace) -> int:
    client = _client(args)
    if args.skills_command == "list":
        _print_json(client.request("GET", "/api/extensions"))
    elif args.skills_command == "toggle":
        _print_json(client.request("POST", f"/api/skills/{urllib.parse.quote(args.name)}/toggle", {"enabled": args.enabled}))
    elif args.skills_command == "grants":
        _print_json(client.request("POST", f"/api/skills/{urllib.parse.quote(args.name)}/grants", {"items": args.items}))
    elif args.skills_command == "queue":
        _print_json(client.request("GET", "/api/skills/lifecycle-queue"))
    return 0


def _marketplace_command(args: argparse.Namespace) -> int:
    client = _client(args)
    hub = args.hub
    if hub == "clawhub":
        paths = {
            "search": f"/api/marketplace/clawhub/search?q={urllib.parse.quote(args.query or '')}",
            "installed": "/api/marketplace/clawhub/installed",
            "preview": f"/api/marketplace/clawhub/preview/{urllib.parse.quote(args.slug)}",
            "install": "/api/marketplace/clawhub/install",
            "update": f"/api/marketplace/clawhub/update/{urllib.parse.quote(args.name)}",
            "uninstall": f"/api/marketplace/clawhub/uninstall/{urllib.parse.quote(args.name)}",
        }
    else:
        paths = {
            "search": f"/api/marketplace/ouroboroshub/catalog?q={urllib.parse.quote(args.query or '')}",
            "installed": "/api/marketplace/ouroboroshub/installed",
            "preview": f"/api/marketplace/ouroboroshub/preview/{urllib.parse.quote(args.slug)}",
            "install": "/api/marketplace/ouroboroshub/install",
            "update": f"/api/marketplace/ouroboroshub/update/{urllib.parse.quote(args.name)}",
            "uninstall": f"/api/marketplace/ouroboroshub/uninstall/{urllib.parse.quote(args.name)}",
        }
    method = "GET" if args.marketplace_command in {"search", "installed", "preview"} else "POST"
    body = {"slug": getattr(args, "slug", "")} if args.marketplace_command == "install" else {}
    _print_json(client.request(method, paths[args.marketplace_command], body if method == "POST" else None))
    return 0


def _local_model_command(args: argparse.Namespace) -> int:
    method = "GET" if args.local_model_command == "status" else "POST"
    body: Dict[str, Any] = {}
    if args.local_model_command == "start":
        body = {
            "source": args.source,
            "filename": args.filename,
            "port": args.port,
            "n_gpu_layers": args.n_gpu_layers,
            "n_ctx": args.n_ctx,
            "chat_format": args.chat_format,
        }
    _print_json(_client(args).request(method, f"/api/local-model/{args.local_model_command}", body if method == "POST" else None))
    return 0


def _mcp_command(args: argparse.Namespace) -> int:
    method = "GET" if args.mcp_command == "status" else "POST"
    body = {"server_id": args.server_id} if getattr(args, "server_id", "") else {}
    _print_json(_client(args).request(method, f"/api/mcp/{args.mcp_command}", body if method == "POST" else None))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ouroboros")
    parser.add_argument("--url", default="", help="Ouroboros server URL")
    subparsers = parser.add_subparsers(dest="command")

    server_parser = subparsers.add_parser("server", help="run the Ouroboros web server")
    server_parser.add_argument("--host", default="", help="host/interface to bind")
    server_parser.add_argument("--port", type=int, default=0, help="port to bind")
    server_parser.add_argument("--no-ui", action="store_true", help="accepted for CLI parity; server mode has no desktop UI")
    server_parser.set_defaults(func=_server_command)

    status = subparsers.add_parser("status", help="show runtime status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_status_command)

    run = subparsers.add_parser("run", help="run a managed headless task")
    run.add_argument("--start", action="store_true", help="start a local server if attach fails")
    run.add_argument("--workspace", default="", help="external workspace root")
    run.add_argument("--project-id", default="", help="per-project facts scope id (else derived from the workspace path)")
    run.add_argument("--memory-mode", choices=["shared", "forked", "empty"], default="")
    run.add_argument("--attach", action="append", default=[])
    run.add_argument("--jsonl", action="store_true")
    run.add_argument("--quiet", action="store_true")
    run.add_argument("--no-stream", action="store_true")
    run.add_argument("--detach", action="store_true", help="create the task and print its task id without waiting")
    run.add_argument("--timeout", type=float, default=0.0, help="maximum seconds to wait for task completion (0 = no limit)")
    run.add_argument("--patch", action="store_true", help="print workspace patch instead of final answer")
    run.add_argument("--patch-out", default="", help="write workspace patch to this path")
    run.add_argument("--result-json-out", default="", help="write final task result JSON to this path")
    run.add_argument("--disable-tools", action="append", default=[], help="comma-separated tool names to withhold from this task")
    run.add_argument(
        "--task-metadata-json",
        default="",
        help="JSON object merged into the task metadata (e.g. budget_profile); "
        "host-owned keys delegation_role/source cannot be overridden",
    )
    run.add_argument("--actor-id", default="cli")
    run.add_argument("--delegation-role", default="root")
    run.add_argument("prompt", nargs=argparse.REMAINDER)
    run.set_defaults(func=_run_command)

    tasks = subparsers.add_parser("tasks", help="inspect managed tasks")
    task_sub = tasks.add_subparsers(dest="tasks_command", required=True)
    task_list = task_sub.add_parser("list")
    task_list.add_argument("--limit", type=int, default=50)
    task_list.add_argument("--status", default="")
    task_list.set_defaults(func=_tasks_list_command)
    task_show = task_sub.add_parser("show")
    task_show.add_argument("task_id")
    task_show.set_defaults(func=_tasks_show_command)
    task_watch = task_sub.add_parser("watch")
    task_watch.add_argument("task_id")
    task_watch.add_argument("--jsonl", action="store_true")
    task_watch.set_defaults(func=_tasks_watch_command)
    task_cancel = task_sub.add_parser("cancel")
    task_cancel.add_argument("task_id")
    task_cancel.set_defaults(func=_tasks_cancel_command)

    chat = subparsers.add_parser("chat", help="send chat messages and read history")
    chat_sub = chat.add_subparsers(dest="chat_command", required=True)
    chat_send = chat_sub.add_parser("send")
    chat_send.add_argument("text", nargs=argparse.REMAINDER)
    chat_send.set_defaults(func=_chat_send_command)
    chat_history = chat_sub.add_parser("history")
    chat_history.add_argument("--limit", type=int, default=100)
    chat_history.set_defaults(func=_chat_history_command)

    logs = subparsers.add_parser("logs", help="read runtime logs")
    logs_sub = logs.add_subparsers(dest="logs_command", required=True)
    logs_tail = logs_sub.add_parser("tail")
    logs_tail.add_argument("name", choices=["chat", "progress", "events", "tools", "supervisor"])
    logs_tail.add_argument("--limit", type=int, default=100)
    logs_tail.add_argument("--task-id", default="")
    logs_tail.add_argument("--json", action="store_true")
    logs_tail.set_defaults(func=_logs_tail_command)
    logs_follow = logs_sub.add_parser("follow")
    logs_follow.add_argument("name", choices=["chat", "progress", "events", "tools", "supervisor"])
    logs_follow.add_argument("--limit", type=int, default=100)
    logs_follow.add_argument("--interval", type=float, default=2.0)
    logs_follow.set_defaults(func=_logs_follow_command)

    evolve = subparsers.add_parser("evolve", help="control evolution mode")
    evo_sub = evolve.add_subparsers(dest="evolve_command", required=True)
    for name in ("start", "stop", "status"):
        p = evo_sub.add_parser(name)
        if name == "start":
            p.add_argument("objective", nargs="*", help="Optional evolution campaign objective")
        p.set_defaults(func=_evolve_command)
    evo_watch = evo_sub.add_parser("watch")
    evo_watch.add_argument("--interval", type=float, default=5.0)
    evo_watch.set_defaults(func=_evolve_command)

    schedule = subparsers.add_parser("schedule", help="manage scheduled tasks")
    schedule_sub = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_sub.add_parser("list").set_defaults(func=_schedule_command)
    schedule_add = schedule_sub.add_parser("add")
    schedule_add.add_argument("--name", required=True)
    schedule_add.add_argument("--cron", required=True, help="5-field cron expression")
    schedule_add.add_argument("--timezone", default="", help="Optional IANA timezone")
    schedule_add.add_argument("prompt", nargs="*", help="Task prompt to enqueue")
    schedule_add.set_defaults(func=_schedule_command)
    schedule_remove = schedule_sub.add_parser("remove")
    schedule_remove.add_argument("schedule_id")
    schedule_remove.set_defaults(func=_schedule_command)

    _add_settings_parser(subparsers)
    _add_skills_parser(subparsers)
    _add_marketplace_parser(subparsers)
    _add_local_model_parser(subparsers)
    _add_mcp_parser(subparsers)
    return parser


def _add_settings_parser(subparsers: argparse._SubParsersAction) -> None:
    settings = subparsers.add_parser("settings", help="read/update settings through gateway")
    sub = settings.add_subparsers(dest="settings_command", required=True)
    get = sub.add_parser("get")
    get.add_argument("key", nargs="?")
    get.set_defaults(func=_settings_get_command)
    setp = sub.add_parser("set")
    setp.add_argument("key")
    setp.add_argument("value")
    setp.set_defaults(func=_settings_set_command)
    mode = sub.add_parser("runtime-mode")
    mode.add_argument("mode", choices=["light", "advanced", "pro"])
    mode.set_defaults(func=_owner_runtime_mode_command)
    context_mode = sub.add_parser("context-mode")
    context_mode.add_argument("mode", choices=["low", "max"])
    context_mode.set_defaults(func=_owner_context_mode_command)
    grant = sub.add_parser("auto-grant")
    grant.add_argument("enabled", choices=["on", "off"])
    grant.set_defaults(func=_owner_auto_grant_command)


def _add_skills_parser(subparsers: argparse._SubParsersAction) -> None:
    skills = subparsers.add_parser("skills", help="skills lifecycle wrappers")
    sub = skills.add_subparsers(dest="skills_command", required=True)
    sub.add_parser("list").set_defaults(func=_skills_command)
    sub.add_parser("queue").set_defaults(func=_skills_command)
    toggle = sub.add_parser("toggle")
    toggle.add_argument("name")
    toggle.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    toggle.set_defaults(func=_skills_command)
    grants = sub.add_parser("grants")
    grants.add_argument("name")
    grants.add_argument("items", nargs="+")
    grants.set_defaults(func=_skills_command)


def _add_marketplace_parser(subparsers: argparse._SubParsersAction) -> None:
    market = subparsers.add_parser("marketplace", help="marketplace wrappers")
    market.add_argument("hub", choices=["clawhub", "ouroboroshub"])
    sub = market.add_subparsers(dest="marketplace_command", required=True)
    search = sub.add_parser("search")
    search.add_argument("query", nargs="?")
    search.set_defaults(func=_marketplace_command)
    sub.add_parser("installed").set_defaults(func=_marketplace_command)
    preview = sub.add_parser("preview")
    preview.add_argument("slug")
    preview.set_defaults(func=_marketplace_command)
    install = sub.add_parser("install")
    install.add_argument("slug")
    install.set_defaults(func=_marketplace_command)
    update = sub.add_parser("update")
    update.add_argument("name")
    update.set_defaults(func=_marketplace_command)
    uninstall = sub.add_parser("uninstall")
    uninstall.add_argument("name")
    uninstall.set_defaults(func=_marketplace_command)


def _add_local_model_parser(subparsers: argparse._SubParsersAction) -> None:
    local = subparsers.add_parser("local-model", help="local model lifecycle wrappers")
    sub = local.add_subparsers(dest="local_model_command", required=True)
    sub.add_parser("status").set_defaults(func=_local_model_command)
    start = sub.add_parser("start")
    start.add_argument("source")
    start.add_argument("--filename", default="")
    start.add_argument("--port", type=int, default=8766)
    start.add_argument("--n-gpu-layers", type=int, default=-1)
    start.add_argument("--n-ctx", type=int, default=0)
    start.add_argument("--chat-format", default="")
    start.set_defaults(func=_local_model_command)
    for name in ("stop", "test", "install-runtime"):
        sub.add_parser(name).set_defaults(func=_local_model_command)


def _add_mcp_parser(subparsers: argparse._SubParsersAction) -> None:
    mcp = subparsers.add_parser("mcp", help="MCP wrappers")
    sub = mcp.add_subparsers(dest="mcp_command", required=True)
    sub.add_parser("status").set_defaults(func=_mcp_command)
    refresh = sub.add_parser("refresh")
    refresh.add_argument("--server-id", default="")
    refresh.set_defaults(func=_mcp_command)
    test = sub.add_parser("test")
    test.add_argument("--server-id", default="")
    test.set_defaults(func=_mcp_command)


def _client(args: argparse.Namespace, *, start: bool = False) -> OuroborosHTTPClient:
    client = OuroborosHTTPClient(getattr(args, "url", "") or "")
    try:
        client.request("GET", "/api/health")
        return client
    except ConnectionCLIError:
        if not start:
            raise
    parsed = urllib.parse.urlparse(client.base_url)
    host = (parsed.hostname or DEFAULT_HOST).lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise CLIError("--start can only launch a local loopback Ouroboros server")
    _start_local_server(client.base_url)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            client.request("GET", "/api/health")
            return client
        except CLIError:
            time.sleep(0.5)
    raise CLIError(f"server did not become ready at {client.base_url}")


def _default_base_url() -> str:
    env_url = os.environ.get("OUROBOROS_URL", "").strip()
    if env_url:
        return env_url
    port = DEFAULT_PORT
    try:
        from ouroboros.config import DATA_DIR

        port_text = (pathlib.Path(DATA_DIR) / "state" / "server_port").read_text(encoding="utf-8").strip()
        if port_text:
            port = int(port_text)
    except Exception:
        pass
    return f"http://{DEFAULT_HOST}:{port}"


def _start_local_server(base_url: str) -> None:
    if os.environ.get("OUROBOROS_PACKAGED_CLI") == "1":
        raise CLIError("packaged CLI must launch the desktop app for --start, not source server.py")
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname or DEFAULT_HOST
    port = int(parsed.port or DEFAULT_PORT)
    cmd = [sys.executable, "-m", "ouroboros.cli", "server", "--host", host, "--port", str(port)]
    from ouroboros.platform_layer import subprocess_new_group_kwargs

    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **subprocess_new_group_kwargs(),
    )


def _deadline_wait_timeout(timeout_sec: float) -> float:
    if not timeout_sec or timeout_sec <= 0:
        return 0.0
    from ouroboros.config import get_finalization_grace_sec

    grace = float(get_finalization_grace_sec())
    return float(timeout_sec) + max(0.0, min(grace, 300.0)) + 5.0


def _watch_task(
    client: OuroborosHTTPClient,
    task_id: str,
    *,
    jsonl: bool,
    quiet: bool,
    timeout_sec: float,
) -> None:
    cursor = 0
    final = False
    deadline = time.time() + timeout_sec if timeout_sec and timeout_sec > 0 else None
    while not final:
        wait_param = 30
        request_timeout = 40.0
        if deadline is not None and time.time() >= deadline:
            raise TaskTimeoutCLIError(f"task {task_id} did not finish within {timeout_sec:g}s")
        if deadline is not None:
            remaining = max(0.0, deadline - time.time())
            wait_param = max(0, min(30, int(remaining)))
            request_timeout = max(1.0, min(40.0, remaining + 1.0))
        path = f"/api/tasks/{urllib.parse.quote(task_id)}/events?cursor={cursor}&wait={wait_param}"
        saw_event = False
        for event in client.stream_sse(path, timeout=request_timeout):
            if deadline is not None and time.time() >= deadline:
                raise TaskTimeoutCLIError(f"task {task_id} did not finish within {timeout_sec:g}s")
            saw_event = True
            cursor = max(cursor, int(event.get("seq") or cursor))
            if jsonl:
                print(json.dumps(event, ensure_ascii=False), flush=True)
            elif not quiet:
                rendered = _render_event_for_stderr(event)
                if rendered:
                    print(rendered, file=sys.stderr, flush=True)
            if event.get("type") == "task_result":
                final = _is_terminal_result((event.get("data") or {}))
        if not saw_event:
            time.sleep(0.5)


def _wait_task(client: OuroborosHTTPClient, task_id: str, *, timeout_sec: float) -> Dict[str, Any]:
    deadline = time.time() + timeout_sec if timeout_sec and timeout_sec > 0 else None
    while True:
        request_timeout: Optional[float] = None
        if deadline is not None:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TaskTimeoutCLIError(f"task {task_id} did not finish within {timeout_sec:g}s")
            request_timeout = min(client.timeout, remaining)
        try:
            result = client.request("GET", f"/api/tasks/{urllib.parse.quote(task_id)}", timeout=request_timeout)
        except ConnectionCLIError:
            if deadline is not None and time.time() >= deadline:
                raise TaskTimeoutCLIError(f"task {task_id} did not finish within {timeout_sec:g}s")
            raise
        if _is_terminal_result(result):
            return result
        if deadline is not None and time.time() >= deadline:
            raise TaskTimeoutCLIError(f"task {task_id} did not finish within {timeout_sec:g}s")
        sleep_for = 1.0
        if deadline is not None:
            sleep_for = max(0.0, min(1.0, deadline - time.time()))
        if sleep_for > 0:
            time.sleep(sleep_for)


def _parse_sse_lines(resp: Iterable[bytes]) -> Iterator[Dict[str, Any]]:
    data_lines: List[str] = []
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        if not line:
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines = []
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    pass
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())


def _render_event_for_stderr(event: Dict[str, Any]) -> str:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    etype = str(event.get("type") or "")
    if etype == "progress":
        return str(data.get("content") or data.get("text") or "").strip()
    if etype == "tool_call":
        return f"tool: {data.get('tool', '?')}"
    if etype in {"task_done", "task_metrics"}:
        return f"{etype}: {data.get('task_id', '')}"
    return ""


def _patch_from_result(
    client: OuroborosHTTPClient,
    task_id: str,
    result: Dict[str, Any],
    *,
    strict: bool,
) -> str:
    bundle = result.get("artifact_bundle") if isinstance(result.get("artifact_bundle"), dict) else {}
    artifact_status = str(bundle.get("status") or result.get("artifact_status") or "").lower()
    if artifact_status == "failed":
        raise PatchCLIError(str(result.get("artifact_error") or "workspace patch artifact failed"))
    if artifact_status == "missing":
        raise PatchCLIError("workspace patch artifact is missing")
    if artifact_status == "ready_no_changes":
        if strict:
            raise PatchCLIError("workspace patch artifact has no changes")
        return ""
    if artifact_status in {"pending", "finalizing"}:
        raise PatchCLIError(f"workspace patch artifact is not finalized (artifact_status={artifact_status})")
    artifacts = [artifact for artifact in result.get("artifacts") or [] if isinstance(artifact, dict)]
    patch_artifact = next((artifact for artifact in artifacts if artifact.get("kind") == "workspace_patch"), None)
    if patch_artifact is None:
        patch_artifact = next((
            artifact
            for artifact in artifacts
            if str(artifact.get("name") or pathlib.Path(str(artifact.get("path") or "")).name) == "workspace.patch"
        ), None)
    if patch_artifact is not None:
        patch_status = str(patch_artifact.get("status") or "").lower()
        if patch_status == "missing":
            raise PatchCLIError("workspace patch artifact is missing")
        if patch_status == "failed":
            raise PatchCLIError("workspace patch artifact failed")
        name = str(patch_artifact.get("name") or pathlib.Path(str(patch_artifact.get("path") or "")).name or "workspace.patch")
        raw = client.get_bytes(f"/api/tasks/{urllib.parse.quote(task_id)}/artifacts/{urllib.parse.quote(name)}")
        if strict and not raw:
            raise PatchCLIError("workspace patch artifact is empty")
        return raw.decode("utf-8", errors="replace")
    if strict:
        raise PatchCLIError("workspace patch artifact is missing")
    return ""


def _await_cost_finality(
    client: "OuroborosHTTPClient",
    task_id: str,
    result: Dict[str, Any],
    *,
    grace_sec: float = 60.0,
) -> Dict[str, Any]:
    """Bounded wait for the post-task cost checkpoint (v6.74.0, C5).

    ``task_cost_finalized`` is emitted ONLY for ``completed``/``degraded``
    outcomes (post_task_checkpoint), so any other terminal status reads
    immediately — an unconditional wait would hang forever on a failed or
    cancelled task. Within the grace window the reader polls until the result
    reports ``cost_final`` (or ``cost_with_children_partial`` false); past it,
    the partial-marked result is returned as-is — the partial flags stay
    visible, disclosed rather than blocking."""
    status = str(result.get("status") or "").lower()
    if status not in {"completed", "degraded"}:
        return result
    # Wait ONLY when the result EXPLICITLY says cost is still partial. A record
    # without the checkpoint fields (older results, minimal test fixtures) has
    # nothing to wait for and reads immediately.
    pending = (
        result.get("cost_final") is False
        or result.get("cost_with_children_partial") is True
    )
    if not pending:
        return result
    deadline = time.time() + max(0.0, float(grace_sec))
    latest = result
    while time.time() < deadline:
        time.sleep(min(2.0, max(0.1, deadline - time.time())))
        try:
            latest = client.request(
                "GET", f"/api/tasks/{urllib.parse.quote(task_id)}",
            )
        except CLIError:
            return result
        if latest.get("cost_final") or latest.get("cost_with_children_partial") is False:
            return latest
    return latest


def _is_terminal_result(result: Dict[str, Any]) -> bool:
    status = str(result.get("status") or "").lower()
    from ouroboros.task_status import SETTLED_STATUSES
    if status not in SETTLED_STATUSES:
        return False
    bundle = result.get("artifact_bundle") if isinstance(result.get("artifact_bundle"), dict) else {}
    artifact_status = str(bundle.get("status") or result.get("artifact_status") or "").lower()
    return artifact_status not in {"pending", "finalizing"}


def _is_terminal_success(result: Dict[str, Any]) -> bool:
    if str(result.get("status") or "").lower() != "completed":
        return False
    artifact_status = str(result.get("artifact_status") or "").lower()
    bundle = result.get("artifact_bundle") if isinstance(result.get("artifact_bundle"), dict) else {}
    bundle_status = str(bundle.get("status") or "").lower()
    bad_artifact_states = {"failed", "pending", "finalizing", "missing"}
    if artifact_status in bad_artifact_states or bundle_status in bad_artifact_states:
        return False
    try:
        from ouroboros.outcomes import normalize_outcome_axes
        axes = normalize_outcome_axes(result)
    except Exception:
        axes = result.get("outcome_axes") if isinstance(result.get("outcome_axes"), dict) else {}
    execution = axes.get("execution") if isinstance(axes.get("execution"), dict) else {}
    objective = axes.get("objective") if isinstance(axes.get("objective"), dict) else {}
    if str(execution.get("status") or "ok").lower() != "ok":
        return False
    if str(objective.get("status") or "not_evaluated").lower() in {"fail", "degraded"}:
        return False
    return True


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except PatchCLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except TaskTimeoutCLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except CLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
