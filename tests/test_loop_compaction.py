from types import SimpleNamespace


def _messages(count=41):
    return [{"role": "assistant", "content": f"msg-{idx}"} for idx in range(count)]


def test_routine_compaction_runs_for_low_remote_but_not_max_remote(monkeypatch, tmp_path):
    from ouroboros import loop

    calls = []

    def fake_checkpoint(messages, **kwargs):
        calls.append(("checkpoint", kwargs["reason"], kwargs["keep_recent"]))
        return True

    def fake_compact(messages, keep_recent, **kwargs):
        calls.append(("compact", keep_recent, kwargs.get("drive_root"), kwargs.get("task_id")))
        return [{"role": "system", "content": "compacted"}], {"prompt_tokens": 1}

    monkeypatch.setattr(loop, "_persist_compaction_checkpoint", fake_checkpoint)
    monkeypatch.setattr(loop, "compact_tool_history_llm", fake_compact)

    base = dict(
        tools=SimpleNamespace(_ctx=SimpleNamespace(_pending_compaction=None)),
        drive_root=tmp_path,
        drive_logs=tmp_path / "logs",
        task_id="task-1",
        round_idx=7,
        event_queue=None,
        checkpoint_injected=False,
        emit_progress=lambda _msg: None,
    )

    low_messages, low_usage = loop._run_round_compaction(
        _messages(),
        loop._CompactionRoundContext(active_use_local=False, active_context_mode="low", **base),
    )
    assert low_messages == [{"role": "system", "content": "compacted"}]
    assert low_usage == {"prompt_tokens": 1}
    assert calls == [("checkpoint", "routine", 20), ("compact", 20, tmp_path, "task-1")]

    calls.clear()
    max_messages, max_usage = loop._run_round_compaction(
        _messages(),
        loop._CompactionRoundContext(active_use_local=False, active_context_mode="max", **base),
    )
    assert len(max_messages) == 41
    assert max_usage is None
    assert calls == []

    local_messages, local_usage = loop._run_round_compaction(
        _messages(),
        loop._CompactionRoundContext(active_use_local=True, active_context_mode="max", **base),
    )
    assert local_messages == [{"role": "system", "content": "compacted"}]
    assert local_usage == {"prompt_tokens": 1}


def test_emergency_compaction_shrinks_keep_recent_to_span_count(monkeypatch, tmp_path):
    """Emergency compaction must pass keep_recent BELOW the span count or the
    compactor no-ops exactly when the transcript is too big (<=50 huge rounds
    over the byte threshold never compacted at all)."""
    from ouroboros import loop

    calls = []

    def fake_checkpoint(messages, **kwargs):
        calls.append(("checkpoint", kwargs["reason"], kwargs["keep_recent"]))
        return True

    def fake_compact(messages, keep_recent, **kwargs):
        calls.append(("compact", keep_recent))
        return [{"role": "system", "content": "compacted"}], None

    monkeypatch.setattr(loop, "_persist_compaction_checkpoint", fake_checkpoint)
    monkeypatch.setattr(loop, "compact_tool_history_llm", fake_compact)
    monkeypatch.setattr(loop, "_estimate_messages_chars", lambda _m: 10**9)

    # 30 tool rounds -> emergency keep_recent must be 15 (30 // 2), not 50.
    messages = []
    for i in range(30):
        messages.append({
            "role": "assistant", "content": f"r{i}",
            "tool_calls": [{"id": f"c{i}", "function": {"name": "x", "arguments": "{}"}}],
        })
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})

    ctx = loop._CompactionRoundContext(
        tools=SimpleNamespace(_ctx=SimpleNamespace(_pending_compaction=None)),
        drive_root=tmp_path,
        drive_logs=tmp_path / "logs",
        task_id="task-em",
        round_idx=3,
        event_queue=None,
        active_use_local=False,
        active_context_mode="max",
        checkpoint_injected=False,
        emit_progress=lambda _msg: None,
    )
    compacted, _usage = loop._run_round_compaction(messages, ctx)

    assert compacted == [{"role": "system", "content": "compacted"}]
    assert calls == [("checkpoint", "emergency_context_size", 15), ("compact", 15)]

    # Few huge rounds (<= 6 spans): keep_recent clamps BELOW the span count so
    # the compactor's len(spans) <= keep_recent gate cannot no-op forever.
    calls.clear()
    small = []
    for i in range(4):
        small.append({
            "role": "assistant", "content": f"r{i}",
            "tool_calls": [{"id": f"s{i}", "function": {"name": "x", "arguments": "{}"}}],
        })
        small.append({"role": "tool", "tool_call_id": f"s{i}", "content": "huge"})
    loop._run_round_compaction(small, ctx)
    assert calls == [("checkpoint", "emergency_context_size", 3), ("compact", 3)]


