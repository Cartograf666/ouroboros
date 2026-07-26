"""ONE deterministic structural gate over EVERY benchmark launcher, for three invariants.

Six review rounds found the same two mistakes in four different launchers each, so the answer
is not four more patches: it is a gate that answers the question for the whole family at once.
It reads sources with ``ast`` — no imports, no execution, no network — so it is cheap enough to
run as an ordinary test and deterministic enough to be a release gate.

INVARIANT A — ADMISSION IS THE OUTER BOUNDARY.
    Nothing that can FAIL may happen before ``admit_benchmark_run()`` has PERSISTED its
    manifest. Not just mutation: a run that dies while reading its dataset leaves no manifest
    at all, so it is INVISIBLE rather than merely footprint-free, which is strictly worse for a
    provenance system. Enforced by walking the statements that precede the admission call —
    INCLUDING the admission call's own argument list, because Python evaluates arguments before
    entering the callee, so a probe nested there runs while there is still nothing on disk —
    and denying every callee whose EFFECT is a mutation, a content read/parse, a network reach,
    a deferred non-stdlib import, or a refusal that depends on probed state. Effects are
    resolved through helper definitions, LOCAL **and IMPORTED**. The imported hop is the
    round-6 lesson: ``ensure_outside_repo`` *creates* the directory it validates and slipped
    past a purely local resolver for six rounds, caught only because somebody had thought to
    name it in the denylist. Names are a list of yesterday's bugs; resolution is the rule. So
    the two ``ensure_*`` names were REMOVED from the denylist and are now caught by what their
    bodies DO (``mkdir``), one module over.

    The line the invariant draws, stated once so it is not re-litigated per launcher:
    ARGUMENT-SHAPED work may precede admission, WORLD-SHAPED work may not. Argument parsing and
    pure path arithmetic (including the confinement primitives, which compute the very path the
    manifest is written to) refuse as a deterministic function of argv and are diagnosable from
    the command line alone. Reading a file, parsing it, importing a dataset library or asking
    the network cannot be reconstructed from argv, so those refusals are exactly the ones that
    need a durable record. A bare EXISTENCE probe (``exists``/``is_file``/``stat``) is the one
    permitted middle: it reads no content and cannot fail on malformed input, which is what
    makes ``scored_claim_state`` a legitimate footprint-free step-aside. It becomes a violation
    the moment the helper holding it can RAISE — probing may not refuse, and refusing may not
    probe.

INVARIANT B — CONFINEMENT IS COMPUTED FROM THE ACTIVE CHECKOUT.
    A launcher whose run provenance is attested against a checkout it was HANDED (``--repo-dir``,
    ``--ouroboros-clone``) must confine its output paths against that same checkout, never
    against a statically derived repo root. Round 6 found two: ``confined_claims_dir`` consulted
    only ``repo_root_from_devtools()``, so ``--repo-dir /other/clone --claim-dir
    /other/clone/.claims`` wrote lock and marker state straight into the execution checkout; and
    ``run_clb.main`` validated ``--out-dir`` against its own ``REPO``, letting admission
    artefacts land inside the very seed being attested. A launcher that attests a STATIC root
    (the in-repo prediction writers) is consistent when it confines against that same static
    root, and is not flagged: the invariant is agreement with the attested checkout, not a ban
    on constants.

    The same authority mistake also wears a second shape, which a smoke found in the field and
    this gate initially missed: a REFUSAL comparing a path against a ``__file__``-derived module
    root. ``run_clb.refuse_live_repo_clone`` compared ``--ouroboros-clone`` against its own
    ``REPO``, so running a pinned seed's own launcher against that same seed — the recipe the
    methodology prescribes — was refused, while the live repo it meant to protect is only that
    tree in the development case. "Wherever I am executing from" is never the authority; the
    live runtime (or the handed checkout) is. So an ``if <__file__-derived root>: raise`` inside
    a dynamically attested launcher is reported here too.

INVARIANT C — THE FINALIZATION SEAM'S EXIT IS THE ONLY PUBLISHER.
    ``finalize_run_manifest`` merges the terminal ``outcome``/``exit_code``/``refusal`` into the
    manifest when its context EXITS. A launcher that writes a manifest file from INSIDE that
    context therefore publishes a PRE-MERGE record: for a refusal, the admission seam's generic
    payload saying ``exit_code`` 1 while the process will really exit 2. The final artefact is
    corrected a moment later, which is what let this survive two review rounds — but the
    intermediate state is observable (OSWorld runs multi-lane, and the canonical per-task
    manifest exists precisely to serve a concurrent reader), and an interruption inside the
    window leaves the wrong record durably. Every such write is also redundant: the seam writes
    the same path on EVERY exit path, including an escaping exception.

    Rounds 9 and 10 fixed this twice in ``run_cu_bridge_agent``, and a by-hand sweep of the other
    launchers still missed ``run_step_agent`` — because the sweep asked "is there a second copy
    that can go stale?" when the hazard is "is anything published before the merge?", which is
    true of a single-path launcher too. So it is a gate now, not a sweep.

    Judged by EFFECT, like Invariant A: a call is a publication when following its body (local
    or imported) reaches a filesystem write primitive whose arguments NAME a run-manifest
    artefact. The offenders are called ``_write_task_records`` and ``_write_cu_outcome`` — named
    for the records they keep, not for the manifest they published — so a check keyed on the
    callee name would have found neither, and would miss the next helper that wraps ``write_json``.

All three invariants are checked from source text, so a SYNTHETIC violating launcher can be
audited through the same entry point (``audit_source``) as the real ones — a gate that passes
only because today's code happens to be clean tells us nothing about tomorrow's.
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
BENCH_ROOT = REPO_ROOT / "devtools" / "benchmarks"

ADMISSION_CALLEE = "admit_benchmark_run"

# Launchers under the admission contract. Named, so a new launcher cannot join silently and the
# ones whose migration belongs to a LATER phase cannot be silently claimed.
MIGRATED_LAUNCHERS: tuple[str, ...] = (
    "programbench/run_programbench.py",
    "programbench/run_programbench_e2e.py",
    "swe_bench/swebench_predictions.py",
    "swe_bench_pro/pro_predictions.py",
    "harness_bench_fast/run_harness_bench_fast.py",
    "swe_bench_pro/e1v2/run_pro.py",
    "swe_bench_pro/e1v2/auto_run.py",
    "continual_learning/run_clb.py",
    "osworld/run_step_agent.py",
    "osworld/run_cu_bridge_agent.py",
    "osworld/osworld_adapter_skeleton.py",
    "gaia/run_gaia.py",
    "terminal_bench/run_tb.py",
    "terminal_bench/run_harbor_smoke.py",
)

# GAIA and both Terminal-Bench launchers were the last pre-seam holdouts and migrated in
# v6.79.0, so the pending set is now EMPTY. It stays declared: a launcher added later that is
# not yet under the contract belongs here, named, rather than simply missing from both lists.
PENDING_LAUNCHERS: tuple[str, ...] = ()


def launcher_paths() -> list[pathlib.Path]:
    return [BENCH_ROOT / rel for rel in MIGRATED_LAUNCHERS]


# --------------------------------------------------------------------------- #
# Invariant A: the pre-admission denylist and its cross-module resolver
# --------------------------------------------------------------------------- #

PRE_ADMISSION_DENIED_NAMES = frozenset({
    # Deliberately NOT here: `ensure_outside_repo` / `ensure_file_output_outside_repo`. They
    # mutate (they mkdir the directory they validate) and they are IMPORTED, which is exactly
    # the combination that defeated the old local-only resolver. Leaving them unnamed is the
    # point: the resolver below reads their bodies and reports `ensure_outside_repo -> mkdir`,
    # so the NEXT imported mutator nobody thought to enumerate is caught the same way.
    "assert_seed_is_git_directory", "ensure_util_image", "dump_state", "runtime_attestation",
    "snapshot", "restore", "reflections", "seed_stamp", "run_one", "run_instance",
    "preflight_cleanroom_container", "prepare_seeded_workspace", "create_submission_tarball",
    "run_official_eval", "write_json", "write_jsonl", "write_result_index", "append_result_index",
    "write_text", "write_bytes", "mkdir", "unlink", "rmtree", "touch", "rename", "chmod",
    "urlopen", "urlretrieve", "Popen", "check_output", "check_call",
    # READS AND PARSES. These are the round-7 widening: none of them mutates anything, and
    # every one of them can take the run down before a manifest exists — a missing dataset
    # file, an unreadable settings.json, a malformed JSONL row. `read_text` sitting one hop
    # inside `_records`/`_rows`/`preflight_model_slots` is how four migrated launchers still
    # did dataset discovery outside the boundary they claimed to enforce.
    "read_text", "read_bytes", "read_json", "read_jsonl", "open", "load", "load_dataset",
    "iterdir", "glob", "rglob", "walk", "listdir", "scandir",
})
# Matched as a prefix on any dotted segment, so `docker_pull_if_missing` / `subprocess.run` /
# `shutil.rmtree` / `requests.get` are all caught without enumerating them.
PRE_ADMISSION_DENIED_PREFIXES = ("subprocess", "docker", "shutil", "requests", "httpx",
                                 "aiohttp", "socket")

# EXISTENCE probes. Not denied on their own — they read no content and cannot fail on a
# malformed input, which is what lets `scored_claim_state` answer "another lane already scored
# this" and step aside leaving zero footprint. They ARE denied inside a helper that can raise:
# see `_helper_effects`. Probing may not refuse; refusing may not probe.
STATE_PROBE_NAMES = frozenset({
    "exists", "is_file", "is_dir", "is_symlink", "is_mount", "stat", "lstat", "samefile",
})

# Modules the resolver will open. First-party only: stdlib and third-party callees stay
# unresolved and are covered by the name/prefix denylist, which keeps the gate hermetic.
RESOLVABLE_PACKAGES = ("devtools.", "ouroboros.")
# A function-level import inside a pre-admission helper is a deferred dependency, and it is
# deferred precisely because it is heavy or optional: `datasets` in `load_pro_rows`,
# `programbench.utils` in `_load_instances`. Its ImportError is a pre-manifest death. Stdlib
# and first-party imports are exempt — `from urllib.parse import urlparse` inside a pure
# predicate is a style choice, not a dependency on the state of the world.
_STDLIB_MODULES = frozenset(getattr(sys, "stdlib_module_names", ()))
_FIRST_PARTY_ROOTS = frozenset(package.rstrip(".") for package in RESOLVABLE_PACKAGES)


def _dotted_callee(node: ast.expr) -> str:
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def denied_pre_admission_call(dotted: str) -> str:
    """The denied token in ``dotted``, or ``""``. Pure name/prefix matching, no resolution."""
    segments = dotted.split(".")
    hits = PRE_ADMISSION_DENIED_NAMES.intersection(segments)
    if hits:
        return sorted(hits)[0]
    for segment in segments:
        for prefix in PRE_ADMISSION_DENIED_PREFIXES:
            if segment.startswith(prefix):
                return segment
    return ""


def _terminates(statement: ast.stmt) -> bool:
    """True if ``statement`` is a branch that ALWAYS leaves the function (return/raise).

    Calls inside such a branch are not on the path to admission — they are the deliberate
    step-aside paths (`--collect-only` re-normalising an existing run dir, "another lane owns
    this task", "these output paths are not confined") that exist precisely to leave NO
    footprint and have no run to record anything against. The branch's TEST expression is
    still walked, because that runs on the way past.
    """
    if isinstance(statement, (ast.Return, ast.Raise)):
        return True
    if isinstance(statement, ast.If) and statement.body:
        return _terminates(statement.body[-1]) and not statement.orelse
    return False


def _walk_calls(statement: ast.stmt) -> list[str]:
    """Dotted callees of ``statement``, skipping the bodies of always-terminating branches."""
    if isinstance(statement, ast.If) and _terminates(statement):
        return [_dotted_callee(node.func) for node in ast.walk(statement.test)
                if isinstance(node, ast.Call)]
    return [_dotted_callee(node.func) for node in ast.walk(statement)
            if isinstance(node, ast.Call)]


def _admission_argument_calls(statement: ast.stmt, stop_callee: str) -> list[str]:
    """Dotted callees evaluated INSIDE the ``stop_callee(...)`` argument list.

    Python evaluates arguments before entering the callee, so anything nested here runs while
    the manifest is still unwritten — it is pre-admission work that merely LOOKS like part of
    admission. `pro_predictions` read every ``--attestation`` file from this position and the
    old gate, which stopped walking at the statement containing the admission call, could not
    see it. Only the argument subtrees are walked: the rest of the statement (an ``except``
    handler recording the refusal, the assignment target) is genuinely post-admission.
    """
    found: list[str] = []
    for node in ast.walk(statement):
        if not (isinstance(node, ast.Call) and _dotted_callee(node.func).endswith(stop_callee)):
            continue
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            found.extend(_dotted_callee(inner.func) for inner in ast.walk(argument)
                         if isinstance(inner, ast.Call))
    return found


def calls_before(function: ast.FunctionDef, stop_callee: str) -> list[str]:
    """Dotted callee names in the statements of ``function`` that PRECEDE ``stop_callee``.

    "Precede" includes the admission call's own arguments, which are evaluated first.
    """
    seen: list[str] = []
    for statement in function.body:
        if any(isinstance(node, ast.Call) and _dotted_callee(node.func).endswith(stop_callee)
               for node in ast.walk(statement)):
            return seen + _admission_argument_calls(statement, stop_callee)
        seen.extend(_walk_calls(statement))
    raise LauncherAuditError(f"{function.name}() never calls {stop_callee}")


class LauncherAuditError(RuntimeError):
    """The audited source does not have the shape the gate can reason about."""


class _Unit:
    """One parsed module: its function definitions and the modules its names came from."""

    __slots__ = ("tree", "functions", "imports", "module_assigns", "import_bound", "name",
                 "file_roots")

    def __init__(self, tree: ast.Module, name: str) -> None:
        self.tree = tree
        self.name = name
        self.functions: dict[str, ast.FunctionDef] = {
            node.name: node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }  # type: ignore[misc]
        # Function-level imports count: the OSWorld launchers import their shared claim helpers
        # inside the functions that use them, and an import the resolver cannot see is an
        # imported mutator it cannot follow.
        self.imports: dict[str, str] = {}
        self.import_bound: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    bound = alias.asname or alias.name
                    self.import_bound.add(bound)
                    self.imports[bound] = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.import_bound.add((alias.asname or alias.name).split(".")[0])
        self.module_assigns: set[str] = set()
        # Module constants derived from ``__file__``: "wherever this file happens to live".
        # Invariant B's second shape reports one of these being used as a REFUSAL authority.
        self.file_roots: set[str] = set()
        for statement in tree.body:
            targets = (statement.targets if isinstance(statement, ast.Assign)
                       else [statement.target] if isinstance(statement, ast.AnnAssign) else [])
            from_file = any(isinstance(node, ast.Name) and node.id == "__file__"
                            for node in ast.walk(statement))
            for target in targets:
                for node in ast.walk(target):
                    if isinstance(node, ast.Name):
                        self.module_assigns.add(node.id)
                        if from_file:
                            self.file_roots.add(node.id)


@functools.lru_cache(maxsize=None)
def _unit_for_module(module: str) -> _Unit | None:
    if not module.startswith(RESOLVABLE_PACKAGES):
        return None
    path = REPO_ROOT.joinpath(*module.split("."))
    for candidate in (path.with_suffix(".py"), path / "__init__.py"):
        if candidate.is_file():
            return _Unit(ast.parse(candidate.read_text(encoding="utf-8")), module)
    return None


def _imported_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        return [node.module] if node.module and not node.level else []
    return [alias.name for alias in node.names]


def _raises(function: ast.FunctionDef) -> bool:
    """True if ``function``'s own body can refuse (``raise`` / ``sys.exit``)."""
    for node in ast.walk(function):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call) and _dotted_callee(node.func).split(".")[-1] == "exit":
            return True
    return False


