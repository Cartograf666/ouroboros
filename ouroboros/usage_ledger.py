"""Durable append-only usage ledger: the substrate the accounting layer writes on.

ONE job, kept apart from policy: own the bytes. Cross-process locking, atomic
append + fsync, structural validation of every row and transition, and
quarantine of a torn tail. It knows what a well-formed ledger row IS; it has no
opinion about reservations, budgets, pricing, or projections — those live in
``usage_accounting``, which imports FROM here and is never imported BY here.

The seam is one-way by construction, so the monetary authority (the file) cannot
be corrupted by a change in accounting policy, and a locking or fsync fix cannot
silently alter what a reservation means.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import pathlib
import threading
import uuid
from typing import Any, Dict, Iterator, Optional, Sequence, Tuple

from ouroboros.utils import append_jsonl, utc_now_iso

log = logging.getLogger(__name__)

LEDGER_REL = pathlib.Path("state/usage_attempts.jsonl")
QUARANTINE_REL = pathlib.Path("state/usage_attempts.quarantine.jsonl")
_TERMINAL = frozenset({"settled", "unresolved", "released"})

__all__ = (
    "LEDGER_REL", "QUARANTINE_REL", "UsageAccountingError", "UsageLedgerCorrupt",
)


class UsageAccountingError(RuntimeError):
    """Base error for fail-closed accounting operations."""


class UsageLedgerCorrupt(UsageAccountingError):
    """Raised when durable history is structurally invalid."""


def _drive_root(value: pathlib.Path | str | None = None) -> pathlib.Path:
    if value is not None:
        if not isinstance(value, (str, pathlib.Path)):
            raise UsageAccountingError(f"invalid usage accounting drive root type: {type(value).__name__}")
        resolved = pathlib.Path(value)
        if not resolved.is_absolute():
            raise UsageAccountingError(f"usage accounting drive root must be absolute: {resolved}")
        return resolved
    configured = str(os.environ.get("OUROBOROS_DATA_DIR") or "").strip()
    if configured:
        resolved = pathlib.Path(configured)
        if not resolved.is_absolute():
            raise UsageAccountingError(f"OUROBOROS_DATA_DIR must be absolute for usage accounting: {resolved}")
        return resolved
    from ouroboros.config import DATA_DIR

    return pathlib.Path(DATA_DIR)


@contextlib.contextmanager
def _named_lock(
    root: pathlib.Path,
    filename: str,
    *,
    timeout_sec: float,
    stale_sec: float,
) -> Iterator[None]:
    from ouroboros.platform_layer import (
        acquire_exclusive_file_lock,
        release_exclusive_file_lock,
    )

    path = root / "state" / filename
    fd = acquire_exclusive_file_lock(path, timeout_sec=timeout_sec, stale_sec=stale_sec)
    if fd is None:
        raise UsageAccountingError(f"usage accounting lock unavailable: {path}")
    try:
        yield
    finally:
        release_exclusive_file_lock(path, fd)


@contextlib.contextmanager
def _locked(root: pathlib.Path) -> Iterator[None]:
    # Operator fix 2026-07-23: 4.0s starves under a grown ledger (reserve_attempt
    # re-reads the whole usage_attempts.jsonl under this lock — ~0.5s hold at 20MB),
    # failing healthy tasks with UsageAccountingError at >=10 concurrent workers.
    # Waiting longer is always correct here; the transaction itself stays atomic.
    with _named_lock(root, "usage_attempts.lock", timeout_sec=45.0, stale_sec=90.0):
        yield


def _append_bytes_fsync(path: pathlib.Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(f"short append to {path}")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_bytes_atomic_fsync(path: pathlib.Path, payload: bytes) -> None:
    """Persist the exact snapshotted bytes without reopening the source."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}")
    fd: Optional[int] = None
    try:
        # Windows defaults low-level descriptors to text mode, which would
        # expand LF bytes and break the archive's immutable source hash.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(str(tmp), flags, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(f"short write to {tmp}")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, path)
    except Exception:
        if fd is not None:
            os.close(fd)
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _quarantine_tail(root: pathlib.Path, raw: bytes, offset: int, reason: str) -> None:
    ledger = root / LEDGER_REL
    row = {
        "ts": utc_now_iso(),
        "reason": reason,
        "source": str(ledger),
        "raw_base64": base64.b64encode(raw).decode("ascii"),
    }
    _append_bytes_fsync(
        root / QUARANTINE_REL,
        (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
    )
    fd = os.open(str(ledger), os.O_RDWR)
    try:
        os.ftruncate(fd, offset)
        os.fsync(fd)
    finally:
        os.close(fd)
    log.error("Quarantined corrupt final usage-ledger row: %s", reason)
    try:
        append_jsonl(
            root / "logs" / "events.jsonl",
            {"type": "usage_ledger_tail_quarantined", "ts": utc_now_iso(), "reason": reason},
        )
    except Exception:
        log.exception("Failed to emit usage-ledger quarantine event")


def _validate_records(records: Sequence[Dict[str, Any]]) -> None:
    states: Dict[str, str] = {}
    expected = 1
    for row in records:
        try:
            sequence = int(row.get("seq") or 0) if isinstance(row, dict) else 0
        except (TypeError, ValueError, OverflowError) as exc:
            raise UsageLedgerCorrupt(f"invalid usage ledger sequence at {expected}") from exc
        if not isinstance(row, dict) or sequence != expected:
            raise UsageLedgerCorrupt(f"usage ledger sequence mismatch at {expected}")
        expected += 1
        attempt_id = str(row.get("attempt_id") or "")
        state = str(row.get("state") or "")
        kind = str(row.get("kind") or "attempt")
        if not attempt_id or state not in {"reserved", "dispatched", *_TERMINAL}:
            raise UsageLedgerCorrupt(f"invalid usage ledger row seq={row.get('seq')}")
        for numeric_field in (
            "cost_usd", "reservation_upper_bound_usd", "reservation_usd",
            "max_budget_usd", "global_limit_usd", "root_limit_usd",
        ):
            if row.get(numeric_field) is not None and _number(row.get(numeric_field)) is None:
                raise UsageLedgerCorrupt(f"invalid {numeric_field} in usage row seq={sequence}")
        for token_field in (
            "prompt_tokens", "completion_tokens", "cached_tokens",
            "cache_write_tokens", "ambiguous_call_count",
        ):
            if row.get(token_field) is None:
                continue
            try:
                value = int(row.get(token_field))
            except (TypeError, ValueError, OverflowError) as exc:
                raise UsageLedgerCorrupt(
                    f"invalid {token_field} in usage row seq={sequence}"
                ) from exc
            if value < 0 or isinstance(row.get(token_field), bool):
                raise UsageLedgerCorrupt(
                    f"invalid {token_field} in usage row seq={sequence}"
                )
        previous = states.get(attempt_id)
        if kind.startswith("legacy_") or kind in {"external_unmetered", "subscription_session"}:
            if previous is not None or state not in {"settled", "unresolved"}:
                raise UsageLedgerCorrupt(f"invalid legacy usage row seq={row.get('seq')}")
        elif previous is None:
            if state != "reserved":
                raise UsageLedgerCorrupt(f"attempt {attempt_id} did not begin reserved")
        elif previous == "reserved":
            if state not in {"dispatched", "released"}:
                raise UsageLedgerCorrupt(f"invalid transition {previous}->{state}")
        elif previous == "dispatched":
            if state not in {"settled", "unresolved"}:
                raise UsageLedgerCorrupt(f"invalid transition {previous}->{state}")
        else:
            raise UsageLedgerCorrupt(f"attempt {attempt_id} changed after terminal state")
        states[attempt_id] = state


def _read_records_locked(root: pathlib.Path) -> list[Dict[str, Any]]:
    path = root / LEDGER_REL
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise UsageAccountingError(f"cannot read usage ledger: {exc}") from exc
    records: list[Dict[str, Any]] = []
    record_locations: list[Tuple[int, bytes]] = []
    chunks = data.splitlines(keepends=True)
    nonempty = [index for index, chunk in enumerate(chunks) if chunk.rstrip(b"\r\n")]
    last_nonempty = nonempty[-1] if nonempty else -1
    offset = 0
    for index, chunk in enumerate(chunks):
        raw = chunk.rstrip(b"\r\n")
        if not raw:
            offset += len(chunk)
            continue
        try:
            row = json.loads(raw.decode("utf-8"))
            if not isinstance(row, dict):
                raise ValueError("row is not an object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            if index == last_nonempty:
                _quarantine_tail(root, chunk, offset, f"{type(exc).__name__}: {exc}")
                break
            raise UsageLedgerCorrupt(f"corrupt usage ledger row before tail: {index + 1}") from exc
        records.append(row)
        record_locations.append((offset, chunk))
        offset += len(chunk)
    try:
        _validate_records(records)
    except UsageLedgerCorrupt:
        # A final row can be valid JSON yet still be torn structurally (wrong
        # seq, illegal transition, missing fields). Preserve the validated
        # history exactly as for a JSON-torn tail; corruption before the final
        # row remains a hard failure.
        if not records or not record_locations:
            raise
        try:
            _validate_records(records[:-1])
        except UsageLedgerCorrupt:
            raise
        bad_offset, bad_chunk = record_locations[-1]
        _quarantine_tail(root, bad_chunk, bad_offset, "structurally invalid final ledger row")
        records.pop()
    return records


def _append_rows_locked(
    root: pathlib.Path,
    records: Sequence[Dict[str, Any]],
    rows: Sequence[Dict[str, Any]],
) -> list[Dict[str, Any]]:
    if not rows:
        return []
    sequence = len(records)
    materialized: list[Dict[str, Any]] = []
    for raw in rows:
        sequence += 1
        materialized.append({**raw, "seq": sequence, "ts": str(raw.get("ts") or utc_now_iso())})
    _validate_records([*records, *materialized])
    payload = b"".join(
        (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        for row in materialized
    )
    _append_bytes_fsync(root / LEDGER_REL, payload)
    return materialized


def _number(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 and parsed == parsed else None


def _final_rows(records: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(row["attempt_id"]): row for row in records}
