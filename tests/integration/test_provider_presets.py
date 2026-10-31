"""The admin snapshot, deployment CLI and initial wizard share provider metadata."""

import asyncio
import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from tests.unit.test_admin_api import TOKEN
from tests.unit.test_admin_api import admin as admin

from yuki.admin_api.http import attach_admin_api
from yuki.deployment_setup.command import _page_basic, _SetupDraft
from yuki.deployment_setup.service import SetupPaths
from yuki.deployment_setup.terminal import TerminalUI
from yuki.model_runtime.profiles import _DEFAULT_REQUIREMENTS


async def test_provider_metadata_matches_cli_without_mutating_configuration(admin, tmp_path):
    _, api, _, _, profiles = admin
    before = profiles.read_bytes()
    app = FastAPI()
    attach_admin_api(app, lambda: api)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    with config.bind_socket() as listener:
        server = uvicorn.Server(config)
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if serving.done():
                    await serving
                    raise AssertionError("isolated HTTP server exited before startup")
                await asyncio.sleep(0.01)
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(
                    f"http://127.0.0.1:{listener.getsockname()[1]}/admin/v1/models",
                    headers={"Authorization": f"Bearer {TOKEN}"},
                )
            assert response.status_code == 200
            snapshot = response.json()["data"]
            presets = snapshot["provider_presets"]
            assert presets
            route_requirements = {
                task.value: sorted(capability.value for capability in required)
                for task, required in _DEFAULT_REQUIREMENTS.items()
            }
            assert {
                task: snapshot["task_requirements"][task] for task in route_requirements
            } == route_requirements
            assert snapshot["task_requirements"]["vision"] == ["image_input"]
            assert set(snapshot["task_requirements"]) == set(route_requirements) | {
                "embedding",
                "vision",
                "asr",
                "search",
            }
            qwen = next(item for item in presets if item["provider"] == "qwen")
            assert qwen["default_base_url"].endswith("/compatible-mode/v1")
            assert qwen["embedding_defaults"] == {
                "provider": "qwen_dashscope",
                "base_url": "https://dashscope.aliyuncs.com/api/v1",
            }
            assert qwen["embedding_provider"] == "qwen_dashscope"
            assert all(
                item["supports_embedding"] == (item["embedding_provider"] is not None)
                for item in presets
            )
            environment = {**os.environ, "PYTHONUTF8": "1"}
            exported = await asyncio.to_thread(
                subprocess.run,
                [
                    sys.executable,
                    "-m",
                    "yuki.cli",
                    "setup",
                    "provider-presets",
                    "--json",
                    "--deployment-root",
                    str(tmp_path / "not-a-deployment"),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
            )
            assert exported.returncode == 0, exported.stderr
            assert json.loads(exported.stdout) == presets
            assert profiles.read_bytes() == before
            assert not (tmp_path / "not-a-deployment").exists()
            assert "provider_presets" not in snapshot["document"]
            artifact = Path("test-results/provider-presets")
            await asyncio.to_thread(artifact.mkdir, parents=True, exist_ok=True)
            (artifact / "metadata.json").write_text(
                json.dumps(
                    {
                        "http_status": response.status_code,
                        "provider_presets": presets,
                        "task_requirements": snapshot["task_requirements"],
                        "cli_equal": True,
                        "configuration_unchanged": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        finally:
            server.should_exit = True
            await serving


def test_initial_wizard_prefills_selected_provider_address(tmp_path):
    answers = iter(("chat_completions", "qwen", "", "explicit-model-id"))
    prompts = StringIO()

    def answer(prompt):
        if "Base URL" in prompt:
            assert "https://dashscope.aliyuncs.com/compatible-mode/v1" in prompt
        return next(answers)

    ui = TerminalUI(
        no_color=True,
        input_fn=answer,
        secret_fn=lambda _prompt: "synthetic-provider-key",
        output=prompts,
    )
    draft = _SetupDraft({}, "chat_completions", False, initial=True)
    _page_basic(SetupPaths(tmp_path), ui, draft)
    assert draft.environment["LLM_PROVIDER"] == "qwen"
    assert draft.environment["LLM_BASE_URL"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert draft.environment["LLM_MODEL"] == "explicit-model-id"
    assert "synthetic-provider-key" not in prompts.getvalue()


def test_provider_presets_cli_runs_without_a_deployment(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "yuki.cli",
            "setup",
            "provider-presets",
            "--json",
            "--deployment-root",
            str(tmp_path / "missing"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert any(preset["provider"] == "qwen" for preset in json.loads(result.stdout))
    assert not (tmp_path / "missing").exists()
