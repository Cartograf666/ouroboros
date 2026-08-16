"""Rendering for the plan-review engine: the wave view, the next-step guidance and the
one host-owned control line. Split from ``plan_review.py`` so the engine stays under the
size target; no behaviour lives here that the engine does not dictate."""

from __future__ import annotations

import json
from typing import List, Optional

from ouroboros.loop_tool_execution import PLAN_REVIEW_CONTROL_PREFIX


_CONTROL_OUTCOME = {
    "GREEN": "GREEN", "REVIEW_REQUIRED": "REVIEW_REQUIRED",
    "REVISE_PLAN": "REVISE_PLAN", "DEGRADED": "REVIEW_REQUIRED",
}


def _quote_control_lines(text: str) -> str:
    """Keep reviewer/agent text visible without impersonating the host control footer."""
    return "".join(
        "> " + line if line.startswith(PLAN_REVIEW_CONTROL_PREFIX) else line
        for line in str(text or "").splitlines(keepends=True)
    )



def _next_step(wave: dict, *, enforcement: str, cap: Optional[int], cycles_paid: int) -> str:
    aggregate = str(wave.get("aggregate") or "")
    fp = str(wave.get("request_fingerprint") or "")
    at_cap = cap is not None and cycles_paid >= cap
    if bool(wave.get("closed")):
        return "Closed: proceed with the reviewed spec."
    if aggregate == "DEGRADED":
        return (
            "DEGRADED: no parseable quorum — re-call plan_task with the same envelope to re-run "
            "the panel; a degraded wave consumes NO cycle."
        )
    if aggregate == "REVIEW_REQUIRED":
        blocking = [f for f in wave.get("findings") or [] if f.get("class") == "blocking"]
        text = (
            "Disposition every finding id (accept | reject | defer, each with a rationale) in ONE "
            f"call: plan_task(review_disposition={{review_fingerprint: '{fp}', items: [...]}}) — no "
            "reviewer call, no cycle. "
        )
        if blocking:
            ids = ", ".join(str(f.get("finding_id") or f.get("id")) for f in blocking[:4])
            text += (
                f"NOTE: {len(blocking)} BLOCKING finding(s) below quorum ({ids}) stay OPEN whatever "
                "you disposition — a blocking finding closes only through a changed spec "
                "(new fingerprint, next paid cycle) or a reject that the next paid delta cycle judges. "
            )
    else:
        text = (
            "Blocking findings: accept ⇒ change the spec and re-call plan_task (new fingerprint, "
            f"{'the cap is reached — no further paid cycle' if at_cap else 'next paid cycle ' + str(cycles_paid + 1) + ('' if cap is None else f' of {cap}')}); "
            "reject ⇒ record reject + rationale via review_disposition naming this fingerprint — it "
            "rides into the next paid delta cycle where reviewers mark it resolved or still-open. "
            "A disposition never closes REVISE_PLAN. "
        )
    if enforcement == "blocking":
        text += (
            "Blocking enforcement: the review must close before the work starts"
            + (" — the cycle cap is reached: exits are owner unstick (Swarm/hurry) or finalizing "
               "with outcome_tier=blocked_with_evidence." if at_cap else ".")
        )
    else:
        text += (
            "Advisory enforcement: you may proceed with the review OPEN; the host discloses "
            "that loudly in the task result."
        )
    return text



def _render_wave(
    wave: dict, *, cap: Optional[int], cycles_paid: int, enforcement: str,
    cached: bool = False, notes: Optional[List[str]] = None, reminder: str = "",
) -> str:
    aggregate = str(wave.get("aggregate") or "")
    closed = bool(wave.get("closed"))
    counts = wave.get("counts") if isinstance(wave.get("counts"), dict) else {}
    manifest = wave.get("evidence_manifest") if isinstance(wave.get("evidence_manifest"), dict) else {}
    lines = [
        f"## Plan Review — cycle {wave.get('cycle_index')} · paid cycles {cycles_paid}"
        + ("" if cap is None else f"/{cap}") + f" · enforcement {enforcement}",
        "",
        f"**Plan fingerprint:** `{wave.get('request_fingerprint') or ''}`"
        + ("  (cached exact review — no reviewer was called)" if cached else ""),
        f"**Constitutional:** {'yes' if wave.get('constitutional') else 'no'} — {wave.get('constitutional_note') or ''}",
        f"**Evidence:** {len(manifest.get('attached') or [])} attached; omissions: "
        + (", ".join(f"{o.get('locator')}: {o.get('reason')}" for o in manifest.get("omissions") or []) or "none"),
    ]
    if wave.get("compact"):
        lines += ["", "(bounded history: this wave is kept as a compact summary; its full findings are no longer recorded)"]
    if reminder:
        lines += ["", "⚠️ " + reminder]
    if aggregate == "DEGRADED":
        lines += ["", "⚠️ DEGRADED: no parseable quorum — re-call to re-run the panel; a degraded wave consumes NO cycle."]
    actor_lines = [
        f"- {a.get('slot_id')} · {a.get('model')} · {a.get('route')} · host_file_read: "
        f"{a.get('host_file_read_attestation')} · {'ok' if a.get('ok') else 'FAILED: ' + str(a.get('error'))}"
        + (f" · disclosures: {', '.join(a['disclosures'])}" if a.get("disclosures") else "")
        for a in wave.get("actors") or []
    ] or ["(no actor records)"]
    lines += [
        "", "### Reviewer slots", "", *actor_lines,
        "", "### Findings (per slot; finding_id = slot:id)", "", "```json",
        json.dumps(wave.get("findings") or [], ensure_ascii=False, indent=2, default=str), "```",
    ]
    previews = [a for a in wave.get("actors") or [] if a.get("raw_text_preview")]
    if previews:
        lines += ["", "### Unparseable reviewer output (bounded preview)", ""]
        for actor in previews:
            lines += [f"#### {actor.get('slot_id')}", _quote_control_lines(str(actor.get("raw_text_preview"))), ""]
    lines += [
        "", f"### Aggregate: {aggregate}" + (" (closed)" if closed else " (open)"),
        "", "Reasons: " + (", ".join(str(r) for r in wave.get("reasons") or []) or "none")
        + f". Counts: {json.dumps(counts, sort_keys=True)}",
    ]
    if wave.get("dispositions"):
        lines += ["", "### Dispositions", "", "```json",
                  json.dumps(wave.get("dispositions"), ensure_ascii=False, indent=2), "```"]
    if wave.get("closure_notes") or notes:
        lines += ["", "Closure notes: " + "; ".join([*(wave.get("closure_notes") or []), *(notes or [])])]
    outcome = _CONTROL_OUTCOME.get(aggregate, "REVIEW_REQUIRED")
    lines += [
        "", "## Plan Review Contract", "",
        _next_step(wave, enforcement=enforcement, cap=cap, cycles_paid=cycles_paid), "",
        PLAN_REVIEW_CONTROL_PREFIX + json.dumps({"outcome": outcome, "closed": closed}, separators=(",", ":")),
    ]
    return "\n".join(lines)
