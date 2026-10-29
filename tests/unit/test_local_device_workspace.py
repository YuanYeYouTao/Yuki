"""Personal-device files reuse selected pixels, attachment authority and immutable sends."""

import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import select, update
from tests.support.canonical_identity import ensure_person
from tests.support.social_identity_cases import social_env
from tests.support.workspace_snapshots import artifact_ids

from yuki.capabilities.media import PreparedMediaData, result_images
from yuki.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.external_agents.local_device import LocalDeviceService
from yuki.persistence.models import ToolArtifactModel
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.social.models import SocialMessage, SocialTarget
from yuki.social.service import SocialContext, SocialService
from yuki.tool_results.access import ArtifactAccess
from yuki.tool_results.artifacts import ToolArtifactRepository
from yuki.workspace.inspect import WorkspaceInspector
from yuki.workspace.service import WorkspaceService
from yuki.workspace.store import WorkspaceError, WorkspaceStore


class DeviceFiles:
    actor = staticmethod(LocalDeviceService.actor)

    def __init__(self, path):
        self.path = path
        self.reads = []
        self.uploads = []
        self.prior = None
        self.queries = []

    @asynccontextmanager
    async def download(
        self, device, actor, path, *, request_id, expected_version=None, max_bytes=None
    ):
        content = self.path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        self.reads.append((device, actor, path, request_id, expected_version))
        if expected_version is not None and expected_version != digest:
            raise WorkspaceError("version_conflict")
        yield (
            self.path,
            {
                "sha256": digest,
                "version": "native-file-version",
                "size": len(content),
                "name": "original.png",
            },
        )

    async def upload_result(self, device, actor, destination, *, request_id):
        self.queries.append((device, actor, destination, request_id))
        return self.prior

    async def upload(self, device, actor, path, destination, *, request_id):
        self.uploads.append((device, actor, destination, request_id, path.read_bytes()))
        return {"path": destination, "device_id": device, "status": "succeeded"}


def workspace(tmp_path):
    original = tmp_path / "selected.png"
    Image.new("RGB", (8, 8), (42, 84, 126)).save(original)
    store = WorkspaceStore(tmp_path / "artifacts")
    devices = DeviceFiles(original)
    media = SimpleNamespace(_sources=AsyncMock(), authorized_path=AsyncMock())
    manager = SimpleNamespace(
        local_devices=devices,
        execute=AsyncMock(side_effect=AssertionError("local file must not be routed to Manager")),
    )
    service = WorkspaceService(
        store,
        sandbox=manager,
        visual_inspector=WorkspaceInspector(store, ImagePreprocessor()),
        conversation_media=media,
    )
    return service, devices, media


async def test_local_inspect_prepares_actual_pixels_with_original_device_and_hash(tmp_path):
    service, devices, _ = workspace(tmp_path)
    digest = hashlib.sha256(devices.path.read_bytes()).hexdigest()
    result = await service.execute(
        "workspace_inspect",
        {
            "device_id": "computer",
            "path": "C:\\pictures\\selected.png",
            "question": "查看原图",
            "expected_version": digest,
        },
        request_id="inspect-original",
        source={"actor_person_id": "source-person"},
    )
    assert isinstance(result, PreparedMediaData) and result.images
    assert result["device_id"] == "computer" and result["version"] == result["sha256"] == digest
    assert result.images[0].content_hash == digest
    assert devices.reads == [
        ("computer", "source-person", "C:\\pictures\\selected.png", "inspect-original", digest)
    ]
    assert list(service.store.root.glob("*.blob")) == []


async def test_local_inspect_does_not_invent_a_person_from_the_device_id(tmp_path):
    service, devices, _ = workspace(tmp_path)
    with pytest.raises(ValueError, match="local_device_person_source_required"):
        await service.execute(
            "workspace_inspect",
            {"device_id": "computer", "path": "selected.png", "question": "看图"},
            request_id="read",
            source=None,
        )
    assert devices.reads == []


