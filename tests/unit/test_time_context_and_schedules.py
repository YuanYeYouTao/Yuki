from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from qq_ai_bot.automation.models import (
    AfterSchedule,
    DailySchedule,
    IntervalSchedule,
    OnceSchedule,
    WeeklySchedule,
)
from qq_ai_bot.time.formatting import (
    local_datetime,
    local_iso,
    local_text,
    stored_utc,
    utc_iso,
)
from qq_ai_bot.time.schedules import initial_run_at, next_run_at, schedule_after_completion
from qq_ai_bot.time.service import TimeContextService


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def test_utc_storage_is_rendered_as_china_local_time() -> None:
    stored = datetime(2026, 7, 26, 19, 35, 44, tzinfo=UTC)

    assert local_iso(stored, "Asia/Shanghai") == "2026-07-27T03:35:44+08:00"
    assert local_text(stored, "Asia/Shanghai") == "2026-07-27 03:35:44"


def test_naive_database_timestamp_is_treated_as_utc_at_the_display_boundary() -> None:
    stored = datetime(2026, 8, 12, 16, 30)

    assert stored_utc(stored) == datetime(2026, 8, 12, 16, 30, tzinfo=UTC)
    assert utc_iso(stored) == "2026-08-12T16:30:00+00:00"
    assert local_datetime(stored, "Asia/Shanghai").isoformat() == "2026-08-13T00:30:00+08:00"
    assert local_iso(stored, "Asia/Shanghai") == "2026-08-13T00:30:00+08:00"


@pytest.mark.asyncio
async def test_time_context_uses_persistent_person_timezone(database) -> None:
    now = datetime(2026, 7, 27, 15, 10, tzinfo=UTC)
    service = TimeContextService(database, clock=FakeClock(now))

    assert (await service.current("10001")).to_model_dict() == {
        "utc": "2026-07-27T15:10:00Z",
        "local": "2026-07-27T23:10:00+08:00",
        "timezone": "Asia/Shanghai",
        "date": "2026-07-27",
        "weekday": "Monday",
    }

    # A person row is required by the database FK and is normally created by
    # the real inbound event pipeline.
    from qq_ai_bot.persistence.repositories import PeopleRepository

    await PeopleRepository(database).observe(user_id="10001", nickname="测试")
    assert await service.set_timezone("10001", "America/New_York") == "America/New_York"
    assert (await service.current("10001")).timezone == "America/New_York"


@pytest.mark.parametrize("timezone", ["No/Such_Zone", "", "x" * 65])
def test_invalid_timezone_is_rejected(database, timezone: str) -> None:
    service = TimeContextService(database)
    with pytest.raises(ValueError):
        service.at(datetime.now(UTC), timezone)


def test_supported_schedules_calculate_utc() -> None:
    now = datetime(2026, 7, 27, 7, 0, tzinfo=UTC)
    assert initial_run_at(AfterSchedule(type="after", seconds=1200), now, "Asia/Shanghai") == (
        now + timedelta(minutes=20)
    )
    assert initial_run_at(
        OnceSchedule(
            type="once",
            local_datetime=datetime(2026, 7, 28, 15, 0),
            timezone="Asia/Shanghai",
        ),
        now,
        "Asia/Shanghai",
    ) == datetime(2026, 7, 28, 7, 0, tzinfo=UTC)
    assert initial_run_at(
        DailySchedule(type="daily", hour=15, minute=0, timezone="Asia/Shanghai"),
        now,
        "Asia/Shanghai",
    ) == datetime(2026, 7, 28, 7, 0, tzinfo=UTC)
    assert initial_run_at(
        WeeklySchedule(
            type="weekly", weekdays=(1, 3, 5), hour=15, minute=0, timezone="Asia/Shanghai"
        ),
        now,
        "Asia/Shanghai",
    ) == datetime(2026, 7, 29, 7, 0, tzinfo=UTC)
    interval = IntervalSchedule(type="interval", seconds=3600)
    assert next_run_at(interval, now, "Asia/Shanghai") == now + timedelta(hours=1)
    assert schedule_after_completion(interval, now, now + timedelta(hours=3), "Asia/Shanghai") == (
        now + timedelta(hours=4)
    )


def test_once_in_past_is_rejected() -> None:
    now = datetime(2026, 7, 27, 7, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="晚于"):
        initial_run_at(
            OnceSchedule(type="once", local_datetime=datetime(2026, 7, 27, 14, 59)),
            now,
            "Asia/Shanghai",
        )


