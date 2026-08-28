from app.modules.catalog.product_moderation import moderate_product_texts


def test_product_moderation_accepts_ordinary_and_toy_water_gun_content() -> None:
    result = moderate_product_texts(["儿童夏日水枪", "户外亲子戏水玩具", "安全材质"])

    assert result.approved is True
    assert result.matched_terms == ()


def test_product_moderation_rejects_firearm_terms_with_separator_variants() -> None:
    result = moderate_product_texts(["金属手 枪模型", "附带子-弹"])

    assert result.approved is False
    assert "手枪" in result.matched_terms
    assert "子弹" in result.matched_terms


def test_product_moderation_normalizes_traditional_and_english_terms() -> None:
    result = moderate_product_texts(["步槍零件", "live ammunition only"])

    assert result.approved is False
    assert "步枪" in result.matched_terms
    assert "ammunition" in result.matched_terms
