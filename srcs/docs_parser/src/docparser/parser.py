"""The PDF orchestrator: PDF in, RAG-ready Markdown + JSON out.

Staying inside 6 GB of VRAM is a scheduling problem. Two models want the GPU:

    PaddleOCR-VL-1.6   ~1.9 GB resident (~5.4 GB transient while loading)
    Qwen2.5-VL-3B      ~2.4 GB resident in 4-bit (~3.5 GB while generating)

Resident together they fit; loading one while the other is resident does not. So
the parse runs in stages, and only one model is on the GPU at any moment:

    stage 1  read every page with PaddleOCR-VL, keeping the figure crops
    stage 2  release PaddleOCR-VL  <- the GPU is now empty
    stage 3  classify every crop (ONNX/torch, CPU: the GPU stays empty)
    stage 4  caption only the figures that warrant it, loading the VLM now
    stage 5  release the VLM

Peak VRAM is therefore max(stage), not sum(stages). Stage 4 is skipped outright
when nothing needs a caption, which is the common case for tender documents.

The result keeps page and section metadata so it can be chunked for RAG.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

from .config import ParserConfig
from .engine import VLEngine
from .figures import process_figures
from .furniture import strip_furniture
from .language import detect_language, script_counts
from .merge import merge_split_tables
from .pagebuild import build_page, sweep_page
from .pdfio import PdfReader
from .render import assign_section_paths, estimate_tokens, render_markdown
from .schema import Block, Document, Page, ParseResult

log = logging.getLogger("docparser")


class DocumentParser:
    """Parse a PDF into Markdown + JSON.

    >>> result = DocumentParser("document.pdf").parse()
    >>> result.markdown, result.json, result.is_signed
    """

    def __init__(self, path: str | Path, config: ParserConfig | None = None):
        self.path = Path(path)
        self.config = config or ParserConfig()

    def parse(self) -> ParseResult:
        cfg = self.config
        reader = PdfReader(self.path, dpi=cfg.dpi)

        blocks: list[Block] = []
        pages: list[Page] = []
        crops: dict[str, Image.Image] = {}  # block id -> figure crop
        pending: list[Block] = []  # figures awaiting classification
        sweep_ids: set[str] = set()
        text_by_script = [0, 0]  # running (arabic, latin) letter counts
        order = 0

        try:
            # -- stage 1: layout + OCR, one page at a time --------------------
            engine = VLEngine(dpi=cfg.dpi, layout_model=cfg.layout_model)
            try:
                for page in reader.pages(max_pages=cfg.max_pages):
                    log.info("page %d/%d", page.number, len(reader))
                    layout, _ = engine.analyse_page(page.image)

                    page_blocks, page_crops, page_pending, order = build_page(
                        page, layout, order, cfg
                    )

                    if cfg.detect_signatures:
                        swept, swept_crops, order = sweep_page(
                            page, engine, page_pending, order, cfg
                        )
                        page_blocks.extend(swept)
                        page_crops.update(swept_crops)
                        page_pending.extend(swept)
                        sweep_ids.update(b.id for b in swept)

                    blocks.extend(page_blocks)
                    crops.update(page_crops)
                    pending.extend(page_pending)

                    page_text = " ".join(b.text for b in page_blocks if b.text)
                    # A scanned page has no text layer of its own, so language has
                    # to come from what the VL model just read off it.
                    source_text = page.text if not page.is_scanned else page_text
                    arabic, latin = script_counts(source_text)
                    text_by_script[0] += arabic
                    text_by_script[1] += latin

                    pages.append(
                        Page(
                            number=page.number,
                            width=page.width,
                            height=page.height,
                            is_scanned=page.is_scanned,
                            language_hint=detect_language(source_text, cfg.arabic_ratio_mixed),
                            n_blocks=len(page_blocks),
                        )
                    )
            finally:
                engine.unload()  # stage 2: hand the GPU back before anything else asks

            # -- stages 3-5 --------------------------------------------------
            evidence = process_figures(pending, crops, cfg, sweep_ids)

        finally:
            reader.close()

        # A figure is kept only if something described it: a logo or a rule has
        # no text and no path, so nothing of it survives. A signature is not a
        # block either -- it is the `is_signed` flag.
        blocks = [b for b in blocks if b.type != "image" or (b.text and b.text.strip())]

        # Furniture before tables: the merger asks whether there is content in the
        # gap between two fragments, and a footer sits in that gap on every page.
        if cfg.detect_furniture:
            blocks = strip_furniture(
                blocks, pages,
                min_pages=cfg.furniture_min_pages,
                max_len=cfg.furniture_max_len,
                margin=cfg.furniture_margin,
                fuzzy=cfg.furniture_fuzzy,
            )

        # A table running over a page break is one table, but the layout model
        # only ever sees one page. Stitching happens here, with the whole
        # document in hand.
        if cfg.merge_split_tables:
            blocks = merge_split_tables(
                blocks, pages, cfg.table_width_tolerance, cfg.table_edge_margin
            )

        for i, block in enumerate(blocks):
            block.order = i  # close the gaps left by everything discarded above
        for p in pages:
            p.n_blocks = sum(1 for b in blocks if b.page == p.number)

        assign_section_paths(blocks)
        markdown = render_markdown(blocks)
        for block in blocks:
            block.token_estimate = estimate_tokens(
                block.text or (block.table.markdown if block.table else "")
            )

        arabic, latin = text_by_script
        language = (
            detect_language("ا" * arabic + "a" * latin, cfg.arabic_ratio_mixed)
            if arabic + latin
            else "fr"
        )
        is_scanned = bool(pages) and sum(p.is_scanned for p in pages) > len(pages) / 2

        document = Document(
            source=str(self.path),
            n_pages=len(pages),
            is_scanned=is_scanned,
            language_hint=language,
            is_signed=bool(evidence),
            blocks=blocks,
            pages=pages,
            signature_evidence=evidence,
            metadata={
                "parser": "docparser",
                "source_format": "pdf",
                "layout_model": cfg.layout_model,
                "vl_model": cfg.vl_model,
                "classifier_model": cfg.classifier_model if cfg.classify_figures else None,
                "caption_model": cfg.caption_model if cfg.caption_figures else None,
                "dpi": cfg.dpi,
                "arabic_letters": arabic,
                "latin_letters": latin,
                **reader.metadata,
            },
        )

        return ParseResult(document, markdown)
