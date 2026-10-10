"""Checks a staged copy of the code before a deploy makes it live.

The API, worker and scrapers bind-mount the server tree, and many imports sit inside functions
behind ``try/except Exception``. A file that drops a name other modules use therefore fails
quietly at run time, or takes every route down when the import is at module level. This reads
every module with ``ast`` and reports names that will not resolve:

* ``from pkg.mod import name`` at any depth, including inside functions;
* ``alias.name`` where ``alias`` is an imported first-party module;
* keyword arguments a first-party function does not accept.

Issues already present are listed in ``deploy_preflight_known.txt``; only new ones fail.
Run ``python -m cashflow_ops.deploy_preflight --root /app`` for the full check (it also imports the
API), or with ``--static-only`` to read files without importing anything.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

FIRST_PARTY = ("cashflow_db", "cashflow_ops", "cashflow_forecast", "cashflow_reconcile")
SKIP_DIRS = frozenset({"venv", ".venv", "__pycache__", "node_modules", "tests", "site-packages"})
OPTIONAL_IMPORT_ERRORS = frozenset({"ImportError", "ModuleNotFoundError"})
KNOWN_FILE = Path(__file__).with_name("deploy_preflight_known.txt")


@dataclass(frozen=True)
class Issue:
    kind: str
    path: str
    line: int
    detail: str

    @property
    def key(self) -> str:
        """Stable across edits that only move lines."""
        return f"{self.kind}|{self.path}|{self.detail}"

    def __str__(self) -> str:
        return f"{self.kind:8} {self.path}:{self.line} {self.detail}"


@dataclass
class ModuleInfo:
    name: str
    path: Path
    tree: ast.Module | None
    names: set[str] = field(default_factory=set)
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = field(default_factory=dict)
    open: bool = False


def _iter_sources(root: Path):
    for package in FIRST_PARTY:
        base = root / package
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(root)
            if SKIP_DIRS.intersection(rel.parts[:-1]):
                continue
            yield path, rel


def _module_name(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _bound_names(target: ast.AST) -> set[str]:
    return {node.id for node in ast.walk(target) if isinstance(node, ast.Name)}


def _top_level(info: ModuleInfo) -> None:
    """Names a module defines at import time, including inside top-level if/try blocks."""
    counts: dict[str, int] = {}

    def visit(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info.names.add(node.name)
                counts[node.name] = counts.get(node.name, 0) + 1
                info.functions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                info.names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    info.names |= _bound_names(target)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                info.names |= _bound_names(node.target)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    info.names.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        info.open = True
                    else:
                        info.names.add(alias.asname or alias.name)
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                visit(node.body)
                visit(getattr(node, "orelse", []))
                visit(getattr(node, "finalbody", []))
                for handler in getattr(node, "handlers", []):
                    visit(handler.body)
                for item in getattr(node, "items", []):
                    if item.optional_vars is not None:
                        info.names |= _bound_names(item.optional_vars)
            elif isinstance(node, (ast.For, ast.While)):
                if isinstance(node, ast.For):
                    info.names |= _bound_names(node.target)
                visit(node.body)
                visit(node.orelse)

    if info.tree is not None:
        visit(info.tree.body)
    if "__getattr__" in info.names:
        info.open = True
    for name, count in counts.items():
        fn = info.functions.get(name)
        if count > 1 or (fn is not None and fn.decorator_list):
            info.functions.pop(name, None)


class Scanner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.modules: dict[str, ModuleInfo] = {}
        self.issues: list[Issue] = []
        for path, rel in _iter_sources(root):
            name = _module_name(rel)
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
            except SyntaxError as exc:
                self.issues.append(Issue("syntax", rel.as_posix(), exc.lineno or 0, str(exc.msg)))
                tree = None
            except UnicodeDecodeError as exc:
                self.issues.append(Issue("syntax", rel.as_posix(), 0, f"not utf-8: {exc.reason}"))
                tree = None
            info = ModuleInfo(name=name, path=rel, tree=tree)
            _top_level(info)
            self.modules[name] = info

    def _resolve(self, current: ModuleInfo, node: ast.ImportFrom) -> str | None:
        if not node.level:
            return node.module
        package = current.name.split(".")
        if current.path.name != "__init__.py":
            package = package[:-1]
        if node.level > 1:
            package = package[: len(package) - (node.level - 1)]
        base = ".".join(package)
        if node.module:
            return f"{base}.{node.module}" if base else node.module
        return base

    def _is_first_party(self, module: str | None) -> bool:
        return bool(module) and module.split(".")[0] in FIRST_PARTY

    def _add(self, kind: str, info: ModuleInfo, line: int, detail: str) -> None:
        self.issues.append(Issue(kind, info.path.as_posix(), line, detail))

    def scan(self) -> list[Issue]:
        for info in self.modules.values():
            if info.tree is not None:
                self._scan_module(info)
        return sorted(self.issues, key=lambda issue: (issue.path, issue.line, issue.kind, issue.detail))

    def _optional(self, parents: dict[ast.AST, ast.AST], node: ast.AST) -> bool:
        """An import under `except ImportError` is meant to be optional."""
        current = parents.get(node)
        while current is not None:
            if isinstance(current, ast.Try):
                for handler in current.handlers:
                    names = _bound_names(handler.type) if handler.type is not None else set()
                    if names & OPTIONAL_IMPORT_ERRORS:
                        return True
            current = parents.get(current)
        return False

    def _scan_module(self, info: ModuleInfo) -> None:
        tree = info.tree
        assert tree is not None
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        module_aliases = self._aliases(info, tree.body, parents)
        self._check_uses(info, tree, module_aliases, parents)

    def _aliases(
        self,
        info: ModuleInfo,
        body: list[ast.stmt],
        parents: dict[ast.AST, ast.AST],
    ) -> dict[str, tuple[str, str | None]]:
        """Names bound by imports in this scope: name -> (module, attribute or None for a module)."""
        found: dict[str, tuple[str, str | None]] = {}
        stack = list(body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if self._is_first_party(alias.name) and alias.asname and alias.name in self.modules:
                        found[alias.asname] = (alias.name, None)
            elif isinstance(node, ast.ImportFrom):
                self._check_from(info, node, parents)
                module = self._resolve(info, node)
                if not self._is_first_party(module):
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname or alias.name
                    sub = f"{module}.{alias.name}"
                    if sub in self.modules:
                        found[local] = (sub, None)
                    else:
                        found[local] = (module, alias.name)
            stack.extend(ast.iter_child_nodes(node))
        return found

    def _check_from(self, info: ModuleInfo, node: ast.ImportFrom, parents: dict[ast.AST, ast.AST]) -> None:
        module = self._resolve(info, node)
        if not self._is_first_party(module):
            return
        if self._optional(parents, node):
            return
        target = self.modules.get(module)
        if target is None:
            self._add("nomodule", info, node.lineno, f"from {module}")
            return
        if target.open or target.tree is None:
            return
        for alias in node.names:
            if alias.name == "*" or alias.name in target.names:
                continue
            if f"{module}.{alias.name}" in self.modules:
                continue
            self._add("missing", info, node.lineno, f"from {module} import {alias.name}")

    def _check_uses(
        self,
        info: ModuleInfo,
        tree: ast.Module,
        module_aliases: dict[str, tuple[str, str | None]],
        parents: dict[ast.AST, ast.AST],
    ) -> None:
        def scopes():
            yield tree, module_aliases, set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    local = dict(module_aliases)
                    local.update(self._aliases(info, node.body, parents))
                    shadowed = {a.arg for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs}
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Assign):
                            for target in sub.targets:
                                shadowed |= _bound_names(target)
                    for name in shadowed:
                        local.pop(name, None)
                    yield node, local, shadowed

        seen: set[tuple[int, int, str]] = set()
        for scope, aliases, _shadowed in scopes():
            for node in self._own_nodes(scope):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    bound = aliases.get(node.value.id)
                    if bound and bound[1] is None:
                        self._check_attr(info, node, bound[0], seen)
                elif isinstance(node, ast.Call):
                    self._check_call(info, node, aliases, seen)

    def _own_nodes(self, scope: ast.AST):
        """Nodes in this scope, not inside nested functions (those are their own scopes)."""
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            yield node
            stack.extend(ast.iter_child_nodes(node))

    def _check_attr(self, info: ModuleInfo, node: ast.Attribute, module: str, seen: set) -> None:
        target = self.modules.get(module)
        if target is None or target.open or target.tree is None:
            return
        if node.attr in target.names or f"{module}.{node.attr}" in self.modules:
            return
        key = (node.lineno, node.col_offset, "attr")
        if key in seen:
            return
        seen.add(key)
        self._add("attr", info, node.lineno, f"{module}.{node.attr}")

    def _check_call(self, info: ModuleInfo, node: ast.Call, aliases: dict, seen: set) -> None:
        func = node.func
        target_module: str | None = None
        target_name: str | None = None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            bound = aliases.get(func.value.id)
            if bound and bound[1] is None:
                target_module, target_name = bound[0], func.attr
        elif isinstance(func, ast.Name):
            bound = aliases.get(func.id)
            if bound and bound[1] is not None:
                target_module, target_name = bound
        if not target_module or not target_name:
            return
        target = self.modules.get(target_module)
        if target is None:
            return
        fn = target.functions.get(target_name)
        if fn is None or fn.args.kwarg is not None:
            return
        accepted = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
        for keyword in node.keywords:
            if keyword.arg is None or keyword.arg in accepted:
                continue
            key = (node.lineno, node.col_offset, keyword.arg)
            if key in seen:
                continue
            seen.add(key)
            self._add("kwarg", info, node.lineno, f"{target_module}.{target_name}({keyword.arg}=)")


def static_issues(root: Path) -> list[Issue]:
    return Scanner(root).scan()


def compile_issues(root: Path) -> list[Issue]:
    """Byte-compile every file in memory, so nothing is written next to the sources."""
    found = []
    for path, rel in _iter_sources(root):
        try:
            compile(path.read_bytes(), str(rel), "exec", dont_inherit=True)
        except SyntaxError as exc:
            found.append(Issue("syntax", rel.as_posix(), exc.lineno or 0, str(exc.msg)))
    return found


def import_issues() -> list[Issue]:
    """Import what the API needs at start. Router failures are swallowed by the app, so read them."""
    found = []
    try:
        import cashflow_db.repository  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        found.append(Issue("import", "cashflow_db/repository", 0, f"{type(exc).__name__}: {exc}"))
        return found
    try:
        from cashflow_forecast import api
    except Exception as exc:  # noqa: BLE001
        found.append(Issue("import", "cashflow_forecast/api.py", 0, f"{type(exc).__name__}: {exc}"))
        return found
    if not getattr(api, "_AUTH_MOUNTED", False):
        found.append(Issue("import", "cashflow_forecast/api.py", 0, "auth routes not mounted"))
    for failure in getattr(api, "_ROUTER_FAILURES", []):
        found.append(Issue("import", "cashflow_forecast/api.py", 0, f"router {failure.get('router')}: {failure.get('error')}"))
    return found


def load_known(path: Path = KNOWN_FILE) -> set[str]:
    if not path.is_file():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def new_issues(issues: list[Issue], known: set[str]) -> list[Issue]:
    return [issue for issue in issues if issue.key not in known]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--static-only", action="store_true", help="read files only, import nothing")
    parser.add_argument("--known", type=Path, default=KNOWN_FILE)
    parser.add_argument("--all", action="store_true", help="also print known issues")
    parser.add_argument("--write-known", type=Path, help="write every current issue key to this file")
    args = parser.parse_args(argv)

    issues = compile_issues(args.root) + static_issues(args.root)
    if not args.static_only:
        if str(args.root) not in sys.path:
            sys.path.insert(0, str(args.root))
        issues += import_issues()
    unique = {issue.key: issue for issue in issues}
    issues = sorted(unique.values(), key=lambda issue: (issue.path, issue.line, issue.kind))

    if args.write_known:
        args.write_known.write_text(
            "# Issues present before the deploy guard. Only new ones fail a deploy.\n"
            + "".join(f"{issue.key}\n" for issue in issues),
            encoding="utf-8",
        )
        print(f"wrote {len(issues)} known issue(s) to {args.write_known}")
        return 0

    known = load_known(args.known)
    fresh = new_issues(issues, known)
    if args.all:
        for issue in issues:
            print(("NEW   " if issue in fresh else "known ") + str(issue))
    else:
        for issue in fresh:
            print("NEW   " + str(issue))
    print(f"preflight: {len(fresh)} new, {len(issues) - len(fresh)} known")
    return 1 if fresh else 0


if __name__ == "__main__":
    raise SystemExit(main())
