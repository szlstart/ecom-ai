from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

# The first automatic policy is intentionally narrow: it blocks explicit firearm,
# ammunition and explosive-ordnance terms without treating benign products such as
# "玩具水枪" as firearms. More policy groups can be added behind the same interface.
_COMPACT_SENSITIVE_TERMS = (
    "枪支",
    "枪械",
    "手枪",
    "步枪",
    "冲锋枪",
    "狙击枪",
    "猎枪",
    "霰弹枪",
    "机枪",
    "火枪",
    "气枪",
    "仿真枪",
    "军用枪",
    "子弹",
    "弹药",
    "实弹",
    "枪弹",
    "弹匣",
    "弹夹",
    "枪管",
    "枪支消音器",
    "雷管",
    "炸药",
    "手榴弹",
    "火箭弹",
    "炮弹",
)
_ENGLISH_SENSITIVE_TERMS = (
    "firearm",
    "pistol",
    "rifle",
    "shotgun",
    "machine gun",
    "submachine gun",
    "ammunition",
    "ammo",
    "live round",
)
_TRADITIONAL_TRANSLATION = str.maketrans({"槍": "枪", "彈": "弹", "藥": "药"})


@dataclass(frozen=True)
class ProductModerationResult:
    approved: bool
    matched_terms: tuple[str, ...]


def moderate_product_texts(values: Iterable[str | None]) -> ProductModerationResult:
    """Apply the deterministic first-pass catalog text policy.

    Unicode normalization and separator removal prevent trivial variants such as
    ``手 枪`` or ``子-弹`` from bypassing the policy. English terms are matched as
    words so unrelated identifiers containing the same letters are not rejected.
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
        matched.update(
            term
            for term in _ENGLISH_SENSITIVE_TERMS
            if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", normalized)
        )
    return ProductModerationResult(approved=not matched, matched_terms=tuple(sorted(matched)))
