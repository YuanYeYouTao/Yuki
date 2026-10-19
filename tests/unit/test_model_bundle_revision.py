"""A model document and its private keys have one control-plane revision."""

import os

from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_admin_api import command

from yuki.admin.config_files import ConfigFileError
from yuki.model_runtime.secrets import encode_model_secrets, model_secrets_path

admin = admin_fixture


async def configure_key(client):
    current = (await client.get("/admin/v1/models")).json()["data"]
    document = current["document"]
    document["profiles"]["main"]["api_key_env"] = "BUNDLE_TEST_KEY"
    applied = await client.post(
        "/admin/v1/models/apply", json=command(current["revision"], document=document)
    )
    assert applied.status_code == 200, applied.text
    return (await client.get("/admin/v1/models")).json()["data"]


async def test_secret_rotation_invalidates_previous_bundle_revision(admin):
    client, api, _, models, *_ = admin
    before = await configure_key(client)
    first = await client.post(
        "/admin/v1/models/secrets",
        json=command(before["revision"], name="BUNDLE_TEST_KEY", value="fixture-first"),
    )
    assert first.status_code == 200, first.text
    after = (await client.get("/admin/v1/models")).json()["data"]
    assert after["revision"] != before["revision"]
    stale = await client.post(
        "/admin/v1/models/secrets",
        json=command(before["revision"], name="BUNDLE_TEST_KEY", value="fixture-second"),
    )
    assert stale.status_code == 409, stale.text
    profile = api.commands.model.files.loaded_catalog.profiles["main"]
    assert models._pool.api_key_for(profile) == "fixture-first"
    assert "fixture-first" not in str(after)


async def test_external_secret_edit_invalidates_document_write(admin):
    client, api, *_ = admin
    before = await configure_key(client)
    path = model_secrets_path(api.commands.model.files._settings.model_profiles_file)
    path.write_bytes(encode_model_secrets({"BUNDLE_TEST_KEY": "fixture-external"}))
    os.chmod(path, 0o600)
    stale = await client.post(
        "/admin/v1/models/apply",
        json=command(before["revision"], document=before["document"]),
    )
    assert stale.status_code == 409, stale.text
    assert "fixture-external" in path.read_text()


async def test_failed_document_write_restores_our_secret_without_switching_live_pool(
    admin, monkeypatch
):
    client, api, _, models, *_ = admin
    before = await configure_key(client)
    files = api.commands.model.files
    path = files._settings.model_profiles_file.absolute()
    old_document = path.read_bytes()
    secret_path = model_secrets_path(path)
    original = files._replace

    def fail_document(selected, expected, replacement):
        if selected == path:
            raise ConfigFileError("operation_unavailable")
        return original(selected, expected, replacement)

    monkeypatch.setattr(files, "_replace", fail_document)
    failed = await client.post(
        "/admin/v1/models/secrets",
        json=command(before["revision"], name="BUNDLE_TEST_KEY", value="fixture-unused"),
    )
    assert failed.status_code != 200
    assert path.read_bytes() == old_document
    assert not secret_path.exists()
    assert models._pool.api_key_for(files.loaded_catalog.profiles["main"]) != "fixture-unused"
