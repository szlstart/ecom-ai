from sqlalchemy import UniqueConstraint

from app.database.base import MySQLBase
from app.main import create_app
from app.modules.messaging import models as messaging_models  # noqa: F401


def test_messaging_schema_has_sequence_and_read_cursor_uniques() -> None:
    assert {"conversations", "messages", "message_reads", "conversation_contexts"} <= set(
        MySQLBase.metadata.tables
    )
    for table_name, name in (
        ("messages", "uk_messages_sequence"),
        ("messages", "uk_messages_client_no"),
        ("message_reads", "uk_message_reads_reader"),
    ):
        assert name in {
            item.name
            for item in MySQLBase.metadata.tables[table_name].constraints
            if isinstance(item, UniqueConstraint)
        }


def test_messaging_contract_is_published() -> None:
    paths = create_app().openapi()["paths"]
    assert paths["/api/v1/conversations"]["get"]["operationId"] == "Conversation_ListMine"
    assert (
        paths["/api/v1/conversations/{conversation_id}/messages"]["post"]["operationId"]
        == "Message_CreateMine"
    )
    assert (
        paths["/api/v1/conversations/{conversation_id}/contexts/{context_type}"]["put"][
            "operationId"
        ]
        == "ConversationContext_PutMine"
    )
    assert (
        paths["/api/v1/conversations/{conversation_id}/contexts/{context_type}"]["delete"][
            "operationId"
        ]
        == "ConversationContext_DeleteMine"
    )
    archive = paths["/api/v1/conversations/{conversation_id}/archivals"]["post"]
    assert archive["operationId"] == "ConversationArchive_CreateMine"
    archive_headers = {
        item["name"]
        for item in archive["parameters"]
        if item["in"] == "header" and item["required"]
    }
    assert {"If-Match", "Idempotency-Key"} <= archive_headers
    message_parameters = paths["/api/v1/conversations/{conversation_id}/messages"]["get"][
        "parameters"
    ]
    assert any(item["name"] == "after_sequence" for item in message_parameters)
    assert (
        paths["/api/v1/conversations/{conversation_id}/read-cursor"]["put"]["operationId"]
        == "MessageReadCursor_PutMine"
    )
    assert (
        paths["/api/v1/merchant/support/exclusive-conversation/read-cursor"]["put"][
            "operationId"
        ]
        == "MerchantExclusiveReadCursor_PutMine"
    )
    assert (
        paths["/api/v1/conversations/{conversation_id}/human-service-ticket"]["get"][
            "operationId"
        ]
        == "HumanServiceTicket_GetMine"
    )
    cancellation = paths["/api/v1/human-service-tickets/{ticket_id}/cancellations"]["post"]
    assert cancellation["operationId"] == "HumanServiceTicketCancellation_CreateMine"
    assert any(
        item["name"] == "Idempotency-Key" and item["required"]
        for item in cancellation["parameters"]
    )
    support = {
        "/api/v1/support/human-service-tickets": ("get", "SupportTicket_List"),
        "/api/v1/support/human-service-tickets/{ticket_id}/workspace": (
            "get",
            "SupportWorkspace_Get",
        ),
        "/api/v1/support/human-service-tickets/{ticket_id}/claims": (
            "post",
            "SupportTicket_Claim",
        ),
        "/api/v1/support/human-service-tickets/{ticket_id}/waits": (
            "post",
            "SupportTicket_Wait",
        ),
        "/api/v1/support/human-service-tickets/{ticket_id}/resumptions": (
            "post",
            "SupportTicket_Resume",
        ),
        "/api/v1/support/human-service-tickets/{ticket_id}/transfers": (
            "post",
            "SupportTicket_Transfer",
        ),
        "/api/v1/support/human-service-tickets/{ticket_id}/resolutions": (
            "post",
            "SupportTicket_Resolve",
        ),
        "/api/v1/support/human-service-tickets/{ticket_id}/messages": (
            "post",
            "SupportMessage_Send",
        ),
        "/api/v1/support/human-service-tickets/{ticket_id}/internal-notes": (
            "post",
            "SupportInternalNote_Create",
        ),
        "/api/v1/support/conversations/{conversation_id}/messages": (
            "get",
            "SupportMessage_List",
        ),
        "/api/v1/support/conversations/{conversation_id}/read-cursor": (
            "put",
            "SupportReadCursor_Put",
        ),
    }
    for path, (method, operation_id) in support.items():
        assert paths[path][method]["operationId"] == operation_id
    ticket_schema = create_app().openapi()["components"]["schemas"]["SupportTicketItem"]
    assert "unread_count" in ticket_schema["required"]
    assert ticket_schema["properties"]["unread_count"]["minimum"] == 0
    for path in (
        "/api/v1/support/human-service-tickets/{ticket_id}/claims",
        "/api/v1/support/human-service-tickets/{ticket_id}/waits",
        "/api/v1/support/human-service-tickets/{ticket_id}/resumptions",
        "/api/v1/support/human-service-tickets/{ticket_id}/transfers",
        "/api/v1/support/human-service-tickets/{ticket_id}/resolutions",
        "/api/v1/support/human-service-tickets/{ticket_id}/internal-notes",
    ):
        parameters = paths[path]["post"]["parameters"]
        required_headers = {
            item["name"] for item in parameters if item["in"] == "header" and item["required"]
        }
        assert {"If-Match", "Idempotency-Key"} <= required_headers


def test_realtime_ticket_contract_and_websocket_route_are_published() -> None:
    app = create_app()
    paths = app.openapi()["paths"]
    assert paths["/api/v1/realtime/tickets"]["post"]["operationId"] == (
        "RealtimeTicket_CreateMine"
    )
    assert paths["/api/v1/support/realtime/tickets"]["post"]["operationId"] == (
        "SupportRealtimeTicket_Create"
    )
    websocket_routes = {
        getattr(route, "path", None): getattr(route, "name", None) for route in app.routes
    }
    assert websocket_routes["/ws/v1"] == "realtime-websocket"


def test_internal_notes_are_not_messages() -> None:
    assert "human_service_internal_notes" in MySQLBase.metadata.tables
    columns = MySQLBase.metadata.tables["human_service_internal_notes"].c
    assert "content_ciphertext" in columns
    assert "content_hash" in columns
    assert "note_text" not in columns
    assert "conversation_id" not in columns


def test_human_support_has_assignment_and_immutable_event_resources() -> None:
    assert {"human_service_assignments", "human_service_ticket_events"} <= set(
        MySQLBase.metadata.tables
    )
    ticket_columns = MySQLBase.metadata.tables["human_service_tickets"].c
    assert {
        "queue_code",
        "handoff_message_refs",
        "handoff_policy_version",
        "sla_remaining_seconds",
        "resolution_summary",
        "resolution_note",
    } <= set(ticket_columns.keys())
    conversation_constraints = {
        item.name for item in MySQLBase.metadata.tables["conversations"].constraints
    }
    assert "ck_conversations_conversation_store_scope" in conversation_constraints
    conversation_indexes = {
        item.name for item in MySQLBase.metadata.tables["conversations"].indexes
    }
    assert "idx_conversations_user_visibility_updated" in conversation_indexes


def test_human_sender_and_conversation_states_are_not_conflated() -> None:
    schema = create_app().openapi()["components"]["schemas"]
    sender_values = schema["MessageView"]["properties"]["sender_type"]["enum"]
    conversation_values = schema["ConversationView"]["properties"]["conversation_status"]["enum"]
    assert "human" in sender_values
    assert "support" not in sender_values
    assert "human_waiting" not in conversation_values
