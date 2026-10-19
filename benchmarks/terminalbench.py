"""Harbor 0.1.44 adapter for the real Yuki runtime."""

from __future__ import annotations

import os

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from benchmarks.config import load_settings
from benchmarks.environment import HarborEnvironment
from benchmarks.runner import run_task
from yuki import __version__


class YukiAgent(BaseAgent):
    """Keep Yuki's loop on the host and its terminal in the Harbor task container."""

    @staticmethod
    def name() -> str:
        return "yuki"

    def version(self) -> str:
        return __version__

    async def setup(self, environment: BaseEnvironment) -> None:
        # The shared runner owns environment setup and cleanup.
        pass

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        output_dir = self.logs_dir / "yuki"
        settings = load_settings(os.environ["YUKI_BENCH_CONFIG"], output_dir)
        if self.model_name != settings.llm_model:
            raise ValueError("Harbor --model must match the benchmark config's llm_model exactly")

        result = await run_task(
            instruction,
            HarborEnvironment(environment),
            settings,
            output_dir,
            task_id=environment.environment_name,
            timeout_seconds=None,
        )
        usage = result["usage"]
        complete_usage = usage["unknown_usage_request_count"] == 0
        context.n_input_tokens = usage["input_tokens"] if complete_usage else None
        context.n_output_tokens = usage["output_tokens"] if complete_usage else None
        context.n_cache_tokens = usage["cached_input_tokens"] if complete_usage else None
        context.metadata = {
            **(context.metadata or {}),
            "yuki": {
                "task_id": result["task_id"],
                "status": result["status"],
                "work_id": result["work_id"],
                "git_commit": result["git_commit"],
                "model_name": settings.llm_model,
                "usage": usage,
            },
        }
