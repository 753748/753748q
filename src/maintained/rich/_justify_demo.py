"""Command line demo for Rich's paragraph justification feature.

Run with the maintained source on the path::

    python -m rich._justify_demo demo     --width 76 --mode full
    python -m rich._justify_demo compare  --width 60
    python -m rich._justify_demo hyphenate hyphenation justification algorithm
    python -m rich._justify_demo patterns
    python -m rich._justify_demo bench    --words 4000

The ``compare`` command is the interesting one for the report: it sets the same
paragraph with upstream ``Text.wrap`` (greedy first fit) and with this module
(Knuth-Plass total fit) and prints the raggedness of both, so the improvement is
measurable rather than asserted.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import textwrap
import time
from typing import List, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ._hyphenation import Hyphenator, load_tex_patterns
from ._hyphenation_patterns import CORPUS_SIZE, TRAINING_RECALL
from ._justify import JustifyOptions, justify, render_lines
from ._layout import LayoutOptions, layout

__all__ = ["main", "DEMO_TEXT", "SAMPLES"]

DEMO_TEXT = (
    "Rich is a Python library for rich text and beautiful formatting in the "
    "terminal. The justification engine added by this practice implements "
    "optimal paragraph line breaking with hyphenation, which the upstream "
    "greedy wrapper cannot do: every break point is chosen so that the total "
    "badness of all the lines of a paragraph is minimised, instead of filling "
    "each line as far as it will go and moving on. Typography in a fixed "
    "width terminal is a surprisingly hard problem, because a terminal cannot "
    "stretch the glyphs themselves."
)

CJK_TEXT = (
    "富文本渲染库需要在终端中以单元格为单位进行排版，"
    "因为中文字符的显示宽度与拉丁字母不同。"
    "这个模块实现了最优断行算法，并遵循基本的中日韩禁则处理规则，"
    "避免标点符号出现在行首或行尾。"
)

SAMPLES = {
    "english": DEMO_TEXT,
    "cjk": CJK_TEXT,
    "long-words": (
        "The implementation documentation configuration parameter "
        "internationalization compatibility interoperability "
        "counterproductive indistinguishable."
    ),
    "tech": (
        "Embedding a modern terminal renderer requires careful attention to the "
        "characterization of wide glyphs, grapheme clusters and combining marks; "
        "misunderstanding these details produces misaligned tables and broken "
        "progress bars. The implementation therefore measures every string in "
        "terminal cells rather than in characters, and the justification engine "
        "reuses that measurement so that optimal line breaking agrees with what "
        "the terminal will actually draw."
    ),
    "lorem": (
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod "
        "tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim "
        "veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea "
        "commodo consequat. Duis aute irure dolor in reprehenderit in voluptate "
        "velit esse cillum dolore eu fugiat nulla pariatur."
    ),
}


def _raggedness(widths: Sequence[int]) -> float:
    """Standard deviation of the line lengths.

    This is the usual numeric proxy for how ragged a paragraph looks: a fully
    justified paragraph has raggedness 0 apart from its last line.
    """
    body = list(widths)[:-1] or list(widths)
    if len(body) < 2:
        return 0.0
    return statistics.pstdev(body)


def _greedy_widths(text: str, width: int) -> List[int]:
    """Line widths produced by upstream greedy first-fit wrapping.

    ``Text.wrap`` only takes effect when a Console renders the object, so the
    measurement goes through :meth:`rich.console.Console.render_lines`, which is
    exactly the path ``Console.print`` uses.
    """
    console = Console(width=width, force_terminal=False)
    rendered = console.render_lines(Text(text), pad=False)
    plain = ["".join(segment.text for segment in line) for line in rendered]
    return [len(line.rstrip()) for line in plain if line.strip()]


def cmd_demo(args: argparse.Namespace) -> int:
    console = Console(width=max(args.width + 4, 40), highlight=False, emoji=False)
    text = SAMPLES.get(args.sample, args.sample)
    console.rule(f"[bold]justify mode={args.mode} width={args.width} sample={args.sample}")
    lines = justify(
        text,
        width=args.width,
        mode=args.mode,
        hyphenate=not args.no_hyphenate,
        from_markup=False,
    )
    for line in lines:
        console.print(line.text, no_wrap=True, overflow="ignore", crop=False, soft_wrap=True)
    console.rule("metrics")
    table = Table(box=None, padding=(0, 2))
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("lines", str(len(lines)))
    table.add_row("max width", str(max((line.width for line in lines), default=0)))
    table.add_row("hyphenated lines", str(sum(1 for line in lines if line.hyphenated)))
    table.add_row("overfull lines", str(sum(1 for line in lines if line.overfull)))
    table.add_row("raggedness (stdev)", f"{_raggedness([ln.width for ln in lines]):.2f}")
    console.print(table)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    console = Console(width=max(args.width + 6, 50), highlight=False, emoji=False)
    text = SAMPLES.get(args.sample, args.sample)
    options = LayoutOptions(width=args.width)

    start = time.perf_counter()
    greedy = _greedy_widths(text, args.width)
    greedy_time = time.perf_counter() - start

    start = time.perf_counter()
    plan = layout(
        Text(text),
        options,
        Hyphenator(min_word_length=args.min_hyphen_length) if not args.no_hyphenate else None,
    )
    knuth_time = time.perf_counter() - start

    console.rule(f"[bold]upstream Text.wrap (greedy first fit), width={args.width}")
    for width in greedy:
        console.print(" " + "." * width, highlight=False, style="yellow")
    console.rule("[bold]Knuth-Plass total fit (this feature)")
    for line in plan.lines:
        console.print(
            " " + "." * line.natural,
            highlight=False,
            style="cyan",
        )

    table = Table(title="comparison", box=None)
    for column in ("metric", "greedy", "knuth-plass"):
        table.add_column(column, justify="right")
    table.add_row("lines", str(len(greedy)), str(len(plan.lines)))
    table.add_row(
        "raggedness (stdev of full lines)",
        f"{_raggedness(greedy):.2f}",
        f"{_raggedness([ln.natural for ln in plan.lines]):.2f}",
    )
    table.add_row("worst shortfall", str(args.width - min(greedy)), str(max(0, args.width - min(ln.natural for ln in plan.lines))))
    table.add_row("total demerits", "n/a", f"{plan.total_demerits:.0f}")
    table.add_row("states considered", "n/a", str(plan.states))
    table.add_row("time (ms)", f"{greedy_time * 1000:.2f}", f"{knuth_time * 1000:.2f}")
    console.print(table)
    return 0


def cmd_hyphenate(args: argparse.Namespace) -> int:
    console = Console(highlight=False, emoji=False)
    hyphenator = Hyphenator(approximate=args.approximate)
    table = Table(title="hyphenation", box=None)
    table.add_column("word")
    table.add_column("offsets", justify="right")
    table.add_column("split")
    table.add_column("source")
    for word in args.words:
        offsets = hyphenator.hyphenate(word)
        source = "dictionary" if hyphenator.known(word) else "patterns/unknown"
        table.add_row(
            word,
            ",".join(str(offset) for offset in offsets) or "-",
            " ".join(hyphenator.break_word(word)),
            source,
        )
    console.print(table)
    return 0


def cmd_patterns(args: argparse.Namespace) -> int:
    console = Console(highlight=False, emoji=False)
    console.print(
        Panel.fit(
            f"bundled dictionary entries : {CORPUS_SIZE}\n"
            f"derived pattern accuracy   : {TRAINING_RECALL * 100:.1f}% on its own corpus\n"
            f"approximate mode enabled   : {args.approximate}",
            title="hyphenation data",
        )
    )
    hyphenator = Hyphenator(approximate=args.approximate)
    console.print("patterns loaded:", hyphenator.pattern_count)
    console.print(
        "load a real TeX table with: Hyphenator(patterns=load_tex_patterns(path)[0])",
        style="dim",
    )
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    console = Console(highlight=False, emoji=False)
    paragraph = (DEMO_TEXT + " ") * max(1, args.words // 90)
    table = Table(title=f"layout benchmark ({args.words} words)", box=None)
    table.add_column("width", justify="right")
    table.add_column("lines", justify="right")
    table.add_column("states", justify="right")
    table.add_column("ms", justify="right")
    table.add_column("demerits", justify="right")
    hyphenator = Hyphenator()
    for width in (40, 60, 80, 100, 120):
        start = time.perf_counter()
        plan = layout(Text(paragraph), LayoutOptions(width=width), hyphenator)
        elapsed = (time.perf_counter() - start) * 1000
        table.add_row(
            str(width),
            str(len(plan.lines)),
            str(plan.states),
            f"{elapsed:.1f}",
            f"{plan.total_demerits:.0f}",
        )
    console.print(table)
    return 0


def cmd_svg(args: argparse.Namespace) -> int:
    """Write an SVG rendering of the justified paragraph for the report."""
    from rich.terminal_theme import MONOKAI

    console = Console(
        width=args.width + 2,
        record=True,
        force_terminal=True,
        highlight=False,
        emoji=False,
        color_system="truecolor",
    )
    console.print("[bold]Optimal paragraph justification with hyphenation[/]")
    console.print("[dim]" + "-" * (args.width + 1) + "[/]")
    console.print_justified(DEMO_TEXT, width=args.width, mode=args.mode)
    console.print("[dim]" + "-" * (args.width + 1) + "[/]")
    console.save_svg(args.out, title="rich justification")
    print(f"wrote {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rich._justify_demo", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="print a justified paragraph")
    demo.add_argument("--width", type=int, default=76)
    demo.add_argument("--mode", default="full", choices=("left", "right", "center", "full"))
    demo.add_argument("--sample", default="english", help="english, cjk, long-words or literal text")
    demo.add_argument("--no-hyphenate", action="store_true")
    demo.set_defaults(func=cmd_demo)

    compare = sub.add_parser("compare", help="compare greedy wrapping with total fit")
    compare.add_argument("--width", type=int, default=60)
    compare.add_argument("--sample", default="english")
    compare.add_argument("--no-hyphenate", action="store_true")
    compare.add_argument("--min-hyphen-length", type=int, default=5)
    compare.set_defaults(func=cmd_compare)

    hyphenate = sub.add_parser("hyphenate", help="show hyphenation of words")
    hyphenate.add_argument("words", nargs="+")
    hyphenate.add_argument("--approximate", action="store_true")
    hyphenate.set_defaults(func=cmd_hyphenate)

    patterns = sub.add_parser("patterns", help="show the hyphenation data provenance")
    patterns.add_argument("--approximate", action="store_true")
    patterns.set_defaults(func=cmd_patterns)

    bench = sub.add_parser("bench", help="benchmark the layout engine")
    bench.add_argument("--words", type=int, default=900)
    bench.set_defaults(func=cmd_bench)

    svg = sub.add_parser("svg", help="write an SVG recording of the output")
    svg.add_argument("--out", default="justify.svg")
    svg.add_argument("--width", type=int, default=76)
    svg.add_argument("--mode", default="full", choices=("left", "right", "center", "full"))
    svg.set_defaults(func=cmd_svg)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
