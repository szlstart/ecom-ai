from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from app.api.dependencies import IdempotencyKey, MerchantContext, PlatformAdminContext, UserContext
from app.api.schemas import Envelope
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.messaging.dependencies import (
    AiFeedbackServiceDependency,
    MessagingServiceDependency,
)
from app.modules.messaging.feedback_schemas import (
    AiFeedbackDetailRequest,
    AiFeedbackView,
    AiReactionRequest,
)
from app.modules.messaging.human_schemas import HumanHandoffRequest, HumanTicketView
from app.modules.messaging.schemas import (
    ConversationArchiveView,
    ConversationContextClearView,
    ConversationContextRequest,
    ConversationContextView,
    ConversationList,
    ConversationView,
    MessageCreateRequest,
    MessageList,
    MessageView,
    ReadCursorRequest,
    ReadCursorView,
)

router = APIRouter(tags=["messaging"])
merchant_router = APIRouter(prefix="/merchant/support", tags=["merchant-support"])
admin_ai_router = APIRouter(prefix="/admin/support/ai-conversation", tags=["admin-support"])


@admin_ai_router.put(
    "",
    response_model=Envelope[ConversationView],
    operation_id="AdminAiConversation_PutMine",
)
async def put_admin_ai_conversation(
    response: Response,
    context: PlatformAdminContext,
    service: MessagingServiceDependency,
) -> Envelope[ConversationView]:
    result = await service.get_or_create_exclusive(context.user)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@admin_ai_router.get(
    "/messages",
    response_model=Envelope[MessageList],
    operation_id="AdminAiMessage_ListMine",
)
async def list_admin_ai_messages(
    response: Response,
    context: PlatformAdminContext,
    service: MessagingServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> Envelope[MessageList]:
    conversation = await service.get_or_create_exclusive(context.user)
    result = await service.messages(context.user, conversation.conversation_id, limit, 0)
    _no_store(response)
    return Envelope(data=result)


@admin_ai_router.post(
    "/messages",
    response_model=Envelope[MessageView],
    status_code=status.HTTP_201_CREATED,
    operation_id="AdminAiMessage_CreateMine",
)
async def send_admin_ai_message(
    payload: MessageCreateRequest,
    response: Response,
    context: PlatformAdminContext,
    service: MessagingServiceDependency,
) -> Envelope[MessageView]:
    conversation = await service.get_or_create_exclusive(context.user)
    result = await service.send(context.user, conversation.conversation_id, payload)
    _no_store(response)
    return Envelope(data=result)


@admin_ai_router.put(
    "/read-cursor",
    response_model=Envelope[ReadCursorView],
    operation_id="AdminAiReadCursor_PutMine",
)
async def put_admin_ai_read_cursor(
    payload: ReadCursorRequest,
    response: Response,
    context: PlatformAdminContext,
    service: MessagingServiceDependency,
) -> Envelope[ReadCursorView]:
    conversation = await service.get_or_create_exclusive(context.user)
    result = await service.read(context.user, conversation.conversation_id, payload)
    _no_store(response)
    return Envelope(data=result)


@merchant_router.put(
    "/exclusive-conversation",
    response_model=Envelope[ConversationView],
    operation_id="MerchantExclusiveConversation_PutMine",
)
async def put_merchant_exclusive(
    response: Response, context: MerchantContext, service: MessagingServiceDependency
) -> Envelope[ConversationView]:
    result = await service.get_or_create_exclusive(context.user)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@merchant_router.get(
    "/exclusive-conversation/messages",
    response_model=Envelope[MessageList],
    operation_id="MerchantExclusiveMessage_ListMine",
)
async def list_merchant_exclusive_messages(
    response: Response,
    context: MerchantContext,
    service: MessagingServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> Envelope[MessageList]:
    conversation = await service.get_or_create_exclusive(context.user)
    result = await service.messages(context.user, conversation.conversation_id, limit, 0)
    _no_store(response)
    return Envelope(data=result)


@merchant_router.post(
    "/exclusive-conversation/messages",
    response_model=Envelope[MessageView],
    status_code=status.HTTP_201_CREATED,
    operation_id="MerchantExclusiveMessage_CreateMine",
)
async def send_merchant_exclusive_message(
    payload: MessageCreateRequest,
    response: Response,
    context: MerchantContext,
    service: MessagingServiceDependency,
) -> Envelope[MessageView]:
    conversation = await service.get_or_create_exclusive(context.user)
    result = await service.send(context.user, conversation.conversation_id, payload)
    _no_store(response)
    return Envelope(data=result)


@merchant_router.put(
    "/exclusive-conversation/read-cursor",
    response_model=Envelope[ReadCursorView],
    operation_id="MerchantExclusiveReadCursor_PutMine",
)
async def put_merchant_exclusive_read_cursor(
    payload: ReadCursorRequest,
    response: Response,
    context: MerchantContext,
    service: MessagingServiceDependency,
) -> Envelope[ReadCursorView]:
    conversation = await service.get_or_create_exclusive(context.user)
    result = await service.read(
        context.user,
        conversation.conversation_id,
        payload,
    )
    _no_store(response)
    return Envelope(data=result)


@merchant_router.post(
    "/exclusive-conversation/human-service-tickets",
    response_model=Envelope[HumanTicketView],
    status_code=status.HTTP_201_CREATED,
    operation_id="MerchantHumanServiceRequest_CreateMine",
)
async def request_merchant_human_service(
    payload: HumanHandoffRequest,
    response: Response,
    context: MerchantContext,
    service: MessagingServiceDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
) -> Envelope[HumanTicketView]:
    conversation = await service.get_or_create_exclusive(context.user)
    result = await service.request_human(
        context.user, conversation.conversation_id, payload, idempotency_key
    )
    _no_store(response)
    return Envelope(data=result)


@router.put(
    "/conversations/{conversation_id}/messages/{message_id}/reaction",
    response_model=Envelope[AiFeedbackView],
    operation_id="AiFeedbackReaction_Put",
)
async def put_ai_feedback_reaction(
    conversation_id: str,
    message_id: str,
    payload: AiReactionRequest,
    response: Response,
    context: UserContext,
    service: AiFeedbackServiceDependency,
) -> Envelope[AiFeedbackView]:
    result = await service.put_reaction(context.user, conversation_id, message_id, payload.reaction)
    _no_store(response)
    return Envelope(data=result)


@router.delete(
    "/conversations/{conversation_id}/messages/{message_id}/reaction",
    response_model=Envelope[AiFeedbackView],
    operation_id="AiFeedbackReaction_Delete",
)
async def delete_ai_feedback_reaction(
    conversation_id: str,
    message_id: str,
    response: Response,
    context: UserContext,
    service: AiFeedbackServiceDependency,
) -> Envelope[AiFeedbackView]:
    result = await service.delete_reaction(context.user, conversation_id, message_id)
    _no_store(response)
    return Envelope(data=result)


def _feedback_detail_route(path: str, operation_id: str, feedback_type: str) -> None:
    async def endpoint(
        conversation_id: str,
        message_id: str,
        payload: AiFeedbackDetailRequest,
        response: Response,
        context: UserContext,
        service: AiFeedbackServiceDependency,
        idempotency_key: IdempotencyKey,
    ) -> Envelope[AiFeedbackView]:
        result = await service.create_detail(
            context.user, conversation_id, message_id, feedback_type, payload, idempotency_key
        )
        _no_store(response)
        return Envelope(data=result)

    endpoint.__name__ = operation_id
    router.add_api_route(
        path,
        endpoint,
        methods=["POST"],
        status_code=status.HTTP_201_CREATED,
        response_model=Envelope[AiFeedbackView],
        operation_id=operation_id,
    )


_feedback_detail_route(
    "/conversations/{conversation_id}/messages/{message_id}/reports",
    "AiFeedbackReport_Create",
    "report",
)
_feedback_detail_route(
    "/conversations/{conversation_id}/messages/{message_id}/corrections",
    "AiFeedbackCorrection_Create",
    "correction",
)


@router.get(
    "/conversations",
    response_model=Envelope[ConversationList],
    operation_id="Conversation_ListMine",
)
async def list_conversations(
    response: Response, context: UserContext, service: MessagingServiceDependency
) -> Envelope[ConversationList]:
    result = await service.list_mine(context.user)
    _no_store(response)
    return Envelope(data=result)


@router.put(
    "/users/me/exclusive-conversation",
    response_model=Envelope[ConversationView],
    operation_id="ExclusiveConversation_PutMine",
)
async def put_exclusive(
    response: Response, context: UserContext, service: MessagingServiceDependency
) -> Envelope[ConversationView]:
    result = await service.get_or_create_exclusive(context.user)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.put(
    "/stores/{store_id}/customer-service-conversation",
    response_model=Envelope[ConversationView],
    operation_id="StoreConversation_PutMine",
)
async def put_store_conversation(
    store_id: str,
    response: Response,
    context: UserContext,
    service: MessagingServiceDependency,
) -> Envelope[ConversationView]:
    result = await service.get_or_create_store(context.user, store_id)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/conversations/{conversation_id}",
    response_model=Envelope[ConversationView],
    operation_id="Conversation_GetMine",
)
async def get_conversation(
    conversation_id: str,
    response: Response,
    context: UserContext,
    service: MessagingServiceDependency,
) -> Envelope[ConversationView]:
    result = await service.detail(context.user, conversation_id)
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=Envelope[MessageList],
    operation_id="Message_ListMine",
)
async def list_messages(
    conversation_id: str,
    response: Response,
    context: UserContext,
    service: MessagingServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
) -> Envelope[MessageList]:
    result = await service.messages(context.user, conversation_id, limit, after_sequence)
    _no_store(response)
    return Envelope(data=result)


