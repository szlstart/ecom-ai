from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Response

from app.api.dependencies import DatabaseSession, PostgresSession
from app.api.schemas import Envelope
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.messaging.deletion_service import ConversationDeletionService
from app.modules.messaging.schemas import (
    ConversationClearView,
    ConversationDeletionView,
    MessageList,
    MessageView,
    ReadCursorRequest,
)
from app.modules.messaging.support_schemas import (
    SupportConversationList,
    SupportInternalNoteList,
    SupportInternalNoteRequest,
    SupportMessageRequest,
    SupportReadCursorView,
    SupportResolveRequest,
    SupportTicketList,
    SupportTicketView,
    SupportTransferRequest,
    SupportWaitRequest,
    SupportWorkspaceView,
)
from app.modules.messaging.support_service import SupportService
from app.modules.rbac.dependencies import AdminAccess, require_admin_permission

router = APIRouter(prefix="/support", tags=["support-workspace"])


def _service(session: DatabaseSession) -> SupportService:
    return SupportService(session)


@router.get(
    "/conversations",
    response_model=Envelope[SupportConversationList],
    operation_id="SupportConversation_List",
)
async def list_support_conversations(
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("support:queue_read")],
    participant_type: Literal["user", "merchant"] | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> Envelope[SupportConversationList]:
    result = await _service(session).list_conversations(access, participant_type, limit)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/human-service-tickets",
    response_model=Envelope[SupportTicketList],
    operation_id="SupportTicket_List",
)
async def list_tickets(
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("support:queue_read")],
    queue_type: Literal["store", "platform"] | None = None,
    ticket_status: Literal["queued", "assigned", "active", "waiting_user", "resolved", "closed"]
    | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[SupportTicketList]:
    result = await _service(session).list(access, queue_type, ticket_status, limit)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/human-service-tickets/{ticket_id}",
    response_model=Envelope[SupportTicketView],
    operation_id="SupportTicket_Get",
)
async def get_ticket(
    ticket_id: str,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("support:queue_read")],
) -> Envelope[SupportTicketView]:
    result = await _service(session).get(access, ticket_id)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/human-service-tickets/{ticket_id}/workspace",
    response_model=Envelope[SupportWorkspaceView],
    operation_id="SupportWorkspace_Get",
)
async def get_workspace(
    ticket_id: str,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("support:queue_read")],
) -> Envelope[SupportWorkspaceView]:
    result = await _service(session).workspace(access, ticket_id)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=Envelope[MessageList],
    operation_id="SupportMessage_List",
)
async def list_support_messages(
    conversation_id: str,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("support:queue_read")],
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
) -> Envelope[MessageList]:
    result = await _service(session).conversation_messages(
        access, conversation_id, limit, cursor, after_sequence
    )
    _no_store(response)
    return Envelope(data=result)


@router.delete(
    "/conversations/{conversation_id}",
    response_model=Envelope[ConversationDeletionView],
    operation_id="SupportConversation_Delete",
)
async def delete_support_conversation(
    conversation_id: str,
    response: Response,
    session: DatabaseSession,
    postgres: PostgresSession,
    access: Annotated[AdminAccess, require_admin_permission("support:reply")],
) -> Envelope[ConversationDeletionView]:
    result = await ConversationDeletionService(session, postgres).delete_scoped(
        access, conversation_id
    )
    _no_store(response)
    return Envelope(data=result)


@router.delete(
    "/conversations/{conversation_id}/messages",
    response_model=Envelope[ConversationClearView],
    operation_id="SupportConversation_ClearMessages",
)
async def clear_support_conversation_messages(
    conversation_id: str,
    response: Response,
    session: DatabaseSession,
    postgres: PostgresSession,
    access: Annotated[AdminAccess, require_admin_permission("support:reply")],
) -> Envelope[ConversationClearView]:
    result = await ConversationDeletionService(session, postgres).clear_scoped(
        access, conversation_id
    )
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=Envelope[MessageView],
    operation_id="SupportConversationMessage_Send",
)
async def send_support_conversation_message(
    conversation_id: str,
    payload: SupportMessageRequest,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("support:reply")],
) -> Envelope[MessageView]:
    result = await _service(session).send_conversation(access, conversation_id, payload)
    _no_store(response)
    return Envelope(data=result)


