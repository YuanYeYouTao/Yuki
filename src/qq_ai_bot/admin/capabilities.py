"""Administrator-only tool definitions and verified execution gateway."""

from __future__ import annotations

import json
import time
from typing import Any, Literal, cast

from qq_ai_bot.admin.action_service import ActionRegistry, AdminActionService, TargetResolver
from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import AdminActor, ConfigChangeResult, EffectiveConfigValue
from qq_ai_bot.admin.permission_catalog import PermissionCatalogService
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.messages import ChatTool
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.service import MemoryRebuildService
from qq_ai_bot.services.agent_tools import ToolRuntime


def _object_schema(
    properties: dict[str, object],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


class CapabilityRegistry:
    """Expose exactly the reviewed administrator functions."""

    def __init__(self, action_registry: ActionRegistry | None = None) -> None:
        self._action_registry = action_registry or ActionRegistry()

    def definitions(self) -> tuple[ChatTool, ...]:
        action_names = [spec.name for spec in self._action_registry.list()]
        scope_properties = {
            "scope_type": {
                "type": "string",
                "enum": ["global", "group", "user"],
            },
            "scope_id": {
                "type": "string",
                "description": (
                    "global 使用空字符串；当前群用 current_group；本人用 self；"
                    "其他 QQ/群号必须真实出现在当前消息中"
                ),
            },
        }
        base = (
            ChatTool(
                name="admin_get_config",
                description="读取一个或多个注册配置的真实有效值；凭证只返回是否已配置。",
                parameters=_object_schema(
                    {
                        "keys": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 20,
                        },
                        **scope_properties,
                    },
                    required=("keys", "scope_type", "scope_id"),
                ),
            ),
            ChatTool(
                name="admin_set_config",
                description=(
                    "设置一个注册配置覆盖；后端执行类型、范围、作用域与权限校验。"
                    "不知道 key/范围时先用 admin_list_capabilities 的 focused+category/query "
                    "内部查找，查到后继续调用本工具，不要把查询结果发给用户。"
                ),
                parameters=_object_schema(
                    {
                        "key": {"type": "string"},
                        "value": {},
                        **scope_properties,
                    },
                    required=("key", "value", "scope_type", "scope_id"),
                ),
            ),
            ChatTool(
                name="admin_delete_config_override",
                description="删除一个数据库配置覆盖并恢复更低优先级值。",
                parameters=_object_schema(
                    {
                        "key": {"type": "string"},
                        **scope_properties,
                    },
                    required=("key", "scope_type", "scope_id"),
                ),
            ),
            ChatTool(
                name="admin_execute_action",
                description=(
                    "执行 ActionRegistry 中的关系、记忆、偏好、群或私聊权限操作。"
                    "arguments.target 必须使用枚举，绝不能直接把 QQ/群号填进 target。"
                    "例如设置明确 QQ 的好感度：action=relationship.set_affection，"
                    'arguments={"target":"explicit_user_id","user_id":"该QQ","value":88}。'
                    "当前消息真实 @ 的用户用 mentioned_user；本人用 self；当前群用 current_group。"
                    "action 参数规则：relationship.get/history 只需 target；"
                    "relationship.set_affection/set_trust 还需 value=0..100；"
                    "relationship.adjust_affection 还需 delta=-20..20；memory.add 需 content，"
                    "memory.update 需 memory_id+content，memory.delete 需 memory_id；"
                    "memory.prune 需 max_importance=1..5 和 older_than_days=1..3650；"
                    "preference.set 需 key+value，preference.delete 需 key；"
                    "表情 action 使用 emoji_id（可为唯一前缀）；emoji.list 可选 status；"
                    "emoji.adopt/unadopt 可选 scope_type=global|group 和 scope_id=current_group；"
                    "emoji.pin 还需 enabled。其余人物/群 action 只需 target。"
                    "speech profile/reference action 使用 profile_id；speech.test 使用 text，"
                    "可选 profile_id 与 style_hint。"
                    "缺少必需信息时先用自然语言简短追问，下一条结合正常聊天上下文继续。"
                ),
                parameters=_object_schema(
                    {
                        "action": {"type": "string", "enum": action_names},
                        "arguments": _object_schema(
                            {
                                "target": {
                                    "type": "string",
                                    "enum": [
                                        "self",
                                        "mentioned_user",
                                        "explicit_user_id",
                                        "current_group",
                                        "explicit_group_id",
                                    ],
                                },
                                "user_id": {
                                    "type": "string",
                                    "description": (
                                        "target=explicit_user_id 时必填；QQ 必须在当前正文"
                                    ),
                                },
                                "group_id": {
                                    "type": "string",
                                    "description": (
                                        "target=explicit_group_id 时必填；群号必须在当前正文"
                                    ),
                                },
                                "value": {
                                    "description": "好感度/信任度整数，或 preference.set 的文本值"
                                },
                                "delta": {"type": "integer", "minimum": -20, "maximum": 20},
                                "memory_id": {"type": "integer", "minimum": 1},
                                "max_importance": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 5,
                                },
                                "older_than_days": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 3650,
                                },
                                "content": {"type": "string"},
                                "key": {"type": "string"},
                                "emoji_id": {"type": "string"},
                                "status": {"type": "string"},
                                "scope_type": {
                                    "type": "string",
                                    "enum": ["global", "group"],
                                },
                                "scope_id": {"type": "string"},
                                "enabled": {"type": "boolean"},
                                "profile_id": {"type": "string"},
                                "style_hint": {"type": "string"},
                                "text": {"type": "string"},
                            },
                        ),
                    },
                    required=("action", "arguments"),
                ),
            ),
            ChatTool(
                name="admin_get_history",
                description=(
                    "查看当前真实管理员最近的配置修改记录，供回答‘之前改过哪些参数’、"
                    "说明或回滚；此类历史问题不需要用户群号，禁止改用待补充工具询问群号。"
                ),
                parameters=_object_schema(
                    {
                        "key": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    }
                ),
            ),
            ChatTool(
                name="admin_rollback_change",
                description="恢复当前管理员本人执行且尚未被后续修改覆盖的一次配置变更。",
                parameters=_object_schema(
                    {"change_id": {"type": "integer", "minimum": 1}},
                    required=("change_id",),
                ),
            ),
        )
        run_schema = _object_schema({"run_id": {"type": "string"}}, required=("run_id",))
        return (
            *base,
            ChatTool(
                name="admin_memory_rebuild_plan",
                description="仅规划 chat_events 历史记忆重建；不调用模型，也不写事实。",
                parameters=_object_schema(
                    {"selection": {"type": "object"}}, required=("selection",)
                ),
            ),
            ChatTool(
                name="admin_memory_rebuild_start",
                description="显式开始 proposal 提取。",
                parameters=run_schema,
            ),
            ChatTool(
                name="admin_memory_rebuild_status",
                description="读取重建状态和有界统计。",
                parameters=run_schema,
            ),
            ChatTool(
                name="admin_memory_rebuild_pause",
                description="在批次边界暂停重建。",
                parameters=run_schema,
            ),
            ChatTool(
                name="admin_memory_rebuild_resume",
                description="显式恢复已暂停重建。",
                parameters=run_schema,
            ),
            ChatTool(
                name="admin_memory_rebuild_cancel",
                description="取消后续处理，不回滚已提交事实。",
                parameters=run_schema,
            ),
            ChatTool(
                name="admin_memory_rebuild_review",
                description="分页查看待审阅的 canonical claims。",
                parameters=_object_schema(
                    {"run_id": {"type": "string"}, "page": {"type": "integer", "minimum": 1}},
                    required=("run_id",),
                ),
            ),
            ChatTool(
                name="admin_memory_rebuild_approve",
                description="批准全部或指定 proposal；不能编辑主体和内容。",
                parameters=_object_schema(
                    {"run_id": {"type": "string"}, "selector": {"type": "string"}},
                    required=("run_id", "selector"),
                ),
            ),
            ChatTool(
                name="admin_memory_rebuild_reject",
                description="拒绝全部或指定 proposal。",
                parameters=_object_schema(
                    {"run_id": {"type": "string"}, "selector": {"type": "string"}},
                    required=("run_id", "selector"),
                ),
            ),
            ChatTool(
                name="admin_memory_rebuild_commit",
                description="审阅完成后按历史顺序提交。",
                parameters=run_schema,
            ),
        )


