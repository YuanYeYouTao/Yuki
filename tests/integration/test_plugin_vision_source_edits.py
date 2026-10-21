"""Public plugin Vision caches observations for the actual native source image."""

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.support.native_media_cases import (
    add_photo,
    drain_native_turns,
    source_record,
    telegram_update,
)
from tests.support.native_media_cases import native_media_env as native_media_env

from yuki.automation.models import TurnOrigin
from yuki.gateway.providers.telegram_ingress import normalize_message
from yuki.persistence.media_repository import MediaAnalysisRepository
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices, PluginInvocation
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.media_resolver import MediaResolver
from yuki.services.vision_rate_limit import VisionRateLimiter
from yuki.services.vision_service import VisionService
from yuki.vision.fake import FakeVisionProvider
from yuki.vision.models import VisualObservation
from yuki_plugin_sdk.permissions import PluginPermission


@pytest.fixture
async def plugin_vision(database, native_media_env):
    env = native_media_env
    provider = FakeVisionProvider(
        lambda inputs, question: VisualObservation(
            items=(),
            overall_description=inputs[0].media_hash,
            provider="fake",
            model="fake-vision",
        )
    )
    resolver = MediaResolver()
    service = VisionService(
        provider=provider,
        resolver=resolver,
        preprocessor=ImagePreprocessor(),
        analyses=MediaAnalysisRepository(database),
        rate_limiter=VisionRateLimiter(),
    )
    context = HostPluginContext(
        plugin_id="vision-source-test",
        approved_permissions=(PluginPermission.VISION_ANALYZE,),
        services=PluginFacadeServices(vision=service),
    )

    async def analyze(raw, event):
        runtime = await env.harness.processor._runtime_config.snapshot(
            user_id=event.author_person_id, group_id=None
        )
        inbound = replace(
            normalize_message(raw, env.gateway.client),
            source_event_id=event.id,
            person_id=event.author_person_id,
            conversation_id=event.canonical_conversation_id,
            presence_id=event.ingress_presence_id,
            legacy_conversation_key=event.scope.key,
        )
        with context.bind(
            PluginInvocation(
                plugin_id="vision-source-test",
                origin=TurnOrigin.USER_MESSAGE,
                actor_user_id="42",
                bot_user_id=env.gateway.client.self_id,
                inbound=inbound,
                runtime_config=runtime,
                gateway=env.media_gateway,
            )
        ):
            result = await context.vision.analyze_current_media("describe the current image")
        assert result.ok, result
        return result.data["observation"]["overall_description"]

    try:
        yield SimpleNamespace(analyze=analyze, provider=provider)
    finally:
        await context.close_host_resources()
        await service.close()
        await resolver.close()


async def test_native_photo_replacement_invalidates_the_public_vision_source_cache(
    database, native_media_env, plugin_vision
):
    env = native_media_env
    original = telegram_update()
    add_photo(original["message"])
    await env.gateway.receive(original)
    await drain_native_turns(env)
    before = await source_record(database)
    first = await plugin_vision.analyze(original["message"], before)
    old_hash = hashlib.sha256(env.contents["old-photo"]).hexdigest()
    new_hash = hashlib.sha256(env.contents["new-photo"]).hexdigest()
    assert first == old_hash and old_hash != new_hash

    edited = telegram_update(2, edit=True)
    add_photo(edited["edited_message"], "new-photo")
    await env.gateway.receive(edited)
    after = await source_record(database)
    assert after.id == before.id
    assert after.author_person_id == before.author_person_id
    assert after.canonical_conversation_id == before.canonical_conversation_id
    assert after.segments != before.segments
    second = await plugin_vision.analyze(edited["edited_message"], after)

    assert second == new_hash
    assert first == old_hash  # The prior paid observation remains an immutable old result.
    assert len(plugin_vision.provider.requests) == 2
    assert env.downloads == ["old-photo", "new-photo"]


@pytest.mark.parametrize("edit_caption", [False, True], ids=["unchanged", "caption-only"])
async def test_same_original_reuses_public_vision_without_another_purchase(
    database, native_media_env, plugin_vision, edit_caption
):
    env = native_media_env
    original = telegram_update()
    add_photo(original["message"])
    await env.gateway.receive(original)
    await drain_native_turns(env)
    source = await source_record(database)
    first = await plugin_vision.analyze(original["message"], source)
    current = original["message"]
    if edit_caption:
        edited = telegram_update(2, edit=True, text="caption changed; same original")
        add_photo(edited["edited_message"])
        await env.gateway.receive(edited)
        current = edited["edited_message"]
        after = await source_record(database)
        assert after.id == source.id and after.author_person_id == source.author_person_id
        source = after
    second = await plugin_vision.analyze(current, source)
    assert first == second == hashlib.sha256(env.contents["old-photo"]).hexdigest()
    assert len(plugin_vision.provider.requests) == 1
    assert env.downloads and set(env.downloads) == {"old-photo"}


async def test_distinct_native_events_reuse_the_same_content_hash_observation(
    database, native_media_env, plugin_vision
):
    env = native_media_env
    results, events = [], []
    for identifier in (1, 2):
        raw = telegram_update(identifier)
        add_photo(raw["message"])
        await env.gateway.receive(raw)
        await drain_native_turns(env)
        source = await source_record(database, f"42:{identifier}")
        events.append(source.id)
        results.append(await plugin_vision.analyze(raw["message"], source))
    assert events[0] != events[1]
    assert results == [hashlib.sha256(env.contents["old-photo"]).hexdigest()] * 2
    assert len(plugin_vision.provider.requests) == 1
    assert env.downloads == ["old-photo", "old-photo"]
