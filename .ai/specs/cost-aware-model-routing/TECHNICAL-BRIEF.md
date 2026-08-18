# Technical Brief: Cost-Aware Model Routing (shadow)

Status: DRAFT
Related PRD: .ai/specs/cost-aware-model-routing/PRD.md
Architecture manifest: none yet — see OPEN-2; to be produced by `architecture-governance`
Design-system manifest: not applicable (no UI design work)

## Technical outcome

A deterministic, side-effect-free recommendation function is consulted at two existing decision
points. It returns a typed recommendation plus reason, or nothing. Its output is recorded and
displayed; no caller reads it back to make a routing choice. Adding a reader that DID would be
the change this cycle deliberately withholds.

## Current architecture and evidence

| Fact | Location |
|---|---|
| Lane resolution, `auto` inherits parent | `ouroboros/subagents.py:697` `resolve_subagent_lane` |
| Dispatch resolution point, live inputs | `ouroboros/subagents.py:1067` `resolve_subagent_dispatch` |
| Lane to (model, locality) mapping | `ouroboros/subagents.py:642` |
| Typed reduction record already flowing | `SubagentDispatch.capability_delta` |
| Mid-flight escalation | `ouroboros/tools/control.py:2025` `switch_model` |
| Per-call attribution incl. `source` | `ouroboros/usage_accounting.py:195` `UsageScope` |
| Durable monetary substrate (append-only) | `ouroboros/usage_ledger.py` |
| Hot-store threshold table + tripwire | `ouroboros/context_budget.py`, `agent_startup_checks.py::hot_store_growth_notes` |
| Outbound projection to the chat card | `ouroboros/contracts/api_v1.py` (`ChatOutbound`) |
| Enrolled internal call sites | `consolidator.py:349`, `consolidator.py:726`, `project_naming.py:218`, `semantic_dedup.py:121`, `review_execution.py:646` |

Established patterns this reuses rather than reinvents: the compact projection maintained beside
an unbounded event log (`delegate_custody.py` / `containment_faults.jsonl`); the
fingerprint-keyed render cache invalidated by advance, never TTL (`_usage_rows_memo.py`); typed
outcome axes kept separate rather than collapsed (`outcomes.py`).

## Proposed approach

Three responsibilities, deliberately separated:

1. **Recommendation (pure).** Given a resolved decision context, return
   `(recommendation, reason)` or `None`. No I/O, no clock, no randomness. Pure functions are
   what make AC-7 (determinism) and AC-5 (no model call) testable as properties rather than as
   mocked behavior.
2. **Evidence projection (bounded, maintained).** A compact, size-capped projection keyed by
   shape, holding aggregate outcome/cost statistics. Written on task settle — a path that
   already writes durably — and read at the decision point. Never derived by scanning the
   ledger at read time.
3. **Disclosure.** Attach the recommendation to the dispatch record and to the outbound chat
   projection; aggregate it into the savings summary.

The recommendation function is consulted AFTER the existing resolution completes and its result
is not an input to that resolution. This is a structural guarantee, not a convention: the
resolver's return value is built before the recommendation exists.

## UI implementation and design-system reuse

Not applicable. No new page, token, primitive, or component API. The recommendation rides the
existing outbound projection already used for `review_projection`; the chat card renders it with
existing typography and status conventions.

## Affected layers and interfaces

| Layer/component | Intended change | Depth | Contract impact |
|---|---|---|---|
| Routing recommendation (new leaf) | New pure module | full | none — internal |
| Evidence projection (new durable store) | New bounded projection + writer on settle | full | new durable file; enrolled in threshold table |
| `ouroboros/subagents.py` | Attach recommendation to the dispatch record after resolution | methods-only | additive field on an internal record |
| Enrolled internal call sites | Record a recommendation beside the existing call | methods-only | none |
| `ouroboros/contracts/api_v1.py` | Additive-optional outbound field | additive-only | frozen ABI extension — Section 11.1 table + `tests/test_contracts.py` update required |
| `ouroboros/context_budget.py` | Threshold entry for the new store | additive-only | none |
| `web/modules/chat.js` | Render the recommendation | methods-only | none — but the file is grandfathered at 4736 lines; changes must be net-neutral |
| `docs/ARCHITECTURE.md` | Document the router, its non-authority, and the new store | additive-only | governance artifact |

## Component names used by the Change Envelope

The contract schema requires component IDENTIFIERS, not paths. These are provisional until
`architecture-governance` binds them to a manifest; they are listed here so no name in the
envelope is undefined.

