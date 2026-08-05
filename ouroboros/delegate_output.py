"""Staged output for delegated runs, and the durable receipt that it was READ.

Extracted from ``ouroboros/tools/delegate.py`` when that module crossed its size gate:
this is one coherent concern with no dependency on authority, containment or transport
— a terminal payload too large for the tool budget is written whole to the task drive,
and D7's acknowledgement records that the model actually received every character of
it. ``tools.delegate`` re-exports these names, so every existing reference (and the
convergence census) still finds them there.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib
from typing import Any, Dict, List, Optional

from ouroboros import delegate_custody as custody
from ouroboros.delegate_custody import RunCustody as _RunCustody
from ouroboros.tool_capabilities import tool_result_limit
from ouroboros.tools.registry import ToolContext

log = logging.getLogger(__name__)

# Where the full terminal detail is staged under the task's own drive, to be read back
# with the ordinary read_file contract.
_ARTIFACT_SUBDIR = "delegated_runs"


def _safe_run_filename(run_id: str) -> str:
    """A filesystem-safe name for a daemon-supplied run id (never trusted verbatim)."""
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(run_id or "")).strip(".")[:80]
    return cleaned or hashlib.sha256(str(run_id or "").encode("utf-8", "replace")).hexdigest()[:16]


def _stage_full_output(ctx: ToolContext, run_id: str, text: str) -> Optional[Dict[str, Any]]:
    """Write the WHOLE terminal detail to the task's own drive and describe it.

    Host-owned by construction: the host writes it, and every profile that can call a
    nanny verb has task_drive as READ-only or better, so the artifact is reachable
    through the ordinary ``read_file`` contract (which already carries a stable
    ``start_line`` cursor) instead of a parallel artifact system.
    """
    from ouroboros.tool_access import resource_root_path

    # The bytes DECLARED here and the bytes WRITTEN must be one object: the sha256 below
    # becomes the artifact's identity (`custody.output_sha`), and the read receipt measures
    # the file with `read_bytes`. A text write translates "\n" to os.linesep, so on Windows
    # the declared hash described a file that never existed on disk and the D7
    # acknowledgement could never be recorded for any staged output.
    staged_bytes = text.encode("utf-8", "replace")
    try:
        base = resource_root_path(ctx, "task_drive") / _ARTIFACT_SUBDIR
        base.mkdir(parents=True, exist_ok=True)
        target = base / f"{_safe_run_filename(run_id)}.json"
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_bytes(staged_bytes)
        tmp.replace(target)
    except Exception:
        log.warning("Failed to stage delegated run output for %s", run_id, exc_info=True)
        return None
    return {
        "root": "task_drive",
        "path": f"{_ARTIFACT_SUBDIR}/{target.name}",
        "abs_path": str(target),
        "chars": len(text),
        "lines": text.count("\n") + 1,
        "bytes": len(staged_bytes),
        "sha256": hashlib.sha256(staged_bytes).hexdigest(),
    }


# Proven-coverage ledger for staged artifacts: merged, sorted [start, end] line
# intervals actually SERVED to the reader, keyed by path plus CONTENT hash — a re-wait
# re-stages the identical payload and must not void an honest reader's partial proof,
# while changed content honestly resets it. Process-local by design: the DURABLE fact
# is the acknowledgement row itself; a restarted worker re-proves coverage by
# re-reading, it never inherits an unproven claim.
_READ_COVERAGE: Dict[str, List[List[int]]] = {}
_READ_COVERAGE_MAX_KEYS = 128


def _covered_whole(key: str, start: int, end: int, total: int) -> bool:
    """Merge one DELIVERED char range [start, end) into the ledger; True when
    [0, total) is covered.

    CONTINUOUS coverage of what was actually DELIVERED, not a reached cursor and not
    source-file line ranges: a head plus a tail with a skipped middle must never read
    as "read to EOF", and neither may a window the delivery layer cut — the model
    received the prefix the truncator kept, nothing more. Ranges may arrive in any
    order; only an unbroken union over every character is full reading.
    """
    if total <= 0:
        return True
    if key not in _READ_COVERAGE and len(_READ_COVERAGE) >= _READ_COVERAGE_MAX_KEYS:
        _READ_COVERAGE.pop(next(iter(_READ_COVERAGE)))
    intervals = _READ_COVERAGE.setdefault(key, [])
    if start < end:
        intervals.append([start, end])
        intervals.sort()
        merged: List[List[int]] = []
        for lo, hi in intervals:
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        _READ_COVERAGE[key] = merged
        intervals = merged
    return bool(intervals) and intervals[0][0] <= 0 and intervals[0][1] >= total


def acknowledge_staged_output_read(ctx: ToolContext, target: Any, content: str,
                                   start_line: Any, max_lines: Any,
                                   start_char: Any = 0, rendered: str = "") -> None:
    """D7's canonical acknowledgement, hooked where the reading actually happens.

    Called by ``read_file`` for every successful ``task_drive`` read, WITH the rendered
    view it is about to return. Coverage is credited in CHARACTERS OF THE FILE and
    bound to what the delivery layer will actually hand the model: the outer truncator
    cuts the result at ``tool_result_limit("read_file")``, so a window whose rendering
    exceeds that budget is credited only for the prefix that survives the cut — a line
    the delivery layer cut is NOT covered, whatever the line range said. (The reader
    reaches the cut-off remainder through ``start_char``, the sub-line cursor.) The
    durable ``delegate_run_output_consumed`` row is written (once per run) exactly when
    the delivered ranges have covered every character, contiguously. It carries the
    byte length and content hash of what was staged, measured from the bytes on disk,
    which ARE what was staged because the file is written atomically and never
    appended. An artifact staged from an UNVERIFIED engine preview is never
    acknowledgeable at all (``record_output_consumed`` refuses it).

    Disclosure, not a gate: this function never blocks a read, never raises, and its
    absence blocks nothing. It exists so "result received" and "result fully read" are
    different durable facts, instead of relying on ledger discipline to notice a
    delegated run that was launched and never collected.
    """
    try:
        from ouroboros.tool_access import resource_root_path
        from ouroboros.tools.core import _coerce_line_window, _coerce_start_char

        path = pathlib.Path(target)
        artifact_dir = (resource_root_path(ctx, "task_drive") / _ARTIFACT_SUBDIR).resolve(strict=False)
        resolved = path.resolve(strict=False)
        if resolved.parent != artifact_dir:
            return
        # The same window math as the read tool's renderer — a served window here must
        # mean exactly what the reader was shown.
        start_raw, max_raw = _coerce_line_window(start_line, max_lines)
        max_raw = max(1, max_raw)
        lines = content.splitlines(keepends=True)
        total_lines = len(lines)
        start = max(1, min(start_raw, total_lines + 1))
        end = min(start + max_raw - 1, total_lines)
        offset = _coerce_start_char(start_char)
        # Char range of the rendered BODY within the file, and the part of it the
        # OUTER truncator will actually deliver (same budget, same head-cut rule).
        window_start = sum(len(line) for line in lines[:start - 1])
        body_full = max(0, sum(len(line) for line in lines[start - 1:end]) - offset)
        header_len = len(rendered) - body_full if rendered else 0
        budget = tool_result_limit("read_file")
        if rendered and len(rendered) > budget:
            delivered_body = max(0, budget - header_len)
        else:
            delivered_body = body_full
        abs_start = window_start + offset
        abs_end = abs_start + min(body_full, delivered_body)
        total = len(content)
        identity = f"{resolved}|{hashlib.sha256(content.encode('utf-8', 'replace')).hexdigest()}"
        if not _covered_whole(identity, abs_start, abs_end, total):
            return                       # served, recorded, but not yet read WHOLE
        task_id = str(getattr(ctx, "task_id", "") or "")
        wanted = path.name
        drive = custody.custody_root(ctx)

        def _match(candidates: Any) -> Optional[_RunCustody]:
            return next((c for c in candidates
                         if c.task_id == task_id
                         and _safe_run_filename(c.run_id) + ".json" == wanted), None)

        # Memo first; a restarted worker (empty or unrelated memo) falls through to the
        # durable replay, the same authority every other custody question consults.
        entry = _match(list(custody._CUSTODY.values())) or _match(custody.replay(drive).values())
        if entry is None or entry.output_consumed:
            return
        raw = path.read_bytes()
        custody.record_output_consumed(
            drive, entry,
            artifact=f"{_ARTIFACT_SUBDIR}/{wanted}",
            byte_length=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            chars=len(content),
            lines=total_lines,
        )
    except Exception:
        log.warning("coverage acknowledgement for a staged delegated output failed", exc_info=True)