def _helper_effects(function: ast.FunctionDef) -> str:
    """Effects of a helper BODY that no callee name can express, or ``""``.

    Two of them, both round-7 additions:

    * a DEFERRED non-stdlib import — the helper pulls in a dataset library (``datasets``,
      ``programbench.utils``) whose ``ImportError`` kills the process while nothing is on disk.
      Purely name-based resolution is blind to this: the offending statement is not a call.
    * a REFUSAL THAT DEPENDS ON PROBED STATE — the helper both raises and asks the filesystem
      whether something exists. Either alone is fine pre-admission (argv-shaped refusals are
      reproducible from the command line; a probe that only returns a value is the documented
      footprint-free step-aside); together they are a refusal no argv can explain, which is
      precisely the class that needs a durable manifest.
    """
    for node in ast.walk(function):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for module in _imported_modules(node):
            root = module.split(".")[0]
            if root in _STDLIB_MODULES or root in _FIRST_PARTY_ROOTS:
                continue
            return f"deferred import {module}"
    if _raises(function) and any(
            isinstance(node, ast.Call)
            and _dotted_callee(node.func).split(".")[-1] in STATE_PROBE_NAMES
            for node in ast.walk(function)):
        return "refuses on probed state"
    return ""


def resolve_denied(dotted: str, unit: _Unit, *, depth: int = 2) -> str:
    """Denied token for ``dotted``, resolving ONE hop through helper bodies.

    The hop crosses module boundaries. A callee defined in this module is resolved from this
    module's source (that is the old behaviour: `_ensure_vmrun_on_path` probing the filesystem,
    `_install_optional_dependency_stubs` mutating `sys.modules`, `repo_provenance` shelling out
    to git — none of them named, all of them real pre-admission work). A callee IMPORTED from a
    first-party module is resolved from THAT module's source, which is what `ensure_outside_repo`
    needed and never got. Offenders are reported as ``helper -> token`` so the report names the
    hop, not just the leaf.

    ONE hop is resolved BY DESIGN and the depth is asserted in the tests rather than implied: a
    two-hop chain is out of the gate's reach and stays a review question.
    """
    direct = denied_pre_admission_call(dotted)
    if direct:
        return direct
    if depth <= 0:
        return ""
    leaf = dotted.split(".")[-1]
    target = unit.functions.get(leaf)
    target_unit = unit
    if target is None:
        imported = _unit_for_module(unit.imports.get(leaf, ""))
        if imported is None:
            return ""
        target = imported.functions.get(leaf)
        target_unit = imported
        if target is None:
            return ""
    # NOTE the confinement primitives get NO exemption here. `assert_outside_repo` clears the
    # effect check on its own merits (it refuses on pure path arithmetic and probes nothing),
    # and `ensure_outside_repo` must stay caught by the `mkdir` in its body — that is the
    # round-6 finding, and an exemption keyed on the name would hand it straight back.
    effect = _helper_effects(target)
    if effect:
        return f"{target.name} -> {effect}"
    for node in ast.walk(target):
        if not isinstance(node, ast.Call):
            continue
        inner = resolve_denied(_dotted_callee(node.func), target_unit, depth=depth - 1)
        if inner:
            return f"{target.name} -> {inner}"
    return ""


