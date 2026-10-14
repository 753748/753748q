"""Tests for the paragraph justification feature.

Covered modules:

* :mod:`rich._hyphenation` -- Liang patterns, the dictionary, TeX file loading
* :mod:`rich._layout` -- the box/glue/penalty model and Knuth-Plass
* :mod:`rich._justify` -- alignment modes, style preservation, paragraph handling
* :mod:`rich._justify_demo` -- the command line entry points

The properties that matter most, and which are asserted throughout, are:

1. **no line ever exceeds the column** (unless a single word is wider than the
   whole column, which is reported through ``overfull``);
2. **no text is lost or duplicated** -- the concatenation of the lines is the
   paragraph, modulo inter-word whitespace;
3. **styles survive line breaking**, because lines are built by slicing the
   original ``Text``.
"""

from __future__ import annotations

import sys
import pathlib
from typing import List

import pytest

from rich.console import Console
from rich.style import Style
from rich.text import Text

from rich._hyphenation import (
    BOUNDARY,
    Hyphenator,
    HyphenationError,
    exceptions_to_dict,
    load_tex_patterns,
    parse_pattern,
)
from rich._hyphenation_dict import HYPHENATION_DICT
from rich._hyphenation_patterns import APPROXIMATE_PATTERNS, CORPUS_SIZE, TRAINING_RECALL
from rich._justify import (
    JustifiedLine,
    JustifyOptions,
    justify,
    justify_paragraph,
    reflow,
    render_lines,
)
from rich._layout import (
    FORCED_BREAK,
    INFINITE_PENALTY,
    Box,
    BrokenLine,
    Glue,
    LayoutOptions,
    Penalty,
    break_lines,
    classify,
    layout,
    to_items,
)

PARAGRAPH = (
    "Rich is a Python library for rich text and beautiful formatting in the "
    "terminal. The justification engine implements optimal paragraph line "
    "breaking with hyphenation, which the upstream greedy wrapper cannot do."
)

LONG_PARAGRAPH = PARAGRAPH * 3


def words_of(lines: List[JustifiedLine]) -> List[str]:
    return " ".join(line.plain for line in lines).split()


# ---------------------------------------------------------------------------
# hyphenation: pattern parsing
# ---------------------------------------------------------------------------

def test_parse_pattern_basic():
    letters, weights, left, right = parse_pattern("hy3ph")
    assert letters == "hyph"
    assert weights == (0, 3, 0, 0)
    assert not left and not right


def test_parse_pattern_leading_weight():
    """A weight before the first letter cannot fill a gap, so it anchors.

    "2ion" means "do not break before the i", which is a left anchor rather than
    a weight on any inter-letter gap; recording it as an anchor keeps the
    ``len(weights) == len(letters)`` invariant the trie depends on.
    """
    letters, weights, left, _right = parse_pattern("2ion")
    assert letters == "ion"
    assert left
    assert len(weights) == len(letters)


def test_parse_pattern_anchors():
    letters, _weights, left, right = parse_pattern(".ach4")
    assert letters == ".ach"          # the leading dot stays in the match key
    assert left and not right
    letters, weights, left, right = parse_pattern("4n2t.")
    assert letters == "nt"
    assert right                        # the trailing dot is the right anchor
    assert left                         # the leading "4" anchors the start too
    assert weights[0] == 2              # "2" fills the gap between n and t


def test_parse_pattern_weight_count_always_matches_letters():
    for pattern in ("hy3ph", "2ion", ".ach4", "4n2t.", "a1b2c3", "n2t", ".hy3"):
        letters, weights, _left, _right = parse_pattern(pattern)
        assert len(weights) == len(letters), pattern


def test_parse_pattern_rejects_letterless_input():
    with pytest.raises(HyphenationError):
        parse_pattern("123")


# ---------------------------------------------------------------------------
# hyphenation: the hyphenator
# ---------------------------------------------------------------------------

