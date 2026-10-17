"""Real installation and canonical target approval for offline background turns."""

from yuki.plugin_host.notification_repository import PluginNotificationRepository
from yuki.plugin_host.repository import PluginInstallationRepository
from yuki_plugin_sdk.models import NotificationTarget


async def approve_background_plugin(database, *, plugin_id, bot_user_id, group_id, creator_user_id):
    installations = PluginInstallationRepository(database)
    await installations.upsert_discovered(
        plugin_id=plugin_id,
        name="Offline background fixture",
        version="1.0",
        plugin_api="1",
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
    from yuki.identity.canonical_repository import person_id_for, presence_id_for, space_id_for

    async with database.sessions() as session:
        person = await person_id_for(session, creator_user_id)
        space = await space_id_for(session, group_id)
        presence = await presence_id_for(session, bot_user_id)
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