def _pre_admission_violations(unit: _Unit) -> list[str]:
    owners = [node.name for node in ast.walk(unit.tree)
              if isinstance(node, ast.FunctionDef)
              and any(isinstance(inner, ast.Call)
                      and _dotted_callee(inner.func).endswith(ADMISSION_CALLEE)
                      for inner in ast.walk(node))]
    if not owners:
        return [f"{unit.name}: bypasses the admission seam ({ADMISSION_CALLEE} is never called)"]
    owner = owners[0]
    prefix = calls_before(unit.functions[owner], ADMISSION_CALLEE)
    if owner != "main" and "main" in unit.functions:
        prefix += calls_before(unit.functions["main"], owner)
    violations: list[str] = []
    for dotted in prefix:
        denied = resolve_denied(dotted, unit)
        if denied:
            violations.append(
                f"{unit.name}: {dotted}() runs BEFORE {ADMISSION_CALLEE}() in {owner}() -- a "
                f"refusal there leaves no durable manifest (denied token: {denied})"
            )
    return violations


# --------------------------------------------------------------------------- #
# Invariant B: confinement authority must come from the attested checkout
# --------------------------------------------------------------------------- #

# The path-confinement primitives. ``_outside`` is the OSWorld skeleton's local form of the same
# predicate; naming it here keeps that launcher inside the invariant instead of outside it.
CONFINEMENT_PRIMITIVES = frozenset({
    "assert_outside_repo", "ensure_outside_repo",
    "assert_file_output_outside_repo", "ensure_file_output_outside_repo",
    "_outside",
})
# Helpers that DERIVE a repo root from the module's own location. Their result is the same value
# whatever checkout is actually executing, which is precisely why it cannot be the sole
# authority for a launcher that was handed one.
STATIC_ROOT_HELPERS = frozenset({
    "repo_root_from_devtools", "workspace_root_from_devtools",
})


