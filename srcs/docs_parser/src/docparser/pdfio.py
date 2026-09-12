"""PDF rasterisation and native-vs-scanned detection (PyMuPDF)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
import numpy as np

# A page with a real text layer has hundreds of characters. Scanners that embed
# a thin OCR layer, or PDFs whose only text is a header, land well below this.
_MIN_CHARS_NATIVE = 100


@dataclass
class RenderedPage:
    """One page, rasterised and ready for the layout model."""

    number: int
    image: np.ndarray
    """RGB, uint8, (H, W, 3)."""

    width: float
    """Page width in PDF points (not pixels)."""

    height: float
    scale: float
    """pixels per point; divide a pixel coordinate by this to get points."""

    text: str
    """Text from the PDF's own text layer. Empty for a scanned page."""

    @property
    def is_scanned(self) -> bool:
        return len(self.text.strip()) < _MIN_CHARS_NATIVE

    def to_points(self, bbox_px: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        """Convert a pixel bbox from the rendered image back to PDF points."""
        s = self.scale
        return (bbox_px[0] / s, bbox_px[1] / s, bbox_px[2] / s, bbox_px[3] / s)


class PdfReader:
    """Rasterises pages and reports whether the document is scanned."""

    def __init__(self, path: str | Path, dpi: int = 150):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"No such PDF: {self.path}")
        self.dpi = dpi
        self._doc = fitz.open(self.path)
        if self._doc.page_count == 0:
            raise ValueError(f"PDF has no pages: {self.path}")
        if self._doc.needs_pass:
            raise ValueError(f"PDF is password-protected: {self.path}")

    def __len__(self) -> int:
        return self._doc.page_count

    @property
    def metadata(self) -> dict:
        return dict(self._doc.metadata or {})

    def pages(self, max_pages: int | None = None):
        """Yield :class:`RenderedPage` objects, one page at a time.

        This is a generator on purpose: a 100-page scan at 150 dpi is several
        GB of pixels if you hold it all at once, and we only ever need one page
        resident at a time.
        """
        n = self._doc.page_count if max_pages is None else min(max_pages, self._doc.page_count)
        scale = self.dpi / 72.0  # PDF user space is 72 points per inch

        for i in range(n):
            page = self._doc[i]
            pix = page.get_pixmap(dpi=self.dpi)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            if pix.n == 4:  # RGBA (rare; some PDFs with transparency)
                img = img[:, :, :3]
            elif pix.n == 1:  # greyscale
                img = np.repeat(img, 3, axis=2)
            img = np.ascontiguousarray(img[:, :, :3])

            yield RenderedPage(
                number=i + 1,
                image=img,
                width=page.rect.width,
                height=page.rect.height,
                scale=scale,
                text=page.get_text("text") or "",
            )

    def close(self) -> None:
        self._doc.close()

    def __enter__(self) -> "PdfReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
