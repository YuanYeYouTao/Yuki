"""The final public context is composed once, with activation-current material."""

from dataclasses import replace

from tests.conftest import MemorySender
from tests.unit.test_history_dispatch_ownership import _scene

from yuki.domain.messages import ChatResponse
from yuki.llm.fake import FakeLLMProvider


async def test_projection_turn_composes_only_the_selected_context_and_refreshes_state(
    database, tmp_path, monkeypatch
):
    provider = FakeLLMProvider(lambda request: ChatResponse("NO_REPLY", 0))
    _, harness, chat, state, message = await _scene(database, tmp_path, provider)
    original = chat._prompt_composer.compose
    contexts = []

    def compose(**kwargs):
        contexts.append(kwargs["context"])
        return original(**kwargs)

    monkeypatch.setattr(chat._prompt_composer, "compose", compose)
    assert (await harness.processor.handle(message, MemorySender())).reason == "chat"
    state.update({"slot": 1, "text": "fresh-next-activation"})
    assert (
        await harness.processor.handle(
            replace(
                message,
                ingress_key="next-activation",
                native_message_id="next-activation",
                text="继续处理",
            ),
            MemorySender(),
        )
    ).reason == "chat"
    assert len(contexts) == len(provider.requests) == 2
    first, second = (
        "\n".join(item.content or "" for item in request.messages) for request in provider.requests
    )
    assert "original-snapshot" in first
    assert "fresh-next-activation" not in first
    assert "fresh-next-activation" in second
    assert "original-snapshot" not in second
    assert contexts[0].current_event_id != contexts[1].current_event_id