class AdminCapabilityService:
    """Execute administrator tools only for an authority-bound current event."""

    def __init__(
        self,
        *,
        settings: Settings,
        runtime_config: RuntimeConfigService,
        actions: AdminActionService,
        registry: CapabilityRegistry | None = None,
        audit: AdminAuditService | None = None,
        permission_catalog: PermissionCatalogService | None = None,
        memory_rebuild: MemoryRebuildService | None = None,
    ) -> None:
        self._settings = settings
        self._runtime_config = runtime_config
        self._actions = actions
        self.registry = registry or CapabilityRegistry(actions.registry)
        self._audit = audit
        self._permission_catalog = permission_catalog or PermissionCatalogService(
            settings=settings,
            config_registry=runtime_config.registry,
            action_registry=actions.registry,
        )
        self._memory_rebuild = memory_rebuild

    def definitions(self) -> tuple[ChatTool, ...]:
        return self.registry.definitions()

    def is_mutating_call(self, name: str, arguments_json: str) -> bool:
        """Classify a tool call from the same ActionRegistry used for execution."""

        if name.startswith("admin_memory_rebuild_"):
            return name not in {"admin_memory_rebuild_status", "admin_memory_rebuild_review"}
        if name in {
            "admin_set_config",
            "admin_delete_config_override",
            "admin_rollback_change",
        }:
            return True
        if name != "admin_execute_action":
            return False
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError:
            return True
        if not isinstance(arguments, dict) or not isinstance(arguments.get("action"), str):
            return True
        try:
            return self._actions.registry.get(arguments["action"]).mutating
        except KeyError:
            return True

    async def execute(
        self,
        name: str,
        arguments_json: str,
        runtime: ToolRuntime,
    ) -> str:
        """Return a bounded JSON result that never trusts model-supplied authority."""

        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError:
            return self._result(error="invalid_json", detail="工具参数不是有效 JSON")
        if not isinstance(arguments, dict):
            return self._result(error="invalid_arguments", detail="工具参数必须是对象")
        actor: AdminActor | None = None
        started = time.perf_counter()
        try:
            actor = self._actor(runtime)
            if name.startswith("admin_memory_rebuild_"):
                return self._result(data=await self._execute_memory_rebuild(name, arguments, actor))
            if name == "admin_list_capabilities":
                return self._result(data=self._list_capabilities(arguments, actor, runtime))
            if name == "admin_get_config":
                return self._result(
                    data=await self._get_config(arguments, actor),
                )
            if name == "admin_set_config":
                return self._change_result(
                    await self._set_config(arguments, actor),
                )
            if name == "admin_delete_config_override":
                return self._change_result(
                    await self._delete_config(arguments, actor),
                )
            if name == "admin_execute_action":
                return self._result(
                    data=await self._execute_action(arguments, actor),
                )
            if name == "admin_get_history":
                return self._result(
                    data=await self._history(arguments, actor),
                )
            if name == "admin_rollback_change":
                return self._change_result(
                    await self._rollback(arguments, actor),
                )
            return self._result(error="unknown_tool", detail=f"未知管理员工具：{name}")
        except PermissionError as exc:
            await self._record_action_failure(
                name,
                arguments,
                actor,
                exc,
                started,
            )
            return self._result(error="permission_denied", detail=str(exc))
        except KeyError as exc:
            await self._record_action_failure(
                name,
                arguments,
                actor,
                exc,
                started,
            )
            return self._result(error="unknown_capability", detail=str(exc))
        except (TypeError, ValueError) as exc:
            await self._record_action_failure(
                name,
                arguments,
                actor,
                exc,
                started,
            )
            return self._result(error="validation_error", detail=str(exc))
        except (OSError, RuntimeError) as exc:
            await self._record_action_failure(
                name,
                arguments,
                actor,
                exc,
                started,
            )
            return self._result(error=type(exc).__name__, detail="管理员操作执行失败")

    async def _record_action_failure(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        actor: AdminActor | None,
        exc: Exception,
        started: float,
    ) -> None:
        if self._audit is None or actor is None or tool_name != "admin_execute_action":
            return
        action = arguments.get("action")
        if not isinstance(action, str):
            action = "unknown"
        await self._audit.record(
            actor=actor,
            capability="admin_action",
            operation=action,
            target_type="unresolved",
            target_id="",
            before=None,
            after={
                "argument_keys": sorted(
                    str(key)[:64]
                    for key in (
                        arguments.get("arguments", {}).keys()
                        if isinstance(arguments.get("arguments"), dict)
                        else ()
                    )
                )
            },
            success=False,
            error_category=type(exc).__name__,
            duration_seconds=time.perf_counter() - started,
        )

    async def _execute_memory_rebuild(
        self,
        name: str,
        arguments: dict[str, Any],
        actor: AdminActor,
    ) -> Any:
        service = self._memory_rebuild
        if service is None:
            raise RuntimeError("memory rebuild service is unavailable")
        operation = name.removeprefix("admin_memory_rebuild_")
        if operation == "plan":
            selection = arguments.get("selection")
            if not isinstance(selection, dict):
                raise ValueError("selection must be an object")
            run = await service.plan(
                MemoryRebuildSelection.model_validate(selection),
                actor_user_id=actor.user_id,
            )
            return run.model_dump(mode="json")
        run_id = _required_string(arguments, "run_id")
        if operation == "status":
            return await service.status(run_id, actor_user_id=actor.user_id)
        if operation == "review":
            page = arguments.get("page", 1)
            if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
                raise ValueError("page must be a positive integer")
            rows = await service.review(run_id, actor_user_id=actor.user_id, page=page)
            return [row.model_dump(mode="json") for row in rows]
        if operation in {"approve", "reject"}:
            selector = _required_string(arguments, "selector")
            return {
                "changed": await service.set_review(
                    run_id,
                    selector,
                    approved=operation == "approve",
                    actor_user_id=actor.user_id,
                )
            }
        method = getattr(service, operation, None)
        if method is None:
            raise ValueError(f"unknown memory rebuild operation: {operation}")
        result = await method(run_id, actor_user_id=actor.user_id)
        return result.model_dump(mode="json")

    def _actor(self, runtime: ToolRuntime) -> AdminActor:
        inbound = runtime.inbound
        if (
            not runtime.actor_is_superuser
            or runtime.actor_user_id != inbound.sender.user_id
            or runtime.actor_user_id not in self._settings.superusers
            or runtime.trigger_message_id != inbound.message_id
            or runtime.current_group_id != inbound.group_id
            or tuple(runtime.mentioned_user_ids) != tuple(inbound.mentioned_user_ids)
        ):
            raise PermissionError("当前管理员工具没有绑定到真实超级管理员事件")
        return AdminActor(
            user_id=runtime.actor_user_id,
            is_superuser=runtime.actor_is_superuser,
            trigger_message_id=runtime.trigger_message_id,
            conversation_key=runtime.conversation_key,
            current_group_id=runtime.current_group_id,
            mentioned_user_ids=runtime.mentioned_user_ids,
            current_message_text=inbound.text,
            bot_user_id=inbound.bot_user_id,
            decision_actor_type="admin",
            decision_actor_id="admin_agent",
        )

    def _list_capabilities(
        self,
        arguments: dict[str, Any],
        actor: AdminActor,
        runtime: ToolRuntime,
    ) -> dict[str, Any]:
        mode, category, query = _capability_options(arguments)
        if actor.user_id != runtime.inbound.sender.user_id:
            raise PermissionError("权限目录没有绑定到当前真实发送者")
        return self._permission_catalog.report_for_message(
            runtime.inbound,
            category=category,
            query=query,
        ).to_model_dict(mode)

    async def _get_config(
        self,
        arguments: dict[str, Any],
        actor: AdminActor,
    ) -> dict[str, Any]:
        raw_keys = arguments.get("keys")
        if (
            not isinstance(raw_keys, list)
            or not raw_keys
            or len(raw_keys) > 20
            or not all(isinstance(key, str) for key in raw_keys)
        ):
            raise ValueError("keys 必须是包含 1～20 个配置键的数组")
        scope_type, scope_id = TargetResolver.config_scope(
            _required_string(arguments, "scope_type"),
            arguments.get("scope_id", ""),
            actor,
        )
        user_id = scope_id if scope_type == "user" else None
        group_id = scope_id if scope_type == "group" else None
        values = [
            await self._runtime_config.get_effective(
                key,
                user_id=user_id,
                group_id=group_id,
            )
            for key in raw_keys
        ]
        return {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "values": [self._effective_json(item) for item in values],
        }

    async def _set_config(
        self,
        arguments: dict[str, Any],
        actor: AdminActor,
    ) -> ConfigChangeResult:
        key = _required_string(arguments, "key")
        if "value" not in arguments:
            raise ValueError("缺少 value")
        scope_type, scope_id = TargetResolver.config_scope(
            _required_string(arguments, "scope_type"),
            arguments.get("scope_id", ""),
            actor,
        )
        return await self._runtime_config.set_override(
            key,
            arguments["value"],
            scope_type=scope_type,
            scope_id=scope_id,
            actor_user_id=actor.user_id,
            trigger_message_id=actor.trigger_message_id,
            conversation_key=actor.conversation_key,
        )

    async def _delete_config(
        self,
        arguments: dict[str, Any],
        actor: AdminActor,
    ) -> ConfigChangeResult:
        key = _required_string(arguments, "key")
        scope_type, scope_id = TargetResolver.config_scope(
            _required_string(arguments, "scope_type"),
            arguments.get("scope_id", ""),
            actor,
        )
        return await self._runtime_config.delete_override(
            key,
            scope_type=scope_type,
            scope_id=scope_id,
            actor_user_id=actor.user_id,
            trigger_message_id=actor.trigger_message_id,
            conversation_key=actor.conversation_key,
        )

    async def _execute_action(
        self,
        arguments: dict[str, Any],
        actor: AdminActor,
    ) -> dict[str, Any]:
        action = _required_string(arguments, "action")
        action_arguments = arguments.get("arguments")
        if not isinstance(action_arguments, dict):
            raise ValueError("arguments 必须是对象")
        result = await self._actions.execute(action, action_arguments, actor)
        return {"action": action, "result": result}

    async def _history(
        self,
        arguments: dict[str, Any],
        actor: AdminActor,
    ) -> dict[str, Any]:
        key = arguments.get("key")
        if key is not None and not isinstance(key, str):
            raise ValueError("key 必须是字符串")
        limit = arguments.get("limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit 必须是 1～20 的整数")
        rows = await self._runtime_config.history(
            key=key,
            actor_user_id=actor.user_id,
            limit=limit,
        )
        return {
            "events": [
                {
                    "change_id": row.id,
                    "operation": row.operation,
                    "key": row.target_id,
                    "scope": row.target_type,
                    "before": row.before,
                    "after": row.after,
                    "success": row.success,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]
        }

    async def _rollback(
        self,
        arguments: dict[str, Any],
        actor: AdminActor,
    ) -> ConfigChangeResult:
        change_id = arguments.get("change_id")
        if isinstance(change_id, bool) or not isinstance(change_id, int) or change_id <= 0:
            raise ValueError("change_id 必须是正整数")
        return await self._runtime_config.rollback(
            change_id,
            actor_user_id=actor.user_id,
            trigger_message_id=actor.trigger_message_id,
            conversation_key=actor.conversation_key,
        )

    @staticmethod
    def _effective_json(value: EffectiveConfigValue) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": value.key,
            "source": value.source,
            "scope_type": value.scope_type.value if value.scope_type else None,
            "scope_id": value.scope_id,
            "apply_mode": value.apply_mode.value,
            "pending_restart": value.pending_restart,
        }
        if value.configured is not None:
            payload["configured"] = value.configured
        else:
            payload["value"] = value.value
        return payload

    def _change_result(self, result: ConfigChangeResult) -> str:
        data = {
            "key": result.key,
            "scope_type": result.scope_type.value,
            "scope_id": result.scope_id,
            "before": result.before,
            "after": result.after,
            "apply_mode": result.apply_mode.value if result.apply_mode else None,
            "pending_restart": result.pending_restart,
            "change_id": result.change_id,
            "version": result.version,
            "detail": result.detail,
        }
        if not result.success:
            return self._result(
                error=result.error_category or "operation_failed",
                detail=result.detail,
                data=data,
            )
        return self._result(data=data)

    @staticmethod
    def _result(
        *,
        data: Any = None,
        error: str | None = None,
        detail: str = "",
    ) -> str:
        payload = (
            {"ok": False, "error": error, "detail": detail, "data": data}
            if error
            else {"ok": True, "data": data}
        )
        rendered = json.dumps(payload, ensure_ascii=False, default=str)
        if len(rendered) <= 24000:
            return rendered
        return json.dumps(
            {
                "ok": False,
                "error": "result_too_large",
                "detail": "管理员工具结果超过字符上限，请缩小查询范围",
                "original_characters": len(rendered),
            },
            ensure_ascii=False,
        )


def _required_string(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必须是非空字符串")
    return value.strip()


def _capability_options(
    arguments: dict[str, Any],
) -> tuple[Literal["summary", "focused", "full"], str | None, str | None]:
    extra = set(arguments) - {"mode", "category", "query"}
    if extra:
        raise ValueError("能力查询只接受 mode、category、query")
    raw_mode = arguments.get("mode", "summary")
    if raw_mode not in {"summary", "focused", "full"}:
        raise ValueError("mode 必须是 summary、focused 或 full")
    category = arguments.get("category")
    query = arguments.get("query")
    if category is not None and not isinstance(category, str):
        raise ValueError("category 必须是字符串")
    if query is not None and not isinstance(query, str):
        raise ValueError("query 必须是字符串")
    if raw_mode == "focused" and not (category or query):
        raise ValueError("focused 模式必须提供 category 或 query")
    return cast(Literal["summary", "focused", "full"], raw_mode), category, query