def _local_assigns(function: ast.FunctionDef) -> dict[str, list[ast.expr]]:
    """Name -> the expressions it is bound from, inside one function (assignments and loops)."""
    bound: dict[str, list[ast.expr]] = {}

    def _add(target: ast.expr, value: ast.expr) -> None:
        for node in ast.walk(target):
            if isinstance(node, ast.Name):
                bound.setdefault(node.id, []).append(value)

    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                _add(target, node.value)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            _add(node.target, node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _add(node.target, node.iter)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            _add(node.optional_vars, node.context_expr)
    return bound


def _authority_tokens(expr: ast.expr, unit: _Unit,
                      assigns: dict[str, list[ast.expr]],
                      seen: tuple[str, ...] = ()) -> set[tuple[str, str]]:
    """Classify every value the authority expression is built from as ``static`` or ``dyn``.

    ``static`` means module scope: a module-level constant, or a call to a helper that derives a
    root from ``__file__``. ``dyn`` means the value came from the function's own inputs — a
    parameter, an ``args.*`` CLI value, a local derived from either. Names bound by imports are
    modules and functions, not authorities, so they are skipped (a call to a static-root helper
    is still recorded, from the call node).
    """
    tokens: set[tuple[str, str]] = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.Call):
            # `ast.walk` continues into the arguments regardless: a wrapped authority
            # (`Path(args.repo_dir).resolve()`) must still contribute its inner names.
            callee = _dotted_callee(node.func).split(".")[-1]
            if callee in STATIC_ROOT_HELPERS:
                tokens.add(("static", callee))
        if not isinstance(node, ast.Name):
            continue
        name = node.id
        if name in STATIC_ROOT_HELPERS or name in unit.import_bound or name in unit.functions:
            continue
        if name in assigns and name not in seen:
            inner: set[tuple[str, str]] = set()
            for value in assigns[name]:
                inner |= _authority_tokens(value, unit, assigns, seen + (name,))
            # A local whose sources resolve to nothing recognisable is still an input-derived
            # value, not a constant: report it as dynamic under its own name rather than
            # collapsing to "no tokens", which a vacuous all() would read as static.
            tokens |= inner or {("dyn", name)}
        elif name in unit.module_assigns:
            tokens.add(("static", name))
        else:
            tokens.add(("dyn", name))
    return tokens


