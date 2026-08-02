from __future__ import annotations

import pathlib
import re
from typing import Literal, get_args, get_origin, get_type_hints

from ouroboros.gateway.contracts import (
    HTTP_ENDPOINTS,
    WS_MESSAGE_TYPES,
    ChatInbound,
    ChatOutbound,
    OwnerScopeReviewFloorResponse,
    PhotoOutbound,
    SkillDeleteResponse,
    SkillLifecycleQueueResponse,
    StateResponse,
    UpdateApplyAssistedStartedResponse,
    UpdateApplyErrorResponse,
    UpdateApplyManualResponse,
    UpdateApplyOkResponse,
    UpdateApplyRequest,
    UpdatePreflightErrorResponse,
    UpdatePreflightProtectedRoute,
    UpdatePreflightResponse,
    UpdatePreflightSuccessResponse,
    VideoOutbound,
)
from ouroboros.gateway.router import collect_routes


def _js_typedef_fields(text: str, name: str) -> set[str]:
    match = re.search(rf"@typedef \{{Object\}} {name}\b(?P<body>.*?)\n \*/", text, re.S)
    assert match, f"api_types.js missing {name}"
    # Types nest braces (``{Object<string, {project_id: string}>}``), so scan for the BALANCED
    # closing brace instead of the first one — a non-greedy ``[^}]+`` silently mis-parses those
    # properties and makes the field set look like it drifted when it has not.
    fields: set[str] = set()
    for line in match.group("body").split("\n"):
        head, sep, rest = line.partition("@property {")
        if not sep:
            continue
        depth = 1
        for idx, char in enumerate(rest):
            depth += (char == "{") - (char == "}")
            if depth == 0:
                identifier = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", rest[idx + 1:])
                if identifier:
                    fields.add(identifier.group(1))
                break
    return fields


def _contains_none(annotation) -> bool:
    return annotation is type(None) or any(_contains_none(arg) for arg in get_args(annotation))


