from app.modules.agent_runtime.memory_runtime import explicit_memory_request


def test_explicit_memory_request_requires_an_explicit_safe_preference() -> None:
    assert explicit_memory_request("请记住\uff1a我喜欢海盐蓝色的商品") == "我喜欢海盐蓝色的商品"
    assert explicit_memory_request("帮我记住 我预算不超过 300 元。") == "我预算不超过 300 元"
    assert explicit_memory_request("我喜欢蓝色商品") is None
    assert explicit_memory_request("请记住\uff1a我的银行卡是 6222021234567890123") is None
    assert explicit_memory_request("请记住\uff1a我的邮箱是 user@example.com") is None
