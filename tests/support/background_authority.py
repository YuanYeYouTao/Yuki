"""Real installation and canonical target approval for offline background turns."""

from qq_ai_bot.plugin_host.notification_repository import PluginNotificationRepository
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
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
    await notifications.grant_target(
        plugin_id=plugin_id,
        target=NotificationTarget(target_type="group", target_id=group_id),
        bot_user_id=bot_user_id,
        created_by_user_id=creator_user_id,
    )
    return notifications
