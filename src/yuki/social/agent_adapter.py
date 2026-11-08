"""Translate event-bound runtime into social context without inventing actors."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from yuki.capabilities.invocation import current_invocation
from yuki.identity.routing import RouteSendError
from yuki.persistence.models import ChatEventModel
from yuki.runtime.origin import TurnOrigin
from yuki.social.models import SocialError
from yuki.social.service import SocialContext, SocialService


async def invoke_social(
    service: SocialService, name: str, arguments: dict[str, Any], runtime: Any
) -> dict[str, Any]:
    if runtime.actor_context is not None and runtime.actor_context.principal_kind == "self":
        invocation = current_invocation.get()
        actor = runtime.require_actor()
        if (
            invocation is None
            or not invocation.invocation_key
            or runtime.tools_closed
            or actor.principal_kind != "self"
            or runtime.inbound is not None
        ):
            raise SocialError("permission_denied")
        scheduled_run = actor.automation_run_id
        context = SocialContext(
            turn_id=f"{actor.conversation_id}:{actor.source_key}",
            call_id=invocation.invocation_key,
            conversation_id=actor.conversation_id or "",
            space_id=runtime.space_id,
            origin=runtime.origin.value,
            initiative_run_id=actor.initiative_run_id,
            automation_run_id=scheduled_run,
            presence_id=actor.presence_id,
            actor=actor,
            runtime_snapshot=getattr(runtime, "runtime_config", None),
            turn_token=getattr(runtime, "turn_token", None),
            conversation_key=getattr(runtime, "conversation_key", ""),
        )
        try:
            return await service.execute(name, arguments, context)
        except RouteSendError as exc:
            raise SocialError(exc.category) from exc
        except ValidationError as exc:
            raise SocialError(validation_code(exc)) from exc
    if name == "send" and runtime.origin is TurnOrigin.PLUGIN_BACKGROUND:
        from yuki.conversation.canonical_db_models import CanonicalConversationModel

        invocation = current_invocation.get()
        event_id = runtime.effective_trigger_event_id
        conversation_id = runtime.effective_conversation_id
        if (
            invocation is None
            or not invocation.invocation_key
            or runtime.inbound is not None
            or runtime.tools_closed
            or not isinstance(event_id, int)
            or not conversation_id
            or arguments.get("target") is not None
        ):
            raise SocialError("permission_denied")
        async with service.database.read_session() as session:
            event = await session.get(ChatEventModel, event_id)
            conversation = await session.get(CanonicalConversationModel, conversation_id)
            if (
                event is None
                or conversation is None
                or event.canonical_conversation_id != conversation_id
                or event.direction != "external"
                or event.event_kind != "external_event"
                or event.origin != "plugin_background"
                or event.suppression_status != "keeper"
                or not event.source_plugin_id
                or (runtime.space_id or None) != (conversation.space_id or None)
                or (runtime.person_id or None) != (conversation.person_id or None)
            ):
                raise SocialError("permission_denied")
        context = SocialContext(
            turn_id=f"{conversation_id}:plugin-event:{event_id}",
            call_id=invocation.invocation_key,
            conversation_id=conversation_id,
            person_refs=(
                {"current_speaker": runtime.person_id} if runtime.person_id is not None else {}
            ),
            space_id=runtime.space_id,
            origin="plugin_background",
            caused_by_event_id=event_id,
            runtime_snapshot=getattr(runtime, "runtime_config", None),
            turn_token=getattr(runtime, "turn_token", None),
            conversation_key=getattr(runtime, "conversation_key", ""),
        )
        try:
            return await service.execute(name, arguments, context)
        except RouteSendError as exc:
            raise SocialError(exc.category) from exc
        except ValidationError as exc:
            raise SocialError(validation_code(exc)) from exc
    if getattr(runtime, "read_target_id", None) is not None and name == "read_chat":
        actor = runtime.actor_context
        invocation = current_invocation.get()
        if invocation is None or runtime.tools_closed:
            raise SocialError("missing_read_scope")
        context = SocialContext(
            turn_id=f"{runtime.effective_conversation_id}:{runtime.effective_execution_id}",
            call_id=invocation.invocation_key,
            conversation_id=runtime.effective_conversation_id or "",
            space_id=runtime.space_id,
            person_refs={"current_speaker": actor.person_id}
            if actor is not None and actor.person_id
            else {},
            actor=actor,
            origin=runtime.origin.value,
        )
        return await service.execute(name, arguments, context)
    if getattr(runtime, "read_target_id", None) is not None and name == "find_contacts":
        from yuki.identity.person_identity import same_person_id
        from yuki.services.history_query import HistoryTarget
        from yuki.social.contacts import contact_subject

        scope = runtime.read_scope
        target_id = runtime.read_target_id
        invocation = current_invocation.get()
        if scope is None or invocation is None or runtime.tools_closed:
            raise SocialError("missing_read_scope")
        kind = "space" if scope.group_id else "person"
        if arguments.get("type", "group" if kind == "space" else "person") != (
            "group" if kind == "space" else "person"
        ):
            raise SocialError("history_scope_denied")
        context = SocialContext(
            turn_id=f"{runtime.effective_conversation_id}:{runtime.effective_execution_id}",
            call_id=invocation.invocation_key,
            conversation_id=runtime.effective_conversation_id or "",
            space_id=target_id if kind == "space" else None,
            person_refs={"current_speaker": target_id} if kind == "person" else {},
        )
        selected = dict(arguments)
        if selected.get("target"):
            async with service.database.read_session() as session:
                chosen_kind, chosen = await contact_subject(
                    session, HistoryTarget.model_validate(selected["target"])
                )
                if chosen != target_id and (
                    chosen_kind != "person" or not await same_person_id(session, chosen, target_id)
                ):
                    raise SocialError("history_scope_denied")
        selected.pop("name", None)
        selected["target"] = {"person_id" if kind == "person" else "space_id": target_id}
        return await service.execute(name, selected, context)
    if (
        runtime.origin
        not in {
            TurnOrigin.USER_MESSAGE,
            TurnOrigin.AUTONOMOUS_GROUP,
            TurnOrigin.SCHEDULED_AUTOMATION,
            TurnOrigin.PLUGIN_SESSION,
        }
        or runtime.tools_closed
    ):
        raise SocialError("permission_denied")
    invocation = current_invocation.get()
    if invocation is None or not invocation.invocation_key:
        raise SocialError("missing_call_context")
    inbound = runtime.inbound
    actor = runtime.require_actor()
    refs = {"current_speaker": actor.person_id} if actor.person_id is not None else {}
    conversation_id = runtime.effective_conversation_id
    if not conversation_id:
        raise SocialError("missing_call_context")
    execution_id = actor.source_key
    context = SocialContext(
        turn_id=f"{conversation_id}:{execution_id}",
        call_id=invocation.invocation_key,
        conversation_id=conversation_id,
        origin=runtime.origin.value,
        person_refs=refs,
        space_id=runtime.space_id or getattr(inbound, "space_id", None),
        trigger_event_id=(
            getattr(runtime, "effective_trigger_event_id", None) if inbound is not None else None
        ),
        reply_presence_id=inbound.presence_id
        if inbound is not None and inbound.scope_type == "private"
        else None,
        actor=actor,
        presence_id=actor.presence_id,
        runtime_snapshot=getattr(runtime, "runtime_config", None),
        turn_token=getattr(runtime, "turn_token", None),
        conversation_key=getattr(runtime, "conversation_key", ""),
        inbound=inbound,
    )
    try:
        return await service.execute(name, arguments, context)
    except RouteSendError as exc:
        raise SocialError(exc.category) from exc
    except ValidationError as exc:
        raise SocialError(validation_code(exc)) from None


def validation_code(exc: ValidationError) -> str:
    """A model-facing code from the failed constraint, never the input values."""
    messages = {str(error.get("msg", "")) for error in exc.errors()}
    for code in (
        "choose_one_source",
        "device_requires_path",
        "message_media_conflict",
        "message_empty",
        "around_with_filters",
        "target_operation_unsupported",
    ):
        if any(code in message for message in messages):
            return code
    return "invalid_arguments"


SOCIAL_ERROR_MESSAGES = {
    "invalid_arguments": "参数不符合工具说明；按 schema 检查字段名、类型与取值",
    "choose_one_source": "file 需要且只能给 path 或 artifact_id 其中之一",
    "device_requires_path": "file.device 只能与 file.path 一起使用",
    "message_media_conflict": "file 与 sticker 不能同时发送；分两次调用",
    "message_empty": "text、file、sticker 至少给一项",
    "empty_message_after_sanitization": "text 清理后为空；给出实际要发送的内容",
    "control_token_not_message": "NO_REPLY 不是消息；不需要发言时不要调用 send",
    "around_with_filters": "around 读取锚点邻文，只能配合 target 与 context，不能加筛选或分页",
    "target_operation_unsupported": "find_contacts 的 target 不接受 operation_id",
    "invalid_target": "target 只能包含一个内部 ID 键",
    "invalid_target_id": "target 中的值必须是内部 UUID；当前会话可省略 target",
    "target_not_found": "target 指向的会话、入口或主体不存在；先用 find_contacts 查找",
    "target_required": "当前会话没有可用的人物目标；用 target 指定",
    "group_target_required": "target 需要指向一个群；当前群可省略",
    "binding_requires_person": "target.binding_id 必须是人物的平台入口",
    "binding_required": "对方有多个入口；从 bindings 选一个 binding_id 作为 target 再发送",
    "target_entry_mismatch": "target.conversation_id 与目标主体不一致",
    "contact_not_allowed": "该人物没有可联系的已知会话，未执行",
    "space_not_allowed": "该群不可用，未执行",
    "reply_event_unavailable": "reply_to 必须是目标会话中的消息事件 ID",
    "route_paused": "该目标的发送路由已暂停，未执行",
    "binding_ambiguous": "目标有多个有效入口；先 find_contacts 再用 binding_id 指定",
    "binding_unavailable": "指定入口无效或不属于目标，未执行",
    "artifact_not_immutable": "file.artifact_id 不是已冻结的文件快照",
    "not_own_message": "只能撤回自己发送的消息",
    "message_recall_unavailable": "该消息没有平台消息号，无法撤回",
    "original_presence_unavailable": "原消息的发送账号当前不可用，未撤回",
    "group_unavailable": "当前没有可访问该群的连接，未执行",
    "history_receipt_unavailable": "target.operation_id 不属于当前会话，或回执已不存在",
    "history_source_changed": "来源授权或隐私状态已变化，未返回旧内容",
    "history_scope_denied": "当前只获准读取指定的人物或群",
    "history_unavailable": "所选范围没有可读取的会话",
    "forward_presence_unavailable": "接收这条转发的原账号当前不可用，未读取转发内容",
    "forward_unsupported": "该消息所在平台连接不提供合并转发读取",
    "forward_read_failed": "平台没有返回这份合并转发的内容",
    "forward_locator_missing": "这条转发缺少平台定位信息，无法读取",
    "invalid_cursor": "cursor 必须原样使用上一页返回的 next_cursor",
    "permission_denied": "当前执行没有该操作的权限",
}


def _status(value: object) -> object:
    return "unknown" if value in {"uncertain", "executing"} else value


def receipt_view(result: dict[str, Any]) -> dict[str, Any]:
    """A send or recall receipt as the model needs it: status and each part's event."""

    def part(item: dict[str, Any]) -> dict[str, Any]:
        value = {
            "status": _status(item.get("status")),
            "event_id": item.get("event_id"),
            "error": item.get("error_category"),
        }
        return {key: entry for key, entry in value.items() if entry is not None}

    # operation_id lets an unknown outcome be checked later with read_chat.
    view: dict[str, Any] = {
        "status": _status(result.get("status")),
        "operation_id": result.get("operation_id"),
    }
    parts = result.get("parts")
    if isinstance(parts, list) and len(parts) > 1:
        view["parts"] = [part(item) for item in parts if isinstance(item, dict)]
    else:
        single = parts[0] if isinstance(parts, list) and parts else result
        if isinstance(single, dict) and single.get("event_id") is not None:
            view["event_id"] = single["event_id"]
    for key in ("artifact_id", "error", "bindings", "unresolved_mentions", "mentioned_accounts"):
        if result.get(key):
            view[key] = result[key]
    sticker = result.get("emoji")
    if isinstance(sticker, dict):
        view["sticker"] = {
            key: _status(sticker[key]) for key in ("status", "error") if sticker.get(key)
        }
    return {key: value for key, value in view.items() if value is not None}


def members_view(result: dict[str, Any]) -> dict[str, Any]:
    items = [
        {
            key: value
            for key, value in {
                "account_id": item.get("account_id"),
                "reference": item.get("reference"),
                "display_name": item.get("display_name"),
                "person_id": item.get("person_id"),
                "yuki": True if item.get("presence_id") else None,
            }.items()
            if value is not None
        }
        for item in result.get("items", ())
    ]
    view: dict[str, Any] = {"items": items}
    if result.get("next_cursor"):
        view["next_cursor"] = result["next_cursor"]
    for key in ("error", "bindings"):
        if result.get(key):
            view[key] = result[key]
    return view
