#!/usr/bin/env python3
"""OSWorld runner: ONE Ouroboros agentic run per task, host-side computer-use bridge.

Unlike ``run_step_agent.py`` (host drives ``env.step`` and Ouroboros is a stateless
per-step action selector with ``--memory-mode empty``), this runner gives Ouroboros
the wheel:

    host: reset VM -> publish VM_IP -> submit ONE task -> wait -> evaluate()
    agent (one run, full memory): screenshot -> reason -> click/type -> screenshot -> ... -> done

The agent acts through the bundled ``unix_computer_use`` skill, whose additive
OSWorld HTTP backend routes ``screenshot``/``click``/``type``/``key``/``scroll``
to the in-VM OSWorld server (GET /screenshot, POST /execute) — the SAME guest
channel ``env.step`` uses. The backend is activated by the ``connections.json`` +
``active_connection.txt`` this runner publishes into the bench data dir's skill
state (see ``_publish_target``); there is no env-var activation path. The brain
stays on the host; only translated pyautogui mutates the guest. ``reset()`` and
``evaluate()`` are the official OSWorld ones.

Protocol note: GUI actions go straight to the guest ``/execute`` server and thus
do NOT populate the official ``DesktopEnv.action_history`` / ``traj.jsonl``; only
the translated ``FAIL`` (for a declared-infeasible task) is an official action.
See ``METHODOLOGY.md`` §7 for the full comparability disclosures.

This is the Terminal-Bench / Pointer shape (persistent agent + computer-use tool),
without installing Ouroboros inside the VM.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from devtools.benchmarks.common.manifests import (
    BenchmarkAdmissionRefused,
    RuntimeAttestationRefused,
    admit_benchmark_run,
    finalize_run_manifest,
    runtime_attestation,
    write_json,
)
from devtools.benchmarks.common.result_index import append_result_index, task_result_row
from devtools.benchmarks.common.run_roots import assert_outside_repo, timestamp_run_id

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKSPACE_ROOT = _REPO_ROOT.parent
VMWARE_FUSION_PATHS = (
    "/Applications/VMware Fusion.app/Contents/Public",
    "/Applications/VMware Fusion.app/Contents/Library",
)

SKILL_NAME = "unix_computer_use"

# The OSWorld task instruction is UNTRUSTED and the VM is driven ONLY through the
# unix_computer_use skill (ext_* tools). Rather than a fragile per-tool DENYLIST
# (which silently misses any host tool added later), the runner keeps a small
# ALLOWLIST of core tools the task legitimately needs and DENIES every other core
# tool — so any host execution/mutation/VCS/GitHub/service/self-mod/chat surface,
# present or future, is blocked by construction. The skill's ext_* tools are not
# core tools, so they are never on the computed denylist and always available.
# `enable_tools` is kept (the agent must enable the computer-use skill), which in
# principle could enable OTHER extensions — but the runner seeds and enables ONLY
# unix_computer_use into a FRESH isolated bench data dir (append-only per task per
# the runbook), so there is no other extension to reach; a reused multi-extension
# data dir is out of the supported bench setup.
# Deliberately NO host filesystem/code read tools (read_file/list_files/search_code/
# query_code): the isolated bench settings.json holds provider API keys, and a
# prompt-injected task is a normal root task that could read_file(root="runtime_data",
# "settings.json") to exfiltrate them. The agent inspects the VM through the skill
# (remote_exec/screenshot), never the host filesystem.
_ALLOWED_CORE_TOOLS = frozenset({
    "list_available_tools", "enable_tools",   # discover + enable the computer-use skill
    "view_image",                             # the vision channel (SEE screenshots)
    "compact_context", "set_tool_timeout",    # agent self-management (no host access)
})


def _core_tool_names() -> set[str]:
    """All built-in (non-extension) core tool names, for the computed denylist."""
    import tempfile

    from ouroboros.tools.registry import ToolRegistry

    tmp = Path(tempfile.mkdtemp(prefix="cu_bridge_toolscan_"))
    reg = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    return {t["function"]["name"] for t in reg.schemas()}


def _host_denied_tools() -> list[str]:
    """Deny every core tool the OSWorld task does not need (allowlist-complement)."""
    return sorted(_core_tool_names() - _ALLOWED_CORE_TOOLS)

# GUI action tools (short skill names) counted for the budget disclosure.
_GUI_ACTION_TOOLS = frozenset({
    "click", "move", "left_click_drag", "mouse_down", "mouse_up",
    "type_text", "key", "hold_key", "scroll",
})


# unix_computer_use ext tools the untrusted task must NOT reach. The runner pins
# the active connection to the published OSWorld VM; a task that could switch the
# backend (use_local/activate_connection local) or retarget it (add_connection)
# would drive the HOST desktop instead — defeating the host lockdown AND the
# fail-closed guarantee. Read-only introspection (list_connections/test_connection)
# stays; the mutating connection-management surface is denied.
_DENIED_SKILL_EXT_TOOLS = ("add_connection", "activate_connection", "use_local", "clear_active_connection")


def _effective_disabled_tools(allow_a11y: bool) -> list[str]:
    """Per-task disabled-tool list = the host-tool complement of the allowlist,
    plus the skill's connection-switching ext tools (the runner pins the VM
    connection), plus ``ax_tree`` unless ``--allow-a11y`` is given (screenshot-only
    by default; enabling it must disclose "a11y tree used"). ext names must be the
    provider-safe full surface names — disabled_tools matches exact names."""
    from ouroboros.extension_loader import extension_surface_name

    disabled = _host_denied_tools()
    disabled += [extension_surface_name(SKILL_NAME, t) for t in _DENIED_SKILL_EXT_TOOLS]
    if not allow_a11y:
        disabled.append(extension_surface_name(SKILL_NAME, "ax_tree"))
    disabled.append("schedule_subagent")  # operator 2026-07-23: subagents=0 no-swarm campaign
    return disabled


def _refuse_live_data_dir(data_dir: Path) -> None:
    """Never publish a bench connection into the owner's LIVE skill state — it
    would hijack the real unix_computer_use skill and point it at a bench VM."""
    live = (Path.home() / "Ouroboros" / "data").expanduser().resolve(strict=False)
    resolved = Path(data_dir).expanduser().resolve(strict=False)
    if resolved == live or live in resolved.parents:
        raise SystemExit(
            f"refusing --data-dir inside the live Ouroboros data root ({live}); "
            "use an isolated bench data dir"
        )


def _dataset_name(variant: str) -> str:
    return {"v2": "OSWorld-V2", "v1": "OSWorld"}.get(variant, f"OSWorld-{variant}")


def _effective_max_rounds(settings_path: Path) -> dict[str, Any]:
    """Report the round budget the bench server actually honors, with provenance.

    The server applies settings.json over env at startup, so settings wins; this
    is best-effort disclosure, not enforcement (there is no per-task step cap)."""
    try:
        settings = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        if isinstance(settings, dict) and settings.get("OUROBOROS_MAX_ROUNDS") is not None:
            return {"value": int(settings["OUROBOROS_MAX_ROUNDS"]), "source": "settings"}
    except Exception:
        pass
    env_val = os.environ.get("OUROBOROS_MAX_ROUNDS")
    if env_val:
        try:
            return {"value": int(env_val), "source": "env"}
        except ValueError:
            pass
    return {"value": 200, "source": "default"}


def _collect_budget_counters(data_dir: Path, latest: dict[str, Any], ouro_task_id: str) -> dict[str, Any]:
    """Disclosure counters for leaderboard comparability (never raises).

    A leaderboard "step" is one model turn; our rounds are not step-equivalent,
    so we publish the raw counts: llm rounds (authoritative, from the task
    result) plus per-tool call counts parsed from the task's own tools.jsonl.
    """
    from ouroboros.extension_loader import extension_name_prefix

    counters: dict[str, Any] = {"llm_rounds": int(latest.get("total_rounds") or 0)}
    prefix = extension_name_prefix(SKILL_NAME)
    child = latest.get("child_drive_root")
    log_path = (Path(child) / "logs" / "tools.jsonl") if child else (
        data_dir / "state" / "headless_tasks" / ouro_task_id / "data" / "logs" / "tools.jsonl"
    )
    fallback = data_dir / "logs" / "tools.jsonl"
    screenshots = gui = remote_exec = total = 0
    src = log_path if log_path.is_file() else (fallback if fallback.is_file() else None)
    if src is not None:
        for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict) or row.get("type") != "tool_call":
                continue
            if src is fallback and str(row.get("task_id") or "") != ouro_task_id:
                continue
            tool = str(row.get("tool") or "")
            if not tool.startswith(prefix):
                continue
            short = tool[len(prefix):]
            total += 1
            if short == "screenshot":
                screenshots += 1
            elif short == "remote_exec":
                remote_exec += 1
            elif short in _GUI_ACTION_TOOLS:
                gui += 1
    counters.update({
        "screenshots": screenshots,
        "gui_action_calls": gui,
        "remote_exec_calls": remote_exec,
        "skill_tool_calls": total,
        "tools_log": str(src) if src is not None else "",
    })
    return counters

OSWORLD_PREAMBLE = (
    "You are operating a real Ubuntu desktop inside an OSWorld VM, by yourself, to "
    "completion. Drive the VM like a skilled human user: look at the screen, click "
    "menus/buttons, type into dialogs, use keyboard shortcuts, save/confirm, and verify.\n"
    "The 'unix_computer_use' skill is enabled with an active OSWorld HTTP backend; its tools act on THIS VM. Call "
    "list_available_tools (or enable_tools) to get the names (ext_<n>_r_unix_computer_use_"
    "screenshot, _click, _type_text, _key, _scroll, _left_click_drag, _move, _wait, "
    "_remote_exec) and enable them.\n"
    "\n"
    "FIRST, DO A FEASIBILITY CHECK: before executing a long plan, decide "
    "whether the task is possible on this VM with the installed apps, hardware, accounts, "
    "and allowed tools. Use at most 1-2 concise remote_exec probes if needed (e.g. hardware "
    "exists? app feature exists? required file exists?). If the requested result requires "
    "missing hardware, unavailable accounts/cloud/collaboration infrastructure, or a feature "
    "the installed application genuinely does not provide, do not keep trying — end your "
    "final message with only: TASK_INFEASIBLE.\n"
    "\n"
    "PRIMARY RULE — HUMAN GUI CONTROL:\n"
    "- For application tasks (Thunderbird, Chrome, LibreOffice, VS Code, GIMP, VLC, OS "
    "settings), solve through the visible application UI unless the task explicitly says "
    "\"command line\" or is obviously file/media batch processing.\n"
    "- Treat GUI actions as the official action surface: screenshot/view_image, click, "
    "type_text, key, scroll, drag. This should be MOST of your actions, like a human using "
    "the VM. Do not replace a GUI workflow with prefs.js edits, UNO/Basic macros, "
    "python-pptx, profile hacks, XML edits, or other behind-the-back mutations.\n"
    "- remote_exec is NOT your main problem-solving channel for app tasks. It is allowed only "
    "for a quick read-only check (for example, verify a saved file or exact setting) or "
    "for tasks whose wording explicitly asks for command-line work/conversion/media tools.\n"
    "\n"
    "VISION LOOP — do exactly this for GUI work:\n"
    "  1. screenshot (returns a 'path') -> immediately view_image(path) so you SEE the desktop.\n"
    "  2. Read coordinates off that viewed image, then act with click/key/type_text/scroll.\n"
    "  3. Take another screenshot+view_image only after a meaningful UI state change.\n"
    "view_image is your visual channel. (vlm_query, analyze_screenshot and browser tools are "
    "DISABLED — do not look for them.)\n"
    "\n"
    "BE FAST — every tool call costs ~30s, so MINIMIZE calls:\n"
    "- Do not spend more than 2 calls on investigation before taking a real GUI action.\n"
    "- Batch 2-4 confident GUI actions before the next screenshot+view_image. Do NOT screenshot "
    "after every single keystroke.\n"
    "- Prefer keyboard shortcuts when faster (menus via Alt, Ctrl+S to save, etc.).\n"
    "- If remote_exec is legitimately needed, use at most 1-2 concise read-only checks before the "
    "next GUI action. Do not repeatedly grep/probe internals. NEVER use remote_exec to see the "
    "screen, pixel-analyze screenshots, or run ImageGrab/scrot/numpy screen analysis.\n"
    "\n"
    "Anti-loop: if the same action fails twice, change approach (different menu path, "
    "keyboard), but stay in the GUI for app tasks; never fall back to pixel analysis or profile "
    "hacking.\n"
    "OSWorld evaluates the VM state, not your chat answer. Unless the task explicitly asks you "
    "to write an answer in a document/app, a textual answer in chat is not success: leave the "
    "requested browser tab, file, setting, app state, or saved artifact in the VM.\n"
    "BEFORE YOUR VERDICT, VERIFY THE FINAL ENVIRONMENT STATE: re-check that the VM state right now "
    "genuinely satisfies EVERY requirement of the task. Judge by the real, observed state — re-open and "
    "look at the relevant file/app/setting — not by your belief that you performed the steps. If any "
    "requirement is not fully met (including a change made but not saved/applied), keep working; declare "
    "done only when the observed state matches the task. If the task is genuinely impossible on this VM, "
    "end with TASK_INFEASIBLE.\n"
    "Be decisive and efficient. When the task is verifiably complete in the real app, stop. "
    "If genuinely infeasible, end your final message with only: TASK_INFEASIBLE\n\nTask:\n"
)

_COMPUTER_USE_SHORT_TOOLS = (
    "list_connections", "test_connection", "screenshot", "click", "move",
    "left_click_drag", "mouse_down", "mouse_up", "type_text", "key", "hold_key",
    "scroll", "wait", "window_list", "ax_tree", "cursor_position", "remote_exec",
)


def _ensure_vmrun_on_path() -> None:
    parts = os.environ.get("PATH", "").split(os.pathsep)
    changed = False
    for cand in VMWARE_FUSION_PATHS:
        if Path(cand, "vmrun").exists() and cand not in parts:
            parts.insert(0, cand)
            changed = True
    if changed:
        os.environ["PATH"] = os.pathsep.join(parts)


def _api(server: str, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(server.rstrip("/") + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip().startswith(("{", "[")) else {"raw": raw}


def _text_declares_infeasible(value: Any) -> bool:
    return isinstance(value, str) and any(
        line.strip() == "TASK_INFEASIBLE" for line in value.splitlines()
    )


def _final_answer_declares_infeasible(latest: dict[str, Any]) -> bool:
    """True iff the agent's FINAL ANSWER is a standalone TASK_INFEASIBLE line.

    OSWorld's infeasible evaluators check the official action history for FAIL; a
    chat marker alone is not enough, so the bridge translates this into an
    official ``env.step("FAIL")`` before evaluate(). Inspect ONLY the terminal
    answer fields of the task result (``final_answer``, ``result``) — never the
    whole result tree, or a marker quoted in intermediate reasoning/tool output
    would spuriously flip a feasible task to a FAIL (reward 0) or fake an
    infeasible pass.
    """
    if not isinstance(latest, dict):
        return False
    return _text_declares_infeasible(latest.get("final_answer")) or _text_declares_infeasible(latest.get("result"))


def _enable_skill(repo_dir: Path, data_dir: Path) -> str:
    """Controlled-seed + native-trust + enable unix_computer_use.

    Launcher auto-seeding won't pick up a brand-new bundled skill on an already
    bootstrapped data dir, and an existing native seed may be stale for this
    worktree. Re-copy the repo skill into THIS isolated bench data dir and stamp
    native trust against the current hash. Idempotent: re-copies each run so repo
    edits are reflected. The ``net`` permission needs no owner grant, but it does
    remove the skill from the launcher's native auto-enable class — this runner
    therefore enables it explicitly via ``save_enabled``.
    """
    import logging
    import shutil
    from ouroboros.launcher_bootstrap import _stamp_native_seed_trust
    from ouroboros.skill_loader import find_skill, save_enabled

    src = repo_dir / "skills" / SKILL_NAME
    if not src.is_dir():
        raise RuntimeError(f"{SKILL_NAME} not found in repo skills: {src}")
    dest = data_dir / "skills" / "native" / SKILL_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    (dest / ".seed-origin").write_text("seeded_from=bench_cu_bridge\n", encoding="utf-8")
    shutil.rmtree(dest / "__pycache__", ignore_errors=True)
    _stamp_native_seed_trust(data_dir, dest, logging.getLogger("osworld_bridge"))
    skill = find_skill(data_dir, SKILL_NAME)
    if skill is None or getattr(skill, "load_error", None):
        raise RuntimeError(f"{SKILL_NAME} unavailable after seed: {getattr(skill, 'load_error', None)}")
    save_enabled(data_dir, SKILL_NAME, True)
    review = getattr(getattr(skill, "review", None), "status", "?")
    return f"{skill.name} ({skill.source}) review={review} enabled=True"


def _publish_target(data_dir: Path, target: str) -> Path:
    """Activate an osworld_http connection in unix_computer_use skill state.

    The skill worker may not inherit the server's custom env, so the robust
    channel is shared skill state: <data>/state/skills/unix_computer_use/connections.json.
    Registry first, active pointer last (both atomic) so a lost second write still
    names a connection that exists in the registry.

    Atomicity comes from the runtime's own SSOT writers (``write_text_atomic`` for the
    text pointers, ``atomic_write_json`` for the JSON registry) — the same helpers the
    skill itself uses, instead of a launcher-local write+rename copy. Note both REPLACE
    a symlink at the destination with a regular file rather than writing through it;
    that is the confinement-preserving behaviour we want for skill state.
    """
    from ouroboros.skill_loader import skill_state_dir
    from ouroboros.utils import atomic_write_json, write_text_atomic

    sdir = Path(skill_state_dir(data_dir, SKILL_NAME))
    sdir.mkdir(parents=True, exist_ok=True)
    target_path = sdir / "osworld_target.txt"
    write_text_atomic(target_path, target)
    registry = {
        "active": "osworld-current",
        "connections": {
            "local": {"backend": "local", "enabled": True},
            "osworld-current": {"backend": "osworld_http", "target_file": str(target_path), "enabled": True},
        },
    }
    atomic_write_json(sdir / "connections.json", registry, trailing_newline=True)
    write_text_atomic(sdir / "active_connection.txt", "osworld-current")
    return target_path


def main() -> int:
    # NOTHING but argument parsing and pure local derivation until `admit_benchmark_run`
    # below: `_ensure_vmrun_on_path()` probes the filesystem for `vmrun` and mutates $PATH, and
    # the `sys.path` insert is process state, so both moved into `_run_cu_bridge`.
    p = argparse.ArgumentParser(description="OSWorld via host-side Ouroboros computer-use bridge (one run per task).")
    p.add_argument("--osworld-root", default=os.environ.get("OSWORLD_ROOT", str(_WORKSPACE_ROOT / "OSWorld")))
    p.add_argument("--provider_name", default="vmware")
    p.add_argument("--path_to_vm", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--result_dir", default="results/osworld_cu_bridge")
    p.add_argument("--repo-dir", default=str(_REPO_ROOT))
    p.add_argument("--data-dir", required=True, help="bench server data dir (skill enablement target)")
    p.add_argument("--settings-path", default="",
                   help="settings.json the bench server was started with (for the max_rounds disclosure); "
                        "defaults to <data-dir>/settings.json — NOT the live workspace settings")
    p.add_argument("--ouroboros-url", default="http://127.0.0.1:8780")
    p.add_argument("--target-file", required=True, help="informational copy of the VM HTTP target URL the runner writes (also recorded in bridge.json); the published osworld_http connection reads a SEPARATE state-confined copy under the skill state dir, since target_file reads are confined there")
    # NOTE: the solve model is set by the Ouroboros server's settings (OUROBOROS_MODEL);
    # this runner does not accept a --model flag so provenance can't be misreported.
    p.add_argument("--task_timeout_sec", type=int, default=3600)
    p.add_argument("--startup_timeout_sec", type=int, default=900)
    p.add_argument("--reset_retries", type=int, default=3)
    p.add_argument("--wait_after_reset_sec", type=float, default=12.0)
    p.add_argument("--show-vm", action="store_true")
    p.add_argument("--allow-a11y", action="store_true",
                   help="expose the ax_tree (accessibility) tool; the run is then NOT screenshot-only "
                        "(disclose 'Additional a11y tree used: Yes'). Off by default.")
    p.add_argument("--allow-live-server", action="store_true",
                   help="permit pointing --ouroboros-url at the live desktop server port 8765 (debug only).")
    p.add_argument("--allow-dirty-seed", action="store_true",
                   help="run even when this Ouroboros checkout is dirty or its git identity is "
                        "unreadable (default: fail closed before the VM boots). Recorded in the "
                        "manifest; a dirty seed makes the run's provenance irreproducible.")
    p.add_argument("--claim-dir", default="",
                   help="shared claim directory for overlapping runs (append-only resumes, retry "
                        "passes, or concurrent runners). Two attempts can then never take the same "
                        "task: the holder keeps an O_EXCL lock for the duration and a scored attempt "
                        "leaves a permanent marker (first SCORED attempt wins).")
    p.add_argument("--claim-margin-sec", type=float, default=900.0,
                   help="extra slack on top of task+startup timeouts before another lane may treat a "
                        "claim lock as stale (default 900).")
    args = p.parse_args()

    # Guards: never drive the live desktop server or publish a bench connection
    # into the owner's live skill state (mirrors run_step_agent.py).
    from devtools.benchmarks.osworld.run_step_agent import (
        _is_default_desktop_server,
        confined_claims_dir,
        scored_claim_state,
        task_claim_key,
    )
    if _is_default_desktop_server(args.ouroboros_url) and not args.allow_live_server:
        raise SystemExit(
            f"refusing the live desktop server URL {args.ouroboros_url}; point at an isolated "
            "bench server (fresh OUROBOROS_DATA_DIR, non-default port) or pass --allow-live-server"
        )
    _refuse_live_data_dir(Path(args.data_dir))

    # PURE derivation only until `admit_benchmark_run` below: no checkout probe, no task-file
    # read, no mkdir, no write. Everything that used to run here (the OSWorld checkout git
    # probe, the run-directory creation and the `task.json` copy) now happens after admission,
    # so a refusal cannot precede the durable record of what was refused.
    repo_dir = Path(args.repo_dir).expanduser().resolve(strict=False)
    # The claim dir is where the lock and the scored markers are CREATED, so it goes through
    # the same repo//live-data boundary as every other benchmark output root — in its PURE
    # form, and FIRST, so a refused path leaves nothing behind at all (not even the results
    # root that `ensure_outside_repo` would create below). `--data-dir` and `--result_dir`
    # were already confined; this one was not. The authority is the EXECUTION checkout
    # (`--repo-dir`), which is why it is resolved just above: confining against this launcher's
    # own location let a claim dir be written straight into the checkout under test.
    if args.claim_dir:
        try:
            claims_dir: Path | None = confined_claims_dir(Path(args.claim_dir), repo_dir=repo_dir)
        except ValueError as exc:
            raise SystemExit(f"refusing --claim-dir: {exc}") from exc
    else:
        claims_dir = None

    osworld_root = Path(args.osworld_root).expanduser().resolve(strict=False)
    task_path = Path(args.task).expanduser()
    if not task_path.is_absolute():
        task_path = osworld_root / task_path
    domain = task_path.parent.name
    example_id = task_path.stem
    data_dir = Path(args.data_dir).expanduser().resolve(strict=False)
    # Default the settings path INTO the isolated bench data dir, not the live
    # workspace settings, so the max_rounds disclosure reflects THIS server.
    settings_path = Path(args.settings_path).expanduser().resolve(strict=False) if args.settings_path else (data_dir / "settings.json")
    result_root = Path(args.result_dir).expanduser()
    if not result_root.is_absolute():
        result_root = osworld_root / result_root
    # ASSERT, not ensure: creating the results root here would put a directory on disk
    # before the run manifest is persisted — a refused run must leave NO footprint. The
    # atomic manifest write creates the tree.
    result_root = assert_outside_repo(result_root, repo_dir)
    run_dir = result_root / domain / example_id
    # EVERY ADMITTED ATTEMPT GETS ITS OWN ADMISSION/FINALIZATION RECORD. `run_dir` is shared between
    # attempts by construction (it is keyed by the task, not by the runner), so writing the
    # admission manifest to the canonical `run_dir/task_run_manifest.json` — which is what this
    # launcher did — let two overlapping lanes overwrite each other's record before either had
    # claimed the task, and let the LOSER later finalize `skipped_in_flight` on top of the
    # holder's still-running record. Now the shared canonical artefacts are written only by the
    # attempt that OWNS the task (see `run.owns_task`), and the per-attempt record under
    # `attempts/<id>/` is append-only evidence that no other attempt can touch.
    # `timestamp_run_id` already carries the pid+counter suffix that makes two attempts started
    # in the same second distinct.
    attempt_dir = run_dir / "attempts" / timestamp_run_id("attempt")
    manifest_path = attempt_dir / "task_run_manifest.json"

    claim_key = task_claim_key(domain, example_id)
    # "First SCORED attempt wins" is answered with a READ before admission, because `run_dir`
    # is SHARED between attempts: one that arrives at an already-scored task must leave no
    # footprint at all, and an admission write would clobber the winner's own record. Either
    # scored state counts, and neither depends on the lock, so neither expires.
    claim_state = scored_claim_state(claims_dir, claim_key)
    if claim_state:
        print(json.dumps({"claim": claim_state, "task_id": example_id, "domain": domain,
                          "claim_dir": str(claims_dir), "skipped": True}, ensure_ascii=False))
        return 4

    run = CuBridgeRun(run_dir=run_dir, attempt_dir=attempt_dir, result_root=result_root,
                      domain=domain, example_id=example_id, base_manifest={},
                      # With no claim dir there is no multi-lane contract to honour and no
                      # second attempt to protect against: the operator has asserted
                      # exclusivity, so the canonical artefacts are this run's, as before.
                      owns_task=claims_dir is None)
    # ONE exit path for the canonical mirror, wrapping EVERY terminal path below — admitted,
    # refused, or crashed. It has to run AFTER a finalization seam's context manager has
    # EXITED, because that exit is when the terminal `outcome`/`exit_code`/`refusal` are merged
    # into `run.base_manifest`: a mirror taken from INSIDE a `with` block copies the pre-merge
    # payload (for a refusal, the admission seam's generic one, which says exit_code 1) and
    # leaves the shared canonical record claiming a status the process never exited with —
    # the "recorded != real" class this release closes. The refusal branch used to mirror only
    # from inside its seam and `return` past this `finally`; both terminal paths now share it,
    # so the next refusal branch added here cannot forget it. Owner-only: a lane that never
    # held the claim must not overwrite the holder's canonical manifest.
    try:
        # ADMISSION: the manifest is built ONCE here, WRITTEN, and only then does the clean-seed
        # gate enforce (that is where `require_clean` lives) — before the VM boots and before the
        # first paid POST. The same dict is amended by every outcome and finalized on every exit.
        try:
            run.base_manifest = admit_benchmark_run(
                manifest_path,
                benchmark="osworld", run_root=result_root, repo_dir=repo_dir,
                requested_task_ids=[example_id], dataset="OSWorld", settings_path=settings_path,
                require_clean=not args.allow_dirty_seed,
                harness={
                    # HONEST contract: reset()/evaluate() are official, but GUI actions
                    # go to the guest /execute channel and are NOT recorded in
                    # DesktopEnv.action_history/traj.jsonl (only a translated FAIL is).
                    "adapter": "host_cu_bridge", "one_run_per_task": True,
                    "official_actions": False, "official_reset_evaluate": True,
                    "action_channel": "guest_execute_not_env_step",
                    "a11y_enabled": bool(args.allow_a11y),
                },
                extra={"allow_dirty_seed": bool(args.allow_dirty_seed),
                       "claim_dir": str(claims_dir) if claims_dir is not None else ""},
            )
        except BenchmarkAdmissionRefused as exc:
            run.base_manifest = exc.manifest
            # exit_code 2 is the status this launcher really exits with for a blocked run; the
            # seam's generic refusal payload says 1 and a record that disagrees with reality is
            # exactly what the exit-status parity test exists to catch.
            with finalize_run_manifest(manifest_path, run.base_manifest,
                                       outcome="refused", exit_code=2) as final:
                final["refusal"] = {**((run.base_manifest.get("extra") or {}).get("refusal") or {}),
                                    "exit_code": 2}
                _write_cu_outcome(run, None, "blocked", "seed_gate_failed",
                                  f"{type(exc).__name__}: {exc}",
                                  extra={"allow_dirty_seed": bool(args.allow_dirty_seed)})
            return 2

        with finalize_run_manifest(manifest_path, run.base_manifest) as final:
            return _run_cu_bridge(args, final, run, CuBridgePaths(
                osworld_root=osworld_root, task_path=task_path, repo_dir=repo_dir,
                data_dir=data_dir, settings_path=settings_path,
                claims_dir=claims_dir, claim_key=claim_key,
            ))
    finally:
        _mirror_canonical_manifest(run)


def _mirror_canonical_manifest(run: CuBridgeRun) -> None:
    """Copy the attempt's manifest to the shared canonical path, IF this attempt owns the task."""
    if not run.owns_task or not run.base_manifest:
        return
    write_json(run.run_dir / "task_run_manifest.json", run.base_manifest)


