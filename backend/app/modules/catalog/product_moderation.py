from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

# The first automatic policy is intentionally narrow: only explicit drug and killing
# terms requested by the current catalog policy are blocked. More policy groups can
# be added behind the same interface.
_COMPACT_SENSITIVE_TERMS = (
    "毒品",
    "杀人",
)
_TRADITIONAL_TRANSLATION = str.maketrans({"殺": "杀"})


@dataclass(frozen=True)
class ProductModerationResult:
    approved: bool
    matched_terms: tuple[str, ...]


def moderate_product_texts(values: Iterable[str | None]) -> ProductModerationResult:
    """Apply the deterministic first-pass catalog text policy.

    Unicode normalization and separator removal prevent trivial variants such as
    ``毒 品`` or ``殺-人`` from bypassing the policy.
    """

    matched: set[str] = set()
    for value in values:
        if not value:
            continue
        normalized = (
            unicodedata.normalize("NFKC", value).casefold().translate(_TRADITIONAL_TRANSLATION)
        )
        compact = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", normalized)
        matched.update(term for term in _COMPACT_SENSITIVE_TERMS if term in compact)
    return ProductModerationResult(approved=not matched, matched_terms=tuple(sorted(matched)))