async def test_local_png_uses_the_real_private_core_media_consumer_after_source_deletion(
    database, tmp_path
):
    env = await social_env(database, tmp_path / "identity")
    service, devices, _ = workspace(tmp_path)
    prepared = await service.execute(
        "workspace_inspect",
        {
            "device_id": "original-device",
            "path": "C:\\pictures\\selected.png",
            "question": "查看原图",
        },
        request_id="original-private-inspect",
        source={"actor_person_id": env.person},
    )
    assert isinstance(prepared, PreparedMediaData) and prepared.images
    async with database.read_session() as reader:
        conversation = await reader.get(CanonicalConversationModel, env.context.conversation_id)
        generation = conversation.generation
    access = ArtifactAccess(env.context.conversation_id, generation, env.person)
    private = ToolArtifactRepository(
        database, tmp_path / "private-tool-images", retention_seconds=60
    )
    budgeted = await ToolResultBudgeter(
        max_characters=24000,
        artifacts=private,
        artifact_access=access,
    ).render(
        ToolExecutionResult(
            ok=True,
            data=dict(prepared),
            images=prepared.images,
            provider_id="core",
            tool_name="workspace_inspect",
        )
    )
    handle = budgeted.artifact_id
    assert handle and json.loads(budgeted.text)["media_artifact_handle"] == handle
    observed = result_images(budgeted.text)
    assert observed and all(
        image.source == "tool" and image.tool_handle == handle for image in observed
    )
    assert prepared.images[0].data_url not in budgeted.text
    assert prepared["device_id"] == "original-device"
    devices.path.unlink()
    recovered = await private.read(handle, operation="image", access=access)
    assert isinstance(recovered, PreparedMediaData)
    assert recovered.images[0].data_url == prepared.images[0].data_url
    assert recovered.images[0].content_hash == prepared["sha256"]
    await private.validate_media(observed, access)
    assert artifact_ids(service.store) == []
    assert not list(service.store.root.glob("*.blob"))
    async with database.read_session() as reader:
        row = await reader.scalar(
            select(ToolArtifactModel).where(ToolArtifactModel.handle_id == handle)
        )
        assert row.provider_id == "core" and row.tool_name == "workspace_inspect"
        assert row.media_type == "application/x-yuki-prepared-images" and row.work_id is None
        assert json.loads(row.access_json)["actor_person_id"] == env.person
    async with database.immediate_session() as writer:
        other = await ensure_person(writer, "another-private-reader")
    denied = await private.read(
        handle, operation="image", access=ArtifactAccess(access.conversation_id, generation, other)
    )
    assert denied["error_code"] == "artifact_not_authorized"
    assert not result_images(denied)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == access.conversation_id)
            .values(generation=generation + 1)
        )
    for reader_access in (
        access,
        ArtifactAccess(access.conversation_id, generation + 1, env.person),
    ):
        denied = await private.read(handle, operation="image", access=reader_access)
        assert denied["error_code"] == "artifact_not_authorized"
        assert not result_images(denied)


async def test_attachment_save_replays_original_receipt_after_cache_expiry(tmp_path):
    service, devices, media = workspace(tmp_path)
    devices.prior = {"device_id": "computer", "path": "C:\\saved.png", "status": "succeeded"}
    media.authorized_path.side_effect = AssertionError(
        "confirmed upload must not fetch expired source"
    )
    result = await service.execute(
        "save_conversation_attachment_to_workspace",
        {
            "event_id": 41,
            "attachment_index": 2,
            "destination": "C:\\saved.png",
            "device_id": "computer",
        },
        request_id="original-save",
        source={"actor_person_id": "person"},
    )
    assert result == devices.prior and devices.uploads == []
    media._sources.assert_awaited_once_with(((41, 2),))
    assert devices.queries == [("computer", "person", "C:\\saved.png", "original-save")]


async def test_attachment_save_uploads_authorized_original_bytes(tmp_path):
    service, devices, media = workspace(tmp_path)
    media.authorized_path.return_value = (SimpleNamespace(), devices.path)
    result = await service.execute(
        "save_conversation_attachment_to_workspace",
        {
            "event_id": 41,
            "attachment_index": 2,
            "destination": "/home/person/saved.png",
            "device_id": "computer",
        },
        request_id="save-once",
        source={"actor_person_id": "person"},
    )
    assert result["status"] == "succeeded"
    assert devices.uploads == [
        ("computer", "person", "/home/person/saved.png", "save-once", devices.path.read_bytes())
    ]
    media.authorized_path.assert_awaited_once_with(event_id=41, attachment_index=2)


async def test_local_send_freezes_once_and_replays_after_original_is_deleted(tmp_path):
    service, devices, _ = workspace(tmp_path)
    content = devices.path.read_bytes()
    identity = str(uuid4())
    arguments = dict(
        path="C:\\pictures\\selected.png",
        request_id="original-publish",
        artifact_id=identity,
        source_key="send:original-source",
        device_id="computer",
        actor_person_id="person",
        name="sent.png",
    )
    published = await service.publish_for_send(**arguments)
    devices.path.unlink()
    replay = await service.publish_for_send(**arguments)
    assert replay == published and len(devices.reads) == 1
    assert service.store.read_bytes(identity)[1] == content
    assert published["device_id"] == "computer" and published["immutable"] is True
    with pytest.raises(WorkspaceError, match="snapshot_identity_conflict"):
        await service.publish_for_send(**{**arguments, "source_key": "different-send"})
    assert len(devices.reads) == 1


async def test_send_path_carries_only_the_trusted_context_person_to_the_file_owner():
    publish = AsyncMock(return_value={"artifact_id": str(uuid4())})
    service = object.__new__(SocialService)
    service.workspace_service = SimpleNamespace(publish_for_send=publish)
    context = SocialContext(
        "original-turn",
        "original-call",
        "original-conversation",
        actor=SimpleNamespace(person_id="real-source-person"),
    )
    message = SocialMessage(
        path="C:\\original.txt", device_id="original-device", attachment_kind="file"
    )
    args = message.model_dump(mode="json", exclude_none=True)
    await service._path_artifact(
        message, args, context, SocialTarget(kind="person", id=uuid4()), None
    )
    assert publish.await_args.kwargs["device_id"] == "original-device"
    assert publish.await_args.kwargs["actor_person_id"] == "real-source-person"
    assert publish.await_args.kwargs["path"] == "C:\\original.txt"
