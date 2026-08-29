from app.modules.reviews.moderation import classify_review_content


def test_review_moderation_auto_publishes_normal_feedback() -> None:
    decision = classify_review_content("包装很好，键盘手感符合预期")
    assert decision.status == "passed"
    assert decision.rule_code == "DETERMINISTIC_SAFE_CONTENT"


def test_review_moderation_blocks_only_high_confidence_illegal_content() -> None:
    decision = classify_review_content("这里可以出售毒品")
    assert decision.status == "blocked"


def test_review_moderation_sends_ambiguous_or_contact_content_to_manual_queue() -> None:
    assert classify_review_content("内容涉及毒品，但我是在投诉").status == "manual"
    assert classify_review_content("加微信了解详情").status == "manual"
