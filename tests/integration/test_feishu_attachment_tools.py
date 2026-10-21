"""Feishu media tools use the original app and reuse its admitted cached bytes."""

import hashlib
import io
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from tests.support.event_seed import seed_scoped_event

from yuki.conversation.media_service import ConversationMediaService
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.providers.feishu import FeishuClient, encode_account_id
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.canonical_repository import (
    create_person_binding,
    create_presence,
    create_space_binding,
)
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.services.agent_tools import ToolRuntime
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.media_resolver import MediaResolver
from yuki.workspace.service import WorkspaceService
from yuki.workspace.store import WorkspaceError, WorkspaceStore


@pytest.mark.parametrize(
    "tool", ["inspect_conversation_attachment", "save_conversation_attachment_to_workspace"]
)
@pytest.mark.parametrize("cached,offline", [(False, False), (True, True), (False, True)])
async def test_feishu_tools_resolve_original_app_and_cached_media(
    database, tmp_path, tool, cached, offline
):
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(output, "PNG")
    picture = output.getvalue()
    downloads = []

    def respond(request):
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            app = json.loads(request.content)["app_id"]
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "token-" + app, "expire": 7200}
            )
        assert request.url.path == "/open-apis/im/v1/messages/om_original/resources/img_original"
        assert request.url.params["type"] == "image"
        downloads.append(request.headers["Authorization"])
        assert downloads[-1] == "Bearer token-cli_a"
        return httpx.Response(200, content=picture, headers={"content-type": "image/png"})

    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        original = FeishuClient("cli_a", "fixture-secret-a", client=http)
        current = FeishuClient("cli_b", "fixture-secret-b", client=http)
        original.self_id = encode_account_id("cli_a", "ou_bot")
        current.self_id = encode_account_id("cli_b", "ou_bot")
        author = encode_account_id("cli_a", "ou_author")
        reader = encode_account_id("cli_b", "ou_reader")
        async with database.immediate_session() as writer:
            await create_space_binding(
                writer,
                external_space_id="oc_group",
                name="Group",
                enabled=True,
                autonomous_enabled=True,
                require_mention=False,
                platform="feishu",
                now=datetime.now(UTC),
            )
            for account in (author, reader):
                await create_person_binding(
                    writer,
                    external_account_id=account,
                    display_name=account,
                    platform="feishu",
                    now=datetime.now(UTC),
                )
            first = await create_presence(
                writer,
                external_account_id=original.self_id,
                platform="feishu",
                now=datetime.now(UTC),
            )
            second = await create_presence(
                writer,
                external_account_id=current.self_id,
                platform="feishu",
                now=datetime.now(UTC),
            )
            first_id, second_id = first.id, second.id
        registry.connect(original, provider_id="feishu", presence_id=first_id)
        registry.connect(current, provider_id="feishu", presence_id=second_id)
        events = ScopedEventLedgerUnitOfWork(database)
        source = (
            await seed_scoped_event(
                events,
                scope=ConversationScope.group(
                    original.self_id,
                    "oc_group",
                    platform="feishu",
                    presence_id=first_id,
                ),
                sender_user_id=author,
                platform_message_id="om_original",
                direction="inbound",
                content="原图",
                segments=(
                    {
                        "type": "image",
                        "data": {
                            "file": json.dumps(
                                {
                                    "message_id": "om_original",
                                    "resource_key": "img_original",
                                    "type": "image",
                                }
                            )
                        },
                    },
                ),
            )
        ).event
        request = (
            await seed_scoped_event(
                events,
                scope=ConversationScope.group(
                    current.self_id,
                    "oc_group",
                    platform="feishu",
                    presence_id=second_id,
                ),
                sender_user_id=reader,
                platform_message_id="om_request",
                direction="inbound",
                content="查看原图",
            )
        ).event
        assert source.canonical_conversation_id == request.canonical_conversation_id
        runtime = ToolRuntime(
            inbound=InboundMessage(
                message_id="om_request",
                event_type="message:feishu",
                platform="feishu",
                bot_user_id=current.self_id,
                scope_type=ScopeType.GROUP,
                group_id="oc_group",
                sender=SenderIdentity(reader),
                text="查看原图",
                source_event_id=request.id,
                conversation_id=request.canonical_conversation_id,
                presence_id=second_id,
                person_id=request.author_person_id,
            )
        )
        resolver = MediaResolver(gateway=current)
        media = ConversationMediaService(
            database,
            tmp_path / "cache",
            resolver,
            ImagePreprocessor(),
            gateway_registry=registry,
        )
        store = WorkspaceStore(tmp_path / "artifacts")
        workspace = WorkspaceService(store)
        workspace.conversation_media = media
        checkouts = []

        async def checkout(method, args, *, request_id):
            assert method == "workspace_checkout"
            metadata, content = store.read_bytes(args["artifact_id"])
            path = tmp_path / metadata["name"]
            path.write_bytes(content)
            checkouts.append(path)
            return {"path": "/workspace/" + metadata["name"], "version": metadata["sha256"]}

        workspace.sandbox = SimpleNamespace(execute=checkout)
        try:
            if cached:
                await media.authorized_path(event_id=source.id, attachment_index=0)
                downloads.clear()
            if offline:
                registry.disconnect(original)
            arguments = {
                "event_id": source.id,
                "attachment_index": 0,
                "question": "查看原图",
                "destination": "original.png",
            }
            if offline and not cached:
                with pytest.raises(WorkspaceError, match="resource_unavailable"):
                    await workspace.execute(tool, arguments, runtime=runtime, request_id=tool)
                assert downloads == checkouts == []
                return
            result = await workspace.execute(tool, arguments, runtime=runtime, request_id=tool)
            assert downloads == ([] if cached else ["Bearer token-cli_a"])
            assert "img_original" not in str(result) and "fixture-secret" not in str(result)
            if tool == "inspect_conversation_attachment":
                assert result.images and result["sha256"] == hashlib.sha256(picture).hexdigest()
            else:
                assert result["file_imported"] is True
                assert checkouts[0].read_bytes() == picture
                assert store.read_bytes(result["artifact_id"])[1] == picture
        finally:
            await media.close()
            await resolver.close()
