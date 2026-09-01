import json

from app.modules.agent_runtime.conversation_summary import MAX_SUMMARY_CHARACTERS, _compact_summary


def test_compact_summary_is_a_bounded_deduplicated_conversation_dossier() -> None:
    previous = "\n".join(
        (
            "[不可信对话连续性摘要; 涉及订单、金额、库存、权限和状态时必须调用业务工具重新核验]",
            "用户: 偏好安静键盘",
            "用户: 偏好安静键盘",
            *(f"AI客服: 旧对话 {index} " + "字" * 180 for index in range(30)),
        )
    )
    result = _compact_summary(previous, ["用户: 确认继续比较", "用户: 确认继续比较"])
    dossier = json.loads(result)

    assert len(result) <= MAX_SUMMARY_CHARACTERS
    assert dossier["schema_version"] == "conversation_dossier_v1"
    assert dossier["business_fact_authoritative"] is False
    assert dossier["current_goal"] == "确认继续比较"
    assert result.count("用户: 确认继续比较") == 1


def test_compact_summary_preserves_goal_constraints_commitments_and_open_questions() -> None:
    first = _compact_summary(
        "",
        [
            "用户: 我预算 500 元，不要太吵的键盘",
            "AI客服: 我会继续帮你对比库存。你更想先看声音还是价格?",
        ],
    )
    second = _compact_summary(
        first,
        [
            "用户: 好",
            "AI客服: 已查到两个候选商品",
        ],
    )
    dossier = json.loads(second)

    assert dossier["current_goal"] == "我预算 500 元，不要太吵的键盘"
    assert dossier["user_constraints"] == ["我预算 500 元，不要太吵的键盘"]
    assert dossier["commitments"] == ["我会继续帮你对比库存。你更想先看声音还是价格?"]
    assert dossier["unresolved_questions"] == [
        "我会继续帮你对比库存。你更想先看声音还是价格?"
    ]
    assert dossier["completed_actions"] == ["已查到两个候选商品"]