@dataclass
class CuBridgeRun:
    """The per-task record surface: where outcomes go and the ADMITTED manifest they amend."""

    run_dir: Path
    attempt_dir: Path
    result_root: Path
    domain: str
    example_id: str
    base_manifest: dict[str, Any]
    # True once THIS attempt holds the task claim (or when no claim dir is configured). Only an
    # owner writes the artefacts under `run_dir` that are shared between attempts.
    owns_task: bool = False


@dataclass
class CuBridgePaths:
    """Resolved inputs handed to the post-admission body."""

    osworld_root: Path
    task_path: Path
    repo_dir: Path
    data_dir: Path
    settings_path: Path
    claims_dir: Path | None
    claim_key: str


def _write_cu_outcome(run: CuBridgeRun, reward: float | None, status: str, reason: str,
                      error: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write the task outcome, amend the ADMITTED manifest IN PLACE, append the ledger row.

    In place, because `finalize_run_manifest` writes the SAME retained dict when the run ends:
    amending a copy would have the final write silently drop these facts.

    The attempt's own records are ALWAYS written; the shared canonical OUTCOME under `run_dir`
    only when this attempt owns the task. Two overlapping lanes therefore produce two independent
    records and exactly one canonical one, instead of silently overwriting each other's. The
    canonical MANIFEST is not written here at all — see the note at that write below.

    EVERY DESTINATION IS ATTEMPTED INDEPENDENTLY AND NOTHING HERE RAISES. This used to be a
    straight-line sequence, so the FIRST dead destination aborted the rest and the exception
    escaped into the broad handler in `_run_cu_bridge`, which republished by calling THIS SAME
    aggregate writer — reproducing the identical failure and leaving the run with no canonical
    outcome and/or no ledger row at all, while the durable `.scored` marker forbids any retry.
    An obtained score must reach every record that is still writable, so a failure is collected
    and DISCLOSED (`publication_errors`, and a best-effort rewrite of the sidecars that carry
    it) instead of cancelling the destinations that would have succeeded.
    """
    outcome = {
        "ok": status == "completed",
        "task_id": run.example_id, "domain": run.domain, "reward": reward,
        "status": status, "reason_code": reason, "error": error,
        "result_dir": str(run.run_dir), "attempt_dir": str(run.attempt_dir),
        "claim_owner": bool(run.owns_task), **(extra or {}),
    }
    publication_errors: list[str] = []

    def _publish(destination: str, write) -> None:
        try:
            write()
        except Exception as exc:  # noqa: BLE001 - one dead destination must not silence the rest
            publication_errors.append(f"{destination}: {type(exc).__name__}: {exc}")
            print(f"[bridge] publication FAILED at {destination}: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)

    def _amend_manifest() -> None:
        from devtools.benchmarks.osworld.run_step_agent import amend_task_manifest
        run.base_manifest.update(amend_task_manifest(
            run.base_manifest,
            output_paths={"task_outcome": str(run.attempt_dir / "task_outcome.json"),
                          "attempt_dir": str(run.attempt_dir)},
            extra={"attempt_dir": str(run.attempt_dir), "claim_owner": bool(run.owns_task),
                   **(extra or {})},
        ))

    _publish("attempt_outcome",
             lambda: write_json(run.attempt_dir / "task_outcome.json", outcome))
    _publish("manifest_amend", _amend_manifest)
    # Neither manifest is published here — not the canonical copy (see below) and not the
    # attempt's own, which is the very path the ACTIVE `finalize_run_manifest` finalizes: it
    # merges the terminal outcome/exit_code/refusal into this retained dict only on context
    # exit, so writing it now publishes a pre-merge record and the seam overwrites it a moment
    # later anyway. Enforced for the whole family by launcher_audit Invariant C.
    if run.owns_task:
        # The canonical MANIFEST is deliberately NOT written here. This function runs INSIDE an
        # active `finalize_run_manifest`, so `run.base_manifest` does not yet carry the terminal
        # `outcome`/`exit_code`/`refusal` that the seam merges on context exit — publishing it
        # now would put a pre-merge record (for a refusal, the admission seam's generic one
        # saying exit_code 1) at the shared path that a CONCURRENT LANE reads, and an
        # interruption before the seam exits would leave that wrong record durably. `main()`
        # mirrors exactly once, from its outer `finally`, after the seam has exited.
        # The OUTCOME sidecar has no such window: it is complete when it is built.
        _publish("canonical_outcome",
                 lambda: write_json(run.run_dir / "task_outcome.json", outcome))
    # The ledger is APPEND-ONLY shared evidence of OUTCOMES, not of attempts: a row is written
    # exactly here, so an attempt that steps aside on a held or already-scored claim (exit 4,
    # no outcome) contributes none, and only its `attempts/<id>/` record shows it was tried.
    # The row says which attempt it came from and whether that attempt held the claim (a
    # pre-claim block is not the owner), so a reader deduping by instance_id can tell the
    # holder's row from a bystander's.
    _publish("result_index", lambda: append_result_index(run.result_root, task_result_row(
        benchmark="osworld", instance_id=run.example_id, status=status, reason_code=reason,
        official_eval_status="completed" if reward is not None else "not_run",
        output_paths={"task_outcome": str(run.attempt_dir / "task_outcome.json")},
        error=error, details={"domain": run.domain, "reward": reward,
                              "attempt_dir": str(run.attempt_dir),
                              "claim_owner": bool(run.owns_task), **(extra or {})},
    )))
    if publication_errors:
        # The sidecars were written BEFORE the later stages failed, so they carry the score but
        # not yet the fact that publication was partial. Amend them so the durable record
        # discloses its own gap; a destination that is dead stays dead, silently, because this
        # pass exists only to add disclosure to records that already exist.
        outcome["publication_errors"] = list(publication_errors)
        for path in ([run.attempt_dir / "task_outcome.json"]
                     + ([run.run_dir / "task_outcome.json"] if run.owns_task else [])):
            try:
                write_json(path, outcome)
            except Exception:  # noqa: BLE001 - disclosure is best-effort by construction
                pass
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return outcome


def _run_cu_bridge(args: argparse.Namespace, final: dict[str, Any], run: CuBridgeRun,
                   paths: CuBridgePaths) -> int:
    """Everything AFTER admission: attestation, lane claim, VM boot, the agent run, evaluate.

    Split out of `main()` so the statements preceding `admit_benchmark_run()` stay trivially
    auditable — the seam meta-test walks them with `ast` and denies every filesystem, docker,
    subprocess and network call there.
    """
    from devtools.benchmarks.osworld.run_step_agent import (
        ClaimMarkerNotDurable,
        acquire_task_claim,
        claim_stale_sec,
        construct_desktop_env,
        mark_task_scored,
        osworld_checkout_info,
        record_unconfirmed_score,
        release_task_claim,
    )
    osworld_root, task_path = paths.osworld_root, paths.task_path
    repo_dir, data_dir, settings_path = paths.repo_dir, paths.data_dir, paths.settings_path
    claims_dir, claim_key = paths.claims_dir, paths.claim_key
    run_dir = run.run_dir
    # Process/environment preparation, after the persisted admission boundary (see main()).
    _ensure_vmrun_on_path()
    sys.path.insert(0, str(osworld_root))

    # Late facts the admission manifest could not carry: they need a git probe and a file read,
    # both of which are forbidden before the run is on disk.
    checkout = osworld_checkout_info(osworld_root)
    run.base_manifest["dataset"] = _dataset_name(str(checkout.get("variant") or "unknown"))
    run.base_manifest["harness"] = {
        **(run.base_manifest.get("harness") or {}),
        "osworld_checkout": checkout,
        "max_rounds_effective": _effective_max_rounds(settings_path),
    }

    example = json.loads(task_path.read_text(encoding="utf-8"))
    run.example_id = str(example.get("id") or run.example_id)
    run.base_manifest["requested_task_ids"] = [run.example_id]
    instruction = str(example["instruction"])
    # `task.json` is a CANONICAL artefact in the shared `run_dir`, so it is written once the
    # claim is held (below), not here: a lane that steps aside must not have touched the
    # holder's directory on its way past.

    def _write_outcome(reward: float | None, status: str, reason: str, error: str = "",
                       extra: dict[str, Any] | None = None) -> dict[str, Any]:
        return _write_cu_outcome(run, reward, status, reason, error, extra)

    example_id = run.example_id
    domain = run.domain

    # Owner Q9=A+B / Q10: attest the RUNNING server (its HTTP `runtime_version`) against the
    # checkout it was started from (local HEAD + VERSION) before any paid work. The shared
    # helper fails CLOSED by raising, so a typed `blocked` row keeps the denominator honest
    # instead of a bare traceback. Deliberately BEFORE the claim: a config-wide skew must not
    # park a lock that nobody will clear.
    #
    # A refusal CARRIES the record it built (`RuntimeAttestationRefused.attestation`), so the
    # durable manifest keeps the EXACT typed reason plus `runtime_version`, `repo_head` and
    # `repo_version` rather than the string `runtime_attestation_failed` — the identities this
    # provenance contract exists to preserve, discarded at the moment they matter most.
    try:
        run.base_manifest["extra"] = {
            **(run.base_manifest.get("extra") or {}),
            "runtime_attestation": runtime_attestation(args.ouroboros_url, repo_dir),
        }
    except RuntimeAttestationRefused as exc:
        reason = str(exc.attestation.get("reason") or "") or "runtime_attestation_failed"
        run.base_manifest["extra"] = {
            **(run.base_manifest.get("extra") or {}),
            "runtime_attestation": dict(exc.attestation),
        }
        final.update({"outcome": "blocked", "exit_code": 2,
                      "refusal": {"stage": "runtime_attestation",
                                  "reason": reason, "exit_code": 2}})
        _write_outcome(None, "blocked", reason, f"{type(exc).__name__}: {exc}",
                       extra={"runtime_attestation": dict(exc.attestation)})
        return 2
    except RuntimeError as exc:
        # No record to keep (raised before one was built).
        final.update({"outcome": "blocked", "exit_code": 2,
                      "refusal": {"stage": "runtime_attestation",
                                  "reason": "runtime_attestation_failed", "exit_code": 2}})
        _write_outcome(None, "blocked", "runtime_attestation_failed", f"{type(exc).__name__}: {exc}")
        return 2

    # Multi-lane claim: take the task exclusively or step aside. Deliberately BEFORE
    # the skill seed and the VM boot, and deliberately WITHOUT writing a ledger row —
    # the lane that owns the task owns its denominator row too.
    claim_fd: int | None = None
    claim_scored = False
    # Set when the official score exists but its permanent marker does NOT: the lock must then
    # be RETAINED rather than released (see the ClaimMarkerNotDurable handler below).
    claim_release_forbidden = False
    env = None
    reward: float | None = None
    # The claim is taken INSIDE the try/finally that releases it. Acquiring it earlier left
    # the lock on disk whenever anything between claim and VM boot raised (an unimportable
    # `desktop_env` being the realistic case): no `.scored` marker, so the task was neither
    # scored nor claimable for the whole staleness window — the opposite of this mechanism's
    # own "an unscored attempt stays claimable" contract.
    try:
        if claims_dir is not None:
            claim_fd, claim_reason = acquire_task_claim(
                claims_dir, claim_key,
                stale_sec=claim_stale_sec(args.task_timeout_sec, args.startup_timeout_sec,
                                          args.claim_margin_sec),
                repo_dir=repo_dir,
                metadata=f"pid={os.getpid()} task={claim_key} result_dir={run_dir}\n",
            )
            if claim_fd is None:
                # The loser finalizes into its OWN attempt record. Writing `skipped_in_flight`
                # into the shared canonical manifest — which is what the shared manifest path
                # used to guarantee — overwrote the holder's still-running record with the
                # bystander's terminal outcome.
                final.update({"outcome": f"skipped_{claim_reason}", "exit_code": 4})
                print(json.dumps({"claim": claim_reason, "task_id": example_id, "domain": domain,
                                  "claim_dir": str(claims_dir), "skipped": True}, ensure_ascii=False))
                return 4
            run.owns_task = True

        # This attempt owns the task, so the shared canonical artefacts are now ours to write.
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "task.json").write_text(json.dumps(example, ensure_ascii=False, indent=2),
                                           encoding="utf-8")

        # Enable the computer-use skill in the server's data dir.
        try:
            enabled = _enable_skill(repo_dir, data_dir)
        except Exception as exc:  # noqa: BLE001
            final.update({"outcome": "blocked", "exit_code": 2,
                          "refusal": {"stage": "skill_enable", "reason": "skill_enable_failed",
                                      "exit_code": 2}})
            _write_outcome(None, "blocked", "skill_enable_failed", f"{type(exc).__name__}: {exc}")
            # Unscored: the finally below releases the claim, so a retry lane may take it.
            return 2

        # Wire OSWorld's proxy pool (e.g. DataImpulse residential) for tasks flagged
        # "proxy": true. Only enable when a proxy config file actually exists, else
        # OSWorld raises "No proxy available" and hard-fails those tasks. Non-proxy
        # tasks are unaffected (OSWorld gates on task_config["proxy"] AND enable_proxy).
        # PROXY_CONFIG_FILE must be set BEFORE importing desktop_env: setup.py loads
        # the pool at import time.
        _proxy_cfg = os.environ.get("PROXY_CONFIG_FILE") or str(
            osworld_root / "evaluation_examples" / "settings" / "proxy" / "dataimpulse.json"
        )
        _enable_proxy = os.path.exists(_proxy_cfg)
        if _enable_proxy:
            os.environ["PROXY_CONFIG_FILE"] = os.path.abspath(_proxy_cfg)
        print(f"[bridge] enable_proxy={_enable_proxy} "
              f"proxy_cfg={os.environ['PROXY_CONFIG_FILE'] if _enable_proxy else '(none)'}", flush=True)

        from desktop_env.desktop_env import DesktopEnv

        # The constructor boots the VM/container, so a transient boot failure must be
        # retried like the reset loop below instead of burning the task; the teardown of
        # each failed attempt is a precaution against the half-built object being
        # discarded with an emulator still running (see construct_desktop_env).
        env = construct_desktop_env(
            DesktopEnv,
            attempts=max(1, int(args.reset_retries)),
            deadline=time.time() + max(1, int(args.startup_timeout_sec)),
            retry_sleep_sec=5.0,
            provider_name=args.provider_name, path_to_vm=args.path_to_vm,
            action_space="pyautogui", screen_size=(1920, 1080),
            headless=not args.show_vm, os_type="Ubuntu", require_a11y_tree=False,
            enable_proxy=_enable_proxy,
        )
        # Reset with retries to a usable screenshot. Its own fresh startup window
        # (mirrors run_step_agent._initial_observation_with_retries): a slow VM boot
        # must not eat the reset budget, which is what sharing one deadline would do.
        deadline = time.time() + max(1, int(args.startup_timeout_sec))
        last_err = ""
        ok = False
        for attempt in range(1, max(1, int(args.reset_retries)) + 1):
            if time.time() >= deadline:
                break
            try:
                env.reset(task_config=example)
                if args.wait_after_reset_sec > 0:
                    time.sleep(args.wait_after_reset_sec)
                obs = env._get_obs()
                if isinstance(obs, dict) and isinstance(obs.get("screenshot"), (bytes, bytearray)) and obs["screenshot"]:
                    ok = True
                    break
                last_err = f"attempt {attempt}: no screenshot"
            except Exception as exc:  # noqa: BLE001
                last_err = f"attempt {attempt}: {type(exc).__name__}: {exc}"
            time.sleep(5)
        if not ok:
            raise RuntimeError(f"OSWorld startup failed: {last_err}")

        target = f"http://{env.vm_ip}:{env.server_port}"
        Path(args.target_file).expanduser().write_text(target, encoding="utf-8")
        state_target = _publish_target(data_dir, target)
        (run_dir / "bridge.json").write_text(json.dumps({
            "target": target, "skill": enabled, "target_file": args.target_file,
            "state_target_file": str(state_target),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        prompt = OSWORLD_PREAMBLE + instruction + (
            "\n\nunix_computer_use tools (enable then use; discover exact ext_<n>_ names via "
            "list_available_tools): " + ", ".join(_COMPUTER_USE_SHORT_TOOLS) + ". They act on THIS VM "
            "because the runner activated the osworld-current connection."
            f"\n\nVM CREDENTIALS: the desktop user is 'user' and its sudo password is "
            f"'{env.client_password}'. When a task GENUINELY needs root (create system users, "
            f"start/enable a service, install packages) or a GUI dialog prompts for a password, "
            f"use it — e.g. run privileged commands as: echo '{env.client_password}' | sudo -S <cmd>. "
            f"Still prefer the visible GUI for application tasks per the rules above; sudo is for "
            f"the OS/CLI steps that truly require root."
        )
        (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

        created = _api(args.ouroboros_url, "POST", "/api/tasks", {
            "description": prompt, "memory_mode": "empty",
            "disabled_tools": _effective_disabled_tools(args.allow_a11y),
        })
        task_id = str(created.get("task_id") or "")
        if not task_id:
            raise RuntimeError(f"task creation returned no task_id: {created!r}")
        (run_dir / "ouroboros_task_id.txt").write_text(task_id, encoding="utf-8")

        final_statuses = {"completed", "failed", "cancelled", "rejected_duplicate"}
        t_deadline = time.time() + max(60, int(args.task_timeout_sec))
        latest: dict[str, Any] = {}
        while True:
            if time.time() >= t_deadline:
                try:
                    _api(args.ouroboros_url, "POST", f"/api/tasks/{task_id}/cancel", {})
                except Exception:
                    pass
                latest = {"status": "timeout"}
                break
            try:
                result = _api(args.ouroboros_url, "GET", "/api/tasks/" + task_id, timeout=30)
            except Exception:
                time.sleep(5)
                continue
            latest = result if isinstance(result, dict) else {}
            if str(latest.get("status") or "") in final_statuses:
                break
            time.sleep(8)
        (run_dir / "ouroboros_task_final.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")

        infeasible_declared = _final_answer_declares_infeasible(latest)
        fail_info: dict[str, Any] = {}
        if infeasible_declared:
            try:
                _obs_after_fail, _reward_after_fail, _done_after_fail, fail_info = env.step("FAIL")
            except Exception as exc:  # noqa: BLE001 - keep denominator-preserving evaluation
                fail_info = {"error": f"{type(exc).__name__}: {exc}"}
            (run_dir / "osworld_fail_action.json").write_text(
                json.dumps({"declared": True, "info": fail_info}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        try:
            budget_counters: dict[str, Any] = _collect_budget_counters(data_dir, latest, task_id)
        except Exception as exc:  # noqa: BLE001 - counters are disclosure-only, never fail the run
            budget_counters = {"budget_counters_error": f"{type(exc).__name__}: {exc}"}

        reward = float(env.evaluate())
        # FAIL-CLOSED DURABLE CLAIM TRANSITION, before the score is projected ANYWHERE.
        # Deferring the marker to the `finally` below meant a disk error or a process death
        # after the official score was written left no marker at all, and another lane reran a
        # task that already had one — the pre-registered "first scored attempt wins" rule
        # violated in the direction that corrupts results. `mark_task_scored` raises
        # `ClaimMarkerNotDurable` rather than swallowing the failure.
        if claims_dir is not None and claim_fd is not None:
            try:
                mark_task_scored(claims_dir, claim_key, repo_dir=repo_dir,
                                 payload={"reward": reward, "result_dir": str(run_dir),
                                          "domain": domain, "task_id": example_id})
            except ClaimMarkerNotDurable as exc:
                # Caught HERE and not by the broad `except Exception` below, because that
                # handler falls through to the `finally`, which would release the lock with
                # `scored=False` and hand an ALREADY-EVALUATED task straight back to the next
                # attempt — the precise corruption the fail-closed marker exists to prevent.
                #
                # The DURABLE part of the protection is `exc.unconfirmed_marker`, not the
                # retained lock: `stale_sec` makes that lock reclaimable by design, so a
                # lock-only protection fails open once somebody waits long enough. The lock is
                # still retained (it costs nothing and covers the interim), but the permanent
                # refusal comes from the marker.
                claim_release_forbidden = True
                print(f"[bridge] {exc}", file=sys.stderr, flush=True)
                if exc.unconfirmed_marker is None:
                    # NOTHING on disk records that this task was scored, and the lock expires.
                    # There is no honest protection left to promise: refuse loudly, with a
                    # distinct status, and tell the operator the claim dir itself is unusable.
                    print("[bridge] FATAL: the claim directory is unusable — this task HAS an "
                          "official score that nothing on disk records, and the in-flight lock "
                          "will expire. Stop, fix the claim dir, and do not run further tasks "
                          "against it.", file=sys.stderr, flush=True)
                    final.update({"outcome": "claim_state_unrecoverable", "exit_code": 3,
                                  "refusal": {"stage": "scored_claim_marker",
                                              "reason": "claim_state_unrecoverable",
                                              "exit_code": 3}})
                    _write_outcome(reward, "adapter_error", "claim_state_unrecoverable",
                                   f"{type(exc).__name__}: {exc}",
                                   extra={"claim_marker_not_durable": True,
                                          "claim_state_unrecoverable": True,
                                          "claim_lock_retained": True})
                    return 3
                final.update({"outcome": "scored_claim_marker_failed", "exit_code": 2,
                              "refusal": {"stage": "scored_claim_marker",
                                          "reason": "claim_marker_not_durable", "exit_code": 2}})
                # The official score is REPORTED (it exists; dropping it would corrupt the
                # denominator in the other direction) with the bookkeeping failure disclosed.
                _write_outcome(reward, "adapter_error", "claim_marker_not_durable",
                               f"{type(exc).__name__}: {exc}",
                               extra={"claim_marker_not_durable": True,
                                      "claim_lock_retained": True,
                                      "claim_unconfirmed_marker": str(exc.unconfirmed_marker)})
                return 2
            except BaseException as exc:
                # `KeyboardInterrupt` and `SystemExit` derive from BaseException, NOT Exception
                # — the same trap that made a refusal handler inert in phase P1. Without this
                # arm a Ctrl-C landing inside `mark_task_scored` unwinds straight through the
                # `finally`, which releases the claim with `scored=False`.
                #
                # RETAINING THE LOCK IS NOT ENOUGH, and this arm used to do only that. The lock
                # is EXPIRABLE by design (`stale_sec` reclaims a crashed holder's task), so an
                # interrupt landing after `env.evaluate()` but before either `.scored` marker
                # was durable left a protection with a countdown on it: once `stale_sec` passed,
                # `acquire_task_claim` handed an ALREADY-EVALUATED task to the next attempt and
                # it was scored twice. So the scored-but-unmarked state is persisted DURABLY
                # first — `record_unconfirmed_score` never raises, so a second failure cannot
                # replace the operator's interrupt with a disk error — and only then do we
                # re-raise, because the interrupt must still stop the run.
                claim_release_forbidden = True
                recorded = record_unconfirmed_score(
                    claims_dir, claim_key, repo_dir=repo_dir,
                    reason=f"interrupted_before_scored_marker:{type(exc).__name__}",
                    payload={"reward": reward, "result_dir": str(run_dir),
                             "domain": domain, "task_id": example_id},
                )
                print("[bridge] interrupted between the official score and its claim marker; "
                      "RETAINING the claim so the task is not handed to another attempt"
                      + (f"; recorded the scored-but-unmarked state at {recorded}" if recorded
                         else "; FATAL: nothing on disk records the score and the lock EXPIRES"),
                      file=sys.stderr, flush=True)
                raise
            claim_scored = True
        (run_dir / "result.txt").write_text(f"{reward}\n", encoding="utf-8")
        final.update({"outcome": "completed", "exit_code": 0})
        published = _write_outcome(reward, "completed", "official_evaluate", extra={
            "ouroboros_status": str(latest.get("status") or ""),
            "task_id_ouroboros": task_id,
            "infeasible_declared": infeasible_declared,
            "a11y_enabled": bool(args.allow_a11y),
            "budget_counters": budget_counters,
            "max_rounds_effective": _effective_max_rounds(settings_path),
            **({"osworld_fail_info": fail_info} if infeasible_declared else {}),
        })
        if published.get("publication_errors"):
            # The score itself reached every record that was still writable, so this is NOT a
            # lost result — but at least one authoritative record is missing, which an operator
            # aggregating the run must be told about rather than reading exit 0 as "complete".
            final.update({"outcome": "publication_failed_after_scoring", "exit_code": 1,
                          "error": {"type": "PublicationIncomplete",
                                    "message": "; ".join(published["publication_errors"])[:4000]}})
            return 1
        return 0
    except Exception as exc:  # noqa: BLE001 - denominator-preserving adapter failure
        # `reward` is None until `env.evaluate()` returns and not None after, so a failure
        # carrying a score is a PUBLICATION failure, not a run that never happened. Reporting
        # None erased it: `.scored` is already durable here, so no later attempt may retry, and
        # the only surviving record would claim `not_run` for a task that WAS scored.
        #
        # This handler no longer REPLAYS a failed publication: `_write_cu_outcome` attempts each
        # destination independently and does not raise, so a failure inside it is disclosed on
        # the success path above and never arrives here. What still arrives is a failure BEFORE
        # publication (`result.txt`, the evaluate/step path), which this republishes — the case
        # the previous round fixed. The guard below keeps that true by construction: publishing
        # from a failure handler must never replace the original error with a second one.
        reason = "publication_failed_after_scoring" if reward is not None else type(exc).__name__
        final.update({"outcome": reason if reward is not None else "adapter_error",
                      "exit_code": 1,
                      "error": {"type": type(exc).__name__, "message": str(exc)[:4000]}})
        try:
            _write_outcome(reward, "adapter_error", reason, f"{type(exc).__name__}: {exc}")
        except Exception as publish_exc:  # noqa: BLE001 - the ORIGINAL failure is the report
            print(f"[bridge] outcome publication from the failure handler also failed: "
                  f"{type(publish_exc).__name__}: {publish_exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if env is not None:
            try:
                env.close()
            except Exception:
                pass
        # Release the lane claim last. `claim_scored` is the ONLY thing that makes the claim
        # permanent, and it is True only once `mark_task_scored` CONFIRMED the marker on disk;
        # an unscored attempt (adapter error, blocked preflight, crash) leaves the task
        # claimable so a later attempt may retry it. `claim_fd is None` means this process
        # never held the lock (no --claim-dir, or another attempt owns it): releasing then would
        # delete a working holder's lockfile. `claim_release_forbidden` is the third case —
        # SCORED but UNMARKED — where releasing is the one thing that must not happen.
        if claim_release_forbidden:
            print("[bridge] RETAINING the claim lock: this task has an official score whose "
                  "canonical marker could not be persisted. The permanent refusal comes from "
                  "the .scored_unconfirmed marker (staleness cannot reclaim it); the retained "
                  "lock only covers the interim. Clear it deliberately once the score is "
                  "reconciled.", file=sys.stderr, flush=True)
        elif claims_dir is not None and claim_fd is not None:
            release_task_claim(claims_dir, claim_key, claim_fd, scored=claim_scored,
                               repo_dir=repo_dir,
                               payload={"reward": reward, "result_dir": str(run_dir),
                                        "domain": domain, "task_id": example_id})


if __name__ == "__main__":
    raise SystemExit(main())
