"""Long screenshot loops through the real Runner and the four real dialect adapters.

Failure cases were listed before the implementation in
docs/operations/computer-browser-sandbox-20261109.md (M1, M2, M4, M5, M7–M15).
Each dialect runs 30 tool rounds; every round's result carries a new
``source="tool"`` image (archived by the real result budgeter into the real
private artifact store), then one ``read_result`` of the oldest image.

Artifacts: ``test-results/computer-browser/media/<dialect>.json`` — every
request body (base64 shortened to hash+length), the replacement texts and the
per-request carried counts; ``no_image_input.json`` and
``workspace_and_oversized_<dialect>.json``; commit and worktree state recorded.
Exactly ``ROUNDS + 2`` requests also proves no compaction request was triggered
by replayed continuation pixels (M15).
"""

import base64
import hashlib
import io
import itertools
import json
import random
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from tests.conftest import build_harness, make_settings
from tests.support.agent_backend import StubAgentBackend
from tests.support.social_identity_cases import social_env

from yuki.capabilities.media import PreparedMediaData, result_images
from yuki.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.messages import ChatImage, ChatMessage, ChatTool
from yuki.llm.anthropic_messages import AnthropicMessagesProvider
from yuki.llm.fake import FakeLLMProvider
from yuki.llm.gemini import GeminiProvider
from yuki.llm.openai_compatible import OpenAICompatibleProvider
from yuki.llm.openai_responses import OpenAIResponsesProvider
from yuki.llm.request_media import MEDIA_NOT_CARRIED
from yuki.model_runtime.executor import TaskModelExecutor
from yuki.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
)
from yuki.model_runtime.pool import ModelClientPool
from yuki.model_runtime.profiles import ModelProfileCatalog
from yuki.model_runtime.routes import ModelRouter
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.work_repository import WorkCapacityError
from yuki.services.agent_runner import AgentRunner, AgentRuntime
from yuki.services.concurrency import ConcurrencyManager
from yuki.tool_results.access import ArtifactAccess
from yuki.tool_results.artifacts import ToolArtifactRepository
from yuki.workspace.store import WorkspaceError

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "test-results" / "computer-browser" / "media"
ROUNDS = 30
FRAMES = 16  # vision.max_frames_per_turn default; the existing limit, not a new one
MEDIA_BYTES = 16_777_216
_BASE64 = re.compile(r"^[A-Za-z0-9+/=]{200,}$")

DIALECTS = {
    "chat_completions": (OpenAICompatibleProvider, ModelProtocol.CHAT_COMPLETIONS),
    "chat_completions_reasoning": (OpenAICompatibleProvider, ModelProtocol.CHAT_COMPLETIONS),
    "anthropic_messages": (AnthropicMessagesProvider, ModelProtocol.ANTHROPIC_MESSAGES),
    "gemini": (GeminiProvider, ModelProtocol.GEMINI),
    "responses": (OpenAIResponsesProvider, ModelProtocol.RESPONSES),
}


def _revision() -> dict:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, cwd=ROOT).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], text=True, cwd=ROOT)
    return {"commit": head, "worktree_dirty": bool(dirty.strip())}


