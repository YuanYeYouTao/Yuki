from qq_ai_bot.conversation.autonomy_binding import (
    AcceptedInitiative,
    AutonomyBinding,
    AutonomyOwner,
    InitiativeSource,
    InitiativeSourceKind,
)


def test_master_off_and_explicit_disable_win_over_provider_recovery() -> None:
    binding = AutonomyBinding("conversation", 2)
    binding = binding.transition(master_enabled=True, external_enabled=True, semantic_ready=True)
    assert binding.effective_owner is AutonomyOwner.SEMANTIC
    epoch = binding.controller_epoch
    binding = binding.transition(master_enabled=True, external_enabled=False, semantic_ready=True)
    assert binding.effective_owner is AutonomyOwner.LEGACY
    assert binding.controller_epoch == epoch + 1
    binding = binding.transition(master_enabled=False, external_enabled=True, semantic_ready=True)
    assert binding.effective_owner is AutonomyOwner.OFF


def test_readiness_loss_falls_back_and_fences_old_proposals() -> None:
    binding = AutonomyBinding("conversation", 2).transition(
        master_enabled=True,
        external_enabled=True,
        semantic_ready=True,
    )
    epoch = binding.controller_epoch
    fallback = binding.transition(
        master_enabled=True,
        external_enabled=True,
        semantic_ready=False,
        fallback_reason="provider_outage",
    )
    assert fallback.fallback_reason == "provider_outage"
    assert not fallback.accepts(
        owner=AutonomyOwner.SEMANTIC, epoch=epoch, conversation_id="conversation", generation=2
    )
    assert fallback.accepts(
        owner=AutonomyOwner.LEGACY,
        epoch=fallback.controller_epoch,
        conversation_id="conversation",
        generation=2,
    )
    assert not fallback.accepts(
        owner=AutonomyOwner.LEGACY,
        epoch=fallback.controller_epoch,
        conversation_id="conversation",
        generation=3,
    )


def test_noop_does_not_advance_epoch_and_accepted_run_does_not_depend_on_it() -> None:
    binding = AutonomyBinding("conversation", 2).transition(
        master_enabled=True,
        external_enabled=True,
        semantic_ready=True,
    )
    unchanged = binding.transition(master_enabled=True, external_enabled=True, semantic_ready=True)
    assert unchanged == binding
    run = AcceptedInitiative(
        "run",
        "proposal",
        "conversation",
        2,
        "space",
        "presence",
        binding.controller_epoch,
        (InitiativeSource(InitiativeSourceKind.EVENT, "123", "v1"),),
        AutonomyOwner.SEMANTIC,
    )
    binding = binding.transition(master_enabled=True, external_enabled=False, semantic_ready=False)
    assert run.belongs_to(binding.conversation_id, binding.generation)
    assert not run.belongs_to(binding.conversation_id, 3)
