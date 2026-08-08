"""Regression suite for the external-workspace git policy classifier.

These pin the bypasses found in the v6.27.0 audit (NW-4): environment-based
repo retargeting (``GIT_DIR`` / ``GIT_WORK_TREE``) and glued / newline shell
separators that previously let ``cd ws;git -C <runtime> reset`` masquerade as a
single ``cd`` segment with the ``-C`` selector never inspected. The round-1
``cd``-bypass fix shipped with zero behavioural coverage; this file is that
coverage plus the env and nested-shell vectors.
"""
import importlib
import pathlib

import pytest

policy = importlib.import_module("ouroboros.git_shell_policy")

WS = pathlib.Path("/tmp/ws")
REPO = pathlib.Path("/Users/anton/Ouroboros/repo")
DATA = pathlib.Path("/Users/anton/Ouroboros/data")
ROOTS = [REPO, DATA]


def _violation(cmd, *, cwd="/tmp/ws", allow_network=True):
    return policy.external_workspace_git_violation(
        cmd, active_root=WS, cwd=cwd, protected_roots=ROOTS, allow_network=allow_network
    )


# --- bypasses that MUST be blocked -----------------------------------------

BLOCKED_CASES = [
    pytest.param("GIT_DIR=/Users/anton/Ouroboros/repo/.git git reset --hard", id="env_git_dir_bare"),
    pytest.param("env GIT_DIR=/Users/anton/Ouroboros/repo/.git git reset --hard", id="env_git_dir_wrapper"),
    pytest.param("GIT_WORK_TREE=/Users/anton/Ouroboros/repo git checkout .", id="env_work_tree"),
    pytest.param("cd /tmp/ws;git -C /Users/anton/Ouroboros/repo reset --hard", id="glued_semicolon_cd_minusC"),
    pytest.param("cd /tmp/ws\ngit -C /Users/anton/Ouroboros/repo reset --hard", id="newline_cd_minusC"),
    pytest.param("git -C /Users/anton/Ouroboros/repo commit -m x", id="direct_minusC_repo_commit"),
    pytest.param("git --git-dir=/Users/anton/Ouroboros/repo/.git reset --hard", id="git_dir_flag_repo_reset"),
    pytest.param("git --work-tree=/Users/anton/Ouroboros/data checkout .", id="work_tree_flag_data_checkout"),
    pytest.param("sh -c 'git -C /Users/anton/Ouroboros/repo reset --hard'", id="nested_sh_c"),
    pytest.param("true && git -C /Users/anton/Ouroboros/repo clean -fd", id="glued_and_minusC"),
    pytest.param("cd /Users/anton/Ouroboros/repo && git commit -am x", id="cd_into_repo_then_commit"),
    # Bidirectional containment: an ANCESTOR target puts repo/ and data/ inside a
    # task-created work tree even though the target CONTAINS the protected roots.
    pytest.param("git -C /Users/anton/Ouroboros init", id="ancestor_init"),
    pytest.param("cd /Users/anton/Ouroboros && git add -A", id="ancestor_cwd_add"),
    pytest.param("git init /Users/anton", id="home_ancestor_init_arg"),
    # Casefold containment: APFS/NTFS are case-insensitive, so a re-cased spelling
    # of a protected root is the SAME directory there.
    pytest.param("git -C /users/anton/ouroboros/REPO reset --hard", id="casefold_minusC_reset"),
]


@pytest.mark.parametrize("cmd", BLOCKED_CASES)
def test_runtime_targeting_git_is_blocked(cmd):
    assert _violation(cmd), f"expected BLOCK for: {cmd!r}"


def test_network_subcommand_blocked_when_network_disabled():
    assert _violation("git clone https://example.com/x.git", allow_network=False)


def test_network_fence_applies_to_readonly_git_too():
    # ls-remote is read-only for TARGET purposes but still reaches the network:
    # the contract fence must hold independently of the read-only carve-out.
    assert _violation("git ls-remote https://example.com/x.git", allow_network=False)


# --- legitimate git that MUST stay allowed -----------------------------------

ALLOWED_CASES = [
    pytest.param("git status", id="status"),
    pytest.param("git commit -m 'work'", id="commit"),
    pytest.param("git clone https://example.com/x.git", id="clone_network_on"),
    pytest.param("cd /tmp/ws/sub && git commit -m x", id="cd_subdir_commit"),
    pytest.param("git -C /tmp/ws/sub status", id="minusC_inside_workspace"),
    pytest.param("echo 'git -C /Users/anton/Ouroboros/repo' > note.txt", id="echo_mentions_git_not_a_git_cmd"),
    pytest.param("grep -r 'git reset' .", id="grep_mentions_git"),
    # READ-ONLY git stays allowed even AT a runtime target (v4.5.1 / f14baf8f
    # false-block line): inspection is the vcs_status-equivalent lane.
    pytest.param("git -C /Users/anton/Ouroboros/repo status", id="readonly_minusC_repo_status"),
    pytest.param("git --git-dir=/Users/anton/Ouroboros/repo/.git log", id="readonly_git_dir_log"),
    pytest.param("cd /Users/anton/Ouroboros/repo && git status", id="readonly_cd_repo_status"),
    pytest.param("cd /Users/anton/Ouroboros/repo && git diff", id="readonly_cd_repo_diff"),
    pytest.param("git -C /Users/anton/Ouroboros/repo branch -l", id="readonly_branch_list_repo"),
]


