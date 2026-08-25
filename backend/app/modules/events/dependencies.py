from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession, get_security_service
from app.core.security import SecurityService
from app.modules.events.service import DeadLetterService


def get_dead_letter_service(
    session: DatabaseSession,
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> DeadLetterService:
    return DeadLetterService(session, security)


DeadLetterServiceDependency = Annotated[DeadLetterService, Depends(get_dead_letter_service)]
