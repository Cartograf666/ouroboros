"""Read/write helpers for the existing append-only Skill Review history."""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any, Dict, List, Optional

from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock
from ouroboros.tools.review_helpers import format_obligation_excerpt
from ouroboros.utils import append_jsonl, iter_jsonl_objects, jsonl_append_lock_path, utc_now_iso

log = logging.getLogger(__name__)


def _redact_history_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    from ouroboros.observability import redact_projection

    redacted = redact_projection(payload).value
    return redacted if isinstance(redacted, dict) else {}


def review_history_path(drive_root: pathlib.Path, skill_name: str) -> pathlib.Path:
    return drive_root / "state" / "skills" / skill_name / "review_history.jsonl"


def dispatch_marker_path(drive_root: pathlib.Path, skill_name: str) -> pathlib.Path:
    return drive_root / "state" / "skills" / skill_name / "review_dispatch.json"


def _emit_history_event(drive_root: pathlib.Path, event: Dict[str, Any]) -> None:
    """Loud typed event on the existing events rail (never raises)."""
    try:
        append_jsonl(
            pathlib.Path(drive_root) / "logs" / "events.jsonl",
            {"ts": utc_now_iso(), **event},
        )
    except Exception:
        log.debug("skill review history event emission failed", exc_info=True)


