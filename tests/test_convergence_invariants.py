"""Invariants that only exist because two phase branches were converged into one tree.

Every test here guards a failure mode that NO SINGLE BRANCH can see, and that no
ordinary suite would catch, because the dangerous merges are the ones git performs
without a conflict:

* a function MOVED to a new module arrives as a clean ADD, so the newer copy of it that
  the other branch had already fixed is reverted with nothing to notice;
* a deduplication pass deletes the surviving definition rather than the duplicate;
* a merge in the wrong ORDER restores a form the owner already overturned, because
  "newest wins" is a property of commit dates, not of decisions.

The parent SHAs are PINNED. Branch names float -- `cxi/p3-transport` will move, and a
gate that reads a moving name stops testing the thing it was written for.
"""
import ast
import os
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

# The two parents this tree was converged from, plus the live head it was branched off.
#
# SPRINT-LOCAL, AND DELIBERATELY NOT LITERALS HERE. These are PRIVATE commits of the
# convergence sprint: they exist in the operator's worktrees and in no public clone, and
# one of them is the owner's private terminal-demo head, which must never be referenced
# from a file that ships. Hard-coding them made this test guaranteed-red for anyone who
# checked out the PR and put a private SHA in tracked source. They are supplied by the
# environment instead (the sprint harness sets it), so a public clone finds nothing to
# check and SKIPS, while the sprint still gets the full census.
#
# REQUIRED PRE-PR DELETION: this whole file is sprint scaffolding for cross-branch
# convergence checking and must be removed in the final pass — see the ledger row. It is
# kept until then because synthesis needs the census right up to the merge.
_PARENT_ENV = "OUROBOROS_CONVERGENCE_PARENTS"      # p3,p4,live_head[,p2_chain]
_STRICT_ENV = "OUROBOROS_CONVERGENCE_CENSUS_STRICT"


def _pinned_parents() -> list:
    raw = str(os.environ.get(_PARENT_ENV, "") or "").strip()
    if not raw:
        local = REPO / ".convergence-parents"          # untracked, operator-local
        try:
            raw = local.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
    return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]


_PINNED = _pinned_parents()
P3_TRANSPORT = _PINNED[0] if len(_PINNED) > 0 else ""
P4_MUTATING = _PINNED[1] if len(_PINNED) > 1 else ""
LIVE_HEAD = _PINNED[2] if len(_PINNED) > 2 else ""