def test_context_compaction_observability_uses_current_task_drive(monkeypatch, tmp_path):
    from ouroboros import context_compaction
    from ouroboros import llm_observability

    seen = {}

    def fake_chat_observed(_client, **kwargs):
        seen.update(kwargs)
        return {"content": "[round:1]\nsummary"}, {"prompt_tokens": 1}

    monkeypatch.setattr(llm_observability, "chat_observed", fake_chat_observed)
    monkeypatch.setattr(context_compaction, "LLMClient", lambda: object(), raising=False)

    summary, usage = context_compaction._summarize_round_batch(
        [(1, "TOOL_CALL x: {}")],
        drive_root=tmp_path,
        task_id="task-42",
    )

    assert summary == {1: "summary"}
    assert usage == {"prompt_tokens": 1}
    assert seen["drive_root"] == tmp_path
    assert seen["task_id"] == "task-42"


def test_emergency_compaction_necessity_uses_calibrated_density(monkeypatch, tmp_path):
    """NECESSITY is total calibrated pressure: the char budget compared in REAL
    tokens via the main-loop density baseline (neutral 1.0 cold, measured
    supersedes) — on a measured ~1.7x-dense route the trigger fires before the
    raw-char form would; on a cold store behavior is unchanged."""
    from ouroboros import context_fit, loop

    calls = []
    monkeypatch.setattr(
        loop, "_persist_compaction_checkpoint",
        lambda m, **k: calls.append(("checkpoint", k["reason"])) or True,
    )
    monkeypatch.setattr(
        loop, "compact_tool_history_llm",
        lambda m, keep_recent, **k: (calls.append(("compact", keep_recent)) or (m, None)),
    )
    # Raw chars sit BELOW the 1.2M max trigger; ~1.7x measured density puts the
    # calibrated real-token pressure over it.
    monkeypatch.setattr(loop, "_estimate_messages_chars", lambda _m: 800_000)

    def _ctx(density):
        monkeypatch.setattr(context_fit, "main_loop_token_density", lambda _dr, _m: density)
        return loop._CompactionRoundContext(
            tools=SimpleNamespace(_ctx=SimpleNamespace(_pending_compaction=None, _accumulated_usage={})),
            drive_root=tmp_path, drive_logs=tmp_path / "logs", task_id="task-cal",
            round_idx=3, event_queue=None, active_use_local=False,
            active_context_mode="max", checkpoint_injected=False,
            emit_progress=lambda _msg: None,
        )

    messages = []
    for i in range(8):
        messages.append({
            "role": "assistant", "content": f"r{i}",
            "tool_calls": [{"id": f"c{i}", "function": {"name": "x", "arguments": "{}"}}],
        })
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})

    loop._run_round_compaction(messages, _ctx(1.7))
    assert ("checkpoint", "emergency_context_size") in calls

    calls.clear()
    loop._run_round_compaction(messages, _ctx(1.0))  # cold baseline: unchanged behavior
    assert calls == []


