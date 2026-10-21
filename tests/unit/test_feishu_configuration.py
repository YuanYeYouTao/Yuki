"""Feishu credentials remain optional and never appear in partial-config errors."""

import pytest
from pydantic import ValidationError
from tests.conftest import make_settings

from yuki.config import Settings
from yuki.container import ApplicationContainer


def test_feishu_environment_selects_lark_and_masks_secret(tmp_path, monkeypatch):
    for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_DOMAIN"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "FEISHU_APP_ID=cli_offline\n"
        "FEISHU_APP_SECRET=offline-only-secret\n"
        "FEISHU_DOMAIN=https://open.larksuite.com\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env, llm_provider="fake")
    assert settings.feishu_app_id == "cli_offline"
    assert settings.feishu_domain == "https://open.larksuite.com"
    assert settings.feishu_app_secret.get_secret_value() == "offline-only-secret"
    assert "offline-only-secret" not in repr(settings)


@pytest.mark.parametrize(("app_id", "secret"), [("cli_offline", ""), ("", "offline-only-secret")])
def test_partial_feishu_credentials_stop_before_resource_creation(app_id, secret, monkeypatch):
    def unexpected_resources(*_args, **_kwargs):
        raise AssertionError("partial configuration must not construct resources")

    monkeypatch.setattr("yuki.container.PersistenceModule", unexpected_resources)
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:", feishu_app_id=app_id, feishu_app_secret=secret
    )
    with pytest.raises(ValueError, match="FEISHU_APP_ID and FEISHU_APP_SECRET") as error:
        ApplicationContainer(settings)
    assert "offline-only-secret" not in str(error.value)


def test_unknown_cloud_is_not_silently_assigned_feishu_identity():
    with pytest.raises(ValidationError):
        make_settings("sqlite+aiosqlite:///:memory:", feishu_domain="https://unconfigured.example")
