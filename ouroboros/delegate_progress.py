"""What a delegated run did DURING one ``delegate_wait`` window, and how it reaches
the human while the model is still waiting.

Extracted from ``ouroboros/tools/delegate.py`` for the same reason ``delegate_output``
was (size gate), and it is one coherent concern: the wait used to hand control back to
the model on the FIRST journal advance, so a healthy streaming run woke a full-context
nanny round every poll interval — measured, 18 rounds and 861k prompt tokens spent
narrating a run that was doing fine. The fix is the TIMER, not the stream: every
advance is still observed and still pushed to the live progress surface the instant it
is seen, but it is RECORDED here instead of returned, and the model is woken once, at
the window's expiry, carrying the whole sequence.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ouroboros.utils import truncate_review_artifact

log = logging.getLogger(__name__)

_TIMELINE_TAIL = 12
_TIMELINE_LABEL_CHARS = 300
# The closing `note` is appended AFTER the advance list is sized, so its cost is
# reserved rather than measured; it is a fixed string of this module's own authorship.
_NOTE_RESERVE_CHARS = 700
# The floor the advance list ASKS for when the rest of the payload has eaten the budget:
# one advance plus its marker still tells the model the run is moving. It is a floor on
# the request, never on the result — a residual too small to hold it drops the list to
# its marker rather than shipping the floor over the limit (see `window_payload`).
_MIN_ADVANCE_BUDGET_CHARS = 400
# Re-measure passes for the fit loop: the first correction is exact for a uniform list,
# the rest absorb the per-row indent rounding. A payload still over after these is one
# whose harness-authored parts alone overflow, which only the truncator can answer.
_BUDGET_FIT_ATTEMPTS = 4


def _rows(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [row for row in (detail.get("timeline") or []) if isinstance(row, dict)]


def _bounded(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The last few session events, each label bounded.

    The same delivery class as the terminal payload, one surface over: a harness-supplied
    title is unbounded, and twelve long ones push the progress payload past the tool-result
    cap, where head-truncation severs the JSON. Bounded through the shared disclosed
    contract (OMISSION NOTE + original length, with its anti-waste floor) rather than a
    hand-rolled slice+ellipsis — DEVELOPMENT.md forbids new hand-rolled sites (P34R.5).

    A DISPLAY tail: it drops rows off the head and says nothing about them, which is the
    right shape for the STANDING timeline (whose head is old news the model has seen) and
    the wrong shape on its own for a BATCH (whose head is news that arrived one poll ago).
    A caller sizing a batch with it owes the reader the count — see `record`.
    """
    def _label(value: Any) -> Any:
        return truncate_review_artifact(value, _TIMELINE_LABEL_CHARS) \
            if isinstance(value, str) else value

    return [
        {"type": _label(row.get("type")), "title": _label(row.get("title")),
         "severity": _label(row.get("severity"))}
        for row in rows[-_TIMELINE_TAIL:]
    ]