def _authority_arg(call: ast.Call) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg in ("repo_dir", "repo_root", "forbidden"):
            return keyword.value
    return call.args[1] if len(call.args) > 1 else None


def _confinement_calls(function: ast.FunctionDef) -> list[ast.Call]:
    return [node for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and _dotted_callee(node.func).split(".")[-1] in CONFINEMENT_PRIMITIVES]


def _attested_authority(unit: _Unit) -> set[tuple[str, str]]:
    """Tokens of the ``repo_dir=`` the launcher attests in its admission call."""
    for function in unit.functions.values():
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call)
                    and _dotted_callee(node.func).endswith(ADMISSION_CALLEE)):
                continue
            for keyword in node.keywords:
                if keyword.arg == "repo_dir":
                    return _authority_tokens(keyword.value, unit, _local_assigns(function))
    return set()


def _refusal_authority_violations(unit: _Unit) -> list[str]:
    """Refusals whose authority is a ``__file__``-derived module root.

    ``if <path> == REPO: raise`` asks "am I being pointed at the tree I am executing from?",
    which is only the same question as "am I being pointed at the LIVE repo" when the launcher
    runs from the live workspace. Ship the launcher inside a pinned seed and the two diverge:
    `run_clb.refuse_live_repo_clone` refused the seed's own launcher being handed that seed,
    which is the documented pinned-seed recipe, while the live repo it exists to protect went
    unmentioned. Same class as the ``confined_claims_dir`` finding, different syntax — a
    comparison instead of a call — which is why it needs its own detector.
    """
    violations: list[str] = []
    for name, function in sorted(unit.functions.items()):
        for node in ast.walk(function):
            if not (isinstance(node, ast.If)
                    and any(isinstance(inner, ast.Raise) for inner in ast.walk(node))):
                continue
            roots = sorted({inner.id for inner in ast.walk(node.test)
                            if isinstance(inner, ast.Name) and inner.id in unit.file_roots})
            if roots:
                violations.append(
                    f"{unit.name}: {name}() REFUSES against {roots} -- a module root derived "
                    f"from __file__, i.e. the tree this launcher happens to execute from. "
                    f"Refuse against the LIVE runtime (or the handed checkout) instead: the "
                    f"two coincide only in the development layout"
                )
    return violations


