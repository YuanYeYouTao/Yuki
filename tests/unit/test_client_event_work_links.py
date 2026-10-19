"""History discovers ended root Work without loading the full Work archive."""

from uuid import uuid4

import pytest
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_client_api import bearer
from tests.unit.test_client_attachment_content import upload
from tests.unit.test_client_works import owner, seeded

admin = admin_fixture
app_env = app_fixture


async def test_private_event_links_include_ended_roots_and_exclude_children_and_other_entries(
    app_env, database
):
    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database)
    lease = await original.repository.acquire(original.row["conversation_id"], 1)
    child = await original.repository.accept(
        lease,
        source_key=f"child:{uuid4()}",
        source=original.source,
        goal="child",
        parent_work_id=original.row["id"],
    )
    await original.repository.release(lease)
    group = await seeded(env, database, group=True)
    stranger = await seeded(env, database, account="1002")
    current = await original.repository.get(original.row["id"])
    cancelled = await env.client.post(
        f"/client/v1/works/{original.row['id']}/cancel",
        headers=bearer(device["token"]),
        json={"request_id": str(uuid4()), "expected_revision": current["revision"]},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert (await original.repository.get(child["id"]))["state"] == "cancelled"
    active = (
        await env.client.get("/client/v1/home?works=active", headers=bearer(device["token"]))
    ).json()["data"]["works"]
    assert original.row["id"] not in {item["work_id"] for item in active}
    page = await env.client.get(
        f"/client/v1/conversations/{original.row['conversation_id']}/events",
        headers=bearer(device["token"]),
        params={"after": original.event.id - 1, "limit": 1},
    )
    assert page.status_code == 200, page.text
    assert page.json()["data"]["items"][0]["work_ids"] == [original.row["id"]]
    for hidden in (group, stranger):
        response = await env.client.get(
            f"/client/v1/conversations/{hidden.row['conversation_id']}/events",
            headers=bearer(device["token"]),
        )
        assert response.status_code == 404


@pytest.mark.parametrize("text", ["", "[file附件0：使用者自己輸入] original"])
async def test_display_body_uses_original_app_text_segments_without_stripping_literal_user_text(
    app_env, text
):
    env = app_env
    device, _ = await owner(env, env.identity.database)
    principal = await env.identity.authenticate(device["token"])
    uploaded = (await upload(env, device, b"test content", kind="file")).json()["data"]
    result = await env.client.post(
        "/client/v1/messages",
        headers=bearer(device["token"]),
        json={"request_id": str(uuid4()), "text": text, "attachments": [uploaded["media_id"]]},
    )
    assert result.status_code == 200, result.text
    event_id = result.json()["data"]["event_id"]
    page = await env.client.get(
        f"/client/v1/conversations/{principal.conversation_id}/events",
        headers=bearer(device["token"]),
        params={"after": event_id - 1, "limit": 1},
    )
    event = page.json()["data"]["items"][0]
    assert event["display_text"] == text
    assert event["text"] != event["display_text"]
    assert len(event["attachments"]) == 1
