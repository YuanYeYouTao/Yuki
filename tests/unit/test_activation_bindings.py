"""Exact activation exits do not restore obsolete conversation input hints."""

from types import SimpleNamespace

from qq_ai_bot.runtime.activation_bindings import ActiveWorkBindings


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
            assert not old_completion.done() and not new_completion.done()
        assert new_completion.done()
        assert not bindings.completions("newer-work")
        assert bindings.get("shared-conversation") is None
        assert not old_completion.done()
    assert old_completion.done()
    assert not bindings.completions("original-work")
    assert bindings.executions.active_count == 0
