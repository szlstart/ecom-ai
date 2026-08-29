from sqlalchemy import CheckConstraint, UniqueConstraint

from app.database.base import MySQLBase
from app.modules.payments import models as payment_models  # noqa: F401


def test_payment_attempt_schema_has_amount_state_and_provider_guards() -> None:
    assert {"payments", "payment_events", "payment_callbacks"} <= set(MySQLBase.metadata.tables)
    payments = MySQLBase.metadata.tables["payments"]
    events = MySQLBase.metadata.tables["payment_events"]
    payment_checks = {
        item.name for item in payments.constraints if isinstance(item, CheckConstraint)
    }
    payment_uniques = {
        item.name for item in payments.constraints if isinstance(item, UniqueConstraint)
    }
    event_uniques = {item.name for item in events.constraints if isinstance(item, UniqueConstraint)}
    assert {"ck_payments_payment_status", "ck_payments_payment_amounts"} <= payment_checks
    assert {"uk_payments_no", "uk_payments_provider_trade"} <= payment_uniques
    assert "uk_payment_events_no" in event_uniques
    callbacks = MySQLBase.metadata.tables["payment_callbacks"]
    callback_uniques = {
        item.name for item in callbacks.constraints if isinstance(item, UniqueConstraint)
    }
    callback_checks = {
        item.name for item in callbacks.constraints if isinstance(item, CheckConstraint)
    }
    assert {"uk_payment_callbacks_no", "uk_payment_callbacks_provider_event"} <= callback_uniques
    assert {
        "ck_payment_callbacks_payment_callback_signature_status",
        "ck_payment_callbacks_payment_callback_process_status",
    } <= callback_checks