| Component | Meaning | Representative paths |
|---|---|---|
| `agent-routing` | Lane/locality/executor resolution | `ouroboros/subagents.py` |
| `routing-evidence` | New pure recommendation leaf + bounded projection | `ouroboros/routing_*.py` |
| `internal-call-sites` | Enrolled non-subagent LLM callers | `consolidator.py`, `project_naming.py`, `semantic_dedup.py`, `review_execution.py` (verdict extraction only) |
| `budget-policy` | Hot-store threshold table | `ouroboros/context_budget.py` |
| `frozen-contracts` | Versioned ABI | `ouroboros/contracts/**` |
| `web-ui` | Browser surface | `web/**` |
| `governance-docs` | Architecture/development/checklist SSOT | `docs/**` |
| `review-stack` | **Forbidden.** Triad/scope/plan/skill/deep review | `ouroboros/tools/review*.py`, `ouroboros/review*.py` |
| `review-lane-routing` | **Forbidden.** Reviewer slot/route selection | `ouroboros/review_execution.py` route selection, `reviewer_slot_config.py` |
| `size-gates` | **Forbidden.** Gate constants and their tests | `ouroboros/review.py` constants, `tests/test_smoke.py` |
| `provider-transport` | **Forbidden.** Provider dispatch | `ouroboros/llm.py` |
| `usage-ledger-substrate` | **Forbidden.** Append-only monetary substrate | `ouroboros/usage_ledger.py` |
| `constitution` | **Forbidden.** | `BIBLE.md` |

`review_execution.py` appears in both `internal-call-sites` and `review-lane-routing`. The split
is by FUNCTION, not file: the verdict-extraction call is an enrolled internal call site; reviewer
slot and route selection is forbidden. Implementation must not widen from one to the other.

## Data and migration

One new bounded projection file under the runtime data root. No schema migration: absence is a
first-class state that yields `recommendation_unavailable` (AC-4). No backfill — an empty
projection simply produces no recommendation until history accumulates, which is the honest
cold-start behavior and the reason DEC-1 chose shadow mode.

Rollback deletes the file; nothing else reads it.

## External integrations

None. The router makes no network call by construction (FR-3, AC-5).

## Failure handling and observability

Every failure mode collapses to one typed outcome, `recommendation_unavailable`, carrying a
reason. The router never raises into its caller, never retries, and never blocks. Its own
latency is bounded by a projection read of capped size.

Observability: recommendation counts and reasons are recorded per decision; the savings summary
aggregates them; the projection's growth is watched by the existing hot-store tripwire once
enrolled (AC-10).

## Security and privacy

The projection holds derived aggregates (shape key, counts, cost statistics, outcome tiers) and
must hold no prompt text, no objective text, and no secret. It inherits the private-file
permissions of the runtime data root. Shape keys must be derived from typed fields, never from
free-text objectives, so the store cannot accumulate user content by accident.

## Alternatives considered

| Option | Benefits | Costs/risks | Decision |
|---|---|---|---|
| LLM judge decides routing | Flexible, no shape key needed | Spends the tokens the feature saves; adds a round of latency per task; non-deterministic | Rejected (DEC-5) |
| Hand-written rule table | Immediate, predictable | Cannot self-correct; the owner asked for empirical | Rejected (DEC-3) |
| Give the router authority now | Immediate savings | Reintroduces the class removed twice; no evidence yet that it is right | Rejected (DEC-1) |
| Keep steering by prompt only | No code change | BIBLE P2: training, not growth — the class already recurred (task 58ecb117) | Rejected |
| Read the ledger at dispatch | No new store | Violates "projection over replay" on a per-turn path | Rejected (FR-4) |
| Extend `capability_delta` to carry it | Reuses a typed record | Conflates "authority was reduced" with "a cheaper placement exists" — different facts, and the delta is read by consumers that would then see a reduction that did not happen | Rejected — separate typed field |

## Test and verification strategy

| AC | Level | Approach |
|---|---|---|
| AC-1, AC-2, AC-3 | unit | Resolve a dispatch/call with and without the router; assert byte-identical resolved values and the expected recorded recommendation |
| AC-4 | unit | Absent, unreadable, and truncated projection; assert normal completion and the typed reason |
| AC-5 | unit | Property test: patch the provider seam and HTTP clients to raise; assert recommendation still succeeds |
| AC-6 | unit | Assert the ledger path is never opened during recommendation |
| AC-7 | unit | Same inputs across repeated calls and a fresh process yield identical output |
| AC-8 | unit | Outbound projection carries the field; `tests/test_contracts.py` pins the additive-optional shape |
| AC-9 | unit | Summary over a seeded projection reports expected counts, difference, and mismatches |
| AC-10 | unit | Threshold table contains the store; growth probe reports it |
| AC-11 | existing gates | `tests/test_smoke.py` size and count gates; diff of gate constants must be empty |

Existing gates that must stay green: `make lint` (`ruff --select F`), `tests/test_smoke.py`,
`tests/test_contracts.py`, and the reviewed commit gate's two-pass hermetic pytest.

## Rollout and rollback

Land phase A, observe, then phase B. Both are shadow, so neither has a compatibility period.
Rollback is deleting the projection and the recording call; no consumer depends on the output.

## Architectural consequences

- One new durable store, which must be enrolled in the threshold table in the same commit
  (a standing repository invariant, not a preference).
- One additive-optional frozen-ABI field, which obliges an update to ARCHITECTURE.md Section 11.1
  and `tests/test_contracts.py`.
- A deliberate architectural debt is NOT taken: the router is not wired into resolution, so no
  caller gains a dependency on it. Granting authority later is a new decision with its own
  contract, and the absence of a reader is what keeps that decision explicit.
- Budget pressure is real: 5782/6000 functions, several modules at exactly 1600 lines. The
  implementation must delete before it adds where a target module is at its ceiling.