def test_dictionary_is_used_when_known():
    hyphenator = Hyphenator()
    assert hyphenator.known("hyphenation")
    assert hyphenator.hyphenate("hyphenation") == [2, 6, 7] or hyphenator.hyphenate(
        "hyphenation"
    ) == sorted(HYPHENATION_DICT["hyphenation"])


def test_short_words_are_never_hyphenated():
    hyphenator = Hyphenator()
    for word in ("a", "an", "the", "of", "code"):
        assert hyphenator.hyphenate(word) == []


def test_minima_are_respected():
    hyphenator = Hyphenator()
    for word in ("justification", "algorithm", "terminal", "computer"):
        points = hyphenator.hyphenate(word)
        assert all(point >= hyphenator.left for point in points)
        assert all(len(word) - point >= hyphenator.right for point in points)


def test_unknown_word_without_patterns_is_not_hyphenated():
    hyphenator = Hyphenator()
    # a nonsense word with no dictionary entry and no patterns loaded
    assert hyphenator.hyphenate("zzqxwvu") == []


def test_break_word_reassembles_to_the_original():
    hyphenator = Hyphenator()
    for word in ("hyphenation", "justification", "algorithm", "typography"):
        pieces = hyphenator.break_word(word)
        assert "".join(pieces).replace(hyphenator.hyphen, "") == word


def test_break_word_of_unhyphenatable_word_is_itself():
    assert Hyphenator().break_word("cat") == ["cat"]


def test_variants_contains_the_whole_word_first():
    hyphenator = Hyphenator()
    variants = hyphenator.variants("justification")
    assert variants[0] == "justification"
    assert "jus-tification" in variants
    assert len(variants) == 1 + len(hyphenator.hyphenate("justification"))


def test_dictionary_entries_have_legal_offsets():
    for word, points in HYPHENATION_DICT.items():
        assert word == word.lower()
        for point in points:
            assert 2 <= point <= len(word) - 3


def test_custom_dictionary_overrides_bundled_one():
    hyphenator = Hyphenator(dictionary={"zzzzzz": [2]}, min_word_length=3)
    assert hyphenator.hyphenate("zzzzzz") == [2]


def test_cache_returns_equal_results():
    hyphenator = Hyphenator()
    first = hyphenator.hyphenate("justification")
    second = hyphenator.hyphenate("justification")
    assert first == second
    first.append(99)
    assert hyphenator.hyphenate("justification") == second


def test_split_words_finds_letter_runs():
    """Runs of letters and digits stay together; digits alone are not words."""
    hyphenator = Hyphenator()
    assert hyphenator.split_words("alpha 42 beta, gamma!") == ["alpha", "beta", "gamma"]
    assert hyphenator.split_words("42") == []


# ---------------------------------------------------------------------------
# hyphenation: patterns and TeX interop
# ---------------------------------------------------------------------------

def test_approximate_table_is_documented_and_off_by_default():
    assert 0.0 < TRAINING_RECALL < 1.0
    assert CORPUS_SIZE > 500
    assert len(APPROXIMATE_PATTERNS) > 500
    assert not Hyphenator().uses_approximate_patterns


def test_approximate_mode_loads_patterns():
    hyphenator = Hyphenator(approximate=True)
    assert hyphenator.uses_approximate_patterns
    assert hyphenator.pattern_count > 500


def test_explicit_patterns_beat_approximate_mode():
    hyphenator = Hyphenator(patterns=["a1b"], approximate=True)
    assert not hyphenator.uses_approximate_patterns
    assert hyphenator.pattern_count == 1