def _confinement_violations(unit: _Unit) -> list[str]:
    attested = _attested_authority(unit)
    if not any(kind == "dyn" for kind, _ in attested):
        # The run's provenance is attested against a statically derived root, so confining
        # against that same root AGREES with the record. Nothing to enforce.
        return []
    violations: list[str] = _refusal_authority_violations(unit)
    for name, function in sorted(unit.functions.items()):
        calls = _confinement_calls(function)
        if not calls:
            continue
        assigns = _local_assigns(function)
        seen_dynamic = False
        static_only: list[str] = []
        params = {arg.arg for arg in function.args.args + function.args.kwonlyargs}
        for call in calls:
            authority = _authority_arg(call)
            tokens = _authority_tokens(authority, unit, assigns) if authority is not None else set()
            if any(kind == "dyn" for kind, _ in tokens):
                seen_dynamic = True
                if params & {"repo_dir", "repo_root"} and not (
                        {token for kind, token in tokens if kind == "dyn"}
                        & (params | {"args", "config"})):
                    violations.append(
                        f"{unit.name}: {name}() takes a checkout parameter "
                        f"({sorted(params & {'repo_dir', 'repo_root'})}) but confines against "
                        f"{sorted(token for _k, token in tokens)} instead of using it"
                    )
            elif tokens:
                static_only.append(_dotted_callee(call.func).split(".")[-1])
        if not seen_dynamic and static_only:
            violations.append(
                f"{unit.name}: {name}() confines paths ONLY against module scope "
                f"({', '.join(sorted(set(static_only)))} -> "
                f"{sorted(token for _k, token in _authority_tokens(_authority_arg(calls[0]), unit, assigns))}) "
                f"while the run is attested against a checkout it was handed -- pass that "
                f"checkout in as the confinement authority"
            )
    return violations


# --------------------------------------------------------------------------- #
# Invariant C: only the finalization seam's EXIT may publish a manifest
# --------------------------------------------------------------------------- #

FINALIZE_CALLEE = "finalize_run_manifest"

# The protected artefact, matched on the FILENAME a launcher names rather than on the helper that
# writes it. Every manifest in the family is a `*run_manifest*.json`: `run_manifest.json`,
# `task_run_manifest.json`, `auto_run_manifest.json`, `<output>.run_manifest.json`.
MANIFEST_ARTEFACT = "run_manifest"

# Marker for a write the gate could not place. Fail CLOSED: reported, never assumed harmless.
UNRESOLVED_WRITE = "UNRESOLVED write form"

# WHERE each write primitive lives. Grounding out in primitives is deliberately the same design
# as Invariant A: the leaf set is short and everything ABOVE it is resolved by reading bodies, so
# the next helper that wraps one of these is caught without anyone having to name it.
# `finalize_run_manifest`'s own write is not reachable this way -- it writes the path it was
# HANDED and names no artefact, which is exactly the difference between publishing on exit and
# publishing early.
#
# This table says what a write IS. It says NOTHING about argument layout: that is read from each
# primitive's REAL signature below. A hand-enumerated position table is a list of the call forms
# somebody thought of, and the first cut of this invariant proved it -- it mapped `rename` to
# argument 0 when `os.rename(src, dst)` publishes to argument 1, and gave standalone
# `write_text(path, ...)` no positional destination at all, so an in-seam
# `os.rename(tmp, run_dir / "run_manifest.json")` went unreported. A gate whose subject is
# incomplete models of where a write goes may not carry one. A signature cannot drift out of step
# with the callable it belongs to.
_PRIMITIVE_HOMES: dict[str, tuple[tuple[str, str], ...]] = {
    # leaf -> ((kind, home), ...). "py": import the STDLIB owner and introspect it. "ast": read
    # the first-party source with the same resolver the rest of the gate uses, so no first-party
    # package is imported and the gate stays hermetic.
    "write_json":        (("ast", "devtools.benchmarks.common.manifests"),),
    "write_jsonl":       (("ast", "devtools.benchmarks.common.manifests"),),
    "atomic_write_json": (("ast", "ouroboros.utils"),),
    "write_text_atomic": (("ast", "ouroboros.utils"),),
    "write_text":        (("py", "pathlib.Path"),),
    "write_bytes":       (("py", "pathlib.Path"),),
    "dump":              (("py", "json"),),
    # Two real callables share each of these leaves, so BOTH signatures are consulted and their
    # destinations unioned -- `os.rename(src, dst)` and `pathlib.Path.rename(self, target)` are
    # different shapes wearing one name, and picking one was the hole.
    "rename":            (("py", "os"), ("py", "pathlib.Path")),
    "replace":           (("py", "os"), ("py", "pathlib.Path")),
    "move":              (("py", "shutil"),),
}
WRITE_PRIMITIVES = frozenset(_PRIMITIVE_HOMES)

# Parameter names that denote a FILE the call acts on, matched against the REAL signature's own
# parameter names rather than against argument positions. `src` counts alongside `dst`: a move
# publishes at both endpoints, and a gate that guesses which end matters is guessing again.
_DESTINATION_PARAMETERS = frozenset({
    "self", "path", "dst", "dest", "destination", "target", "src", "source",
    "file", "filename", "fp",
})


def _stdlib_signature(home: str, leaf: str) -> list[tuple[tuple[str, ...], frozenset[str]]]:
    """``leaf``'s real parameter names on a STDLIB owner, via ``inspect``.

    Restricted to the standard library on purpose: the gate must not import a benchmark package
    to audit it. First-party primitives go through ``_source_signature`` instead.
    """
    module_name, _, attribute = home.partition(".")
    if module_name not in _STDLIB_MODULES:
        return []
    try:
        owner: Any = importlib.import_module(module_name)
    except ImportError:                                     # pragma: no cover - stdlib is present
        return []
    if attribute:
        owner = getattr(owner, attribute, None)
    target = getattr(owner, leaf, None)
    if target is None:
        return []
    try:
        parameters = inspect.signature(target).parameters
    except (TypeError, ValueError):                         # no introspectable signature
        return []
    positional = tuple(name for name, parameter in parameters.items()
                       if parameter.kind in (parameter.POSITIONAL_ONLY,
                                             parameter.POSITIONAL_OR_KEYWORD))
    return [(positional, frozenset(parameters))]


