"""PDF to RAG-ready Markdown + JSON, fully local.

    from docparser import DocumentParser

    result = DocumentParser("tender.pdf").parse()

    result.markdown        # str
    result.json            # dict
    result.is_scanned      # bool
    result.language_hint   # "fr" | "ar" | "mixed"
    result.is_signed       # bool  (a signature or stamp was found)
    result.save("./output")

Handles native and scanned PDFs, French and Arabic, tables with merged cells,
figures, repeated page furniture, and signature/stamp detection.
"""

from __future__ import annotations

from . import _compat

# Import-time, before anything can pull in paddle or bitsandbytes: without these
# fixes PaddlePaddle fails to import and bitsandbytes fails to quantise. See
# docparser/_compat.py for what is patched and why.
_compat.apply()

from .config import ParserConfig  # noqa: E402
from .parser import DocumentParser  # noqa: E402
from .schema import Block, Cell, Document, Figure, Page, ParseResult, Table  # noqa: E402

__version__ = "0.2.0"

__all__ = [
    "DocumentParser",
    "ParserConfig",
    "ParseResult",
    "Document",
    "Block",
    "Table",
    "Cell",
    "Figure",
    "Page",
    "__version__",
]