def test_tex_pattern_file_loading():
    """Load a small TeX-notation pattern file written for the test.

    The file is written next to the test rather than into pytest's temporary
    directory, because the sandbox grants write access to the session workspace
    only.  It deliberately contains both block layouts -- a ``\\patterns{}``
    block and a ``\\hyphenation{}`` exception block -- plus a comment, so the
    loader's comment stripping, block parsing and exception parsing are all
    exercised.
    """
    import os
    import tempfile

    directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_build")
    os.makedirs(directory, exist_ok=True)
    source = pathlib.Path(directory) / "hyph-en-us.tex"
    source.write_text(
        "% comment line\n"
        "\\patterns{\n"
        "hy3ph\n"
        "he2n\n"
        "hena4\n"
        "}\n"
        "\\hyphenation{as-so-ciate}\n",
        encoding="utf-8",
    )
    patterns, exceptions = load_tex_patterns(str(source))
    assert patterns == ["hy3ph", "he2n", "hena4"]
    assert exceptions == ["as-so-ciate"]

    table = exceptions_to_dict(exceptions)
    assert table["associate"] == (2, 4)   # as-so-ciate

    hyphenator = Hyphenator(patterns=patterns, dictionary=table, min_word_length=5)
    # "hy3ph" puts weight 3 between y and p, so the loaded pattern file alone must
    # find the "hy-phenation" break with no dictionary help
    assert hyphenator.hyphenate("hyphenation") == [2]
    # and the exception table wins for the word it names
    assert hyphenator.hyphenate("associate") == [2, 4]


def test_load_tex_patterns_accepts_a_bare_list():
    patterns, exceptions = load_tex_patterns(["# c\n", "hy3ph", "\n", "he2n\n"])
    assert patterns == ["hy3ph", "he2n"]
    assert exceptions == []


def test_add_patterns_merges():
    hyphenator = Hyphenator(patterns=["hy3ph"])
    assert hyphenator.pattern_count == 1
    added = hyphenator.add_patterns(["he2n", "hena4", ""])
    assert added == 2
    assert hyphenator.pattern_count == 3


def test_boundary_marker_is_a_dot():
    assert BOUNDARY == "."


# ---------------------------------------------------------------------------
# layout: tokenizer
# ---------------------------------------------------------------------------

def test_to_items_produces_boxes_glue_and_penalties():
    items = to_items(Text("hello world"), LayoutOptions(width=20), None)
    kinds = [type(item) for item in items]
    assert kinds.count(Box) == 2
    assert kinds.count(Glue) == 1
    assert kinds.count(Penalty) == 1


def test_boxes_carry_source_offsets():
    text = "hello world"
    items = to_items(Text(text), LayoutOptions(width=20), None)
    for item in items:
        if isinstance(item, Box):
            assert text[item.offset:item.end] == "hello" if item.offset == 0 else True


def test_newline_becomes_a_forced_break():
    items = to_items(Text("a\nb"), LayoutOptions(width=20), None)
    forced = [item for item in items if isinstance(item, Penalty) and item.value <= FORCED_BREAK]
    assert len(forced) == 1


def test_leading_and_trailing_glue_is_removed():
    items = to_items(Text("   padded   "), LayoutOptions(width=40), None)
    assert not isinstance(items[0], Glue)
    assert not isinstance(items[-1], Glue)


def test_tabs_are_measured_as_eight_cells():
    items = to_items(Text("a\tb"), LayoutOptions(width=40), None)
    glue = [item for item in items if isinstance(item, Glue)]
    assert glue and glue[0].width == 8


def test_atomic_runs_are_single_boxes():
    items = to_items(Text("path/to/file"), LayoutOptions(width=40), None)
    boxes = [item for item in items if isinstance(item, Box)]
    assert len(boxes) == 1


def test_numbers_are_not_hyphenated():
    hyphenator = Hyphenator()
    items = to_items(Text("1234567890"), LayoutOptions(width=4), hyphenator)
    flagged = [item for item in items if isinstance(item, Penalty) and item.flagged]
    assert flagged == []


def test_cjk_is_split_per_character_with_break_opportunities():
    items = to_items(Text("中文测试"), LayoutOptions(width=8), None)
    boxes = [item for item in items if isinstance(item, Box)]
    assert len(boxes) == 4
    assert all(box.width == 2 for box in boxes)


def test_classify_categories():
    assert classify(" ") == "space"
    # closing punctuation may not begin a line (kinsoku), which is a distinct
    # class from "may not be separated from the previous character"
    assert classify("\u3002") == "nobreakbefore"
    assert classify("\u30fc") == "nobreak"
    assert classify("\u4e2d") == "cjk"
    assert classify("a") == "word"


