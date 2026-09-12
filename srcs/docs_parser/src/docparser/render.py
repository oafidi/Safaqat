"""Assemble blocks into Markdown, and record where each block landed.

The Markdown and the JSON are two views of the same block list, not two
independent renderings. As the Markdown is built, each block's character span
is written back onto it (``Block.markdown_span``), so a chunker can slice the
Markdown and know exactly which blocks -- and therefore which pages and bboxes
-- a chunk covers.
"""

from __future__ import annotations

from .config import TITLE_LABELS
from .schema import Block

# Rough, and deliberately so: ~4 characters per token for Latin script, fewer
# for Arabic. Good enough to pack chunks to a budget, not a substitute for a
# real tokeniser.
_CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / _CHARS_PER_TOKEN)) if text else 0


def heading_level(label: str, order_in_doc: int = 0) -> int:
    """Markdown heading depth for a title label."""
    if label == "doc_title":
        return 1
    if label in ("chapter_title",):
        return 2
    return 3  # paragraph_title and friends


def assign_section_paths(blocks: list[Block]) -> None:
    """Walk the blocks and stamp each with the heading trail above it."""
    stack: list[tuple[int, str]] = []  # (level, text)

    for block in blocks:
        if block.is_furniture:
            # Headers and page numbers are not part of the document's structure;
            # they would otherwise pollute every section path.
            block.section_path = [text for _, text in stack]
            continue

        if block.type == "title" and block.text:
            level = block.level or 3
            while stack and stack[-1][0] >= level:
                stack.pop()
            # A title's own path is the trail *above* it, not including itself.
            block.section_path = [text for _, text in stack]
            stack.append((level, block.text))
        else:
            block.section_path = [text for _, text in stack]


def render_markdown(blocks: list[Block], include_furniture: bool = False) -> str:
    """Render blocks to Markdown and set ``markdown_span`` on each.

    Furniture is skipped by default: running headers and page numbers repeat on
    every page and add nothing to a document read end to end.
    """
    parts: list[str] = []
    cursor = 0

    for block in blocks:
        if block.is_furniture and not include_furniture:
            continue

        chunk = _render_block(block)
        if not chunk:
            continue

        start = cursor
        parts.append(chunk)
        cursor += len(chunk)

        separator = "\n\n"
        parts.append(separator)
        cursor += len(separator)

        block.markdown_span = (start, start + len(chunk))

    return "".join(parts).rstrip() + "\n"


def _render_block(block: Block) -> str:
    if block.type == "title":
        return f"{'#' * (block.level or 3)} {block.text}".strip()

    if block.type == "table" and block.table is not None:
        # Always GFM, never the source HTML -- even when cells are merged. GFM
        # cannot express a span, but the renderer repeats a merged value across
        # every cell it covers, which loses nothing a reader needs and keeps the
        # document in one syntax. A wall of <td rowspan="3"> in the middle of a
        # Markdown file is readable by a browser, not by a language model.
        return (block.table.markdown or "").strip()

    if block.type == "image":
        return _render_figure(block)

    return block.text.strip()


def _render_figure(block: Block) -> str:
    """A figure is worth a line only if something described it.

    No image is written to disk and no path is referenced, because a path is not
    something a language model reading this file can follow. What survives is the
    caption -- marked with its class so a reader can tell a generated
    description from text that was printed on the page.
    """
    figure = block.figure
    if figure is None or not figure.caption:
        return ""
    return f"*[{figure.figure_class.replace('_', ' ')}]* {figure.caption}"


def map_label(label: str) -> tuple[str, bool]:
    """Map a PP-DocLayoutV3 label onto a ``BlockType``.

    Returns ``(block_type, is_title)``.
    """
    if label in TITLE_LABELS:
        return "title", True
    return _LABEL_MAP.get(label, "other"), False


_LABEL_MAP: dict[str, str] = {
    "text": "paragraph",
    "table": "table",
    "image": "image",
    "chart": "image",
    "figure": "image",
    "header_image": "image",
    "footer_image": "image",
    "figure_title": "image_caption",
    "table_title": "image_caption",
    "chart_title": "image_caption",
    "table_caption": "image_caption",
    "image_caption": "image_caption",
    "formula": "formula",
    "formula_number": "formula",
    "algorithm": "other",
    "header": "header",
    "footer": "footer",
    "number": "page_number",
    "footnote": "reference",
    "reference": "reference",
    "reference_content": "reference",
    "abstract": "paragraph",
    "content": "paragraph",
    "seal": "image",
    "aside_text": "other",
}
