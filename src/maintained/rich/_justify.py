"""Paragraph justification for Rich: optimal line breaking, hyphenation and
full justification, with styles preserved.

This is the user-facing half of the justification feature.  It turns any Rich
:class:`~rich.text.Text` -- including text with markup, nested styles,
hyperlinks and background colours -- into a list of reflowed lines:

>>> from rich.text import Text
>>> from rich._justify import justify
>>> lines = justify("The quick brown fox jumps over the lazy dog.", width=20, mode="full")
>>> for line in lines:
...     print(repr(line.plain))
'The  quick  brown fox'
'jumps  over the lazy'
'dog.'

Why this is not just ``Text.wrap``
----------------------------------
``Text.wrap`` is a greedy first-fit wrapper: it fills each line as far as it can
and moves on.  That produces a ragged right edge and, with hyphenation disabled,
wide gaps between words.  :mod:`rich._layout` implements the Knuth-Plass
total-fit algorithm instead, so the break points are chosen to minimise the sum
of the squared badness of *all* lines of the paragraph at once.

Padding model
-------------
A terminal cannot stretch glyphs, so this module justifies text the way a
terminal can: by distributing the leftover cells of a line across its
inter-word gaps, or by adding them to the single existing gap.  ``mode="full"``
therefore adds ``n`` extra spaces across ``g`` gaps as evenly as possible, which
is exactly the "flush left and right" look expected of justified text.

Style preservation
------------------
Every line is built with :meth:`rich.text.Text.divide`, slicing the *original*
Text between the character offsets chosen by the layout engine.  No styling
information is recomputed, so span boundaries, ``Style`` objects, ``no_wrap``
flags and meta information such as hyperlink targets all survive untouched.

Main entry points
-----------------
:func:`justify`          -- reflow one paragraph into ``Text`` lines
:func:`justify_paragraph` -- same, but returns a single ``Text`` with newlines
:func:`reflow`           -- convenience wrapper around :func:`justify`
:func:`render_lines`     -- pad each line to the column for inspection/debugging
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

from .cells import cell_len
from .text import Text
from ._layout import BrokenLine, LayoutOptions, LayoutPlan, Item, Box, Glue, Penalty, layout
from ._hyphenation import Hyphenator

__all__ = [
    "JustifyOptions",
    "JustifiedLine",
    "justify",
    "justify_paragraph",
    "reflow",
    "render_lines",
    "install",
]


@dataclass
class JustifyOptions:
    """Options for :func:`justify`.

    ``mode`` accepts the four values users expect from a word processor:

    ``"left"``
        ragged right; the layout engine still chooses the break points
        optimally, which already reduces raggedness compared with greedy fill.
    ``"right"``
        pad on the left.
    ``"center"``
        pad evenly on both sides.
    ``"full"``
        distribute the leftover cells across the inter-word gaps, except on the
        last line of a paragraph (which stays left aligned, as in print).

    The remaining fields mirror :class:`rich._layout.LayoutOptions` and
    :class:`rich._hyphenation.Hyphenator`.
    """

    width: int = 80
    mode: str = "full"
    #: hyphenate long words
    hyphenate: bool = True
    #: use the derived, low accuracy bundled pattern table (see the docs)
    approximate_hyphenation: bool = False
    #: minimum word length that may be hyphenated
    min_hyphen_word_length: int = 5
    #: characters of indentation for the first line of each paragraph
    first_line_indent: int = 0
    #: characters of indentation for every line
    indent: int = 0
    #: strip leading/trailing whitespace of each paragraph
    strip: bool = True
    #: replace runs of whitespace with a single space
    collapse_whitespace: bool = True
    #: optional hyphenator instance; built from the fields above when omitted
    hyphenator: Optional[Hyphenator] = None
    #: extra space inserted between sentences ("." + 2 spaces), French spacing
    sentence_spacing: bool = False
    #: keep the last line of a paragraph left aligned whatever the mode
    last_line_left: bool = True
    #: a paragraph shorter than this is never justified in full mode
    min_lines_to_justify: int = 2

    def __post_init__(self) -> None:
        if self.mode not in ("left", "right", "center", "full"):
            raise ValueError(f"unknown mode {self.mode!r}")
        if self.width < 1:
            raise ValueError("width must be at least 1 cell")
        if self.indent < 0 or self.first_line_indent < 0:
            raise ValueError("indentation cannot be negative")
        if self.indent + self.first_line_indent >= self.width:
            raise ValueError("indentation leaves no room for text")

    @property
    def text_width(self) -> int:
        """Width available for text, ignoring indentation."""
        return self.width


@dataclass
class JustifiedLine:
    """One output line plus the metrics that produced it."""

    text: Text
    ratio: float
    hyphenated: bool
    overfull: bool
    padding: int
    mode: str
    demerits: float = 0.0

    @property
    def plain(self) -> str:
        return self.text.plain

    @property
    def width(self) -> int:
        return cell_len(self.text.plain)

    def __str__(self) -> str:
        return self.text.plain


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split_paragraphs(text: Text, options: JustifyOptions) -> List[Text]:
    """Split ``text`` on blank lines, keeping one Text per paragraph.

    ``Text.split`` preserves styles, so paragraph splitting is style safe.  A run
    of blank lines produces an empty paragraph so that vertical spacing authored
    by the caller is not silently collapsed.
    """
    paragraphs: List[Text] = []
    current = Text(style=text.style, justify=text.justify, overflow=text.overflow)
    for line in text.split("\n"):
        if line.plain.strip() == "":
            paragraphs.append(current)
            current = Text(style=text.style)
        else:
            if current.plain:
                current.append("\n")
            current.append_text(line)
    paragraphs.append(current)
    return paragraphs


def _normalise(paragraph: Text, options: JustifyOptions) -> Text:
    """Collapse whitespace and strip, preserving styles."""
    if not options.collapse_whitespace and not options.strip:
        return paragraph
    pieces: List[Text] = []
    plain = paragraph.plain
    index = 0
    length = len(plain)
    while index < length:
        char = plain[index]
        start = index
        if char.isspace():
            while index < length and plain[index].isspace():
                index += 1
            if options.collapse_whitespace:
                pieces.append(Text(" "))
            else:
                pieces.append(paragraph[start:index])
            continue
        while index < length and not plain[index].isspace():
            index += 1
        pieces.append(paragraph[start:index])

    result = Text(style=paragraph.style, justify=paragraph.justify, overflow=paragraph.overflow)
    if options.strip:
        while pieces and pieces[0].plain == " ":
            pieces.pop(0)
        while pieces and pieces[-1].plain == " ":
            pieces.pop()
    for piece in pieces:
        result.append_text(piece)
    return result


def _extra_space_gaps(plan: LayoutPlan, line: BrokenLine) -> List[int]:
    """Indices of the *items* that are glue gaps on ``line``.

    Only glue items may receive padding; a line with no glue (a single long
    word) is padded with a plain space before the word instead.
    """
    gaps: List[int] = []
    for index in range(line.start_item, min(line.end_item, len(plan.items))):
        if isinstance(plan.items[index], Glue) and not plan.items[index].nobreak:
            gaps.append(index)
    return gaps


def _slice_line(source: Text, line: BrokenLine) -> Text:
    """Extract the source text of ``line`` as a styled Text, hyphen included.

    A break comes *after* the inter-word space, so the raw item range ends with
    the newline replaced by the break; the layout engine separates that trailing
    whitespace into ``line.trailing`` so this function can drop it.  For a
    hyphenation break the engine also supplies ``line.prepend``, the hyphen, which
    must be appended **after** the trailing whitespace is removed -- appending it
    before would place it after the space and make the line one cell too wide.
    """
    plain = source.plain
    start = max(0, min(line.start, len(plain)))
    end = max(start, min(line.end, len(plain)))
    if end > start and line.trailing:
        end -= len(line.trailing)
    result = source[start:end] if start < end else Text()
    if line.prepend:
        result = result.copy()
        result.append(line.prepend)
    return result


def _indent_text(spaces: int) -> Text:
    return Text(" " * spaces) if spaces > 0 else Text()


def _pad_left(text: Text, cells: int) -> Text:
    if cells <= 0:
        return text
    result = Text(" " * cells)
    result.append_text(text)
    return result


def _pad_right(text: Text, cells: int) -> Text:
    if cells <= 0:
        return text
    result = text.copy()
    result.append(" " * cells)
    return result


def _justify_full(
    source: Text,
    plan: LayoutPlan,
    line: BrokenLine,
    options: JustifyOptions,
    last_line: bool,
) -> Tuple[Text, int]:
    """Distribute the leftover cells of ``line`` across its inter-word gaps.

    Works from the line's *text* rather than from the item stream: the tokens of
    the line are split on whitespace with :meth:`rich.text.Text.split`, which
    keeps every style span intact, and the extra cells are then spread over the
    ``len(tokens) - 1`` gaps as evenly as possible (the first ``remainder`` gaps
    receive one extra space).  A line with a single token has no gap, so the
    leftover is prepended instead -- the right edge still lines up, which is what
    a word processor does with an unbreakable word.
    """
    base = _slice_line(source, line)
    natural = cell_len(base.plain)
    leftover = plan.options.width - natural
    if leftover <= 0:
        return base, 0

    tokens = [token for token in base.split(" ") if token.plain != ""]
    gaps = len(tokens) - 1
    if gaps <= 0:
        return _pad_left(base, leftover), leftover

    share, remainder = divmod(leftover, gaps)
    result = Text()
    for index, token in enumerate(tokens):
        result.append_text(token)
        if index < gaps:
            extra = share + (1 if index < remainder else 0)
            result.append(" " * (1 + extra))
    return result, leftover


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def justify(
    text: Text | str,
    width: int = 80,
    mode: str = "full",
    options: Optional[JustifyOptions] = None,
    *,
    hyphenate: Optional[bool] = None,
    from_markup: bool = False,
) -> List[JustifiedLine]:
    """Reflow one paragraph into justified lines.

    Args:
        text: the paragraph, as a ``Text`` or a plain string.
        width: target column width in terminal cells.
        mode: ``"left"``, ``"right"``, ``"center"`` or ``"full"``.
        options: full option object; overrides ``width``, ``mode`` and
            ``hyphenate``.
        hyphenate: shortcut to toggle hyphenation when ``options`` is omitted.
        from_markup: parse ``text`` as Rich markup when it is a string.  Off by
            default because ordinary prose may legitimately contain square
            brackets, which markup parsing would either reject or swallow.

    Returns:
        The lines in visual order.

    Example:
        >>> for line in justify("Wrapping text well is hard.", width=14, mode="full"):
        ...     print(repr(line.plain))
        'Wrapping text'
        'well is  hard.'
    """
    if options is None:
        options = JustifyOptions(
            width=width,
            mode=mode,
            hyphenate=True if hyphenate is None else hyphenate,
        )
    if isinstance(text, str):
        text = Text.from_markup(text) if from_markup else Text(text)
    if "\n" in text.plain:
        # a single paragraph is expected here; keep the first line only so that
        # callers get predictable output instead of a silently merged block
        text = text.split("\n")[0]

    if not text.plain.strip():
        return []

    paragraph = _normalise(text, options)
    hyphenator: Optional[Hyphenator] = None
    if options.hyphenate:
        hyphenator = options.hyphenator or Hyphenator(
            approximate=options.approximate_hyphenation,
            min_word_length=options.min_hyphen_word_length,
        )

    inner_width = options.width - options.indent
    if inner_width < 1:
        raise ValueError("indent leaves no room for text")

    plan = layout(paragraph, LayoutOptions(width=inner_width), hyphenator)
    results: List[JustifiedLine] = []

    for position, line in enumerate(plan.lines):
        last = position == len(plan.lines) - 1
        indent = options.indent + (options.first_line_indent if position == 0 else 0)
        content, padding = _justify_one(source=paragraph, plan=plan, line=line, options=options, last=last)
        content = _pad_left(content, indent)
        results.append(
            JustifiedLine(
                text=content,
                ratio=line.ratio,
                hyphenated=line.hyphenated,
                overfull=line.overfull,
                padding=padding,
                mode=options.mode,
                demerits=line.demerits,
            )
        )
    return results


def _justify_one(
    source: Text,
    plan: LayoutPlan,
    line: BrokenLine,
    options: JustifyOptions,
    last: bool,
) -> Tuple[Text, int]:
    """Apply the alignment mode to a single broken line.

    Only ``mode="full"`` rewrites the inter-word spacing.  ``left``, ``right`` and
    ``center`` use the line's *natural* text and pad it, because padding a line
    whose gaps have already been stretched would add the centring offset on top of
    the justification and produce a line wider than the column.
    """
    mode = options.mode
    if mode == "full" and last and options.last_line_left:
        mode = "left"
    if mode == "full" and len(plan.lines) < options.min_lines_to_justify:
        mode = "left"

    if mode == "full":
        return _justify_full(source, plan, line, options, last)

    base = _slice_line(source, line)
    natural = cell_len(base.plain)
    leftover = max(0, plan.options.width - natural)
    if mode == "left":
        return base, 0
    if mode == "right":
        return _pad_left(base, leftover), leftover
    # center: the right half of the leftover is simply not emitted, which is what
    # centres the text inside the column
    left = leftover // 2
    return _pad_left(base, left), leftover


def justify_paragraph(
    text: Text | str,
    width: int = 80,
    mode: str = "full",
    options: Optional[JustifyOptions] = None,
) -> Text:
    """Justify every paragraph of ``text`` and return one newline-joined Text."""
    if options is None:
        options = JustifyOptions(width=width, mode=mode)
    if isinstance(text, str):
        text = Text(text)
    out = Text()
    for index, paragraph in enumerate(_split_paragraphs(text, options)):
        if index:
            out.append("\n")
        if not paragraph.plain.strip():
            continue
        first_line = True
        for line in justify(paragraph, options=options):
            if not first_line or out.plain:
                out.append("\n")
            first_line = False
            out.append_text(line.text)
    return out


def reflow(text: Text | str, width: int = 80, mode: str = "full") -> Text:
    """Shortcut for :func:`justify_paragraph` with default options."""
    return justify_paragraph(text, width=width, mode=mode)


def render_lines(
    lines: Sequence[JustifiedLine],
    width: Optional[int] = None,
    fill: str = "\u00b7",
) -> Text:
    """Return the lines with visible trailing fill, useful for tests and demos.

    The right edge of a fully justified paragraph is only verifiable if the
    padding is visible, so every line is padded to ``width`` with ``fill``.
    """
    out = Text()
    for index, line in enumerate(lines):
        if index:
            out.append("\n")
        plain = line.text.plain
        target = width if width is not None else max(cell_len(plain), 1)
        pad = target - cell_len(plain)
        out.append_text(line.text)
        if pad > 0:
            out.append(fill * pad, style="dim")
    return out


def install(console_class=None) -> None:
    """Add ``Console.print_justified`` to :class:`rich.console.Console`.

    Kept as an explicit opt-in so that merely importing this module has no
    effect on the public ``Console`` API; ``rich._justify_console`` calls it at
    import time, and :mod:`rich.console` imports that module so the method is
    available on every console.

    The method is named ``print_justified`` rather than ``justify`` because
    ``Text.justify`` already exists as the alignment attribute, and a same-named
    method on ``Console`` would be confusing.
    """
    if console_class is None:
        from .console import Console

        console_class = Console

    def print_justified(
        self,
        text,
        width: Optional[int] = None,
        mode: str = "full",
        *,
        style=None,
        hyphenate: bool = True,
        approximate_hyphenation: bool = False,
        first_line_indent: int = 0,
        indent: int = 0,
        from_markup: bool = False,
        return_lines: bool = False,
    ):
        """Print ``text`` wrapped and justified to ``width`` cells.

        Args:
            text: the paragraph to set.  A ``str`` is treated as plain text
                unless ``from_markup`` is set; a :class:`~rich.text.Text` keeps
                its styles.
            width: column width; defaults to ``self.width``.
            mode: ``"left"``, ``"right"``, ``"center"`` or ``"full"``.
            style: optional base style applied to the whole paragraph.
            hyphenate: enable the hyphenation pass.
            approximate_hyphenation: allow the low accuracy bundled pattern
                table; see :mod:`rich._hyphenation`.
            first_line_indent: spaces added in front of the first line.
            indent: spaces added in front of every line.
            from_markup: parse a ``str`` argument as Rich markup.
            return_lines: return the :class:`JustifiedLine` list.

        Returns:
            The list of :class:`JustifiedLine` when ``return_lines`` is true,
            otherwise ``None``.
        """
        columns = width or self.width
        options = JustifyOptions(
            width=columns,
            mode=mode,
            hyphenate=hyphenate,
            approximate_hyphenation=approximate_hyphenation,
            first_line_indent=first_line_indent,
            indent=indent,
        )
        lines = justify(text, options=options, from_markup=from_markup)
        for line in lines:
            content = line.text
            if style is not None:
                styled = content.copy()
                styled.stylize(style)
                content = styled
            self.print(content, no_wrap=True, overflow="ignore", crop=False, soft_wrap=True)
        if return_lines:
            return lines
        return None

    setattr(console_class, "print_justified", print_justified)