def test_gateway_contract_endpoint_index_matches_router_and_types(tmp_path):
    tokens: set[str] = set()
    for route in collect_routes(data_dir=tmp_path):
        path = getattr(route, "path", "")
        if not path:
            continue
        methods = getattr(route, "methods", None)
        if methods is None:
            tokens.add(f"WS {path}")
            continue
        normalized = sorted(m for m in methods if m not in {"HEAD", "OPTIONS"})
        if set(normalized) == {"DELETE", "GET", "PATCH", "POST", "PUT"}:
            tokens.add(f"ANY {path}")
        else:
            for method in normalized:
                tokens.add(f"{method} {path}")
    contract_tokens = set(HTTP_ENDPOINTS)
    missing = contract_tokens - tokens
    extra = tokens - contract_tokens
    assert not missing, f"HTTP_ENDPOINTS includes routes not mounted by gateway.router: {sorted(missing)}"
    assert not extra, f"gateway.router mounts routes missing from HTTP_ENDPOINTS: {sorted(extra)}"
    text = (pathlib.Path(__file__).resolve().parent.parent / "web" / "modules" / "api_types.js").read_text(
        encoding="utf-8"
    )
    version = (pathlib.Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    assert f"GATEWAY_CONTRACT_VERSION = '{version}'" in text
    for name in (
        "StateResponse",
        "HealthResponse",
        "SettingsMeta",
        "OpenAICompatibleModelsResponse",
        "UiPreferencesResponse",
        "ChatInbound",
        "ChatOutbound",
        "PhotoOutbound",
        "VideoOutbound",
        "MessageAnnotationOutbound",
        "UploadResponse",
        "TaskCreateResponse",
        "TaskEvent",
        "TaskListResponse",
        "TaskCancelResponse",
        "LogTailResponse",
        "SkillDeleteResponse",
        "UpdatePreflightProtectedRoute",
        "UpdatePreflightSuccessResponse",
        "UpdatePreflightErrorResponse",
        "UpdateApplyRequest",
        "UpdateApplyOkResponse",
        "UpdateApplyAssistedStartedResponse",
        "UpdateApplyManualResponse",
        "UpdateApplyErrorResponse",
    ):
        assert re.search(rf"@typedef \{{Object\}} {name}\b", text), f"api_types.js missing {name}"
    api_client = (pathlib.Path(__file__).resolve().parent.parent / "web" / "modules" / "api_client.js").read_text(
        encoding="utf-8"
    )
    assert "openAICompatibleModels" in api_client
    # v6.88.1: `updateApply` is the one typed helper whose REQUEST body is a declared contract, and
    # what it carries is a safety override. The typedef parity below only proves api_types.js knows
    # the fields, and pinning the parameter list alone proves no more: a helper that destructures
    # all five and then posts `{strategy}` would still satisfy it while degrading an acknowledged
    # apply into an unacknowledged one, with no test going red. So pin BOTH ends of the helper —
    # the destructured request it accepts AND the object literal it hands to jsonPost — because it
    # is the forwarding, not the signature, that carries the override to the wire.
    apply_helper = re.search(
        r"updateApply: \((?P<accepts>\{.*?\})\) => jsonPost\('/api/update/apply', (?P<forwards>\{.*?\})\)",
        api_client,
        re.S,
    )
    assert apply_helper, (
        "api_client.js updateApply must destructure ONE UpdateApplyRequest object and post ONE "
        "object literal to /api/update/apply"
    )
    for end, verb in (("accepts", "does not accept"), ("forwards", "does not forward")):
        dropped = sorted(
            field
            for field in get_type_hints(UpdateApplyRequest, include_extras=True)
            if not re.search(rf"\b{field}\b", apply_helper.group(end))
        )
        assert not dropped, f"api_client.js updateApply {verb} UpdateApplyRequest fields: {dropped}"
    # v6.88.1: the RESPONSE is a discriminated union, and the browser previously told the variants
    # apart by probing keys — including the protected DISCLOSURE, the one frame that carries a
    # safety decision. The union and the helper's return annotation are pinned so a caller can be
    # pointed at a declared shape rather than at the endpoint's source.
    union = re.search(r"@typedef \{(?P<members>[^}]+)\} UpdateApplyResponse\b", text)
    assert union, "api_types.js missing the UpdateApplyResponse union"
    assert set(union.group("members").split("|")) == {
        "UpdateApplyOkResponse",
        "UpdateApplyAssistedStartedResponse",
        "UpdateApplyManualResponse",
        "UpdateApplyErrorResponse",
    }
    assert re.search(
        r"@returns \{Promise<import\('\./api_types\.js'\)\.UpdateApplyResponse>\}\s*\n\s*\*/\s*\n\s*updateApply:",
        api_client,
    ), "api_client.js updateApply must declare the typed response it hands to its callers"
    # v6.88.1 r6: the discriminator must be a LITERAL on both sides. Declared as a plain string it
    # named a field the variants happen to share instead of the value that tells them apart, so
    # neither `get_type_hints` nor a JSDoc consumer could narrow the union at all.
    for variant, literal in (
        (UpdateApplyOkResponse, "ok"),
        (UpdateApplyAssistedStartedResponse, "assisted_started"),
        (UpdateApplyManualResponse, "manual"),
    ):
        hint = get_type_hints(variant, include_extras=True)["status"]
        assert get_origin(hint) is Literal and get_args(hint) == (literal,), (
            f"{variant.__name__}.status must be Literal[{literal!r}], not {hint!r}"
        )
        assert re.search(rf"@property \{{'{literal}'\}} status\b", text), (
            f"api_types.js must declare the {literal!r} discriminator as a JSDoc literal"
        )
    # v6.88.1 r6: preflight is a UNION too — an exception replaces both success keys with `error`,
    # so a single object declaring them required described a frame the endpoint never emits.
    preflight_union = re.search(r"@typedef \{(?P<members>[^}]+)\} UpdatePreflightResponse\b", text)
    assert preflight_union, "api_types.js missing the UpdatePreflightResponse union"
    assert set(preflight_union.group("members").split("|")) == {
        "UpdatePreflightSuccessResponse",
        "UpdatePreflightErrorResponse",
    }
    assert get_args(UpdatePreflightResponse) == (
        UpdatePreflightSuccessResponse, UpdatePreflightErrorResponse
    ), "UpdatePreflightResponse must stay the success|error union the endpoint actually answers"
    # v6.80.0: the two contracts extended this release join the FIELD-level parity list. The name-level
    # loop above cannot see a new @property, so an ABI field added on the Python side would otherwise
    # never have to appear in the browser's typedef (ARCHITECTURE.md §11.3).
    # v6.88.1: the managed-update preflight/apply ABI joins the field-level list too — the update
    # dialog decides what action to OFFER from `protected_route`, so a field that drifts silently
    # would make the browser offer an action the backend then refuses.
    for cls in (ChatInbound, ChatOutbound, PhotoOutbound, VideoOutbound,
                StateResponse, OwnerScopeReviewFloorResponse,
                UpdatePreflightProtectedRoute, UpdatePreflightSuccessResponse,
                UpdatePreflightErrorResponse, UpdateApplyRequest,
                UpdateApplyOkResponse, UpdateApplyAssistedStartedResponse,
                UpdateApplyManualResponse, UpdateApplyErrorResponse):
        expected = set(get_type_hints(cls, include_extras=True))
        actual = _js_typedef_fields(text, cls.__name__)
        assert actual == expected, f"{cls.__name__} JSDoc fields drifted: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
    assert re.search(r"@property \{boolean\} context_mode_auto_low\b", text), (
        "StateResponse.context_mode_auto_low must be a JSDoc boolean — the owner control branches on it"
    )
    assert re.search(r"@property \{string\} deprecation_notice\b", text), (
        "OwnerScopeReviewFloorResponse.deprecation_notice must be declared for the browser"
    )
    assert re.search(r"@property \{boolean=\} force_plan\b", text), "ChatInbound missing force_plan"
    for field in ("model_lane", "requested_model_lane", "effective_model_lane", "model", "task_group_id"):
        assert re.search(rf"@property \{{string=\}} {field}\b", text), f"ChatOutbound missing {field}"
    for field in ("source", "line", "root"):
        assert re.search(rf"@property \{{[^}}]+=\}} {field}\b", text), f"TaskEvent missing {field}"
    for field in (
        "subagent_event",
        "subagent_task_id",
        "root_task_id",
        "parent_task_id",
        "delegation_role",
        "subagent_role",
        "task_event",
        "status",
        "result",
        "trace_summary",
        "error",
        "artifact_status",
    ):
        assert re.search(rf"@property \{{string=\}} {field}\b", text), f"ChatOutbound missing {field}"
    assert re.search(r"@property \{\?number=\} cost_usd\b", text), "ChatOutbound cost_usd must be nullable"
    assert re.search(r"@property \{number=\} chat_id\b", text), "ChatOutbound missing chat_id"
    assert re.search(r"@property \{boolean=\} worker_saturation_warning\b", text), "ChatOutbound missing worker_saturation_warning"
    assert "review_projection" in get_type_hints(ChatOutbound, include_extras=True)
    assert re.search(r"@property \{Object=\} review_projection\b", text)
    assert "setup_contract" in text
    assert re.search(r"@property \{string=\} error\b", text), "SkillDeleteResponse missing optional error"
    assert {"chat", "command", "photo", "video", "typing", "log", "heartbeat", "extension_lifecycle"} <= set(WS_MESSAGE_TYPES)
    assert "message_annotation" in WS_MESSAGE_TYPES
    assert _js_typedef_fields(text, "MessageAnnotationOutbound") == {
        "type",
        "annotation_type",
        "chat_id",
        "client_message_id",
        "action",
        "target",
        "status",
        "options",
        "suppress_bubble",
        "ts",
    }


def test_gateway_money_contracts_keep_unavailable_distinct_from_zero():
    from ouroboros.gateway.contracts import StateResponse

    state_hints = get_type_hints(StateResponse, include_extras=True)
    for field in ("spent_usd", "budget_pct", "spent_calls"):
        assert _contains_none(state_hints[field]), f"StateResponse.{field} must admit ledger-unavailable null"

    chat_hints = get_type_hints(ChatOutbound, include_extras=True)
    for field in (
        "cost_usd",
        "cost_usd_with_children",
        "reserved_usd",
        "unresolved_upper_bound_usd",
        "unknown_unmetered",
    ):
        assert _contains_none(chat_hints[field]), f"ChatOutbound.{field} must admit ledger-unavailable null"
    assert {"cost_accounting_status", "cost_final", "cost_with_children_partial"} <= set(chat_hints)


def test_skill_lifecycle_queue_contract_matches_runtime_shape():
    fields = set(SkillLifecycleQueueResponse.__annotations__)

    assert {"active", "events"} <= fields
    assert {"queue", "recent_events", "running"}.isdisjoint(fields)


def test_skill_delete_contract_matches_runtime_shape():
    fields = set(SkillDeleteResponse.__annotations__)

    assert {
        "ok",
        "skill",
        "source",
        "deleted_payload_root",
        "deleted_state",
        "extension_action",
        "extension_reason",
        "error",
    } <= fields


def test_v682_cancellation_contract_fields_are_mirrored_in_both_languages():
    """The additive cancellation ABI (v6.82) must exist in BOTH mirrors: the
    host-attested cancelable marker and the cancel endpoint's cascade echo."""
    repo = pathlib.Path(__file__).resolve().parents[1]
    python_contract = (repo / "ouroboros" / "gateway" / "contracts.py").read_text(encoding="utf-8")
    js_contract = (repo / "web" / "modules" / "api_types.js").read_text(encoding="utf-8")

    assert "cancelable: NotRequired[bool]" in python_contract
    assert "cascade: bool" in python_contract
    assert "@property {boolean=} cancelable" in js_contract
    assert "@property {boolean=} cascade" in js_contract
