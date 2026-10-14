"""Verify that annotations changed nothing but comments and docstrings, and
report the annotation coverage.

Two independent checks are performed for every annotated file:

1. **Structural equality** -- the annotated file and the pristine upstream file
   must have identical ASTs once every docstring is removed.  That proves no
   statement, signature, default value or type annotation was altered, added or
   deleted.
2. **Annotation coverage** -- how many class and function definitions carry at
   least one ``【..】`` annotation block, how many annotation markers of each
   kind are present, and how the file size changed.

Usage::

    python tools/annotation_stats.py --base src/original-provided/rich \
                                     --annotated src/annotated/rich \
                                     --out docs/development/04-标注产出统计.md
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
from typing import Dict, List, Sequence, Tuple

__all__ = ["normalise", "check_file", "collect", "render_markdown", "main"]

#: Files that make up the key module (the annotated subsystem).
KEY_FILES: Tuple[str, ...] = (
    "console.py",
    "text.py",
    "segment.py",
    "style.py",
    "color.py",
    "theme.py",
    "_win32_console.py",
    "_windows_renderer.py",
    "_windows.py",
    "_fileno.py",
    "_null_file.py",
    "_ratio.py",
    "__init__.py",
    "cells.py",
    "control.py",
    "errors.py",
    "protocol.py",
    "region.py",
    "screen.py",
    "_loop.py",
    "_pick.py",
    "_timer.py",
)

#: The marker tags used by the annotation convention, in report order.
MARKERS = (
    "模块定位",
    "关键数据结构",
    "设计要点",
    "阅读建议",
    "职责",
    "关键属性",
    "不变量",
    "协作",
    "功能",
    "算法",
    "参数",
    "返回",
    "注意",
    "要点",
    "为什么",
    "易错",
    "优化",
    "兼容",
    "约束",
    "历史",
)

_MARKER_RE = re.compile(r"【([^】]+)】")


def normalise(path: pathlib.Path) -> str:
    """AST dump of ``path`` with every docstring removed.

    A docstring is the first statement of a module, class or function when it is
    a bare string constant.  Removing them makes two files with identical code
    but different documentation compare equal.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module),
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                del body[0]
    return ast.dump(tree)


def check_file(base: pathlib.Path, annotated: pathlib.Path, name: str) -> Dict[str, object]:
    """Run both checks for one file and return a result record."""
    original = base / name
    target = annotated / name
    record: Dict[str, object] = {
        "name": name,
        "exists": target.exists(),
        "structural": False,
        "orig_lines": 0,
        "ann_lines": 0,
        "defs": 0,
        "annotated_defs": 0,
        "markers": {},
    }
    if not original.exists() or not target.exists():
        return record

    record["structural"] = normalise(original) == normalise(target)

    orig_text = original.read_text(encoding="utf-8")
    ann_text = target.read_text(encoding="utf-8")
    record["orig_lines"] = len(orig_text.splitlines())
    record["ann_lines"] = len(ann_text.splitlines())

    # count definitions and how many of them are preceded by an annotation block
    ann_lines = ann_text.split("\n")
    tree = ast.parse(ann_text)
    defs: List[int] = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    annotated_defs = 0
    for lineno in defs:
        # walk back over decorators, comment lines and blank lines looking for a
        # marker; stop at the first line that is real code
        index = lineno - 2
        found = False
        while index >= 0:
            stripped = ann_lines[index].strip()
            if not stripped:
                index -= 1
                continue
            if stripped.startswith("#"):
                if _MARKER_RE.search(stripped):
                    found = True
                index -= 1
                continue
            if stripped.startswith("@"):
                index -= 1
                continue
            break
        annotated_defs += found
    record["defs"] = len(defs)
    record["annotated_defs"] = annotated_defs
    record["markers"] = {
        marker: ann_text.count(f"【{marker}】") for marker in MARKERS
    }
    return record