# A definition present in a parent and absent here is DATA LOSS unless it is named
# below with the symbol that replaced it. "Deleted on purpose" is a motivated tombstone
# (replacement `None`); everything else must point at a symbol that really exists, so a
# reconciliation cannot be satisfied by a name someone meant to add and did not.
RECONCILED = {
    # -- the transport lifecycle moved out of the tool module (cxi/p3-transport) ------
    "ouroboros.tools.delegate:_RunCustody": "ouroboros.delegate_custody:RunCustody",
    "ouroboros.tools.delegate:_settle": "ouroboros.delegate_custody:settle_run",
    "ouroboros.tools.delegate:_summary_of": "ouroboros.delegate_custody:summary_of",
    "ouroboros.tools.delegate:_disclosed_spend": "ouroboros.delegate_custody:disclosed_spend",
    "ouroboros.tools.delegate:_disclosed_tokens": "ouroboros.delegate_custody:disclosed_tokens",
    "ouroboros.tools.delegate:_retire_owned_project": "ouroboros.delegate_custody:retire_project",
    # -- the authority axis replaced the bare profile string (cxi/p4-mutating) --------
    "ouroboros.tools.delegate:_access_profile": "ouroboros.tools.delegate:_derive_authority",
    "ouroboros.tools.delegate:_route_health": "ouroboros.subagents:route_health",
    "ouroboros.tools.delegate:_exhausted_window_reset_at":
        "ouroboros.subagents:_exhausted_window_reset_at",
    # The truncator and every producer that must fit inside it now ask ONE function
    # instead of reading the dict pair through private aliases; the p3 output-delivery
    # port is what forced the move, because `_delivered_terminal_payload` has to bound
    # itself against the SAME limit the truncator applies.
    "ouroboros.loop_tool_execution:_TOOL_RESULT_LIMITS":
        "ouroboros.tool_capabilities:tool_result_limit",
    "ouroboros.loop_tool_execution:_DEFAULT_TOOL_RESULT_LIMIT":
        "ouroboros.tool_capabilities:tool_result_limit",
    # -- owner decision D28 renamed the two rows that pinned the OLD auto+exhausted
    #    behaviour (nanny-anyway) to the ones that pin the loud API fallback -----------
    "tests.test_delegated_subagent_transport:test_rule_auto_with_spent_window_delegates_and_carries_reset_at":
        "tests.test_delegated_subagent_transport:test_rule_auto_with_every_profile_spent_falls_back_to_the_api_loudly",
    "tests.test_delegated_subagent_transport:test_dispatch_row_auto_with_a_spent_window_still_delegates_with_the_reset":
        "tests.test_delegated_subagent_transport:test_dispatch_row_auto_with_every_profile_spent_falls_back_to_the_api",
    # -- deliberately DELETED, no replacement (empty right side = adjudicated removal) -
    # P34R.10: exported and census-pinned but caller-free since the invocation-id
    # doctrine made `invocation_record` the one lookup surface; a dead durable-scan
    # helper invites drift between two readers of the same rows.
    "ouroboros.delegate_custody:start_was_requested": "",
    # -- inherited from the parents themselves, not produced by this convergence ------
    "ouroboros.launcher_bootstrap:python_bytecode_env":
        "ouroboros.launcher_bootstrap:embedded_python_env",
    "launcher:python_bytecode_env": "ouroboros.launcher_bootstrap:embedded_python_env",
    "ouroboros.packaged_cli:python_bytecode_env":
        "ouroboros.launcher_bootstrap:embedded_python_env",
    "ouroboros.subagents:_capability_depth_limit": None,   # v6.87.7: the three axes
    "ouroboros.subagents:_review_or_scope_slots": None,    # v6.87.7: no lane fans out
    # v6.87.7 deleted the lane/depth coupling these pinned (`auto` resolved to heavy for a
    # "mutating" child, and depth rewrote the lane). The transport phase branched before
    # that commit, so it still carries them; they assert behaviour that no longer exists.
    "ouroboros.usage_accounting:_quarantine_tail": "ouroboros.usage_ledger:_quarantine_tail",
    # The invocation-id contract replaced the content-stable Idempotency-Key (a stable
    # content hash made a deliberate re-run of the same prompt collide onto the finished
    # old run); the test moved with the contract it pins.
    "tests.test_delegated_subagent_transport:test_one_logical_start_presents_one_idempotency_key":
        "tests.test_delegated_subagent_transport:"
        "test_the_invocation_id_is_reused_on_retry_and_fresh_per_intended_start",
    **{f"tests.test_model_slot_role_model:{name}": None for name in (
        "test_auto_mutating_child_routes_to_heavy",
        "test_auto_readonly_child_routes_to_light",
        "test_depth_cap_is_configurable",
        "test_explicit_heavy_beyond_depth_cap_downgrades_with_note",
        "test_explicit_main_honored_within_depth_cap",
        "test_string_false_may_mutate_does_not_route_auto_to_heavy",
    )},
    **{f"tests.test_subagents_phase3:{name}": None for name in (
        "test_schedule_subagent_group_drive_failure_is_fail_closed",
        "test_schedule_subagent_review_lane_emits_task_group_metadata",
        "test_subagent_lane_resolution_fans_out_and_depth_coerces_light",
    )},
}


