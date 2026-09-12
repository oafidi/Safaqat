"""Running headers, footers and page numbers -- and getting rid of them.

The layout model labels *some* of this (`header`, `footer`, `number`), but it
misses a great deal: the marché reference printed above the rule on every page,
the "Page 12 / 48" in the corner, the ministry's name repeated at the top of a
scan. Those come back labelled `text`, indistinguishable from a sentence, and
they land in the Markdown 48 times.

That is not merely untidy. Each repetition is a paragraph in the block list, so
a chunker either embeds 48 near-identical chunks, or embeds them *inside* real
chunks, where they interrupt a sentence that spans a page break. None of it
carries information -- the reference is in the document's own title, and no one
retrieves "Page 12 / 48".

So a second pass finds them by what actually distinguishes them: **position and
repetition**. A block is furniture when it

1. sits in the top or bottom margin band of its page,
2. is short, and
3. appears in that band on enough pages to be a running fixture.

Rule 3 is what keeps a real sentence safe: a paragraph that happens to be printed
near the bottom of one page is printed there once. A running footer is printed
there on every page.

Two wrinkles that rule 3 alone would miss:

* **A footer with the page number baked into it** ("Marché 32/2026 -- page 3/48")
  is a *different string* on every page, so it never repeats exactly. Near-
  identical variants are therefore clustered (:func:`_cluster`) and their pages
  pooled, which is what makes the family look repeated even though no single
  member does.
* **A bare page number** ("12", "13") repeats no better, and is too short for
  fuzzy matching to link to "13" with any confidence. It is recognised outright
  by :func:`_is_page_number` -- in a margin band, a block that is nothing but
  digits and separators is a page number and nothing else.

Everything detected here is *dropped* from the output, not flagged: see
:func:`strip_furniture`.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from difflib import SequenceMatcher

from .schema import Block, Page

log = logging.getLogger("docparser")

# Block types that can be furniture at all. A table or a figure in a margin is
# not a running header, and dropping one would lose real content.
_ELIGIBLE = frozenset(
    {"paragraph", "title", "header", "footer", "page_number", "reference", "other"}
)

# "12", "- 12 -", "12/48", "Page 12 / 48", "p. 12". Roman numerals included:
# front matter is often numbered i, ii, iii.
_PAGE_NUMBER = re.compile(
    r"^(page|p\.?|pg\.?)?\s*[\[\(\-–—]*\s*[\divxlcdm]+\s*(/|sur|of|de)?\s*[\divxlcdm]*\s*[\]\)\-–—.]*$",
    re.IGNORECASE,
)


def strip_furniture(
    blocks: list[Block],
    pages: list[Page],
    min_pages: int | None = None,
    max_len: int = 120,
    margin: float = 0.12,
    fuzzy: float = 0.80,
) -> list[Block]:
    """Mark every furniture block and return the list with them removed.

    Args:
        blocks: the document's blocks, in reading order.
        pages: used for their heights, to locate the margin bands.
        min_pages: how many distinct pages a text must appear on, in a margin
            band, to count as a running fixture. Defaults to 30% of the
            document, never fewer than 2 -- repetition cannot be observed on a
            single page.
        max_len: a running header is short. Anything longer is prose.
        margin: height of the top and bottom bands, as a fraction of the page.
        fuzzy: similarity above which two texts are variants of one another.
    """
    heights = {p.number: p.height for p in pages}
    banded = [b for b in blocks if _in_margin(b, heights, margin)]

    # What the layout model already knew: `header`, `footer`, `number` labels.
    by_label = sum(1 for b in blocks if b.is_furniture)

    for block in banded:
        if block.type in _ELIGIBLE and _is_page_number(block.text):
            block.is_furniture = True

    repeated = _repeated_texts(banded, len(pages), min_pages, max_len, fuzzy)
    for block in banded:
        # Only the occurrences *in the margin* are furniture. The same words in
        # the body of a page are a sentence, and stay.
        if block.type in _ELIGIBLE and _normalise(block.text) in repeated:
            block.is_furniture = True

    kept = [b for b in blocks if not b.is_furniture]
    dropped = len(blocks) - len(kept)
    if dropped:
        # The split is worth reporting: it says whether this pass is earning its
        # keep on a given document, or whether the layout model had it covered.
        log.info(
            "furniture: dropped %d block(s) -- %d labelled by the layout model, "
            "%d found by position + repetition",
            dropped, by_label, dropped - by_label,
        )
    # This pass deletes content, so what it found on its own is named, not just
    # counted. A false positive here is a lost sentence, and the only way anyone
    # notices is if the log says what went.
    for text in sorted(repeated):
        log.info("furniture: repeated margin text dropped: %r", text[:90])
    return kept


def _repeated_texts(
    banded: list[Block],
    n_pages: int,
    min_pages: int | None,
    max_len: int,
    fuzzy: float,
) -> set[str]:
    """The normalised texts that recur, in a margin band, across enough pages."""
    if n_pages < 2:
        return set()  # nothing can be shown to repeat
    if min_pages is None:
        min_pages = max(2, round(n_pages * 0.3))

    seen_on: dict[str, set[int]] = defaultdict(set)
    for block in banded:
        if block.type not in _ELIGIBLE:
            continue
        text = _normalise(block.text)
        if text and len(text) <= max_len:
            seen_on[text].add(block.page)

    furniture = {t for t, pages in seen_on.items() if len(pages) >= min_pages}

    # A footer carrying the page number is a different string on every page, so
    # no single variant repeats. Pool the pages of each family of variants.
    for family in _cluster(list(seen_on), fuzzy):
        if len(family) < 2:
            continue
        pooled: set[int] = set()
        for text in family:
            pooled |= seen_on[text]
        if len(pooled) >= min_pages:
            furniture |= set(family)

    return furniture


def _cluster(texts: list[str], threshold: float) -> list[list[str]]:
    """Group near-identical texts. Union-find; O(n^2), but n is small here."""
    parent = list(range(len(texts)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            if _similar(texts[i], texts[j]) >= threshold:
                parent[find(j)] = find(i)

    families: dict[int, list[str]] = defaultdict(list)
    for i, text in enumerate(texts):
        families[find(i)].append(text)
    return list(families.values())


def _similar(a: str, b: str) -> float:
    # Two texts of very different lengths cannot be variants of each other, and
    # SequenceMatcher is the expensive part of this module.
    if min(len(a), len(b)) / max(len(a), len(b), 1) < 0.6:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _in_margin(block: Block, heights: dict[int, float], margin: float) -> bool:
    """Does the block start in the top band, or end in the bottom band?"""
    height = heights.get(block.page)
    if not height:
        return False
    return block.bbox[1] < height * margin or block.bbox[3] > height * (1 - margin)


def _is_page_number(text: str) -> bool:
    text = text.strip()
    return bool(text) and len(text) <= 24 and bool(_PAGE_NUMBER.match(text))


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()
