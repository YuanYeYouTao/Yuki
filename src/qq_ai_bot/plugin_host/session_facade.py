"""Plugin SDK adapter for a Host-bound independent Agent session authority."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from qq_ai_bot.plugin_host.session_repository import PluginAgentSessionRecord
from qq_ai_bot.services.plugin_sessions import (
    PluginAgentSessionService,
    PluginSessionAuthority,
)
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.sessions import (
    AgentSession,
    AgentSessionRunResult,
    CreateAgentSessionRequest,
    RunAgentSessionRequest,
    SessionContextProfile,
    SessionPersistence,
    SessionStatus,
)


class BoundAgentSessionFacade:
    """Expose sessions with plugin, actor, group, and approvals fixed by the Host."""

    def __init__(
        self,
        *,
        service: PluginAgentSessionService,
        plugin_id: str,
        actor_user_id: str,
        current_group_id: str | None,
        approved_permissions: Iterable[PluginPermission | str],
        conversation_id: str | None = None,
    ) -> None:
        self._service = service
        self._authority = PluginSessionAuthority(
            plugin_id=plugin_id,
            actor_user_id=actor_user_id,
            current_group_id=current_group_id,
            approved_permissions=frozenset(
                permission.value if isinstance(permission, PluginPermission) else str(permission)
                for permission in approved_permissions
            ),
            conversation_id=conversation_id,
        )

    async def create(self, request: CreateAgentSessionRequest) -> AgentSession:
        record = await self._service.create(
            self._authority,
            name=request.name,
            instructions=request.instructions,
            persistence=request.persistence.value,
            context_profile=request.context_profile.value,
            allowed_capabilities=request.allowed_capabilities,
        )
        return _sdk_session(record)

    async def run(self, request: RunAgentSessionRequest) -> AgentSessionRunResult:
        result = await self._service.run(
            self._authority,
            session_id=str(request.session_id),
            user_input=request.user_input,
            allowed_capabilities=request.allowed_capabilities,
            max_tool_calls=request.max_tool_calls,
            max_model_requests=request.max_model_requests,
        )
        return AgentSessionRunResult(
            session=_sdk_session(result.session),
            text=result.text,
            tool_calls_used=result.tool_calls_used,
            model_requests=result.model_requests,
        )

    async def reset(self, session_id: UUID) -> AgentSession:
        return _sdk_session(await self._service.reset(self._authority, session_id=str(session_id)))

    async def close(self, session_id: UUID) -> AgentSession:
        return _sdk_session(await self._service.close(self._authority, session_id=str(session_id)))


def _sdk_session(record: PluginAgentSessionRecord) -> AgentSession:
    return AgentSession(
        session_id=UUID(record.session_id),
        name=record.name,
        status=SessionStatus(record.status),
        persistence=SessionPersistence(record.persistence),
        context_profile=SessionContextProfile(record.context_profile),
        created_at=record.created_at,
        updated_at=record.updated_at,
        turn_count=record.turn_count,
    )


__all__ = ["BoundAgentSessionFacade"]
