"""Compile model-friendly task intents into the strict automation runtime IR."""

from __future__ import annotations

import json
from typing import Literal

from qq_ai_bot.automation.models import (
    AutomationLimits,
    AutomationScript,
    AutomationStep,
    StrictModel,
)
from qq_ai_bot.automation.task_spec import (
    TaskDelivery as TaskDelivery,
)
from qq_ai_bot.automation.task_spec import (
    TaskSpec as TaskSpec,
)
from qq_ai_bot.automation.task_spec import (
    TaskStrategy as TaskStrategy,
)
from qq_ai_bot.automation.validator import CreationProvenance
from qq_ai_bot.config import Settings


class ExecutionPlan(StrictModel):
    """Backend-generated plan; only the contained script is persisted and executed."""

    strategy: Literal["static", "generated", "agentic"]
    script: AutomationScript
    warnings: tuple[str, ...] = ()


class AutomationCompiler:
    """Deterministically lower a TaskSpec into the already-audited DSL."""

    def __init__(
        self,
        *,
        settings: Settings,
    ) -> None:
        self._settings = settings

    def compile(
        self,
        task: TaskSpec,
        provenance: CreationProvenance,
        *,
        default_timezone: str,
    ) -> ExecutionPlan:
        strategy = self._resolve_strategy(task)
        timezone = task.timezone or default_timezone
        delivery = self._resolve_delivery(task.delivery, provenance)
        warnings: list[str] = []
        steps: tuple[AutomationStep, ...]

        if strategy == "static":
            if delivery is None:
                raise ValueError("static 任务必须声明消息投递目标")
            steps = (self._delivery_step(delivery, task.delivery.text or task.goal),)
            limits = AutomationLimits(
                max_steps=1,
                max_llm_calls=0,
                max_tool_calls=1,
                max_messages=1,
                timeout_seconds=min(30, self._settings.automation_max_runtime_seconds),
            )
        elif strategy == "generated":
            generate = AutomationStep(
                id="generate",
                call="yuki.generate",
                arguments={
                    "instruction": self._instruction(task),
                    "context_profile": task.context.scene,
                    "max_characters": 4000,
                },
                save_as="result",
            )
            steps = (generate,)
            if delivery is not None:
                steps += (
                    self._delivery_step(
                        delivery,
                        "${result.text}",
                        step_id="deliver",
                        reply_state="${result.reply_state}",
                    ),
                )
            limits = AutomationLimits(
                agent_budget_managed=True,
                max_steps=len(steps),
                max_llm_calls=1,
                max_tool_calls=len(steps),
                max_messages=self._settings.automation_max_messages_per_run,
                timeout_seconds=min(120, self._settings.automation_max_runtime_seconds),
            )
        else:
            delivery_calls = int(delivery is not None)
            execute = AutomationStep(
                id="execute",
                call="yuki.agent",
                arguments={
                    "instruction": self._instruction(task),
                    "context_profile": task.context.scene,
                },
                save_as="result",
            )
            steps = (execute,)
            if delivery is not None:
                steps += (
                    self._delivery_step(
                        delivery,
                        "${result.text}",
                        step_id="deliver",
                        reply_state="${result.reply_state}",
                    ),
                )
            limits = AutomationLimits(
                agent_budget_managed=True,
                max_steps=len(steps),
                max_llm_calls=1,
                max_tool_calls=1 + delivery_calls,
                # Main Agent tools can deliver before the final reply step.
                max_messages=self._settings.automation_max_messages_per_run,
                timeout_seconds=self._settings.automation_max_runtime_seconds,
            )

        script = AutomationScript(
            version=1,
            name=task.name,
            timezone=timezone,
            schedule=task.trigger,
            context=task.context,
            steps=steps,
            limits=limits,
        )
        return ExecutionPlan(
            strategy=strategy,
            script=script,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _resolve_strategy(task: TaskSpec) -> str:
        if task.strategy is TaskStrategy.AUTO:
            return "agentic"
        return task.strategy.value

    @staticmethod
    def _resolve_delivery(
        delivery: TaskDelivery,
        provenance: CreationProvenance,
    ) -> Literal["self_private", "current_group"] | None:
        target = delivery.target
        if target == "none":
            return None
        if target == "auto":
            return "current_group" if provenance.current_group_id else "self_private"
        if target == "current_group" and provenance.current_group_id is None:
            raise ValueError("当前消息不是群聊，不能把任务结果投递到 current_group")
        return target

    @staticmethod
    def _delivery_step(
        target: Literal["self_private", "current_group"],
        text: str,
        *,
        step_id: str = "deliver",
        reply_state: str | None = None,
    ) -> AutomationStep:
        if target == "current_group":
            return AutomationStep(
                id=step_id,
                call="onebot.send_group_message",
                arguments={
                    "group_id": "$current_group_id",
                    "text": text,
                    **({"reply_state": reply_state} if reply_state is not None else {}),
                },
            )
        return AutomationStep(
            id=step_id,
            call="onebot.send_private_message",
            arguments={
                "user_id": "$creator_user_id",
                "text": text,
                **({"reply_state": reply_state} if reply_state is not None else {}),
            },
        )

    @staticmethod
    def _instruction(task: TaskSpec) -> str:
        payload = {
            "goal": task.goal,
            "constraints": task.constraints,
            "delivery": task.delivery.target,
            "rules": (
                "围绕目标自主选择已授权工具及调用顺序；目标要求后续提醒时可以创建或修改"
                "创建者自己的自动化。动态编号和价格必须在任务运行时查询；"
                "不要假装外部操作成功。最终只返回适合直接发给用户的简短结果。"
            ),
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:4000]
