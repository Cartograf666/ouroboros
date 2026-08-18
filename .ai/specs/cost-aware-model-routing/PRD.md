# PRD: Cost-Aware Model Routing (shadow)

Status: DRAFT
Document depth: COMPACT_PRD
Owner: alex
Last updated: 2026-08-18

## Executive summary

Ouroboros can already run one task on a local GGUF model, another on Claude, and a third on
GPT: lane (`main`/`heavy`/`light`) and locality (`USE_LOCAL_*`) are independent axes, and a
third axis — metering — decides whether generative work is paid for per token or hosted on the
owner's flat-rate harness subscription. What is missing is the DECISION. `resolve_subagent_lane`
resolves `auto` by inheriting the parent's lane; nothing inspects the task. Placement is
therefore either declared by hand or inherited by accident, and the owner cannot see what a
better placement would have cost.

This cycle delivers a **shadow router**: a deterministic component that computes and records the
placement it WOULD have chosen, on every eligible dispatch and internal call, without changing
any route. It produces the evidence needed to decide whether the router should later be given
authority — and produces it from real traffic rather than from a guess.

## Evidence and current state

- `ouroboros/subagents.py:697` (`resolve_subagent_lane`): `auto` inherits the parent lane. Its
  docstring records that automatic inference was REMOVED twice — v6.87.7 (`auto -> heavy` when
  mutating, "deleted rather than re-tuned (BIBLE P2: remove the class)") and v6.87.26
  (`auto -> light`, "silently demoting every unspecified child made the declaration invisible
  and the default wrong"). Both removals were about inference making a decision INVISIBLE.
- `ouroboros/subagents.py:642` maps each lane to its own `(model env, use_local env)` pair, so
  local and remote placement is already expressible per lane.
- Metering: a delegated harness session is described in-tree as a "$0 delegated run". Commit
  `8044da9b` steers work toward it with a SYSTEM.md instruction, written after live task
  `58ecb117` authored large bodies in metered output instead. BIBLE P2 calls a prompt rule
  "training, not growth" once the class has recurred.
- `ouroboros/tools/control.py:2025` (`switch_model`) lets a task raise its own power mid-flight,
  so a too-cheap placement degrades into an extra round rather than a failure.
- `UsageScope` (`ouroboros/usage_accounting.py:195`) stamps every ledger row with `task_id`,
  `root_task_id`, `parent_task_id`, `category` and `source`. `source` is already a usable shape
  key for internal call sites; subagents have no equivalent key yet.
- `docs/DEVELOPMENT.md` invariant "projection over replay": a reader that runs per interaction —
  explicitly including a task turn — must not replay a growing store. Dispatch is such a path.
- `SubagentDispatch` already carries a typed `capability_delta` describing what a dispatch took
  away from the request, and `ChatOutbound` already carries `review_projection` to the chat card.

## Users and use cases

- **Owner (primary).** Wants minimum spend without hand-placing every task, and wants to see
  where placement decisions are being made and what they would save.
- **Ouroboros itself (system actor).** Consumes the recommendation as evidence; in this cycle it
  does not obey it.
- **Reviewers (secondary).** Must be able to audit any placement decision after the fact.

## Goals

- GOAL-1: Every eligible placement decision produces a recorded, reasoned recommendation.
- GOAL-2: The owner can see, per task and in aggregate, what the router would have saved and
  where it would have been wrong.
- GOAL-3: Recommendations are derived from this installation's own measured history, not from a
  hand-written table.
- GOAL-4: The routing decision itself costs no model call and no growing-store scan.

## Non-goals

- The router does NOT change any route in this cycle. Granting it authority is a separate,
  later contract that this cycle exists to inform.
- Review lanes (triad, scope, plan, skill review, deep self-review) are out of scope entirely.
- No change to any threshold or gate: `MAX_MODULE_LINES`, `MAX_FUNCTION_LINES`,
  `MAX_TOTAL_FUNCTIONS`, `GRANDFATHERED_*`, and the `OUROBOROS_CONTEXT_MODE=max` default are
  untouched.
- No change to the default model of any slot.
- No new UI page; the chat card reuses the existing outbound contract.

## System behavior

**Normal flow, subagent (phase A).** A child is dispatched. `resolve_subagent_dispatch` resolves
executor, lane and profile as today. The router is then asked for a metering RECOMMENDATION from
facts already in hand (declared lane, task constraint, whether a harness route is live, the
child's own authority, historical evidence for this shape). The recommendation and its reason
are attached to the dispatch record. The dispatch itself is unchanged.

**Normal flow, internal call (phase B).** An enrolled internal call site (dialogue consolidation,
project naming, semantic dedup, review verdict extraction) resolves its slot as today. The router
is asked for a lane/locality recommendation keyed on that call site's `source` label plus its
measured history. The recommendation is recorded. The call is unchanged.

**Declaration wins.** When a lane is explicitly declared, the router records
`explicit_declaration` and makes no recommendation. Silence is the only case it speaks to.

**Failure.** Any router error, missing projection, or unreadable history records
`recommendation_unavailable` with the reason and returns nothing. Dispatch proceeds exactly as
before. The router can never block, delay past its bound, or fail a task.

**Aggregate.** A savings summary reports, over a window: recommendations made by kind, the
estimated spend difference had they been followed, and observed mismatches — cases where the
cheaper placement was recommended but the task's actual outcome (escalation via `switch_model`,
a failed objective, or a retry) indicates it would not have held.

## Experience and design constraints

Not applicable as UI design work. No new page, token, primitive, or component API. The
per-task disclosure reuses the existing `ChatOutbound` projection path already used by
`review_projection`; the savings summary is a text/JSON report surfaced through existing
reporting surfaces.

## Functional requirements

- FR-1: On every subagent dispatch whose requested lane is `auto`, the router produces a metering
  recommendation (`native_metered` | `subscription_delegated` | `no_recommendation`) with a typed
  reason, recorded on the dispatch record.
- FR-2: On every enrolled internal call site, the router produces a lane+locality recommendation
  with a typed reason, recorded against that call.
- FR-3: The router performs NO model call and NO network call.
- FR-4: The router reads only a maintained projection and facts already resolved at the decision
  point; it never scans `usage_attempts.jsonl` or any other append-only store.
- FR-5: An explicitly declared lane suppresses the recommendation and records
  `explicit_declaration`.
- FR-6: Each recommendation and its reason are visible on the task's chat card.
- FR-7: A savings summary reports recommendations, estimated spend difference, and observed
  mismatches over a bounded window.
- FR-8: The projection is enrolled in the `ouroboros/context_budget.py` threshold table with a
  justified constant, in the same commit that introduces it.
- FR-9: The router is fail-soft: on any internal error it records `recommendation_unavailable`
  and the surrounding operation is unaffected.

## Quality attributes

- NFR-1: Zero routing behavior change. Effective lane, model, locality, executor and profile for
  every task are byte-identical to the pre-change resolution.
- NFR-2: Added dispatch-path cost is O(response): bounded projection read, no growing-store
  replay, no model call.
- NFR-3: The recommendation is deterministic — identical inputs yield an identical
  recommendation and reason.
- NFR-4: Net function count stays under `MAX_TOTAL_FUNCTIONS`, and no module crosses
  `MAX_MODULE_LINES`; no entry is added to any grandfather list.
- NFR-5: The projection is bounded in size and its growth is observable through the same
  hot-store tripwire as every other durable store.

## Acceptance criteria

- AC-1: For a dispatch with `model_lane="auto"`, the dispatch record carries a metering
  recommendation with a typed reason, and the resolved lane, model, locality, executor and tool
  profile are identical to the values resolved without the router.
- AC-2: For a dispatch with an explicitly declared lane, the recorded recommendation is
  `explicit_declaration` and no placement recommendation is produced.
- AC-3: An enrolled internal call site records a lane+locality recommendation keyed on its
  `source` label; the call's actual model, locality and parameters are unchanged.
- AC-4: With the projection file absent, unreadable, or truncated, the operation completes
  normally and records `recommendation_unavailable` with the reason.
- AC-5: The router makes no model call and no network call on any path, proven by a test that
  fails if the provider seam or an HTTP client is reached during recommendation.
- AC-6: The router performs no full read of `usage_attempts.jsonl` or any other append-only
  store on the decision path.
- AC-7: Identical inputs produce an identical recommendation and reason across repeated runs and
  across processes.
- AC-8: The chat card for a task carrying a recommendation displays it and its reason.
- AC-9: The savings summary reports, for a bounded window, recommendation counts by kind, the
  estimated spend difference, and the observed mismatch count.
- AC-10: The projection appears in the `context_budget.py` threshold table with a justified
  constant and is reported by the hot-store growth probe.
- AC-11: `MAX_TOTAL_FUNCTIONS`, `MAX_MODULE_LINES`, `MAX_FUNCTION_LINES`, every
  `GRANDFATHERED_*` set, and the `OUROBOROS_CONTEXT_MODE` default are unchanged, and the size
  gates pass without a new grandfather entry.

- AC-12: The routing leaf imports no routing owner (one-way seam), no dispatch resolver reads
  its output, and the change introduces no platform-layer or process-custody violation.

## Success measures

| Measure | Baseline | Target | Window | Owner |
|---|---|---|---|---|
| Eligible dispatches carrying a recommendation | 0% | >= 95% | 7 days after enable | alex |
| Recommendations produced without a model call | n/a | 100% | continuous | alex |
| Added dispatch latency (p95) | 0 ms | <= 5 ms | 7 days | alex |
| Estimated spend difference if followed | unknown | measured, signed | 14 days | alex |
| Observed mismatch rate | unknown | measured | 14 days | alex |

The last two have no target on purpose: this cycle exists to MEASURE them. A target before the
first measurement would be the hand-written heuristic this design rejects.

## Rollout and rollback

Shadow only. The router is enabled by default because it changes no route; its output is
evidence. Rollback is a single switch that stops recording; nothing else depends on it. No
migration, no data rewrite, no compatibility period. The decision to grant the router authority
is explicitly OUT of this cycle and requires a new contract informed by the measured mismatch
rate.

## Risks and mitigations

- RISK-1: The shadow router silently becomes an authority through a later careless change —
  reintroducing the class removed in v6.87.7 and v6.87.26. Mitigation: AC-1 and AC-3 pin
  byte-identical resolution; the recommendation type is structurally separate from the resolved
  dispatch, not a field the resolver reads.
- RISK-2: The projection becomes a growing hot store read on the dispatch path, violating
  "projection over replay". Mitigation: FR-4, FR-8, AC-6, AC-10 — bounded projection, enrolled
  threshold, explicit test.
- RISK-3: The empirical signal is unusable because subagents have no stable shape key.
  Mitigation: phase A uses only facts already resolved at dispatch; the shape key is a phase-B
  deliverable validated against real history before it is relied on.
- RISK-4: Function/module budget exhaustion — the tree stands at 5782/6000 functions and several
  modules sit at exactly 1600 lines. Mitigation: NFR-4 and AC-11; the implementation must land
  net-neutral or negative, deleting before adding where needed.
- RISK-5: The savings estimate is misread as realized savings. Mitigation: the summary names it
  as counterfactual and reports mismatches beside it, never a bare number.
- RISK-6: The branch is currently red (10 inherited failures), so the reviewed commit gate cannot
  pass. Mitigation: implementation is blocked until the gate is green; this is recorded as a
  precondition, not absorbed into this scope.

## Decisions

- DEC-1: Shadow first. The router advises and records; it does not route. Chosen because it
  removes regression risk entirely, solves the cold-start problem for an empirical signal, and
  cannot reproduce the twice-removed "invisible decision" class.
- DEC-2: One contract, two phases — subagent metering (A) and internal-call lane/locality (B) —
  because the metering axis does not apply to internal calls and the two need different shape
  keys.
- DEC-3: Empirical signal from the installation's own ledger history, not a hand-written table,
  per the owner's decision.
- DEC-4: Full visibility — dispatch record, chat card, and aggregate savings summary.
- DEC-5: No LLM call inside the router. A model call to decide routing spends what the feature
  exists to save and adds a round of latency to every task.
- DEC-6: An explicitly declared lane always suppresses the recommendation, preserving declaration
  primacy.

## Assumptions and open questions

- ASSUMPTION-1: A metering recommendation can be derived from facts already resolved at dispatch
  (declared lane, task constraint, live harness route, child authority) with no additional I/O.
  Validation: phase A implementation measures the added dispatch cost against NFR-2.
- ASSUMPTION-2: `UsageScope.source` is a sufficient shape key for enrolled internal call sites.
  Validation: phase B verifies each enrolled site emits a stable, distinct `source`.
- ASSUMPTION-3: The chat card can carry the recommendation through the existing outbound
  projection without a new frozen-contract field beyond an additive-optional one.
  Validation: confirmed against `ouroboros/contracts/api_v1.py` during implementation.
- RESOLVED (was OPEN-1): the router is its own component `routing-evidence`
  (`ouroboros/routing_recommendation.py` + `ouroboros/routing_evidence.py`), governed by
  `.ai/architecture.yaml` rules ARCH-ROUTING_LEAF_IS_ONE_WAY and
  ARCH-ROUTING_LEAF_HAS_NO_AUTHORITY.
- RESOLVED (was OPEN-2): `.ai/architecture.yaml` exists as a PROVISIONAL, pointer-only
  projection of the existing SSOT; no baseline file was created because accepted debt already
  has canonical homes (P7).
- OPEN-3: `ouroboros/tools/antigravity.py` (added by `29fd9e28`) violates the platform-layer and
  process-custody rules. It is a NEW violation, not legacy, so it is not baselined. It must be
  fixed before this contract's GATE-PLATFORM and GATE-CUSTODY can pass. Owner: alex.
