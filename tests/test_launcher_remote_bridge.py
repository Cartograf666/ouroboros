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
# needs status + a way home. Everything else in MainApi is origin-gated.
REMOTE_SAFE_METHODS = {"remote_status", "remote_disconnect"}
# File-bridge methods use the active-view-port URL validator instead of the
# origin gate (they must work on the remote page against the remote server).
ACTIVE_PORT_VALIDATED = {"download_file_to_downloads", "open_file_with_default_app"}


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
    ungated = [
        name
        for name, fn in methods.items()
        if name not in REMOTE_SAFE_METHODS
        and name not in ACTIVE_PORT_VALIDATED
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
    source = (REPO_ROOT / "launcher.py").read_text(encoding="utf-8")
    fn_start = source.index("def _resolve_bridge_file_url")
    fn_body = source[fn_start : source.index("def _unique_bridge_target")]
    assert 'view_state["port"]' in fn_body, (
        "_resolve_bridge_file_url must validate against the ACTIVE view port "
        "(local or tunnel), not the pinned local server port"
    )


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


def test_shutdown_paths_tear_down_the_tunnel():
    source = (REPO_ROOT / "launcher.py").read_text(encoding="utf-8")
    closing = source[source.index("def _on_closing") : source.index("window.events.closing")]
    assert "_disconnect_tunnel_quietly()" in closing
    panic = source[source.index("if exit_code == PANIC_EXIT_CODE") :]
    panic = panic[: panic.index("time.sleep(2)")]
    assert "_disconnect_tunnel_quietly()" in panic
