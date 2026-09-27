"""Model contract revisions must survive process restart and hash randomization."""

import os
import subprocess
import sys


def test_revision_is_stable_across_processes_and_changes_with_effective_options():
    script = """
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability, ModelProfile, ModelProtocol, ModelRoute, ModelTask,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter

profile = ModelProfile(id='main', provider='fake', protocol=ModelProtocol.RESPONSES,
    base_url='http://test.invalid', api_key_env='UNUSED', model='m', timeout_seconds=1,
    max_retries=0, default_temperature=0.1, default_max_output_tokens=100,
    capabilities=frozenset(ModelCapability))
routes = {task: ModelRoute(task=task, profile_id='main',
    required_capabilities=frozenset(ModelCapability)) for task in ModelTask}
def revision(p):
    router = ModelRouter(ModelProfileCatalog(profiles={'main':p}, routes=routes))
    return TaskModelExecutor(router=router, pool=ModelClientPool()).profile_revision(
        ModelTask.CHAT_AGENT)
print(revision(profile))
print(revision(profile.model_copy(update={'default_temperature':0.7})))
"""
    results = []
    for seed in ("1", "2", "10", "42"):
        output = subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        values = output.stdout.splitlines()
        assert len(values) == 2 and values[0] != values[1]
        results.append(values)
    assert all(values == results[0] for values in results)
