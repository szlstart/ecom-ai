from app.main import create_app
from app.modules.files.policies import upload_policy


def test_review_eligibility_and_create_contract_is_published() -> None:
    schema = create_app().openapi()
    assert (
        schema["paths"]["/api/v1/review-eligibilities/{order_item_id}"]["get"]["operationId"]
        == "ReviewEligibility_Get"
    )
    assert schema["paths"]["/api/v1/reviews"]["post"]["operationId"] == "Review_Create"
    request = schema["components"]["schemas"]["ReviewCreateRequest"]
    assert request["additionalProperties"] is False
    assert request["properties"]["rating"]["minimum"] == 1
    assert request["properties"]["rating"]["maximum"] == 5
    assert request["properties"]["content"]["anyOf"][0]["maxLength"] == 500
    assert request["properties"]["image_file_ids"]["maxItems"] == 6


def test_review_image_upload_policy_is_user_owned_and_bounded() -> None:
    policy = upload_policy("review_image")
    assert policy.owner_type == "user"
    assert policy.max_count == 6
    assert policy.processor == "public_image"
