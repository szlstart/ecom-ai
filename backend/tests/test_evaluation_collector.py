from app.modules.evaluation.collector import _score_route


def test_abstention_accepts_safe_read_preflight_and_conservative_denial() -> None:
    assert _score_route(
        expected="abstain",
        expected_tool=None,
        decision="tool_supported",
        tool_code="logistics.get_user_order_shipments",
        allowed_tools=["logistics.get_user_order_shipments"],
    ) == (True, True, 0)
    assert _score_route(
        expected="abstain",
        expected_tool=None,
        decision="deny",
        tool_code=None,
        allowed_tools=["knowledge.search"],
    ) == (True, True, 0)


def test_denial_case_still_blocks_tool_execution() -> None:
    assert _score_route(
        expected="deny",
        expected_tool=None,
        decision="tool_supported",
        tool_code="order.list_user_orders",
        allowed_tools=["order.list_user_orders"],
    ) == (False, False, 1)
