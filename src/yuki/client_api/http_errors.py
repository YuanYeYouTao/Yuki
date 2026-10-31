"""Credential-safe errors shared by the two finite HTTP entry points."""

from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from yuki.client_api.errors import ClientError
from yuki.identity.errors import CanonicalIdentityError
from yuki.persistence.database import (
    write_failure_committed,
    write_failure_phase,
    write_failure_retryable,
)


def unknown_write_response(exc: BaseException) -> JSONResponse | None:
    if write_failure_phase(exc) is not None and write_failure_committed(exc) is not False:
        return JSONResponse({"problem": {"code": "unknown"}}, status_code=500)
    return None


async def client_error_response(_request: Any, exc: Any) -> JSONResponse:
    unknown = unknown_write_response(exc)
    if unknown is not None:
        return unknown
    status = {
        "unauthenticated": 401,
        "owner_required": 403,
        "forbidden": 403,
        "not_found": 404,
        "validation_error": 422,
        "unavailable": 503,
    }.get(exc.code, 409)
    return JSONResponse({"problem": {"code": exc.code, **exc.details}}, status_code=status)


async def credential_safe_validation(request: Any, exc: Any) -> JSONResponse:
    if request.url.path.startswith(("/client/v1/", "/admin/v1/", "/agents/v1/")):
        return JSONResponse({"problem": {"code": "validation_error"}}, status_code=422)
    from fastapi.exception_handlers import request_validation_exception_handler

    return await request_validation_exception_handler(request, exc)


def install_identity_errors(app: FastAPI) -> None:
    app.add_exception_handler(ClientError, client_error_response)

    async def identity_error(_request: Any, exc: Any) -> JSONResponse:
        unknown = unknown_write_response(exc)
        if unknown is not None:
            return unknown
        return JSONResponse(
            {"problem": {"code": exc.category}},
            status_code=(
                404 if exc.category in {"missing_canonical_owner", "unclassified"} else 409
            ),
        )

    app.add_exception_handler(CanonicalIdentityError, identity_error)
    app.add_exception_handler(RequestValidationError, credential_safe_validation)

    async def database_error(_request: Any, exc: Any) -> JSONResponse:
        # Only the owner can establish that a BUSY failed before any commit.
        # COMMIT/rollback/close uncertainty keeps the original request unknown.
        busy = write_failure_retryable(exc)
        return JSONResponse(
            {"problem": {"code": "unavailable" if busy else "unknown"}},
            status_code=503 if busy else 500,
        )

    app.add_exception_handler(SQLAlchemyError, database_error)