@router.put(
    "/conversations/{conversation_id}/contexts/{context_type}",
    response_model=Envelope[ConversationContextView],
    operation_id="ConversationContext_PutMine",
)
async def put_context(
    conversation_id: str,
    context_type: str,
    payload: ConversationContextRequest,
    response: Response,
    context: UserContext,
    service: MessagingServiceDependency,
    if_match: Annotated[str, Header(alias="If-Match")],
) -> Envelope[ConversationContextView]:
    result = await service.set_context(
        context.user, conversation_id, context_type, payload, _expected_version(if_match)
    )
    _no_store(response)
    return Envelope(data=result)


@router.delete(
    "/conversations/{conversation_id}/contexts/{context_type}",
    response_model=Envelope[ConversationContextClearView],
    operation_id="ConversationContext_DeleteMine",
)
async def delete_context(
    conversation_id: str,
    context_type: str,
    response: Response,
    context: UserContext,
    service: MessagingServiceDependency,
    if_match: Annotated[str, Header(alias="If-Match")],
) -> Envelope[ConversationContextClearView]:
    result = await service.clear_context(
        context.user, conversation_id, context_type, _expected_version(if_match)
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/conversations/{conversation_id}/archivals",
    response_model=Envelope[ConversationArchiveView],
    status_code=status.HTTP_201_CREATED,
    operation_id="ConversationArchive_CreateMine",
)
async def archive_conversation(
    conversation_id: str,
    response: Response,
    context: UserContext,
    service: MessagingServiceDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
    if_match: Annotated[str, Header(alias="If-Match")],
) -> Envelope[ConversationArchiveView]:
    result = await service.archive(
        context.user,
        conversation_id,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(result.version)
    _no_store(response)
    return Envelope(data=result)


@router.get(
    "/conversations/{conversation_id}/human-service-ticket",
    response_model=Envelope[HumanTicketView],
    operation_id="HumanServiceTicket_GetMine",
)
async def get_human_service_ticket(
    conversation_id: str,
    response: Response,
    context: UserContext,
    service: MessagingServiceDependency,
) -> Envelope[HumanTicketView]:
    result = await service.current_human_ticket(context.user, conversation_id)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/human-service-tickets/{ticket_id}/cancellations",
    response_model=Envelope[HumanTicketView],
    operation_id="HumanServiceTicketCancellation_CreateMine",
)
async def cancel_human_service_ticket(
    ticket_id: str,
    response: Response,
    context: UserContext,
    service: MessagingServiceDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
) -> Envelope[HumanTicketView]:
    result = await service.cancel_human_ticket(context.user, ticket_id, idempotency_key)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=Envelope[MessageView],
    status_code=status.HTTP_201_CREATED,
    operation_id="Message_CreateMine",
)
async def send_message(
    conversation_id: str,
    payload: MessageCreateRequest,
    response: Response,
    context: UserContext,
    service: MessagingServiceDependency,
) -> Envelope[MessageView]:
    result = await service.send(context.user, conversation_id, payload)
    _no_store(response)
    return Envelope(data=result)


@router.put(
    "/conversations/{conversation_id}/read-cursor",
    response_model=Envelope[ReadCursorView],
    operation_id="MessageReadCursor_PutMine",
)
async def put_read_cursor(
    conversation_id: str,
    payload: ReadCursorRequest,
    response: Response,
    context: UserContext,
    service: MessagingServiceDependency,
) -> Envelope[ReadCursorView]:
    result = await service.read(context.user, conversation_id, payload)
    response.headers["ETag"] = _etag(result.cursor_version)
    _no_store(response)
    return Envelope(data=result)


@router.post(
    "/conversations/{conversation_id}/human-service-tickets",
    response_model=Envelope[HumanTicketView],
    status_code=status.HTTP_201_CREATED,
    operation_id="HumanServiceRequest_CreateMine",
)
async def request_human_service(
    conversation_id: str,
    payload: HumanHandoffRequest,
    response: Response,
    context: UserContext,
    service: MessagingServiceDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)],
) -> Envelope[HumanTicketView]:
    result = await service.request_human(context.user, conversation_id, payload, idempotency_key)
    _no_store(response)
    return Envelope(data=result)
