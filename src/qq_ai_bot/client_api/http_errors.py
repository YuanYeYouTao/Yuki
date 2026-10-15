"""Credential-safe errors shared by the two finite HTTP entry points."""

import sqlite3
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from qq_ai_bot.client_api.errors import ClientError


async def client_error_response(_request: Any, exc: Any) -> JSONResponse:
    status = {
        "unauthenticated": 401,
        "not_found": 404,
        "validation_error": 422,
        "unavailable": 503,
    }.get(exc.code, 409)
    return JSONResponse({"problem": {"code": exc.code}}, status_code=status)


async def credential_safe_validation(request: Any, exc: Any) -> JSONResponse:
    if request.url.path.startswith(("/client/v1/", "/admin/v1/")):
        return JSONResponse({"problem": {"code": "validation_error"}}, status_code=422)
    from fastapi.exception_handlers import request_validation_exception_handler

    return await request_validation_exception_handler(request, exc)


def install_identity_errors(app: FastAPI) -> None:
    app.add_exception_handler(ClientError, client_error_response)
    app.add_exception_handler(RequestValidationError, credential_safe_validation)

    async def database_error(_request: Any, exc: Any) -> JSONResponse:
        # Busy before COMMIT has no committed effect; lost commit acknowledgements
        # stay unknown and must be recovered using the original request ID.
        busy = (
            isinstance(exc, OperationalError)
            and isinstance(exc.orig, sqlite3.Error)
            and getattr(exc.orig, "sqlite_errorcode", 0) & 0xFF
            in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            and exc.statement is not None
            and not exc.statement.lstrip().upper().startswith("COMMIT")
            and not any(
                note.startswith("rollback_failed:") for note in getattr(exc, "__notes__", ())
            )
        )
        return JSONResponse(
            {"problem": {"code": "unavailable" if busy else "unknown"}},
            status_code=503 if busy else 500,
        )

    app.add_exception_handler(SQLAlchemyError, database_error)
