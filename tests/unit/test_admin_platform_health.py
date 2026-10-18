"""A live personal entry does not claim that a social platform is connected."""

from tests.unit.test_group_route_recovery import stack

from yuki.application.modules.admin_api import runtime_health


async def test_personal_gateway_health_keeps_qq_telegram_and_environment_facts_separate(database):
    app, bot, *_ = await stack(database)
    try:
        await app.app_gateway.start()
        app.gateway_registry.disconnect(bot)
        rows = {row["name"]: row for row in await runtime_health(app)}
        assert rows["app"]["running"]
        assert not rows["qq"]["running"]
        assert rows["qq"]["healthy"] is False
        assert not rows["telegram"]["enabled"]
        assert not rows["telegram"]["running"]
        assert rows["persistent_environment"]["healthy"] is None
        assert rows["persistent_environment"]["running"] is None
        assert "completion_receiver_running" in rows["persistent_environment"]
        assert all(row["checked_at"] is not None for row in rows.values())
    finally:
        await app.close()
