"""Deterministic hygiene pins for runtime code.

Each guards a CLASS that ruff or the existing gates do not cover, and each was
written against a real defect found in the tree rather than in the abstract.
"""
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


def test_runtime_carries_no_personal_absolute_paths():
    """A developer's own home directory must not ship inside runtime code.

    ouroboros/tools/antigravity.py listed "/Users/alex/.local/bin/agy" beside the
    portable "~/.local/bin/agy" that already resolves to it through
    os.path.expanduser, so the absolute entry was dead for its author and wrong for
    everyone else.
    """
    import re

    roots = ["ouroboros", "supervisor", "skills", "web"]
    pattern = re.compile(r"""["'](?:/Users/|/home/)[A-Za-z][A-Za-z0-9._-]*/""")
    offenders = []
    for root in roots:
        for path in pathlib.Path(root).rglob("*"):
            if path.suffix not in {".py", ".js"} or not path.is_file():
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path}:{number}: {line.strip()[:100]}")
    for extra in ("server.py", "launcher.py"):
        for number, line in enumerate(pathlib.Path(extra).read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{extra}:{number}: {line.strip()[:100]}")
    assert not offenders, "personal absolute path in runtime code:\n" + "\n".join(offenders)