def test_emergency_compaction_necessity_counts_tool_schemas(monkeypatch, tmp_path):
    """NECESSITY is TOTAL pressure: the tool schemas travel beside `messages` on
    the wire, so they must count. A transcript just under the trigger plus a big
    schema envelope is over it — the submarine class where compaction fired a
    whole tool envelope (~148K chars) late."""
    from ouroboros import context_fit, loop

    calls = []
    monkeypatch.setattr(
        loop, "_persist_compaction_checkpoint",
        lambda m, **k: calls.append(("checkpoint", k["reason"])) or True,
    )
    monkeypatch.setattr(
        loop, "compact_tool_history_llm",
        lambda m, keep_recent, **k: (calls.append(("compact", keep_recent)) or (m, None)),
    )
    monkeypatch.setattr(context_fit, "main_loop_token_density", lambda _dr, _m: 1.0)
    # 1.19M chars ≈ 297.5K tokens: just UNDER the 1.2M-char (300K-token) max trigger.
    monkeypatch.setattr(loop, "_estimate_messages_chars", lambda _m: 1_190_000)

    messages = []
    for i in range(8):
        messages.append({
            "role": "assistant", "content": f"r{i}",
            "tool_calls": [{"id": f"c{i}", "function": {"name": "x", "arguments": "{}"}}],
        })
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})

    def _ctx(schemas):
        return loop._CompactionRoundContext(
            tools=SimpleNamespace(_ctx=SimpleNamespace(_pending_compaction=None, _accumulated_usage={})),
            drive_root=tmp_path, drive_logs=tmp_path / "logs", task_id="task-schemas",
            round_idx=3, event_queue=None, active_use_local=False,
            active_context_mode="max", checkpoint_injected=False,
            emit_progress=lambda _msg: None, tool_schemas=schemas,
        )

    loop._run_round_compaction(messages, _ctx(None))
    assert calls == []  # transcript alone stays under the trigger

    # ~40K tokens of schemas (the submarine envelope) push the TOTAL over it.
    schemas = [
        {"type": "function", "function": {"name": f"tool_{i}", "description": "d" * 4000,
                                          "parameters": {"type": "object"}}}
        for i in range(40)
    ]
    assert context_fit.tool_schema_tokens(schemas) > 30_000
    loop._run_round_compaction(messages, _ctx(schemas))
    assert ("checkpoint", "emergency_context_size") in calls


def test_emergency_compaction_arms_when_nothing_is_compactable(monkeypatch, tmp_path):
    """A frozen frame over the trigger with UNDER two tool rounds: the compactor
    structurally no-ops (`len(spans) <= keep_recent`), so running it bought nothing
    and wrote a forensic checkpoint every round. It must arm the hysteresis instead
    — and the arm has to hold with an EMPTY compactable region, where a 20%-growth
    test on zero never suppresses."""
    from ouroboros import context_fit, loop
    from ouroboros.context_budget import COMPACTION_HYSTERESIS_ROUNDS

    calls, progress, events = [], [], []
    monkeypatch.setattr(
        loop, "_persist_compaction_checkpoint",
        lambda m, **k: calls.append("checkpoint") or True,
    )
    monkeypatch.setattr(
        loop, "compact_tool_history_llm",
        lambda m, keep_recent, **k: (calls.append("compact") or (m, None)),
    )
    monkeypatch.setattr(loop, "_emit_checkpoint_event", lambda _q, _t, _l, row: events.append(row))
    monkeypatch.setattr(context_fit, "main_loop_token_density", lambda _dr, _m: 1.0)
    # A low-mode frozen frame (~630K chars) is already over the 400K trigger in the
    # task's FIRST rounds, before any tool round exists.
    monkeypatch.setattr(loop, "_estimate_messages_chars", lambda _m: 630_000)

    state = {}
    frame = [{"role": "system", "content": "frame"}, {"role": "user", "content": "go"}]

    def _ctx(round_idx):
        return loop._CompactionRoundContext(
            tools=SimpleNamespace(_ctx=SimpleNamespace(_pending_compaction=None, _accumulated_usage=state)),
            drive_root=tmp_path, drive_logs=tmp_path / "logs", task_id="task-nc",
            round_idx=round_idx, event_queue=None, active_use_local=False,
            active_context_mode="low", checkpoint_injected=True,
            emit_progress=progress.append,
        )

    for rnd in range(1, COMPACTION_HYSTERESIS_ROUNDS):
        loop._run_round_compaction(frame, _ctx(rnd))

    assert calls == []  # no summarizer call, no forensic checkpoint churn
    assert len(progress) == 1  # disclosed exactly once, not per round
    assert state["_compaction_hysteresis"] == {"round": 1, "region_chars": 0}
    assert [e["reason"] for e in events] == ["nothing_compactable"]


