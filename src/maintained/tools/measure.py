"""Source metrics for a Rich checkout: file sizes, class/function counts and the
internal dependency graph.

Written for the open-source reading practice so that every number quoted in the
report can be reproduced instead of estimated.  Read-only: it never writes to
the tree it measures.

Usage::

    python tools/measure.py --root src/original-provided/rich
    python tools/measure.py --root src/original-provided/rich --json metrics.json
"""

from __future__ import annotations

import argparse
import ast
import collections
import json
import pathlib
from typing import Dict, List, Sequence, Set, Tuple

__all__ = ["measure", "main"]


def _count(path: pathlib.Path) -> Tuple[int, int, int]:
    """Return ``(lines, classes, top_level_functions)`` for one source file."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    classes = sum(1 for node in tree.body if isinstance(node, ast.ClassDef))
    functions = sum(
        1
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    return len(text.splitlines()), classes, functions


def _internal_imports(path: pathlib.Path) -> Set[str]:
    """Set of sibling module names imported with a relative import.

    ``from .console import Console`` inside ``rich/table.py`` yields ``"console"``.
    Relative imports are the only reliable signal here because Rich always uses
    them internally; absolute ``from rich.x import y`` would be ambiguous with a
    third-party package of the same name.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    deps: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            deps.add(node.module.split(".")[0])
    return deps


def measure(root: pathlib.Path) -> Dict[str, object]:
    """Collect the metrics for every ``*.py`` file under ``root``."""
    files = sorted(root.glob("*.py"))
    rows: List[Tuple[int, str, int, int]] = []
    graph: Dict[str, Set[str]] = {}
    for path in files:
        lines, classes, functions = _count(path)
        rows.append((lines, path.name, classes, functions))
        graph[path.stem] = _internal_imports(path)
    rows.sort(reverse=True)

    # in-degree: how many sibling modules import this one
    reverse: Dict[str, Set[str]] = collections.defaultdict(set)
    for module, deps in graph.items():
        for dep in deps:
            reverse[dep].add(module)

    in_degree = sorted(
        ((len(users), module, sorted(users)) for module, users in reverse.items()),
        reverse=True,
    )
    out_degree = sorted(
        ((len(deps), module, sorted(deps)) for module, deps in graph.items()),
        reverse=True,
    )
    return {
        "file_count": len(files),
        "total_lines": sum(row[0] for row in rows),
        "edge_count": sum(len(deps) for deps in graph.values()),
        "files": [
            {"lines": n, "name": name, "classes": c, "functions": f}
            for n, name, c, f in rows
        ],
        "in_degree": [
            {"count": n, "module": module, "users": users}
            for n, module, users in in_degree
        ],
        "out_degree": [
            {"count": n, "module": module, "deps": deps}
            for n, module, deps in out_degree
        ],
    }


def _print_report(data: Dict[str, object], top: int) -> None:
    print(
        f"files={data['file_count']}  lines={data['total_lines']}  "
        f"internal_edges={data['edge_count']}\n"
    )
    print(f"=== 行数排名 (top {top}) ===")
    print(f"{'lines':>6}  {'classes':>7}  {'funcs':>5}  file")
    for entry in data["files"][:top]:  # type: ignore[index]
        print(
            f"{entry['lines']:6d}  {entry['classes']:7d}  {entry['functions']:5d}  "
            f"{entry['name']}"
        )

    print(f"\n=== 入度排名（被依赖最多，top {top}） ===")
    for entry in data["in_degree"][:top]:  # type: ignore[index]
        users = ", ".join(entry["users"][:6])
        print(f"{entry['count']:3d}  {entry['module']:22s} {users}")

    print(f"\n=== 出度排名（依赖最多，top {top}） ===")
    for entry in data["out_degree"][:top]:  # type: ignore[index]
        deps = ", ".join(entry["deps"][:8])
        print(f"{entry['count']:3d}  {entry['module']:22s} {deps}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="src/original-provided/rich")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--json", default=None, help="also write the raw data here")
    args = parser.parse_args(argv)

    data = measure(pathlib.Path(args.root))
    _print_report(data, args.top)
    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
