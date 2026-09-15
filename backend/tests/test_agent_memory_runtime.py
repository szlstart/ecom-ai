from app.modules.agent_runtime.memory_runtime import (
    explicit_memory_request,
    is_sensitive_explicit_memory_request,
)


def test_explicit_memory_request_requires_an_explicit_safe_preference() -> None:
    assert explicit_memory_request("请记住\uff1a我喜欢海盐蓝色的商品") == "我喜欢海盐蓝色的商品"
    assert explicit_memory_request("请记住我喜欢蓝色、简约风格的文具") == (
        "我喜欢蓝色、简约风格的文具"
    )
    assert explicit_memory_request("帮我记住 我预算不超过 300 元。") == "我预算不超过 300 元"
    assert explicit_memory_request("以后推荐文具时，记住我喜欢蓝色、单价二十元以内的") == (
        "我喜欢蓝色、单价二十元以内的"
    )
    assert explicit_memory_request("我喜欢蓝色商品") is None
    assert explicit_memory_request("按我记住的偏好推荐考试文具") is None
    assert explicit_memory_request("请记住我喜欢蓝色、20元以内的文具，并顺便推荐3款") == (
        "我喜欢蓝色、20元以内的文具"
    )
    assert explicit_memory_request(
        "以后买文具预算改成50元以内，请记住，但先不要真正保存"
    ) == "以后买文具预算改成50元以内"
    assert explicit_memory_request("请记住\uff1a我的银行卡是 6222021234567890123") is None
    assert explicit_memory_request("请记住\uff1a我的邮箱是 user@example.com") is None
    assert is_sensitive_explicit_memory_request(
        "请记住我的银行卡号是6222021234567890，以后付款用"
    ) is True
    assert is_sensitive_explicit_memory_request("记住我喜欢蓝色文具") is False
