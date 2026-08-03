import ast
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def install_namespace_stub(qualified_name, package_path):
    if qualified_name in sys.modules:
        return
    module = types.ModuleType(qualified_name)
    module.__path__ = [str(package_path)]
    module.__package__ = qualified_name
    sys.modules[qualified_name] = module


def install_cr2w_stubs():
    install_namespace_stub("witcher3_tools", REPO_ROOT / "witcher3_tools")
    install_namespace_stub("witcher3_tools.CR2W", REPO_ROOT / "witcher3_tools" / "CR2W")


def exec_functions(path, names, namespace=None):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    wanted = set(names)
    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted
    ]
    missing = wanted.difference(node.name for node in nodes)
    if missing:
        raise AssertionError(f"Missing function(s) in {path}: {sorted(missing)}")
    namespace = dict(namespace or {})
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace
