from typing import Annotated

from fastapi import Depends

from app.api.dependencies import DatabaseSession
from app.core.config import get_settings
from app.modules.batch_jobs.service import BatchJobService


def get_batch_job_service(session: DatabaseSession) -> BatchJobService:
    return BatchJobService(session, get_settings())


BatchJobServiceDependency = Annotated[BatchJobService, Depends(get_batch_job_service)]