def load_dispatch_marker(drive_root: pathlib.Path, skill_name: str) -> Dict[str, Any]:
    """The current write-ahead dispatch marker, ``{}`` when none/unreadable."""
    try:
        data = json.loads(dispatch_marker_path(drive_root, skill_name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def clear_dispatch_marker(drive_root: pathlib.Path, skill_name: str, *, wave_id: str) -> None:
    """Remove the marker once its wave's terminal row landed (merge complete)."""
    if not wave_id:
        return
    marker = load_dispatch_marker(drive_root, skill_name)
    if str(marker.get("wave_id") or "") != str(wave_id):
        return
    try:
        dispatch_marker_path(drive_root, skill_name).unlink()
    except OSError:
        log.debug("skill review dispatch marker unlink failed", exc_info=True)


def write_dispatch_marker(
    drive_root: pathlib.Path,
    skill_name: str,
    *,
    wave_id: str,
    group_id: str,
    content_hash: str,
    root_task_id: str = "",
    review_contract_fingerprint: str = "",
    rebuttal_sha256: str = "",
) -> None:
    """Durable WRITE-AHEAD dispatch marker (Q17; same principle as the commit
    gate's paid stamp): written immediately before the first physical reviewer
    transport call of ONE skill-review wave, shared by the lifecycle runner and
    direct ``review_skill`` callers. Terminal history rows merge it (facts ride
    the row, marker cleared); a wave that never lands a terminal row keeps the
    marker, so the derived paid-cycle count never forgets spent money. An
    unmerged predecessor from a crashed wave is first flushed into the history
    as an infra terminal carrying its paid facts. A failing write surfaces as a
    loud typed event — this is fail-open cost accounting, not a safety gate."""
    from ouroboros.utils import atomic_write_json

    predecessor = load_dispatch_marker(drive_root, skill_name)
    if predecessor.get("wave_id") and str(predecessor["wave_id"]) != str(wave_id):
        _flush_orphan_dispatch_marker(drive_root, skill_name, predecessor)
    payload = {
        "ts": utc_now_iso(),
        "wave_id": str(wave_id),
        "group_id": str(group_id or ""),
        "content_hash": str(content_hash or ""),
        "root_task_id": str(root_task_id or ""),
        "paid": True,
        "review_contract_fingerprint": str(review_contract_fingerprint or ""),
        "rebuttal_sha256": str(rebuttal_sha256 or ""),
    }
    try:
        path = dispatch_marker_path(drive_root, skill_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Same redaction invariant as the history rows: marker fields are all
        # hashes/ids today, so this is a no-op class guard, not a data change.
        atomic_write_json(path, _redact_history_payload(payload), trailing_newline=True)
    except Exception:
        log.warning("skill review dispatch marker write failed for %s", skill_name, exc_info=True)
        _emit_history_event(drive_root, {
            "type": "skill_review_history_append_failed",
            "skill": skill_name, "wave_id": str(wave_id),
            "reason": "dispatch marker write failed",
        })


def _flush_orphan_dispatch_marker(
    drive_root: pathlib.Path, skill_name: str, marker: Dict[str, Any]
) -> None:
    """A previous wave dispatched but never finalized (crash, or a direct-call
    infra outcome that returns without a history row): append its paid facts
    as an infra terminal row — idempotently keyed by the wave id — so the
    ledger catches up instead of forgetting the spend."""
    append_history_once(drive_root, skill_name, {
        "ts": utc_now_iso(),
        "status": "interrupted",
        "terminal_reason": "dispatched_wave_never_finalized",
        "content_hash": str(marker.get("content_hash") or ""),
        "group_id": str(marker.get("group_id") or ""),
        "root_task_id": str(marker.get("root_task_id") or ""),
        "paid": True,
        "review_contract_fingerprint": str(marker.get("review_contract_fingerprint") or ""),
        "rebuttal_sha256": str(marker.get("rebuttal_sha256") or ""),
        "job_id": str(marker.get("wave_id") or ""),
        "wave_id": str(marker.get("wave_id") or ""),
        "failure_signature": [],
        "fail_findings": [],
    })


def _merge_dispatch_marker_facts(
    drive_root: pathlib.Path, skill_name: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Merge the write-ahead marker's paid facts into ITS wave's terminal row.

    The producer can lose the facts legitimately — a lifecycle timeout
    finalizes with no result object — but the marker recorded the dispatch
    before the first transport call, so the terminal row still carries
    ``paid``/contract/rebuttal. Rows of other waves pass through untouched."""
    marker = load_dispatch_marker(drive_root, skill_name)
    wave = str(marker.get("wave_id") or "")
    row_wave = str(payload.get("wave_id") or payload.get("job_id") or "")
    if not wave or wave != row_wave:
        return payload
    merged = dict(payload)
    if marker.get("paid") and not merged.get("paid"):
        merged["paid"] = True
    for key in ("review_contract_fingerprint", "rebuttal_sha256",
                "group_id", "content_hash", "root_task_id"):
        if marker.get(key) and not merged.get(key):
            merged[key] = marker[key]
    merged.setdefault("wave_id", wave)
    return merged


def finding_signature(findings: List[Dict[str, Any]]) -> List[str]:
    return sorted({
        f"{finding.get('item')}:{finding.get('verdict')}:{finding.get('severity')}"
        for finding in findings
        if isinstance(finding, dict) and str(finding.get("verdict") or "").upper() == "FAIL"
    })


def extract_fail_findings(findings: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for finding in findings:
        if not isinstance(finding, dict) or str(finding.get("verdict") or "").upper() != "FAIL":
            continue
        entry = {
            "item": str(finding.get("item") or "?"),
            "severity": str(finding.get("severity") or ""),
            "reason_excerpt": format_obligation_excerpt(str(finding.get("reason") or "")),
        }
        if finding.get("model"):
            entry["model"] = str(finding["model"])
        out.append(entry)
    return out


def _ordinal(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def normalize_history(entries: List[Dict[str, Any]], skill_name: str) -> List[Dict[str, Any]]:
    """Add read-time ordinals to legacy rows without rewriting the audit log."""
    group_rounds: Dict[str, int] = {}
    snapshot_attempts: Dict[tuple[str, str], int] = {}
    last_hash: Dict[str, str] = {}
    out: List[Dict[str, Any]] = []
    for source in entries:
        entry = dict(source)
        group_id = str(entry.get("group_id") or f"manual:{skill_name}")
        content_hash = str(entry.get("content_hash") or "")
        review_round = max(
            group_rounds.get(group_id, 0) + 1,
            _ordinal(entry.get("review_round")),
        )
        group_rounds[group_id] = review_round
        attempt_key = (group_id, content_hash)
        snapshot_attempt = max(
            snapshot_attempts.get(attempt_key, 0) + 1,
            _ordinal(entry.get("snapshot_attempt")),
        )
        snapshot_attempts[attempt_key] = snapshot_attempt
        revised = bool(last_hash.get(group_id) and last_hash[group_id] != content_hash)
        if content_hash:
            last_hash[group_id] = content_hash
        entry.update(
            group_id=group_id,
            review_round=review_round,
            snapshot_attempt=snapshot_attempt,
            snapshot_revised=bool(entry.get("snapshot_revised", revised)),
        )
        out.append(entry)
    return out


def load_history(
    drive_root: pathlib.Path,
    skill_name: str,
    limit: int = 3,
    *,
    group_id: str = "",
) -> List[Dict[str, Any]]:
    try:
        entries = normalize_history(
            list(iter_jsonl_objects(review_history_path(drive_root, skill_name))),
            skill_name,
        )
    except OSError:
        return []
    if group_id:
        entries = [entry for entry in entries if entry.get("group_id") == group_id]
    return entries[-limit:] if limit > 0 else entries


def allocate_ordinals(
    drive_root: pathlib.Path,
    skill_name: str,
    group_id: str,
    content_hash: str,
) -> tuple[int, int, bool]:
    history = load_history(drive_root, skill_name, limit=0, group_id=group_id)
    review_round = max(
        (_ordinal(row.get("review_round")) for row in history), default=0,
    ) + 1
    snapshot_attempt = max(
        (
            _ordinal(row.get("snapshot_attempt"))
            for row in history
            if str(row.get("content_hash") or "") == content_hash
        ),
        default=0,
    ) + 1
    previous_hash = str(history[-1].get("content_hash") or "") if history else ""
    return review_round, snapshot_attempt, bool(previous_hash and previous_hash != content_hash)


def count_attempts(
    drive_root: pathlib.Path,
    skill_name: str,
    content_hash: str,
    *,
    group_id: str = "",
) -> int:
    history = load_history(drive_root, skill_name, limit=0, group_id=group_id)
    return sum(1 for row in history if str(row.get("content_hash") or "") == content_hash)


def append_history(
    drive_root: pathlib.Path,
    skill_name: str,
    *,
    status: str,
    content_hash: str,
    findings: List[Dict[str, Any]],
    raw_actor_records: Optional[List[Dict[str, Any]]] = None,
    single_reviewer_no_diversity: bool = False,
    paid: bool = False,
    review_contract_fingerprint: str = "",
    rebuttal_sha256: str = "",
    replayed_from_ts: str = "",
    wave_id: str = "",
) -> None:
    try:
        payload: Dict[str, Any] = {
            "ts": utc_now_iso(),
            "status": status,
            "content_hash": content_hash,
            "failure_signature": finding_signature(findings),
            "fail_findings": extract_fail_findings(findings),
        }
        if single_reviewer_no_diversity:
            payload["single_reviewer_no_diversity"] = True
        if raw_actor_records:
            payload["raw_actor_records"] = list(raw_actor_records)
        # Max-Review-Cycles facts (Q17/Q23): the paid-dispatch fact and the
        # panel contract identity ride the history row — counts and free-replay
        # decisions are DERIVED from this ledger (P7, no counter file).
        if paid:
            payload["paid"] = True
        if review_contract_fingerprint:
            payload["review_contract_fingerprint"] = str(review_contract_fingerprint)
        if rebuttal_sha256:
            payload["rebuttal_sha256"] = str(rebuttal_sha256)
        if replayed_from_ts:
            payload["replayed_from_ts"] = str(replayed_from_ts)
        if wave_id:
            payload["wave_id"] = str(wave_id)
        payload = _merge_dispatch_marker_facts(drive_root, skill_name, payload)
        if not append_jsonl(
            review_history_path(drive_root, skill_name),
            _redact_history_payload(payload),
        ):
            raise OSError("append_jsonl reported failure")
        if wave_id:
            clear_dispatch_marker(drive_root, skill_name, wave_id=wave_id)
    except Exception:
        # LOUD failure (F3): a lost terminal row silently un-counts spent
        # money and hides a verdict — log at warning and emit the typed event.
        log.warning("skill review history append failed for %s", skill_name, exc_info=True)
        _emit_history_event(drive_root, {
            "type": "skill_review_history_append_failed",
            "skill": skill_name,
            "status": str(status or ""),
            "content_hash": str(content_hash or ""),
            "wave_id": str(wave_id or ""),
            "reason": "direct history append failed",
        })


def append_history_once(
    drive_root: pathlib.Path,
    skill_name: str,
    payload: Dict[str, Any],
) -> bool:
    """Append one lifecycle terminal row, idempotently keyed by ``job_id``."""
    job_id = str(payload.get("job_id") or "")
    if not job_id:
        return False
    path = review_history_path(drive_root, skill_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = jsonl_append_lock_path(path)
    lock_fd = acquire_exclusive_file_lock(lock_path, timeout_sec=2.0, stale_sec=10.0)
    if lock_fd is None:
        return False
    try:
        try:
            if any(str(row.get("job_id") or "") == job_id for row in iter_jsonl_objects(path)):
                # Already landed (idempotent retry): finish the merge by
                # clearing this wave's dispatch marker if it is still present.
                clear_dispatch_marker(drive_root, skill_name, wave_id=job_id)
                return True
            payload = _merge_dispatch_marker_facts(drive_root, skill_name, payload)
            safe_payload = _redact_history_payload(payload)
            data = (json.dumps(safe_payload, ensure_ascii=False) + "\n").encode("utf-8")
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            clear_dispatch_marker(
                drive_root, skill_name,
                wave_id=str(payload.get("wave_id") or job_id),
            )
            return True
        except OSError:
            log.warning("skill review terminal history append failed for %s", skill_name, exc_info=True)
            return False
    finally:
        release_exclusive_file_lock(lock_path, lock_fd)
