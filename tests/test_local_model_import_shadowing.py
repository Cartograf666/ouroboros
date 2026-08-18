"""Regression pin for the local-model stop path (F823 class)."""
from __future__ import annotations

import ast
import pathlib


def test_local_model_stop_server_does_not_shadow_module_imports():
    """A function-local `import X` where X is ALSO a module-level import makes X
    local to the WHOLE function, so every earlier `X.attr` raises
    UnboundLocalError. In stop_server that hit `subprocess.TimeoutExpired` on the
    force-kill path: a local llama.cpp server that ignored SIGTERM was never
    killed, and the pkill fallback below it never ran either.
    """
    src = pathlib.Path("ouroboros/local_model.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    module_names = {a.asname or a.name.split(".")[0]
                    for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    module_names |= {a.asname or a.name
                     for n in tree.body if isinstance(n, ast.ImportFrom) for a in n.names}

    shadows = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    if name in module_names:
                        shadows.append(f"{fn.name}() re-imports {name!r} at line {node.lineno}")
    assert not shadows, "module-level import shadowed inside a function:\n" + "\n".join(shadows)
