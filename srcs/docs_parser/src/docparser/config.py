"""Tunable settings for the parser.

The defaults target an RTX 3060 with 6 GB of VRAM. The single most important
rule for staying inside that budget is in :mod:`docparser.parser`: only one
model is resident on the GPU at a time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Layout labels that PP-DocLayoutV3 uses for picture regions. Every region with
# one of these labels goes through the figure pipeline (classify -> caption or
# drop). Header/footer images are included on purpose: that is where logos live,
# and also where stamps and signatures tend to appear.
PICTURE_LABELS: frozenset[str] = frozenset(
    {"image", "chart", "figure", "header_image", "footer_image"}
)

# Labels that are page furniture rather than content. They are kept in the JSON
# (with is_furniture=True) so nothing is silently lost, but they are excluded
# from the Markdown body and should be excluded from RAG chunks.
FURNITURE_LABELS: frozenset[str] = frozenset(
    {"header", "footer", "number", "aside_text", "header_image", "footer_image"}
)

# Heading labels, most significant first. Used to build the section hierarchy.
TITLE_LABELS: tuple[str, ...] = ("doc_title", "paragraph_title", "chapter_title", "title")

# --- Figure routing (DocumentFigureClassifier-v2.5, 26 labels) --------------

# A figure of one of these classes gets a VLM caption: it carries information
# that is otherwise lost, because the layout model returns no text for it.
CAPTION_CLASSES: frozenset[str] = frozenset(
    {
        "flow_chart",
        "geographical_map",
        "topographical_map",
        "screenshot_from_computer",
        "screenshot_from_manual",
        "other",
        "bar_chart",
        "line_chart",
        "pie_chart",
        "scatter_plot",
        "box_plot",
        "engineering_drawing",
        "photograph",
        "full_page_image",
    }
)

# Finding one of these means the document carries a handwritten signature or an
# official stamp, so the document is flagged as signed.
SIGNATURE_CLASSES: frozenset[str] = frozenset({"signature", "stamp"})


@dataclass
class ParserConfig:
    """Configuration for :class:`docparser.DocumentParser`."""

    # --- rendering ---
    dpi: int = 150
    """Rasterisation DPI. 150 is the sweet spot for PaddleOCR-VL: 200+ mostly
    buys larger images and slower inference, not better text."""

    # --- models ---
    layout_model: str = "PP-DocLayoutV3"
    vl_model: str = "PaddleOCR-VL-1.6"
    classifier_model: str = "docling-project/DocumentFigureClassifier-v2.5"
    caption_model: str = "Qwen/Qwen2.5-VL-3B-Instruct"

    classifier_backend: str = "torch"
    """Either "torch" or "onnx", both on CPU. Defaults to torch because the
    published ONNX export of the classifier is broken -- its output does not
    depend on its input. See docparser/classifier.py."""

    # --- figure pipeline ---
    classify_figures: bool = True
    caption_figures: bool = True
    caption_max_new_tokens: int = 128
    """Enough for the model to finish its sentence. At 96 it was reliably
    truncating mid-clause, which leaves a dangling fragment in the index."""
    min_figure_px: int = 32
    """Ignore picture regions smaller than this on either side: they are
    rules, bullets and scan speckle, not figures."""

    classifier_min_confidence: float = 0.0
    """Figures classified below this confidence are still recorded, but their
    class is not trusted for the signed flag. 0 disables the check."""

    # --- signature sweep ---
    detect_signatures: bool = True
    """Look for signatures and stamps that the main parse does not surface.

    A signature in one of these documents is almost always inside a signature
    *table*, and the layout model reports that whole area as one `table` region
    -- the ink never becomes a picture region of its own, so the figure pipeline
    never sees it. (Measured: a genuinely stamped page yielded zero picture
    regions and `is_signed=False`.) The scores are there, just below threshold:
    the same signature is detected as a picture at 0.23, under the ~0.5 default.

    So a second, low-threshold pass of the layout detector runs purely to find
    picture regions, and the 26-class classifier decides what they are. Only
    'signature' and 'stamp' are kept; everything else the sweep turns up is
    discarded. Content comes from the main parse, which the sweep never touches,
    so lowering this threshold cannot degrade the extracted text."""

    signature_sweep_threshold: float = 0.15
    """Detector score above which a region is worth classifying. Low on purpose:
    the classifier, not the detector, decides what the region is."""

    signature_min_confidence: float = 0.5
    """Classifier confidence required to call a swept region a signature. The
    true positives measured here land at 0.70-0.78, while the nearest false
    positive (a dotted signature line on a blank form) is classified as
    'engineering_drawing' and never reaches this test."""

    # --- page furniture ---
    detect_furniture: bool = True
    """Find running headers, footers and page numbers that the layout model did
    not label as such, and drop every one of them from the output. See
    docparser/furniture.py -- position plus repetition, because the layout model
    hands most of them back labelled `text`."""

    furniture_margin: float = 0.12
    """Height of the top and bottom margin bands, as a fraction of the page. A
    block outside both bands is never furniture, however often it repeats."""

    furniture_min_pages: int | None = None
    """Distinct pages a text must appear on, in a margin band, before it counts
    as a running fixture. None = 30% of the document, never fewer than 2."""

    furniture_max_len: int = 120
    """A running header is short; past this a margin block is prose."""

    furniture_fuzzy: float = 0.80
    """Similarity above which two margin texts are variants of one another --
    what links "Marche 32/2026 page 3/48" to "... page 4/48" so the family can be
    seen to repeat even though no single member does."""

    # --- split tables ---
    merge_split_tables: bool = True
    """Stitch a table that runs over a page break back into one table, dropping
    the header the continuation reprints and repairing the merged cells the
    break cut in half. See docparser/merge.py for the rules that decide whether
    two fragments are really one table."""

    table_width_tolerance: float = 0.15
    """How much two fragments' widths (as a fraction of their page) may differ
    and still be taken for the same table. Tables that continue over a break are
    drawn to the same margins; two unrelated tables rarely are."""

    table_edge_margin: float = 0.25
    """How close to the edge of the page a fragment must reach, as a fraction of
    the page height, for the page break to be what interrupted it: the first
    fragment must end in the bottom quarter and the second start in the top
    quarter. A table that stops halfway down a page and leaves the rest blank
    was not interrupted -- it finished."""

    # --- language ---
    arabic_ratio_mixed: float = 0.15
    """Above this share of Arabic letters (and below 1-x Latin) a document is
    'mixed' rather than purely 'fr' or 'ar'."""

    # --- memory ---
    caption_in_4bit: bool = True
    """Load the captioning VLM with 4-bit NF4 weights. Qwen2.5-VL-3B needs
    ~7.5 GB in bf16, which does not fit in 6 GB; 4-bit brings it to ~2.5 GB."""

    max_pages: int | None = None
    """Parse only the first N pages. Useful for smoke tests."""

    # --- output ---
    # Figure crops are never written to disk and never referenced from the
    # Markdown: a path is not something a language model can read. A figure
    # reaches the output as its caption, or not at all.

    picture_labels: frozenset[str] = field(default_factory=lambda: PICTURE_LABELS)
    furniture_labels: frozenset[str] = field(default_factory=lambda: FURNITURE_LABELS)
