"""The data model, and the JSON it serialises to.

There are two different things here, and the difference is deliberate.

**In memory**, a block is rich: bboxes, layout labels, confidences, a table's
full cell grid with its spans. The pipeline needs all of it -- the table merger
reads bboxes and spans, the renderer reads levels, the furniture pass reads
positions.

**On disk**, the JSON is deliberately thin: it holds what a *chunker* needs and
nothing else. Every field earns its place by answering a question a chunker has
to ask:

* ``type``          -- is this a table? then keep it whole.
* ``text``          -- what do I embed? For a table this is the whole table as
                       Markdown; for a captioned figure, the caption. Every
                       block type answers the same way, so a chunker never has
                       to special-case one.
* ``section_path``  -- the heading trail above the block, e.g.
                       ``["CHAPITRE II", "Article 5 - Delais"]``. Prepend it to
                       a chunk and a retrieved fragment still says where it came
                       from.
* ``page``          -- for a citation back to the PDF (``pages``, when a table
                       spans several).
* ``token_estimate``-- to pack chunks to a budget.

What is *not* in the JSON is as important: no bboxes, no layout labels, no
confidences, no HTML, no cell grid, and no furniture. Page headers and footers
are dropped outright (see :mod:`docparser.furniture`) -- they carry nothing, and
a chunker that has to filter them is a chunker that can get it wrong.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

BBox = tuple[float, float, float, float]  # (x0, y0, x1, y1), PDF points, origin top-left

BlockType = Literal[
    "title",
    "paragraph",
    "table",
    "image",
    "image_caption",
    "formula",
    "list",
    "header",
    "footer",
    "page_number",
    "reference",
    "other",
]


@dataclass
class Cell:
    """One cell of a table, including its span. Internal: never serialised."""

    row: int
    col: int
    row_span: int
    col_span: int
    text: str
    is_header: bool = False


@dataclass
class Table:
    """A table. What reaches the JSON is its Markdown, via the block's ``text``.

    The cell grid stays in memory because :mod:`docparser.merge` needs the spans
    to stitch fragments back together, and because the Markdown is generated
    from it -- but it is not written out. A chunker cannot do anything with a
    span that it cannot already do with the Markdown, where a merged value is
    *repeated* across every cell it covers rather than left blank.
    """

    n_rows: int
    n_cols: int
    cells: list[Cell]
    markdown: str
    """GFM, with merged values repeated. This is the table, as far as the rest
    of the world is concerned."""

    html: str = ""
    """Source HTML from the layout model, kept for debugging. Not serialised."""

    has_merged_cells: bool = False

    n_fragments: int = 1
    """How many page-fragments were stitched into this table (:mod:`docparser.merge`)."""

    pages: list[int] = field(default_factory=list)
    """Pages the table spans. Only set when ``n_fragments > 1``."""


@dataclass
class Figure:
    """A picture region, after classification and (optionally) captioning.

    Crops are never written to disk and never referenced from the Markdown: an
    image path is not something a language model can read. A figure earns its
    place in the output only by being *described* -- so a figure with a caption
    becomes a block of text, and one without (a logo, a rule, a barcode)
    disappears.
    """

    figure_class: str
    """One of the 26 DocumentFigureClassifier-v2.5 labels."""

    confidence: float
    caption: str | None = None
    captioned: bool = False
    is_signature: bool = False
    """True for the 'signature' and 'stamp' classes: the evidence behind
    ``Document.is_signed``."""


@dataclass
class Block:
    """A single unit of content, in reading order."""

    id: str
    type: BlockType
    page: int
    order: int
    bbox: BBox
    text: str = ""
    level: int | None = None
    """Heading depth (1 = document title). Only set for titles."""

    section_path: list[str] = field(default_factory=list)
    is_furniture: bool = False
    layout_label: str = ""
    confidence: float | None = None
    markdown_span: tuple[int, int] | None = None
    token_estimate: int = 0
    table: Table | None = None
    figure: Figure | None = None

    def to_dict(self) -> dict[str, Any]:
        """The chunker's view: what to embed, and where it came from."""
        d: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "page": self.page,
            "section_path": self.section_path,
            "text": self.text,
            "token_estimate": self.token_estimate,
        }
        if self.level is not None:
            d["level"] = self.level
        if self.table is not None and self.table.n_fragments > 1:
            # The table was split across a page break and stitched back together;
            # `page` is where it starts, these are all the pages it covers.
            d["pages"] = self.table.pages
        return d


@dataclass
class Page:
    number: int
    width: float
    height: float
    is_scanned: bool
    language_hint: str
    n_blocks: int = 0


@dataclass
class Document:
    """Everything the parser knows about one PDF."""

    source: str
    n_pages: int
    is_scanned: bool
    language_hint: str
    is_signed: bool
    blocks: list[Block]
    pages: list[Page]
    signature_evidence: list[dict[str, Any]] = field(default_factory=list)
    """Which figures triggered ``is_signed``, with page and class, so the flag
    can be audited. In memory only."""

    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "n_pages": self.n_pages,
            "is_scanned": self.is_scanned,
            "language": self.language_hint,
            "is_signed": self.is_signed,
            "blocks": [b.to_dict() for b in self.blocks],
        }


class ParseResult:
    """What :meth:`docparser.DocumentParser.parse` gives back."""

    def __init__(self, document: Document, markdown: str):
        self.document = document
        self._markdown = markdown

    # --- the public surface --------------------------------------------
    @property
    def markdown(self) -> str:
        return self._markdown

    @property
    def json(self) -> dict[str, Any]:
        return self.document.to_dict()

    @property
    def is_scanned(self) -> bool:
        return self.document.is_scanned

    @property
    def language_hint(self) -> str:
        """``"fr"``, ``"ar"`` or ``"mixed"``."""
        return self.document.language_hint

    @property
    def is_signed(self) -> bool:
        """True if any figure was classified as a signature or a stamp."""
        return self.document.is_signed

    @property
    def blocks(self) -> list[Block]:
        return self.document.blocks

    def save(self, output_dir: str | Path) -> dict[str, Path]:
        """Write ``output.md`` and ``output.json``. Returns the paths written."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        md_path = out / "output.md"
        json_path = out / "output.json"
        md_path.write_text(self._markdown, encoding="utf-8")
        json_path.write_text(
            json.dumps(self.json, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"markdown": md_path, "json": json_path}

    def __repr__(self) -> str:
        d = self.document
        return (
            f"ParseResult(pages={d.n_pages}, blocks={len(d.blocks)}, "
            f"scanned={d.is_scanned}, lang={d.language_hint!r}, signed={d.is_signed})"
        )
