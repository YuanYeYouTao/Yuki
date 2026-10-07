"""Finite reviewed HTTP routes; original services own all authorization and effects."""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.staticfiles import StaticFiles

from qq_ai_bot.application.modules.control_plane import ControlPlaneBundle
from qq_ai_bot.config import Settings
from qq_ai_bot.control_plane.command_types import ControlCommandError
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import (
    ControlQueryError,
    DownloadView,
)
from qq_ai_bot.control_plane.surface import execute_command, execute_query
from qq_ai_bot.control_plane.wire import control_response, decode_command
from qq_ai_bot.domain.control import DecisionContext, YukiControlTarget
from qq_ai_bot.domain.identity import (
    ConversationId,
    PersonId,
    PresenceId,
    RequestId,
    SpaceBindingId,
    SpaceId,
)
from qq_ai_bot.webui.sessions import BrowserSessions

_STATUS = {
    ProblemCode.UNAUTHENTICATED: 401,
    ProblemCode.CAPABILITY_DENIED: 403,
    ProblemCode.NOT_FOUND: 404,
    ProblemCode.VERSION_CONFLICT: 409,
    ProblemCode.IDEMPOTENCY_CONFLICT: 409,
    ProblemCode.STATE_MISMATCH: 409,
    ProblemCode.OPERATION_UNAVAILABLE: 503,
}


def attach_webui(
    app: FastAPI, settings: Settings, control: Callable[[], ControlPlaneBundle]
) -> None:
    if not settings.webui_enabled:
        return
    assets = Path(__file__).with_name("assets")
    if not (assets / "index.html").is_file():
        raise RuntimeError("WebUI assets missing; run npm ci and npm run build in frontend")
    sessions: BrowserSessions | None = None
    secure = settings.webui_origin.startswith("https:")
    cookie = "__Host-yuki_control" if secure else "yuki_control"
    router = APIRouter(prefix="/api/control")

    def store() -> BrowserSessions:
        nonlocal sessions
        if sessions is None:
            sessions = BrowserSessions(control().access, lifetime=settings.webui_session_seconds)
        return sessions

    async def body(request: Request, *, max_bytes: int | None = None) -> dict[str, Any]:
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise ValueError("JSON required")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > (max_bytes or settings.webui_max_body_bytes):
                raise ValueError("request too large")
            data.extend(chunk)
        parsed = json.loads(data)
        if type(parsed) is not dict:
            raise ValueError("object required")
        return parsed

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next: Any) -> Any:
        path = request.url.path
        if not (path == "/ui" or path.startswith("/ui/") or path.startswith("/api/control/")):
            return await call_next(request)
        request_id = RequestId.new()
        try:
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin is not None and origin != settings.webui_origin
            ):
                raise ControlQueryError(Problem(ProblemCode.CAPABILITY_DENIED))
            if path.startswith("/api/control/"):
                if request.method not in {"GET", "HEAD"} and origin != settings.webui_origin:
                    raise ControlQueryError(Problem(ProblemCode.CAPABILITY_DENIED))
                if path != "/api/control/login":
                    session, principal = await store().resolve(request.cookies.get(cookie))
                    if request.method not in {"GET", "HEAD"} and not hmac.compare_digest(
                        request.headers.get("x-yuki-csrf", ""), session.csrf
                    ):
                        raise ControlQueryError(Problem(ProblemCode.CAPABILITY_DENIED))
                    request.state.principal = principal
                    request.state.csrf = session.csrf
                try:
                    request_id = RequestId.parse(request.headers["x-request-id"])
                except KeyError:
                    pass
            request.state.request_id = request_id
            response = await call_next(request)
        except (ControlQueryError, ControlCommandError) as exc:
            response = JSONResponse(
                control_response(request_id, exc.problem),
                status_code=_STATUS.get(exc.problem.code, 400),
            )
        except (KeyError, TypeError, ValueError, RecursionError):
            response = JSONResponse(
                control_response(request_id, Problem(ProblemCode.VALIDATION_ERROR)), status_code=400
            )
        avatar_cache = (
            path.startswith("/api/control/files/avatar/")
            and response.status_code == 200
            and response.headers.get("Cache-Control", "").startswith("private,")
        )
        response.headers.update(
            {
                "Cache-Control": (
                    response.headers["Cache-Control"] if avatar_cache else "no-store"
                ),
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; "
                    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                ),
            }
        )
        if avatar_cache:
            response.headers["Vary"] = "Cookie"
        return response

    def context(
        request: Request,
        request_id: RequestId | None = None,
        target: object = YukiControlTarget.PERMANENT_YUKI,
    ) -> DecisionContext[Any, Any, Any]:
        principal = request.state.principal
        return DecisionContext(
            request_id or request.state.request_id,
            principal,
            principal.source,
            target,
        )

    @router.post("/login")
    async def login(request: Request) -> JSONResponse:
        data = await body(request)
        if set(data) != {"credential"}:
            raise ValueError("invalid login")
        token, _session = await store().login(
            data["credential"], request.client.host if request.client else "local"
        )
        response = JSONResponse({"authenticated": True})
        response.set_cookie(
            cookie,
            token,
            httponly=True,
            secure=secure,
            samesite="strict",
            path="/",
            max_age=settings.webui_session_seconds,
        )
        return response

    @router.get("/session")
    async def session(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "csrf": request.state.csrf,
                "content_access": {
                    "chat": request.state.principal.allows("control.chat.content.read")
                },
                "surface": control_response(
                    request.state.request_id, control().queries.describe(context(request))
                )["data"],
            }
        )

    @router.post("/logout")
    async def logout(request: Request) -> JSONResponse:
        store().revoke(request.cookies.get(cookie))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(cookie, path="/", secure=secure, httponly=True, samesite="strict")
        return response

    @router.post("/queries/{method}")
    async def query(method: str, request: Request) -> JSONResponse:
        data = await body(request)
        ctx = context(request)
        result = await execute_query(control().queries, ctx, method, data)
        return JSONResponse(control_response(ctx.request_id, result))

    @router.post("/commands/{method}")
    async def command(method: str, request: Request) -> JSONResponse:
        from qq_ai_bot.sandbox.client import MAX_CONTROL_UPLOAD_WIRE

        data = await body(
            request,
            max_bytes=max(settings.webui_max_body_bytes, MAX_CONTROL_UPLOAD_WIRE)
            if method == "mutate_environment_file"
            else None,
        )
        raw_target = data.pop("target", {"kind": "yuki"})
        if type(raw_target) is not dict:
            raise ValueError("invalid target")
        kind = raw_target.get("kind")
        target: object = YukiControlTarget.PERMANENT_YUKI
        if kind == "yuki":
            if set(raw_target) != {"kind"}:
                raise ValueError("invalid target")
        else:
            if set(raw_target) != {"kind", "id"}:
                raise ValueError("invalid target")
            types = {
                "person": PersonId,
                "space": SpaceId,
                "presence": PresenceId,
                "space_binding": SpaceBindingId,
            }
            if kind not in types:
                raise ValueError("invalid target kind")
            target = types[kind].parse(raw_target["id"])
        command = decode_command(data)
        if command.request_id != request.state.request_id:
            raise ValueError("request ID must match header")
        ctx = context(request, command.request_id, target)
        result = await execute_command(control().commands, ctx, method, command)
        return JSONResponse(control_response(ctx.request_id, result))

    def file_response(download: DownloadView) -> Response:
        disposition = "inline" if download.media_type.startswith("image/") else "attachment"
        filename = quote(download.name, safe="")
        return Response(
            download.content,
            media_type=download.media_type,
            headers={
                "Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}",
            },
        )

    @router.get("/files/workspace/{artifact_id}")
    async def workspace_file(artifact_id: str, request: Request) -> Response:
        return file_response(
            await control().queries.download_workspace(context(request), artifact_id)
        )

    @router.get("/files/emoji/{asset_id}")
    async def emoji_file(asset_id: str, request: Request) -> Response:
        return file_response(await control().queries.download_emoji(context(request), asset_id))

    @router.get("/files/avatar/{kind}/{owner_id}")
    async def avatar_file(kind: str, owner_id: str, request: Request) -> Response:
        result = file_response(
            await control().queries.download_avatar(context(request), kind, owner_id)
        )
        result.headers["Cache-Control"] = "private, max-age=3600"
        return result

    @router.get("/files/environment")
    async def environment_file(path: str, request: Request) -> Response:
        return file_response(
            await control().queries.download_environment_file(context(request), path)
        )

    @router.get("/files/chat/{conversation_id}/{event_id}/{attachment_index}")
    async def chat_file(
        conversation_id: str, event_id: int, attachment_index: int, request: Request
    ) -> Response:
        return file_response(
            await control().queries.download_chat_media(
                context(request),
                ConversationId.parse(conversation_id),
                event_id,
                attachment_index,
            )
        )

    app.include_router(router)
    app.mount("/ui", StaticFiles(directory=assets, html=True), name="webui")
