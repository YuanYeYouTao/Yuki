"""Finite reviewed HTTP routes; original services own all authorization and effects."""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.staticfiles import StaticFiles

from qq_ai_bot.application.modules.control_plane import ControlPlaneBundle
from qq_ai_bot.config import Settings
from qq_ai_bot.control_plane.command_types import ControlCommandError
from qq_ai_bot.control_plane.json_types import freeze_json_object
from qq_ai_bot.control_plane.operations import OperationKind
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import (
    ChatHistoryFilter,
    ConfigQueryScope,
    ControlQueryError,
    DownloadView,
    ExecutionTraceFilter,
    MemoryQueryFilter,
    ReflectionQueryFilter,
)
from qq_ai_bot.control_plane.surface import _METHODS
from qq_ai_bot.control_plane.wire import control_response, decode_command, decode_page
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

_SIMPLE_QUERIES = frozenset(name for kind, name, _ in _METHODS if kind == "query") - {
    "list_memory_facts",
    "list_memory_evidence",
    "read_memory_fact",
    "list_memory_rebuild_proposals",
    "read_memory_maintenance_run",
    "read_relationship",
    "list_relationship_history",
    "list_self_reflection_history",
    "read_work",
    "list_work_history",
    "read_config_file",
    "read_model_usage_summary",
    "read_execution_trace",
    "list_execution_trace",
    "list_chat_events",
    "read_conversation_execution",
    "list_event_turns",
    "list_social_receipts",
    "list_effective_configs",
    "list_operations",
    "read_operation",
    "read_plugin_approval",
    "read_plugin_runtime",
    "read_plugin_configuration",
    "read_plugin_observation",
    "list_plugin_outbox",
    "list_plugin_background_turns",
    "list_participation_feedback",
    "read_automation",
    "list_automation_runs",
    "list_automation_steps",
    "read_terminal_submission",
    "read_environment",
    "read_workspace",
    "list_work",
    "download_workspace",
    "download_chat_media",
    "download_emoji",
    "download_environment_file",
    "read_display_names",
    "list_participation_runs",
}
_COMMANDS = frozenset(name for kind, name, _ in _METHODS if kind == "command")
_STATUS = {
    ProblemCode.UNAUTHENTICATED: 401,
    ProblemCode.CAPABILITY_DENIED: 403,
    ProblemCode.NOT_FOUND: 404,
    ProblemCode.VERSION_CONFLICT: 409,
    ProblemCode.IDEMPOTENCY_CONFLICT: 409,
    ProblemCode.STATE_MISMATCH: 409,
    ProblemCode.OPERATION_UNAVAILABLE: 503,
}


