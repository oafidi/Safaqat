"""Language detection for French / Arabic / mixed documents.

Script detection by Unicode range is used rather than a statistical model.
For this problem it is strictly better: Arabic and Latin occupy disjoint code
point ranges, so counting letters is exact, needs no model, and cannot be
thrown off by the short, boilerplate-heavy text typical of tender documents.
"""

from __future__ import annotations

import unicodedata

# Arabic, Arabic Supplement, Extended-A, and the presentation forms that PDFs
# often use for ligatures.
_ARABIC_RANGES = (
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
)


def _is_arabic(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _ARABIC_RANGES)


def _is_latin(ch: str) -> bool:
    # 'LATIN SMALL LETTER E WITH ACUTE' -> starts with LATIN, so accented French
    # characters count as Latin, which is what we want.
    try:
        return unicodedata.name(ch).startswith("LATIN")
    except ValueError:
        return False


def script_counts(text: str) -> tuple[int, int]:
    """Return ``(n_arabic_letters, n_latin_letters)``."""
    arabic = latin = 0
    for ch in text:
        if not ch.isalpha():
            continue
        if _is_arabic(ch):
            arabic += 1
        elif _is_latin(ch):
            latin += 1
    return arabic, latin


def detect_language(text: str, mixed_threshold: float = 0.15) -> str:
    """Classify text as ``"fr"``, ``"ar"`` or ``"mixed"``.

    A document counts as mixed when the minority script holds more than
    ``mixed_threshold`` of the letters. Below that it is treated as noise: a
    French tender quoting one Arabic word is still a French document.

    Falls back to ``"fr"`` for text with no letters at all (an empty or purely
    numeric page), since these documents are French-administrative by default.
    """
    arabic, latin = script_counts(text)
    total = arabic + latin
    if total == 0:
        return "fr"

    arabic_ratio = arabic / total
    if arabic_ratio > 1.0 - mixed_threshold:
        return "ar"
    if arabic_ratio < mixed_threshold:
        return "fr"
    return "mixed"
