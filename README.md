# Ouroboros

[![GitHub stars](https://img.shields.io/github/stars/razzant/ouroboros?style=flat&logo=github)](https://github.com/razzant/ouroboros/stargazers)
[![Downloads](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Frazzant%2Fouroboros%2Fbadges%2Fdownloads.json)](https://github.com/razzant/ouroboros/releases)
[![Website](https://img.shields.io/badge/website-ouroboros--agent.ai-c93545.svg)](https://ouroboros-agent.ai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![macOS 12+](https://img.shields.io/badge/macOS-12%2B-black.svg)](https://github.com/razzant/ouroboros/releases)
[![Linux](https://img.shields.io/badge/Linux-x86__64-orange.svg)](https://github.com/razzant/ouroboros/releases)
[![Windows](https://img.shields.io/badge/Windows-x64-blue.svg)](https://github.com/razzant/ouroboros/releases)
[![OuroborosHub](https://img.shields.io/badge/OuroborosHub-skills%20marketplace-8A2BE2.svg)](https://github.com/razzant/OuroborosHub)
[![Version 6.94.0](https://img.shields.io/badge/version-6.94.0-green.svg)](VERSION)

Ouroboros is an open-source, general-purpose AI agent whose identity, durable memory, and history continue across tasks and restarts. It works on external projects, coordinates a live swarm of specialist agents, and can rewrite the implementation it runs on, including its code, architecture, prompts, tools, and dependencies. Reflection can also change how it understands itself without severing that continuity.

It runs as a native desktop app or through a headless CLI. The runtime keeps its repository, durable memory, history, and interface on your machine, while model inference can use remote APIs you configure or a local GGUF model.

The charts below are self-reported results on Terminal-Bench 2.1, OSWorld-Verified, and CL-Bench, measured against Codex, Claude Code, Cursor, and Hermes — on the same model where a matched pair was run, and against the public leaderboard where it was not.

<p align="center">
  <a href="https://ouroboros-agent.ai/benchmarks/"><img src="assets/bench-terminal-bench.svg" width="760" alt="Terminal-Bench 2.1: Ouroboros against Claude Code, Codex CLI, Cursor CLI, and Hermes on matched models, with a same-harness portability row"></a>
</p>

<p align="center">
  <a href="https://ouroboros-agent.ai/benchmarks/"><img src="assets/bench-osworld.svg" width="375" alt="OSWorld-Verified: Ouroboros against the public leaderboard, including the matched Claude Sonnet-4.6 pair"></a>
  <a href="https://ouroboros-agent.ai/benchmarks/"><img src="assets/bench-cl-bench.svg" width="375" alt="CL-Bench: Ouroboros against in-context learning baselines, Claude Code, and Codex on matched models"></a>
</p>

## Install

### macOS (Apple silicon)

1. Open the [latest stable release](https://github.com/razzant/ouroboros/releases/latest) and download `Ouroboros-<version>.dmg`.
2. Open the DMG and drag `Ouroboros.app` onto the **Applications** shortcut.
3. Open Ouroboros from Applications. If Gatekeeper asks, right-click the app and choose **Open**.

<p align="center">
  <img src="assets/install-macos.png" width="760" alt="Ouroboros DMG window with a large arrow from Ouroboros.app to the Applications shortcut and Install CLI.command below">
</p>

Optional CLI: after the app is in Applications, double-click `Install CLI.command` in the mounted DMG. It creates a user-local `ouroboros` command without sudo.

To run tasks, configure at least one supported remote provider API key or a local GGUF model. The first-run wizard guides model access, review policy, and budget setup.

### Linux and Windows

- **Debian / Ubuntu / Astra Linux x86_64:** when the selected release lists `ouroboros_<version>_amd64.deb`, download it and run `sudo apt install ./ouroboros_<version>_amd64.deb`. It installs Git as a package dependency, installs Ouroboros to `/opt/ouroboros`, puts `ouroboros` on `PATH`, and adds a desktop entry.
- **Fedora / RHEL x86_64:** when listed, download `ouroboros-<version>-1.x86_64.rpm` and run `sudo dnf install ./ouroboros-<version>-1.x86_64.rpm`. Same layout and Git dependency as the `.deb`.
- **RED OS 8 x86_64:** when listed, download `ouroboros-<version>-1.red80.x86_64.rpm` and run `sudo dnf install ./ouroboros-<version>-1.red80.x86_64.rpm`. It carries the `red80` release tag. CI attempts non-blocking install-and-run smokes on Astra Linux 1.8 and RED OS 8; inspect the tagged workflow run for their outcome.
- **Other Linux x86_64:** from the [latest stable release](https://github.com/razzant/ouroboros/releases/latest), download `Ouroboros-<version>-linux-x86_64.tar.gz`, extract it, and run `./Ouroboros/Ouroboros`. The optional CLI installer is `./Ouroboros/bin/install-ouroboros-cli`.
- **Windows x64:** from the [latest stable release](https://github.com/razzant/ouroboros/releases/latest), download `Ouroboros-<version>-windows-x64.zip`, extract it, and run `Ouroboros\Ouroboros.exe`. The optional CLI installer is `Ouroboros\bin\install-ouroboros-cli.cmd`.

Prerelease artifacts stay on their tag pages; `/releases/latest` points to the latest stable release. If bundled browser tools on Linux need host libraries, run `./Ouroboros/python-standalone/bin/python3 -m playwright install-deps chromium webkit`. See the [full install and verification guide](https://ouroboros-agent.ai/install/) for source setup and release proof files.

Use your existing **Codex, Claude Code, or Cursor subscriptions** for
delegated coding and review — Ouroboros drives them through
[Claudexor](https://github.com/razzant/claudexor), its bundled multi-harness
engine. Connect accounts in **Settings → Agents**; no separate
install is needed. Works on macOS and Linux. Release artifacts carry the exact
reviewed engine archive; source checkouts fetch that same pinned archive on
first use. Connecting an account installs or repairs the engine in the
foreground, and delegated work does the same lazily. If that checkout or an
older package lacks the exact tested Node, the same action obtains its
review-bound official archive too. A newer pinned engine is staged while the
current daemon keeps running, then activates on its next natural start. This
also covers upgrades from older Ouroboros versions that did not bundle
Claudexor.

---

Ouroboros first booted on February 16, 2026. During the following 48 hours, the repository advanced from the v4.1 line to v6.2.0. The self-authored record preserved from that period counts 32 evolution cycles. That first generation ran in Google Colab through Telegram and remains preserved on the [`legacy-google-colab`](https://github.com/razzant/ouroboros/tree/legacy-google-colab) branch and its [original project page](https://ouroboros-agent.ai/archive/first-generation/); the current generation carries the same identity into a native desktop and headless runtime.

<p align="center">
  <img src="assets/evolution.png" width="760" alt="Code, prompt, and memory growth across Ouroboros releases, from v3.0.0 to the v6.85 line">
</p>

> ⭐ **[Star Ouroboros](https://github.com/razzant/ouroboros)** to follow its next evolution. A star also helps more people find the project, trace its history, and take part in what it becomes.

Reviewed skills, transport bridges, tools, and widgets are available through [OuroborosHub](https://github.com/razzant/OuroborosHub).

<p align="center">
  <img src="assets/swarm.jpg" width="760" alt="A live subagent swarm inside the Ouroboros chat: nested planner, builder, and researcher tasks with their outcomes">
</p>

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

<p align="center">
  <img src="assets/game-demo.png" width="760" alt="A project room where Ouroboros built a 3D game, verified it with a screenshot, and served it locally">
</p>
<p align="center">
  <img src="assets/skill-hub.png" width="760" alt="OuroborosHub inside the app: official reviewed skills, each security-reviewed before it can be enabled">
</p>

This list is an orientation, not a second specification. [BIBLE.md](BIBLE.md) defines Ouroboros's identity and constitutional boundaries; [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) are the current technical sources of truth.

---

## Benchmarks

Ouroboros has reproducible self-reported state-of-the-art results on Terminal-Bench 2.1, OSWorld-Verified, and CL-Bench. In those model-matched results, it leads Codex, Claude Code, Cursor, and Hermes. The public SWE-bench Pro matched pair is a statistical tie with Codex CLI. A separate GAIA campaign reports 129/165 for Ouroboros and 131/165 for Claude Code, with strict pass@1 at 128/165 for both; its scrubbed trace capsule is still pending. Upstream review can take time, so open submissions are marked without delaying publication. Read every row as model plus harness because the same model can score differently inside a different harness.

| Benchmark | Model | Ouroboros | Comparison | Status | Evidence |
|-----------|-------|----------:|------------|--------|----------|
| Terminal-Bench 2.1 | Claude Opus-5 high | **86.74%** after zeroing one disclosed reward-hack trial (raw: 86.97%) | Claude Code + Fable 5: 83.8% | Self-reported, submission open | [submission](https://github.com/harbor-framework/terminal-bench-2-1/pull/175) · [run](https://hub.harborframework.com/jobs/2b145543-edeb-4a3b-b46f-4800310f1182) |
| Terminal-Bench 2.1 | Claude Opus-4.8 high | **80.22%** | Claude Code: 78.9% | Self-reported, public run | [run](https://hub.harborframework.com/jobs/4b8e244f-8ab0-4d28-8218-7cf346282faa) |
| Terminal-Bench 2.1 | GPT-5.5 | **84.3%** | Codex CLI: 83.1% | Self-reported, public run | [run](https://hub.harborframework.com/jobs/f02fd019-23e1-495f-af0a-ebd9a65f3079) |
| Terminal-Bench 2.1 | Grok-4.5 | **84.94%** after a reward-hack audit | Cursor CLI: 79.3% · Hermes: 77.53% | Self-reported, submission open | [submission](https://github.com/harbor-framework/terminal-bench-2-1/pull/146) |
| OSWorld-Verified | Claude Opus-5 | **90.69%** | previous best on the public board: 90.19% | Self-reported, full traces | [full traces](https://huggingface.co/datasets/razzant/ouroboros-osworld-verified-opus5) |
| OSWorld-Verified | Claude Sonnet-4.6 | **83.27%** | Pointer: 81.45% | Self-reported, full traces | [full traces](https://huggingface.co/datasets/razzant/ouroboros-osworld-verified-sonnet46) |
| CL-Bench | Claude Sonnet-4.6 | **0.2301, rank 1** | previous top: 0.1960 | Self-reported, submission open | [submission](https://github.com/pgasawa/continual-learning-bench/pull/10) · [full traces](https://huggingface.co/datasets/razzant/ouroboros-clbench-traces) |
| SWE-bench Pro | GPT-5.6-luna | 58.2% | Codex CLI: 59.4%, with no significant difference | Self-reported, matched traces | [matched-pair traces](https://huggingface.co/datasets/razzant/swepro-luna-matched-pair) |
| GAIA | Claude Sonnet-5 | 129/165, 78.2% | Claude Code: 131/165, 79.4%; strict pass@1 was 128/165 for both | Self-reported, scrubbed trace capsule pending | [methodology](devtools/benchmarks/gaia/METHODOLOGY.md) |

Benchmark adapters, run scripts, and per-benchmark methodology live in [`devtools/benchmarks/`](devtools/benchmarks/). The [benchmark evidence page](https://ouroboros-agent.ai/benchmarks/) gives a text-first summary for search and retrieval. The full story, including protocols, reward-hack audits, and leakage findings, is in the [launch write-up](https://habr.com/ru/companies/airi/articles/1065428/) (Russian).

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

Output: `dist/Ouroboros-<VERSION>.dmg`, containing `Ouroboros.app`, an `Applications` shortcut, and `Install CLI.command`. Omit `OUROBOROS_SIGN=0` when a Developer ID signing identity is configured.

### Linux (.tar.gz)

```bash
bash scripts/download_python_standalone.sh
bash build_linux.sh
```

Output: `dist/Ouroboros-<VERSION>-linux-<arch>.tar.gz`, containing `Ouroboros/bin/install-ouroboros-cli`. If bundled browser tools need host libraries, run `./Ouroboros/python-standalone/bin/python3 -m playwright install-deps chromium webkit`.

### Linux (.deb and .rpm)

Wraps the payload `build_linux.sh` just produced, so run it afterwards:

```bash
sudo apt-get install -y dpkg-dev rpm   # rpm provides rpmbuild
bash scripts/build_linux_packages.sh
```

Output: `dist/ouroboros_<VERSION>_amd64.deb`, `dist/ouroboros-<VERSION>-1.x86_64.rpm` and `dist/ouroboros-<VERSION>-1.red80.x86_64.rpm` (RED OS 8). All three declare Git as a runtime dependency and install to `/opt/ouroboros` with a `/usr/bin/ouroboros` symlink and a desktop entry. The Linux launcher is built by the bundled portable Python so the build runner cannot raise its glibc floor. `bash scripts/smoke_linux_packages.sh official <deb> <rpm> <red80-rpm>` installs all three through `apt` or `dnf` in Ubuntu 22.04 and Fedora 42 containers, resolves Git, and checks both the real CLI and a bounded desktop-launcher start; this lane gates the release. Swap `official` for `vendor` to repeat the check on Astra Linux 1.8 and RED OS 8 images from the vendors' own registries — that lane runs informationally in CI, so an outage at a third-party registry cannot block a tagged release.

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
| 6.94.0 | 2026-08-10 | **feat: external-project tasks can finally delegate onto the subscription substrate, and the ordinary finalization nudge tells the truth about delegated runs.** The Slime Lab Escape saga (4 attempts, ~$215, the game built twice) exposed that the workspace tool envelope predates delegation: tasks in external project workspaces — and their read-only children — were resolved onto the harness route and told to be nannies while `delegate_start`/`delegate_wait`/`delegate_cancel` were filtered out of their toolset, so every "nanny" burned metered opus tokens and the subscription paid $0. The envelope now carries the delegate verbs plus `switch_model` and the `send_photo`/`send_video`/`send_file` media family (a task contract demanded send_photo while the filter hid it), with an invariant test pinning both child profiles as strict subsets so a future tool cannot silently diverge. `enable_tools` distinguishes "hidden by policy: <reason>" from "not found" through the new read-only `policy_hidden_reason` (drift-pinned to `get_schema_by_name` across six context variants); the nanny finalization nudge consults durable delegate-custody evidence — suppressed when the verbs are structurally invisible, and speaking `NANNY_DELEGATED_RUN_FAILED` with the terminal states when a child delegated and the run crashed, so a killed run can never again be misread as a judgment call ("0 delegate_start calls" accused children whose runs died in 6 seconds). The dispatch note tells a nanny to decide delegation FIRST with typed cost classes (known-zero when the route settled $0; estimated/undisclosed is never zero; metered is real money), and the swarm-router promote turn sees the built-in tool-name envelope so a contract can no longer demand a built-in the runtime does not expose. The landed amendments close the honesty loop end to end: a capability preflight before the first paid round proves a harness-dispatched child can actually see all three delegate verbs — an explicit harness pin blocks typed with zero spend (`delegate_tools_invisible`; `delegate_visibility_unverified` when toolset introspection itself failed), while `auto` falls back LOUDLY to native with a typed capability delta; execution reporting speaks the purely factual `actual_substrate` vocabulary (`harness_used`/`harness_attempted`/`native_only`, derived from custody evidence alone, raw attested counts riding beside the enum, and the claim omitted entirely when evidence is unreadable); the nanny evidence distinguishes PENDING from FAILED — `NANNY_DELEGATED_RUN_PENDING` says wait or cancel over an in-flight run instead of accusing it; the promoted-task toolset is resolved from LIVE registry availability (typed `unavailable_builtin_tools` reasons) instead of a static allowlist union; and `evidence_read_failed` renders as typed unknown ("evidence unavailable"), never as a false no-run receipt — the `wait_tasks` projection then omits the run counts and the substrate claim entirely (an unread log yields no numeric facts). Disclosed residual: the FORCED-finalization note remains trace-only — its honesty upgrade lands with the lifecycle release. This release also integrates community PR #176: native Linux `.deb`/`.rpm` packages join the release pipeline (contributed by Fgeeha). |
| 6.93.1 | 2026-08-10 | **fix: the three v6.93.0 tag-matrix failures.** chmod-based unreadability is injected instead (Windows chmod cannot revoke reads); two `read_text()` calls gain explicit utf-8 (server.py carries non-ASCII bytes that cp1252 rejects); and the overlapping dismiss/start smoke now pins the merge’s transition-queue semantics — the overlapped start lands after the dismissal settles instead of being dropped — asserting the preserved C7 invariant as order. Test-only changes. |
| 6.93.0 | 2026-08-09 | **feat: onboarding connects agent plans, Settings gets an Agents tab, and the account surfaces stop stating more than they know.** Two sprints integrated on one backbone. The wizard is served by the live gateway on every host and gains a skippable Connect-your-agents step with sign-in through the same login cards Settings uses; completion is one atomic request, and install-time defaults point reviewers and subagents at connected subscriptions without ever overwriting an owner choice. A new Agents tab holds the account cards per family, engine-contract removal, one service banner, and the delegation controls; docs/DESIGN.md becomes the visual authority, and the ratified BIBLE P3 amendment admits owner-declared retrieving scope reviewers as an alternate authoritative delivery mode. Underneath, `GET /api/claudexor/status` carries per-facet read provenance — reads:{catalog, accounts, quota}, each ok\|not_read\|failed — so a lazily-started daemon’s silence stops rendering as "no account connected": a facet is ok only when the full promised envelope arrived, refused per-harness model reads are disclosed, and POST /api/claudexor/wake is the owner’s start button behind Refresh. The client reads it all through ONE store (single writer, visibility-gated polling, dispose): the status line asks the facets before the aggregate, a partial refusal names what went unread, a wake refusal expires only on proven recovery, saved reviewer pins are labelled from their own facet ("not checked", never "not in discovery", until the facet was read), and a login seen online is monotone evidence. |
| 6.92.1 | 2026-08-08 | **chore: the managed Claudexor runtime pin moves to 3.3.13.** The reviewed pin (`ouroboros/claudexor_runtime_pin.json`) selects the release the delegation lanes provision and self-heal to, so a host running an older engine converges on the new one at the next handshake: 3.3.13 carries the dev-hygiene follow-up (Spotlight-shielded app bundle, dev-build chip suppression, a skew-safe opt-in for the gc data-root advisory, data-root allowlist truth, and confinement-policy shape validation ahead of the platform-availability branch). Pin fields move together — version, build sha, archive URL, sha256 and size; the Node artifact set (24.16.0) and protocol major 3 are unchanged. |
| 6.92.0 | 2026-08-08 | **fix: the four submarine-task failure classes are closed — budget honesty with a typed soft landing, acceptance bypasses that leave a record, one global prompt-cache TTL, and target-aware git everywhere.** The in-task cost stop becomes a typed ceiling (disabled/active/exhausted_soft_land/unknown) bound to the root tree cap: the deciding spend is the ledger-accounted TREE number with a disclosed own-cost fallback and a 120s staleness bound, a root cap with no working room soft-lands the task through the graceful wrap-up instead of running uncapped, waits terminate on SETTLED statuses with typed unknown_task_id rows plus a children_roster repair surface, and the root task detail gains a read-side cost_breakdown. Every forced rail (budget, round limit, deadline, provider death, unabsorbed children) now stamps a typed acceptance-bypass record from a closed reason enum — an owed-but-bypassed panel is no longer indistinguishable from "no panel warranted"; acceptance_claims flow contract-first from plan scope through wave freeze to schedule_subagent (success_criteria becomes an input alias), and reviewer evidence_refs resolve by exact packet membership, feeding only the release-clean bit. Prompt caching gets the owner's ONE global TTL (OUROBOROS_PROMPT_CACHE_TTL, default 1h) applied at the send-time finalizer with applied-TTL-aware pricing, cache-cold-restart telemetry and a cache-horizon note on long waits; emergency compaction splits necessity from utility with hysteresis, so an incompressible frozen frame stops buying per-round no-op passes. The shell git policy becomes one composed target-aware resolver — read-only git works everywhere, mutating git is judged by its real target/destination (bidirectional, casefolded, symlink-resolved containment), the strict self_worktree lane and the network fence stay intact; file-less project promotes auto-provision and bind a genesis workspace; ARCHITECTURE.md follows the owner context mode for every task class while DEVELOPMENT.md keys on the active repo binding (D-ARCH/D-DEV); confinement refusals become typed, legible policy denials. The unset `OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS` default becomes surface-aware — LIGHT mode now allows acting children on the `external_workspace`/`genesis` surfaces (previously unset meant all surfaces off there) while keeping `self_worktree` off, with a truthful Auto state in Settings. |
| 6.91.1 | 2026-08-08 | **fix: the SSE-follow performance budget test measures on-disk bytes correctly on Windows.** The release full-test matrix (first to run this suite on windows-latest — branch pushes only run the quick tier) caught the byte-offset expectation drifting by one byte per appended line: the test wrote its progress fixture in text mode, where Windows translates `\n` to `\r\n`, while the expected byte count was computed from `len(line.encode())`. The product offset tracking is byte-exact; the fixture writers now pin `newline='\n'` so the accounting is platform-stable. Test-only change. |
| 6.91.0 | 2026-08-08 | **feat: the Linux binary boots into the browser when no desktop webview backend exists.** Instead of dying with `WebViewException` on every GTK/QT-less Linux box, the launcher probes the pywebview backend AND whether a display actually exists (Linux-only probe; zero new code runs on macOS/Windows) — importing GTK is not the same as having a display, so an ssh session on a box carrying the system gi bindings gets browser mode instead of the crash (a Qt-selected backend is judged on the session environment alone — probing Qt would itself crash a display-less process; disclosed residual, documented in ARCHITECTURE), installs SIGINT/SIGTERM shutdown handlers BEFORE the lifecycle thread spawns the server (via the new `platform_layer.install_shutdown_signal_handlers` — the signal surface stays behind the platform layer), prints the URL, opens the default browser without blocking, and keeps the process alive while the server lifecycle runs — first-run onboarding included, served by the existing web overlay; a shutdown signal during startup aborts the readiness wait into the same clean teardown. Headless boxes get a clear message instead of a crash. README now leads its integrations story with subscription-powered delegation: use existing Codex, Claude Code, or Cursor subscriptions through the bundled [Claudexor](https://github.com/razzant/claudexor) engine. |
| 6.90.3 | 2026-08-08 | **fix: every native browser dialog is replaced by the in-app dialog, and the harness-accounts panel stops polling a dead daemon forever.** All 21 `window.prompt`/`confirm`/`alert` sites across `web/modules` migrate to the in-app `openConfirmDialog` (a new single-button alert mode included), fixing the two dead `window.prompt` sites on the macOS desktop shell — marketplace version updates and evolution campaign objectives silently returned null there — while preserving each site's contract (empty update version = latest, evolution cancel = do NOT start, `/panic` confirm keeps panic working — its complete confirm-and-send flow is node-tested through the injectable `confirmAndSendPanic`). A static CI gate bans the native dialog trio from `web/modules` so the class stays closed. Harness Accounts now fetches immediately on page show (no 5-second «Checking daemon…»), re-checks on tab/page activation, and login polling backs off (6→30s cap) and gives up honestly into the existing unconfirmed verdict after 10 consecutive failures instead of polling a dead daemon forever. Deleting every reviewer-slot row and saving now surfaces the backend's 400 («needs at least one slot») instead of pretending success while saving nothing. |
| 6.90.2 | 2026-08-08 | **fix: the commit gate asks whether the advisory lane is actually available — route- and slot-aware — not whether an Anthropic key exists.** The bypass test is now route- and slot-aware: a DISABLED advisory slot with a key present used to silently drop the compensating hermetic pytest (neither advisory nor tests ran before triad+scope), and a keyless DELEGATED advisory route — which does run, on the owner's subscription — used to be mislabelled `bypassed` and pay a duplicate full test run every commit. The gate reads the CURRENT advisory availability, not the stored advisory record's status — an owner-accepted residual disclosed at the gate. A malformed reviewer-slot configuration fails CLOSED into that preflight, and the parser now raises its documented `ValueError` for a non-dict advisory route instead of an `AttributeError` that escaped every fail-closed handler. Skill review's optional advisory pre-review now asks the same availability question (still fail-open), so a DISABLED advisory slot no longer dispatches — and no longer spends — review budget the owner switched off. A malformed structured reviewer-slot config also stops being silent where it mattered: plan review returns a typed unavailable result and skill review returns `pending` with the row-precise parse error, instead of quietly running the shipped default panel; task acceptance keeps reading the projected legacy/default panel by owner decision, and the architecture map now says so rather than claiming the refusal is universal. Delegated review-session custody rows carry root/parent task lineage, so a run recovered by restart reconciliation settles its spend on the real root instead of charging the reviewing task as its own. Also unblocks the commit gate on any machine running Ouroboros: the managed-update writer-fence ORDER test reached the operator's LIVE process ledger (the fence's custody step reads an import-time `DRIVE_ROOT` that an isolated `OUROBOROS_DATA_DIR` does not rebind), so it failed on a stale ledger entry and could have killed a ledgered task/session service that happened to be running. |
| 6.90.0 | 2026-08-07 | **fix: the subscription substrate actually carries the work — quota exhaustion is judged against the route's own model, and every silent seam found by the integration audit is closed.** The route-health predicate now scopes quota windows by `applies_to_models` (a Fable-only weekly window no longer takes an opus-pinned route offline for days — the live incident that burned ~$83 of metered API in 43 minutes), treats a spent window with no reset instant as spent rather than healthy, and demands POSITIVE evidence for the whole route: a profile whose quota endpoint answered 429 is unknown-not-dead, so the daemon's own rotation gets to try. A nanny dispatched onto the delegated substrate that reaches finalization with zero `delegate_start` calls gets one structural reminder (the wave-0 child burned $8.89 of metered opus under a subscription dispatch and only its prose admitted it); the decision stays the child's. The blocking scope reviewer no longer silently runs at `medium` on stock installs (the legacy slot path now honours `OUROBOROS_EFFORT_SCOPE_REVIEW`); scope and advisory session schemas grow a one-finding floor so the one answer their contracts reject by design cannot arrive schema-blessed; Panic Stop kills the owned Claudexor daemon's process group (subscription runs no longer outlive the red button); startup reconciliation of orphaned delegated runs goes through the ENSURE path instead of handshaking the corpse it just reaped — which also adopts a staged runtime update on restart; the production poll retries the engine's transient Git atomic-object ENOENT exactly once (CI had learned this tolerance while the live `delegate_wait` kept failing); `harness:effort` parses per the documented grammar and an unparseable configured route warns instead of silently disabling delegation. Web: "Add account…" replaces `window.prompt` (dead in the desktop WKWebView) with the standard input dialog and shows the normalized profile name before login; the account quota summary applies the same model-scope rule as the resolver; an empty Heavy slot visibly says it inherits Main. The Repo Commit Checklist gains item 23 (`delegated_transport`): the delegation invariants — capability_delta to three destinations, hash-bound EOF reads, typed blockers over silent re-routes, positive-evidence quota, custody lineage, no vendor branching — are now a named review surface. |
Older releases are preserved in Git tags and GitHub releases. Older 6.x rows (including 6.87.5, 6.87.4, 6.87.3, 6.87.2, 6.84.0, 6.87.1, 6.83.0, 6.86.1, 6.81.1, 6.76.0, 6.75.0, 6.74.5, 6.74.4, 6.74.1, 6.74.0, 6.73.2, 6.73.1, 6.73.0, 6.72.0, 6.71.2, 6.71.1, 6.71.0, 6.70.0, 6.69.0, 6.68.0, 6.67.0, 6.66.0, 6.65.4, 6.65.3, 6.65.2, 6.65.1, 6.65.0, 6.64.3, 6.64.2, 6.64.1, 6.64.0, 6.63.0, 6.62.0, 6.61.4, 6.61.3, 6.61.1, 6.61.0, 6.60.0, 6.59.0, 6.58.0, 6.57.0, 6.56.0, 6.55.0, 6.54.4, 6.54.2, 6.54.1, 6.54.0, 6.53.4, 6.53.0, 6.51.0), the 5.2.0 through 5.33.0-rc.6 rows, and former `4.0.0` rows are rolled off to respect the P9 changelog cap; their full bodies remain at their git tags.

---

## License

[MIT License](LICENSE)

Created by [Anton Razzhigaev](https://t.me/abstractDL) & Andrew Kaznacheev