def _source_signature(home: str, leaf: str) -> list[tuple[tuple[str, ...], frozenset[str]]]:
    """``leaf``'s real parameter names read from first-party SOURCE, no import."""
    unit = _unit_for_module(home)
    function = unit.functions.get(leaf) if unit is not None else None
    if function is None:
        return []
    arguments = function.args
    positional = tuple(argument.arg
                       for argument in [*arguments.posonlyargs, *arguments.args])
    every = set(positional) | {argument.arg for argument in arguments.kwonlyargs}
    return [(positional, frozenset(every))]


@functools.lru_cache(maxsize=None)
def primitive_signatures(leaf: str) -> tuple[tuple[tuple[str, ...], frozenset[str]], ...]:
    """Every real ``(positional names, all names)`` form ``leaf`` can denote.

    Derived from the actual callables and the actual sources, never enumerated here, so the gate
    follows a signature that changes instead of drifting away from it.
    """
    forms: list[tuple[tuple[str, ...], frozenset[str]]] = []
    for kind, home in _PRIMITIVE_HOMES.get(leaf, ()):
        forms.extend(_stdlib_signature(home, leaf) if kind == "py"
                     else _source_signature(home, leaf))
    return tuple(forms)


def destination_expressions(call: ast.Call, leaf: str) -> list[ast.expr] | None:
    """Expressions naming a file ``call`` acts on, or ``None`` when the form is UNRESOLVED.

    ``None`` is the FAIL-CLOSED answer: the leaf says this call writes, but no real signature
    told us where it writes, so the caller reports it instead of assuming it is harmless. An
    unmodelled form passing quietly is precisely how the hand-written position table grew a hole.

    Binding is decided by the signature, not guessed: a first parameter named ``self`` means the
    receiver of an attribute call is the destination and the remaining parameters shift by one
    (``p.write_text(data)``), while the same primitive called flat keeps its positions
    (``write_text(path, data)``). A signature without ``self`` never shifts, so
    ``os.rename(src, dst)`` is read as the module function it is even though it too is spelled as
    an attribute access.
    """
    forms = primitive_signatures(leaf)
    found: list[ast.expr] = []
    resolved = False
    attribute_call = isinstance(call.func, ast.Attribute)
    for positional, every in forms:
        indices = [index for index, name in enumerate(positional)
                   if name in _DESTINATION_PARAMETERS]
        if not indices:
            continue
        resolved = True
        bound = attribute_call and bool(positional) and positional[0] == "self"
        for index in indices:
            if bound and index == 0:
                found.append(call.func.value)
                continue
            position = index - 1 if bound else index
            if 0 <= position < len(call.args):
                found.append(call.args[position])
        keyword_names = every & _DESTINATION_PARAMETERS
        found.extend(keyword.value for keyword in call.keywords
                     if keyword.arg in keyword_names)
    return found if resolved else None


def _names_manifest_artefact(call: ast.Call,
                             assigns: dict[str, list[ast.expr]]) -> bool | None:
    """True if a DESTINATION of ``call`` names a run-manifest file. ``None`` == unresolved form.

    Only destinations are inspected, never the payload: CL-Bench's `collect_results` records
    POINTERS to the runner's sidecar manifests (`.../cl_bench/*/run_manifest.json`) in the
    `results.json` it writes, and a first cut that read every argument called that a publication.
    Recording a path is not writing to it.

    The local hop matters in the other direction: ``manifest_path = run_dir / "run_manifest.json"``
    followed by ``write_json(manifest_path, manifest)`` is the same publication with the literal
    moved one line up, and a check that only read the call site would wave it through. That is
    not hypothetical — it is the exact shape of the `run_pro` instance this invariant found.
    """
    def _literal(expr: ast.expr) -> bool:
        return any(isinstance(node, ast.Constant) and isinstance(node.value, str)
                   and MANIFEST_ARTEFACT in node.value for node in ast.walk(expr))

    destinations = destination_expressions(call, _dotted_callee(call.func).split(".")[-1])
    if destinations is None:
        return None
    for destination in destinations:
        if _literal(destination):
            return True
        for node in ast.walk(destination):
            if isinstance(node, ast.Name) and any(_literal(source)
                                                  for source in assigns.get(node.id, [])):
                return True
    return False


def _resolve_function(leaf: str, unit: _Unit) -> tuple[ast.FunctionDef | None, _Unit]:
    """``leaf``'s definition, from this module or from the first-party module it came from."""
    target = unit.functions.get(leaf)
    if target is not None:
        return target, unit
    imported = _unit_for_module(unit.imports.get(leaf, ""))
    if imported is None:
        return None, unit
    return imported.functions.get(leaf), imported


