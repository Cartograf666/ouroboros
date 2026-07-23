"""Launcher Remote-connection bridge: origin-gate authority contract.

The MainApi bridge is a closure inside ``launcher.main()``, so the gate is
verified two ways: the pure origin predicate directly, and an AST pin that
every privileged MainApi method begins with the origin check (JS visibility is
presentation; the Python-side refusal is the authority boundary — D20).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from ouroboros.remote_tunnel import is_local_origin

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Bridge methods deliberately callable from a REMOTE page: the connection pill
# needs status + a way home. EVERY other MainApi method — including the host
# file bridge (download_file_to_downloads/open_file_with_default_app) — is
# origin-gated, so the untrusted remote page cannot reach a privileged action.
REMOTE_SAFE_METHODS = {"remote_status", "remote_disconnect"}


@pytest.mark.parametrize(
    "url,port,expected",
    [
        ("http://127.0.0.1:8765/", 8765, True),
        ("http://localhost:8765/settings", 8765, True),
        ("http://127.0.0.1:51234/", 8765, False),  # tunnel page is NOT local origin
        ("http://evil.example:8765/", 8765, False),
        ("https://127.0.0.1:8765/", 8765, False),
        ("file:///tmp/x.html", 8765, False),
        ("", 8765, False),
    ],
)
def test_is_local_origin(url, port, expected):
    assert is_local_origin(url, port) is expected


def _main_api_methods() -> dict:
    tree = ast.parse((REPO_ROOT / "launcher.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MainApi":
            return {
                item.name: item
                for item in node.body
                if isinstance(item, ast.FunctionDef)
            }
    raise AssertionError("MainApi class not found in launcher.py")


def _starts_with_origin_gate(fn: ast.FunctionDef) -> bool:
    for stmt in fn.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # docstring
        return (
            isinstance(stmt, ast.If)
            and isinstance(stmt.test, ast.UnaryOp)
            and isinstance(stmt.test.op, ast.Not)
            and isinstance(stmt.test.operand, ast.Call)
            and getattr(stmt.test.operand.func, "id", "") == "_current_page_is_local_origin"
        )
    return False


def test_every_privileged_main_api_method_is_origin_gated():
    methods = _main_api_methods()
    assert "remote_connect" in methods and "remote_save" in methods  # surface exists
    # The host file bridge is included here (not exempt): its R1 security gate
    # must stay pinned so a future refactor cannot silently drop it.
    assert "download_file_to_downloads" in methods and "open_file_with_default_app" in methods
    ungated = [
        name
        for name, fn in methods.items()
        if name not in REMOTE_SAFE_METHODS
        and not _starts_with_origin_gate(fn)
    ]
    assert ungated == [], (
        f"MainApi methods missing the origin gate: {ungated}. Every privileged "
        "bridge method must refuse calls from a non-local page (D20)."
    )


def test_remote_safe_methods_are_not_origin_gated():
    methods = _main_api_methods()
    for name in REMOTE_SAFE_METHODS:
        assert name in methods
        assert not _starts_with_origin_gate(methods[name]), (
            f"{name} must stay callable from the remote page (connection pill)"
        )


def test_file_bridge_validates_active_view_port():
    # Defense-in-depth beneath the origin gate: even though the file bridge is
    # origin-gated to the local page, the URL resolver still scopes the fetch to
    # the active view port so it can never target a same-shaped path on a
    # different server if it were ever reached mid-navigation.
    source = (REPO_ROOT / "launcher.py").read_text(encoding="utf-8")
    fn_start = source.index("def _resolve_bridge_file_url")
    fn_body = source[fn_start : source.index("def _unique_bridge_target")]
    assert 'view_state["port"]' in fn_body, (
        "_resolve_bridge_file_url must validate against the ACTIVE view port "
        "(local or tunnel), not the pinned local server port"
    )


def test_privileged_remote_modules_are_protected_paths():
    """C3: the launcher/owner-authority modules must be protected from
    advanced-mode self-modification (origin gate + owner-only profile writer)."""
    from ouroboros.runtime_mode_policy import is_protected_runtime_path, protected_path_category

    for path in ("ouroboros/remote_tunnel.py", "ouroboros/remote_support.py"):
        assert is_protected_runtime_path(path), path
        assert protected_path_category(path) == "safety-critical", path


def test_remote_connections_self_change_detector():
    """W1: the shell self-change guard for OUROBOROS_REMOTE_CONNECTIONS mirrors
    the other owner-only-key detectors (safety-mode/context-mode)."""
    from ouroboros.tools.registry import _detect_remote_connections_self_change as det

    # Blocked: the writer, or the key together with a settings-write channel.
    assert det("python -c 'from ouroboros.config import update_remote_connections; update_remote_connections([])'")
    assert det('echo \'{"ouroboros_remote_connections": []}\' > ~/ouroboros/data/settings.json')
    assert det("curl -x post /api/settings -d ouroboros_remote_connections=...")
    # Not blocked: unrelated commands, or a bare mention without a write channel.
    assert not det("ls -la ~/ouroboros/data")
    assert not det("grep ouroboros_remote_connections docs/architecture.md")


def test_disconnect_tunnel_quietly_handles_absent_and_failing_manager(monkeypatch):
    import launcher

    monkeypatch.setattr(launcher, "_tunnel_manager", None)
    launcher._disconnect_tunnel_quietly()  # no-op

    class _Boom:
        def disconnect(self):
            raise RuntimeError("nope")

    monkeypatch.setattr(launcher, "_tunnel_manager", _Boom())
    launcher._disconnect_tunnel_quietly()  # swallowed

    calls = []

    class _Ok:
        def disconnect(self):
            calls.append(True)

    monkeypatch.setattr(launcher, "_tunnel_manager", _Ok())
    launcher._disconnect_tunnel_quietly()
    assert calls == [True]


def test_on_tunnel_state_guards_navigation_by_generation():
    # R11C1(b): _on_tunnel_state must reject a navigation whose generation is
    # stale (a delayed gave_up/reconnected from a superseded connection must not
    # move the window). Pinned by source: the generation guard precedes any
    # load_url in the handler.
    source = (REPO_ROOT / "launcher.py").read_text(encoding="utf-8")
    start = source.index("def _on_tunnel_state")
    body = source[start : source.index("global _tunnel_manager", start)]
    assert "current_generation" in body, "handler must consult the manager generation"
    assert body.index("current_generation") < body.index("load_url"), (
        "the stale-generation guard must precede any navigation"
    )


def test_shutdown_paths_tear_down_the_tunnel():
    source = (REPO_ROOT / "launcher.py").read_text(encoding="utf-8")
    closing = source[source.index("def _on_closing") : source.index("window.events.closing")]
    assert "_disconnect_tunnel_quietly()" in closing  # window close: graceful
    panic = source[source.index("if exit_code == PANIC_EXIT_CODE") :]
    panic = panic[: panic.index("time.sleep(2)")]
    # Panic must use the FORCE teardown (immediate SIGKILL, no graceful wait) —
    # Emergency Stop Invariant forbids delay.
    assert "_force_disconnect_tunnel()" in panic
    assert "_disconnect_tunnel_quietly()" not in panic
