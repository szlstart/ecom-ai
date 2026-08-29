from app.core.exceptions import ApplicationError

PAYMENT_TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    "MarkProviderAccepted": (frozenset({"created"}), "pending"),
    "ConfirmPaymentSucceeded": (frozenset({"created", "pending"}), "succeeded"),
    "ConfirmPaymentFailed": (frozenset({"created", "pending"}), "failed"),
    "ClosePaymentAttempt": (frozenset({"created", "pending"}), "closed"),
    "RecordPartialRefund": (frozenset({"succeeded"}), "partially_refunded"),
    "RecordFullRefund": (frozenset({"succeeded", "partially_refunded"}), "refunded"),
}


def require_payment_transition(current: str, command: str) -> str:
    transition = PAYMENT_TRANSITIONS.get(command)
    if transition is None or current not in transition[0]:
        raise ApplicationError(
            status=409,
            code="PAYMENT_STATE_CONFLICT",
            title="Payment state conflict",
            detail=f"支付单当前状态不允许执行 {command}。",
        )
    return transition[1]