def test_emergency_compaction_hysteresis_suppresses_futile_refire(monkeypatch, tmp_path):
    """UTILITY/rearm: a pass that could not get below the trigger arms a
    hysteresis — no per-round refire (no light-model call, no cache-destroying
    rewrite) until the compactable region grows ~20% or N rounds pass. One loud
    disclosure on arming."""
    from ouroboros import context_fit, loop
    from ouroboros.context_budget import COMPACTION_HYSTERESIS_ROUNDS

    calls = []
    progress = []
    monkeypatch.setattr(
        loop, "_persist_compaction_checkpoint",
        lambda m, **k: calls.append("checkpoint") or True,
    )
    # FUTILE pass: returns the transcript unchanged.
    monkeypatch.setattr(
        loop, "compact_tool_history_llm",
        lambda m, keep_recent, **k: (calls.append("compact") or (m, None)),
    )
    monkeypatch.setattr(context_fit, "main_loop_token_density", lambda _dr, _m: 1.0)
    # Size scales with message count so the compactable region can grow.
    monkeypatch.setattr(loop, "_estimate_messages_chars", lambda m: len(m) * 100_000)

    def _messages(rounds):
        out = []
        for i in range(rounds):
            out.append({
                "role": "assistant", "content": f"r{i}",
                "tool_calls": [{"id": f"h{i}", "function": {"name": "x", "arguments": "{}"}}],
            })
            out.append({"role": "tool", "tool_call_id": f"h{i}", "content": "ok"})
        return out

    state = {}

    def _ctx(round_idx):
        return loop._CompactionRoundContext(
            tools=SimpleNamespace(_ctx=SimpleNamespace(_pending_compaction=None, _accumulated_usage=state)),
            drive_root=tmp_path, drive_logs=tmp_path / "logs", task_id="task-hyst",
            round_idx=round_idx, event_queue=None, active_use_local=False,
            active_context_mode="max", checkpoint_injected=False,
            emit_progress=progress.append,
        )

    # 10 rounds x 2 msgs x 100K = 2M chars > 1.2M: fires, futile, arms.
    loop._run_round_compaction(_messages(10), _ctx(3))
    assert calls == ["checkpoint", "compact"]
    assert "_compaction_hysteresis" in state
    assert any("cannot help" in p and "frozen frame" in p for p in progress)

    # Same region, next round: suppressed (no checkpoint, no light-model call).
    calls.clear()
    progress.clear()
    loop._run_round_compaction(_messages(10), _ctx(4))
    assert calls == []
    assert progress == []  # disclosed once, on arming — not per round

    # Region grew >=20% (10 -> 13 rounds = 2.6M >= 2.4M): re-fires.
    loop._run_round_compaction(_messages(13), _ctx(5))
    assert calls == ["checkpoint", "compact"]

    # Re-armed by the futile re-fire; N rounds later it re-fires on time alone.
    calls.clear()
    loop._run_round_compaction(_messages(13), _ctx(5 + COMPACTION_HYSTERESIS_ROUNDS))
    assert calls == ["checkpoint", "compact"]
