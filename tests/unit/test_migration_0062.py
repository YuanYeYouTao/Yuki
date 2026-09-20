"""The social frequency-cap migration removes only retired overrides."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def test_remove_social_rate_limit_overrides(monkeypatch) -> None:
    migration = importlib.import_module("migrations.versions.0062_remove_social_rate_limits")
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE runtime_config_overrides (config_key TEXT NOT NULL)")
        )
        keys = (
            "social.send_per_target_per_minute",
            "social.send_global_per_minute",
            "social.poke_per_target_per_minute",
            "social.poke_global_per_minute",
            "social.other_setting",
        )
        for key in keys:
            connection.execute(
                text("INSERT INTO runtime_config_overrides (config_key) VALUES (:key)"),
                {"key": key},
            )
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        remaining = connection.execute(
            text("SELECT config_key FROM runtime_config_overrides")
        ).scalars().all()
    assert remaining == ["social.other_setting"]