@router.put(
    "/conversations/{conversation_id}/read-cursor",
    response_model=Envelope[SupportReadCursorView],
    operation_id="SupportReadCursor_Put",
)
async def put_support_read_cursor(
    conversation_id: str,
    payload: ReadCursorRequest,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("support:queue_read")],
) -> Envelope[SupportReadCursorView]:
    result = await _service(session).read_conversation(access, conversation_id, payload)
    response.headers["ETag"] = _etag(result.cursor_version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/human-service-tickets/{ticket_id}/claims",
    response_model=Envelope[SupportTicketView],
    operation_id="SupportTicket_Claim",
)
async def claim_ticket(
    ticket_id: str,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("support:claim")],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> Envelope[SupportTicketView]:
    result = await _service(session).claim(
        access, ticket_id, _expected_version(if_match), idempotency_key
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/human-service-tickets/{ticket_id}/waits",
    response_model=Envelope[SupportTicketView],
    operation_id="SupportTicket_Wait",
)
async def wait_ticket(
    ticket_id: str,
    payload: SupportWaitRequest,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("support:wait")],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> Envelope[SupportTicketView]:
    result = await _service(session).wait(
        access, ticket_id, payload, _expected_version(if_match), idempotency_key
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/human-service-tickets/{ticket_id}/resumptions",
    response_model=Envelope[SupportTicketView],
    operation_id="SupportTicket_Resume",
)
async def resume_ticket(
    ticket_id: str,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("support:resume")],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> Envelope[SupportTicketView]:
    result = await _service(session).resume(
        access, ticket_id, _expected_version(if_match), idempotency_key
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/human-service-tickets/{ticket_id}/transfers",
    response_model=Envelope[SupportTicketView],
    operation_id="SupportTicket_Transfer",
)
async def transfer_ticket(
    ticket_id: str,
    payload: SupportTransferRequest,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("support:transfer")],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> Envelope[SupportTicketView]:
    result = await _service(session).transfer(
        access, ticket_id, payload, _expected_version(if_match), idempotency_key
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/human-service-tickets/{ticket_id}/resolutions",
    response_model=Envelope[SupportTicketView],
    operation_id="SupportTicket_Resolve",
)
async def resolve_ticket(
    ticket_id: str,
    payload: SupportResolveRequest,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("support:resolve")],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> Envelope[SupportTicketView]:
    result = await _service(session).resolve(
        access, ticket_id, payload, _expected_version(if_match), idempotency_key
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/human-service-tickets/{ticket_id}/messages",
    response_model=Envelope[MessageView],
    operation_id="SupportMessage_Send",
)
async def send_support_message(
    ticket_id: str,
    payload: SupportMessageRequest,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("support:reply")],
) -> Envelope[MessageView]:
    result = await _service(session).send(access, ticket_id, payload)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/human-service-tickets/{ticket_id}/internal-notes",
    response_model=Envelope[SupportTicketView],
    operation_id="SupportInternalNote_Create",
)
async def create_internal_note(
    ticket_id: str,
    payload: SupportInternalNoteRequest,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("support:internal_notes_write")],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> Envelope[SupportTicketView]:
    result = await _service(session).note(
        access, ticket_id, payload, _expected_version(if_match), idempotency_key
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/human-service-tickets/{ticket_id}/internal-notes",
    response_model=Envelope[SupportInternalNoteList],
    operation_id="SupportInternalNote_List",
)
async def list_internal_notes(
    ticket_id: str,
    response: Response,
    session: DatabaseSession,
    access: Annotated[AdminAccess, require_admin_permission("support:internal_notes_read")],
) -> Envelope[SupportInternalNoteList]:
    result = await _service(session).internal_notes(access, ticket_id)
    _no_store(response)
    return Envelope(data=result)
