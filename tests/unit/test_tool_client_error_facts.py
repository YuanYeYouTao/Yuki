"""Known public-safe ClientError facts reach the model instead of a bare tool_failed.

Failure cases written before the change:
1. Unknown or foreign device id before any effect -> ``not_found``, points to
   ``get_coding_agents`` for the device list, and states nothing happened.
2. The same error after the effect may have started -> still ``tool_failed`` with
   unknown effect; never claims nothing happened.
3. Revoked/expired device credential -> ``unauthenticated`` with the reconnect step.
4. Any other ClientError code keeps its code with a generic "no effect" sentence.
5. Non-ClientError exceptions keep today's ``tool_failed`` result.
6. ``TurnSupersededError`` keeps ``superseded``.
"""

import pytest

from yuki.client_api.errors import ClientError
from yuki.services.main_agent_backend import _unexpected_failure
from yuki.services.turn_coordinator import TurnSupersededError


def failure(exc, *, uncertain=False):
    return _unexpected_failure(
        exc, uncertain=uncertain, provider_id="sandbox", tool_name="get_environment"
    )


def test_unknown_device_is_a_fact_the_model_can_correct():
    result = failure(ClientError("not_found"))
    assert (result.ok, result.error_code, result.uncertain) == (False, "not_found", False)
    assert result.mutation_committed is False
    assert "get_coding_agents" in result.public_message
    assert "没有产生效果" in result.public_message


def test_client_error_after_possible_effect_stays_unknown():
    result = failure(ClientError("not_found"), uncertain=True)
    assert (result.error_code, result.uncertain) == ("tool_failed", True)
    assert result.mutation_committed is None


def test_expired_credential_names_the_reconnect_step():
    result = failure(ClientError("unauthenticated"))
    assert result.error_code == "unauthenticated"
    assert "Yuki Desktop" in result.public_message


@pytest.mark.parametrize("code", ["idempotency_conflict", "anything_else"])
def test_other_client_codes_keep_their_code(code):
    result = failure(ClientError(code))
    assert result.error_code == code
    assert code in result.public_message and "没有产生效果" in result.public_message


def test_other_exceptions_and_supersession_are_unchanged():
    assert failure(RuntimeError("boom")).error_code == "tool_failed"
    assert failure(TurnSupersededError("x")).error_code == "superseded"
