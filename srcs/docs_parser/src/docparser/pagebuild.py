"""Convert one rendered PDF page into structured blocks.

Native and scanned pages share this layout, OCR, furniture, table, and figure
pipeline.
"""

from __future__ import annotations

from PIL import Image

from .config import ParserConfig
from .engine import LayoutBlock, VLEngine
from .pdfio import RenderedPage
from .render import heading_level, map_label
from .schema import Block, Figure
from .tables import parse_table_html

BBox = tuple[float, float, float, float]


def iou(a: BBox, b: BBox) -> float:
    """Intersection over union of two boxes."""
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    if inter <= 0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def crop(
    page: RenderedPage, bbox_px: BBox, cached: Image.Image | None, min_px: int
) -> tuple[Image.Image | None, Image.Image]:
    """Cut a figure out of the rendered page. Returns (crop, the page image)."""
    image = cached if cached is not None else Image.fromarray(page.image)

    x0, y0, x1, y1 = (int(round(v)) for v in bbox_px)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(image.width, x1), min(image.height, y1)

    if x1 - x0 < min_px or y1 - y0 < min_px:
        return None, image
    return image.crop((x0, y0, x1, y1)), image


def build_page(
    page: RenderedPage, layout: list[LayoutBlock], order: int, config: ParserConfig
) -> tuple[list[Block], dict[str, Image.Image], list[Block], int]:
    """Turn one page's layout regions into blocks (+ crops for the figures)."""
    blocks: list[Block] = []
    crops: dict[str, Image.Image] = {}
    pending: list[Block] = []  # figures awaiting classification
    page_image: Image.Image | None = None

    for item in layout:
        block_type, is_title = map_label(item.label)
        block_id = f"p{page.number}_b{len(blocks)}"

        block = Block(
            id=block_id,
            type=block_type,  # type: ignore[arg-type]
            page=page.number,
            order=order,
            bbox=page.to_points(item.bbox),
            text=item.content.strip(),
            layout_label=item.label,
            is_furniture=item.label in config.furniture_labels,
            confidence=item.confidence,
            level=heading_level(item.label) if is_title else None,
        )
        order += 1

        if block_type == "table" and item.content:
            block.table = parse_table_html(item.content)
            # The table's text is its Markdown: that is what should be embedded.
            block.text = block.table.markdown

        elif item.label in config.picture_labels:
            image, page_image = crop(page, item.bbox, page_image, config.min_figure_px)
            if image is None:
                continue  # smaller than min_figure_px: a rule or scan speckle
            block.figure = Figure(figure_class="", confidence=0.0)
            crops[block_id] = image
            pending.append(block)

        blocks.append(block)

    return blocks, crops, pending, order


def sweep_page(
    page: RenderedPage,
    engine: VLEngine,
    already_found: list[Block],
    order: int,
    config: ParserConfig,
) -> tuple[list[Block], dict[str, Image.Image], int]:
    """Look for signatures and stamps the main parse did not surface.

    Candidates overlapping a figure the parse already found are dropped: that
    figure goes through the pipeline anyway.
    """
    seen = [b.bbox for b in already_found]

    blocks: list[Block] = []
    crops: dict[str, Image.Image] = {}
    page_image: Image.Image | None = None

    for label, bbox_px, score in engine.sweep_pictures(
        page.image, threshold=config.signature_sweep_threshold
    ):
        bbox_pt = page.to_points(bbox_px)
        if any(iou(bbox_pt, other) > 0.3 for other in seen):
            continue

        image, page_image = crop(page, bbox_px, page_image, config.min_figure_px)
        if image is None:
            continue

        block_id = f"p{page.number}_s{len(blocks)}"
        block = Block(
            id=block_id,
            type="image",
            page=page.number,
            order=order,
            bbox=bbox_pt,
            layout_label=label,
            confidence=score,
            figure=Figure(figure_class="", confidence=0.0),
        )
        order += 1
        crops[block_id] = image
        blocks.append(block)

    return blocks, crops, order
