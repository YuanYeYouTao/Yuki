"""Collect original Social source, target and account boundaries in ordinary pytest."""

import pytest
from tests.support import social_history_cases as history
from tests.support import social_identity_cases as identity


@pytest.mark.parametrize(
    "scenario,arguments",
    [
        (history.history_agent_loop, ()),
        (history.history_receipt, ()),
        (history.history_targets_and_delegation, ()),
        (identity.current_speaker_mention, ()),
        *[
            (identity.test_recall_uses_original_presence, (state,))
            for state in ("paused", "switched", "deleted")
        ],
        (identity.test_members_ignore_send_pause_and_try_accessible_connections, ()),
        (identity.test_multiple_space_bindings_require_explicit_selection, ()),
        (identity.test_disabled_presence_cannot_read_or_recall, ()),
        (identity.test_private_poke_uses_pinned_binding_and_rejects_conflict, ()),
        *[
            (identity.test_group_poke_resolves_binding_without_first_item, (selector,))
            for selector in ("explicit", "subject", "ambiguous", "foreign")
        ],
        *[
            (identity.test_real_mentions_preserve_segments_and_replay, (attachment,))
            for attachment in (None, "image", "file")
        ],
        (identity.test_mentions_reject_ambiguous_or_nonmember_accounts, ()),
    ],
    ids=lambda value: getattr(value, "__name__", None),
)
async def test_social_identity_contract(database, tmp_path, scenario, arguments):
    env = await identity.social_env(database, tmp_path)
    await scenario(env, *arguments)


async def test_recall_rechecks_account_before_dispatch(database, tmp_path, monkeypatch):
    env = await identity.social_env(database, tmp_path)
    await identity.test_recall_rechecks_connection_before_claim(env, monkeypatch)
