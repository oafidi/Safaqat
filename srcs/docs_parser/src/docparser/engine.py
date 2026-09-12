"""PaddleOCR-VL-1.6: layout detection, OCR and table structure in one pass.

The pipeline runs PP-DocLayoutV3 to find regions, then the 0.9B VLM to read each
one. It handles native and scanned pages the same way (it always reads pixels),
and it reads French and Arabic without being told which to expect.

Importing this module has a side effect: it applies the PaddlePaddle fixes in
:mod:`docparser._compat`, without which the model cannot load at all.
"""

from __future__ import annotations

import gc
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import _compat


@dataclass
class LayoutBlock:
    """One region on a page, as the layout model sees it."""

    label: str
    content: str
    """Text for text regions; HTML for tables; empty for pictures."""

    bbox: tuple[float, float, float, float]
    """(x0, y0, x1, y1) in pixels of the rendered page."""

    order: int
    confidence: float | None = None


class VLEngine:
    """Wraps :class:`paddleocr.PaddleOCRVL`, adding a way to free the GPU."""

    def __init__(self, dpi: int = 150, layout_model: str = "PP-DocLayoutV3"):
        self.dpi = dpi
        self.layout_model = layout_model
        self._pipeline = None
        self._detector = None

    @property
    def loaded(self) -> bool:
        return self._pipeline is not None

    # Loading the VL model peaks at ~4.7 GB. Starting with less than this free is
    # not a tight fit, it is a guaranteed failure partway through the load.
    _REQUIRED_FREE_BYTES = 4.9e9

    def load(self) -> None:
        if self._pipeline is not None:
            return

        _compat.apply_paddlex_patches()  # must precede the import of the pipeline
        self._check_free_vram()
        from paddleocr import PaddleOCRVL

        self._pipeline = PaddleOCRVL()
        self._release_cached_blocks()

    @classmethod
    def _check_free_vram(cls) -> None:
        """Fail early, and clearly, if another process is holding the GPU.

        Paddle's own out-of-memory error arrives halfway through loading the
        weights and blames the batch size, which is not the problem. The usual
        cause is simply another process (often a second parse) still resident.
        """
        try:
            import torch

            if not torch.cuda.is_available():
                return  # CPU-only install: nothing to check
            free_bytes, total_bytes = torch.cuda.mem_get_info()
        except Exception:  # noqa: BLE001 - cannot query; never block the parse over this
            return

        if free_bytes < cls._REQUIRED_FREE_BYTES:
            raise RuntimeError(
                f"Only {free_bytes / 1e9:.1f} GB of GPU memory is free "
                f"(of {total_bytes / 1e9:.1f} GB); PaddleOCR-VL needs about "
                f"{cls._REQUIRED_FREE_BYTES / 1e9:.1f} GB to load. Another process is "
                "most likely holding the GPU -- check `nvidia-smi`. Parsing two "
                "documents at once on a 6 GB card will not work; run them in sequence."
            )

    def unload(self) -> None:
        """Drop the models and return their VRAM, so the captioner can have it."""
        self._pipeline = None
        self._detector = None
        gc.collect()
        self._release_cached_blocks()

    def sweep_pictures(
        self, image: np.ndarray, threshold: float = 0.15
    ) -> list[tuple[str, tuple[float, float, float, float], float]]:
        """Find picture regions the main parse misses, at a low threshold.

        This runs PP-DocLayoutV3 on its own rather than re-reading the pipeline's
        detections, because the two need different thresholds. The pipeline must
        keep its default (a low threshold there pulls spurious regions into the
        parse and *loses* real blocks -- observed: a figure_title disappearing).
        This pass only ever contributes picture candidates for classification, so
        its threshold can be as low as we like without touching the content.

        Costs ~60 MB of VRAM and well under a second per page.

        Returns:
            ``(label, bbox_px, score)`` for each picture-labelled region.
        """
        from .config import PICTURE_LABELS

        if self._detector is None:
            _compat.apply_paddlex_patches()
            from paddleocr import LayoutDetection

            self._detector = LayoutDetection(model_name=self.layout_model)

        bgr = np.ascontiguousarray(image[:, :, ::-1])
        result = list(self._detector.predict(bgr, threshold=threshold, layout_nms=True))[0]
        raw = result.json
        raw = raw.get("res", raw)

        out: list[tuple[str, tuple[float, float, float, float], float]] = []
        for box in raw.get("boxes", []) or []:
            label = box.get("label", "")
            coord = box.get("coordinate") or []
            # Everything that is not a picture is ignored: the parse, not this
            # sweep, is the source of truth for text, tables and titles.
            if label not in PICTURE_LABELS or len(coord) != 4:
                continue
            bbox = tuple(float(v) for v in coord)
            out.append((label, bbox, float(box.get("score", 0.0))))  # type: ignore[arg-type]
        return out

    @staticmethod
    def _release_cached_blocks() -> None:
        """Return Paddle's cached-but-unused GPU blocks to the driver.

        Loading the model briefly needs ~5.4 GB (the fp32 model is built, then
        cast down to bf16). Paddle's caching allocator holds on to that memory
        afterwards even though only ~1.9 GB is live, which would leave too
        little for the captioner. empty_cache gives it back.
        """
        try:
            import paddle

            if paddle.device.cuda.device_count() > 0:
                paddle.device.cuda.empty_cache()
        except Exception:  # noqa: BLE001 - CPU-only install; nothing to free
            pass

    def analyse_page(self, image: np.ndarray) -> tuple[list[LayoutBlock], dict[str, Any]]:
        """Run layout + OCR on one rendered page.

        Args:
            image: RGB uint8 array of the page.

        Returns:
            The page's blocks in reading order, and the raw result dict.
        """
        self.load()
        assert self._pipeline is not None

        # PaddleOCR/OpenCV convention is BGR.
        bgr = np.ascontiguousarray(image[:, :, ::-1])
        result = list(self._pipeline.predict(bgr))[0]

        raw = result.json
        raw = raw.get("res", raw)

        # Layout detection scores live in a parallel list, keyed by geometry.
        scores = self._score_index(raw.get("layout_det_res", {}))

        blocks: list[LayoutBlock] = []
        for item in raw.get("parsing_res_list", []):
            bbox = tuple(float(v) for v in item.get("block_bbox", (0, 0, 0, 0)))
            label = item.get("block_label", "other")
            blocks.append(
                LayoutBlock(
                    label=label,
                    content=item.get("block_content", "") or "",
                    bbox=bbox,  # type: ignore[arg-type]
                    order=int(item.get("block_order") or item.get("block_id") or len(blocks)),
                    confidence=scores.get((label, tuple(round(v) for v in bbox))),
                )
            )

        blocks.sort(key=lambda b: b.order)
        return blocks, raw

    @staticmethod
    def _score_index(layout_det: dict) -> dict[tuple, float]:
        """Map (label, rounded bbox) -> detection score."""
        index: dict[tuple, float] = {}
        for box in layout_det.get("boxes", []) or []:
            coord = box.get("coordinate") or []
            if len(coord) != 4:
                continue
            key = (box.get("label"), tuple(round(float(v)) for v in coord))
            index[key] = float(box.get("score", 0.0))
        return index

    def __enter__(self) -> "VLEngine":
        self.load()
        return self

    def __exit__(self, *exc) -> None:
        self.unload()