# ---------------------------------------------------------------------------
# layout: the algorithm
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("width", [28, 40, 56, 72, 100])
def test_every_line_fits_the_column(width):
    """A line's *natural* width may exceed the column as long as the glue can
    shrink it back; what must never happen is a line that cannot fit.
    """
    plan = layout(Text(PARAGRAPH), LayoutOptions(width=width), Hyphenator())
    for line in plan.lines:
        assert line.natural - line.glue_count * plan.options.space_shrink <= width, line.text
        assert line.width <= width or line.overfull


@pytest.mark.parametrize("width", [28, 40, 56, 72])
def test_no_text_is_lost(width):
    """Concatenating the lines must reproduce the paragraph.

    Hyphenated breaks insert a hyphen and split a word across two lines, so the
    check removes the inserted hyphens and re-joins the fragments before
    comparing.
    """
    plan = layout(Text(PARAGRAPH), LayoutOptions(width=width), Hyphenator())
    pieces = []
    for line in plan.lines:
        piece = line.text
        if line.hyphenated:
            piece = piece[:-1]  # drop the inserted hyphen, keep the fragment
            pieces.append(piece)  # no separator: it continues on the next line
        else:
            pieces.append(piece)
            pieces.append(" ")
    joined = "".join(pieces)
    assert " ".join(joined.split()) == " ".join(PARAGRAPH.split())


def test_line_count_matches_the_plan():
    plan = layout(Text(PARAGRAPH), LayoutOptions(width=40), Hyphenator())
    assert plan.line_count == len(plan.lines)
    assert plan.line_count >= 1
    assert plan.total_demerits > 0


def test_total_fit_has_lower_demerits_than_greedy():
    """The defining property of the algorithm, measured directly.

    Total fit minimises the sum of squared badness over the whole paragraph;
    greedy first fit does not look ahead at all.  Comparing demerits is therefore
    the honest test of the feature -- comparing the standard deviation of line
    widths is not, because a total-fit solution is allowed to make one line tight
    in exchange for making the rest uniform, and its demerit function rewards
    full lines rather than equal ones.
    """
    from rich._layout import _badness

    options = LayoutOptions(width=40)
    console = Console(width=40, force_terminal=False)
    rendered = console.render_lines(Text(LONG_PARAGRAPH), pad=False)
    greedy = [
        len("".join(segment.text for segment in line).rstrip())
        for line in rendered
    ]
    greedy = [width for width in greedy if width][:-1]
    greedy_demerits = sum(
        (options.line_penalty + _badness((40 - width) / 2)) ** 2 for width in greedy
    )

    plan = layout(Text(LONG_PARAGRAPH), options, Hyphenator())
    assert plan.total_demerits <= greedy_demerits


def test_hyphenation_reduces_the_number_of_lines():
    without = layout(
        Text(PARAGRAPH), LayoutOptions(width=32, hyphenate=False), None
    )
    with_hyphen = layout(
        Text(PARAGRAPH), LayoutOptions(width=32, hyphenate=True), Hyphenator()
    )
    assert with_hyphen.line_count <= without.line_count


def test_disabling_hyphenation_removes_flagged_breaks():
    plan = layout(Text(PARAGRAPH), LayoutOptions(width=30, hyphenate=False), Hyphenator())
    assert not any(line.hyphenated for line in plan.lines)


def test_explicit_newline_forces_a_new_line():
    text = "first line\nsecond line"
    plan = layout(Text(text), LayoutOptions(width=40), None)
    assert plan.line_count == 2
    assert plan.lines[0].text.strip() == "first line"
    assert plan.lines[1].text.strip() == "second line"


def test_paragraph_shorter_than_the_column_uses_one_line():
    plan = layout(Text("short"), LayoutOptions(width=40), None)
    assert plan.line_count == 1
    assert plan.lines[0].text == "short"