def _shorten(value):
    if isinstance(value, dict):
        return {key: _shorten(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_shorten(item) for item in value]
    if isinstance(value, str) and (value.startswith("data:image/") or _BASE64.match(value)):
        digest = hashlib.sha256(value.encode()).hexdigest()[:16]
        return f"<image sha256:{digest} len:{len(value)}>"
    return value


def _write(name: str, value: dict) -> None:
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    text = json.dumps({**_revision(), **value}, ensure_ascii=False, indent=1, sort_keys=True)
    (ARTIFACT / name).write_text(text + "\n", encoding="utf-8")


def screenshot(index: int) -> ChatImage:
    """A final-size JPEG like the environment produces (Host does not rescale it)."""
    noise = random.Random(index)
    image = Image.new("RGB", (640, 400), (index * 7 % 256, 90, 160))
    pixels = image.load()
    for _ in range(6000):
        x, y = noise.randrange(640), noise.randrange(400)
        pixels[x, y] = (noise.randrange(256), noise.randrange(256), noise.randrange(256))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    encoded = base64.b64encode(buffer.getvalue()).decode()
    return ChatImage(f"data:image/jpeg;base64,{encoded}", source="tool")


INBOUND = ChatImage(screenshot(999).data_url, source="current")


def wire_images(dialect: str, body: dict) -> list[str]:
    """Every image the body actually sends, read from the wire independently."""
    found = []
    if dialect.startswith("chat"):
        for message in body["messages"]:
            if isinstance(message.get("content"), list):
                found += [
                    part["image_url"]["url"]
                    for part in message["content"]
                    if part.get("type") == "image_url"
                ]
    elif dialect == "anthropic_messages":
        for message in body["messages"]:
            found += [
                f"data:{part['source']['media_type']};base64,{part['source']['data']}"
                for part in message["content"]
                if part.get("type") == "image"
            ]
    elif dialect == "gemini":
        for content in body["contents"]:
            found += [
                f"data:{part['inlineData']['mimeType']};base64,{part['inlineData']['data']}"
                for part in content["parts"]
                if "inlineData" in part
            ]
    else:
        for item in body["input"]:
            if isinstance(item.get("content"), list):
                found += [
                    part["image_url"]
                    for part in item["content"]
                    if part.get("type") == "input_image"
                ]
    return found


def wire_texts(body) -> list[str]:
    if isinstance(body, dict):
        return [text for value in body.values() for text in wire_texts(value)]
    if isinstance(body, list):
        return [text for value in body for text in wire_texts(value)]
    return [body] if isinstance(body, str) else []


def tool_call(dialect: str, call_id: str, name: str, arguments: dict, index: int) -> dict:
    if dialect.startswith("chat"):
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }
        if dialect == "chat_completions_reasoning":
            message["reasoning_details"] = [{"type": "reasoning.text", "text": f"step {index}"}]
        return {
            "id": f"chat-{index}",
            "choices": [{"message": message, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }
    if dialect == "anthropic_messages":
        return {
            "id": f"msg-{index}",
            "content": [{"type": "tool_use", "id": call_id, "name": name, "input": arguments}],
            "stop_reason": "tool_use",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }
    if dialect == "gemini":
        return {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"functionCall": {"name": name, "args": arguments, "id": call_id}}
                        ],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2},
        }
    return {
        "id": f"resp-{index}",
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "id": f"fc-{index}",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
                "status": "completed",
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
    }


def final_text(dialect: str) -> dict:
    if dialect.startswith("chat"):
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }
    if dialect == "anthropic_messages":
        return {
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }
    if dialect == "gemini":
        return {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "done"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2},
        }
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
    }


def executor(provider, protocol: ModelProtocol, *, image_input: bool = True) -> TaskModelExecutor:
    profile = ModelProfile(
        id="main",
        provider="fake",
        protocol=protocol,
        model="offline",
        timeout_seconds=5,
        max_retries=0,
        default_temperature=0.7,
        default_max_output_tokens=256,
        thinking_enabled=False,
        capabilities=frozenset(
            {ModelCapability.TOOLS} | ({ModelCapability.IMAGE_INPUT} if image_input else set())
        ),
    )
    catalog = ModelProfileCatalog(
        profiles={profile.id: profile},
        routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
    )
    return TaskModelExecutor(
        router=ModelRouter(catalog),
        pool=ModelClientPool(injected_profiles={"main": provider}),
    )


async def loop_env(database, tmp_path):
    env = await social_env(database, tmp_path / "identity")
    async with database.read_session() as reader:
        conversation = await reader.get(CanonicalConversationModel, env.context.conversation_id)
        generation = conversation.generation
    access = ArtifactAccess(env.context.conversation_id, generation, env.person)
    store = ToolArtifactRepository(
        database, tmp_path / "private-tool-images", retention_seconds=3600
    )
    harness = build_harness(database, make_settings(database.url), FakeLLMProvider(lambda _: ""))
    config = await harness.processor._chat._runtime_config.snapshot()
    assert config.vision.max_frames_per_turn == FRAMES
    budgeter = ToolResultBudgeter(max_characters=24000, artifacts=store, artifact_access=access)
    return SimpleNamespace(access=access, store=store, config=config, budgeter=budgeter)


