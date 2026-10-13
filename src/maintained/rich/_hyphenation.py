"""Pattern-based hyphenation: the Liang algorithm, pattern loading and the
hyphenator used by Rich's paragraph layout engine.

This module is part of the *justification* feature added on top of upstream
Rich (see ``docs/justification.md``).  It is import-light and has no dependency
beyond the standard library, so it can also be used standalone.

Design overview
---------------
``Hyphenator`` is the only class most callers need::

    from rich._hyphenation import Hyphenator

    h = Hyphenator()                       # bundled dictionary only
    h.hyphenate("hyphenation")             # [2, 6]
    h.points("hyphenation", "hyphenation") # [2, 6]

Hyphenation quality has three tiers, in decreasing accuracy:

1. **dictionary** -- a curated table of words with exact break positions
   (:data:`rich._hyphenation_dict.HYPHENATION_DICT`).  Exact by construction.
2. **patterns** -- Liang patterns loaded from a real TeX pattern file with
   :func:`load_tex_patterns`.  Accurate for arbitrary words and the tier used by
   typesetting systems.
3. **approximate patterns** -- the derived table bundled in
   :data:`rich._hyphenation_patterns.APPROXIMATE_PATTERNS`, used only when
   ``approximate=True``.  See that module for the measured accuracy; it is a
   deliberate, documented fallback and is off by default.

The Liang algorithm
-------------------
A *pattern* is a string of letters with single digit weights interleaved, where
a weight may also sit at the very start or the very end::

    "hy3ph"   the weight 3 sits between "y" and "p"
    "2ion"    the weight 2 sits before "i"
    "n2t."    the weight 2 sits between "n" and "t", the dot anchors the end

Hyphenating a word ``w``:

1. lower-case ``w`` and wrap it in boundary markers: ``.w.``;
2. for every pattern, find every occurrence inside the wrapped word and write
   the pattern's weights into the gap array of the wrapped word, keeping the
   **maximum** weight per gap;
3. a gap whose final weight is **odd** is a legal break.

Only gaps strictly inside the word count, and the left/right minima are applied
afterwards.  The "maximum wins / odd breaks" rule makes the result independent
of the order in which patterns are applied, which is what allows several pattern
files to be merged safely.

Performance
-----------
A naive scan is O(patterns x word length).  Real TeX pattern sets have ~4500
patterns and a paragraph has thousands of words, so :class:`_PatternTrie`
stores the letters in a trie and walks it from every start position, giving
O(word length x max pattern length) lookups with no per-pattern string search.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from ._hyphenation_dict import HYPHENATION_DICT
from ._hyphenation_patterns import APPROXIMATE_PATTERNS, TRAINING_RECALL

__all__ = [
    "Hyphenator",
    "HyphenationError",
    "load_tex_patterns",
    "parse_pattern",
    "BOUNDARY",
    "DIGITS",
]


#: Word-boundary marker used by Liang's algorithm.  Real words never contain a
#: dot here because the hyphenator works on single words, and using "." keeps the
#: notation identical to TeX's ``hyph-utf8`` text files.
BOUNDARY = "."

DIGITS = "0123456789"

#: Anything that is not a letter is treated as a word separator.  ``-`` is a
#: separator as well so that already hyphenated input is not re-hyphenated.
_WORD_SPLIT = re.compile(r"[^\w\u0080-\uffff]+", re.UNICODE)
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


class HyphenationError(ValueError):
    """Raised for malformed pattern input."""


def parse_pattern(pattern: str) -> Tuple[str, Tuple[int, ...], bool, bool]:
    """Split one TeX-notation pattern into letters, weights and anchors.

    Returns ``(letters, weights, anchored_left, anchored_right)``.  A boundary dot
    is kept in ``letters`` so that it matches the dot :func:`_gap_scores` wraps
    around the word, which is what anchors a leading-dot pattern to the start of a
    word.  A dot *after* the last letter is a pure anchor and is dropped from the
    key so that ``"n2t."`` and ``"n2t"`` merge in the trie.

    ``weights`` always has exactly ``len(letters)`` entries and ``weights[i]`` is
    the weight of the gap *before* ``letters[i + 1]``; the last entry is the gap
    after the final letter, which only right-anchored patterns use.  A weight
    written before the first letter has no gap to attach to, so it is recorded as
    the left-anchor flag instead.

    That length invariant is what :meth:`_PatternTrie.walk` relies on: it yields
    the number of letters it matched and the caller writes ``weights[i]`` to
    ``scores[start + i]``.

    >>> parse_pattern("hy3ph")
    ('hyph', (0, 3, 0, 0), False, False)
    >>> parse_pattern(".ach4")
    ('.ach', (0, 0, 4), True, False)
    >>> parse_pattern("n2t.")
    ('nt', (0, 2), False, True)
    """
    # ---- 1. tokenize -------------------------------------------------------
    # Everything that is not a digit is a token by itself, so letters and
    # boundary dots share one code path.  A multi-digit run is not valid TeX
    # notation; tolerating it by keeping the last digit is friendlier than
    # raising, and keeps the parser total.
    stream: List[str] = []
    position = 0
    length = len(pattern)
    while position < length:
        char = pattern[position]
        if char in DIGITS:
            number = char
            while position + 1 < length and pattern[position + 1] in DIGITS:
                position += 1
                number = pattern[position]
            stream.append(number)
        else:
            stream.append(char)
        position += 1

    # ---- 2. build the key --------------------------------------------------
    # A weight always applies to the gap that *follows* the token before it,
    # which is exactly how TeX reads the notation: "hy3ph" puts 3 in the gap
    # after "y".
    letters: List[str] = []
    gaps: List[int] = []
    pending = 0
    anchored_left = False
    anchored_right = False

    #: index of the letter in front of the last gap, for error messages
    for token in stream:
        if token in DIGITS:
            if not letters:
                # A weight before any letter cannot describe a gap; the only
                # meaning it can carry is "never break before the first letter".
                anchored_left = True
                continue
            # A digit always belongs to the gap that follows the letter before it.
            # That gap is already the newest entry of ``gaps``, so the weight is
            # written straight into it.  Deferring the digit instead would push it
            # one letter too far to the right.
            gaps[-1] = int(token)
            continue
        if token == BOUNDARY and not letters:
            # A leading dot stays in the key: :func:`_gap_scores` wraps the word
            # in dots, so keeping it makes the pattern match only at the start of
            # a word.  It is also an explicit left anchor.
            anchored_left = True
        letters.append(token)
        gaps.append(0)

    # a trailing dot anchors the pattern to the end of the word without being
    # part of the key; a *leading* dot is kept, see above
    if len(letters) > 1 and letters[-1] == BOUNDARY:
        letters.pop()
        if gaps:
            gaps.pop()
        anchored_right = True

    if not letters or all(letter == BOUNDARY for letter in letters):
        raise HyphenationError(f"pattern {pattern!r} has no letters")
    while len(gaps) < len(letters):
        gaps.append(0)
    # A weight written before the first letter anchors the pattern instead of
    # filling a gap, so expose it there rather than leaving a misleading zero.
    if anchored_left and gaps and not any(gaps):
        pass
    return "".join(letters), tuple(gaps), anchored_left, anchored_right


class _PatternTrie:
    """Trie over pattern letters carrying the weights at each node.

    A node exists for every prefix of every pattern.  ``weights`` is only
    non-empty for nodes that terminate a pattern.  Walking the trie from each
    start position of the wrapped word is what makes application fast.
    """

    __slots__ = ("children", "weights", "terminal")

    def __init__(self) -> None:
        self.children: Dict[str, "_PatternTrie"] = {}
        self.weights: Tuple[int, ...] = ()
        self.terminal = False

    def add(self, letters: str, weights: Sequence[int]) -> None:
        node = self
        for char in letters:
            node = node.children.setdefault(char, _PatternTrie())
        if weights and (
            not node.terminal or tuple(weights) > node.weights
        ):
            # two patterns with the same letters cannot occur in a well formed
            # file; if they do, keep the larger weight per gap
            if node.terminal and len(node.weights) == len(weights):
                merged = tuple(max(a, b) for a, b in zip(node.weights, weights))
                node.weights = merged
            else:
                node.weights = tuple(weights)
        node.terminal = True

    def walk(self, text: str, start: int) -> Iterator[Tuple[int, Tuple[int, ...]]]:
        """Yield ``(end_index, weights)`` for every pattern starting at ``start``."""
        node = self
        index = start
        length = len(text)
        while index < length:
            node = node.children.get(text[index])
            if node is None:
                return
            index += 1
            if node.terminal:
                yield index, node.weights


def _gap_scores(word: str, trie: _PatternTrie) -> List[int]:
    """Apply the trie to ``word`` and return the gap scores of ``.word.``."""
    wrapped = BOUNDARY + word + BOUNDARY
    scores = [0] * (len(wrapped) + 1)
    for start in range(len(wrapped)):
        for end, weights in trie.walk(wrapped, start):
            for offset, weight in enumerate(weights):
                position = start + offset
                if weight > scores[position]:
                    scores[position] = weight
    return scores


def _breaks_from_scores(scores: Sequence[int]) -> List[int]:
    """Convert gap scores of ``.word.`` into offsets inside the bare word.

    ``scores`` is indexed over ``"." + word + "."``, so ``scores[i]`` is the gap
    that follows ``word[i - 1]``.  A gap is a break when its score is odd, and it
    is a real offset inside the word when ``1 <= i <= len(word)``: ``i == 0`` is
    before the leading dot and ``i == len(word) + 1`` is after the trailing one.
    The returned value is the number of characters that stay on the first line,
    which equals ``i``.
    """
    if not scores:
        return []
    word_length = len(scores) - 2
    points: List[int] = []
    for index in range(1, word_length + 1):
        if scores[index] % 2 == 1:
            points.append(index)
    return points


class Hyphenator:
    """Hyphenate words using a dictionary plus an optional Liang pattern table.

    The constructor never fails: with no arguments it uses the bundled
    dictionary and no patterns, which is accurate but limited to known words.

    Args:
        patterns: TeX-notation pattern strings.  Replaces the trie entirely.
        dictionary: word -> iterable of break offsets.  Defaults to the bundled
            validated dictionary.
        approximate: consult
            :data:`rich._hyphenation_patterns.APPROXIMATE_PATTERNS` when no
            patterns were supplied.  Off by default because that table is only
            ~44% accurate; see the module docstring.
        left: minimum characters before the first break.
        right: minimum characters after the last break.
        min_word_length: words shorter than this are never hyphenated.
        hyphen: the character inserted at break points.

    Example:
        >>> h = Hyphenator()
        >>> h.points("justification", "justification")
        [3, 5, 7, 9]
    """

    __slots__ = (
        "_trie",
        "_dict",
        "_approximate",
        "left",
        "right",
        "min_word_length",
        "hyphen",
        "_cache",
    )

    def __init__(
        self,
        patterns: Optional[Iterable[str]] = None,
        dictionary: Optional[Mapping[str, Iterable[int]]] = None,
        *,
        approximate: bool = False,
        left: int = 2,
        right: int = 3,
        min_word_length: int = 5,
        hyphen: str = "-",
    ) -> None:
        self.left = left
        self.right = right
        self.min_word_length = min_word_length
        self.hyphen = hyphen
        self._cache: Dict[Tuple[str, bool], Tuple[int, ...]] = {}

        self._trie = _PatternTrie()
        self._approximate = False
        if patterns is not None:
            self.load_patterns(patterns)
        elif approximate:
            self.load_patterns(APPROXIMATE_PATTERNS)
            self._approximate = True

        if dictionary is None:
            self._dict: Mapping[str, Iterable[int]] = HYPHENATION_DICT
        else:
            self._dict = dictionary

    # ------------------------------------------------------------------ setup
    @property
    def uses_approximate_patterns(self) -> bool:
        """True when only the low-accuracy bundled table is active."""
        return self._approximate

    @property
    def pattern_count(self) -> int:
        """Number of patterns currently loaded."""

        def count(node: _PatternTrie) -> int:
            total = 1 if node.terminal else 0
            for child in node.children.values():
                total += count(child)
            return total

        return count(self._trie)

    def load_patterns(self, patterns: Iterable[str]) -> None:
        """Add TeX-notation patterns, replacing the current trie."""
        self._trie = _PatternTrie()
        self._cache.clear()
        self._approximate = False
        self.add_patterns(patterns)

    def add_patterns(self, patterns: Iterable[str]) -> int:
        """Merge more TeX-notation patterns into the trie; return how many.

        Because application is order independent, merging a second pattern file
        (for example a domain-specific supplement) is safe.
        """
        count = 0
        for pattern in patterns:
            pattern = pattern.strip()
            if not pattern or pattern.startswith("#"):
                continue
            letters, weights, _left, _right = parse_pattern(pattern)
            self._trie.add(letters, weights)
            count += 1
        self._cache.clear()
        return count

    # ------------------------------------------------------------ hyphenation
    def known(self, word: str) -> bool:
        """True when the dictionary has an exact answer for ``word``."""
        return word.lower() in self._dict

    def hyphenate(self, word: str) -> List[int]:
        """Return the legal break offsets inside ``word``.

        A break at offset ``p`` leaves ``p`` characters on the first line and
        ``len(word) - p`` on the second, so the legal range is
        ``left <= p <= len(word) - right``.  Words shorter than
        ``min_word_length`` are never considered, which keeps the common case of
        short words cheap.
        """
        lowered = word.lower()
        if len(lowered) < self.min_word_length:
            return []
        cached = self._cache.get((lowered, False))
        if cached is not None:
            return list(cached)

        points: List[int] = []
        exact = self._dict.get(lowered)
        if exact is not None:
            points = [int(p) for p in exact]
        elif self._trie.children or self._trie.terminal:
            points = _breaks_from_scores(_gap_scores(lowered, self._trie))

        limit = len(lowered) - self.right
        points = [p for p in points if self.left <= p <= limit]
        points = sorted(set(points))
        self._cache[(lowered, False)] = tuple(points)
        return list(points)

    def points(self, word: str, original: Optional[str] = None) -> List[int]:
        """Alias of :meth:`hyphenate` kept for API symmetry."""
        return self.hyphenate(word if original is None else original)

    def break_word(self, word: str) -> List[str]:
        """Split ``word`` at every legal break, hyphen included.

        >>> Hyphenator().break_word("hyphenation")
        ['hy-', 'phen-', 'ation']
        """
        points = self.hyphenate(word)
        if not points:
            return [word]
        pieces: List[str] = []
        previous = 0
        for point in points:
            pieces.append(word[previous:point] + self.hyphen)
            previous = point
        pieces.append(word[previous:])
        return pieces

    def variants(self, word: str) -> List[str]:
        """All hyphenation variants of ``word`` as whole words.

        Each variant replaces exactly one break with the hyphen character, which
        is what a line breaker needs: it considers "use the whole word" plus one
        variant per candidate break.

        >>> Hyphenator().variants("hyphenation")
        ['hyphenation', 'hy-phenation', 'hyphen-ation']
        """
        variants = [word]
        for point in self.hyphenate(word):
            variants.append(word[:point] + self.hyphen + word[point:])
        return variants

    def split_words(self, text: str) -> List[str]:
        """Split text into candidate words (letters only, digits excluded)."""
        return [word for word in _WORD_SPLIT.split(text) if _HAS_LETTER.search(word)]


def load_tex_patterns(
    source: str | Iterable[str],
    *,
    encoding: str = "utf-8",
) -> Tuple[List[str], List[str]]:
    """Load a real TeX ``hyph-utf8`` pattern file.

    Args:
        source: a path to the pattern file, or an iterable of lines.
        encoding: file encoding, ``utf-8`` by default.

    Returns:
        ``(patterns, exceptions)`` where ``exceptions`` are the entries of the
        optional ``\\hyphenation{}`` block with hyphens already inserted.

    The loader understands the two block layouts used by ``hyph-utf8`` text
    files: a bare list of patterns, or ``\\patterns{...}`` and
    ``\\hyphenation{...}`` blocks, with ``%`` comments and arbitrary whitespace.
    """
    if isinstance(source, str):
        with open(source, "r", encoding=encoding) as handle:
            text = handle.read()
    else:
        text = "\n".join(source)

    # strip comments: both TeX's "%" and the "#" used by some derived files
    lines = []
    for line in text.splitlines():
        for marker in ("%", "#"):
            comment = line.find(marker)
            if comment != -1:
                line = line[:comment]
        lines.append(line)
    body = "\n".join(lines)

    patterns: List[str] = []
    exceptions: List[str] = []

    for match in re.finditer(r"\\patterns\s*\{(.*?)\}", body, re.S):
        patterns.extend(match.group(1).split())
    for match in re.finditer(r"\\hyphenation\s*\{(.*?)\}", body, re.S):
        exceptions.extend(match.group(1).split())

    if not patterns:
        # bare list layout: every whitespace separated token is a pattern
        for token in body.split():
            if token.startswith("\\"):
                continue
            patterns.append(token)

    return patterns, exceptions


def exceptions_to_dict(exceptions: Iterable[str]) -> Dict[str, Tuple[int, ...]]:
    """Convert ``\\hyphenation`` entries (``hy-phen-ation``) into a mapping."""
    table: Dict[str, Tuple[int, ...]] = {}
    for entry in exceptions:
        clean = entry.replace("-", "")
        points: List[int] = []
        position = 0
        for char in entry:
            if char == "-":
                points.append(position)
            else:
                position += 1
        table[clean.lower()] = tuple(points)
    return table