def test_nonexistent_dst_time_moves_forward() -> None:
    now = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
    result = initial_run_at(
        OnceSchedule(
            type="once",
            local_datetime=datetime(2026, 3, 8, 2, 30),
            timezone="America/New_York",
        ),
        now,
        "America/New_York",
    )
    assert result == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)


_V2_NOW = datetime(2026, 8, 25, 15, 10, tzinfo=UTC)
_V2_CUTOVER = "550e8400-e29b-41d4-a716-4466554400aa"


async def _flip_complete_v2(database) -> None:
    from qq_ai_bot.identity.db_models import IdentityRuntimeStateModel

    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _V2_CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _V2_NOW


async def _two_bindings_one_person(database, first: str = "1001", second: str = "1002"):
    from uuid import uuid4

    from qq_ai_bot.identity.db_models import IdentityBindingModel
    from qq_ai_bot.identity.dual_write import _create_person_binding
    from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM

    async with database.sessions() as session, session.begin():
        created = await _create_person_binding(
            session, external_id=first, display_name="", now=_V2_NOW
        )
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=created.person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id=second,
                display_name="",
                status="active",
                revision=1,
                created_at=_V2_NOW,
                updated_at=_V2_NOW,
            )
        )
        return created.person_id


@pytest.mark.asyncio
async def test_v2_timezone_is_shared_across_bindings_without_people(database) -> None:
    from sqlalchemy import func, select

    from qq_ai_bot.identity.canonical_projections import canonical_person_storage_key
    from qq_ai_bot.persistence.models import PersonModel, PersonTimeSettingModel

    await _flip_complete_v2(database)
    person_id = await _two_bindings_one_person(database)
    service = TimeContextService(database, clock=FakeClock(_V2_NOW))

    assert await service.set_timezone("1001", "America/New_York") == "America/New_York"
    current_b = await service.current("1002")
    assert current_b.timezone == "America/New_York"
    assert current_b.to_model_dict()["local"] == "2026-08-25T11:10:00-04:00"

    assert await service.set_timezone("1002", "Europe/London") == "Europe/London"
    assert (await service.current("1001")).timezone == "Europe/London"

    async with database.sessions() as session:
        rows = list(await session.scalars(select(PersonTimeSettingModel)))
        people = int(await session.scalar(select(func.count()).select_from(PersonModel)) or 0)
    assert people == 0
    assert len(rows) == 1
    assert rows[0].user_id == canonical_person_storage_key(person_id)
    assert rows[0].canonical_person_id == person_id


@pytest.mark.asyncio
async def test_v2_conflicting_time_settings_fail_closed(database) -> None:
    from qq_ai_bot.identity.errors import IdentityDualWriteError
    from qq_ai_bot.persistence.models import PersonTimeSettingModel

    await _flip_complete_v2(database)
    person_id = await _two_bindings_one_person(database)
    async with database.sessions() as session, session.begin():
        session.add(
            PersonTimeSettingModel(
                user_id="1001",
                timezone="America/New_York",
                created_at=_V2_NOW,
                updated_at=_V2_NOW,
                canonical_person_id=person_id,
            )
        )
        session.add(
            PersonTimeSettingModel(
                user_id="1002",
                timezone="Europe/London",
                created_at=_V2_NOW,
                updated_at=_V2_NOW,
                canonical_person_id=person_id,
            )
        )
    service = TimeContextService(database)
    with pytest.raises(IdentityDualWriteError) as exc:
        await service.timezone_for("1002")
    assert exc.value.category == "canonical_owner_mismatch"
    assert "1002" not in str(exc.value)


@pytest.mark.asyncio
async def test_v2_missing_and_inactive_binding_fail_closed_for_timezone(database) -> None:
    from sqlalchemy import select

    from qq_ai_bot.identity.db_models import IdentityBindingModel
    from qq_ai_bot.identity.errors import IdentityDualWriteError
    from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM

    await _flip_complete_v2(database)
    service = TimeContextService(database)
    with pytest.raises(IdentityDualWriteError) as exc:
        await service.current("1001")
    assert exc.value.category == "unclassified"
    assert "1001" not in str(exc.value)

    await _two_bindings_one_person(database)
    async with database.sessions() as session, session.begin():
        binding = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == IDENTITY_PLATFORM,
                IdentityBindingModel.external_account_id == "1002",
            )
        )
        assert binding is not None
        binding.status = "disabled"
    with pytest.raises(IdentityDualWriteError) as exc:
        await service.set_timezone("1002", "UTC")
    assert exc.value.category == "unclassified"
    assert "1002" not in str(exc.value)