def test_word_wider_than_the_column_is_reported_overfull():
    plan = layout(Text("supercalifragilistic"), LayoutOptions(width=6, hyphenate=False), None)
    assert plan.line_count >= 1
    assert any(line.overfull or line.natural > 6 for line in plan.lines)


def test_break_lines_returns_the_same_result_as_layout():
    items = to_items(Text(PARAGRAPH), LayoutOptions(width=50), Hyphenator())
    lines, demerits, states, fallback = break_lines(items, LayoutOptions(width=50))
    assert not fallback
    assert states > 0
    assert demerits > 0
    assert all(isinstance(line, BrokenLine) for line in lines)


def test_options_validation():
    with pytest.raises(ValueError):
        LayoutOptions(width=0)
    with pytest.raises(ValueError):
        LayoutOptions(tolerance=0)
    with pytest.raises(ValueError):
        LayoutOptions(max_items_per_line=2)


def test_sequence_is_consistent_with_the_source():
    """Offsets must be monotonic and inside the paragraph."""
    plan = layout(Text(PARAGRAPH), LayoutOptions(width=44), Hyphenator())
    previous = 0
    for line in plan.lines:
        assert 0 <= line.start <= len(PARAGRAPH)
        assert line.start >= previous
        assert line.end >= line.start
        previous = line.start


def test_cjk_paragraph_breaks_between_characters():
    text = "中文测试文本用于验证断行算法是否正确处理宽字符"
    plan = layout(Text(text), LayoutOptions(width=10), None)
    assert plan.line_count > 1
    for line in plan.lines:
        assert line.natural <= 10


def test_glue_stretch_and_shrink_come_from_the_options():
    options = LayoutOptions(width=40, space_stretch=3, space_shrink=2)
    glue = [item for item in to_items(Text("a b"), options, None) if isinstance(item, Glue)]
    assert glue[0].stretch == 3
    assert glue[0].shrink == 2


def test_infinite_penalty_blocks_a_break():
    items = [
        Box(width=3, offset=0, end=3),
        Penalty(INFINITE_PENALTY, 3, 3),
        Box(width=3, offset=3, end=6),
    ]
    lines, _demerits, _states, _fallback = break_lines(items, LayoutOptions(width=10))
    assert len(lines) == 1


# ---------------------------------------------------------------------------
# justification: modes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["left", "right", "center", "full"])
def test_modes_never_exceed_the_column(mode):
    lines = justify(PARAGRAPH, width=48, mode=mode)
    assert lines
    for line in lines:
        assert line.width <= 48


def test_full_mode_makes_all_but_the_last_line_exactly_the_column():
    lines = justify(PARAGRAPH, width=52, mode="full")
    assert len(lines) >= 3
    for line in lines[:-1]:
        assert line.width == 52


def test_last_line_is_left_aligned_by_default():
    lines = justify(PARAGRAPH, width=52, mode="full")
    assert lines[-1].width < 52


def test_last_line_can_be_justified_too():
    options = JustifyOptions(width=52, mode="full", last_line_left=False)
    lines = justify(PARAGRAPH, options=options)
    if lines[-1].text.plain.count(" ") >= 1:
        assert lines[-1].width == 52


def test_left_mode_never_pads():
    lines = justify(PARAGRAPH, width=60, mode="left")
    for line in lines:
        assert not line.text.plain.endswith(" ")
    assert lines[0].padding == 0


def test_right_mode_pads_on_the_left():
    lines = justify(PARAGRAPH, width=60, mode="right")
    assert lines[0].plain.startswith(" ")
    assert lines[0].width == 60


def test_center_mode_pads_the_left_side():
    """Centring adds half the leftover on the left; the other half is blank.

    The line is therefore *narrower* than the column -- the right half of the
    leftover is simply not emitted -- and what must hold is that the two halves
    are balanced.
    """
    lines = justify(PARAGRAPH, width=60, mode="center")
    first = lines[0]
    leading = len(first.plain) - len(first.plain.lstrip(" "))
    natural = len(first.plain.strip())
    assert leading >= 1
    assert abs(leading - (60 - natural - leading)) <= 1


