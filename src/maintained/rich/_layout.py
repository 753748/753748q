"""Optimal paragraph line breaking for Rich.

This module implements the box/glue/penalty model and the Knuth-Plass total-fit
line breaking algorithm, which is the piece upstream Rich does not have: Rich's
``Text.wrap`` is a greedy first-fit wrapper, so every line is sized without
looking at what follows and the right margin ends up visibly ragged.

The public entry point is :func:`layout`:

>>> from rich.text import Text
>>> from rich._layout import LayoutOptions, layout
>>> plan = layout(Text(open("README.md").read()), LayoutOptions(width=60))
>>> [line.start for line in plan.lines][:3]
[0, 54, 112]

Everything else in this module is machinery:

``Item``      tagged union of ``box`` / ``glue`` / ``penalty``
``to_items``  text -> item stream, the tokenizer
``break_lines``  the actual dynamic program
``LayoutPlan``   the result, in *character offsets* into the source text

Keeping the result in character offsets instead of materialised strings is what
lets :mod:`rich._justify` rebuild each line with :meth:`rich.text.Text.divide`,
so nested styles, hyperlinks and background colours survive line breaking
without this module knowing anything about styling.

The algorithm
-------------
A paragraph is a sequence of *items*.  A **box** is unbreakable content of a
fixed width (a word, a wide CJK character, an image placeholder).  A **glue** is
flexible space with a natural width plus a stretch and a shrink amount (the
space between words).  A **penalty** marks a legal break with a cost, and a
penalty of ``-inf`` forces a break (a hard newline).

For a candidate line from item ``i`` to item ``j`` the *adjustment ratio* is

    r = (width - natural_width) / stretch      when the line must stretch
    r = (width - natural_width) / shrink       when the line must shrink

``r < -1`` means the line cannot shrink enough, i.e. it is overfull, which is
only allowed at a forced break.  Otherwise the demerits of the line are the
classic sum of squares::

    d = (line_penalty + |r|)^2                    badness
      + (if the break is flagged) penalty^2
      + flagged_penalty  when two flagged breaks are adjacent
      + fitness_penalty  when consecutive lines jump two fitness classes

and the total demerits of a paragraph is the sum over its lines.  ``break_lines``
minimises that sum with a forward dynamic program over break candidates:
``best[j] = min(best[i] + demerits(i, j))`` for every feasible ``i``.  The
straightforward form is O(n^2); the ``line width / average item width`` bound on
how many items fit on a line is what keeps it fast in practice.

Complexity and limits
---------------------
* ``max_items_per_line`` (default 512) caps the inner loop, so a pathological
  paragraph degrades gracefully instead of hanging.
* Infeasible states are pruned; if the paragraph has no feasible solution at all
  (a single word wider than the column) the algorithm falls back to a greedy
  first-fit pass rather than failing, and marks the line ``overfull``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .cells import cell_len
from .text import Text

__all__ = [
    "Item",
    "Box",
    "Glue",
    "Penalty",
    "LayoutOptions",
    "BrokenLine",
    "LayoutPlan",
    "layout",
    "to_items",
    "break_lines",
    "INFINITE_PENALTY",
    "FORCED_BREAK",
    "CJKClass",
    "classify",
]

#: A penalty this large or larger means "do not break here".
INFINITE_PENALTY = 10000

#: A penalty this small or smaller means "you must break here".
FORCED_BREAK = -10000

#: Upper bound on a line's badness, from Knuth's algorithm.  Capping is what
#: keeps one hopeless line from outweighing the rest of the paragraph.
BADNESS_CAP = 10000.0

#: Sentinel ``_State.item`` marking the start of a paragraph segment, i.e. the
#: position immediately after a forced break.  It is not a real item index, so
#: :func:`_line_start_item` maps it back to ``0``; without the sentinel the
#: boundary state would share its ``item`` with the state that closed the
#: preceding line and the backtrack would emit a phantom empty line.
SEGMENT_START = -1


# ---------------------------------------------------------------------------
# Character classes
# ---------------------------------------------------------------------------

class CJKClass:
    """Line breaking classes for East Asian text (UAX #14, simplified).

    :mod:`rich._layout` only needs three behaviours: which characters may start
    a line, which may end one, and which may never be separated from the
    character before them.  That covers the kinsoku rules that matter for
    terminal output without shipping the whole Unicode line breaking table.
    """

    #: Characters that may not appear at the *start* of a line.  Closing
    #: punctuation and small kana: a line ending "「" or starting "」" is wrong.
    NO_START = frozenset(
        "、。，．：；！？）］｝〉》」』】〙〗〟’”｠»"
        "ヽヾーァィゥェォッャュョヮヵヶぁぃぅぇぉっゃゅょゎゕゖ"
        "!%),.:;?]}¢°·’”″℃"
    )

    #: Characters that may not appear at the *end* of a line.  Opening
    #: punctuation: a line ending with "「" is wrong.
    NO_END = frozenset(
        "（［｛〈《「『【〘〖〝‘“｟«$([{£¥·‘“"
    )

    #: Characters that may not be separated from the preceding character, such
    #: as a combining mark or a unit symbol.
    NO_BREAK_AFTER = frozenset(
        "ー々〆〤"          # iteration marks and the prolonged sound mark
        "°′″%‰"            # units that bind to their number
    )

    #: Full-width space: glue, never a box.
    SPACE = "\u3000"


def _is_wide(code: int) -> bool:
    """True for characters that occupy two terminal cells in East Asian width."""
    import unicodedata

    return unicodedata.east_asian_width(chr(code)) in ("W", "F")


def _is_ideographic(code: int) -> bool:
    """True for characters that are individually breakable (CJK, kana, Hangul)."""
    import unicodedata

    category = unicodedata.category(chr(code))
    if category.startswith("P") or category.startswith("S"):
        return False
    return (
        0x3040 <= code <= 0x30FF        # kana
        or 0x3400 <= code <= 0x4DBF     # CJK extension A
        or 0x4E00 <= code <= 0x9FFF     # CJK unified ideographs
        or 0xF900 <= code <= 0xFAFF     # CJK compatibility ideographs
        or 0xAC00 <= code <= 0xD7AF     # Hangul syllables
        or 0x20000 <= code <= 0x3FFFF   # CJK extensions B..F
    )


def classify(char: str) -> str:
    """Classify one character for line breaking.

    Returns one of:

    ``"space"``
        Whitespace; becomes glue.
    ``"nobreak"``
        Joins with the *previous* character (prolonged sound marks, units).
    ``"nobreakbefore"``
        May not begin a line: closing punctuation and small kana.  This is the
        kinsoku rule that stops a line from starting with ``"。"``.
    ``"cjk"``
        An individually breakable ideograph, kana or Hangul syllable.
    ``"word"``
        Ordinary text; only breakable at an explicit hyphen or hyphenation point.
    """
    if char == " " or char == CJKClass.SPACE or char in "\t":
        return "space"
    if char in CJKClass.NO_BREAK_AFTER:
        return "nobreak"
    if char in CJKClass.NO_START:
        return "nobreakbefore"
    code = ord(char)
    if _is_ideographic(code):
        return "cjk"
    return "word"


# ---------------------------------------------------------------------------
# Item stream
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Box:
    """Unbreakable content of fixed width, measured in terminal cells.

    ``word`` is set for boxes that came from a word, so the hyphens pass can
    split them further.  ``offset`` is the character offset of this box in the
    source text and ``end`` the offset just past it; ``to_items`` uses them to
    map break decisions back onto the original string.
    """

    width: int
    offset: int
    end: int
    word: Optional[str] = None
    kind: str = "box"
    #: True for content that must never be hyphenated (numbers, code, links).
    atomic: bool = False


@dataclass(frozen=True)
class Glue:
    """Flexible space: width plus stretch and shrink capacities."""

    width: int
    stretch: int
    shrink: int
    offset: int
    end: int
    kind: str = "glue"
    #: Glue that may not be used as a break point (non-breaking space).
    nobreak: bool = False


@dataclass(frozen=True)
class Penalty:
    """A legal break with a cost.

    ``flagged`` marks breaks that are typographically risky (hyphens, dashes);
    two adjacent flagged breaks are charged ``LayoutOptions.flagged_penalty``.
    ``width`` is the width of the content the penalty itself contributes, which
    is the hyphen character for a hyphenation break, and ``text`` is that
    content.  Both are zero for an ordinary inter-word break.
    """

    value: int
    offset: int
    end: int
    flagged: bool = False
    #: cells the penalty contributes when the line is *not* broken here.  A
    #: hyphenation penalty contributes the hyphen when the break is taken and zero
    #: otherwise, so this is 0 for it; the hyphen width is what
    #: :func:`_adjustment_ratio` subtracts at the moment the break is taken.  A
    #: forced newline occupies no cells either way.
    width: int = 0
    text: str = ""
    kind: str = "penalty"


Item = Box | Glue | Penalty


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

@dataclass
class LayoutOptions:
    """Everything that influences line breaking.

    The demerit constants default to Knuth's published values, which are tuned
    for a 60-70 character column and are a good starting point for terminals.
    """

    #: Target line width in terminal cells.
    width: int = 80

    #: ``line_penalty`` is added to the badness of every line, which makes the
    #: algorithm prefer fewer lines when the raggedness is comparable.
    line_penalty: int = 10

    #: Tolerance on the adjustment ratio.  ``1`` allows any amount of stretch;
    #: ``2``-``3`` is the usual range for justified text.
    tolerance: float = 1.0

    #: Charge when two flagged (hyphenated) breaks are on consecutive lines.
    flagged_penalty: int = 3000

    #: Charge when a break changes the fitness class by two steps.
    fitness_penalty: int = 1000

    #: Explicit hyphen penalty, charged for hyphenated breaks.
    hyphen_penalty: int = 50

    #: Penalty for ending a line with a single short word, and for starting a
    #: paragraph with a very short last line (widow/orphan control).
    widow_penalty: int = 150
    orphan_penalty: int = 150

    #: Never allow more than one consecutive hyphenated line.
    max_consecutive_hyphens: int = 1

    #: Enable the hyphenation pass.
    hyphenate: bool = True

    #: Allow lines to be slightly overfull instead of failing.
    allow_overfull: bool = True

    #: safety bound on the inner loop of the dynamic program.
    max_items_per_line: int = 512

    #: the character inserted at a hyphenation break
    hyphen_char: str = "-"

    #: Stretch and shrink of an inter-word space, as fractions.  Terminals do
    #: not stretch text, so the layout engine simulates justification by
    #: inserting extra spaces; these values are therefore in cells.
    space_stretch: int = 2
    space_shrink: int = 1

    def __post_init__(self) -> None:
        if self.width < 1:
            raise ValueError("width must be at least 1 cell")
        if self.tolerance <= 0:
            raise ValueError("tolerance must be positive")
        if self.max_items_per_line < 8:
            raise ValueError("max_items_per_line must be at least 8")


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

#: Characters that may never be hyphenated and that bind into one atomic box.
_ATOMIC = frozenset("/\\@#_=<>|&+*^~`")


def to_items(
    text: Text,
    options: LayoutOptions,
    hyphenator=None,
) -> List[Item]:
    """Tokenize ``text`` into a box/glue/penalty stream.

    The tokenizer walks the source once and emits:

    * a :class:`Box` per word (or per CJK character, or per non-breaking run);
    * a :class:`Glue` per run of spaces;
    * a :class:`Penalty` at every legal break inside a word, either an explicit
      hyphen/dash already present or a hyphenation point;
    * a :class:`Penalty` of ``FORCED_BREAK`` per explicit newline.

    Insignificant glue at the start of a line and glue followed by a forced
    break are removed, which is the standard cleanup that keeps the demerit
    arithmetic from counting phantom spaces.
    """
    plain = text.plain
    items: List[Item] = []
    index = 0
    length = len(plain)

    while index < length:
        char = plain[index]

        # ---------------------------------------------------------- newline
        if char == "\n":
            while items and isinstance(items[-1], Glue):
                items.pop()
            # The newline itself is not part of the line, so the penalty spans no
            # characters: ``offset == end == index``.  Spanning the newline would
            # put it inside the finished line's text (and add a phantom empty line
            # immediately after it, because the next line would start one
            # character later than the break).
            items.append(Penalty(FORCED_BREAK, index, index))
            index += 1
            continue

        # ------------------------------------------------------------ space
        if char in (" ", CJKClass.SPACE, "\t"):
            start = index
            width = 0
            nobreak = False
            while index < length and plain[index] in (" ", CJKClass.SPACE, "\t"):
                if plain[index] == "\t":
                    width += 8
                else:
                    width += 1
                index += 1
            # a non-breaking space joins the words around it
            if start > 0 and plain[start - 1] == "\u00a0":
                nobreak = True
            items.append(
                Glue(
                    width=width,
                    stretch=options.space_stretch,
                    shrink=options.space_shrink,
                    offset=start,
                    end=index,
                    nobreak=nobreak,
                )
            )
            # An inter-word space is a legal break, expressed as a zero-value
            # penalty right after the glue.  Without this item the dynamic program
            # would have no candidate at which to end a line: glue alone is not a
            # break in the box/glue/penalty model.
            #
            # The penalty is skipped when the glue is followed by a newline or by
            # nothing at all (trailing whitespace).  Such a glue can never be a
            # break -- the following item already provides one -- and emitting the
            # penalty anyway creates a second break opportunity one item before the
            # paragraph end, which the dynamic program reports as an extra empty
            # line.
            followed_by_text = (
                index < length and plain[index] not in ("\n",)
            )
            if not nobreak and followed_by_text:
                items.append(Penalty(0, index, index, flagged=False))
            continue

        # ------------------------------------------------------------- word
        start = index
        kind = classify(char)
        if kind in ("cjk", "nobreakbefore"):
            # one box per ideograph, plus a break opportunity after it.  The
            # opportunity is suppressed when the next character may not start a
            # line (kinsoku) or is itself unbreakable, which keeps closing
            # punctuation attached to the character it follows.
            width = cell_len(char)
            index += 1
            items.append(Box(width=width, offset=start, end=index, kind=kind))
            following = classify(plain[index]) if index < length else "space"
            if following not in ("nobreakbefore", "nobreak") and following != "space":
                items.append(Penalty(0, index, index, flagged=False))
            elif following == "space":
                pass  # the space itself will contribute the break penalty
            continue

        if char in _ATOMIC or kind == "nobreak":
            # an atomic run: consume neighbours until a space or a CJK char
            while (
                index < length
                and plain[index] not in (" ", "\n", "\t", CJKClass.SPACE)
                and classify(plain[index]) not in ("cjk", "nobreakbefore")
            ):
                index += 1
            run = plain[start:index]
            items.append(
                Box(
                    width=cell_len(run),
                    offset=start,
                    end=index,
                    word=run,
                    atomic=True,
                )
            )
            continue

        # a normal word: letters, digits, internal punctuation such as "-"
        while (
            index < length
            and plain[index] not in (" ", "\n", "\t", CJKClass.SPACE)
            and classify(plain[index]) not in ("cjk", "nobreakbefore", "nobreak")
        ):
            index += 1
        word = plain[start:index]
        items.extend(_word_items(word, start, options, hyphenator))

    _cleanup(items)
    return items


def _word_items(
    word: str,
    offset: int,
    options: LayoutOptions,
    hyphenator,
) -> List[Item]:
    """Emit the items for one whitespace-delimited word.

    The word becomes a single box; if it is long enough and hyphenation is on,
    or if it already contains a hard hyphen, additional break penalties are
    inserted at the legal positions.  The box is then split at every penalty so
    that a break there is representable.
    """
    items: List[Item] = []
    atomic = any(char in _ATOMIC or char.isdigit() for char in word)

    breaks: List[int] = []
    # an explicit hyphen or dash is always a legal break
    for position, char in enumerate(word):
        if char in (HYPHEN_CHARS) and 0 < position < len(word) - 1:
            breaks.append(position + 1)

    if options.hyphenate and hyphenator is not None and not atomic:
        breaks.extend(hyphenator.hyphenate(word))

    breaks = sorted({point for point in breaks if 0 < point < len(word)})

    if not breaks:
        items.append(
            Box(
                width=cell_len(word),
                offset=offset,
                end=offset + len(word),
                word=word,
                atomic=atomic,
            )
        )
        return items

    # split the box at each break, leaving a penalty carrying the hyphen width
    previous = 0
    for point in breaks:
        segment = word[previous:point]
        if segment:
            items.append(
                Box(
                    width=cell_len(segment),
                    offset=offset + previous,
                    end=offset + point,
                    word=None,
                    atomic=atomic,
                )
            )
        items.append(
            Penalty(
                options.hyphen_penalty,
                offset + point,
                offset + point,
                flagged=True,
                width=cell_len(options.hyphen_char),
                text=options.hyphen_char,
            )
        )
        previous = point
    tail = word[previous:]
    if tail:
        items.append(
            Box(
                width=cell_len(tail),
                offset=offset + previous,
                end=offset + len(word),
                word=None,
                atomic=atomic,
            )
        )
    return items


#: Characters that already mark a legitimate break in the source text.
HYPHEN_CHARS = "-\u2010\u2011\u2012\u2013\u2014"


def _cleanup(items: List[Item]) -> None:
    """Remove glue and penalties that can never contribute.

    * leading glue (a paragraph never starts with a space),
    * glue directly before a forced break (trailing whitespace),
    * penalties at the very start,
    * runs of consecutive penalties (keep the cheapest).
    """
    while items and isinstance(items[0], Glue):
        items.pop(0)
    if items and isinstance(items[0], Penalty) and not isinstance(items[0], Glue):
        if items[0].value > FORCED_BREAK:
            items.pop(0)

    cleaned: List[Item] = []
    index = 0
    while index < len(items):
        item = items[index]
        if isinstance(item, Glue):
            nxt = items[index + 1] if index + 1 < len(items) else None
            if isinstance(nxt, Penalty) and nxt.value <= FORCED_BREAK:
                index += 1
                continue
        if isinstance(item, Penalty):
            nxt = items[index + 1] if index + 1 < len(items) else None
            if isinstance(nxt, Penalty):
                # Two adjacent penalties at the *same* source position are one
                # break opportunity too many, so keep the more attractive one.
                # Penalties at different positions are different breaks and must
                # both survive: collapsing "a\n\nb" to a single break would delete
                # the blank line the author wrote.
                if item.offset != nxt.offset:
                    cleaned.append(item)
                    index += 1
                    continue
                if item.value <= FORCED_BREAK:
                    cleaned.append(item)
                    index += 2
                    continue
                if nxt.value <= FORCED_BREAK:
                    cleaned.append(nxt)
                    index += 2
                    continue
                if item.value >= nxt.value:
                    index += 1
                    continue
        cleaned.append(item)
        index += 1

    # A forced break at the very end of the paragraph is redundant, but a forced
    # break in the middle must survive: it is the hard newline the caller asked
    # for.  Only trim a trailing run, and only when nothing follows it.
    while (
        cleaned
        and isinstance(cleaned[-1], Penalty)
        and cleaned[-1].value <= FORCED_BREAK
    ):
        trimmed = False
        while cleaned and isinstance(cleaned[-1], Penalty) and cleaned[-1].value <= FORCED_BREAK:
            cleaned.pop()
            trimmed = True
        while cleaned and isinstance(cleaned[-1], Glue):
            cleaned.pop()
        if not trimmed:
            break
    # Trailing whitespace is not part of the last line.  The tokenizer emits glue
    # for it, and because the paragraph does not end with a newline there is no
    # following break that would let the layout drop the glue for us, so it is
    # removed here.
    while cleaned and isinstance(cleaned[-1], Glue):
        cleaned.pop()
    items[:] = cleaned


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class BrokenLine:
    """One line of the plan, described in source character offsets."""

    #: index of the first item on the line, inclusive
    start_item: int
    #: index one past the last item on the line
    end_item: int
    #: character offset of the first character of the line
    start: int
    #: character offset one past the last character of the line
    end: int
    #: break item that ended this line (a Penalty) or None for the last line
    break_item: Optional[int]
    #: adjustment ratio; negative means the line was shrunk
    ratio: float
    #: natural width in cells before adjustment
    natural: int
    #: final width in cells after adjustment
    width: int
    #: number of glue items on the line, i.e. how many gaps can be adjusted
    glue_count: int
    #: True when the line had to break inside a word
    hyphenated: bool = False
    #: True when the line could not be made to fit
    overfull: bool = False
    #: total demerits of this line, or 0 for the greedy fallback
    demerits: float = 0.0
    #: text inserted after ``source[start:end]``, i.e. the hyphen of a
    #: hyphenation break.  The caller must append it *before* any trailing
    #: whitespace, otherwise the hyphen would end up after a space.
    prepend: str = ""
    #: the line's source text, filled in by :func:`layout` so that
    #: :meth:`text` also works on a plan built without :func:`layout`
    source_text: str = ""
    #: trailing whitespace that belongs to the source slice but not to the line
    trailing: str = ""

    @property
    def adjustment(self) -> int:
        """Cells that must be added to (positive) or removed from the line."""
        return self.width - self.natural

    @property
    def text(self) -> str:
        """The line as plain text: source slice, plus the hyphen, minus the
        trailing whitespace that the break consumed."""
        return self.source_text + self.prepend

    def span(self, plain: str) -> str:
        """The line's plain text, given the paragraph's plain text."""
        return plain[self.start:self.end] + self.prepend


@dataclass
class LayoutPlan:
    """The result of :func:`layout`."""

    lines: List[BrokenLine]
    items: List[Item]
    options: LayoutOptions
    #: total demerits of the chosen solution, lower is better
    total_demerits: float
    #: True when the dynamic program failed and a greedy pass was used
    fallback: bool = False
    #: number of states considered, for the benchmark command
    states: int = 0

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def widths(self) -> List[int]:
        return [line.width for line in self.lines]


# ---------------------------------------------------------------------------
# Knuth-Plass
# ---------------------------------------------------------------------------

def _width_of(items: Sequence[Item], start: int, end: int) -> Tuple[int, int, int, int]:
    """Return ``(width, stretch, shrink, glue_count)`` for items[start:end].

    Penalties contribute nothing here.  Their ``width`` is the material the break
    *adds* (the hyphen), so it is subtracted at the moment a break is actually
    taken; see the ``line_width -= item.width`` step in :func:`break_lines`.  A
    forced newline has width ``0`` and therefore never affects a line.
    """
    width = stretch = shrink = 0
    glue_count = 0
    for item in items[start:end]:
        if isinstance(item, Glue):
            width += item.width
            stretch += item.stretch
            shrink += item.shrink
            glue_count += 1
        elif isinstance(item, Box):
            width += item.width
    return width, stretch, shrink, glue_count


def _adjustment_ratio(
    width: int,
    target: int,
    stretch: int,
    shrink: int,
) -> float:
    """Knuth's adjustment ratio ``r``; ``+inf`` when the line cannot be set."""
    if width == target:
        return 0.0
    if width < target:
        if stretch <= 0:
            return math.inf
        return (target - width) / stretch
    if shrink <= 0:
        return -math.inf
    return (target - width) / shrink


def _fitness_class(ratio: float) -> int:
    """Bucket a ratio into the four fitness classes of the algorithm."""
    if ratio < -0.5:
        return 0        # tight
    if ratio <= 0.5:
        return 1        # decent
    if ratio <= 1.0:
        return 2        # loose
    return 3            # very loose


def _badness(ratio: float) -> float:
    """Knuth's badness for an adjustment ratio, capped at ``BADNESS_CAP``.

    TeX defines ``badness = 100 * |r|^3`` and clamps it at 10000, which is the
    crucial property here: the cap stops one hopeless line from dominating the
    demerit sum.  Without it, a forced line ending with ``r = 15`` produces a
    badness of ``15^3 = 3375`` times the line penalty and the dynamic program
    happily prefers that single terrible line over several good ones -- which is
    exactly the bug that made a hard newline look optional.
    """
    if ratio == 0:
        return 0.0
    return min(100.0 * abs(ratio) ** 3, BADNESS_CAP)


def _demerits(
    ratio: float,
    penalty: int,
    flagged: bool,
    previous_fitness: int,
    fitness: int,
    options: LayoutOptions,
) -> float:
    """Demerits of setting a line with the given ratio and break penalty.

    This is Knuth's formula: ``(line_penalty + badness)^2`` for a normal break,
    which makes a line with zero badness still cost ``line_penalty^2`` and so
    biases the solution towards fewer lines.  A forced break is free, because the
    author asked for it.
    """
    badness = _badness(ratio)
    if penalty <= FORCED_BREAK:
        total = 0.0
    else:
        total = (options.line_penalty + badness) ** 2
        if penalty > 0:
            total += penalty * penalty
        if flagged:
            total += options.flagged_penalty
    if abs(fitness - previous_fitness) > 1:
        total += options.fitness_penalty
    return total


@dataclass
class _State:
    """One dynamic-programming state: a break candidate that was reached."""

    __slots__ = ("item", "offset", "demerits", "ratio", "fitness", "line", "previous", "hyphens")

    item: int
    offset: int
    demerits: float
    ratio: float
    fitness: int
    line: int
    previous: Optional["_State"]
    hyphens: int


def _close_line(
    items: Sequence[Item],
    state: _State,
    index: int,
    width: int,
    options: LayoutOptions,
) -> Tuple[Optional[_State], float]:
    """Terminate a line at a forced break, returning ``(state, ratio)``.

    A forced break cannot be declined, so this differs from the ordinary
    candidate evaluation in one way: an overfull or hopelessly loose line is still
    accepted, because rejecting it would leave the paragraph unset.  The caller
    records the resulting state as the start of the next line.
    """
    line_width, stretch, shrink, _glue = _width_of(items, state.item, index + 1)
    item = items[index]
    if isinstance(item, Penalty):
        line_width -= item.width
    ratio = _adjustment_ratio(line_width, width, stretch, shrink)
    if ratio == math.inf or ratio == -math.inf:
        ratio = options.tolerance
    if ratio < -1 and not options.allow_overfull:
        return None, ratio
    fitness = _fitness_class(ratio)
    total = state.demerits + _demerits(
        ratio,
        FORCED_BREAK,
        False,
        state.fitness,
        fitness,
        options,
    )
    return (
        _State(
            item=index + 1,
            offset=item.end,
            demerits=total,
            ratio=ratio,
            fitness=fitness,
            line=state.line + 1,
            previous=state,
            hyphens=0,
        ),
        ratio,
    )


#: ``_split_segments`` kind: a range of items that must be set as a paragraph.
SEGMENT_CONTENT = 0

#: ``_split_segments`` kind: a single hard newline that stands for a blank line.
SEGMENT_BLANK = 1


def _split_segments(items: Sequence[Item]) -> List[Tuple[int, int, int]]:
    """Split ``items`` into ``(start, end, kind)`` half-open ranges.

    A range is one paragraph segment: items that must be set together because no
    hard newline separates them.  ``kind`` is :data:`SEGMENT_CONTENT` for a real
    paragraph and :data:`SEGMENT_BLANK` for an empty line between two hard
    newlines.

    The forced break that terminates a content range is its last item, so a line
    ending there still reports the break (and therefore its hyphen, if it had
    one).  The next range starts one item later, which skips the newline itself.

    Solving each range independently is what makes a hard newline hard: the
    dynamic program never gets the chance to break across the boundary, and
    demerits cannot leak from one segment into the next.
    """
    segments: List[Tuple[int, int, int]] = []
    start = 0
    previous_forced: Optional[int] = None
    for index, item in enumerate(items):
        if not (isinstance(item, Penalty) and item.value <= FORCED_BREAK):
            continue
        if previous_forced is not None and previous_forced + 1 == index:
            # Two hard newlines in a row: the line between them is blank.  A run
            # of n newlines therefore yields n - 1 blank lines, exactly as the
            # author wrote: "a\n\nb" is one blank line, "a\nb" is none.
            segments.append((previous_forced, index, SEGMENT_BLANK))
        else:
            # a content segment, terminated by this hard newline
            segments.append((start, index + 1, SEGMENT_CONTENT))
        previous_forced = index
        start = index + 1
    if start <= len(items) - 1:
        segments.append((start, len(items), SEGMENT_CONTENT))
    return segments


def break_lines(
    items: Sequence[Item],
    options: LayoutOptions,
) -> Tuple[List[BrokenLine], float, int, bool]:
    """Break ``items`` into lines of at most ``options.width`` cells.

    Returns ``(lines, total_demerits, states_visited, fallback)``.

    The paragraph is first split at forced breaks (see :func:`_split_segments`)
    and each segment is solved on its own by :func:`_break_segment`.  The segment
    lines are then concatenated, so a hard newline is honoured unconditionally
    while everything between two hard newlines is optimised as one paragraph.
    """
    if not items:
        return [], 0.0, 0, False

    all_lines: List[BrokenLine] = []
    total_demerits = 0.0
    total_states = 0
    any_fallback = False

    for start, end, kind in _split_segments(items):
        if kind == SEGMENT_BLANK:
            # A blank line: no items to set, but it must appear in the output or
            # the blank line the author wrote would silently disappear.
            offset = items[start].offset if start < len(items) else 0
            all_lines.append(
                BrokenLine(
                    start_item=start,
                    end_item=start,
                    start=offset,
                    end=offset,
                    break_item=None,
                    ratio=0.0,
                    natural=0,
                    width=0,
                    glue_count=0,
                )
            )
            continue
        segment = list(items[start:end])
        # The terminating newline belongs to the segment only so that a line
        # ending on it can report the break; its characters must not be measured,
        # and it must not be treated as a break candidate.
        while (
            segment
            and isinstance(segment[-1], Penalty)
            and segment[-1].value <= FORCED_BREAK
        ):
            segment.pop()
        if not segment:
            continue
        lines, demerits, states, fallback = _break_segment(segment, options)
        any_fallback = any_fallback or fallback
        total_demerits += demerits
        total_states += states
        # re-base the item indices and character offsets onto the whole paragraph
        for line in lines:
            line.start_item += start
            line.end_item += start
            if line.break_item is not None:
                line.break_item += start
        all_lines.extend(lines)
    return all_lines, total_demerits, total_states, any_fallback


def _break_segment(
    items: Sequence[Item],
    options: LayoutOptions,
) -> Tuple[List[BrokenLine], float, int, bool]:
    """Knuth-Plass over one segment: no forced breaks, so one clean search."""
    if not items:
        return [], 0.0, 0, False

    width = options.width
    active: List[_State] = [_State(0, 0, 0.0, 0.0, 1, 0, None, 0)]
    final: Optional[_State] = None
    states = 0

    # Candidate break points are the penalties plus the end of the segment.
    # Glue is never a break by itself: the tokenizer puts a zero-value penalty
    # after each inter-word glue, so treating glue as a candidate would evaluate
    # lines ending in the middle of a space.
    last_index = len(items) - 1
    stops: List[Tuple[int, bool]] = [
        (index, index == last_index)
        for index, item in enumerate(items)
        if isinstance(item, Penalty) and item.value < INFINITE_PENALTY
    ]
    if not stops or stops[-1][0] != last_index:
        stops.append((last_index, True))

    for index, at_end in stops:
        item = items[index]
        is_break = isinstance(item, Penalty)
        penalty_value = item.value if is_break else -INFINITE_PENALTY

        candidates: List[_State] = []
        for state in active:
            start = state.item
            if index - start > options.max_items_per_line:
                continue
            line_width, stretch, shrink, glue_count = _width_of(items, start, index + 1)
            # the break item's own width (the hyphen) is not part of the line
            if is_break:
                line_width -= item.width
            ratio = _adjustment_ratio(line_width, width, stretch, shrink)
            states += 1
            # The end of the segment must always terminate a line, so a line that
            # cannot be set within tolerance is still accepted there: there is no
            # alternative break to choose instead.
            must_terminate = at_end

            if ratio == -math.inf:
                if not (must_terminate and options.allow_overfull):
                    continue
                ratio = -1.0
            elif ratio == math.inf:
                if not (must_terminate and options.allow_overfull):
                    continue
                ratio = options.tolerance
            else:
                if ratio < -1 and not (must_terminate and options.allow_overfull):
                    continue                      # overfull, not allowed here
                if ratio > options.tolerance and not must_terminate:
                    continue                      # too loose to be acceptable

            if is_break and item.flagged and state.hyphens >= options.max_consecutive_hyphens:
                continue

            fitness = _fitness_class(ratio)
            hyphens = state.hyphens + 1 if (is_break and item.flagged) else 0
            total = state.demerits + _demerits(
                ratio,
                penalty_value,
                is_break and item.flagged,
                state.fitness,
                fitness,
                options,
            )
            candidates.append(
                _State(
                    item=index + 1,
                    offset=item.end,
                    demerits=total,
                    ratio=ratio,
                    fitness=fitness,
                    line=state.line + 1,
                    previous=state,
                    hyphens=hyphens,
                )
            )

        if not candidates:
            # A segment that cannot be set at all falls back to greedy wrapping.
            if at_end:
                break
            continue

        # keep the best state per fitness class
        best: Dict[int, _State] = {}
        for candidate in candidates:
            current = best.get(candidate.fitness)
            if current is None or candidate.demerits < current.demerits:
                best[candidate.fitness] = candidate
        active = list(best.values())

        if at_end:
            final = min(active, key=lambda state: state.demerits)

    if final is None:
        return _greedy_lines(items, options)

    # ------------------------------------------------------------- backtrack
    chain: List[_State] = []
    node: Optional[_State] = final
    while node is not None and node.previous is not None:
        chain.append(node)
        node = node.previous
    chain.reverse()

    # ------------------------------------------------ widow / orphan control
    # The repair deletes chain entries in place; because every line is derived
    # from `state.previous.item .. state.item`, deleting an entry is enough to
    # merge the two lines it separated, with no re-linking needed.
    _apply_widow_orphan(chain, items, options)

    lines: List[BrokenLine] = []
    for position, state in enumerate(chain):
        lines.append(
            _materialise(chain, position, items, options)
        )
    return lines, final.demerits, states, False


def _line_start_item(state: _State) -> int:
    """First item index of the line that ``state`` closes.

    ``state.previous`` is either the state that closed the previous line, or a
    segment-start sentinel (the position just after a forced break).  The
    sentinel means "this line begins at item 0 of its segment", which is also
    item 0 of the whole paragraph because segments are consecutive.
    """
    if state.previous is None:
        return 0
    if state.previous.item == SEGMENT_START:
        return 0
    return state.previous.item


def _materialise(
    chain: Sequence[_State],
    position: int,
    items: Sequence[Item],
    options: LayoutOptions,
) -> BrokenLine:
    """Build the :class:`BrokenLine` for ``chain[position]``.

    The trailing whitespace of the source slice is separated out into
    ``trailing`` and the hyphen of a hyphenation break into ``prepend``.  Keeping
    them apart is what lets :mod:`rich._justify` rebuild the line as
    ``source[start:end] + "-"`` instead of ``"..." + "-" + " "``; the latter
    would put the hyphen after the word-separating space and produce a line that
    is one cell too wide.
    """
    state = chain[position]
    start_item = state.previous.item if state.previous else 0
    natural, _stretch, _shrink, _glue = _width_of(items, start_item, state.item)
    break_item: Optional[int] = None
    hyphenated = False
    prepend = ""
    if state.item - 1 < len(items) and isinstance(items[state.item - 1], Penalty):
        penalty = items[state.item - 1]
        break_item = state.item - 1
        natural -= penalty.width
        hyphenated = penalty.flagged
        prepend = penalty.text
    start = _offset_of(items, start_item)
    # The line spans through the *end* of its last item.  Using the offset would
    # drop the last character of every line, and would produce an empty line for a
    # one-line paragraph whose last item is the only box.
    end = _end_of(items, state.item - 1)
    return BrokenLine(
        start_item=start_item,
        end_item=state.item,
        start=start,
        end=end,
        break_item=break_item,
        ratio=0.0 if math.isinf(state.ratio) else state.ratio,
        natural=natural,
        width=options.width if not math.isinf(state.ratio) else natural,
        glue_count=_glue,
        hyphenated=hyphenated,
        overfull=state.ratio < -1,
        demerits=state.demerits,
        prepend=prepend,
    )


def _offset_of(items: Sequence[Item], index: int) -> int:
    """Character offset of the first character of item ``index``."""
    if index < 0:
        return 0
    if index >= len(items):
        return items[-1].end if items else 0
    return items[index].offset


def _end_of(items: Sequence[Item], index: int) -> int:
    """Character offset just past item ``index``, used to close a line."""
    if index < 0:
        return 0
    if index >= len(items):
        return items[-1].end if items else 0
    return items[index].end


def _next_break(items: Sequence[Item], after: int) -> Optional[int]:
    """Index of the first legal break strictly after item ``after``.

    Hyphenation penalties are candidates, so a repair may legitimately land on a
    hyphen; callers that only want whole-word breaks filter on ``flagged``.
    """
    for candidate in range(max(0, after + 1), len(items)):
        item = items[candidate]
        if isinstance(item, Penalty) and item.value < INFINITE_PENALTY:
            return candidate
    return None


def _line_words(items: Sequence[Item], start: int, end: int) -> int:
    """Count the whole words in ``items[start:end]``.

    A box immediately followed by a flagged penalty is only a *fragment* of a
    word that hyphenation split, so it is not counted as a word of its own.  This
    matters for orphan detection: a line ending in ``"for-"`` reads as one word,
    and counting the fragment separately would hide the orphan.
    """
    total = 0
    for index in range(start, min(end, len(items))):
        if not isinstance(items[index], Box):
            continue
        following = items[index + 1] if index + 1 < len(items) else None
        if isinstance(following, Penalty) and following.flagged:
            continue
        total += 1
    return total


def _fits(items: Sequence[Item], start: int, end: int, options: LayoutOptions) -> bool:
    """True when ``items[start:end]`` fits the column, ignoring the break width."""
    natural, _stretch, _shrink, _glue = _width_of(items, start, end)
    if 0 <= end - 1 < len(items) and isinstance(items[end - 1], Penalty):
        natural -= items[end - 1].width
    return natural <= options.width


def _earliest_fitting_break(
    items: Sequence[Item],
    start: int,
    minimum: int,
    options: LayoutOptions,
    *,
    allow_flagged: bool = False,
) -> Optional[int]:
    """Earliest legal break at or after ``minimum`` whose line fits the column.

    Three details matter:

    * the scan starts *at* ``minimum`` so a caller can require that the break move
      strictly to the right by passing the stub's own break as ``minimum``;
    * hyphenated breaks are skipped unless ``allow_flagged`` is set.  Replacing one
      hyphenation break with the next one down the same word simply moves the stub
      rather than removing it, so an orphan repair has to look for a whole-word
      break;
    * the fit is measured excluding each candidate's own width, because the
      penalty's width (the hyphen) collapses when the line is actually set there.

    Returning the earliest fit rather than the best-looking fit keeps the merged
    line as short as possible, and returning ``None`` when nothing fits means the
    caller must leave the paragraph alone rather than create an overfull line.
    """
    for candidate in range(max(0, minimum), len(items)):
        item = items[candidate]
        if not isinstance(item, Penalty) or item.value >= INFINITE_PENALTY:
            continue
        if item.flagged and not allow_flagged:
            continue
        natural, _stretch, _shrink, _glue = _width_of(items, start, candidate + 1)
        if item.value >= FORCED_BREAK:
            natural -= item.width
        if natural <= options.width:
            return candidate
    return None


def _merge_if_settable(
    items: Sequence[Item],
    start: int,
    minimum: int,
    options: LayoutOptions,
) -> Optional[int]:
    """Find a whole-word break that repairs a stub line *and* sets normally.

    A merge is only accepted when the resulting line has an adjustment ratio
    inside ``[-1, tolerance]``.  Fitting the full column width is not enough: a
    merged line stretched across 60 of 72 cells is far worse typography than the
    stub it replaced, so the repair must produce a line the reader would accept
    rather than merely a line that does not overflow.
    """
    candidate = _earliest_fitting_break(items, start, minimum, options)
    while candidate is not None:
        natural, stretch, shrink, _glue = _width_of(items, start, candidate + 1)
        item = items[candidate]
        if isinstance(item, Penalty) and item.value >= FORCED_BREAK:
            natural -= item.width
        ratio = _adjustment_ratio(natural, options.width, stretch, shrink)
        if ratio != math.inf and -1.0 <= ratio <= options.tolerance:
            return candidate
        candidate = _earliest_fitting_break(items, start, candidate + 1, options)
    return None


def _is_settable(
    items: Sequence[Item],
    start: int,
    end: int,
    options: LayoutOptions,
) -> bool:
    """True when ``items[start:end]`` can be set as a line within tolerance."""
    natural, stretch, shrink, _glue = _width_of(items, start, end)
    if 0 <= end - 1 < len(items) and isinstance(items[end - 1], Penalty):
        natural -= items[end - 1].width
    ratio = _adjustment_ratio(natural, options.width, stretch, shrink)
    if ratio == math.inf:
        # a line with no stretchable glue is only acceptable as the last line
        return end >= len(items)
    return -1.0 <= ratio <= options.tolerance


def _apply_widow_orphan(
    chain: List[_State],
    items: Sequence[Item],
    options: LayoutOptions,
) -> None:
    """Remove the break that creates an orphan or a widow line, in place.

    Two classic paragraph faults are invisible to a pure demerit minimisation
    because the offending line is cheap on its own:

    * **orphan** -- a line near the start holds a single word, so the paragraph
      opens with a stub such as ``"for-"``;
    * **widow** -- the last line holds a single word while the penultimate line
      ends near the right margin, so a stub dangles at the end.

    Both are repaired by **deleting** a break: deleting a stub's break merges it
    into the following line, and deleting the second-to-last break merges the
    widowed tail into the line before it.  A merge is accepted only when the
    resulting line sets within tolerance (see :func:`_merge_if_settable`), and the
    repair is abandoned entirely if any line of the resulting paragraph would
    overflow.  That validation is what makes the repair safe: it can improve a
    paragraph but it can never make one worse than the optimal solution it is
    layered on.

    This runs *after* the dynamic program, so the demerits reported by
    :func:`break_lines` remain those of the optimal solution; the repair is a
    documented typographic tie-break on top of it.
    """
    if len(chain) < 3:
        return

    accepted = list(chain)

    def sizes(lines: Sequence[_State]) -> List[Tuple[int, int]]:
        result: List[Tuple[int, int]] = []
        previous = 0
        for state in lines:
            result.append((previous, state.item))
            previous = state.item
        return result

    def valid(lines: Sequence[_State]) -> bool:
        spans = sizes(lines)
        return all(
            end > start and _is_settable(items, start, end, options)
            or end >= len(items)
            for start, end in spans
        )

    # --- orphan: a stub line near the start -------------------------------
    for position in range(min(2, len(accepted) - 1)):
        head = accepted[position]
        following = accepted[position + 1]
        start = head.previous.item if head.previous else 0
        if _line_words(items, start, head.item) > 1:
            continue
        merged = _merge_if_settable(items, start, head.item, options)
        if merged is None or merged + 1 > following.item:
            continue
        candidate = list(accepted)
        if position == 0:
            candidate[0].item = merged + 1
            candidate[0].offset = items[merged].end
        else:
            del candidate[position]
            candidate[position].item = merged + 1
            candidate[position].offset = items[merged].end
        if valid(candidate):
            accepted = candidate
            break

    # --- widow: the tail line is a stub -----------------------------------
    if len(accepted) >= 3:
        penultimate = accepted[-2]
        last = accepted[-1]
        if _line_words(items, penultimate.item, last.item) <= 1:
            start = penultimate.previous.item if penultimate.previous else 0
            merged = _merge_if_settable(items, start, penultimate.item, options)
            if merged is not None and merged + 1 <= last.item:
                candidate = list(accepted)
                candidate[-2].item = 0
                candidate[-2].offset = 0
                candidate[-1].item = merged + 1
                candidate[-1].offset = items[merged].end
                if valid(candidate):
                    accepted = candidate

    chain[:] = accepted



def _make_line(
    items: Sequence[Item],
    start_item: int,
    end_item: int,
    options: LayoutOptions,
    ratio: float,
    overfull: bool,
) -> BrokenLine:
    """Build a BrokenLine for an arbitrary item range (used by the fallback)."""
    natural, _stretch, _shrink, glue_count = _width_of(items, start_item, end_item)
    break_item: Optional[int] = None
    hyphenated = False
    prepend = ""
    if end_item - 1 < len(items) and isinstance(items[end_item - 1], Penalty):
        penalty = items[end_item - 1]
        break_item = end_item - 1
        natural -= penalty.width
        hyphenated = penalty.flagged
        prepend = penalty.text
    return BrokenLine(
        start_item=start_item,
        end_item=end_item,
        start=_offset_of(items, start_item),
        end=_offset_of(items, end_item - 1),
        break_item=break_item,
        ratio=ratio,
        natural=natural,
        width=natural if math.isinf(ratio) else options.width,
        glue_count=glue_count,
        hyphenated=hyphenated,
        overfull=overfull,
        prepend=prepend,
    )


def _greedy_lines(
    items: Sequence[Item],
    options: LayoutOptions,
) -> Tuple[List[BrokenLine], float, int, bool]:
    """First-fit fallback used when the dynamic program finds no solution.

    This mirrors what upstream ``Text.wrap`` does, so the layout engine always
    returns something usable even for a word wider than the column.
    """
    lines: List[BrokenLine] = []
    start_item = 0
    index = 0
    width = options.width

    while index < len(items):
        item = items[index]
        candidate_width, _s, _h, _g = _width_of(items, start_item, index + 1)
        break_here = isinstance(item, Penalty) and item.value < INFINITE_PENALTY
        current_width = candidate_width - (item.width if break_here else 0)
        if current_width > width and index > start_item:
            break_item = _last_break(items, start_item, index)
            lines.append(
                _make_line(items, start_item, break_item + 1, options, -1.0, True)
            )
            start_item = break_item + 1
            index = start_item
            continue
        index += 1

    if start_item < len(items):
        line = _make_line(items, start_item, len(items), options, math.inf, False)
        line.overfull = line.natural > width
        lines.append(line)
    return lines, 0.0, 0, True


def _last_break(items: Sequence[Item], start: int, end: int) -> int:
    """Index of the last legal break in ``items[start:end]``, else ``end - 1``."""
    for index in range(end - 1, start, -1):
        item = items[index]
        if isinstance(item, Penalty) and item.value < INFINITE_PENALTY:
            return index
        if isinstance(item, Glue):
            return index
    return max(start, end - 1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def layout(
    text: Text,
    options: Optional[LayoutOptions] = None,
    hyphenator=None,
) -> LayoutPlan:
    """Break ``text`` into lines and return a :class:`LayoutPlan`.

    Args:
        text: any Rich :class:`~rich.text.Text`; styles are preserved by the
            caller because the plan references character offsets.
        options: layout parameters; defaults to an 80 cell column.
        hyphenator: a :class:`rich._hyphenation.Hyphenator` enabling breaks
            inside long words.

    Returns:
        A plan whose ``lines`` are in visual order and whose offsets index into
        ``text.plain``.
    """
    options = options or LayoutOptions()
    plain = text.plain
    items = to_items(text, options, hyphenator)
    lines, demerits, states, fallback = break_lines(items, options)
    for line in lines:
        # hand each line its own source text so that BrokenLine.text works
        # without the caller having to keep the paragraph around
        line.source_text = plain[line.start:line.end]
        trimmed = line.source_text.rstrip(" \t")
        line.trailing = line.source_text[len(trimmed):]
        line.source_text = trimmed
    return LayoutPlan(
        lines=lines,
        items=items,
        options=options,
        total_demerits=demerits,
        fallback=fallback,
        states=states,
    )
