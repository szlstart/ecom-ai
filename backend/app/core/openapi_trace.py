from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.generated.operation_trace_catalog import OPERATIONS


def install_traced_openapi(app: FastAPI) -> None:
    def traced_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            summary=app.summary,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
            servers=app.servers,
            terms_of_service=app.terms_of_service,
            contact=app.contact,
            license_info=app.license_info,
            separate_input_output_schemas=app.separate_input_output_schemas,
        )
        for path_item in schema.get("paths", {}).values():
            for operation in path_item.values():
                if not isinstance(operation, dict):
                    continue
                operation_id = operation.get("operationId")
                contract = OPERATIONS.get(operation_id) if isinstance(operation_id, str) else None
                if contract is not None:
                    operation.update(contract)
        app.openapi_schema = schema
        return schema

    app.openapi = traced_openapi  # type: ignore[method-assign]
