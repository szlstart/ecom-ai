from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession
from app.modules.messaging.service import MessagingService


def get_messaging_service(session: DatabaseSession) -> MessagingService:
    return MessagingService(session)


MessagingServiceDependency = Annotated[MessagingService, Depends(get_messaging_service)]