def _history(raw: Any) -> ChatHistoryFilter:
    if type(raw) is not dict or set(raw) - {
        "descending",
        "event_id",
        "through_event_id",
        "since",
        "until",
    }:
        raise ValueError("invalid chat history filter")
    return ChatHistoryFilter(
        descending=raw.get("descending", False),
        event_id=raw.get("event_id"),
        through_event_id=raw.get("through_event_id"),
        since=datetime.fromisoformat(raw["since"]) if raw.get("since") else None,
        until=datetime.fromisoformat(raw["until"]) if raw.get("until") else None,
    )


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
        page = decode_page(data.get("page", {}))
        ctx = context(request)
        queries = control().queries
        result: object
        if method == "read_display_names":
            if set(data) != {"references"}:
                raise ValueError("read_display_names requires references only")
            result = await queries.read_display_names(
                ctx, freeze_json_object(data.get("references", {}))
            )
        elif method in _SIMPLE_QUERIES:
            if set(data) - {"page"}:
                raise ValueError("unknown query fields")
            result = (
                await getattr(queries, method)(ctx, page)
                if method.startswith("list_")
                else await getattr(queries, method)(ctx)
            )
        elif method in {"list_chat_events", "list_social_receipts"}:
            allowed = {"page", "conversation_id"}
            if method == "list_chat_events":
                allowed |= {"include_content", "history"}
            if set(data) - allowed:
                raise ValueError("unknown query fields")
            conversation = ConversationId.parse(data["conversation_id"])
            result = (
                await queries.list_chat_events(
                    ctx,
                    page,
                    conversation_id=conversation,
                    include_content=data.get("include_content", False),
                    history=_history(data.get("history", {})),
                )
                if method == "list_chat_events"
                else await queries.list_social_receipts(ctx, page, conversation_id=conversation)
            )
        elif method == "read_conversation_execution":
            if set(data) - {"conversation_id", "include_content"} or "conversation_id" not in data:
                raise ValueError("invalid conversation execution query")
            result = await queries.read_conversation_execution(
                ctx,
                ConversationId.parse(data["conversation_id"]),
                include_content=data.get("include_content", False),
            )
        elif method == "read_model_usage_summary":
            if (
                set(data) != {"window"}
                or type(data["window"]) is not str
                or data["window"] not in {"24h", "7d", "30d"}
            ):
                raise ValueError("invalid model usage window")
            result = await queries.read_model_usage_summary(ctx, data["window"])
        elif method == "list_event_turns":
            if set(data) - {"conversation_id", "event_id", "direction", "page"} or not {
                "conversation_id",
                "event_id",
                "direction",
            } <= set(data):
                raise ValueError("invalid event turn query")
            result = await queries.list_event_turns(
                ctx,
                page,
                conversation_id=ConversationId.parse(data["conversation_id"]),
                event_id=data["event_id"],
                direction=data["direction"],
            )
        elif method == "list_execution_trace":
            if set(data) - {"page", "scope", "include_content"}:
                raise ValueError("unknown query fields")
            scope = dict(data.get("scope", {}))
            if scope.get("conversation_id") is not None:
                scope["conversation_id"] = ConversationId.parse(scope["conversation_id"])
            result = await queries.list_execution_trace(
                ctx,
                page,
                scope=ExecutionTraceFilter(**scope),
                include_content=data.get("include_content", False),
            )
        elif method == "list_memory_rebuild_proposals":
            if set(data) - {"page", "run_id", "include_content"} or "run_id" not in data:
                raise ValueError("invalid rebuild history")
            result = await queries.list_memory_rebuild_proposals(
                ctx, page, run_id=data["run_id"], include_content=data.get("include_content", False)
            )
        elif method == "read_terminal_submission":
            if set(data) != {"request_id"}:
                raise ValueError("invalid terminal request")
            result = await queries.read_terminal_submission(
                ctx, RequestId.parse(data["request_id"])
            )
        elif method == "read_environment":
            if set(data) != {"section", "arguments"}:
                raise ValueError("invalid environment query")
            result = await queries.read_environment(
                ctx, data["section"], freeze_json_object(data["arguments"])
            )
        elif method == "read_execution_trace":
            if set(data) != {"entry_id"}:
                raise ValueError("invalid trace lookup")
            result = await queries.read_execution_trace(ctx, data["entry_id"])
        elif method == "list_self_reflection_history":
            if set(data) - {"page", "section", "scope"} or "section" not in data:
                raise ValueError("invalid reflection history")
            if type(data.get("scope", {})) is not dict:
                raise ValueError("scope must be an object")
            scope = dict(data.get("scope", {}))
            for key, cls in (("person_id", PersonId), ("space_id", SpaceId)):
                if scope.get(key) is not None:
                    scope[key] = cls.parse(scope[key])
            result = await queries.list_self_reflection_history(
                ctx, page, section=data["section"], scope=ReflectionQueryFilter(**scope)
            )
        elif method == "read_relationship":
            if set(data) != {"person_id"}:
                raise ValueError("invalid relationship lookup")
            result = await queries.read_relationship(ctx, PersonId.parse(data["person_id"]))
        elif method == "list_relationship_history":
            if set(data) - {"page", "person_id", "section"} or not {"person_id", "section"} <= set(
                data
            ):
                raise ValueError("invalid relationship history")
            result = await queries.list_relationship_history(
                ctx, page, person_id=PersonId.parse(data["person_id"]), section=data["section"]
            )
        elif method in {"list_memory_facts", "list_memory_evidence"}:
            if set(data) - {"page", "scope"}:
                raise ValueError("invalid memory scope")
            if type(data.get("scope", {})) is not dict:
                raise ValueError("scope must be an object")
            scope = dict(data.get("scope", {}))
            for key, cls in (
                ("person_id", PersonId),
                ("space_id", SpaceId),
                ("visibility_person_id", PersonId),
                ("visibility_space_id", SpaceId),
            ):
                if scope.get(key) is not None:
                    scope[key] = cls.parse(scope[key])
            result = await getattr(queries, method)(ctx, page, scope=MemoryQueryFilter(**scope))
        elif method == "read_memory_fact":
            if set(data) != {"fact_id"}:
                raise ValueError("invalid memory lookup")
            result = await queries.read_memory_fact(ctx, data["fact_id"])
        elif method == "list_effective_configs":
            raw = data.get("scope", {})
            if set(data) - {"page", "scope"} or set(raw) - {"person_id", "space_id"}:
                raise ValueError("invalid config scope")
            result = await queries.list_effective_configs(
                ctx,
                page,
                scope=ConfigQueryScope(
                    person_id=PersonId.parse(raw["person_id"]) if raw.get("person_id") else None,
                    space_id=SpaceId.parse(raw["space_id"]) if raw.get("space_id") else None,
                ),
            )
        elif method == "list_operations":
            if set(data) - {"page", "kind"}:
                raise ValueError("invalid operation scope")
            result = await queries.list_operations(
                ctx, page, kind=OperationKind(data.get("kind", "control"))
            )
        elif method == "list_participation_runs":
            if set(data) - {"page", "conversation_id"}:
                raise ValueError("invalid participation scope")
            result = await queries.list_participation_runs(
                ctx,
                page,
                conversation_id=ConversationId.parse(data["conversation_id"])
                if data.get("conversation_id")
                else None,
            )
        elif method == "read_work":
            if set(data) - {"work_id", "include_content"} or "work_id" not in data:
                raise ValueError("invalid work lookup")
            result = await queries.read_work(
                ctx, data["work_id"], include_content=data.get("include_content", False)
            )
        elif method == "list_work":
            if set(data) - {"page", "include_content"}:
                raise ValueError("invalid work query")
            result = await queries.list_work(
                ctx, page, include_content=data.get("include_content", False)
            )
        elif method == "list_work_history":
            if set(data) - {"page", "work_id", "section", "include_content"} or not {
                "work_id",
                "section",
            } <= set(data):
                raise ValueError("invalid work history")
            result = await queries.list_work_history(
                ctx,
                page,
                work_id=data["work_id"],
                section=data["section"],
                include_content=data.get("include_content", False),
            )
        elif method == "read_config_file":
            if set(data) != {"file_id"}:
                raise ValueError("invalid configuration file lookup")
            result = await queries.read_config_file(ctx, data["file_id"])
        elif method in {"read_automation", "read_workspace"}:
            key = "automation_id" if method == "read_automation" else "artifact_id"
            if set(data) != {key}:
                raise ValueError("invalid activity lookup")
            result = await getattr(queries, method)(ctx, data[key])
        elif method in {"list_automation_runs", "list_automation_steps"}:
            keys = {"page", "automation_id"} | (
                {"run_id"} if method == "list_automation_steps" else set()
            )
            if set(data) - keys or "automation_id" not in data:
                raise ValueError("invalid automation history")
            args = {"automation_id": data["automation_id"]}
            if method == "list_automation_steps":
                args["run_id"] = data.get("run_id")
            result = await getattr(queries, method)(ctx, page, **args)
        elif method == "list_participation_feedback":
            if set(data) - {"run_id", "page", "include_content"} or "run_id" not in data:
                raise ValueError("invalid participation feedback scope")
            result = await queries.list_participation_feedback(
                ctx, page, run_id=data["run_id"], include_content=data.get("include_content", False)
            )
        elif method in {"list_plugin_outbox", "list_plugin_background_turns"}:
            if set(data) - {"plugin_id", "page"} or "plugin_id" not in data:
                raise ValueError("invalid plugin outbox scope")
            result = await getattr(queries, method)(ctx, page, plugin_id=data["plugin_id"])
        elif method == "read_plugin_observation":
            if set(data) - {"plugin_id", "cursor", "limit"} or "plugin_id" not in data:
                raise ValueError("invalid plugin observation lookup")
            result = await queries.read_plugin_observation(
                ctx, data["plugin_id"], cursor=data.get("cursor"), limit=data.get("limit", 10)
            )
        elif method == "read_plugin_configuration":
            if set(data) - {"plugin_id", "scope_type", "owner_id"} or "plugin_id" not in data:
                raise ValueError("invalid plugin configuration lookup")
            result = await queries.read_plugin_configuration(
                ctx,
                data["plugin_id"],
                scope_type=data.get("scope_type", "global"),
                owner_id=data.get("owner_id"),
            )
        elif method == "read_memory_maintenance_run":
            if set(data) != {"operation_id"}:
                raise ValueError("invalid maintenance lookup")
            result = await queries.read_memory_maintenance_run(ctx, data["operation_id"])
        elif method in {"read_operation", "read_plugin_runtime", "read_plugin_approval"}:
            if method == "read_operation" and set(data) == {"request_id"}:
                original = RequestId.parse(data["request_id"])
                principal_id = request.state.principal.principal_id.text
                data = {"operation_id": f"control:{principal_id}:{original.text}"}
            key = "operation_id" if method == "read_operation" else "plugin_id"
            if set(data) != {key}:
                raise ValueError("invalid lookup")
            result = await getattr(queries, method)(ctx, data[key])
        else:
            raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
        return JSONResponse(control_response(ctx.request_id, result))

    @router.post("/commands/{method}")
    async def command(method: str, request: Request) -> JSONResponse:
        if method not in _COMMANDS:
            raise ControlQueryError(Problem(ProblemCode.NOT_FOUND))
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
        result = await getattr(control().commands, method)(ctx, command)
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
