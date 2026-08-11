# Ouroboros

[![GitHub stars](https://img.shields.io/github/stars/razzant/ouroboros?style=flat&logo=github)](https://github.com/razzant/ouroboros/stargazers)
[![Downloads](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Frazzant%2Fouroboros%2Fbadges%2Fdownloads.json)](https://github.com/razzant/ouroboros/releases)
[![Website](https://img.shields.io/badge/website-ouroboros--agent.ai-c93545.svg)](https://ouroboros-agent.ai/)
[![Technical report](https://img.shields.io/badge/technical_report-arXiv%3A2608.08311-b31b1b.svg)](https://arxiv.org/abs/2608.08311)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![macOS 12+](https://img.shields.io/badge/macOS-12%2B-black.svg)](https://github.com/razzant/ouroboros/releases)
[![Linux](https://img.shields.io/badge/Linux-x86__64-orange.svg)](https://github.com/razzant/ouroboros/releases)
[![Windows](https://img.shields.io/badge/Windows-x64-blue.svg)](https://github.com/razzant/ouroboros/releases)
[![OuroborosHub](https://img.shields.io/badge/OuroborosHub-skills%20marketplace-8A2BE2.svg)](https://github.com/razzant/OuroborosHub)
[![Version 6.97.2](https://img.shields.io/badge/version-6.97.2-green.svg)](VERSION)

Ouroboros is an open-source, general-purpose AI agent whose identity, durable memory, and history continue across tasks and restarts. It works on external projects, coordinates a live swarm of specialist agents, and can rewrite the implementation it runs on, including its code, architecture, prompts, tools, and dependencies. Reflection can also change how it understands itself without severing that continuity.

It runs as a native desktop app or through a headless CLI. The runtime keeps its repository, durable memory, history, and interface on your machine, while model inference can use remote APIs you configure or a local GGUF model.

The technical report, [Ouroboros: A Self-Developing Frontier Coding Agent with Reviewed Core Evolution](https://arxiv.org/abs/2608.08311), describes the reviewed core-evolution system, the 161-day Hope deployment, and the benchmark campaigns summarized below. [Paper page](https://ouroboros-agent.ai/paper/) · [Hugging Face](https://huggingface.co/papers/2608.08311)

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
- **Other Linux x86_64:** from the [selected release](https://github.com/razzant/ouroboros/releases), use `Ouroboros-<version>-linux-x86_64.AppImage` when listed: make it executable and run it, or pass `--cli <args>` for the bundled CLI. Git must already be installed. If that release does not list an AppImage, use the extraction-friendly tarball.
- **Windows x64:** from the [latest stable release](https://github.com/razzant/ouroboros/releases/latest), download `Ouroboros-<version>-windows-x64.zip`, extract it, and run `Ouroboros\Ouroboros.exe`. The optional CLI installer is `Ouroboros\bin\install-ouroboros-cli.cmd`.

Prerelease artifacts stay on their tag pages; `/releases/latest` points to the latest stable release. If bundled browser tools on Linux need host libraries, run `./Ouroboros/_internal/python-standalone/bin/python3 -m playwright install-deps chromium webkit`. See the [full install and verification guide](https://ouroboros-agent.ai/install/) for source setup and release proof files.

#### Install the Linux AppImage

When a release lists the AppImage, user-level installation means copying the
portable executable to a stable path and making it executable; it does not need
root access. Ouroboros bootstrap still requires Git on the host:

```bash
VERSION=x.y.z
install -Dm755 "./Ouroboros-${VERSION}-linux-x86_64.AppImage" \
  "$HOME/Applications/Ouroboros.AppImage"
"$HOME/Applications/Ouroboros.AppImage"
```

The embedded desktop file and icon allow compatible AppImage integration tools
to register that stable path with the application menu. The same file exposes
the packaged CLI:

```bash
"$HOME/Applications/Ouroboros.AppImage" --cli status
```

If FUSE mounting is unavailable, extract and run ephemerally instead:

```bash
APPIMAGE_EXTRACT_AND_RUN=1 "$HOME/Applications/Ouroboros.AppImage"
```

Chromium and WebKit binaries are bundled, but their distro-level shared
libraries remain host dependencies. If a browser engine reports missing
libraries, use the native `.deb`/`.rpm` package where available, or extract the
AppImage and let its bundled Playwright report/install the packages required by
your distribution:

```bash
"$HOME/Applications/Ouroboros.AppImage" --appimage-extract
./squashfs-root/usr/lib/ouroboros/_internal/python-standalone/bin/python3 \
  -m playwright install-deps chromium webkit
```

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

## Install the isolated CLI with uv

For a user-level CLI/server install without cloning a working tree, uv can
build Ouroboros directly from the contribution branch:

```bash
uv tool install "git+https://github.com/razzant/ouroboros.git@ouroboros"
ouroboros --help
```

The tool environment is isolated and exposes the `ouroboros` and
`ouroboros-web` commands. Update or remove it with:

```bash
uv tool upgrade ouroboros
uv tool uninstall ouroboros
```

This Git-branch form follows the latest `ouroboros` commit and resolves the
dependencies declared in `pyproject.toml`; `uv tool install` does not consume
the repository's `uv.lock`. Replacing `ouroboros` after the `@` with a reviewed
full commit SHA pins the Ouroboros source revision, but dependencies are still
resolved from `pyproject.toml`. Use the source setup below for a lock-verified
environment, development, repository tests, and the complete browser extras,
or use a platform release artifact for the packaged desktop runtime.

---

## Run from Source

### Requirements

- Python 3.10+
- uv 0.12.1 (the exact resolver version pinned by this checkout)
- macOS, Linux, or Windows
- Git
- [GitHub CLI (`gh`)](https://cli.github.com/), optional unless you use GitHub integration

### Setup

Install the pinned resolver version:

```bash
curl -LsSf https://astral.sh/uv/0.12.1/install.sh | sh
```

Windows PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/0.12.1/install.ps1 | iex"
```

```bash
git clone https://github.com/razzant/ouroboros.git
cd ouroboros
uv sync --locked --extra browser --group dev
source .venv/bin/activate
```

Windows PowerShell:

```powershell
uv sync --locked --extra browser --group dev
.\.venv\Scripts\Activate.ps1
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

`pyproject.toml` is the direct-dependency authority and `uv.lock` is the
cross-platform resolution lock. Release builds install the generated
`requirements-runtime.lock` compatibility export into embedded interpreters
that intentionally ship pip rather than uv. Build-only requirements are
exported ephemerally from `uv.lock` and are not committed. The tiny
`requirements.txt` file is only a pointer to that export for already-released
managed updaters; it is not a second dependency declaration. After changing
dependencies, refresh the reviewed lock and runtime export with:

```bash
uv lock
uv export --locked --no-dev --extra browser --no-emit-project --no-hashes --no-annotate --output-file requirements-runtime.lock
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

### Linux (.AppImage and .tar.gz)

```bash
bash scripts/download_python_standalone.sh
bash build_linux.sh
```

Outputs: `dist/Ouroboros-<VERSION>-linux-<arch>.AppImage` and the extraction-friendly `dist/Ouroboros-<VERSION>-linux-<arch>.tar.gz`. The AppImage needs host Git; run it after `chmod +x`, or pass `--cli` to reach its bundled CLI. If FUSE is unavailable, set `APPIMAGE_EXTRACT_AND_RUN=1` when launching it. The tarball contains `./Ouroboros/bin/install-ouroboros-cli`. If bundled browser tools need host libraries, run `./Ouroboros/_internal/python-standalone/bin/python3 -m playwright install-deps chromium webkit` from the extracted tarball.

On a build host where system packages are managed separately, set `OUROBOROS_SKIP_PLAYWRIGHT_INSTALL_DEPS=1`; Chromium and WebKit are still downloaded and bundled, but the build does not invoke `sudo` to install host libraries.

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
| 6.97.2 | 2026-08-11 | **fix: nested AppImage cleanup has a real lifecycle owner, and path guards keep the same fail-closed meaning on every supported Python.** The marker-gated `AppRun` now remains between the type-2 runtime and desktop launcher, waits for the launcher recorded by the PID file, removes only its verified extraction, and removes the empty private runtime base before returning the payload status. Linux release smoke proves that process chain and both cleanup boundaries. A shared allow-missing resolver first validates existing path ancestry, so Python 3.13's changed symlink-loop behavior can no longer turn an unresolvable delegated write root, read target, or harness home into a partially resolved path; ordinary missing targets remain supported. Packaging and delegated-containment correctness only; workspace authority and frozen Tool API contracts are unchanged. |
| 6.97.1 | 2026-08-11 | **fix: Linux AppImage relaunches use independent extraction roots, and Windows release builds avoid a false dirty-tree failure.** Nested extract-and-run starts no longer share the outer CLI runtime's temporary payload; Linux release smoke waits for the owning runtime to finish cleanup instead of racing a fixed pathname timeout. The generated runtime lock is pinned to LF so `uv export` preserves the strict clean-tree bundle gate on Windows, and that gate now reports offending paths. Packaging and release-CI only; workspace authority and frozen Tool API contracts are unchanged. |
| 6.97.0 | 2026-08-11 | **feat: workspaces now choose the default focus without narrowing Ouroboros's top-level authority, while target-sensitive tool calls bind each selected physical target once.** Immutable resource bindings now carry project, system-repository, task, data, and exact skill-payload roots through file, edit, process, service, VCS, verification, and skill-lifecycle consumers, with batch calls retaining one binding per physical target. Omitted root or cwd stays in the active project; explicit typed roots can operate on authorized system and non-native-skill resources without changing workspace, and ambiguous skill-topology collisions fail before lifecycle mutation. Canonical skill lifecycle state remains shared while task evidence stays task-custodied. This release also completes the uv-managed dependency migration, adds the Linux AppImage release artifact with its hardened launcher lifecycle, and publishes the technical report page for arXiv:2608.08311 with citation metadata. Frozen Tool API contracts remain unchanged; no Tool API v3 or skill-service layer is introduced. |
| 6.96.2 | 2026-08-11 | **fix: the hermetic preflight capture is byte-faithful on Windows too, and a POSIX-only filename test is skipped there.** The v6.96.1 release full-test matrix (first tag build since the byte-faithful capture landed) caught two windows-latest failures in the preflight runner. The hermetic worktree checked HEAD out and applied the `--binary` candidate diff under the runner's default `core.autocrlf=true`, so an LF payload landed on a CRLF-converted base and every line ending was mangled — breaking the exact byte-faithfulness the capture exists to hold; the `worktree add` and `git apply` now pin `core.autocrlf=false` + `core.eol=lf` (no `.gitattributes text` directive governs the affected paths, so the override is authoritative and inert on POSIX). The second failure was a synthetic non-UTF-8 filename test asserting an os.fsdecode round-trip that only holds under POSIX surrogateescape: Windows uses a UTF-16 filesystem where such a name cannot exist and its fs codec raises on the injected 0xff byte, and git never emits such a name there, so the production path is unaffected — the test is now skipped on Windows with that reason. Test/CI-correctness only; no runtime behavior change on any platform. |
| 6.96.1 | 2026-08-11 | **chore: the managed Claudexor runtime pin moves to 3.3.15.** The reviewed pin (`ouroboros/claudexor_runtime_pin.json`) selects the release the delegation lanes provision and self-heal to, so a host running an older engine converges on the new one at the next handshake: 3.3.15 carries the CODEX_HOME seatbelt metadata carve-out and the managed-toolchain exec carve-out that let confined delegated codex runs actually start on macOS, plus formal v6 release governance. Pin fields move together — version, build sha, archive URL, sha256 and size; the Node artifact set (24.16.0) and protocol major 3 are unchanged. |
| 6.96.0 | 2026-08-11 | **fix: the owner's final answer goes out through the live worker event channel BEFORE blocking post-task cognition, and post-task synthesis reads sealed ground truth instead of reconstructing the delivery from memory (sprint phase C).** Motivated by the e9108a09 incident: a project root's final answer sat buffered behind minutes of blocking post-task work (and was lost when that work died), and the reflection then described a delivery that never happened. The worker now sends the final `send_message` over the LIVE event queue before summary/reflection/consolidation start, while `task_done` stays LAST via the buffered return (an early task_done would release the queue slot and start child-drive cleanup mid-post-task). The live slot is selected by the finalizing task's own id — a buffered proactive message can never hijack it — and `delivery_id` carries the real task id. Never-lost-never-doubled: both copies ride the same `delivery_id` and the supervisor suppresses the duplicate only AFTER a successful first send, so a raising send is retried by the buffered copy ("never lost" outranks "never doubled"). Summary and reflection prompts receive one sealed final package — the delivered result text plus an artifact manifest REUSED from the stored result's own artifact records — as mandatory ground truth (a prompt input, never a validator). The projects registry gains a durable per-project `last_task_result_id` pointer replacing the newest-64 global result scan: stamped at project-task finalization, with absent-pointer-only write-back — an unresolved non-empty pointer is served from the scan WITHOUT being overwritten, so the split-drive copy-back window cannot regress it — and a disclosed repeats-until-found self-heal for pre-pointer projects. Project reflections are read BACK into project-task context (bounded tail, the same resolver the writer uses; the canonical log keeps a bounded pointer row), `project_name` is inherited on promotion in a projectless room (an explicit input losing to a genuine room binding is disclosed, never silently dropped), finalized terminal accounting survives a late stale child-drive copy-back (`TASK_COST_META_FIELDS` plus rounds/tokens), a finished root whose effective tree is off-registry writes one typed work-location journal row (admission-time sha disclosed by design), and the moved-HEAD fail-closed patch check is restricted to `self_worktree` — in a shared tree the parent's own commits legitimately move HEAD. New module `ouroboros/task_finalization.py` carries the delivery + sealed-package mechanics out of `agent_task_pipeline.py` at its module ceiling. |
| 6.95.0 | 2026-08-11 | **fix: the task lifecycle tells the truth at every forced exit — delivery-control protocol JSON, provider death, unabsorbed children, and the reaper's silence (sprint phase B).** Forced delivery-control resolution is PURE and no-retry (`_resolve_forced_delivery_control`): under the armed latch, ANY parsed object carrying the `delivery_control` key (unknown verbs included) and any JSON-looking text that fails to parse degrade to the retained candidate with the typed `delivery_control_degraded` reason — protocol JSON can no longer ship as the owner's answer, while unarmed JSON passes through untouched (armed prose stands; disclosed residual). Parents disposition children in batch: a `tree_note(kind='decision')` payload accepts a `children` array expanded into the same per-child authoritative ledger rows — exact `child_result_sha256` binding and per-entry rejection, single form unchanged. Acceptance review now runs on the forced `children_unabsorbed` rail (owner Q2A) through the ordinary panel entry point before the answer is sealed, with the undispositioned children (ids/statuses/hashes; explicit omission marker past 20) in the evidence packet; a requested improvement pass the rail cannot grant terminalizes as `finalized_unaccepted` with the typed `revision_unavailable_on_forced_rail` reason. Provider-death honesty: a provider-killed task terminalizes `failed`/`provider_unavailable` instead of "completed (best effort)" (the code left `BEST_EFFORT_REASON_CODES`; salvage text still rides the result body), and the owner gets an immediate "NOT completed" notification with no false resume promise — re-run is the honest verb. The reaper-delivered notification suppression bug is fixed and proven by regression test: the single-shot registry is registered only after a successful send, runs after task-done cleanup and below the ephemeral-turn return, so a raising send is retried later and an ephemeral decision turn gets no duplicate outage ping. A child's settled result stamps the PARENT's own progress (including reaper-delivered terminals via the `final_task_result` fallback), so a coordinator waiting in `wait_tasks` is no longer idle-killed the moment its last child delivers. The forced-path nanny note closes the 6.94.0 disclosed residual: it is grounded in durable custody evidence from the canonical custody root (the same `task_execution_evidence` reducer as the ordinary nudge, which now also reports `delegated_runs_succeeded`) — succeeded runs silence it, unsettled runs get pending wording, settled-without-success gets truthful failure wording, and unreadable evidence never accuses. |
| 6.94.0 | 2026-08-10 | **feat: external-project tasks can finally delegate onto the subscription substrate, and the ordinary finalization nudge tells the truth about delegated runs.** The Slime Lab Escape saga (4 attempts, ~$215, the game built twice) exposed that the workspace tool envelope predates delegation: tasks in external project workspaces — and their read-only children — were resolved onto the harness route and told to be nannies while `delegate_start`/`delegate_wait`/`delegate_cancel` were filtered out of their toolset, so every "nanny" burned metered opus tokens and the subscription paid $0. The envelope now carries the delegate verbs plus `switch_model` and the `send_photo`/`send_video`/`send_file` media family (a task contract demanded send_photo while the filter hid it), with an invariant test pinning both child profiles as strict subsets so a future tool cannot silently diverge. `enable_tools` distinguishes "hidden by policy: <reason>" from "not found" through the new read-only `policy_hidden_reason` (drift-pinned to `get_schema_by_name` across six context variants); the nanny finalization nudge consults durable delegate-custody evidence — suppressed when the verbs are structurally invisible, and speaking `NANNY_DELEGATED_RUN_FAILED` with the terminal states when a child delegated and the run crashed, so a killed run can never again be misread as a judgment call ("0 delegate_start calls" accused children whose runs died in 6 seconds). The dispatch note tells a nanny to decide delegation FIRST with typed cost classes (known-zero when the route settled $0; estimated/undisclosed is never zero; metered is real money), and the swarm-router promote turn sees the built-in tool-name envelope so a contract can no longer demand a built-in the runtime does not expose. The landed amendments close the honesty loop end to end: a capability preflight before the first paid round proves a harness-dispatched child can actually see all three delegate verbs — an explicit harness pin blocks typed with zero spend (`delegate_tools_invisible`; `delegate_visibility_unverified` when toolset introspection itself failed), while `auto` falls back LOUDLY to native with a typed capability delta; execution reporting speaks the purely factual `actual_substrate` vocabulary (`harness_used`/`harness_attempted`/`native_only`, derived from custody evidence alone, raw attested counts riding beside the enum, and the claim omitted entirely when evidence is unreadable); the nanny evidence distinguishes PENDING from FAILED — `NANNY_DELEGATED_RUN_PENDING` says wait or cancel over an in-flight run instead of accusing it; the promoted-task toolset is resolved from LIVE registry availability (typed `unavailable_builtin_tools` reasons) instead of a static allowlist union; and `evidence_read_failed` renders as typed unknown ("evidence unavailable"), never as a false no-run receipt — the `wait_tasks` projection then omits the run counts and the substrate claim entirely (an unread log yields no numeric facts). Disclosed residual: the FORCED-finalization note remains trace-only — its honesty upgrade lands with the lifecycle release. This release also integrates community PR #176: native Linux `.deb`/`.rpm` packages join the release pipeline (contributed by Fgeeha). |
| 6.93.1 | 2026-08-10 | **fix: the three v6.93.0 tag-matrix failures.** chmod-based unreadability is injected instead (Windows chmod cannot revoke reads); two `read_text()` calls gain explicit utf-8 (server.py carries non-ASCII bytes that cp1252 rejects); and the overlapping dismiss/start smoke now pins the merge’s transition-queue semantics — the overlapped start lands after the dismissal settles instead of being dropped — asserting the preserved C7 invariant as order. Test-only changes. |
| 6.93.0 | 2026-08-09 | **feat: onboarding connects agent plans, Settings gets an Agents tab, and the account surfaces stop stating more than they know.** Two sprints integrated on one backbone. The wizard is served by the live gateway on every host and gains a skippable Connect-your-agents step with sign-in through the same login cards Settings uses; completion is one atomic request, and install-time defaults point reviewers and subagents at connected subscriptions without ever overwriting an owner choice. A new Agents tab holds the account cards per family, engine-contract removal, one service banner, and the delegation controls; docs/DESIGN.md becomes the visual authority, and the ratified BIBLE P3 amendment admits owner-declared retrieving scope reviewers as an alternate authoritative delivery mode. Underneath, `GET /api/claudexor/status` carries per-facet read provenance — reads:{catalog, accounts, quota}, each ok\|not_read\|failed — so a lazily-started daemon’s silence stops rendering as "no account connected": a facet is ok only when the full promised envelope arrived, refused per-harness model reads are disclosed, and POST /api/claudexor/wake is the owner’s start button behind Refresh. The client reads it all through ONE store (single writer, visibility-gated polling, dispose): the status line asks the facets before the aggregate, a partial refusal names what went unread, a wake refusal expires only on proven recovery, saved reviewer pins are labelled from their own facet ("not checked", never "not in discovery", until the facet was read), and a login seen online is monotone evidence. |
Older releases are preserved in Git tags and GitHub releases. Older 6.x rows (including 6.92.1, 6.92.0, 6.91.1, 6.90.3, 6.91.0, 6.90.2, 6.90.0, 6.87.5, 6.87.4, 6.87.3, 6.87.2, 6.84.0, 6.87.1, 6.83.0, 6.86.1, 6.81.1, 6.76.0, 6.75.0, 6.74.5, 6.74.4, 6.74.1, 6.74.0, 6.73.2, 6.73.1, 6.73.0, 6.72.0, 6.71.2, 6.71.1, 6.71.0, 6.70.0, 6.69.0, 6.68.0, 6.67.0, 6.66.0, 6.65.4, 6.65.3, 6.65.2, 6.65.1, 6.65.0, 6.64.3, 6.64.2, 6.64.1, 6.64.0, 6.63.0, 6.62.0, 6.61.4, 6.61.3, 6.61.1, 6.61.0, 6.60.0, 6.59.0, 6.58.0, 6.57.0, 6.56.0, 6.55.0, 6.54.4, 6.54.2, 6.54.1, 6.54.0, 6.53.4, 6.53.0, 6.51.0), the 5.2.0 through 5.33.0-rc.6 rows, and former `4.0.0` rows are rolled off to respect the P9 changelog cap; their full bodies remain at their git tags.

---

## License

[MIT License](LICENSE)

Created by [Anton Razzhigaev](https://t.me/abstractDL) & Andrew Kaznacheev