@pytest.mark.parametrize("cmd", [
    pytest.param("git -C /tmp/proj commit -m x", id="minusC_outside_from_runtime_cwd"),
    pytest.param("git -C /tmp/proj init", id="minusC_outside_init_from_runtime_cwd"),
    pytest.param("git -C /tmp -C proj add -A", id="chained_minusC_outside_from_runtime_cwd"),
])
def test_minusC_retarget_outside_runtime_allowed_from_runtime_cwd(cmd):
    """Global `-C` chdirs BEFORE git runs: the effective directory — not the
    shell cwd — is the mutating target. The default (non-workspace) lane's
    default cwd IS the system repo, so `git -C <outside-tree> ...` from there
    must stay allowed or the flip re-creates the false-block class."""
    assert _violation(cmd, cwd=str(REPO)) == ""


def test_minusC_into_runtime_still_blocked_from_any_cwd():
    assert _violation("git -C /Users/anton/Ouroboros/repo commit -m x", cwd="/tmp/ws")
    # A RELATIVE -C resolves against the shell cwd and is still caught.
    assert _violation("git -C repo reset --hard", cwd="/Users/anton/Ouroboros")


def test_commit_minusC_message_reuse_is_not_a_path():
    # `git commit -C <commit>` (after the subcommand) reuses a commit message;
    # it must not be parsed as a path selector.
    assert _violation("git commit -C HEAD", cwd="/tmp/ws") == ""


@pytest.mark.parametrize("cmd", ALLOWED_CASES)
def test_legitimate_workspace_git_is_allowed(cmd):
    assert not _violation(cmd), f"expected ALLOW for: {cmd!r}"


def test_clone_allowed_when_network_enabled():
    assert not _violation("git clone https://example.com/x.git", allow_network=True)


# --- destination-aware init/clone (the default lane's cwd IS the system repo) ---

@pytest.mark.parametrize("cmd", [
    pytest.param("git init /tmp/proj", id="init_positional_destination_outside"),
    pytest.param("git init --bare /tmp/proj.git", id="init_bare_destination_outside"),
    pytest.param("git clone https://example.com/x.git /tmp/proj", id="clone_url_destination_outside"),
    pytest.param("git clone git@github.com:o/x.git /tmp/proj", id="clone_scp_url_destination_outside"),
])
def test_init_clone_with_outside_destination_allowed_from_runtime_cwd(cmd):
    """`git init <dir>` / `git clone <url> <dir>` mutate the DESTINATION, not the
    working directory. The default (non-workspace) lane's default cwd IS the system
    repo, so judging these against the cwd refused `git init ~/projects/foo` from
    direct chat and light mode — contradicting the owner contract that mutating git
    is free OUTSIDE the runtime in every lane and mode."""
    assert _violation(cmd, cwd=str(REPO)) == "", cmd


@pytest.mark.parametrize("cmd", [
    pytest.param("git init", id="init_no_destination_at_runtime_cwd"),
    pytest.param("git clone https://example.com/x.git", id="clone_no_destination_at_runtime_cwd"),
    pytest.param("git init /Users/anton/Ouroboros/repo/sub", id="init_destination_inside_runtime"),
    pytest.param("git init /Users/anton/Ouroboros", id="init_destination_is_runtime_ancestor"),
    pytest.param("git clone https://example.com/x.git /Users/anton/Ouroboros/data/x", id="clone_destination_inside_data"),
    pytest.param(
        "git init --separate-git-dir=/Users/anton/Ouroboros/repo/x /tmp/proj",
        id="init_separate_git_dir_into_runtime",
    ),
])
def test_init_clone_targeting_the_runtime_still_blocked(cmd):
    """With NO explicit destination the cwd IS the target, and an explicit
    destination (or a `--flag=<path>` retarget) inside/over a runtime root stays
    blocked — the unwind frees the tree, never the runtime."""
    assert _violation(cmd, cwd=str(REPO)), cmd


def test_relative_argument_symlinked_into_the_runtime_is_blocked():
    """A relative arg with no `..` was skipped on the assumption that a plain
    descend from a safe base cannot reach a protected root. A SYMLINK breaks that
    assumption, and resolve() follows it."""
    import os
    import tempfile

    base = pathlib.Path(tempfile.mkdtemp()).resolve()
    runtime = base / "runtime"
    runtime.mkdir()
    tree = base / "tree"
    tree.mkdir()
    os.symlink(runtime, tree / "link")
    violation = policy.external_workspace_git_violation(
        "git init ./link", active_root=tree, cwd="", protected_roots=[runtime], allow_network=True,
    )
    assert violation, "a symlinked relative destination must not bypass containment"


# --- read-only git classification (the runtime-read guard's exemption key) -----

@pytest.mark.parametrize("cmd,expected", [
    pytest.param("git status", True, id="ro_status"),
    pytest.param("git -C /Users/anton/Ouroboros/repo log", True, id="ro_minusC_runtime_log"),
    pytest.param("git log; git diff", True, id="ro_two_git_segments"),
    pytest.param("cd /Users/anton/Ouroboros/repo && git status", True, id="ro_cd_then_git"),
    pytest.param("git branch -l", True, id="ro_branch_list"),
    pytest.param("sh -c 'git log'", True, id="ro_nested_shell"),
    pytest.param("git commit -m x", False, id="mut_commit"),
    pytest.param("git branch -D x", False, id="mut_branch_delete"),
    pytest.param("git status && cat /Users/anton/Ouroboros/data/settings.json", False, id="mixed_git_and_cat"),
    pytest.param("cat /etc/passwd", False, id="not_git_at_all"),
    pytest.param("sh -c 'git commit -m x'", False, id="mut_nested_shell"),
    pytest.param("", False, id="empty"),
])
def test_is_readonly_git_command(cmd, expected):
    """ALL-or-nothing per segment: this is what lets read-only git through the
    external-workspace runtime-read guard WITHOUT opening a shell bypass for the
    secret/credential surface."""
    assert policy.is_readonly_git_command(cmd) is expected, cmd
