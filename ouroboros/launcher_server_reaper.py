"""Launcher-owned reaping of same-install stray `server.py` processes.

A launcher that holds the single-instance pid lock is, by that lock, the only actor entitled to run
a managed server against this data directory — so any OTHER process running THIS install's
`server.py` under THIS launcher's stamped environment is a leftover generation, not a peer. This
module finds those processes from live kernel state and kills them; `launcher.py` owns only the
thin wrapper and the call sites.

A pid is PROVEN only on three live facts, all read fresh: the exact `<REPO_DIR>/server.py` path in
its command line, `OUROBOROS_DATA_DIR=<our data dir>` in its environment, and
`OUROBOROS_MANAGED_BY_LAUNCHER=1` in its environment. `start_agent` stamps both assignments, so
every launcher-started generation carries them, while a direct or dev run of the same checkout does
not and is spared with a warning. An environment that cannot be READ is never a licence to kill.

The custody ledger is deliberately never consulted: missing ledger entries are the defect this
sweep repairs, so a ledger lookup would spare exactly the strays that matter. POSIX only — on
Windows the launcher's kill-on-close Job Object already reaps orphans.
"""

from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import time
from typing import Iterable, List, Optional, Set, Tuple

# Module-object access (not from-imports): tests monkeypatch these names on platform_layer.
from ouroboros import platform_layer as _pl
from ouroboros.process_containment import (
    ENV_ASSIGNMENT_PRESENT,
    pid_environment_assignment_state,
)

log = logging.getLogger(__name__)

# Both stamped by `launcher.start_agent`; both required before anything is signalled.
MANAGED_MARKER_ENV = "OUROBOROS_MANAGED_BY_LAUNCHER"
MANAGED_MARKER_VALUE = "1"
DATA_DIR_ENV = "OUROBOROS_DATA_DIR"

# A stray can fork between the scan and the signal and the child inherits both the cmdline and the
# environment, so one pass proves nothing. Bounded so a pid that refuses to die cannot turn this
# into an unbounded kill loop — the caller is told about survivors instead.
REAP_PASSES = 3
_SETTLE_SEC = 0.05
# How long a signalled pid gets to actually die before it is counted a survivor.
_CONFIRM_DEADLINE_SEC = 1.0


def _path_forms(base, leaf: str = "") -> Set[str]:
    """The literal and the symlink-resolved spelling of a path. A command line or an environment
    carries whatever spelling the launcher passed, which `resolve()` may not reproduce (``/var`` vs
    ``/private/var``), so both are accepted as the same install."""
    path = pathlib.Path(base)
    forms = {str(path / leaf) if leaf else str(path)}
    try:
        resolved = path.resolve()
        forms.add(str(resolved / leaf) if leaf else str(resolved))
    except OSError:
        pass
    return forms