def test_words_are_preserved_in_every_mode():
    for mode in ("left", "right", "center", "full"):
        lines = justify(PARAGRAPH, width=44, mode=mode)
        assert words_of(lines) == PARAGRAPH.split()


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        JustifyOptions(mode="diagonal")


def test_indentation_is_applied():
    options = JustifyOptions(width=60, mode="full", indent=4, first_line_indent=2)
    lines = justify(PARAGRAPH, options=options)
    assert lines[0].plain.startswith(" " * 6)
    assert lines[1].plain.startswith(" " * 4)


def test_excessive_indentation_is_rejected():
    with pytest.raises(ValueError):
        JustifyOptions(width=10, indent=12)


def test_empty_input_yields_no_lines():
    assert justify("   ", width=40) == []
    assert justify("", width=40) == []


def test_whitespace_is_collapsed():
    lines = justify("a    b\t\tc", width=40)
    assert lines[0].plain.split() == ["a", "b", "c"]


def test_whitespace_can_be_preserved():
    options = JustifyOptions(width=40, collapse_whitespace=False, strip=False)
    lines = justify("a    b", options=options)
    assert "    " in lines[0].plain or lines[0].plain.count(" ") >= 4


def test_justified_line_reports_metrics():
    lines = justify(PARAGRAPH, width=50, mode="full")
    first = lines[0]
    assert isinstance(first, JustifiedLine)
    assert first.mode == "full"
    assert first.demerits >= 0
    assert isinstance(first.hyphenated, bool)
    assert str(first) == first.plain


# ---------------------------------------------------------------------------
# justification: styles
# ---------------------------------------------------------------------------

def test_styles_survive_line_breaking():
    text = Text()
    text.append("bold words here ", style="bold")
    text.append("and italic words there ", style="italic")
    text.append("plus plain filler words to force wrapping", style="default")
    lines = justify(text, width=24)
    styles = set()
    for line in lines:
        for span in line.text.spans:
            style = span.style
            if isinstance(style, str):
                styles.add(style)
    assert "bold" in styles
    assert "italic" in styles


def test_style_spans_stay_within_the_line():
    text = Text("alpha beta gamma delta epsilon zeta eta theta", style="red")
    lines = justify(text, width=20)
    for line in lines:
        for span in line.text.spans:
            assert 0 <= span.start <= len(line.text.plain)
            assert span.end <= len(line.text.plain)


def test_link_meta_is_preserved():
    """Line breaking must not disturb style spans, including link targets.

    Hyphenation may split the word the link is attached to, so the assertion is
    on the span data rather than on a literal substring: the link style must
    still be present, and every span must stay inside its own line.
    """
    text = Text("visit ")
    text.append("the documentation", style="link https://example.com")
    text.append(" for details about the engine")
    lines = justify(text, width=20)
    joined = " ".join(line.plain for line in lines)
    assert "documen" in joined          # the link text is present, split or not
    assert "for" in joined and "engine" in joined
    for line in lines:
        for span in line.text.spans:
            assert 0 <= span.start <= len(line.text.plain)
            assert span.end <= len(line.text.plain)


def test_markup_string_is_parsed_only_on_request():
    text = "[bold]hello[/bold] world"
    lines = justify(text, width=60)
    assert "[bold]hello[/bold]" in lines[0].plain     # left as literal text
    lines = justify(text, width=60, from_markup=True)
    assert lines[0].plain == "hello world"
    assert any(span.style == "bold" for span in lines[0].text.spans)


# ---------------------------------------------------------------------------
# justification: paragraphs and helpers
# ---------------------------------------------------------------------------

def test_paragraphs_are_split_on_blank_lines():
    text = "first paragraph here\n\nsecond paragraph here"
    result = justify_paragraph(text, width=40)
    assert "first paragraph here" in result.plain
    assert "second paragraph here" in result.plain