def timeline_tail(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    return _bounded(_rows(detail))


def _omission_marker(through_seq: int, count: int, *, whole: bool = False) -> Dict[str, Any]:
    """The row that stands where dropped advances were.

    ONE vocabulary for both places a drop can happen — the head shedding inside the list,
    and the whole list yielding when the rest of the payload leaves no room for even the
    floor — so a reader learns the same fact from the same keys either way.

    What the note may CLAIM is bounded by what is actually recoverable. It used to say
    every omitted advance "was streamed live and is in the event log", and both halves
    were wrong: a batch bigger than the display tail never reached the live line at all
    (the defect `record` now counts as `events_omitted`), and nothing on this side keeps
    the rows — Ouroboros persists no timeline anywhere, the daemon's own run directory
    does. So the note says where they really are.
    """
    return {
        "advances_omitted": count,
        "omitted_through_seq": through_seq,
        "note": ("every advance of this window is" if whole else
                 "older advances of this window are")
        + " omitted from THIS payload only; the rows live on the RUN, in Claudexor's own "
          "timeline — poll it again for the standing tail, nothing here keeps a copy",
    }


@dataclass
class _Advance:
    """One movement of the run's journal cursor, and what arrived with it."""

    seq: int
    at_sec: int
    events: List[Dict[str, Any]]
    # What the batch published BEYOND the display tail `record` sizes it with. Folded
    # into the same `events_omitted` the budget shedding writes, so a reader gets ONE
    # honest number per advance instead of two vocabularies for the same kind of cut.
    events_omitted: int = 0


@dataclass
class WindowObservations:
    """Every advance seen inside ONE wait window, in order."""

    advances: List[_Advance] = field(default_factory=list)
    # The last timeline this window actually saw, kept whole: the only thing that can
    # say which rows of the NEXT one are new. See `_fresh`.
    _prev: List[Dict[str, Any]] = field(default_factory=list)

    def observe_baseline(self, detail: Dict[str, Any], seq: int) -> None:
        """Adopt what was ALREADY on the timeline as history, not as this window's news.

        Without it the first real advance re-announced the whole standing tail as "new
        session events" — to the human's live stream and to the model alike — because the
        comparison in `_fresh` started from nothing while the run had been talking for a
        while.

        Only for a caller that is CAUGHT UP, though. `seq` is where the caller says it has
        read to, and one BEHIND the daemon's cursor (a `since_seq` under `lastSeq`) is
        asking for exactly the rows already standing there; adopting them would drop the
        very batch it called for. Rows carry no cursor of their own, so the choice is
        between the whole standing tail and none of it, and only a caught-up caller can
        be told nothing.
        """
        if int(seq or 0) >= int(detail.get("lastSeq") or 0):
            self._prev = _rows(detail)

    def record(self, detail: Dict[str, Any], seq: int, at_sec: int) -> _Advance:
        """Observe ONE journal advance, and return what arrived with it.

        The batch is still BOUNDED — an unbounded one is not free, a busy daemon can
        publish hundreds of rows between two three-second polls — but by a bound that
        COUNTS what it cut. It used to cut in silence, with the display tail written for
        the standing timeline: measured against a harness emitting sixteen rows per
        cursor step, the first four of every batch were gone from the live line AND from
        `advances`, with no `events_omitted` and no other key naming them. The count
        rides the advance to both surfaces (`rows`, `live_line`).
        """
        rows = _rows(detail)
        fresh = self._fresh(rows)
        advance = _Advance(seq=seq, at_sec=at_sec, events=_bounded(fresh),
                           events_omitted=max(0, len(fresh) - _TIMELINE_TAIL))
        if rows:
            self._prev = rows
        self.advances.append(advance)
        return advance

    def _fresh(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The rows of this timeline that the previous observation did not already hold.

        Read off the DATA, because nothing the daemon reports says how big a batch is.
        Not the CURSOR: it moves by one per FOUR rows against a harness that emits four
        timeline rows per `lastSeq` step (reading the batch off it there dropped three of
        every four rows from the live stream AND from the record, silently, which is the
        one failure this path exists to prevent), and by MORE than the rows added when
        journal entries never become timeline rows (reading the batch off it there
        re-emitted rows already reported as new). Not the LENGTH either, once the
        timeline is bounded and rolling and its length stops growing.

        So the tails are compared: the longest overlap between the END of the previous
        one and the START of this one is what survived the roll, and everything past it
        is new. A pure append overlaps at the previous tail's whole length, which is the
        answer `rows[seen:]` gave while that held; a timeline replaced outright overlaps
        nowhere, and then all of it is new. Rows compare by value — identical rows are
        indistinguishable by construction, so a roll that shifts an unchanging row out
        and an identical one in reads as no news, which is also all it is.
        """
        if not rows:
            return []          # nothing observed; the last real tail stays the reference
        for overlap in range(min(len(self._prev), len(rows)), 0, -1):
            if self._prev[-overlap:] == rows[:overlap]:
                return rows[overlap:]
        return rows

    def rows(self, budget: int) -> List[Dict[str, Any]]:
        """The advance sequence, bounded to ``budget`` chars of JSON.

        A LIST, never a count: there is one row per observed advance carrying its `seq`
        and `at_sec`, and under pressure rows shed their event LABELS oldest-first with
        the shedding disclosed ON the row. Only if the bare spine still overflows does
        the HEAD shed, and it says so — a truncation that pretended the run started later
        than it did is the one shape forbidden here.

        `events_omitted` is ONE number per row and covers BOTH cuts a row can carry: the
        labels this payload shed for budget, and the rows the batch itself lost to the
        display tail when it was observed (`record`). They differ in what a reader can go
        find — a shed label reached the human's live line at observation time, a batch-cut
        row never did — and NEITHER is recoverable on this side: no timeline is persisted
        here, only the run's own timeline in Claudexor's run directory holds the rows. So
        the count is stated rather than the loss being waved away as immaterial.
        """
        out: List[Dict[str, Any]] = []
        for a in self.advances:
            row: Dict[str, Any] = {"seq": a.seq, "at_sec": a.at_sec, "events": list(a.events)}
            if a.events_omitted:
                row["events_omitted"] = a.events_omitted
            out.append(row)

        def _size(rows: List[Dict[str, Any]]) -> int:
            # MEASURED, never estimated, and rendered the way the caller renders it
            # (indent=2): an estimate that assumed a fixed cost per row overflowed the
            # tool-result limit on a long busy window, and the generic truncator then
            # cut the JSON mid-structure — the model got a payload it could not parse
            # at all, which is worse than any amount of honest shedding.
            return len(json.dumps(rows, ensure_ascii=False, indent=2))

        # Label shedding, oldest-first. The running total is kept by SUBTRACTING what
        # each row actually gave up (measured on that row, not assumed), and a real
        # whole-list measure confirms before returning — the arithmetic only decides
        # when to stop shedding, never what the payload is.
        running = _size(out)
        for row in out:
            if running <= budget and _size(out) <= budget:
                return out
            if not row["events"]:
                continue
            before = len(json.dumps(row, ensure_ascii=False, indent=2))
            # ACCUMULATED, never overwritten: a row whose batch was already cut at
            # observation would otherwise report only what THIS payload shed and quietly
            # forget the rest — two cuts, one of them invisible again.
            row["events_omitted"] = int(row.get("events_omitted") or 0) + len(row["events"])
            row["events"] = []
            running -= before - len(json.dumps(row, ensure_ascii=False, indent=2))
        if _size(out) <= budget:
            return out
        # The bare spine still overflows: shed from the HEAD. Shedding one row per
        # re-measure is O(n²) and cost ~7s of CPU at a full 1800s window (600 advances
        # at the 3s poll), so the drop count is ESTIMATED from the measured average row
        # and then corrected by measurement — the estimate only picks where to start,
        # the loop below still decides on the real rendered size.
        spine, kept = out, list(out)
        over = _size(kept) - budget
        if over > 0 and kept:
            per_row = max(1, _size(kept) // len(kept))
            guess = min(len(kept) - 1, max(1, over // per_row + 1))
            kept = spine[guess:]
        while kept and _size([_omission_marker(spine[len(spine) - len(kept) - 1]["seq"],
                                              len(spine) - len(kept))] + kept) > budget:
            kept = kept[1:]
        if len(kept) == len(spine):
            return out
        dropped = len(spine) - len(kept)
        if not kept:                      # even one row plus its marker overflows
            kept, dropped = spine[-1:], len(spine) - 1
        return [_omission_marker(spine[dropped - 1]["seq"], dropped)] + kept


def poll_bound(seconds_left: float) -> float:
    """What one call inside a clamped window may ASK the transport for.

    NARROWING in both directions, which is the only thing a bound is for. Never more
    than the window has left — that is the whole point — and never more than the read
    default the client would have used anyway: floored alone, the arithmetic RAISED the
    ask above that default whenever the window had over a minute left (measured: 1797.0,
    1000.0, 61.0), so a hung daemon stopped failing at sixty seconds and held the whole
    window instead, to be reported afterwards as a wait that simply saw nothing. And
    never less than the floor a bound is useful at: a nearly spent window that asked for
    its own 0.2s would turn every daemon into a timeout, and the honest answer there is
    one short read, then expiry.

    ONE place computes it, for both entry points below, so the two cannot drift.
    """
    from ouroboros.gateways.claudexor import _READ_TIMEOUT_SEC, SHORT_POLL_TIMEOUT_SEC

    return min(_READ_TIMEOUT_SEC, max(SHORT_POLL_TIMEOUT_SEC, float(seconds_left)))


def is_transient_git_object_race(exc: Exception) -> bool:
    """Recognize ONLY Git's disappearing atomic-object scratch path.

    The engine's run reader loses a race against Git renaming ``.git/objects/…/
    tmp_obj_*`` into place and answers one poll with an ENOENT naming that scratch
    path — a fact about a file that existed a moment ago and exists again a moment
    later. The CI platform gate learned to retry exactly this shape (938094a9) while
    the production poll kept propagating it, so CI could pass on an engine whose
    live ``delegate_wait`` still failed. The matcher is deliberately this narrow:
    any other ENOENT, or the same errno on any other path, stays a real failure.
    """
    detail = str(exc).replace("\\", "/").lower()
    code = str(getattr(exc, "code", "") or "").upper()
    return (
        (code == "ENOENT" or "enoent" in detail)
        and "/.git/objects/" in detail
        and "/tmp_obj_" in detail
    )


def bounded_poll(gateway: Any, run_id: str, seconds_left: float) -> Dict[str, Any]:
    """One poll that may not ask for longer than the window has left.

    The client's read default is minutes-scale, which is right for a call with all the
    time it needs and wrong for one inside a clamped window: a poll started a moment
    before expiry could answer a minute after it, so the clamp bounded the sleeping and
    not the waiting (measured, 4.51s of wall against a 4.2s window — and that was a fast
    daemon). Bounded here, the wait cannot outlive its window through the transport.

    A failure PROPAGATES, as the typed refusal the gateway already raised. There is time
    left on the window here, so there is no expiry to report and nothing true to say
    about a daemon that has stopped answering: swallowing it handed the model a
    completed, uneventful wait of a duration nobody waited (measured — a
    ``daemon_unreachable`` three seconds into a 1800s window came back as
    ``status: progress``, ``waited_sec: 1800``, "The run advanced 1 time(s) during the
    1800s you asked to wait", after 3.0s of wall; before any advance, a 503 came back as
    ``no_progress`` over a full window with a note inviting a `delegate_cancel` of a live
    run because the transport had blipped). Only the LAST poll may expire instead.

    One exception to the propagation rule, matched as narrowly as it occurs: the
    engine's transient Git atomic-object race (see ``is_transient_git_object_race``)
    gets ONE immediate re-read while the window still has time — it is a lie about a
    file mid-rename, not a daemon that stopped answering.
    """
    try:
        return gateway.get_run(run_id, timeout_sec=poll_bound(seconds_left))
    except Exception as exc:
        if seconds_left > 0 and is_transient_git_object_race(exc):
            log.debug("transient Git object race on poll of %s; one re-read", run_id)
            return gateway.get_run(run_id, timeout_sec=poll_bound(seconds_left))
        raise


def expiring_poll(gateway: Any, run_id: str) -> Optional[Dict[str, Any]]:
    """The poll of a SPENT window. ``None`` when the daemon did not answer inside it.

    A spent window still takes its poll rather than skipping it, at the floor: skipping
    looked like the cheap trade and cost more, because terminal state and containment
    breach were then judged on data read BEFORE the last sleep — a run that finished
    during that sleep came back as ``progress``/``running`` with no settlement, and the
    model paid another full-context round for a run already done.

    This is the ONE poll whose silence is not a failure, and the reason it is a separate
    entry point: the window is already over, so a daemon too slow to answer inside the
    bound is this window's EXPIRY, which the caller knows how to report, and never a
    transport failure raised out of a tool holding a live overpowered run.
    """
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    try:
        return bounded_poll(gateway, run_id, 0.0)
    except ClaudexorUnavailable:
        log.debug("the last poll of a spent delegate_wait window went unanswered", exc_info=True)
        return None


def rendered_window(**kwargs: Any) -> str:
    """``window_payload`` rendered the way the wait returns it (and measures it)."""
    return json.dumps(window_payload(**kwargs), ensure_ascii=False, indent=2)


def live_line(run_id: str, advance: _Advance) -> str:
    titles = " · ".join(
        str(row.get("title") or row.get("type") or "") for row in advance.events
    )
    # A batch larger than the display tail shows its LAST rows. Saying how many it is not
    # showing is the difference between an honest subset and a line that reads like the
    # whole batch — the human's stream is the one surface that had no marker at all. It
    # says where they are, not that they are somewhere: these rows never reached this
    # line, and nothing on this side stores them — the run's own timeline does.
    more = (f" (+{advance.events_omitted} earlier in this batch, on the run's timeline)"
            if advance.events_omitted else "")
    return f"🛰 delegated run {run_id} @seq {advance.seq}: {titles or '(new session events)'}{more}"


def emit(ctx: Any, run_id: str, advance: _Advance) -> None:
    """Push one advance to the LIVE progress surface, from inside the wait.

    `ctx.emit_progress_fn` is immediate (it puts the frame straight on the event queue),
    unlike `ctx.pending_events`, which is drained only when the task ends — a whole
    window's narration held until the nanny's turn was over is the collapse this fix
    exists to avoid. The frame is also what stamps the supervisor's `last_progress_at`,
    which a silently blocking wait would otherwise starve. A broken progress channel
    must never abort a wait that is holding a live overpowered run.
    """
    fn = getattr(ctx, "emit_progress_fn", None)
    if not callable(fn):
        return
    try:
        fn(live_line(run_id, advance))
    except Exception:
        log.debug("delegated progress emit failed", exc_info=True)


def window_payload(
    *,
    run_id: str,
    state: str,
    last_seq: int,
    window: int,
    elapsed_seconds: Optional[int],
    max_seconds: Optional[int],
    waiting_on_user: bool,
    detail: Dict[str, Any],
    seen: WindowObservations,
    budget: int,
) -> Dict[str, Any]:
    """What the model is handed when the window expires without a terminal state."""
    payload: Dict[str, Any] = {
        "status": "progress" if seen.advances else "no_progress",
        "run_id": run_id,
        "state": state,
        "last_seq": last_seq,
        "waited_sec": window,
        "elapsed_seconds": elapsed_seconds,
        "max_seconds": max_seconds,
        "waiting_on_user": waiting_on_user,
    }
    if not seen.advances:
        payload["reason"] = "non_terminal_and_no_new_session_events_within_wait_window"
        payload["note"] = ("The run is alive but silent. Decide: keep waiting (call again), "
                           "or delegate_cancel if it is stuck.")
        return payload
    payload["timeline_tail"] = timeline_tail(detail)
    # The advance list gets what is LEFT, measured — not a fixed share. A fixed share
    # bounded only itself: `timeline_tail` carries harness-authored text (three fields,
    # each capped at 300 chars, twelve rows) and can reach most of the limit on its own,
    # so the whole result overflowed and the generic truncator cut the JSON mid-structure
    # — the very failure this list is bounded to avoid, and one the base did not have.
    # `note` is added after this line, so its own cost is reserved here too. The list is
    # then sized against the WHOLE rendered payload rather than against itself: nested a
    # level deeper it costs more than it measures standalone (two spaces of indent per
    # line, plus its key), so a sub-block that fits its share can still overflow the
    # result. The loop re-measures what is actually shipped and hands back the overflow.
    _room = max(_MIN_ADVANCE_BUDGET_CHARS,
                budget - len(json.dumps(payload, ensure_ascii=False, indent=2))
                - _NOTE_RESERVE_CHARS)
    for _ in range(_BUDGET_FIT_ATTEMPTS):
        payload["advances"] = seen.rows(_room)
        _over = len(json.dumps(payload, ensure_ascii=False, indent=2)) + _NOTE_RESERVE_CHARS - budget
        if _over <= 0:
            break
        if _room <= _MIN_ADVANCE_BUDGET_CHARS:
            # The residual cannot hold even the FLOOR, and the floor is what overflowed:
            # measured, twelve tail rows of three 20 000-char harness fields leave ~400
            # chars once the `note` is reserved, and a floor-sized list shipped 422 on
            # top — a 15 018-char result against a 15 000 limit, which the generic
            # truncator then cut mid-structure. That is the round-1 defect from the other
            # end, and it is this module's own floor that caused it, not the harness text.
            # So the floor is an aspiration and the MEASUREMENT is the rule: the list
            # yields entirely rather than the payload crossing the limit, and it says so
            # where it stood, in the same vocabulary a head-shed uses.
            payload["advances"] = [_omission_marker(
                seen.advances[-1].seq, len(seen.advances), whole=True)]
            break
        _room = max(_MIN_ADVANCE_BUDGET_CHARS, _room - _over)
    payload["quiet_for_sec"] = max(0, window - seen.advances[-1].at_sec)
    payload["note"] = (
        f"The run advanced {len(seen.advances)} time(s) during the {window}s you asked to "
        "wait; each advance was offered to your human's live stream as it happened "
        "(delivery is best-effort) — this call held the window instead of waking you "
        "per event. `advances` is that sequence. "
        "Call delegate_wait again with since_seq=last_seq to keep watching; "
        "`quiet_for_sec` is how long it has been silent at the end of the window."
    )
    return payload
