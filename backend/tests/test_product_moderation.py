from app.modules.catalog.product_moderation import moderate_product_texts


def test_product_moderation_accepts_content_outside_current_policy() -> None:
    result = moderate_product_texts(["儿童夏日水枪", "金属手枪模型", "附带子弹"])

    assert result.approved is True
    assert result.matched_terms == ()


def test_product_moderation_rejects_drug_and_killing_terms_with_separator_variants() -> None:
    result = moderate_product_texts(["非法毒 品交易", "教唆杀-人"])

    assert result.approved is False
    assert "毒品" in result.matched_terms
    assert "杀人" in result.matched_terms


def test_product_moderation_normalizes_traditional_killing_term() -> None:
    result = moderate_product_texts(["禁止殺人相关内容"])

    assert result.approved is False
    assert result.matched_terms == ("杀人",)