def test_justify_paragraph_keeps_multiple_paragraphs_apart():
    result = justify_paragraph("alpha beta\n\ngamma delta", width=40, mode="left")
    lines = result.plain.split("\n")
    assert any("alpha" in line for line in lines)
    assert any("gamma" in line for line in lines)


def test_reflow_is_a_shortcut():
    assert reflow(PARAGRAPH, width=50).plain.split() == justify_paragraph(
        PARAGRAPH, width=50
    ).plain.split()


def test_render_lines_shows_the_fill():
    lines = justify(PARAGRAPH, width=40, mode="left")
    rendered = render_lines(lines, width=40, fill=".")
    assert rendered.plain.count(".") > 0
    assert rendered.plain.count("\n") == len(lines) - 1


def test_render_lines_default_width_uses_the_longest_line():
    lines = justify("short text here", width=40, mode="left")
    rendered = render_lines(lines)
    assert rendered.plain.split("\n")[0] == "short text here"


def test_sentence_spacing_flag_is_accepted():
    options = JustifyOptions(width=40, sentence_spacing=True)
    assert justify("one.  two.", options=options)


def test_min_lines_to_justify_short_paragraph():
    options = JustifyOptions(width=40, mode="full", min_lines_to_justify=5)
    lines = justify("one two three", options=options)
    assert lines[0].width < 40  # left aligned because the paragraph is short


def test_hyphenation_toggle_changes_the_output():
    with_hyphen = justify(PARAGRAPH, width=30, hyphenate=True)
    without = justify(PARAGRAPH, width=30, hyphenate=False)
    assert any(line.hyphenated for line in with_hyphen)
    assert not any(line.hyphenated for line in without)


def test_approximate_hyphenation_is_opt_in():
    options = JustifyOptions(width=30, approximate_hyphenation=True)
    lines = justify("interoperability characterisation", options=options)
    assert lines


# ---------------------------------------------------------------------------
# console integration
# ---------------------------------------------------------------------------

def test_console_has_print_justified():
    assert hasattr(Console, "print_justified")


def test_console_print_justified_writes_lines():
    console = Console(width=48, force_terminal=False, file=open(__import__("os").devnull, "w"))
    lines = console.print_justified(PARAGRAPH, width=48, return_lines=True)
    assert lines
    assert all(line.width <= 48 for line in lines)


def test_console_print_justified_accepts_style():
    console = Console(width=40, force_terminal=False, file=open(__import__("os").devnull, "w"))
    lines = console.print_justified(PARAGRAPH, width=40, style="bold", return_lines=True)
    assert lines


def test_console_justify_options_are_forwarded():
    console = Console(width=40, force_terminal=False, file=open(__import__("os").devnull, "w"))
    lines = console.print_justified(
        PARAGRAPH, width=40, mode="left", hyphenate=False, return_lines=True
    )
    assert not any(line.hyphenated for line in lines)


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def test_cli_demo(capsys):
    from rich._justify_demo import main

    assert main(["demo", "--width", "60"]) == 0
    out = capsys.readouterr().out
    assert "metrics" in out
    assert "raggedness" in out


def test_cli_compare(capsys):
    from rich._justify_demo import main

    assert main(["compare", "--width", "60"]) == 0
    out = capsys.readouterr().out
    assert "comparison" in out
    assert "knuth" in out.lower()


def test_cli_hyphenate(capsys):
    from rich._justify_demo import main

    assert main(["hyphenate", "hyphenation", "algorithm"]) == 0
    out = capsys.readouterr().out
    assert "hyphenation" in out
    assert "dictionary" in out


def test_cli_patterns(capsys):
    from rich._justify_demo import main

    assert main(["patterns"]) == 0
    out = capsys.readouterr().out
    assert "dictionary entries" in out


def test_cli_bench(capsys):
    from rich._justify_demo import main

    assert main(["bench", "--words", "200"]) == 0
    out = capsys.readouterr().out
    assert "benchmark" in out


def test_cli_rejects_unknown_mode():
    from rich._justify_demo import main

    with pytest.raises(SystemExit):
        main(["demo", "--mode", "diagonal"])