class ScreenshotBackend(StubAgentBackend):
    """A sandbox-like tool: each click returns a new final-size screenshot (``source="tool"``)."""

    media_max_bytes = MEDIA_BYTES

    def __init__(self, env):
        self.env = env
        self.handles: dict[str, str] = {}
        self.originals: dict[str, ChatImage] = {}
        self.reread: list[dict] = []
        self.validated: list[int] = []
        self.validated_paths: list[list[str]] = []
        self.changed_paths: set[str] = set()

    def parallel_safe(self, name, runtime):
        return False

    def is_side_effecting(self, name, arguments, runtime):
        return name == "computer"

    def finalize(self, text, runtime):
        return text

    async def validate_images(self, images, runtime):
        self.validated.append(len(images))
        paths = [image.workspace_path for image in images if image.source == "workspace"]
        self.validated_paths.append(paths)
        if self.changed_paths.intersection(paths):
            # What the real workspace validator does for a changed file version.
            raise WorkspaceError("workspace_media_version_changed")
        await self.env.store.validate_media(
            tuple(image for image in images if image.source == "tool"), self.env.access
        )

    async def execute_call(self, invocation):
        call = invocation.call
        arguments = json.loads(call.function.arguments)
        if call.function.name == "computer":
            image = screenshot(int(arguments["step"]))
            self.originals[call.id] = image
            budgeted = await self.env.budgeter.render(
                ToolExecutionResult(
                    ok=True,
                    data={"action": "click", "x": 10, "y": 20, "width": 640, "height": 400},
                    images=(image,),
                    mutation_committed=True,
                    provider_id="sandbox",
                    tool_name="computer",
                )
            )
            assert budgeted.artifact_id
            self.handles[call.id] = budgeted.artifact_id
            return budgeted.text
        if call.function.name == "view_file":
            image = ChatImage(
                screenshot(500).data_url,
                source="workspace",
                workspace_path=arguments["path"],
                version="v1",
                content_hash="v1",
            )
            budgeted = await self.env.budgeter.render(
                ToolExecutionResult(
                    ok=True,
                    data={"path": arguments["path"], "version": "v1"},
                    images=(image,),
                    provider_id="core",
                    tool_name="view_file",
                )
            )
            assert budgeted.artifact_id is None
            return budgeted.text
        if call.function.name == "burst":
            # The old workspace file changes after its observation was replaced.
            self.changed_paths.add("shots/a.png")
            budgeted = await self.env.budgeter.render(
                ToolExecutionResult(
                    ok=True,
                    data={"action": "burst-click", "frames": FRAMES},
                    images=tuple(screenshot(600 + n) for n in range(FRAMES)),
                    mutation_committed=True,
                    provider_id="sandbox",
                    tool_name="computer",
                )
            )
            return budgeted.text
        prepared = await self.env.store.read(
            arguments["handle"], operation="image", access=self.env.access
        )
        assert isinstance(prepared, PreparedMediaData)
        budgeted = await self.env.budgeter.render(
            ToolExecutionResult(
                ok=True,
                data=dict(prepared),
                images=prepared.images,
                provider_id="artifacts",
                tool_name="read_result",
            )
        )
        images = result_images(budgeted.text)
        self.reread.append(
            {
                "artifact_id": budgeted.artifact_id,
                "handles": [image.tool_handle for image in images],
                "data": [image.data_url for image in images],
            }
        )
        return budgeted.text


TOOLS = (
    ChatTool("computer", "click and screenshot", {"type": "object"}),
    ChatTool("view_file", "view a workspace image", {"type": "object"}),
    ChatTool("burst", "click and return many frames", {"type": "object"}),
    ChatTool("read_result", "read an artifact", {"type": "object"}),
)


def runtime(config) -> AgentRuntime:
    return AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        actor_is_superuser=False,
        conversation_key="media-loop",
        current_group_id=None,
        bot_user_id="80001",
        gateway=None,
        runtime_config=config,
        allowed_capabilities=frozenset(),
        fixed_tools=TOOLS,
    )


