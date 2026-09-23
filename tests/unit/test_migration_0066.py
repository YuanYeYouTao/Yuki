"""0066 adds dormant storage and keeps accepted records on code rollback."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from qq_ai_bot.conversation.autonomy_db_models import (
    AutonomyBindingModel,
    InitiativeFeedbackModel,
    InitiativeRunModel,
    InitiativeSourceClaimModel,
)


def _schema(connection, name):
    inspector = inspect(connection)
    return {
        "columns": [
            (column["name"], str(column["type"]), column["nullable"], column["default"])
            for column in inspector.get_columns(name)
        ],
        "primary_key": inspector.get_pk_constraint(name)["constrained_columns"],
        "foreign_keys": sorted(
            (
                tuple(key["constrained_columns"]),
                key["referred_table"],
                tuple(key["referred_columns"]),
                key["options"].get("ondelete"),
            )
            for key in inspector.get_foreign_keys(name)
        ),
        "unique": sorted(
            tuple(key["column_names"]) for key in inspector.get_unique_constraints(name)
        ),
        "checks": sorted(
            " ".join(check["sqltext"].split()) for check in inspector.get_check_constraints(name)
        ),
        "indexes": sorted(
            (
                index["name"],
                tuple(index["column_names"]),
                index["unique"],
                str(index.get("dialect_options", {}).get("sqlite_where", "")),
            )
            for index in inspector.get_indexes(name)
        ),
    }


def test_autonomy_migration_is_additive_and_preserves_records(monkeypatch):
    migration = importlib.import_module("migrations.versions.0066_autonomy_admission")
    assert migration.down_revision == "0065"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        for name in ("canonical_conversations", "spaces", "presences", "persons"):
            connection.execute(text(f"CREATE TABLE {name} (id TEXT PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE existing_evidence (value TEXT)"))
        connection.execute(text("INSERT INTO existing_evidence VALUES ('preserve')"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        expected = {
            "autonomy_bindings",
            "autonomy_initiative_runs",
            "autonomy_source_claims",
            "autonomy_initiative_feedback",
        }
        assert expected <= set(inspect(connection).get_table_names())
        assert connection.scalar(text("SELECT count(*) FROM autonomy_bindings")) == 0
        connection.execute(
            text("""INSERT INTO autonomy_bindings
            (conversation_id, generation, master_enabled, external_enabled, effective_owner,
             controller_epoch, revision, updated_at)
            VALUES ('conversation', 1, 0, 0, 'off', 0, 1, '2026-09-22')""")
        )
        migration.downgrade()
        assert connection.scalar(text("SELECT effective_owner FROM autonomy_bindings")) == "off"
        assert connection.scalar(text("SELECT value FROM existing_evidence")) == "preserve"


def test_frozen_migration_matches_runtime_metadata(monkeypatch):
    migration = importlib.import_module("migrations.versions.0066_autonomy_admission")
    intrinsic_migration = importlib.import_module(
        "migrations.versions.0070_intrinsic_initiative_thread"
    )
    models = (
        AutonomyBindingModel,
        InitiativeRunModel,
        InitiativeSourceClaimModel,
        InitiativeFeedbackModel,
    )
    deployed = create_engine("sqlite:///:memory:")
    runtime = create_engine("sqlite:///:memory:")
    with deployed.begin() as migration_connection, runtime.begin() as model_connection:
        for connection in (migration_connection, model_connection):
            for name in ("canonical_conversations", "spaces", "presences", "persons"):
                connection.execute(text(f"CREATE TABLE {name} (id TEXT PRIMARY KEY)"))
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(migration_connection))
        )
        migration.upgrade()
        monkeypatch.setattr(
            intrinsic_migration,
            "op",
            Operations(MigrationContext.configure(migration_connection)),
        )
        intrinsic_migration.upgrade()
        for model in models:
            model.__table__.create(model_connection)
            name = model.__tablename__
            assert _schema(migration_connection, name) == _schema(model_connection, name)
