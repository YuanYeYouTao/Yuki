"""Consume SnowLuma's native group-name notice without opening a chat turn."""

from __future__ import annotations

from nonebot import on_notice
from nonebot.adapters.onebot.v11 import Bot, NoticeEvent
from sqlalchemy import select

from yuki.container import get_container
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.db_models import PresenceModel, SpaceBindingModel
from yuki.identity.space_metadata import observe_space_name
from yuki.persistence.database import Database


def is_group_name_notice(event: NoticeEvent) -> bool:
    return event.notice_type == "notify" and event.model_dump().get("sub_type") == "group_name"


group_name_notice = on_notice(rule=is_group_name_notice, priority=10, block=False)


async def observe_group_name_notice(
    database: Database,
    registry: GatewayConnectionRegistry,
    bot: object,
    event: NoticeEvent,
) -> bool:
    """Map an original registered QQ connection and native group to its existing binding."""

    if not is_group_name_notice(event):
        return False
    fields = event.model_dump()
    group_id, name = fields.get("group_id"), fields.get("name_new")
    if not isinstance(group_id, (str, int)) or not isinstance(name, str):
        return False
    connection = registry.resolve_by_handle(bot).snapshot
    if (
        connection.platform != "qq"
        or connection.external_account_id != str(event.self_id)
        or connection.presence_id is None
    ):
        return False
    async with database.sessions() as session:
        binding_id = await session.scalar(
            select(SpaceBindingModel.id)
            .join(
                PresenceModel,
                (PresenceModel.id == connection.presence_id)
                & (PresenceModel.platform == SpaceBindingModel.platform)
                & (PresenceModel.external_account_id == connection.external_account_id),
            )
            .where(
                SpaceBindingModel.platform == connection.platform,
                SpaceBindingModel.external_space_id == str(group_id),
            )
        )
    if binding_id is None:
        return False
    return await observe_space_name(
        database, presence_id=connection.presence_id, space_binding_id=binding_id, name=name
    )


@group_name_notice.handle()
async def handle_group_name_notice(bot: Bot, event: NoticeEvent) -> None:
    container = get_container()
    await observe_group_name_notice(container.database, container.gateway_registry, bot, event)