def _candidate_pids() -> "Optional[List[int]]":
    """This user's pids whose command line mentions ouroboros, or ``None`` when enumeration itself
    failed. ``-U``: other accounts run their own legitimate installs and are unsignallable anyway.
    ``-i``: a packaged install runs ``EMBEDDED_PYTHON .../Ouroboros/repo/server.py`` with a capital
    O. ``None`` (no pgrep, or pgrep errored — rc>1; rc 1 is the documented no-matches answer) must
    never read as a clean sweep: nothing was checked."""
    try:
        out = subprocess.run(
            ["pgrep", "-U", str(os.getuid()), "-fi", "ouroboros"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if out.returncode not in (0, 1):
        return None
    pids: List[int] = []
    for line in (out.stdout or "").splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid > 0:
            pids.append(pid)
    return pids


def install_server_path_forms(repo_dir) -> Set[str]:
    """The spellings of THIS install's server.py a live command line may carry."""
    return _path_forms(repo_dir, "server.py")


def command_names_our_server(command: str, server_paths: Set[str]) -> bool:
    """Whether a live command line IS this install's server: an exact argv token equal to
    ``<REPO_DIR>/server.py`` directly after a python-named interpreter token — the launcher's only
    spawn shape. A bare substring test would also match editors, pagers or log tools whose
    arguments merely mention the path; those are not server generations and must not even appear
    as spared candidates. A sibling install, a bench clone and a dev checkout carry different
    paths and never match. Shared with the startup stray check so its ``scope`` labels agree with
    what this sweep would actually treat as a server."""
    tokens = (command or "").split()
    for i in range(1, len(tokens)):
        if tokens[i] in server_paths and pathlib.PurePath(tokens[i - 1]).name.lower().startswith("python"):
            return True
    return False


def _runs_our_server(pid: int, server_paths: Set[str]) -> bool:
    return command_names_our_server(_pl.process_command(pid) or "", server_paths)


def _is_launcher_managed(pid: int, data_dir_values: Set[str]) -> bool:
    """Whether ``pid``'s live environment carries BOTH launcher assignments.

    ABSENT and UNREADABLE are equally not-a-proof: a direct run of this checkout lacks the marker,
    and an environment that could not be read has answered nothing."""
    if (pid_environment_assignment_state(pid, MANAGED_MARKER_ENV, MANAGED_MARKER_VALUE)
            != ENV_ASSIGNMENT_PRESENT):
        return False
    return any(
        pid_environment_assignment_state(pid, DATA_DIR_ENV, value) == ENV_ASSIGNMENT_PRESENT
        for value in data_dir_values
    )


def find_same_install_server_pids(
    repo_dir, data_dir, exclude_pids: "Optional[Iterable[int]]" = None
) -> "Tuple[List[int], List[int]]":
    """``(proven, unproven)`` pids running this install's ``server.py``, from live kernel state.

    ``proven`` carries all three facts and may be killed; ``unproven`` matches the path only and is
    reported so a spared process is never invisible."""
    if _pl.IS_WINDOWS:
        return [], []
    server_paths = _path_forms(repo_dir, "server.py")
    data_dir_values = _path_forms(data_dir)
    if any(" " in path for path in server_paths):
        # The exact-token proof cannot represent a whitespace path, so nothing
        # could ever be proven; the caller (reap) names this once per sweep.
        return [], []
    candidates = _candidate_pids()
    if candidates is None:
        # Enumeration itself failed: nothing was CHECKED, which is different from
        # nothing found. Raising (instead of returning empty) routes a MID-sweep
        # failure into reap's aborted-mid-work path, so pids already proven in
        # this sweep are still reported as survivors; the systemic no-pgrep case
        # is answered by reap's pre-check with its own named warning.
        raise RuntimeError("process enumeration unavailable")
    known = {os.getpid(), os.getppid()}
    known.update(int(pid) for pid in (exclude_pids or ()) if int(pid) > 0)
    known_groups: Set[int] = set()
    for pid in known:
        if pid > 0:
            try:
                known_groups.add(_pl.process_group_id(pid))
            except Exception:
                continue
    known_groups.discard(0)
    proven: List[int] = []
    unproven: List[int] = []
    for pid in candidates:
        if pid in known or not _runs_our_server(pid, server_paths):
            continue
        try:
            if _pl.process_group_id(pid) in known_groups:
                continue  # part of a tree we already know about
        except Exception:
            pass
        (proven if _is_launcher_managed(pid, data_dir_values) else unproven).append(pid)
    return proven, unproven


def _revalidate_and_kill(pid: int, server_paths: Set[str], data_dir_values: Set[str]) -> bool:
    """Re-prove ``pid`` from live state and signal it with NOTHING in between: any lookup in that
    gap is a window for the pid to exit and be recycled onto a stranger. Kills the pid TREE, never
    the group — server workers hold their own sessions and a reused pgid reaches bystanders.

    True means CONFIRMED DEAD, not merely signalled: ``kill_pid_tree`` swallows per-pid errors, so
    only a liveness read after the signal can say what it achieved — a pid logged as reaped while
    it survived would contradict the survivor report from the same generation."""
    if not _runs_our_server(pid, server_paths) or not _is_launcher_managed(pid, data_dir_values):
        return False
    _pl.kill_pid_tree(pid)
    deadline = time.time() + _CONFIRM_DEADLINE_SEC
    while True:
        if not _pl.pid_is_alive(pid):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(_SETTLE_SEC)


def reap_same_install_strays(
    repo_dir, data_dir, reason: str = "startup",
    exclude_pids: "Optional[Iterable[int]]" = None,
) -> List[int]:
    """Kill proven same-install strays; return the proven pids still alive after the last pass.

    A non-empty return is the caller's signal that starting another generation would collide."""
    if _pl.IS_WINDOWS:
        return []
    server_paths = _path_forms(repo_dir, "server.py")
    data_dir_values = _path_forms(data_dir)
    if any(" " in path for path in server_paths):
        log.warning(
            "Same-install stray sweep disabled (%s): the repo path contains whitespace, which the "
            "exact-token identity proof cannot represent — no process was checked.", reason,
        )
        return []
    if _candidate_pids() is None:
        log.warning(
            "Same-install stray sweep could not enumerate processes (%s) — nothing was checked, "
            "which is not a clean sweep.", reason,
        )
        return []
    killed: Set[int] = set()
    proven_seen: Set[int] = set()
    survivors: List[int] = []
    try:
        for attempt in range(REAP_PASSES):
            proven, unproven = find_same_install_server_pids(repo_dir, data_dir, exclude_pids)
            proven_seen.update(proven)
            if unproven and attempt == 0:
                log.warning(
                    "Sparing same-install server process(es) with no launcher proof (%s): %s — a "
                    "direct run of this checkout, or an environment that could not be read.",
                    reason, sorted(unproven),
                )
            if not proven:
                break
            for pid in sorted(proven):
                if _revalidate_and_kill(pid, server_paths, data_dir_values):
                    killed.add(pid)
            time.sleep(_SETTLE_SEC)
        else:
            # The pass budget ran out with pids still proven: read survivors FRESH rather than
            # inferring them from the last kill attempt, which cannot see what the signal achieved.
            survivors, _ = find_same_install_server_pids(repo_dir, data_dir, exclude_pids)
    except Exception:
        # A sweep interrupted mid-work must not read as swept-clean: everything proven in this
        # sweep and not confirmed dead is reported as surviving, so the caller cannot start a
        # colliding generation on the strength of an exception.
        log.warning("Same-install stray sweep aborted mid-work (%s)", reason, exc_info=True)
        survivors = sorted(proven_seen - killed)
    if killed:
        log.info("Reaped same-install stray server process(es) (%s): %s", reason, sorted(killed))
    return sorted(survivors)
