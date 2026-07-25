# Ouroboros

[![GitHub stars](https://img.shields.io/github/stars/razzant/ouroboros?style=flat&logo=github)](https://github.com/razzant/ouroboros/stargazers)
[![Downloads](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Frazzant%2Fouroboros%2Fbadges%2Fdownloads.json)](https://github.com/razzant/ouroboros/releases)
[![Website](https://img.shields.io/badge/website-razzant.github.io%2Fouroboros-c93545.svg)](https://razzant.github.io/ouroboros/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![macOS 12+](https://img.shields.io/badge/macOS-12%2B-black.svg)](https://github.com/razzant/ouroboros/releases)
[![Linux](https://img.shields.io/badge/Linux-x86__64-orange.svg)](https://github.com/razzant/ouroboros/releases)
[![Windows](https://img.shields.io/badge/Windows-x64-blue.svg)](https://github.com/razzant/ouroboros/releases)
[![OuroborosHub](https://img.shields.io/badge/OuroborosHub-skills%20marketplace-8A2BE2.svg)](https://github.com/razzant/OuroborosHub)
[![Version 6.75.0](https://img.shields.io/badge/version-6.75.0-green.svg)](VERSION)

Ouroboros is an open-source, general-purpose AI agent whose identity, durable memory, and history continue across tasks and restarts. It works on external projects, coordinates a live swarm of specialist agents, and can rewrite the implementation it runs on, including its code, architecture, prompts, tools, and dependencies. Reflection can also change how it understands itself without severing that continuity.

It runs as a native desktop app or through a headless CLI. The runtime keeps its repository, durable memory, history, and interface on your machine, while model inference can use remote APIs you configure or a local GGUF model.

Ouroboros first booted on February 16, 2026. During the following 48 hours, the repository advanced from the v4.1 line to v6.2.0. The self-authored record preserved from that period counts 32 evolution cycles. That first generation ran in Google Colab through Telegram and remains preserved on the [`legacy-google-colab`](https://github.com/razzant/ouroboros/tree/legacy-google-colab) branch and its [original project page](https://razzant.github.io/ouroboros/archive/first-generation/); the current generation carries the same identity into a native desktop and headless runtime.

> ⭐ **[Star Ouroboros](https://github.com/razzant/ouroboros)** to follow its next evolution. A star also helps more people find the project, trace its history, and take part in what it becomes.

Reviewed skills, transport bridges, tools, and widgets are available through [OuroborosHub](https://github.com/razzant/OuroborosHub).

<p align="center">
  <img src="assets/chat.png" width="700" alt="Chat interface">
</p>
<p align="center">
  <img src="assets/settings.png" width="700" alt="Settings page">
</p>

---

## Install

| Platform | Download | Instructions |
|----------|----------|--------------|
| **macOS** 12+ | [Ouroboros.dmg](https://github.com/razzant/ouroboros/releases/latest) | Open DMG → drag to Applications → optional CLI: run `Install CLI.command` after the app is in Applications |
| **Linux** x86_64 | [Ouroboros-linux.tar.gz](https://github.com/razzant/ouroboros/releases/latest) | Extract → run `./Ouroboros/Ouroboros` → optional CLI: `./Ouroboros/bin/install-ouroboros-cli`. If browser tools fail due to missing system libs, run: `./Ouroboros/python-standalone/bin/python3 -m playwright install-deps chromium webkit` |
| **Windows** x64 | [Ouroboros-windows.zip](https://github.com/razzant/ouroboros/releases/latest) | Extract → run `Ouroboros\Ouroboros.exe` → optional CLI: `Ouroboros\bin\install-ouroboros-cli.cmd` |

Prerelease artifacts stay on their tag pages; `/releases/latest` points to the latest stable release.

<p align="center">
  <img src="assets/setup.png" width="500" alt="Drag Ouroboros.app to install">
</p>

On macOS, use right-click → **Open** on first launch if Gatekeeper asks. The setup wizard configures model access, review policy, and budget. Packaged CLI installers create a user-local `ouroboros` command without sudo; `ouroboros run --start "2+2?"` starts or attaches to the same managed runtime used by the desktop app.

---

## What Ouroboros Can Do

- **Modify its implementation.** Its editable surface spans application code, architecture, prompts, tools, and dependencies, while reflection can also reshape its living self-understanding.
- **Evolve autonomously.** Evolution campaigns turn selected improvements into reviewed changes that remain part of its Git history.
- **Continue across restarts.** Identity, memory, dialogue, knowledge, reflections, and version history form one ongoing biography.
- **Think between requests.** Background consciousness supports reflection, initiative, and preparation outside the immediate request-response loop.
- **Coordinate a live swarm.** Specialist agents can investigate or act in parallel, share task-tree findings, and return work for integration.
- **Work on external projects.** A separate Git workspace can receive the full task loop while Ouroboros keeps its own repository and governance boundary distinct.
- **Operate through desktop or CLI.** The native app and gateway-backed command line expose the same managed tasks, progress, artifacts, logs, and schedules.
- **Organize long-running work.** Project rooms keep working folders, journals, knowledge, task history, and conversations connected to the same identity.
- **Use remote or local models.** Supported provider APIs and local GGUF models can fill the runtime's configurable cognitive roles.
- **Grow through reviewed extensions.** Skills, transport bridges, widgets, MCP tools, and companion processes expand capability without folding every integration into the core.
- **Keep self-change inspectable.** Git history, review evidence, explicit protected surfaces, and restart checks make implementation changes traceable.

This list is an orientation, not a second specification. [BIBLE.md](BIBLE.md) defines Ouroboros's identity and constitutional boundaries; [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) are the current technical sources of truth.

---

## Run from Source

### Requirements

- Python 3.10+
- macOS, Linux, or Windows
- Git
- [GitHub CLI (`gh`)](https://cli.github.com/), optional unless you use GitHub integration

### Setup

```bash
git clone https://github.com/razzant/ouroboros.git
cd ouroboros
python3.11 -m venv .venv      # any Python >= 3.10 is OK
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv      # any Python >= 3.10 is OK
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

### Run

```bash
ouroboros server
```

Then open `http://127.0.0.1:8765` in your browser. The setup wizard will guide you through API key configuration.

### Google Colab

Use [`notebooks/colab_quickstart.py`](notebooks/colab_quickstart.py) as a Colab-compatible cell script when you need a source-mode runtime without the desktop UI. It keeps runtime data on Google Drive and preserves the original Colab path without making it the primary installation flow.

### CLI / Headless

The `ouroboros` command attaches to the local runtime by default and starts one when `--start` is passed. It exposes managed tasks, progress streams, artifacts, logs, schedules, settings, skills, and evolution controls without duplicating the server's business logic.

```bash
ouroboros status
ouroboros run --start "2+2?"
ouroboros run "Summarize current runtime state"
ouroboros run --workspace /path/to/project --memory-mode forked --patch-out result.patch "Fix the failing test"
ouroboros tasks list
ouroboros logs tail progress --task-id <task_id>
ouroboros schedule add --name nightly-review --cron "0 2 * * *" "Run a maintenance review"
ouroboros schedule list
```

External workspaces must be separate Git worktree roots and may not overlap Ouroboros's own repository or data directory. Patch, streaming, detached-task, and schedule semantics are documented in the CLI help and the canonical [architecture](docs/ARCHITECTURE.md).

### For Agents

Another agent, script, or CI job can invoke Ouroboros through the same gateway-backed CLI:

```bash
ouroboros run --start \
  --workspace /path/to/project \
  --memory-mode forked \
  --patch-out result.patch \
  --result-json-out result.json \
  "Investigate the task, act, and verify the result"
```

Use `--jsonl` for a machine-readable event stream and `--detach` when the caller will follow the task with `ouroboros tasks watch <task_id>` or inspect it with `ouroboros tasks show <task_id>`. External workspace runs keep Ouroboros's own repository and governance context separate, then export changes as reviewable patch artifacts.

To change Ouroboros itself, follow [CONTRIBUTING.md](CONTRIBUTING.md) and read [BIBLE.md](BIBLE.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md), and [docs/CHECKLISTS.md](docs/CHECKLISTS.md) in full before editing.

### Configuration

The first-run wizard and **Settings** configure model access, cognitive roles, local models, review policy, runtime mode, budget, skills, and optional integrations. Ouroboros supports configurable remote providers, compatible endpoints, and local GGUF inference; exact settings and defaults live in [`ouroboros/config.py`](ouroboros/config.py) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

The server binds to `127.0.0.1:8765` by default. Read [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) before exposing it beyond loopback; non-local binds need `OUROBOROS_NETWORK_PASSWORD` or an explicitly trusted external access layer.

### Run Tests

```bash
make test
```

---

## Build

### Docker

```bash
docker build -t ouroboros-web .
docker run --rm -p 8765:8765 \
  -e OUROBOROS_NETWORK_PASSWORD='choose-a-password' \
  -e OUROBOROS_FILE_BROWSER_DEFAULT=/workspace \
  -v "$PWD:/workspace" \
  ouroboros-web
```

Docker runs the web runtime, not the native desktop shell. It bundles Chromium and WebKit support; use [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for network and container policy.

### Release tag prerequisite

Platform build scripts package only a commit already tagged with `v$(cat VERSION)`. Tag the exact release commit first:

```bash
git tag -a "v$(tr -d '[:space:]' < VERSION)" -m "Release v$(tr -d '[:space:]' < VERSION)"
```

`scripts/build_repo_bundle.py` verifies the tag and embeds the source binding into the packaged repository bundle. Signing, notarization, bytecode sealing, and CI invariants are documented in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

### macOS (.dmg)

```bash
bash scripts/download_python_standalone.sh
OUROBOROS_SIGN=0 bash build.sh
```

Output: `dist/Ouroboros-<VERSION>.dmg`, containing `Ouroboros.app` and `Install CLI.command`. Omit `OUROBOROS_SIGN=0` when a Developer ID signing identity is configured.

### Linux (.tar.gz)

```bash
bash scripts/download_python_standalone.sh
bash build_linux.sh
```

Output: `dist/Ouroboros-<VERSION>-linux-<arch>.tar.gz`, containing `Ouroboros/bin/install-ouroboros-cli`. If bundled browser tools need host libraries, run `./Ouroboros/python-standalone/bin/python3 -m playwright install-deps chromium webkit`.

### Windows (.zip)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/download_python_standalone.ps1
powershell -ExecutionPolicy Bypass -File build_windows.ps1
```

Output: `dist\Ouroboros-<VERSION>-windows-x64.zip`, containing `Ouroboros\bin\install-ouroboros-cli.cmd`.


## Architecture and Runtime Data

The native launcher starts a web runtime and supervisor-managed agent workers. The agent core lives in `ouroboros/`, the interface in `web/`, the process plane in `supervisor/`, and the runtime's durable identity, state, history, logs, and skills under `~/Ouroboros/data/`.

The full component map, data flow, API surface, storage layout, safety boundary, and operational rationale live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Deployment details live in [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Runtime Commands

| Command | Purpose |
|---------|---------|
| `/panic` | Stop the runtime and its managed processes immediately. |
| `/restart` | Restart without automatically resuming the active owner task. |
| `/status` | Show workers, task queue, and budget state. |
| `/evolve on\|off` | Start or stop autonomous evolution. |
| `/review` | Queue a deep constitutional and architectural self-review. |
| `/bg start\|stop\|status` | Control background consciousness. |


## Philosophy

The 13 Constitution principles — Agency, Continuity, Meta-over-Patch,
Immune Integrity, Self-Creation, LLM-First, Authenticity & Reality
Discipline, Minimalism, Becoming, Versioning and Releases, the absorbed
Iterations / Spiral lineage, and Epistemic Stability — are defined in
full in [`BIBLE.md`](BIBLE.md). That file is the constitutional SSOT
(Bible P4 Ship-of-Theseus protection) and this README intentionally does
not paraphrase it.

---

## Contributing

External contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
for the complete workflow. Open pull requests against the lowercase
`ouroboros` branch and leave release-version allocation to maintainers. A
current OpenRouter triad + scope packet is the optional fast path; pull
requests without one remain welcome but require more maintainer-side review
and integration work.

---

## Version History

| Version | Date | Description |
|---------|------|-------------|
| 6.75.0 | 2026-07-25 | **fix: benchmark runs tell the truth about themselves — clean seed, attested runtime, honest denominators.** Provenance becomes a GATE instead of a report: `benchmark_run_manifest()` carries a universal seed gate (`require_clean=True` by default, recorded `seed_gate` block, `--allow-dirty-seed` escape, `expect=` pin that no escape waives) and every P1 launcher now builds its manifest ONCE right after argument parsing, WRITES IT TO DISK IMMEDIATELY (so a refusal after admission still leaves a durable record of what was refused, with a typed `refusal` block and the exit code, instead of nothing but a discarded stderr line), keeps it, augments it, and rewrites it at the end with a final `outcome` — SWE-Pro's `run_pro.py`/`auto_run.py` wrote no manifest at all and ProgramBench wrote its one manifest after every instance AND the official eval, i.e. after all the spend. New `runtime_attestation()` records BOTH facts about a live server (the HTTP `runtime_version` from the frozen `/api/health` contract and the local HEAD/VERSION of its checkout) and fails closed on a skew unless the named `OBO_ALLOW_EVOLVED_VOLUME=1` override is set, which it records; that override waives ONLY the deliberately accepted skew, never `runtime_unreachable` (any transport or parse failure, i.e. no live identity was established at all) and never `commit_unavailable` (no commit to attribute the numbers to), so admission cannot continue past an unreachable `/api/health` just because the override happens to be exported; `commit_lineage_ok()` compares a line of descent (`merge-base --is-ancestor`), never equality, so an evolution run legitimately moving HEAD forward is not corruption. It rides inside readiness paths that cannot be skipped (`IsolatedServer._wait_ready`, ProgramBench admission) and, inside the SWE-Pro container where the evolved commits actually exist, as ONE-SHOT steps around a seed stamp written in the UNCHANGED `[ -e /obo-repo/.git ] \|\|` seeding branch — never inside the polled `ready_probe`, whose loop reads any non-zero rc as "not ready yet" and would swallow the refusal for 900s. **Egress isolation is NOT part of this release.** A structural egress-isolation subsystem for SWE-Pro solve containers (an `--internal` docker network plus a fixed-upstream TLS pass-through relay per configured provider host, with in-container reachability probes and a fail-closed refusal) was built during this phase and then EXTRACTED before release, and is deferred to a later one: there is no `--network-mode` flag, no relay machinery and no isolation claim in this tree, and solve containers run with the same OPEN network they had before v6.75.0. The owner ruled (2026-07-25) that it would be off by default in any case — the comparison baseline (codex) also runs with network access, so cheating will be looked for in the traces rather than prevented by the sandbox, and losing ~11% of instances (measured: 31 musl/Alpine instances out of 287 ever attempted, 84% of which previously produced a non-empty patch; that transport needs general network during setup) is the worse trade. The disclosure in `swe_bench_pro/METHODOLOGY.md` §0(c) is corrected to match: the official SWE-bench Pro harness does not regulate the solve container's network at all (the official evaluator's `--block_network` applies to the EVAL container), and what keeps the solver off the upstream fix is the adapter's tool policy (`--disable-tools`), not the network. A provenance refusal is CAMPAIGN-fatal, not a per-task skip (`stamp_absent`, `seed_mismatch`, `lineage_broken`, `runtime_skew`, `seed_head_unreadable` are properties of the volume plus the mounted seed, so `auto_run.py` stops the shard once instead of restoring the same broken volume N times and exiting 0 with a zero headline), unknown cleanliness is not cleanliness (a `git status` probe that fails or times out now records `status_available: false` and refuses with `seed_status_unavailable` instead of coercing to `dirty: false`). Grading gains a third state: `pass\|fail\|ungraded` + typed reason with `ungraded=N/total` printed beside the UNCHANGED headline formula and the shrunken-denominator percentage explicitly labelled diagnostic, not leaderboard-valid. ProgramBench's run ledger is append-only per row at BOTH the run root and the instance dir, skip rows included (readers dedup by `instance_id`, last row wins), `common/manifests.py::write_json` is atomic and byte-identical (lazy `atomic_write_json` import keeps the module stdlib-only for the container-side harbor agent), and `openrouter_key_remaining()` reads the authoritative `limit_remaining` with `limit - usage` only as a fallback. **Round-2 commit-gate fixes (same release).** The provenance LIFECYCLE is now enforced by two shared seams in `common/manifests.py` instead of by convention in each launcher: `admit_benchmark_run(manifest_path, ...)` builds the complete manifest, WRITES it, and only then lets the gate enforce — the refusal rides on a typed `BenchmarkAdmissionRefused` (a `RuntimeError`, so every pre-existing caller is unchanged) that carries the same payload, because `_seed_gate` used to raise before the dict reached any caller and no launcher could persist the refusal record the contract promised; and `finalize_run_manifest(manifest_path, manifest)` is the ONE finalization seam, a context manager whose yielded mapping is merged into the retained manifest and written on EVERY exit path — normal return, early typed return, and an escaping `BaseException` alike (recorded as `outcome: crashed` plus a typed `error`), which is the case that used to leave `outcome: started` on disk forever. All seven migrated launchers route through both (`run_programbench.py`, `run_programbench_e2e.py`, `swebench_predictions.py`, `pro_predictions.py`, `run_harness_bench_fast.py`, `e1v2/run_pro.py`, `e1v2/auto_run.py`); a meta-test names them and fails if any calls the builder directly again, and each has a failure-path test. The evolved-volume override is narrowed in the SHELL too, not only in the Python helper: `e1v2/entrypoint_pro.sh` waived ANY non-empty `SEED_REASON`, so with `OBO_ALLOW_EVOLVED_VOLUME=1` exported a volume with no stamp, a foreign seed, a broken lineage or an unreadable seed HEAD all solved on anyway; it now waives exactly `runtime_skew` (asserted equal to `manifests.OVERRIDABLE_ATTESTATION_REASONS`, and the decision is executed under bash in the tests rather than string-matched), an empty or unparseable `/api/health` gets its own non-overridable `runtime_unreachable` reason, and `/out/seed_attestation.json` now reports `override_set` separately from `overridden`. In `runtime_attestation()` commit availability is decided BEFORE version skew, so a checkout with no readable commit that also disagrees on the version is `commit_unavailable` (never waivable) instead of being mislabelled as the waivable skew and then waived. And `e1v2/orchestrate_probe.py::grade_one` can no longer publish a stale grade: it unlinks `pro_eval/grade_summary.json` before invoking the grader and accepts a verdict only when the grader exited 0 AND produced that artefact in THIS attempt — a rerun whose grader timed out or crashed used to attribute the earlier attempt's PASS/FAIL to the new one. **Stale test locks repaired, not adapters.** The two bench-denylist expectations updated here (`disabled_tools == ["claude_code_edit", "schedule_subagent"]` in `test_programbench_task_body_sets_executor_and_protected_policy` and `test_bench_template_scaffold_defaults_v655`) were STALE relative to operator commit `8ad83e8` (2026-07-23), which is already an ancestor of this release's base: `programbench/schemas.py` and `terminal_bench/harbor_installed_agent.py` have emitted BOTH ids since then while the tests still asserted the old single-item list. Only the assertions move; no adapter behaviour changes, and both tests fail on the bare base and pass here. **Round-3 commit-gate fixes (control flow in the new seams).** Two narrow ordering bugs in the code round 2 added. (a) ProgramBench's e2e launcher evaluated `runtime_attestation()` inside `admit_benchmark_run()`'s ARGUMENT list, and Python evaluates arguments before entering the callee — so an attestation refusal raised with no `run_manifest.json` on disk at all, defeating the durable-refusal contract by evaluation order; attestation now runs inside the finalization block, after the seed-admission manifest is persisted, and records a typed `refusal` with stage `runtime_attestation` before re-raising. A static guard in the launcher meta-test forbids the shape returning; the other six migrated launchers were audited and evaluate nothing that can refuse in that position (`_collect_attestations` records a broken record instead of raising, `programbench_command_for_manifest` falls back on `RuntimeError`, `resolve_preset` is a dict lookup). (b) `finalize_run_manifest` flattened every escaping `BaseException` to `exit_code: 1`, so `auto_run`'s campaign-fatal `SystemExit(2)` produced a record that disagreed with the status the process actually exits with; an integer `SystemExit.code` is now preserved, and `auto_run.main` names that stop `refused` with stage `campaign_fatal_infra` instead of leaving the generic `crashed`. **Round-4 commit-gate fixes (last two, both consequences of the seams).** (a) The campaign-fatal provenance contract is now ONE shared authority, `manifests.CAMPAIGN_FATAL_PROVENANCE_REASONS`, consumed by BOTH SWE-Pro drivers; `auto_run`'s private copy is deleted. It was only ever applied in `auto_run.run_one`, so `run_pro.py` invoked DIRECTLY — and `orchestrate_probe.py`, which shells out to it — appended one INFRA timeline row per task, refused the whole schedule and still returned 0 with `outcome: completed`, reporting a zero headline as success. `_run_schedule` now stops on the first such reason with a typed `volume_provenance` refusal carrying the EXACT reason, task index and instance id, and exits 2 (the status `auto_run` already uses for this class); the refused task's timeline row is still written first, because `auto_run.run_one` reads it. (b) `runtime_attestation()` no longer discards the record it just built: a refusal raises `RuntimeAttestationRefused` (a `RuntimeError`, so every existing caller and test is unaffected) carrying the attestation, exactly parallel to `BenchmarkAdmissionRefused`, and ProgramBench's e2e launcher persists that record plus its exact typed reason — so the durable manifest keeps `reason`, `runtime_version`, `repo_head` and `repo_version` instead of a generic `runtime_attestation_failed` string at the moment they matter most. **Round-5 commit-gate fixes: admission is the OUTER boundary, now guarded by a test.** Three review rounds each found a different operation running before persisted admission, so the rule is enforced structurally rather than case by case: in every migrated launcher, everything preceding `admit_benchmark_run()` is argument parsing and pure local derivation only — no filesystem assertion, no docker, no subprocess, no network, no state mutation — and the seam meta-test walks each launcher's admission function (plus the statements of `main()` that precede it, for `run_pro.py`, where admission lives in `_run_schedule`) with `ast` and fails on any call matching one named, extendable denylist. The four instances it caught and that are fixed here: `assert_seed_is_git_directory()` ran before admission in BOTH SWE-Pro drivers, so a worktree-pointer `.git` refused with no durable manifest (it now runs inside the finalization block and records a typed `seed_shape` refusal, exit 2); `run_programbench_e2e.py` wrote `instance_order.json` before admission (moved after, so a refused run leaves no artefacts); `auto_run.py`'s `done_idx` mkdir moved after admission; and `run_harness_bench_fast.py`'s pre-admission `out_root.mkdir()` is DELETED as redundant — the atomic manifest write creates the run root. One further ordering bug of the same family: the campaign-fatal provenance stop in `_run_schedule` was tested only after `dump_state()`/`read_spent_usd()`, so a known volume-wide refusal first performed volume archival that its own comment says can hang for hours, stranding the promised refusal and exit code — it is now tested immediately after the timeline row is persisted (`auto_run` still parses that row) and before any further volume work, asserted by `dump_state` never being called. **Round-6 commit-gate fix.** The round-5 `seed_shape` refusal was INERT: `assert_seed_is_git_directory` raised `SystemExit`, which derives from `BaseException`, so both launchers' `except Exception` handlers never ran and the manifest recorded the seam's generic `crashed` instead of the typed refusal — the shape without the effect. The check now raises `SeedShapeRefused`, a third member of this phase's typed-refusal family alongside `BenchmarkAdmissionRefused` and `RuntimeAttestationRefused` (all `RuntimeError` subclasses carrying a typed `reason`), so the handlers work by construction and every refusal in the phase has ONE shape; both callers now return 2 rather than re-raising, so the recorded `exit_code` equals the status the process really exits with. The tests assert the persisted record (`outcome: refused`, `refusal.stage: seed_shape`, `exit_code: 2`), not merely that something was raised — asserting the raise alone is what let an inert handler pass. **Round-7 commit-gate fix, and the class is closed.** ProgramBench's e2e launcher recorded the runtime-attestation refusal as `exit_code: 3` and then re-raised, so the process exited 1 (`raise SystemExit(main())` is never reached when an exception escapes) while the durable record claimed 3 — the third round in a row to find `recorded status != real status` at a new site. It now logs the message the exception used to surface and RETURNS 3, with the manifest record unchanged. The whole class was then swept across all seven migrated launchers: the other six were already consistent — `run_pro.py` and `auto_run.py` return every recorded code (and `auto_run`'s campaign-fatal path deliberately re-raises a `SystemExit` whose integer code IS the recorded one), while `run_programbench.py`, `swebench_predictions.py` and `pro_predictions.py` record `exit_code: 1` and let a plain exception escape, which exits 1, so record and reality already agreed. The invariant is now asserted as a PROPERTY rather than as syntax: one parametrised behavioural test per launcher drives `main()` into a refusal path and compares the status `raise SystemExit(main())` would produce against the `extra.exit_code` the run's own record claims, so recording a code and then letting an exception escape fails a test instead of a review. **Round-8 commit-gate fix.** On the DEFAULT admission path, `runtime_attestation()` fell back to a generic `version` field when the frozen health contract's `runtime_version` was absent, so any unrelated HTTP server answering `{"version": "6.75.0"}` attested successfully and ProgramBench could bless a server that is not Ouroboros at all. `runtime_version` is part of the frozen `HealthResponse` (`ouroboros/gateway/contracts.py`), so its absence is not a version to guess at: only the contracted field is read, and its absence is the distinct NON-overridable reason `runtime_version_absent` — the endpoint answered, but not with the health contract, so no live runtime identity was established (it sits with `runtime_unreachable` and `commit_unavailable`, never with a deliberate skew). |
| 6.74.5 | 2026-07-22 | **fix: subagents can read the skill payloads they audit; budget drift compares like with like.** (1) v6.70.0 granted read-only scouts read/list/search on `root=skill_payload`, but the path layer still resolved payloads against the child's isolated drive (`data/state/headless_tasks/<tid>/data`), which physically has no `skills/` tree — every scout was blinded with a bare "Directory not found" (2026-07-21 anime_studio audit swarm: three children produced zero payload reads and the parent died budget_exhausted doing everything alone). `resource_root_path` now resolves `skill_payload` against the canonical data root (new `canonical_data_root` helper: task_metadata `budget_drive_root` → ctx `budget_drive_root` → `drive_root`), so root tasks and isolated benchmark roots are unchanged while children read the real payload; the verb matrix is untouched — write/edit/review stay parent-only, path confinement and control-plane sidecar guards unchanged, native bucket stays out of the data-plane resolver. (2) `budget_drift_alert` compared the ALL-provider ledger delta against ONE OpenRouter key's usage delta, so real direct-provider spend (Anthropic advisory ~$98/day) latched the alert at ~88% while nothing was wrong. Drift now compares the OpenRouter-only settled ledger delta (`by_provider` from the attempt ledger; settled-only, reservations excluded) against the key's usage delta, rebaselines silently when the configured key changes mid-session (non-secret sha256 fingerprint) or when a pre-upgrade state lacks the new snapshot, suppresses the comparison honestly while the ledger is integrity-degraded, and `status_text` renders exactly the same deltas as the computation (the warning event keeps the all-provider delta as context). |
| 6.74.4 | 2026-07-21 | **feat: workspace-tree freeze directives (mitigation) + truthful ProgramBench submission contract.** Root cause (PB cmatsuoka__figlet smoke): an agent committed a compiling state, then broke the tree with one last uncommitted edit as the acceptance improvement loop hit its pass cap — and the harness ships the LIVE tree (`.git` dropped), so the verified commit protected nothing. All existing salvage machinery guards the answer TEXT only. Fix, prompt-only (P5) over existing channels (P7): the acceptance rails line marks the last admitted improvement pass (`passes_done+1 >= cap`, within cap>0) as FINAL, and EVERY workspace-delivery capsule (canonical `is_workspace_mode()` authority, attribute fallback for light contexts) carries the tree directive — a deadline or cost rail can end the loop between capsules — keep the tree VERIFIED (rebuild, verify, and commit if the task calls for a commit; revert unverified edits); the 10% deadline flush AND the ~80% cost wrap-up gain one shared commit-NEUTRAL tree sentence (acting self_worktree subagents cannot commit; a moved HEAD fails patch capture closed), byte-identical for non-workspace tasks; the ProgramBench instruction now states the true submission model — a source tarball from the CURRENT tree state (uncommitted edits DO ship; `.git`, root binaries and build/cache noise excluded), run `./compile.sh` one final time — replacing the false "fresh checkout" framing. Disclosed residual (mitigation, not closure): a forced tool-less exit — deadline grace or budget stop crossed inside one long round, with no pacing note or capsule in the terminal stretch — can still ship an unverified last edit; the structural verification-freshness seam is a filed follow-up pending an owner decision. |
| 6.74.3 | 2026-07-21 | **fix: Windows portability of one v6.74.0 guard test.** `test_genuine_repo_target_still_blocks` built its shell command via an f-string embedding a Windows path (backslashes mangled by shlex) and failed the 3-OS full matrix on windows-latest; the test now passes argv lists. No runtime code changes. |
| 6.74.2 | 2026-07-21 | **fix: CI portability of the two new GAIA sandbox-staging tests.** They imported `inspect_ai` directly — an optional benchmark dependency absent on CI runners — and failed quick-test with ModuleNotFoundError. The tests now inject a fake `inspect_ai.util.sandbox` module via monkeypatch, keeping the success-path coverage on every environment. No runtime code changes. |
| 6.74.1 | 2026-07-21 | **fix: CI lint gate — remove one unused test import.** The v6.74.0 tag CI failed on the deterministic ruff F-rule gate: `tests/test_devtools_benchmarks.py` carried an unused `types.SimpleNamespace` import added with the final GAIA staging tests. Import removed; no runtime code changes. Fix-forward release so the v6.74.x artifacts build (the published v6.74.0 tag is never re-tagged). |
| 6.74.0 | 2026-07-21 | **feat: the acceptance review becomes a reviewer-authored terminating dialogue.** The improvement capsule now LEADS with the actual outcome — aggregate verdict + tier + the real blocker (one shared `panel_reason` reducer feeds capsule, projection, and progress lines) — plus the concrete open obligation ids and one rails line naming every active termination source with its remaining headroom (money/time/rounds/review passes, each from its real source). The do-nothing tail is replaced by the three real moves: FIX the work, REBUT structurally via `obligation_dispositions`, or DECLARE a requirement unreachable here; the obligations clause records disagreement ONLY via dispositions. Obligation identity becomes reviewer-authored: findings carry `disposition_kind` (`new`/`re_raise`); a `re_raise` MUST name an existing catalog id (unknown ids fail closed to `new`, disclosed), and a re-raise REOPENS the row without wiping the agent's argument (`previous_disposition`/`previous_reason`/`reopened_count` survive; the reviewer sees the prior argument and adjudicates it with the commit gate's rebuttal contract). Each acceptance reviewer also emits a typed `dialogue_status` (`continue_actionable`/`unreachable_here`/`stable_disagreement`); a NEW pure reducer over ALL contract-valid actors (not the aggregate-filtered set) applies the panel's own quorum — any contributing continue keeps the loop; a quorum of terminal votes finalizes through the existing honest `best_effort_open_obligations` path with both positions recorded. One short reachability clause + review-register framing joins the acceptance prompt (an outside perspective, not a gate; unreachable-here requirements are classified honestly, never re-raised as blocking). Reviewer prompts split into two cache-marked segments (byte-stable governance + task-stable contract) with the mutable evidence tail unmarked and the slot label moved off byte 0 (concurrent same-model slots share a warm prefix; ≤4 breakpoints asserted on the final payload). Harness truthfulness: GAIA stages official sandbox `Sample.files` via `sandbox().read_file` with exact-relative shared-root lookup + per-file provenance (a declared-but-unresolvable attachment is a typed infra error); a tracked CLB operator patch populates `acceptance_claims` in all three task-body writers + the knowledge-topic steer nudge + a bounded cost-finality wait; the SWE-Pro shard budget is derived as `per_task_cost × scheduled_tasks` (total==cap starvation fails loudly, and `run_pro.py` seeds each task's `TOTAL_BUDGET` from the cumulative ledger spend on the first task of an invocation too — the `i > 1` fast-path defeated the derived total on per-task auto_run drives); CLI/PB readers wait (bounded) for `task_cost_finalized` only on `completed`/`degraded`. Runtime fixes: the light-mode shell guard resolves cwd BEFORE judging repo targets (a resource-root label cwd no longer false-blocks task-drive writes; resolution failure fails closed), and the post-task cost publish uses `try_get_bridge` so headless finalization stops warning about an uninitialized message bus. Governance: the commit/plan minimalism items gain the generative surface-duty (name the existing ARCHITECTURE mechanism when a diff/plan adds a surface). |
| 6.73.0 | 2026-07-20 | **feat: project origin invariant — the start-message-loss class is closed.** The identity of the owner message that starts a project is now CAPTURED AT CHAT INGRESS (where the canonical `chat.jsonl` row is written) and passed BY VALUE through every path — `promote_chat_to_task`, `route_to_project`, `ensure_project_scope`, direct project-room turns, and post-hoc UI conversion (which reads it from the persisted task record). `bind_task_to_project` requires a TYPED origin: the ingress-captured ref (+the full `source_text` for cross-thread origins — the retention-proof copy) or a closed-enum absence reason; omission raises, and a same-project re-bind may one-way enrich a ref-less row (a valid ref is never changed; bindings gain `_schema_version` 1). The content-hash identity lookup (`find_owner_message_ref`) — the root cause behind four serial start-message-loss fixes — is DELETED, and a new DEVELOPMENT.md anti-pattern (content-derived identity for host-minted records) pins the class. The Project history lens synthesizes the start message from the binding's own text when the canonical row left the bounded read window (post-quota, identity-deduped, hard-capped, `origin_projected=true`), so an old project's origin survives any number of chat-log rotations. Same-class neighbors fixed: the memory consolidator's cursor is generation-aware (locates its stored `chat_log_signature` across the ordered archive chain and consolidates `archives[i:]+live`, per-segment signature discipline — up to 99 messages were silently dropped at EVERY ~800KB rotation; an unfindable generation now appends an explicit durable `[MEMORY GAP]` block), a failed durable bind is loud (`log.warning` + typed `project_binding_failed` event) instead of `except: log.debug`, and verification-receipt (`except: pass`) / reflection memory-action (`except: log.debug`) write failures now warn instead of vanishing. |
| 6.72.0 | 2026-07-18 | **feat: chronological chat history and honest UI presentation.** Chat bubbles, live-card roots, photos, videos, and documents now carry sortable epoch `data-ts` values from their raw source timestamps. Timeline insertion orders against the first strictly newer sibling, preserves equal-timestamp arrival order, keeps typing last, and retains append behavior for timestamp-free nodes; inserting older history above a scrolled-up viewport compensates `scrollTop`, while near-bottom autoscroll stays unchanged. Two-pass replay keeps progress-only, terminal-without-summary, disconnected, and nested-subagent cards intact, with task cards anchored by their earliest source event and non-today live/log timestamps showing a date. `FINAL ANSWER:` remains the unchanged protocol-gated backend latch/extractor contract but renders as ordinary assistant/system text instead of an Answer capsule across live, history, and reconnect paths. The chat composer’s Context Mode copy now describes the hot-applied owner setting without promising a next-task boundary, the unchanged busy-state 409 names queued as well as running work, and the chat header reserves top padding from its real measured height so wrapped narrow-viewport headers no longer clip the first message. |
| 6.71.0 | 2026-07-17 | **feat: UI polish — a shared rich-content contract, one composer dock, calm charts, and whole-row disclosures.** Rendered markdown and review disclosures adopt one `.ui-rich-content` contract (reserved list gutter, anywhere-wrapping, no marker bleed outside the card) applied to widget markdown and the Skills review history/findings; the skill-review bubble gets a symmetric bottom timestamp inset. Live-line disclosures toggle from the WHOLE non-interactive row surface (nested buttons/links/selection keep their own behavior; focus lands on the real toggle button; Enter/Space/aria-expanded unchanged) with a larger expand label. Project/side-chat composers dock exactly like the main chat (same absolute overlay, same bottom fade, same scroll reserve — no second fade layer). Widget charts render in a bounded 260-360px box and poll ticks update `chart.data` in place instead of destroy/recreate flicker; background refetches keep the content and show a thin pulsing indicator instead of a loading swap (loading only when there is nothing to show yet). New node tests pin the disclosure guards; the declarative Playwright smoke gains a markdown fixture with geometry asserts (list markers inside the card box) and a bounded-canvas assert. |
Older releases are preserved in Git tags and GitHub releases. Older 6.x rows (including 6.73.2, 6.73.1, 6.70.0, 6.71.1, 6.71.2, 6.65.4, 6.69.0, 6.65.3, 6.65.2, 6.68.0, 6.67.0, 6.65.1, 6.66.0, 6.65.0, 6.64.3, 6.64.0, 6.64.2, 6.64.1, 6.63.0, 6.62.0, 6.61.3, 6.61.4, 6.61.0, 6.61.1, 6.60.0, 6.59.0, 6.54.4, 6.58.0, 6.57.0, 6.56.0, 6.55.0, 6.54.2, 6.54.1, 6.54.0, 6.53.4, 6.53.0 and 6.51.0), the 5.2.0 through 5.33.0-rc.6 rows, and former `4.0.0` rows are rolled off to respect the P9 changelog cap; their full bodies remain at their git tags.

---

## License

[MIT License](LICENSE)

Created by [Anton Razzhigaev](https://t.me/abstractDL) & Andrew Kaznacheev
