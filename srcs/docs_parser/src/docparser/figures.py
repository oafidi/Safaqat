"""The figure pipeline: classify every picture, caption the ones worth it.

Used by the PDF pipeline. The GPU discipline lives here: the
classifier runs on CPU precisely so the card is empty, and the captioner is
loaded only if something actually needs a caption -- which, for tender
documents, is usually nothing at all.
"""

from __future__ import annotations

import logging
from typing import Any

from PIL import Image

from .captioner import Captioner
from .classifier import FigureClassifier
from .config import ParserConfig
from .schema import Block

log = logging.getLogger("docparser")


def process_figures(
    pending: list[Block],
    crops: dict[str, Image.Image],
    config: ParserConfig,
    sweep_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Classify every figure, then caption the ones that carry information.

    Returns the signature/stamp evidence behind ``Document.is_signed``.
    """
    sweep_ids = sweep_ids or set()
    if not pending or not config.classify_figures:
        return []

    # Classify on CPU: the GPU is empty at this point, and it must stay that way.
    classifier = FigureClassifier(config.classifier_model, backend=config.classifier_backend)
    predictions = classifier.classify_batch([crops[b.id] for b in pending])

    evidence: list[dict[str, Any]] = []
    to_caption: list[Block] = []

    for block, (label, confidence) in zip(pending, predictions):
        figure = block.figure
        assert figure is not None
        figure.figure_class = label
        figure.confidence = confidence

        from_sweep = block.id in sweep_ids
        # A swept region was found below the parse's own threshold, so it is held
        # to a higher bar: it counts only if the classifier is sure.
        floor = config.signature_min_confidence if from_sweep else config.classifier_min_confidence
        figure.is_signature = FigureClassifier.is_signature(label) and confidence >= floor

        if figure.is_signature:
            evidence.append(
                {
                    "block_id": block.id,
                    "page": block.page,
                    "class": label,
                    "confidence": round(confidence, 4),
                    "bbox": list(block.bbox),
                    "found_by": "sweep" if from_sweep else "layout",
                }
            )

        if from_sweep and not figure.is_signature:
            continue  # a speculative detection that turned out to be nothing

        # Swept regions are never captioned: they are speculative by design, and
        # a caption for one is noise in a retrieval index.
        if config.caption_figures and not from_sweep and FigureClassifier.should_caption(label):
            to_caption.append(block)

    log.info(
        "figures: %d classified, %d to caption, %d signature/stamp",
        len(pending), len(to_caption), len(evidence),
    )

    if to_caption:
        captioner = Captioner(
            model_id=config.caption_model,
            load_in_4bit=config.caption_in_4bit,
            max_new_tokens=config.caption_max_new_tokens,
        )
        try:
            for block in to_caption:
                assert block.figure is not None
                caption = captioner.caption(crops[block.id])
                block.figure.caption = caption or None
                block.figure.captioned = bool(caption)
                if caption:
                    # The caption *is* the block's text: it is what a retriever
                    # will match against for this figure.
                    block.text = caption
        finally:
            captioner.unload()

    return evidence
