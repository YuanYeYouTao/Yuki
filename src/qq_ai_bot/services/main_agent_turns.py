"""Shared preparation and execution boundary for composed Yuki turns."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, InboundMessage
from qq_ai_bot.services.agent_runner import (
    AgentRunner,
    AgentRunResult,
    AgentRuntime,
    AgentToolBackend,
)
from qq_ai_bot.services.context_assembler import AssembledContext
from qq_ai_bot.services.prompt_composer import PromptComposer, PromptComposition
from qq_ai_bot.vision.models import VisualObservation


class MainAgentTurnService:
    """Capture turn state before compilation; never refresh a submitted input."""

    def __init__(self, composer: PromptComposer, runner: AgentRunner) -> None:
        self._composer = composer
        self._runner = runner

    async def compose(
        self,
        *,
        inbound: InboundMessage | None,
        context: AssembledContext,
        runtime: RuntimeConfigSnapshot,
        visual_observation: VisualObservation | None,
        visual_failure: bool,
        scope_type: ScopeType | None = None,
        include_plugin_context: bool = True,
    ) -> PromptComposition:
        contract = self._runner.main_contract
        state = await asyncio.to_thread(contract.state.snapshot) if contract else None
        return self._composer.compose(
            inbound=inbound,
            context=context,
            runtime=runtime,
            visual_observation=visual_observation,
            visual_failure=visual_failure,
            scope_type=scope_type,
            include_plugin_context=include_plugin_context,
            short_state=state,
        )

    async def run(
        self,
        messages: tuple[ChatMessage, ...],
        runtime: AgentRuntime,
        backend: AgentToolBackend | None,
    ) -> AgentRunResult:
        return await self._runner.run(
            messages, replace(runtime, dynamic_context_prepared=True), backend
        )