@pytest.mark.parametrize("dialect", list(DIALECTS))
async def test_thirty_screenshots_replace_oldest_observations(database, tmp_path, dialect):
    env = await loop_env(database, tmp_path)
    backend = ScreenshotBackend(env)
    bodies: list[dict] = []

    def transport(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        index = len(bodies)
        if index <= ROUNDS:
            response = tool_call(dialect, f"shot-{index}", "computer", {"step": index}, index)
        elif index == ROUNDS + 1:
            response = tool_call(
                dialect, "reread-1", "read_result", {"handle": backend.handles["shot-1"]}, index
            )
        else:
            response = final_text(dialect)
        return httpx.Response(200, json=response)

    kind, protocol = DIALECTS[dialect]
    async with httpx.AsyncClient(
        base_url="https://offline.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        provider = kind(
            base_url="https://offline.invalid",
            api_key="synthetic",
            timeout_seconds=5,
            max_retries=0,
            client=client,
        )
        models = executor(provider, protocol)
        try:
            result = await AgentRunner(models, ConcurrencyManager()).run(
                (ChatMessage("user", "点三十次并看截图", images=(INBOUND,)),),
                runtime(env.config),
                backend,
            )
        finally:
            await models.close()

    # M5: no admission rejection at image 17; the loop finishes.
    assert result.text == "done"
    assert len(bodies) == ROUNDS + 2
    shots = [backend.originals[f"shot-{n}"].data_url for n in range(1, ROUNDS + 1)]
    report = []
    replaced_by_request: list[set[str]] = []
    wire_of_replaced: dict[str, str] = {}
    for number, body in enumerate(bodies, start=1):
        images = wire_images(dialect, body)
        texts = wire_texts(body)
        # M1: the newest screenshot of the previous round is in this request.
        if 1 < number <= ROUNDS + 1:
            assert images[-1] == shots[number - 2]
        # M12: the inbound image is never replaced.
        assert images.count(INBOUND.data_url) == 1
        assert len(images) <= FRAMES
        assert sum(len(url) for url in images) <= MEDIA_BYTES
        carried_shots = [shots.index(url) + 1 for url in images if url in shots]
        replaced = set()
        for observation in _observation_texts(texts):
            call_id = re.search(r"call_id=([\w-]+)；", observation).group(1)
            if MEDIA_NOT_CARRIED in _observation_wire(body, call_id):
                replaced.add(call_id)
                # M5: the replacement names the handle that read_result reads back.
                assert f"media_artifact_handle={backend.handles[call_id]}；" in observation
        replaced_by_request.append(replaced)
        sent_shots = number - 1 if number <= ROUNDS + 1 else ROUNDS
        expected_replaced = max(0, sent_shots - (FRAMES - 1))
        if number <= ROUNDS + 1:
            assert replaced == {f"shot-{n}" for n in range(1, expected_replaced + 1)}
            assert carried_shots == list(range(expected_replaced + 1, sent_shots + 1))
        report.append(
            {
                "request": number,
                "images_sent": len(images),
                "image_bytes": sum(len(url) for url in images),
                "carried_screenshots": carried_shots,
                "replaced_call_ids": sorted(replaced, key=lambda c: int(c.split("-")[1])),
            }
        )
        # M8: an observation once replaced stays replaced with identical wire.
        for call_id in replaced:
            wire = _observation_wire(body, call_id)
            if call_id in wire_of_replaced:
                assert wire == wire_of_replaced[call_id], (dialect, number, call_id)
            else:
                wire_of_replaced[call_id] = wire
    for earlier, later in itertools.pairwise(replaced_by_request):
        assert earlier <= later
    # M2/M5: read_result returns the replaced image under its original handle,
    # it is not archived again, and the re-read reaches the next request.
    assert backend.reread == [
        {
            "artifact_id": None,
            "handles": [backend.handles["shot-1"]],
            "data": [shots[0]],
        }
    ]
    assert wire_images(dialect, bodies[-1])[-1] == shots[0]
    # M10: only carried images are source-validated (inbound images have no tool source).
    assert backend.validated and max(backend.validated) <= FRAMES - 1
    replacement_texts = sorted(
        {
            text
            for body in bodies
            for text in wire_texts(body)
            if "call_id=shot-1；" in text or text == MEDIA_NOT_CARRIED
        }
    )
    _write(
        f"{dialect}.json",
        {
            "dialect": dialect,
            "frames_limit": FRAMES,
            "bytes_limit": MEDIA_BYTES,
            "requests": report,
            "replacement_texts": replacement_texts,
            "bodies": [_shorten(body) for body in bodies],
        },
    )


def _observation_texts(texts: list[str]) -> list[str]:
    return [text for text in texts if text.startswith("[Host 工具媒体观察：call_id=")]


def _observation_wire(body: dict, call_id: str) -> str:
    """The serialized wire message holding this call's observation."""
    marker = f"call_id={call_id}；"
    for container in ("messages", "contents", "input"):
        for message in body.get(container, ()):
            if marker in json.dumps(message, ensure_ascii=False) and (
                "工具媒体观察" in json.dumps(message, ensure_ascii=False)
            ):
                return json.dumps(message, ensure_ascii=False, sort_keys=True)
    raise AssertionError(call_id)


async def test_model_without_image_input_keeps_click_effect(database, tmp_path):
    env = await loop_env(database, tmp_path)
    backend = ScreenshotBackend(env)
    bodies: list[dict] = []

    def transport(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        index = len(bodies)
        return httpx.Response(
            200,
            json=tool_call("chat_completions", f"shot-{index}", "computer", {"step": index}, index)
            if index <= 2
            else final_text("chat_completions"),
        )

    async with httpx.AsyncClient(
        base_url="https://offline.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://offline.invalid",
            api_key="synthetic",
            timeout_seconds=5,
            max_retries=0,
            client=client,
        )
        models = executor(provider, ModelProtocol.CHAT_COMPLETIONS, image_input=False)
        try:
            result = await AgentRunner(models, ConcurrencyManager()).run(
                (ChatMessage("user", "点两次"),), runtime(env.config), backend
            )
        finally:
            await models.close()
    assert result.text == "done"
    # M4: no pixels reach the model; the committed click stays in the receipt.
    tool_results = [
        json.loads(message["content"])
        for message in bodies[-1]["messages"]
        if message["role"] == "tool"
    ]
    assert len(tool_results) == 2
    for receipt in tool_results:
        assert receipt["action"] == "click" and receipt["x"] == 10
        assert receipt["media_read"] is False
        assert receipt["media_error"] == "image_capability_unavailable"
    assert not any(wire_images("chat_completions", body) for body in bodies)
    assert not any(_observation_texts(wire_texts(body)) for body in bodies)
    _write(
        "no_image_input.json",
        {"dialect": "chat_completions", "tool_results": tool_results},
    )


async def test_device_view_file_archives_and_read_result_does_not(database, tmp_path):
    """M2 rule: no handle and not workspace/history → archived; a handle → never again."""
    env = await loop_env(database, tmp_path)
    device_image = ChatImage(screenshot(1).data_url, source="tool", content_hash="abc")
    workspace_image = ChatImage(
        screenshot(2).data_url, source="workspace", workspace_path="a.png", version="v"
    )
    archived = await env.budgeter.render(
        ToolExecutionResult(
            ok=True,
            data={"path": "C:\\a.png", "device_id": "pc"},
            images=(device_image,),
            provider_id="core",
            tool_name="view_file",
        )
    )
    handle = archived.artifact_id
    assert handle and json.loads(archived.text)["media_artifact_handle"] == handle
    assert [image.tool_handle for image in result_images(archived.text)] == [handle]
    workspace = await env.budgeter.render(
        ToolExecutionResult(
            ok=True,
            data={"path": "a.png"},
            images=(workspace_image,),
            provider_id="core",
            tool_name="view_file",
        )
    )
    assert workspace.artifact_id is None
    assert result_images(workspace.text) == (workspace_image,)
    reread = await env.store.read(handle, operation="image", access=env.access)
    again = await env.budgeter.render(
        ToolExecutionResult(
            ok=True,
            data=dict(reread),
            images=reread.images,
            provider_id="artifacts",
            tool_name="read_result",
        )
    )
    assert again.artifact_id is None
    assert [image.tool_handle for image in result_images(again.text)] == [handle]
    async with database.read_session() as reader:
        from yuki.persistence.models import ToolArtifactModel

        rows = (await reader.scalars(select(ToolArtifactModel.handle_id))).all()
    assert rows == [handle]


async def _run(env, backend, dialect, script, messages):
    """Run the real Runner over one real adapter; ``script(index)`` is each response."""
    bodies: list[dict] = []

    def transport(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=script(len(bodies)))

    kind, protocol = DIALECTS[dialect]
    async with httpx.AsyncClient(
        base_url="https://offline.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        provider = kind(
            base_url="https://offline.invalid",
            api_key="synthetic",
            timeout_seconds=5,
            max_retries=0,
            client=client,
        )
        models = executor(provider, protocol)
        try:
            result = await AgentRunner(models, ConcurrencyManager()).run(
                messages, runtime(env.config), backend
            )
        finally:
            await models.close()
    return result, bodies


@pytest.mark.parametrize("dialect", ["chat_completions", "responses"])
async def test_workspace_observation_and_oversized_result(database, tmp_path, dialect):
    """M10/M11/M13: a replaced workspace image is named by path and not rechecked;
    a single result that cannot fit beside the inbound image is unread, effect kept."""
    env = await loop_env(database, tmp_path)
    backend = ScreenshotBackend(env)
    shots = 16

    def script(index):
        if index == 1:
            return tool_call(dialect, "view-1", "view_file", {"path": "shots/a.png"}, index)
        if index <= shots + 1:
            step = index - 1
            return tool_call(dialect, f"shot-{step}", "computer", {"step": step}, index)
        if index == shots + 2:
            return tool_call(dialect, "burst-1", "burst", {}, index)
        return final_text(dialect)

    result, bodies = await _run(
        env,
        backend,
        dialect,
        script,
        (ChatMessage("user", "先看文件再点", images=(INBOUND,)),),
    )
    assert result.text == "done"
    assert len(bodies) == shots + 3
    last = bodies[-1]
    texts = wire_texts(last)
    view = next(text for text in _observation_texts(texts) if "call_id=view-1；" in text)
    assert "path=shots/a.png；" in view
    assert MEDIA_NOT_CARRIED in _observation_wire(last, "view-1")
    # The changed old file is never rechecked once it is no longer carried.
    assert backend.validated_paths[-1] == []
    assert any("shots/a.png" in paths for paths in backend.validated_paths)
    images = wire_images(dialect, last)
    assert len(images) == FRAMES and images.count(INBOUND.data_url) == 1
    burst = next(
        json.loads(text) for text in texts if text.startswith("{") and '"burst-click"' in text
    )
    assert burst["action"] == "burst-click" and burst["media_read"] is False
    assert burst["media_error"] == "media_request_budget_exceeded"
    assert not any("call_id=burst-1；" in text for text in texts)
    _write(
        f"workspace_and_oversized_{dialect}.json",
        {
            "dialect": dialect,
            "view_observation": view,
            "burst_receipt": burst,
            "validated_workspace_paths": backend.validated_paths,
            "last_body": _shorten(last),
        },
    )


async def test_inbound_images_alone_over_limit_are_refused(database, tmp_path):
    """M12: inbound images are never replaced; alone over the limit, admission refuses."""
    env = await loop_env(database, tmp_path)
    backend = ScreenshotBackend(env)
    inbound = tuple(
        ChatImage(screenshot(700 + n).data_url, source="current") for n in range(FRAMES + 1)
    )
    with pytest.raises(WorkCapacityError, match="media_request_budget_exceeded"):
        await _run(
            env,
            backend,
            "chat_completions",
            lambda index: final_text("chat_completions"),
            (ChatMessage("user", "看这些图", images=inbound),),
        )