def _symbols(source: str, path: str) -> tuple:
    """What one module binds, split into (definitions, import aliases).

    The two are not the same kind of fact. A DEFINITION that disappears is loss until
    something is named as its replacement. An ALIAS that disappears is only loss if the
    definition behind it is gone too -- a module that stops importing a helper it no
    longer uses has lost nothing, while a module that splits in two and RE-EXPORTS its
    substrate (``usage_accounting`` after the ledger split) still answers to every one of
    those names. A census that conflates them either reports dozens of phantom losses --
    and then gets relaxed until it catches nothing -- or misses the real ones.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(), set()
    mod = path[:-3].replace("/", ".")
    out, aliases = set(), set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            aliases |= {f"{mod}:{a.asname or a.name.split('.')[0]}" for a in node.names}
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(f"{mod}:{node.name}")
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.add(f"{mod}:{node.name}.{sub.name}")
                elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    out.add(f"{mod}:{node.name}.{sub.target.id}")
        elif isinstance(node, ast.Assign):
            out |= {f"{mod}:{t.id}" for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(f"{mod}:{node.target.id}")
    return out, aliases


def _git(*args) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True, check=True).stdout


def _inventory_at(sha: str) -> tuple:
    """Every .py blob of one commit, read through a SINGLE `git cat-file --batch`.

    The first cut spawned one `git show` per file per pinned parent — ~400 spawns
    times three parents on EVERY default pytest run, a tax on every future preflight
    (P34R.8). Two subprocesses per parent now: one tree listing, one batch read.
    """
    entries = []
    for line in _git("ls-tree", "-r", sha).splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if path.endswith(".py") and len(parts) >= 3 and parts[1] == "blob":
            entries.append((parts[2], path))
    defs, aliases = set(), set()
    if not entries:
        return defs, aliases
    batch = subprocess.run(
        ["git", "-C", str(REPO), "cat-file", "--batch"],
        input=("\n".join(oid for oid, _ in entries) + "\n").encode("ascii"),
        capture_output=True, check=True).stdout
    blobs, idx = {}, 0
    while idx < len(batch):
        line_end = batch.find(b"\n", idx)
        if line_end < 0:
            break
        header = batch[idx:line_end].decode("ascii", errors="replace").split()
        if len(header) == 3 and header[1] == "blob":
            size = int(header[2])
            blobs[header[0]] = batch[line_end + 1:line_end + 1 + size]
            idx = line_end + 1 + size + 1     # blob bytes + trailing newline
        else:
            idx = line_end + 1                # "<oid> missing" and friends
    for oid, path in entries:
        content = blobs.get(oid)
        if content is not None:
            d, a = _symbols(content.decode("utf-8", errors="replace"), path)
            defs |= d
            aliases |= a
    return defs, aliases


def _inventory_here() -> tuple:
    defs, aliases = set(), set()
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if any(part in {".git", "__pycache__", "node_modules", "build", "dist"}
               for part in rel.parts):
            continue
        d, a = _symbols(path.read_text(encoding="utf-8", errors="replace"), str(rel))
        defs |= d
        aliases |= a
    return defs, aliases


def _have(sha: str) -> bool:
    try:
        _git("cat-file", "-e", f"{sha}^{{commit}}")
        return True
    except subprocess.CalledProcessError:
        return False


def _clone_is_partial() -> bool:
    """Only a genuinely shallow/partial clone may excuse a missing pinned parent.

    The census used to `pytest.skip` on ANY missing parent object, so the advertised
    parent-loss guard silently performed no check wherever the objects were absent —
    including a full clone whose pinned SHA had been pruned or mistyped, which is not
    a clone artifact but the guard's own evidence going missing (P34R.9).
    """
    try:
        if _git("rev-parse", "--is-shallow-repository").strip() == "true":
            return True
    except subprocess.CalledProcessError:
        return True                      # cannot even ask: do not fake a verdict
    probe = subprocess.run(
        ["git", "-C", str(REPO), "config", "--get-regexp", r"remote\..*\.partialclonefilter"],
        capture_output=True, text=True)
    return bool(probe.stdout.strip())


def test_every_reconciliation_names_a_replacement_that_exists():
    """A reconciliation map is only worth what its right-hand side is worth.

    Always on, no git required: an entry pointing at a symbol nobody defines would let a
    real loss be waved through by a note, which is the exact failure the map exists to
    prevent.
    """
    here_defs, here_aliases = _inventory_here()
    here = here_defs | here_aliases
    dangling = sorted(new for new in RECONCILED.values() if new and new not in here)
    assert not dangling, (
        "Reconciliation map names replacements that do not exist:\n  "
        + "\n  ".join(dangling))


@pytest.mark.parametrize("parent", [P3_TRANSPORT, P4_MUTATING, LIVE_HEAD])
def test_no_definition_from_a_parent_vanished_without_a_reconciliation(parent):
    """A symbol a parent defined and this tree does not is loss until it is explained.

    This is the check an ordinary merge cannot perform. `git merge` reconciles TEXT; a
    definition that one parent moved to a new file and the other parent had meanwhile
    improved in place is a clean merge and a silent revert.
    """
    if not parent:
        pytest.skip(
            f"no convergence parents pinned ({_PARENT_ENV} unset): this census is "
            f"SPRINT-LOCAL scaffolding over private commits and cannot run in a public "
            f"clone. Slated for deletion in the final pass (see the ledger row).")
    if not _have(parent):
        # P34R.9 asked for a LOUD failure instead of a silent skip, and that still holds
        # wherever the census is claimed as evidence — but only there. A public clone has
        # no way to hold these private objects, so absence is the EXPECTED state and a
        # hard failure would only be red noise. The sprint sets the strict flag, which is
        # what turns a declared-but-missing pin back into a loud failure.
        if str(os.environ.get(_STRICT_ENV, "") or "").strip():
            pytest.fail(
                f"pinned parent {parent} is missing while {_STRICT_ENV} is set: the "
                f"convergence census cannot run, and skipping here is exactly how a "
                f"parent loss would go unnoticed. Fetch the phase branches or re-pin.")
        pytest.skip(f"pinned parent {parent} is not in this clone")
    parent_defs, parent_aliases = _inventory_at(parent)
    here_defs, here_aliases = _inventory_here()
    # Bare names still defined SOMEWHERE here: an alias that moved home is not a loss.
    live_names = {s.split(":", 1)[1] for s in here_defs}
    # Bare names the parent itself DEFINED. An alias of anything else is an import of an
    # external module (base64, json, ...); a module that stops importing one has lost
    # nothing, and only an alias of a REPO symbol can be a re-export worth tracking.
    parent_names = {s.split(":", 1)[1] for s in parent_defs}

    def explained(sym: str) -> bool:
        if sym in RECONCILED or ":_RunCustody." in sym:
            return True
        if sym not in parent_aliases:
            return False
        bare = sym.split(":", 1)[1]
        return bare in live_names or bare not in parent_names

    unexplained = sorted(s for s in (parent_defs | parent_aliases) - (here_defs | here_aliases)
                         if not explained(s))
    assert not unexplained, (
        f"Bindings present in parent {parent} and absent here, with no entry in "
        f"RECONCILED and no surviving definition:\n  " + "\n  ".join(unexplained))


# Every mechanism the transport phase contributed. The consult that reviewed this
# convergence listed them precisely because choosing either FILE wholesale drops them.
TRANSPORT_LIFECYCLE = {
    "ouroboros.delegate_custody": [
        "RunCustody", "replay", "lookup", "record_start_requested", "record_started",
        "open_runs", "idempotency_key", "cancel_and_verify",
        "settle_run", "retire_project", "release_task_runs", "reconcile_orphaned_runs",
        "open_containment_faults", "record_containment_fault", "resolve_containment_fault",
        "daemon_says_absent", "close_absent_run",
    ],
    "ouroboros.tools.delegate": [
        "_safe_run_filename", "_stage_full_output", "_preview_payload",
        "_delivered_terminal_payload", "_start_request", "_retire_orphaned_registration",
        "_CANCEL_NOTES", "_TIMELINE_LABEL_CHARS",
    ],
}

# Every mechanism the mutating phase contributed, in the same file the transport phase
# rewrote. Neither list is a subset of the other, which is why no branch is "the base".
MUTATING_BEHAVIOUR = {
    "ouroboros.tools.delegate": [
        "_derive_authority", "_widened_access", "_Breach", "_home_isolation_breach",
        "_containment_breach", "_containment_evidence", "_record_containment",
        "_resolved", "_mutating_run_root", "_halt_breached_run", "_host_instructions",
        "_HOST_INSTRUCTIONS", "_UNPROVEN_BOUNDARY_INSTRUCTION", "_NO_BOUNDARY_NOTE",
        "_ACCESS_RANK", "_CLAUDEXOR_MAX_SECONDS",
    ],
}


@pytest.mark.parametrize("catalog,label", [(TRANSPORT_LIFECYCLE, "transport lifecycle"),
                                           (MUTATING_BEHAVIOUR, "mutating behaviour")])
def test_both_phases_survive_the_convergence(catalog, label):
    """Named, not derived: this is the list a reviewer can argue with."""
    import importlib

    missing = []
    for module_name, names in catalog.items():
        module = importlib.import_module(module_name)
        missing += [f"{module_name}.{n}" for n in names if not hasattr(module, n)]
    assert not missing, f"{label} mechanisms lost in convergence:\n  " + "\n  ".join(missing)


def test_settlement_did_not_revert_the_cost_fixes_it_absorbed():
    """The moved function must carry the fixes the OTHER branch made to it in place.

    `_settle` moved from `tools/delegate.py` into `delegate_custody.settle_run` on one
    branch while the other branch fixed three defects in the version it was moving away
    from: an ESTIMATED charge recorded as settled, an unreported token count written as
    zero, and a projection called final over both. A move is a clean git ADD, so nothing
    conflicts and nothing fails -- this test is the only thing standing between the
    convergence and a silent three-fix revert.
    """
    import inspect

    from ouroboros import delegate_custody as custody

    # The reader answers BOTH halves at once, so no call site can ask only the amount.
    assert custody.disclosed_spend({"spendUsd": 1.5, "spendEstimated": True}) == (1.5, True)
    assert custody.disclosed_spend({"spendUsd": 0.0}) == (0.0, False)
    assert custody.disclosed_spend({}) == (None, False)

    # An unreported token count is UNKNOWN, never a confident zero.
    assert custody.disclosed_tokens(None) is None
    assert custody.disclosed_tokens(0) == 0
    assert custody.disclosed_tokens(41) == 41

    src = inspect.getsource(custody.settle_run)
    for fragment, why in [
        ("spend_estimated=estimated", "the ledger row must carry the estimated flag"),
        ("disclosed_tokens(", "token reads must keep None for an unreported count"),
        ("cached_tokens=", "the third token field the schema defines must be recorded"),
        ("not estimated", "an estimated amount must not settle as final"),
    ]:
        assert fragment in src, f"settle_run reverted a fix it absorbed: {why}"
    # The undisclosed case must not be republished as a confident 0.0 in the envelope.
    assert '"cost_usd": spend' in src, "an undisclosed spend must stay None in the envelope"


# ---------------------------------------------------------------------------
# Decisions the owner overturned, which a merge must not be able to restore.
#
# Each is a PAIR of witnesses: the superseded form, and the replacement that retired it
# on `cxi/p2-axes`. Which one may be present depends on WHICH TREE this is, and the
# guard derives that from the tree itself -- never from a flag someone can forget:
#
#   * BEFORE the p2 material arrives (this tree today, converging only the transport
#     and mutating phases), the superseded form is the expected -- and only -- state.
#   * AFTER the p2 material arrives, the replacement is the ONLY passing state. A
#     superseded form that survives that merge is a decision the owner overturned,
#     restored by merge order, and "exactly one is present" would have blessed it.
#
# "The p2 material arrived" is evidenced two ways, either sufficient:
#   * the p2 decision chain is in this tree's HISTORY (P2_AXES_CHAIN below is the
#     commit where the last of these retirements landed, so its ancestry implies all
#     of them), which also catches the coherent-reversion case where a bad merge
#     restored EVERY superseded form and deleted every replacement; or
#   * ANY replacement witness is already observable in the tree -- a cherry-picked or
#     partially merged arrival flips the era even without the commits, so the pairs it
#     did NOT update fail loudly instead of reading as "legitimately pre-p2".
#
# Within the required era, both failure directions remain:
#
#   both present  -> a merge kept the old form beside the new one. The old one still
#                    runs, and nothing else would say so.
#   neither       -> the decision EVAPORATED. A deduplication pass that deleted the
#                    surviving implementation instead of the duplicate lands here, and
#                    that loss class has already happened on this sprint.
# ---------------------------------------------------------------------------

# v6.87.28 on `cxi/p2-axes`: the commit that landed the LAST of the retirements below
# (D2's RETIRED_SCHEDULE_PARAMS and the D4 chain; D3's inheritance landed in v6.87.26,
# its ancestor). Pinned like the parent SHAs above: branch names float, commits do not.
P2_AXES_CHAIN = _PINNED[3] if len(_PINNED) > 3 else ""


def _p2_chain_in_history() -> bool:
    """Is the p2 decision chain an ancestor of this tree's HEAD?

    False when the commit is simply not in this clone (shallow / grafted): ancestry is
    a SUFFICIENT arrival signal, never a necessary one -- witness evidence still flips
    the era on its own.
    """
    if not P2_AXES_CHAIN:
        return False                    # no pin supplied: ancestry says nothing
    probe = subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", P2_AXES_CHAIN, "HEAD"],
        capture_output=True, text=True)
    return probe.returncode == 0


def _overturned_verdicts(witnesses, p2_chain_in_history):
    """Desired-state verdicts for a whole witness table. Pure over its inputs.

    ``witnesses`` is ``[(name, superseded_present, replacement_present), ...]``; the
    era is derived HERE, from the table plus history, so no caller can hand a pair an
    era that contradicts the evidence. Returns ``(p2_material_arrived, {name:
    failure_reason_or_None})``.
    """
    arrived = p2_chain_in_history or any(new for _, _, new in witnesses)
    verdicts = {}
    for name, old, new in witnesses:
        if old and new:
            verdicts[name] = (
                "BOTH are present: a merge kept the retired form beside the one that "
                "replaced it, and the retired form still runs.")
        elif not old and not new:
            verdicts[name] = (
                "NEITHER is present: the decision has evaporated. Something deleted "
                "the surviving implementation, not the duplicate.")
        elif old and arrived:
            verdicts[name] = (
                "the p2 material has arrived ("
                + ("the p2 decision chain is in this tree's history"
                   if p2_chain_in_history else
                   "another pair already shows its replacement")
                + "), yet the SUPERSEDED form is what survived here: the merge "
                  "restored a decision the owner overturned.")
        else:
            verdicts[name] = None
    return arrived, verdicts


def _public_effort_is_published() -> bool:
    from ouroboros.tools.control import schedule_subagent_properties

    return "effort" in schedule_subagent_properties()


def _effort_is_retired() -> bool:
    import ouroboros.tools.control as control

    return "effort" in getattr(control, "RETIRED_SCHEDULE_PARAMS", {})


def _omitted_lane_collapses_to_light() -> bool:
    import inspect

    from ouroboros.subagents import resolve_subagent_lane

    return "parent_lane" not in inspect.signature(resolve_subagent_lane).parameters


def _omitted_lane_inherits_the_parents() -> bool:
    import inspect

    from ouroboros.subagents import resolve_subagent_lane

    return "parent_lane" in inspect.signature(resolve_subagent_lane).parameters


def _capability_delta_chain_is_absent() -> bool:
    import ouroboros.subagents as subagents

    return not hasattr(subagents, "capability_delta_notice")


def _capability_delta_chain_is_present() -> bool:
    """Present means the WHOLE chain: durable record, child's prompt, parent's result."""
    import ouroboros.agent as agent
    import ouroboros.subagents as subagents
    import ouroboros.tools.control as control

    return (hasattr(subagents, "capability_delta_notice")
            and hasattr(agent, "capability_delta_prompt_block")
            and hasattr(control, "disclosable_capability_delta"))


OVERTURNED = [
    ("D2 a public `effort` parameter on schedule_subagent",
     _public_effort_is_published, _effort_is_retired,
     "effort is derived from the owner's configured effort for the task type; a public "
     "knob was a second answer to the question model_lane already answers"),
    ("D3 an omitted model_lane collapsing to Light",
     _omitted_lane_collapses_to_light, _omitted_lane_inherits_the_parents,
     "an omitted lane INHERITS the parent's: a Heavy parent handing a child a piece of "
     "its own job must not get a Light child with no signal that it happened"),
    ("D4 no capability_delta reaching the record, the prompt and the result",
     _capability_delta_chain_is_absent, _capability_delta_chain_is_present,
     "a child that runs below what was asked for must say so, in the durable record, in "
     "its own prompt, and in what its parent reads back"),
]


def _witness_table():
    return [(row[0], row[1](), row[2]()) for row in OVERTURNED]


@pytest.mark.parametrize("name,superseded,replacement,decision",
                         OVERTURNED, ids=[row[0].split()[0] for row in OVERTURNED])
def test_a_merge_cannot_restore_a_decision_the_owner_overturned(
        name, superseded, replacement, decision):
    old, new = superseded(), replacement()
    arrived, verdicts = _overturned_verdicts(_witness_table(), _p2_chain_in_history())
    reason = verdicts[name]
    assert reason is None, (
        f"{name}\n"
        f"  the owner's decision: {decision}\n"
        f"  superseded form present: {old}\n"
        f"  replacement present:     {new}\n"
        f"  p2 material arrived:     {arrived}\n"
        f"  {reason}\n"
        "  Resolve by hand -- do not silence this by deleting the branch you did not "
        "expect to see.")


# All four quadrants of one witness pair, in both eras, against the SAME function the
# live guard runs. The pair under test is fed alongside a neighbour so the mixed-tree
# derivation is exercised too, not just the chain-ancestry one.
@pytest.mark.parametrize("old,new,chain,ok", [
    # -- before the p2 material arrives -------------------------------------------
    (True,  False, False, True),    # superseded-only: this tree today. Expected.
    (False, False, False, False),   # neither: evaporated, era cannot excuse it
    (True,  True,  False, False),   # both: the replacement's presence itself flips
                                    # the era, and coexistence fails in any era
    (False, True,  False, True),    # replacement-only: its own presence IS arrival
                                    # evidence (cherry-pick with no commits), and
                                    # replacement-only is the desired end state
    # -- after the p2 material arrives (chain in history) -------------------------
    (True,  False, True,  False),   # THE CASE "exactly one" BLESSED: the merge
                                    # restored the overturned form. Must fail.
    (False, True,  True,  True),    # replacement-only: the ONLY passing state
    (True,  True,  True,  False),   # both: still fails
    (False, False, True,  False),   # neither: still fails
], ids=["pre-superseded-only", "pre-neither", "pre-both", "pre-replacement-only",
        "post-superseded-only", "post-replacement-only", "post-both", "post-neither"])
def test_overturned_guard_quadrants(old, new, chain, ok):
    witnesses = [("pair", old, new)]
    _, verdicts = _overturned_verdicts(witnesses, p2_chain_in_history=chain)
    assert (verdicts["pair"] is None) is ok, verdicts["pair"]


def test_partial_arrival_fails_the_pairs_it_skipped():
    """A cherry-picked p2 arrival must not leave the other pairs reading as pre-p2.

    One pair showing its replacement is arrival evidence for the WHOLE table: the
    pair still in superseded form fails as a restoration even though the p2 commits
    are nowhere in history.
    """
    arrived, verdicts = _overturned_verdicts(
        [("landed", False, True), ("skipped", True, False)],
        p2_chain_in_history=False)
    assert arrived is True
    assert verdicts["landed"] is None
    assert verdicts["skipped"] is not None
    assert "another pair already shows its replacement" in verdicts["skipped"]


def test_coherent_reversion_is_caught_by_the_chain():
    """A merge that restored EVERY superseded form and deleted every replacement looks
    exactly like the legitimate pre-p2 tree to the witnesses alone; the pinned decision
    chain in history is what refuses it."""
    table = [("D2", True, False), ("D3", True, False), ("D4", True, False)]
    pre_arrived, pre = _overturned_verdicts(table, p2_chain_in_history=False)
    post_arrived, post = _overturned_verdicts(table, p2_chain_in_history=True)
    assert pre_arrived is False and all(v is None for v in pre.values())
    assert post_arrived is True and all(v is not None for v in post.values())
    assert all("history" in v for v in post.values())
