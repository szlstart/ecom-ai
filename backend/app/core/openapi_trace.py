from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.core.exceptions import ProblemDetails
from app.generated.operation_trace_catalog import OPERATIONS

PROBLEM_DESCRIPTIONS = {
    "401": "Authentication is missing, expired, revoked, or has the wrong audience.",
    "403": "The authenticated subject lacks the required permission or data scope.",
    "404": "The resource does not exist or is deliberately hidden by ownership rules.",
    "409": "The command conflicts with current state, replay scope, or a concurrent command.",
    "412": "The supplied entity version is stale.",
    "428": "A required precondition such as If-Match is missing.",
    "429": "The caller exceeded an API or Agent rate limit.",
    "500": "The service failed without exposing internal details.",
    "503": "A required dependency is unavailable or the write outcome is unknown.",
}


def _problem_response(status: str) -> dict[str, Any]:
    return {"$ref": f"#/components/responses/Problem{status}"}


def _problem_response_component(status: str) -> dict[str, Any]:
    return {
        "description": PROBLEM_DESCRIPTIONS[status],
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/ProblemDetails"}
            }
        },
    }


def _document_problem_responses(operation: dict[str, Any], path: str, method: str) -> None:
    operation_id = operation.get("operationId")
    contract = OPERATIONS.get(operation_id) if isinstance(operation_id, str) else None
    if contract is None:
        return
    statuses = {"429", "500", "503"}
    if operation.get("security"):
        statuses.add("401")
    if contract["x-permission-codes"]:
        statuses.add("403")
    if "{" in path:
        statuses.add("404")
    policy = str(contract["x-idempotency-policy"])
    if method not in {"GET", "HEAD", "OPTIONS"}:
        statuses.add("409")
    if "if_match_required" in policy:
        statuses.update({"412", "428"})
    responses = operation.setdefault("responses", {})
    for status in sorted(statuses):
        responses.setdefault(status, _problem_response(status))


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
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components["ProblemDetails"] = ProblemDetails.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        response_components = schema["components"].setdefault("responses", {})
        for status in PROBLEM_DESCRIPTIONS:
            response_components[f"Problem{status}"] = _problem_response_component(status)
        for path, path_item in schema.get("paths", {}).items():
            for method, operation in path_item.items():
                if not isinstance(operation, dict):
                    continue
                operation_id = operation.get("operationId")
                contract = OPERATIONS.get(operation_id) if isinstance(operation_id, str) else None
                if contract is not None:
                    operation.update(contract)
                    _document_problem_responses(operation, path, method.upper())
        app.openapi_schema = schema
        return schema

    app.openapi = traced_openapi  # type: ignore[method-assign]
