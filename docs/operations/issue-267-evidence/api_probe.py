"""Manual paid smoke: isolated ledger + real Main loop, synthetic transports only."""

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory


async def main():
    sys.path[:0] = [str(Path.cwd()), str(Path.cwd() / "src")]
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from sqlalchemy import select, update
    from tests.conftest import MemorySender, build_harness, make_settings
    from tests.support.fixed_contract_fixture import bind_main_contract
    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.conversation.ordinary_admission_db_models import OrdinaryTurnAdmissionModel
    from qq_ai_bot.domain.conversations import ScopeType
    from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
    from qq_ai_bot.identity.db_models import CanonicalSpaceModel
    from qq_ai_bot.identity.write_settings import (
        IdentityWriteSettings,
        configure_identity_write_settings,
    )
    from qq_ai_bot.model_runtime.executor import TaskModelExecutor
    from qq_ai_bot.model_runtime.models import ModelCapability, ModelProfile, ModelRoute, ModelTask
    from qq_ai_bot.model_runtime.pool import ModelClientPool
    from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
    from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
    from qq_ai_bot.model_runtime.routes import ModelRouter
    from qq_ai_bot.persistence.database import Database
    from qq_ai_bot.services.autonomous_groups import AutonomousGroupService
    from qq_ai_bot.social.db_models import SocialOperationModel

    issue = int(sys.argv[1])
    report = {
        "issue": issue,
        "model": "deepseek-flash",
        "endpoint": "https://api.deepseek.com",
        "source": "real TaskModelExecutor + Main loop; isolated SQLite; synthetic delivery",
        "requests": [],
    }
    text = await asyncio.to_thread(Path(sys.argv[2]).read_text)
    key = re.search(r"\bsk-[A-Za-z0-9_-]+", text).group(0)
    with TemporaryDirectory(prefix=f"yuki-live-{issue}-", dir="/private/tmp") as directory:
        root = await asyncio.to_thread(Path(directory).resolve)
        db = Database(f"sqlite+aiosqlite:///{root}/test.db")
        await db.create_schema()
        configure_identity_write_settings(IdentityWriteSettings())
        env = await social_env(db, root)
        async with db.immediate_session() as writer:
            await writer.execute(
                update(CanonicalSpaceModel)
                .where(CanonicalSpaceModel.id == env.space)
                .values(require_mention=False)
            )
        profile = ModelProfile(
            id="deepseek-test",
            provider="deepseek",
            base_url="https://api.deepseek.com",
            api_key_env="YUKI_TEST_DEEPSEEK",
            model="deepseek-flash",
            timeout_seconds=120,
            max_retries=0,
            default_temperature=0.2,
            default_max_output_tokens=2048,
            thinking_enabled=False,
            reasoning_effort=None,
            capabilities=frozenset({ModelCapability.TOOLS}),
        )
        catalog = ModelProfileCatalog(
            profiles={profile.id: profile},
            routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
        )
        pool = ModelClientPool(secret_overrides={profile.api_key_env: key})
        executor = TaskModelExecutor(
            router=ModelRouter(catalog), pool=pool, invocations=ModelInvocationRepository(db)
        )

        class Provider:
            async def complete(self, request):
                started = time.perf_counter()
                response = await executor.execute(ModelTask.CHAT_AGENT, request)
                report["requests"].append(
                    {
                        "seconds": round(time.perf_counter() - started, 3),
                        "tools_declared": len(request.tools),
                        "tool_calls": [c.function.name for c in response.tool_calls],
                        "prompt_tokens": response.prompt_tokens,
                        "completion_tokens": response.completion_tokens,
                        "cached_tokens": response.cached_prompt_tokens,
                    }
                )
                return response

        settings = make_settings(
            db.url,
            llm_model="deepseek-flash",
            enabled_groups_csv="20001",
            conversation_autonomous_debounce_seconds=1.0,
            agent_max_model_requests=4,
            agent_max_tool_calls=8,
        )
        harness = build_harness(db, settings, Provider())
        bind_main_contract(harness, root)
        harness.processor._chat._tools.social_service = env.service
        sender = MemorySender()

        def message(i, text):
            return InboundMessage(
                message_id=f"live-{i}",
                event_type="message:test",
                scope_type=ScopeType.GROUP,
                sender=SenderIdentity("10001"),
                text=text,
                bot_user_id="80001",
                group_id="20001",
                conversation_id=env.context.conversation_id,
                person_id=env.person,
                presence_id=env.presence,
                space_id=env.space,
            )

        service = None
        try:
            if issue in {267, 268}:
                service = AutonomousGroupService(
                    chat=harness.processor._chat,
                    admission_features=SimpleNamespace(),
                    runtime_config=harness.processor._runtime_config,
                    turn_coordinator=harness.processor._turn_coordinator,
                )
                harness.processor._autonomous = service
                if issue == 268:
                    from tests.unit.test_participation_flags import override

                    await override(db, env.space, "conversation.auto_reply_enabled", False)
                    await override(db, env.space, "conversation.proactive_wakeup_enabled", False)
                    await harness.processor.handle(
                        message(-1, "产品开关关闭的合成群消息，不应启动新 Main。"), sender
                    )
                    observed = next(iter(harness.processor._observed_human_turns.values()))
                    await service.wait_until_idle(observed.snapshot.scope_key)
                    async with db.sessions() as reader:
                        disabled = list(
                            (await reader.scalars(select(OrdinaryTurnAdmissionModel))).all()
                        )
                    report.update(
                        disabled_requests=len(report["requests"]), disabled_admissions=len(disabled)
                    )
                    async with db.immediate_session() as writer:
                        from qq_ai_bot.persistence.models import RuntimeConfigOverrideModel

                        await writer.execute(
                            update(RuntimeConfigOverrideModel)
                            .where(
                                RuntimeConfigOverrideModel.canonical_space_id == env.space,
                                RuntimeConfigOverrideModel.config_key
                                == "conversation.auto_reply_enabled",
                            )
                            .values(value_json="true")
                        )
                results = [
                    await harness.processor.handle(message(i, t), sender)
                    for i, t in enumerate(
                        [
                            "这是合成 API 测试，只在隔离环境研究。",
                            "我分三段输入，请等我说完。",
                            (
                                "请用 send_message 回复一句：已合并收到三段测试消息。"
                                "无需创建任务或使用其他工具。"
                            ),
                        ]
                    )
                ]
                observed = list(harness.processor._observed_human_turns.values())
                await service.wait_until_idle(observed[-1].snapshot.scope_key)
                async with db.sessions() as reader:
                    admissions = list(
                        (await reader.scalars(select(OrdinaryTurnAdmissionModel))).all()
                    )
                receipts = []
                async with db.sessions() as reader:
                    receipts = list((await reader.scalars(select(SocialOperationModel))).all())
                report.update(
                    social_receipts=[{"action": r.action, "status": r.status} for r in receipts],
                    gateway_sent_messages=sum(
                        a in {"send_group_msg", "send_private_msg"} for a, _ in env.bot.calls
                    ),
                    ingress_reasons=[r.reason for r in results],
                    admitted_event_ids=[a.event_id for a in admissions],
                    synthetic_sent_messages=len(sender.messages),
                    merged_one_admission=len(admissions) == 1,
                )
            else:

                async def gateway(action, **params):
                    env.bot.calls.append((action, params))
                    if action == "get_forward_msg":
                        return {
                            "messages": [
                                {
                                    "sender": {"nickname": "synthetic"},
                                    "content": "合成转发的校验词是蓝鲸731。",
                                    "unknown": None,
                                },
                                {
                                    "message": [
                                        {"type": "forward", "data": {"id": "inner-not-fetched"}}
                                    ]
                                },
                            ]
                        }
                    if action in {"send_group_msg", "send_private_msg"}:
                        return {"message_id": 99001}
                    raise AssertionError(action)

                env.bot.call_api = gateway
                env.bot.calls.clear()
                # Persist the real outer event before asking Main, so event IDs are ledger facts.
                event, _ = await harness.ledger.append(
                    bot_user_id="80001",
                    platform_message_id="forward-source",
                    scope_type=ScopeType.GROUP,
                    sender_user_id="10001",
                    direction="inbound",
                    content="合成合并转发",
                    group_id="20001",
                    segments=[{"type": "forward", "data": {"id": "outer-test"}}],
                    occurred_at=datetime.now(UTC),
                )
                result = await harness.processor.handle(
                    message(
                        1,
                        (
                            "这是隔离 API 测试。请调用 read_conversation_history，"
                            f"event_id={event.id}，"
                            "读取原合并转发第一层，用 send_message 告诉我校验词。"
                            "不要读取内层或使用其他工具。"
                        ),
                    ),
                    sender,
                )
                report.update(
                    process_reason=result.reason,
                    gateway_actions=[{"action": a, "params": p} for a, p in env.bot.calls],
                    synthetic_sent_messages=len(sender.messages),
                )
        except Exception as exc:
            report["error_category"] = type(exc).__name__
        finally:
            if service:
                await service.close()
            await pool.close()
            await db.close()
    await asyncio.to_thread(
        Path(sys.argv[3]).write_text, json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(json.dumps(report, ensure_ascii=False))


asyncio.run(main())
