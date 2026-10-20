"""Real installation and canonical target approval for offline background turns."""

from yuki.plugin_host.notification_repository import PluginNotificationRepository
from yuki.plugin_host.repository import PluginInstallationRepository
from yuki_plugin_sdk import PLUGIN_API_VERSION
from yuki_plugin_sdk.models import NotificationTarget


async def approve_background_plugin(database, *, plugin_id, bot_user_id, group_id, creator_user_id):
    installations = PluginInstallationRepository(database)
    await installations.upsert_discovered(
        plugin_id=plugin_id,
        name="Offline background fixture",
        version="1.0",
        plugin_api=PLUGIN_API_VERSION,
        yuki_requires=">=3",
        entrypoint="plugin:Plugin",
        requested_permissions=(),
        manifest_hash="a" * 64,
    )
    await installations.approve(plugin_id)
    await installations.set_enabled(plugin_id, enabled=True)
    await installations.set_status(plugin_id, status="running")
    notifications = PluginNotificationRepository(database)
    from yuki.conversation.hydrate import conversation_for_scope
    from yuki.domain.conversations import ConversationScope
    from yuki.identity.canonical_repository import (
        find_identity_binding,
        find_presence,
        find_space_binding,
    )

    async with database.sessions() as session:
        creator = await find_identity_binding(session, creator_user_id)
        assert creator is not None
        person = creator.person_id
        binding = await find_space_binding(session, group_id)
        assert binding is not None
        space = binding.space_id
        bot = await find_presence(session, bot_user_id)
        assert bot is not None
        presence = bot.id
        conversation = await conversation_for_scope(
            session, ConversationScope.group(bot_user_id, group_id)
        )
        assert person is not None and space is not None and conversation is not None
    await notifications.grant_target(
        plugin_id=plugin_id,
        target=NotificationTarget(
            target_type="group", target_id=space, conversation_id=conversation.id
        ),
        bot_user_id=bot_user_id,
        created_by_user_id=person,
        presence_id=presence,
    )
    return notifications
