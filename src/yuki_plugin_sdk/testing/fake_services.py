"""Network-free Facade fakes for plugin unit and contract tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from yuki_plugin_sdk.events import EventEnvelope
from yuki_plugin_sdk.models import (
    BackgroundTargetGrantView,
    CurrentMessage,
    GeneratedSpeechHandle,
    JsonValue,
    MediaArtifactHandle,
    NotificationPublishReceipt,
    NotificationTarget,
    PublishNotificationRequest,
)
from yuki_plugin_sdk.results import PluginResult
from yuki_plugin_sdk.sessions import (
    AgentSession,
    AgentSessionRunResult,
    CreateAgentSessionRequest,
    RunAgentSessionRequest,
    SessionStatus,
)


class FakeMessageFacade:
    def __init__(self, current: CurrentMessage | None = None) -> None:
        self.current = current
        self.reply: CurrentMessage | None = None
        self.recent: list[CurrentMessage] = []
        self.sent: list[str] = []
        self.routed: list[tuple[str, str, str]] = []
        self.images: list[tuple[str, str, str]] = []

    async def get_current(self) -> CurrentMessage | None:
        return self.current

    async def get_reply(self) -> CurrentMessage | None:
        return self.reply

    async def get_recent(self, limit: int = 20) -> tuple[CurrentMessage, ...]:
        return tuple(self.recent[-limit:])

    async def search_history(self, query: str, limit: int = 20) -> tuple[CurrentMessage, ...]:
        needle = query.casefold()
        matches = (message for message in self.recent if needle in message.text.casefold())
        return tuple(matches)[-limit:]

    async def send_text(self, text: str) -> PluginResult:
        self.sent.append(text)
        return PluginResult(data={"sent": True, "characters": len(text)})

    async def send_private(self, user_id: str, text: str) -> PluginResult:
        self.routed.append(("private", user_id, text))
        return PluginResult(data={"sent": True})

    async def send_group(self, group_id: str, text: str) -> PluginResult:
        self.routed.append(("group", group_id, text))
        return PluginResult(data={"sent": True})

    async def send_image(
        self, *, target_type: str, target_id: str, media_reference: str
    ) -> PluginResult:
        self.images.append((target_type, target_id, media_reference))
        return PluginResult(data={"sent": True})


class FakePeopleFacade:
    def __init__(self) -> None:
        self.current_user_id: str | None = None
        self.people: dict[str, dict[str, JsonValue]] = {}
        self.aliases: dict[str, tuple[str, ...]] = {}

    async def get_current(self) -> Mapping[str, JsonValue] | None:
        return self.people.get(self.current_user_id or "")

    async def get(self, user_id: str) -> Mapping[str, JsonValue] | None:
        return self.people.get(user_id)

    async def list_aliases(self, user_id: str) -> tuple[str, ...]:
        return self.aliases.get(user_id, ())

    async def add_alias(self, user_id: str, alias: str) -> PluginResult:
        current = list(self.aliases.get(user_id, ()))
        if alias not in current:
            current.append(alias)
        self.aliases[user_id] = tuple(current)
        return PluginResult(data={"updated": True})


class FakeGroupFacade:
    def __init__(self) -> None:
        self.current_group_id: str | None = None
        self.groups: dict[str, dict[str, JsonValue]] = {}
        self.members: dict[str, list[dict[str, JsonValue]]] = {}
        self.settings: dict[str, dict[str, JsonValue]] = {}

    async def get_current(self) -> Mapping[str, JsonValue] | None:
        return self.groups.get(self.current_group_id or "")

    async def get(self, group_id: str) -> Mapping[str, JsonValue] | None:
        return self.groups.get(group_id)

    async def list_members(
        self, group_id: str, limit: int = 100
    ) -> tuple[Mapping[str, JsonValue], ...]:
        return tuple(self.members.get(group_id, ())[:limit])

    async def get_settings(self, group_id: str) -> Mapping[str, JsonValue]:
        return self.settings.get(group_id, {})

    async def set_setting(self, group_id: str, key: str, value: JsonValue) -> PluginResult:
        self.settings.setdefault(group_id, {})[key] = value
        return PluginResult(data={"updated": True})


class FakeMemoryFacade:
    def __init__(self) -> None:
        self.people: dict[str, list[dict[str, JsonValue]]] = {}
        self.groups: dict[str, list[dict[str, JsonValue]]] = {}
        self.records: dict[str, dict[str, JsonValue]] = {}

    async def list_person(
        self, user_id: str, limit: int = 20
    ) -> tuple[Mapping[str, JsonValue], ...]:
        return tuple(self.people.get(user_id, ())[-limit:])

    async def list_group(
        self, group_id: str, limit: int = 20
    ) -> tuple[Mapping[str, JsonValue], ...]:
        return tuple(self.groups.get(group_id, ())[-limit:])

    async def search(
        self,
        query: str,
        *,
        scope_type: str,
        subject_id: str,
        limit: int = 20,
    ) -> tuple[Mapping[str, JsonValue], ...]:
        source = self.people if scope_type == "person" else self.groups
        needle = query.casefold()
        return tuple(
            item
            for item in source.get(subject_id, ())
            if needle in str(item.get("content", "")).casefold()
        )[:limit]

    async def add(
        self,
        *,
        scope_type: str,
        subject_id: str,
        content: str,
        source_type: str,
        confidence: float,
        source_event_ids: tuple[str, ...] = (),
    ) -> PluginResult:
        memory_id = str(uuid4())
        record: dict[str, JsonValue] = {
            "id": memory_id,
            "scope_type": scope_type,
            "subject_id": subject_id,
            "content": content,
            "source_type": source_type,
            "confidence": confidence,
            "source_event_ids": list(source_event_ids),
        }
        self.records[memory_id] = record
        target = self.people if scope_type == "person" else self.groups
        target.setdefault(subject_id, []).append(record)
        return PluginResult(data={"memory_id": memory_id})

    async def update(
        self,
        memory_id: str,
        *,
        content: str,
        confidence: float | None = None,
    ) -> PluginResult:
        record = self.records.get(memory_id)
        if record is None:
            return PluginResult(ok=False, error_code="memory.not_found")
        record["content"] = content
        if confidence is not None:
            record["confidence"] = confidence
        return PluginResult(data={"updated": True})

    async def delete(self, memory_id: str) -> PluginResult:
        record = self.records.pop(memory_id, None)
        if record is None:
            return PluginResult(ok=False, error_code="memory.not_found")
        for collection in (*self.people.values(), *self.groups.values()):
            collection[:] = [item for item in collection if item.get("id") != memory_id]
        return PluginResult(data={"deleted": True})


class FakeRelationshipFacade:
    def __init__(self) -> None:
        self.current_user_id: str | None = None
        self.relationships: dict[str, dict[str, JsonValue]] = {}
        self.events: dict[str, list[dict[str, JsonValue]]] = {}

    async def get_current(self) -> Mapping[str, JsonValue] | None:
        return self.relationships.get(self.current_user_id or "")

    async def get(self, user_id: str) -> Mapping[str, JsonValue] | None:
        return self.relationships.get(user_id)

    async def list_events(
        self, user_id: str, limit: int = 20
    ) -> tuple[Mapping[str, JsonValue], ...]:
        return tuple(self.events.get(user_id, ())[-limit:])

    async def adjust(
        self,
        user_id: str,
        *,
        affection_delta: int = 0,
        trust_delta: int = 0,
        reason: str,
    ) -> PluginResult:
        current = self.relationships.setdefault(user_id, {"affection": 0, "trust": 0})
        affection = current.get("affection", 0)
        trust = current.get("trust", 0)
        current["affection"] = (
            int(affection) if isinstance(affection, int | float) else 0
        ) + affection_delta
        current["trust"] = (int(trust) if isinstance(trust, int | float) else 0) + trust_delta
        self.events.setdefault(user_id, []).append(
            {
                "affection_delta": affection_delta,
                "trust_delta": trust_delta,
                "reason": reason,
            }
        )
        return PluginResult(data={"updated": True})


class FakeLLMFacade:
    def __init__(self, response: str = "fake response") -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    async def generate(self, instruction: str, *, max_characters: int = 2_000) -> str:
        self.calls.append(("none", instruction))
        return self.response[:max_characters]

    async def generate_with_context(
        self,
        instruction: str,
        *,
        context_profile: str,
        max_characters: int = 2_000,
    ) -> str:
        self.calls.append((context_profile, instruction))
        return self.response[:max_characters]


class FakeAgentFacade:
    def __init__(self, response: str = "fake agent response") -> None:
        self.response = response
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def result(self, work_id: str) -> PluginResult:
        return PluginResult(ok=False, error_code="work_not_found_or_archived")

    async def resume(self, work_id: str, text: str, *, request_id: str) -> PluginResult:
        return await self.result(work_id)

    async def run(
        self,
        instruction: str,
        *,
        allowed_capabilities: tuple[str, ...] = (),
        max_tool_calls: int | None = None,
        max_model_requests: int | None = None,
    ) -> PluginResult:
        self.calls.append((instruction, allowed_capabilities))
        return PluginResult(
            data={
                "text": self.response,
                "max_tool_calls": max_tool_calls,
                "max_model_requests": max_model_requests,
            }
        )


class FakeEmojiFacade:
    def __init__(self) -> None:
        self.assets: dict[str, dict[str, JsonValue]] = {}
        self.queued: list[dict[str, str]] = []

    async def list(
        self, status: str | None = None, limit: int = 30
    ) -> tuple[Mapping[str, JsonValue], ...]:
        rows = tuple(self.assets.values())
        if status is not None:
            rows = tuple(row for row in rows if row.get("status") == status)
        return rows[:limit]

    async def get(self, emoji_id: str) -> Mapping[str, JsonValue] | None:
        return self.assets.get(emoji_id)

    async def search(self, query: str, limit: int = 20) -> tuple[Mapping[str, JsonValue], ...]:
        needle = query.casefold()
        return tuple(
            row
            for row in self.assets.values()
            if needle in str(row.get("description", "")).casefold()
        )[:limit]

    async def collect_current(self) -> PluginResult:
        return PluginResult(data={"collected": 0})

    async def select(
        self,
        *,
        goal: str,
        emotion: str = "",
        mode: str = "optional",
        placement: str = "after_text",
    ) -> PluginResult:
        first = next(iter(self.assets.values()), None)
        return PluginResult(data={"selected": first})

    async def queue_reply_effect(
        self,
        *,
        goal: str,
        emotion: str = "",
        mode: str = "optional",
        placement: str = "after_text",
    ) -> PluginResult:
        self.queued.append({"goal": goal, "emotion": emotion, "mode": mode, "placement": placement})
        return PluginResult(data={"queued": True})

    async def adopt(
        self, emoji_id: str, *, scope_type: str = "global", scope_id: str = ""
    ) -> PluginResult:
        return PluginResult(data={"emoji_id": emoji_id, "adopted": True})

    async def reject(self, emoji_id: str) -> PluginResult:
        return PluginResult(data={"emoji_id": emoji_id, "status": "rejected"})

    async def ban(self, emoji_id: str) -> PluginResult:
        return PluginResult(data={"emoji_id": emoji_id, "status": "banned"})


class FakeWebFacade:
    def __init__(self) -> None:
        self.searches: list[str] = []
        self.reads: list[tuple[str, str]] = []

    async def search(self, query: str) -> PluginResult:
        self.searches.append(query)
        return PluginResult(data={"query": query, "sources": []})

    async def read(self, url: str, question: str = "") -> PluginResult:
        self.reads.append((url, question))
        return PluginResult(data={"url": url, "text": ""})


class FakeHttpFacade:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        auth_secret: str | None = None,
    ) -> PluginResult:
        self.requests.append((method, url))
        return PluginResult(data={"status_code": 200, "body": ""})


class FakeVisionFacade:
    def __init__(self) -> None:
        self.observation: dict[str, JsonValue] | None = None

    async def get_current_observation(self) -> Mapping[str, JsonValue] | None:
        return self.observation

    async def analyze_current_media(self, question: str = "") -> PluginResult:
        return PluginResult(data=self.observation or {})


class FakeMediaFacade:
    def __init__(self) -> None:
        self.current: list[dict[str, JsonValue]] = []

    async def get_current(self) -> tuple[Mapping[str, JsonValue], ...]:
        return tuple(self.current)

    async def create_artifact(
        self,
        *,
        data: bytes,
        content_type: str,
        filename: str,
        ttl_seconds: int = 86_400,
    ) -> MediaArtifactHandle:
        import hashlib

        return MediaArtifactHandle(
            handle_id=uuid4().hex,
            content_type=content_type,
            filename=filename,
            byte_size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            expires_at=datetime.now(UTC),
        )


class FakeNotificationFacade:
    def __init__(self) -> None:
        self.published: list[PublishNotificationRequest] = []
        self.grants: dict[tuple[str, str], BackgroundTargetGrantView] = {}

    async def publish(self, request: PublishNotificationRequest) -> NotificationPublishReceipt:
        self.published.append(request)
        return NotificationPublishReceipt(
            notification_id=uuid4().hex,
            source_event_id=len(self.published),
            event_created=True,
            delivery_enqueued=bool(request.text or request.media_handles),
            agent_turn_enqueued=request.ask_agent,
            deduplicated=False,
        )

    async def grant_target(
        self,
        target: NotificationTarget,
        *,
        bot_user_id: str,
    ) -> BackgroundTargetGrantView:
        view = BackgroundTargetGrantView(
            target_type=target.target_type,
            target_id=target.target_id,
            bot_user_id=bot_user_id or "test-bot",
            enabled=True,
            created_by_user_id="test-admin",
        )
        self.grants[(target.target_type, target.target_id)] = view
        return view

    async def revoke_target(self, target: NotificationTarget) -> bool:
        return self.grants.pop((target.target_type, target.target_id), None) is not None

    async def list_grants(self) -> tuple[BackgroundTargetGrantView, ...]:
        return tuple(self.grants.values())

    async def status(self) -> Mapping[str, int]:
        return {
            "grants": len(self.grants),
            "published": len(self.published),
        }


class FakeAutomationFacade:
    def __init__(self) -> None:
        self.tasks: list[dict[str, JsonValue]] = []

    async def list_current_owner(self) -> tuple[Mapping[str, JsonValue], ...]:
        return tuple(self.tasks)

    async def create_from_template(
        self, template: str, parameters: Mapping[str, JsonValue]
    ) -> PluginResult:
        task_id = str(uuid4())
        self.tasks.append(
            {
                "id": task_id,
                "template": template,
                "parameters": dict(parameters),
                "status": "active",
            }
        )
        return PluginResult(data={"task_id": task_id})

    async def pause(self, task_id: str) -> PluginResult:
        return self._set_status(task_id, "paused")

    async def resume(self, task_id: str) -> PluginResult:
        return self._set_status(task_id, "active")

    async def cancel(self, task_id: str) -> PluginResult:
        return self._set_status(task_id, "cancelled")

    def _set_status(self, task_id: str, status: str) -> PluginResult:
        for task in self.tasks:
            if task.get("id") == task_id:
                task["status"] = status
                return PluginResult(data={"updated": True})
        return PluginResult(ok=False, error_code="automation.not_found")


class FakeConfigFacade:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str, str], JsonValue] = {}

    async def get(self, key: str, *, scope_type: str = "global", scope_id: str = "") -> JsonValue:
        return self.values.get((scope_type, scope_id, key))

    async def set(
        self,
        key: str,
        value: JsonValue,
        *,
        scope_type: str = "global",
        scope_id: str = "",
    ) -> None:
        self.values[(scope_type, scope_id, key)] = value


class FakeSecretsFacade:
    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def configured(self, name: str) -> bool:
        return name in self._values

    def get(self, name: str) -> str:
        if name not in self._values:
            raise KeyError(name)
        return self._values[name]


class FakeStorage:
    def __init__(self) -> None:
        self._values: dict[tuple[str, str], JsonValue] = {}

    async def get(self, namespace: str, key: str) -> JsonValue:
        return self._values.get((namespace, key))

    async def set(self, namespace: str, key: str, value: JsonValue) -> None:
        self._values[(namespace, key)] = value

    async def delete(self, namespace: str, key: str) -> bool:
        return self._values.pop((namespace, key), None) is not None

    async def list(self, namespace: str) -> Mapping[str, JsonValue]:
        return {key: value for (owner, key), value in self._values.items() if owner == namespace}

    async def compare_and_set(
        self,
        namespace: str,
        key: str,
        expected: JsonValue,
        value: JsonValue,
    ) -> bool:
        current = self._values.get((namespace, key))
        if current != expected:
            return False
        self._values[(namespace, key)] = value
        return True


class FakeScheduler:
    def __init__(self) -> None:
        self.runners: dict[str, tuple[str, Callable[[], Awaitable[None]]]] = {}
        self._stopped = asyncio.Event()

    def create_task(self, name: str, runner: Callable[[], Awaitable[None]]) -> str:
        task_id = str(uuid4())
        self.runners[task_id] = (name, runner)
        return task_id

    async def cancel(self, task_id: str) -> bool:
        return self.runners.pop(task_id, None) is not None

    async def sleep_until_stopped(self) -> None:
        await self._stopped.wait()

    def stop(self) -> None:
        self._stopped.set()


class FakeOneBotFacade:
    def __init__(self) -> None:
        self.read_calls: list[str] = []
        self.mutating_calls: list[str] = []
        self.sent: list[tuple[str, str, str]] = []
        self.music_cards: list[tuple[str, str]] = []
        self.custom_music_cards: list[dict[str, str]] = []

    async def send_music_card(self, *, provider: str, resource_id: str) -> PluginResult:
        self.music_cards.append((provider, resource_id))
        return PluginResult(data={"sent": True})

    async def send_custom_music_card(
        self,
        *,
        url: str,
        image: str,
        title: str,
        singer: str = "",
        content: str = "",
    ) -> PluginResult:
        self.custom_music_cards.append(
            {
                "url": url,
                "image": image,
                "title": title,
                "singer": singer,
                "content": content,
            }
        )
        return PluginResult(data={"sent": True})

    async def send_private(self, user_id: str, text: str) -> PluginResult:
        self.sent.append(("private", user_id, text))
        return PluginResult(data={"sent": True})

    async def send_group(self, group_id: str, text: str) -> PluginResult:
        self.sent.append(("group", group_id, text))
        return PluginResult(data={"sent": True})

    async def call_read_action(self, action: str, params: Mapping[str, JsonValue]) -> PluginResult:
        self.read_calls.append(action)
        return PluginResult(data={"action": action, "ok": True})

    async def call_mutating_action(
        self, action: str, params: Mapping[str, JsonValue]
    ) -> PluginResult:
        self.mutating_calls.append(action)
        return PluginResult(data={"action": action, "ok": True})


class FakeClock:
    def __init__(self, current: datetime | None = None) -> None:
        self.current = current or datetime.now(UTC)

    def now(self) -> datetime:
        return self.current


class FakeEventBus:
    def __init__(self) -> None:
        self.events: list[EventEnvelope] = []

    async def publish(self, event: EventEnvelope) -> None:
        self.events.append(event)


class FakeSpeechFacade:
    def __init__(self) -> None:
        self.queued: list[tuple[str, str, str]] = []
        self.sent: list[tuple[str, str, str]] = []

    async def status(self) -> Mapping[str, JsonValue]:
        return {"enabled": True, "available": True}

    async def list_profiles(self) -> tuple[Mapping[str, JsonValue], ...]:
        return ()

    async def get_profile(self, profile_id: str) -> Mapping[str, JsonValue] | None:
        return None

    async def list_styles(self, profile_id: str) -> tuple[str, ...]:
        return ()

    async def synthesize(
        self,
        text: str,
        *,
        profile_id: str = "",
        style_hint: str = "",
    ) -> GeneratedSpeechHandle:
        return GeneratedSpeechHandle(
            handle_id=uuid4().hex,
            generation_id=1,
            profile_id=profile_id or "default",
            duration_milliseconds=0,
        )

    async def queue_reply_voice(
        self,
        *,
        profile_id: str = "",
        style_hint: str = "",
        mode: str = "optional",
    ) -> PluginResult:
        self.queued.append((profile_id, style_hint, mode))
        return PluginResult(data={"queued": True})

    async def send_private(self, user_id: str, handle: GeneratedSpeechHandle) -> PluginResult:
        self.sent.append(("private", user_id, handle.handle_id))
        return PluginResult(data={"sent": True})

    async def send_group(self, group_id: str, handle: GeneratedSpeechHandle) -> PluginResult:
        self.sent.append(("group", group_id, handle.handle_id))
        return PluginResult(data={"sent": True})


class FakeAgentSessionFacade:
    """Independent transcripts keyed by UUID; sessions never share history."""

    def __init__(self, responder: Callable[[str, tuple[str, ...]], str] | None = None) -> None:
        self._responder = responder or (lambda text, _history: f"session: {text}")
        self.sessions: dict[UUID, AgentSession] = {}
        self.history: dict[UUID, list[str]] = {}

    async def create(self, request: CreateAgentSessionRequest) -> AgentSession:
        now = datetime.now(UTC)
        session = AgentSession(
            session_id=uuid4(),
            name=request.name,
            status=SessionStatus.ACTIVE,
            persistence=request.persistence,
            context_profile=request.context_profile,
            created_at=now,
            updated_at=now,
        )
        self.sessions[session.session_id] = session
        self.history[session.session_id] = []
        return session

    async def run(self, request: RunAgentSessionRequest) -> AgentSessionRunResult:
        session = self._active(request.session_id)
        history = self.history[request.session_id]
        text = self._responder(request.user_input, tuple(history))
        history.extend((request.user_input, text))
        updated = session.model_copy(
            update={"updated_at": datetime.now(UTC), "turn_count": session.turn_count + 1}
        )
        self.sessions[request.session_id] = updated
        return AgentSessionRunResult(session=updated, text=text)

    async def reset(self, session_id: UUID) -> AgentSession:
        session = self._active(session_id)
        self.history[session_id] = []
        updated = session.model_copy(update={"updated_at": datetime.now(UTC), "turn_count": 0})
        self.sessions[session_id] = updated
        return updated

    async def close(self, session_id: UUID) -> AgentSession:
        session = self._active(session_id)
        closed = session.model_copy(
            update={"status": SessionStatus.CLOSED, "updated_at": datetime.now(UTC)}
        )
        self.sessions[session_id] = closed
        return closed

    def _active(self, session_id: UUID) -> AgentSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError("unknown plugin AI session")
        if session.status is SessionStatus.CLOSED:
            raise ValueError("plugin AI session is closed")
        return session


class FakeMCPFacade:
    calls: list[tuple[str, str, Mapping[str, JsonValue]]]

    def __init__(self) -> None:
        self.calls = []

    async def status(self) -> Mapping[str, JsonValue]:
        return {"enabled": False, "configured_servers": 0}

    async def list_servers(self) -> tuple[Mapping[str, JsonValue], ...]:
        return ()

    async def search_tools(self, query: str) -> tuple[Mapping[str, JsonValue], ...]:
        return ()

    async def call(
        self,
        server_id: str,
        tool_name: str,
        arguments: Mapping[str, JsonValue],
    ) -> PluginResult:
        self.calls.append((server_id, tool_name, arguments))
        return PluginResult(data={"called": True})