def publishes_manifest(call: ast.Call, unit: _Unit,
                       assigns: dict[str, list[ast.expr]] | None = None,
                       *, depth: int = 3) -> str:
    """Description of a manifest publication effected by ``call``, or ``""``.

    Resolves through helper bodies exactly as ``resolve_denied`` does, and for the same reason:
    the two real offenders are named for the records they keep, so only their BODIES say that a
    manifest gets written. Reported as ``helper -> primitive`` so the report names the hop.
    """
    assigns = {} if assigns is None else assigns
    callee = _dotted_callee(call.func)
    leaf = callee.split(".")[-1]
    if leaf in WRITE_PRIMITIVES:
        # A primitive DECIDES here; there is no point walking a writer's own body. `None` is the
        # fail-closed verdict for a write form no real signature could place.
        named = _names_manifest_artefact(call, assigns)
        if named is None:
            return f"{callee} [{UNRESOLVED_WRITE}]"
        return callee if named else ""
    if depth <= 0:
        return ""
    target, target_unit = _resolve_function(leaf, unit)
    if target is None:
        return ""
    inner_assigns = _local_assigns(target)
    for node in ast.walk(target):
        if not isinstance(node, ast.Call):
            continue
        inner = publishes_manifest(node, target_unit, inner_assigns, depth=depth - 1)
        if inner:
            return f"{target.name} -> {inner}"
    return ""


def _seam_publication_violations(unit: _Unit) -> list[str]:
    """Manifest publications reachable from INSIDE an active finalization seam."""
    # Keyed by the offending call's line so a `with` inside a nested def is reported once, and
    # attributed to the INNERMOST enclosing function (the largest def line that contains it).
    found: dict[int, tuple[int, str, str]] = {}
    for name, function in sorted(unit.functions.items()):
        assigns = _local_assigns(function)
        for node in ast.walk(function):
            if not isinstance(node, (ast.With, ast.AsyncWith)):
                continue
            if not any(isinstance(item.context_expr, ast.Call)
                       and _dotted_callee(item.context_expr.func).endswith(FINALIZE_CALLEE)
                       for item in node.items):
                continue
            for statement in node.body:
                for call in [inner for inner in ast.walk(statement)
                             if isinstance(inner, ast.Call)]:
                    published = publishes_manifest(call, unit, assigns)
                    if not published:
                        continue
                    if call.lineno not in found or function.lineno > found[call.lineno][0]:
                        found[call.lineno] = (function.lineno, name, published)
    reports: list[str] = []
    for lineno, (_def_line, name, published) in sorted(found.items()):
        if UNRESOLVED_WRITE in published:
            reports.append(
                f"{unit.name}: {name}() performs a write at line {lineno} ({published}) from "
                f"INSIDE an active {FINALIZE_CALLEE}, and no real signature places its "
                f"destination -- so the gate cannot tell whether it publishes a manifest. "
                f"Reported rather than assumed harmless: give the primitive a home in "
                f"_PRIMITIVE_HOMES, or move the write out of the seam"
            )
            continue
        reports.append(
            f"{unit.name}: {name}() publishes a manifest from INSIDE an active {FINALIZE_CALLEE} "
            f"at line {lineno} ({published}) -- the seam merges the terminal outcome/exit_code/"
            f"refusal only when the context EXITS, so this writes a pre-merge record that a "
            f"concurrent reader can observe and an interruption makes durable. The seam writes "
            f"the same path on every exit path already: delete the early write"
        )
    return reports


# --------------------------------------------------------------------------- #
# Seam shape (kept with the invariants: ONE gate, one report)
# --------------------------------------------------------------------------- #

def _seam_violations(source: str, name: str) -> list[str]:
    violations: list[str] = []
    if f"{ADMISSION_CALLEE}(" not in source:
        violations.append(f"{name}: bypasses the admission seam")
        return violations
    if "finalize_run_manifest(" not in source:
        violations.append(f"{name}: records no final outcome")
    if "benchmark_run_manifest(" in source:
        violations.append(
            f"{name}: calls the builder directly again: its refusal would never be persisted")
    # Python evaluates ARGUMENTS before entering the callee, so a gate called inside the
    # admission call's argument list refuses BEFORE the manifest can be written — the durable
    # refusal defeated by evaluation order. Attestation belongs after admission.
    call = source.split(f"{ADMISSION_CALLEE}(", 1)[1].split("\n    )\n", 1)[0]
    if "runtime_attestation(" in call:
        violations.append(
            f"{name}: evaluates runtime_attestation inside the admission argument list")
    return violations


def audit_source(source: str, *, name: str = "<synthetic>") -> list[str]:
    """Every invariant violation in one launcher source. Empty list == the gate passes."""
    unit = _Unit(ast.parse(source), name)
    violations = _seam_violations(source, name)
    if any("bypasses the admission seam" in item for item in violations):
        return violations
    return (violations + _pre_admission_violations(unit) + _confinement_violations(unit)
            + _seam_publication_violations(unit))


def audit_launcher(path: pathlib.Path) -> list[str]:
    return audit_source(pathlib.Path(path).read_text(encoding="utf-8"),
                        name=pathlib.Path(path).name)


def audit_all_launchers() -> list[str]:
    """The gate: every migrated launcher, both invariants, one report."""
    violations: list[str] = []
    for path in launcher_paths():
        violations.extend(audit_launcher(path))
    return violations
