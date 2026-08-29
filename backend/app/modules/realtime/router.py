from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from redis.asyncio import Redis

from app.api.dependencies import AdminContext, UserContext
from app.api.schemas import Envelope
from app.core.config import Settings, get_settings
from app.database.redis import get_redis
from app.modules.rbac.dependencies import AdminAccess, require_admin_permission
from app.modules.realtime.schemas import RealtimeTicketView
from app.modules.realtime.tickets import RealtimeTicketService

router = APIRouter(prefix="/realtime", tags=["realtime"])
support_router = APIRouter(prefix="/support/realtime", tags=["support-realtime"])


def _ticket_service(
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RealtimeTicketService:
    return RealtimeTicketService(redis, settings)


@router.post(
    "/tickets",
    response_model=Envelope[RealtimeTicketView],
    operation_id="RealtimeTicket_CreateMine",
)
async def create_user_ticket(
    response: Response,
    context: UserContext,
    service: Annotated[RealtimeTicketService, Depends(_ticket_service)],
) -> Envelope[RealtimeTicketView]:
    ticket, expires_in = await service.issue(context, "user")
    response.headers["Cache-Control"] = "no-store"
    return Envelope(data=RealtimeTicketView(ticket=ticket, expires_in=expires_in))


@support_router.post(
    "/tickets",
    response_model=Envelope[RealtimeTicketView],
    operation_id="SupportRealtimeTicket_Create",
)
async def create_support_ticket(
    response: Response,
    context: AdminContext,
    _access: Annotated[AdminAccess, require_admin_permission("support:queue_read")],
    service: Annotated[RealtimeTicketService, Depends(_ticket_service)],
) -> Envelope[RealtimeTicketView]:
    ticket, expires_in = await service.issue(context, "admin")
    response.headers["Cache-Control"] = "no-store"
    return Envelope(data=RealtimeTicketView(ticket=ticket, expires_in=expires_in))