def collect(base: pathlib.Path, annotated: pathlib.Path, names: Sequence[str]) -> List[Dict[str, object]]:
    """Check every named file, skipping the ones that do not exist."""
    records = []
    for name in names:
        record = check_file(base, annotated, name)
        if record["exists"] or (base / name).exists():
            records.append(record)
    return records


def render_markdown(records: Sequence[Dict[str, object]], title: str) -> str:
    """Render the results as a Markdown document for the report."""
    lines: List[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(
        "> 本文件由 `tools/annotation_stats.py` 自动生成，可复现："
        "`python tools/annotation_stats.py --base src/original-provided/rich "
        "--annotated src/annotated/rich`。"
    )
    lines.append("")
    lines.append("## 1. 标注前后规模与结构校验")
    lines.append("")
    lines.append(
        "「结构一致」一列的含义：把标注后文件与上游原始文件的 AST 分别去掉所有文档字符串后"
        "完全相等，证明标注只增加了注释与文档字符串，没有改动任何可执行语句。"
    )
    lines.append("")
    lines.append("| 文件 | 原始行数 | 标注后行数 | 增幅 | 结构一致 | 定义数 | 已标注定义 | 标注覆盖率 |")
    lines.append("| --- | ---: | ---: | ---: | :---: | ---: | ---: | ---: |")

    total_orig = total_ann = total_defs = total_ann_defs = 0
    all_ok = True
    for record in records:
        orig = int(record["orig_lines"])
        ann = int(record["ann_lines"])
        defs = int(record["defs"])
        ann_defs = int(record["annotated_defs"])
        total_orig += orig
        total_ann += ann
        total_defs += defs
        total_ann_defs += ann_defs
        ok = bool(record["structural"])
        all_ok = all_ok and ok
        growth = f"{ann / orig:.2f}×" if orig else "-"
        coverage = f"{100.0 * ann_defs / defs:.0f}%" if defs else "-"
        lines.append(
            f"| `{record['name']}` | {orig} | {ann} | {growth} | "
            f"{'✅' if ok else '❌'} | {defs} | {ann_defs} | {coverage} |"
        )
    growth = f"{total_ann / total_orig:.2f}×" if total_orig else "-"
    coverage = f"{100.0 * total_ann_defs / total_defs:.0f}%" if total_defs else "-"
    lines.append(
        f"| **合计** | **{total_orig}** | **{total_ann}** | **{growth}** | "
        f"{'**全部通过**' if all_ok else '**存在不一致**'} | **{total_defs}** | "
        f"**{total_ann_defs}** | **{coverage}** |"
    )
    lines.append("")

    lines.append("## 2. 标注标记使用统计")
    lines.append("")
    totals: Dict[str, int] = {marker: 0 for marker in MARKERS}
    for record in records:
        for marker, count in record["markers"].items():  # type: ignore[union-attr]
            totals[marker] += int(count)
    lines.append("| 标记 | 出现次数 | 用途 |")
    lines.append("| --- | ---: | --- |")
    usage = {
        "模块定位": "文件在架构中的位置",
        "关键数据结构": "核心结构与不变量",
        "设计要点": "架构级设计动机",
        "阅读建议": "推荐阅读顺序",
        "职责": "类的职责",
        "关键属性": "类的重要属性",
        "不变量": "类必须成立的条件",
        "协作": "类的协作对象与接口",
        "功能": "方法做什么",
        "算法": "方法使用的算法",
        "参数": "不易理解的参数",
        "返回": "返回值与特殊情况",
        "注意": "陷阱与边界条件",
        "要点": "实现关键一步",
        "为什么": "设计动机",
        "易错": "容易改错的地方",
        "优化": "性能考虑",
        "兼容": "平台/版本差异",
        "约束": "跨文件必须一致的约定",
        "历史": "历史与演进说明",
    }
    for marker in MARKERS:
        lines.append(f"| 【{marker}】 | {totals[marker]} | {usage.get(marker, '')} |")
    lines.append(f"| **合计** | **{sum(totals.values())}** | |")
    lines.append("")

    lines.append("## 3. 结论")
    lines.append("")
    if all_ok:
        lines.append(
            f"- 结构一致性校验 **全部通过**：{len(records)} 个文件在去掉文档字符串后与上游 AST 完全相等。"
        )
    else:
        lines.append("- ⚠️ 存在结构不一致的文件，需要人工核查（见第 1 节）。")

    # Split the summary by scope: the annotated subsystem versus the pristine
    # files that only exist to make the annotated package importable.
    scope = [record for record in records if record["name"] in KEY_FILES]
    scope_defs = sum(int(record["defs"]) for record in scope)
    scope_ann = sum(int(record["annotated_defs"]) for record in scope)
    scope_orig = sum(int(record["orig_lines"]) for record in scope)
    scope_new = sum(int(record["ann_lines"]) for record in scope)
    lines.append("")
    lines.append("## 4. 按范围汇总")
    lines.append("")
    lines.append("| 范围 | 文件数 | 原始行数 | 标注后行数 | 增幅 | 定义数 | 已标注定义 | 覆盖率 |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    lines.append(
        f"| 标注范围（关键模块子系统） | {len(scope)} | {scope_orig} | {scope_new} | "
        f"{scope_new / scope_orig:.2f}× | {scope_defs} | {scope_ann} | "
        f"{100.0 * scope_ann / max(scope_defs, 1):.0f}% |"
    )
    lines.append(
        f"| 全树（含为使包可导入而保留的未标注模块） | {len(records)} | {total_orig} | "
        f"{total_ann} | {total_ann / total_orig:.2f}× | {total_defs} | {total_ann_defs} | "
        f"{100.0 * total_ann_defs / max(total_defs, 1):.0f}% |"
    )
    lines.append("")
    lines.append(
        "本次实践标注的是「标注范围」这一行：终端渲染管线子系统。"
        "其余文件是上游原样拷贝，目的是让标注后的包可以独立导入与测试（否则 "
        "``rich/__init__.py`` 会因为缺少 ``_extension`` 等模块而无法加载）；"
        "它们同样通过了结构一致性校验，说明没有被意外改动。"
    )
    lines.append("")
    lines.append(
        f"- 标注范围内的模块从 {scope_orig} 行增至 {scope_new} 行"
        f"（{scope_new / scope_orig:.2f} 倍），覆盖 {scope_ann}/{scope_defs} 个类与函数定义"
        f"（{100.0 * scope_ann / max(scope_defs, 1):.0f}%）。"
    )
    lines.append(
        f"- 全部 {len(records)} 个文件共使用 {sum(totals.values())} 处标注标记。"
    )
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="src/original-provided/rich")
    parser.add_argument("--annotated", default="src/annotated/rich")
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--title", default="Rich 关键模块代码标注产出统计"
    )
    parser.add_argument(
        "--files",
        default=None,
        help="comma separated file names; defaults to every file of the original tree",
    )
    args = parser.parse_args(argv)

    if args.files:
        names = tuple(part.strip() for part in args.files.split(",") if part.strip())
    else:
        # Check the whole tree: the annotated copy is complete, so every file can
        # be compared and any accidental edit anywhere is caught.
        base_dir = pathlib.Path(args.base)
        names = tuple(
            sorted(
                str(path.relative_to(base_dir)).replace("\\", "/")
                for path in base_dir.rglob("*.py")
            )
        )
    records = collect(pathlib.Path(args.base), pathlib.Path(args.annotated), names)
    markdown = render_markdown(records, args.title)

    for record in records:
        flag = "OK" if record["structural"] else ("MISSING" if not record["exists"] else "MISMATCH")
        print(
            f"{record['name']:24s} {flag:9s} "
            f"{record['orig_lines']:5d} -> {record['ann_lines']:5d} lines, "
            f"{record['annotated_defs']}/{record['defs']} defs annotated"
        )
    if args.out:
        pathlib.Path(args.out).write_text(markdown, encoding="utf-8", newline="\n")
        print(f"\nwrote {args.out}")
    return 0 if all(record["structural"] for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
