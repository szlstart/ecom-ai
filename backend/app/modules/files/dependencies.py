from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession
from app.core.config import get_settings
from app.integrations.object_storage import ObjectStorage, get_object_storage
from app.modules.files.service import FileService


def get_file_service(
    session: DatabaseSession,
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> FileService:
    return FileService(session, get_settings(), storage)


FileServiceDependency = Annotated[FileService, Depends(get_file_service)]
