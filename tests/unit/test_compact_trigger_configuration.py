"""Compact waterlines are independent of retired Host input capacities."""

import pytest
from pydantic import ValidationError
from tests.conftest import make_settings
from tests.unit.test_runtime_config_read_snapshot import _owners

from yuki.admin.config_service import RuntimeConfigService
from yuki.admin.models import ConfigApplyMode
from yuki.config import Settings


@pytest.mark.parametrize("value", [None, 1, 2097153])
def test_compact_trigger_environment_and_domain_have_only_positive_bound(
    tmp_path, monkeypatch, value
):
    monkeypatch.delenv("CONTEXT_COMPACT_TRIGGER_TOKENS", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        f"CONTEXT_COMPACT_TRIGGER_TOKENS={value}\n" if value is not None else "",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env, llm_provider="fake")
    expected = 96000 if value is None else value
    assert settings.context_compact_trigger_tokens == expected
    assert settings.conversation.context_compact_trigger_tokens == expected


def test_compact_trigger_rejects_nonpositive_configuration():
    with pytest.raises(ValidationError):
        make_settings("sqlite+aiosqlite:///:memory:", context_compact_trigger_tokens=0)


async def test_hot_trigger_uses_existing_scope_owners_and_reverts_without_restart(database):
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    person, _, space, _ = await _owners(database)
    for scope, owner, value in (
        ("global", "", 2097153),
        ("group", space, 1),
        ("user", person, 70000),
    ):
        result = await runtime.set_override(
            "context.compact_trigger_tokens",
            value,
            scope_type=scope,
            scope_id=owner,
            actor_user_id="operator",
            trigger_message_id="",
        )
        assert result.success and result.apply_mode is ConfigApplyMode.HOT

    assert (await runtime.snapshot()).context.compact_trigger_tokens == 2097153
    assert (await runtime.snapshot(user_id=person)).context.compact_trigger_tokens == 70000
    assert (
        await runtime.snapshot(user_id=person, group_id=space)
    ).context.compact_trigger_tokens == 1
    removed = await runtime.delete_override(
        "context.compact_trigger_tokens",
        scope_type="group",
        scope_id=space,
        actor_user_id="operator",
        trigger_message_id="",
    )
    assert removed.success
    assert (
        await runtime.snapshot(user_id=person, group_id=space)
    ).context.compact_trigger_tokens == 2097153


async def test_hot_trigger_rejects_nonpositive_values(database):
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    result = await runtime.set_override(
        "context.compact_trigger_tokens",
        0,
        scope_type="global",
        scope_id="",
        actor_user_id="operator",
        trigger_message_id="",
    )
    assert not result.success and result.error_category == "validation_error"
    assert (await runtime.snapshot()).context.compact_trigger_tokens == 96000
