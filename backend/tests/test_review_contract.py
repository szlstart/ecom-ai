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

    assert schema["paths"]["/api/v1/users/me/reviews"]["get"]["operationId"] == "Review_ListMine"
    detail_path = schema["paths"]["/api/v1/reviews/{review_id}"]
    assert detail_path["get"]["operationId"] == "Review_GetMine"
    assert detail_path["patch"]["operationId"] == "Review_Update"
    assert (
        schema["paths"]["/api/v1/reviews/{review_id}/append-records"]["post"]["operationId"]
        == "Review_Append"
    )
    update_request = schema["components"]["schemas"]["ReviewUpdateRequest"]
    append_request = schema["components"]["schemas"]["ReviewAppendCreateRequest"]
    assert update_request["additionalProperties"] is False
    assert update_request["properties"]["image_file_ids"]["maxItems"] == 6
    assert append_request["additionalProperties"] is False
    assert append_request["properties"]["content"]["maxLength"] == 500
    assert append_request["properties"]["image_file_ids"]["maxItems"] == 6

    admin_path = schema["paths"]["/api/v1/admin/reviews/{review_id}"]
    assert schema["paths"]["/api/v1/admin/reviews"]["get"]["operationId"] == "AdminReview_List"
    assert admin_path["get"]["operationId"] == "AdminReview_Get"
    assert (
        schema["paths"]["/api/v1/admin/reviews/{review_id}/replies"]["post"]["operationId"]
        == "AdminReview_Reply"
    )
    assert (
        schema["paths"]["/api/v1/admin/reviews/{review_id}/moderations"]["post"][
            "operationId"
        ]
        == "AdminReview_Moderate"
    )


def test_review_image_upload_policy_is_user_owned_and_bounded() -> None:
    policy = upload_policy("review_image")
    assert policy.owner_type == "user"
    assert policy.max_count == 6
    assert policy.processor == "public_image"