@pytest.mark.asyncio
async def test_v2_enabled_person_uuid_uses_default_timezone(database) -> None:
    await _flip_complete_v2(database)
    person_id = await _two_bindings_one_person(database)
    service = TimeContextService(database, default_timezone="Asia/Shanghai")
    assert await service.timezone_for(person_id) == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_v2_disabled_person_uuid_timezone_fails_closed(database) -> None:
    from qq_ai_bot.identity.db_models import CanonicalPersonModel
    from qq_ai_bot.identity.errors import IdentityDualWriteError

    await _flip_complete_v2(database)
    person_id = await _two_bindings_one_person(database)
    async with database.sessions() as session, session.begin():
        person = await session.get(CanonicalPersonModel, person_id)
        assert person is not None
        person.enabled = False
    service = TimeContextService(database)
    with pytest.raises(IdentityDualWriteError) as exc:
        await service.timezone_for(person_id)
    assert exc.value.category == "canonical_owner_disabled"
    assert person_id not in str(exc.value)
    assert "1001" not in str(exc.value)


@pytest.mark.asyncio
async def test_v2_disabled_person_qq_timezone_fails_closed_and_admin_can_reenable(
    database,
) -> None:
    from qq_ai_bot.identity.errors import IdentityDualWriteError
    from qq_ai_bot.persistence.repositories import PeopleRepository

    await _flip_complete_v2(database)
    await _two_bindings_one_person(database)
    people = PeopleRepository(database)
    service = TimeContextService(database)
    assert await service.set_timezone("1001", "America/New_York") == "America/New_York"

    disabled = await people.set_enabled("1001", False)
    assert disabled.enabled is False
    with pytest.raises(IdentityDualWriteError) as read_exc:
        await service.timezone_for("1001")
    assert read_exc.value.category == "canonical_owner_disabled"
    assert "1001" not in str(read_exc.value)
    with pytest.raises(IdentityDualWriteError) as write_exc:
        await service.set_timezone("1001", "UTC")
    assert write_exc.value.category == "canonical_owner_disabled"
    assert "1001" not in str(write_exc.value)
    with pytest.raises(IdentityDualWriteError) as sibling_exc:
        await service.timezone_for("1002")
    assert sibling_exc.value.category == "canonical_owner_disabled"
    assert "1002" not in str(sibling_exc.value)

    restored = await people.set_enabled("1001", True)
    assert restored.enabled is True
    assert await service.timezone_for("1001") == "America/New_York"
    assert await service.set_timezone("1002", "Europe/London") == "Europe/London"
    assert await service.timezone_for("1001") == "Europe/London"


@pytest.mark.asyncio
async def test_v2_missing_and_wrong_kind_uuid_timezone_fail_closed(database) -> None:
    from qq_ai_bot.identity.dual_write import (
        ensure_canonical_presence_preconfig,
        ensure_canonical_space_preconfig,
    )
    from qq_ai_bot.identity.errors import IdentityDualWriteError

    await _flip_complete_v2(database)
    async with database.sessions() as session, session.begin():
        presence_id = await ensure_canonical_presence_preconfig(session, "8000", now=_V2_NOW)
        space_id = await ensure_canonical_space_preconfig(session, "2001", now=_V2_NOW)
    service = TimeContextService(database)
    missing = "550e8400-e29b-41d4-a716-4466554400ff"
    with pytest.raises(IdentityDualWriteError) as missing_exc:
        await service.timezone_for(missing)
    assert missing_exc.value.category == "unclassified"
    assert missing not in str(missing_exc.value)
    with pytest.raises(IdentityDualWriteError) as presence_exc:
        await service.timezone_for(presence_id)
    assert presence_exc.value.category == "canonical_kind_mismatch"
    assert presence_id not in str(presence_exc.value)
    with pytest.raises(IdentityDualWriteError) as space_exc:
        await service.timezone_for(space_id)
    assert space_exc.value.category == "canonical_kind_mismatch"
    assert space_id not in str(space_exc.value)
