"""Control operators administer real owners without acquiring their identity."""

from datetime import UTC, datetime

from tests.conftest import make_settings
from tests.support.automation_runtime_helpers import FakeClock, _script

from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.time.service import TimeContextService


def automation_service(database):
    return AutomationService(
        settings=make_settings(database.url, automation_enabled=True, superusers=("9999",)),
        repository=AutomationRepository(database),
        registry=build_capability_registry(),
        time_service=TimeContextService(database, clock=FakeClock(datetime.now(UTC))),
    )


def group_script():
    script = _script().model_dump(mode="json")
    script["context"] = {"scene": "current_group"}
    script["steps"][0]["arguments"].pop("target")
    return script
