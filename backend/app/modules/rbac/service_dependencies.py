from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession, get_security_service
from app.core.security import SecurityService
from app.modules.rbac.service import RbacService


def get_rbac_service(
    session: DatabaseSession,
    security: Annotated[SecurityService, Depends(get_security_service)],
) -> RbacService:
    return RbacService(session, security)


RbacServiceDependency = Annotated[RbacService, Depends(get_rbac_service)]
