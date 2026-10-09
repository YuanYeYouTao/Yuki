"""Durable work race and recovery contracts against a real SQLite database."""


# P10: explicit Invocation fixture contract; existing assertions are retained.


async def _persisted_tool_receipt(
    control, key, name, result, *, arguments="{}", side_effecting=True, owner=None
):
    from qq_ai_bot.capabilities.results import normalize_legacy_result
    from qq_ai_bot.runtime.effect_outcomes import execution_evidence

    owner = owner or control.current["id"]
    outcome = normalize_legacy_result(result, provider_id="core", tool_name=name)
    assert await control.repository.prepare_effect(control.lease, owner, key, "tool")
    await control.repository.record_effect(
        key,
        "accepted",
        {
            "result": result,
            "outcome": execution_evidence(
                outcome, tool=name, side_effecting=side_effecting, arguments=arguments
            ),
        },
    )
