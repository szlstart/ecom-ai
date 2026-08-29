from app.modules.identity.access_policy import classify_identity_grants


def test_consumer_identity_is_exclusive_to_user_portal() -> None:
    eligibility = classify_identity_grants([("user", "platform", 0)])

    assert eligibility.consumer
    assert not eligibility.merchant
    assert not eligibility.platform_admin
    assert eligibility.allows_session("user", "web")
    assert not eligibility.allows_session("admin", "merchant")
    assert not eligibility.allows_session("admin", "admin_password")


def test_merchant_identity_is_exclusive_to_merchant_portal() -> None:
    eligibility = classify_identity_grants([("store_operator", "store", 7)])

    assert not eligibility.consumer
    assert eligibility.merchant
    assert not eligibility.platform_admin
    assert eligibility.allows_session("admin", "merchant")
    assert not eligibility.allows_session("user", "web")
    assert not eligibility.allows_session("admin", "admin_password")


def test_platform_identity_is_exclusive_to_admin_portal() -> None:
    eligibility = classify_identity_grants([("platform_super_admin", "platform", 0)])

    assert not eligibility.consumer
    assert not eligibility.merchant
    assert eligibility.platform_admin
    assert eligibility.allows_session("admin", "admin_password")
    assert eligibility.allows_session("admin", "admin")
    assert not eligibility.allows_session("user", "web")
    assert not eligibility.allows_session("admin", "merchant")


def test_mixed_consumer_and_privileged_grants_fail_closed() -> None:
    user_and_merchant = classify_identity_grants(
        [("user", "platform", 0), ("store_operator", "store", 7)]
    )
    user_and_admin = classify_identity_grants(
        [("user", "platform", 0), ("platform_super_admin", "platform", 0)]
    )
    admin_and_store = classify_identity_grants(
        [("platform_super_admin", "platform", 0), ("store_operator", "store", 7)]
    )

    for eligibility in (user_and_merchant, user_and_admin, admin_and_store):
        assert not eligibility.consumer
        assert not eligibility.merchant
        assert not eligibility.platform_admin


def test_invalid_scope_ids_do_not_create_an_identity() -> None:
    invalid_user = classify_identity_grants([("user", "platform", 1)])
    invalid_merchant = classify_identity_grants([("store_operator", "store", 0)])
    invalid_admin = classify_identity_grants([("platform_super_admin", "platform", 9)])

    assert not invalid_user.consumer
    assert not invalid_merchant.merchant
    assert not invalid_admin.platform_admin


def test_malformed_grant_cannot_hide_behind_a_valid_identity() -> None:
    consumer_with_malformed_user = classify_identity_grants(
        [("user", "platform", 0), ("user", "store", 7)]
    )
    merchant_with_malformed_admin = classify_identity_grants(
        [("store_operator", "store", 7), ("platform_operator", "platform", 9)]
    )
    admin_with_malformed_store_operator = classify_identity_grants(
        [
            ("platform_super_admin", "platform", 0),
            ("store_operator", "queue", 7),
        ]
    )

    assert not consumer_with_malformed_user.consumer
    assert not merchant_with_malformed_admin.merchant
    assert not admin_with_malformed_store_operator.platform_admin
