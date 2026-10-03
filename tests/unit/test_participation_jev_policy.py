"""Pinned controller behavior under the Host's source-only tuning policy."""

import json
from types import SimpleNamespace

import pytest
from tests.conftest import make_settings
from yuki_participation.autonomy_parameters import AutonomyParameters as LibraryParameters
from yuki_participation.controller import Controller
from yuki_participation.models import Choice, Observation, Scope, ScopedEvent, Snapshot, SourceRef
from yuki_participation.rubric import CRITERIA, REVISION

from qq_ai_bot.admin.config_files import ConfigFileService
from qq_ai_bot.services.participation_parameters import DEFAULT_AUTONOMY_PARAMETERS
from qq_ai_bot.services.semantic_participation import SemanticParticipationService


def observed_controller(parameters, mark="open_group"):
    scope = Scope(conversation_id="synthetic-group", generation=1)
    controller = Controller(scope, 1000, parameters)
    event = ScopedEvent(
        scope=scope,
        ref=SourceRef(event_id="event:1", revision=1),
        thread="synthetic-thread",
        author="synthetic-author",
        target="group",
        text="Synthetic source for an offline policy test.",
        at=1001,
    )
    assert controller.observe_committed_event(event)
    answers = {}
    for dimension, option in (
        ("interaction_mark", mark),
        ("information_state", "new"),
        ("floor_state", "yuki"),
        ("boundary_scope", "group_thread"),
    ):
        answers[dimension] = Choice(
            choice=option,
            probabilities={key: float(key == option) for key in CRITERIA[dimension]},
        )
    assert controller.apply_semantic_observation(
        Observation(
            observation_id="synthetic-observation",
            snapshot=Snapshot(scope=scope, focus=event, context=(), sequence=1, issued_at=1002),
            provider="offline_fixture",
            model_revision="fixture",
            rubric_revision=REVISION,
            received_at=1002,
            answers=answers,
        )
    )
    return controller


def test_open_group_rate_increases_without_changing_intrinsic_rate():
    previous = observed_controller(LibraryParameters(source_interval_seconds=45))
    current = observed_controller(DEFAULT_AUTONOMY_PARAMETERS)
    old_rate = previous.opportunity_scores(1010)["event:1"]
    assert old_rate > 0
    assert current.opportunity_scores(1010)["event:1"] == pytest.approx(old_rate * 1.5)
    assert current.intrinsic_opportunity(1010) == pytest.approx(
        previous.intrinsic_opportunity(1010)
    )


def test_invitation_is_immediate_and_host_availability_still_controls_admission():
    for parameters in (LibraryParameters(source_interval_seconds=45), DEFAULT_AUTONOMY_PARAMETERS):
        controller = observed_controller(parameters, "invite_yuki")
        assert controller.advance(1002, controller_epoch=0, host_available=False) is None
        proposal = controller.advance(1002, controller_epoch=0, host_available=True)
        assert proposal is not None
        assert proposal.sources == (SourceRef(event_id="event:1", revision=1),)


@pytest.mark.parametrize("mark", ["ask_yuki_stop", "unknown"])
def test_tuning_does_not_turn_stop_or_unknown_into_an_opportunity(mark):
    controller = observed_controller(DEFAULT_AUTONOMY_PARAMETERS, mark)
    assert controller.opportunity_scores(1010) == {}
    assert controller.advance(1010, controller_epoch=0, host_available=True) is None


def test_tuning_does_not_revive_invalidated_source():
    controller = observed_controller(DEFAULT_AUTONOMY_PARAMETERS)
    assert controller.opportunity_scores(1010)
    controller.observe_source_change(SourceRef(event_id="event:1", revision=1))
    assert controller.opportunity_scores(1010) == {}
    assert controller.advance(1010, controller_epoch=0, host_available=True) is None


async def test_partial_hot_profile_and_explicit_override_keep_state_and_last_good_value(
    database, tmp_path
):
    path = tmp_path / "autonomous-model.json"
    settings = make_settings(database.url, semantic_participation_model_config_file=path)
    host = SemanticParticipationService(SimpleNamespace(settings=settings, database=database))
    controller = observed_controller(DEFAULT_AUTONOMY_PARAMETERS)
    host._sessions[("synthetic-group", 1)] = SimpleNamespace(controller=controller)
    service = ConfigFileService(settings, autonomy_parameters=host.control_model_parameters)
    before = controller.state.model_dump_json()

    empty = await service.read("autonomous_model")
    assert empty["defaults"]["source_interval_seconds"] == 30
    assert empty["parameter_schema"]["properties"]["source_interval_seconds"]["default"] == 30
    assert empty["document"] == empty["loaded_document"]

    path.write_text(json.dumps({"pressure_bias": 0.2}), encoding="utf-8")
    host._refresh_model_parameters()
    assert controller.parameters.source_interval_seconds == 30
    assert (await service.read("autonomous_model"))["matches_loaded"] is True

    path.write_text('{"source_interval_seconds":45}', encoding="utf-8")
    host._refresh_model_parameters()
    assert controller.parameters.source_interval_seconds == 45
    assert (await service.read("autonomous_model"))["document"]["source_interval_seconds"] == 45

    path.write_text('{"source_interval_seconds":0}', encoding="utf-8")
    host._refresh_model_parameters()
    assert host._model_config_error is not None
    assert controller.parameters.source_interval_seconds == 45

    path.unlink()
    host._refresh_model_parameters()
    assert controller.parameters.source_interval_seconds == 30
    assert controller.state.model_dump_json() == before
