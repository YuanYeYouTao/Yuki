"""Indexed delay validation preserves inherited endpoints and writer serialization."""

import asyncio
import random
from dataclasses import replace
from datetime import UTC, datetime
from itertools import product

from tests.conftest import make_settings
from tests.unit.test_control_config_consistency import set_value, setup

from qq_ai_bot.admin.config_service import RuntimeConfigOverrideRecord, RuntimeConfigService
from qq_ai_bot.admin.models import ConfigScopeType


def record(runtime, key, scope, owner, value):
    spec = runtime.registry.get(key)
    return RuntimeConfigOverrideRecord(
        id=1,
        config_key=key,
        scope_type=scope,
        scope_id=owner,
        value=float(value),
        value_type=spec.value_type,
        apply_mode=spec.apply_mode,
        version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        updated_by="test",
        canonical_person_id=owner if scope is ConfigScopeType.USER else None,
        canonical_space_id=owner if scope is ConfigScopeType.GROUP else None,
    )


def test_delay_extrema_match_actual_resolver_for_sparse_overrides(database):
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    rng = random.Random(755)
    keys = ("reply.delay_min_seconds", "reply.delay_max_seconds")
    users = (None, "person-a", "person-b", "person-c")
    groups = (None, "space-a", "space-b", "space-c")
    for _ in range(100):
        records = tuple(
            record(runtime, key, scope, owner, rng.randrange(11))
            for key in keys
            for scope, owners in (
                (ConfigScopeType.GLOBAL, ("",)),
                (ConfigScopeType.USER, users[1:]),
                (ConfigScopeType.GROUP, groups[1:]),
            )
            for owner in owners
            if rng.random() < 0.6
        )
        reference_valid = all(
            runtime._resolve(
                runtime.registry.get(keys[0]),
                records,
                user_id=None,
                group_id=None,
                person_id=user,
                space_id=group,
            ).value
            <= runtime._resolve(
                runtime.registry.get(keys[1]),
                records,
                user_id=None,
                group_id=None,
                person_id=user,
                space_id=group,
            ).value
            for user, group in product(users, groups)
        )
        try:
            runtime._validate_reply_delay_records(records)
        except ValueError:
            actual_valid = False
        else:
            actual_valid = True
        assert actual_valid == reference_valid


def test_large_override_validation_reads_each_record_once(database, monkeypatch):
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    base = record(runtime, "reply.delay_min_seconds", ConfigScopeType.USER, "person", 0)
    records = tuple(
        replace(base, scope_id=f"person-{i}", canonical_person_id=f"person-{i}")
        for i in range(2000)
    ) + tuple(
        record(runtime, "reply.delay_max_seconds", ConfigScopeType.GROUP, f"space-{i}", 10)
        for i in range(2000)
    )
    checked = 0
    original = runtime._valid_stored_record

    def valid(row):
        nonlocal checked
        checked += 1
        return original(row)

    monkeypatch.setattr(runtime, "_valid_stored_record", valid)
    runtime._validate_reply_delay_records(records)
    assert checked == len(records)


async def test_opposite_endpoints_cannot_commit_an_invalid_pair(database):
    runtime, _, _ = await setup(database)
    results = await asyncio.gather(
        set_value(runtime, "reply.delay_min_seconds", 8),
        set_value(runtime, "reply.delay_max_seconds", 4),
    )
    assert sum(result.success for result in results) == 1
    snapshot = await runtime.snapshot()
    assert snapshot.reply.delay_min_seconds <= snapshot.reply.delay_max_seconds
