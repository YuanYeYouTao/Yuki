"""Exact activation exits do not restore obsolete conversation input hints."""

import asyncio
from types import SimpleNamespace

import pytest

from yuki.runtime.activation_bindings import ActiveWorkBindings
from yuki.services.concurrency import ConcurrencyManager, RequestCancelledError


async def test_exact_work_completions_survive_a_newer_scope_binding():
    bindings = ActiveWorkBindings()
    original = SimpleNamespace(current={"id": "original-work"})
    newer = SimpleNamespace(current={"id": "newer-work"})
    with bindings.bind("shared-conversation", original):
        (old_completion,) = bindings.completions("original-work")
        with bindings.bind("shared-conversation", newer):
            (new_completion,) = bindings.completions("newer-work")
            assert bindings.get("shared-conversation") is newer
            assert bindings.completions("original-work") == (old_completion,)
            assert bindings.owners(("original-work", "newer-work")) == {asyncio.current_task()}
            assert not old_completion.done() and not new_completion.done()
        assert new_completion.done()
        assert not bindings.completions("newer-work")
        assert bindings.get("shared-conversation") is None
        assert not old_completion.done()
    assert old_completion.done()
    assert not bindings.completions("original-work")
    assert bindings.executions.active_count == 0


async def test_original_activation_keeps_its_provider_when_a_new_work_reuses_the_scope():
    bindings = ActiveWorkBindings()
    concurrency = ConcurrencyManager()
    entered = {identity: asyncio.Event() for identity in ("original-work", "newer-work")}
    release = asyncio.Event()

    async def activate(identity):
        control = SimpleNamespace(current={"id": identity})

        async def provider():
            entered[identity].set()
            await release.wait()
            return identity

        with bindings.bind("shared-conversation", control):
            return await concurrency.run_llm("shared-conversation", provider)

    original = asyncio.create_task(activate("original-work"))
    newer = None
    try:
        await entered["original-work"].wait()
        newer = asyncio.create_task(activate("newer-work"))
        await entered["newer-work"].wait()
        (provider,) = concurrency.request_tasks(bindings.owners(("original-work",)))
        assert provider is not concurrency.request_task("shared-conversation")
        (completion,) = bindings.completions("original-work")
        provider.cancel()
        with pytest.raises(RequestCancelledError):
            await original
        await completion
        assert not newer.done()
        assert concurrency.is_processing("shared-conversation")
        release.set()
        assert await newer == "newer-work"
        assert not concurrency.is_processing("shared-conversation")
    finally:
        release.set()
        await asyncio.gather(
            original, *([newer] if newer is not None else []), return_exceptions=True
        )
