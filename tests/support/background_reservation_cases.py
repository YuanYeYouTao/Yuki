"""Exercise the real background worker's reservation through result persistence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from qq_ai_bot.plugin_host.background_turns import PluginBackgroundTurnWorker
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator


async def background_attempt_reservation():
    turns = ConversationTurnCoordinator()
    context = SimpleNamespace(
        generation=1,
        conversation_id="conversation",
        person_id="person",
        space_id=None,
        primary_alias="private:80001:10001",
        scope_id=1,
    )
    event = SimpleNamespace(
        id=1,
        event_kind="external_event",
        source_plugin_id="plugin",
        canonical_conversation_id="conversation",
    )
    job = SimpleNamespace(
        id=1,
        generation=1,
        attempts=1,
        source_event_id=1,
        plugin_id="plugin",
        target_type="private",
        agent_intent="continue",
    )
    resolved = SimpleNamespace(
        sender_account_id="80001",
        external_target_id="10001",
        presence_id="presence",
        connection=SimpleNamespace(bot=None),
    )
    repository = SimpleNamespace(
        load_background_context=AsyncMock(return_value=context),
        ensure_resolved_transport_alias=AsyncMock(),
        validate_turn_attempt=AsyncMock(),
        defer_turn=AsyncMock(),
        fail_turn=AsyncMock(),
    )
    checked = []

    async def finish(*args, **kwargs):
        # Generation tracking is gone but finish_turn has not committed yet.
        assert not turns._states[context.primary_alias].tasks
        assert await turns.begin_background(context.primary_alias) is None
        checked.append(True)
        return True

    repository.finish_turn = AsyncMock(side_effect=finish)
    runtime = SimpleNamespace(
        reply=SimpleNamespace(cancel_on_new_message=True),
        conversation_policy=lambda: SimpleNamespace(interrupt_autonomous_on_new_message=True),
    )
    chat = SimpleNamespace(
        configure_runtime_controls=Mock(),
        generate_main_agent_wakeup=AsyncMock(
            return_value=SimpleNamespace(
                text="done",
                tool_calls_used=0,
                model_requests=1,
                work_state="completed",
                work_id="completed-work",
            )
        ),
    )
    worker = PluginBackgroundTurnWorker(
        repository=repository,
        ledger=SimpleNamespace(get_event=AsyncMock(return_value=event)),
        runtime_config=SimpleNamespace(snapshot=AsyncMock(return_value=runtime)),
        chat=chat,
        turns=turns,
        conversation_scopes=None,
        router=SimpleNamespace(resolve_send_for_person=AsyncMock(return_value=resolved)),
    )
    async with turns.hold(context.primary_alias):
        await worker._execute_admitted(job)
    chat.generate_main_agent_wakeup.assert_not_called()
    repository.defer_turn.assert_awaited_once()
    await worker._execute_admitted(job)
    repository.fail_turn.assert_not_called()
    assert checked == [True]
    assert await turns.begin_background(context.primary_alias) is not None
