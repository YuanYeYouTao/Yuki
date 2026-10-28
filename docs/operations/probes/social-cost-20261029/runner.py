import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path[:0] = [str(Path.cwd() / "src"), str(Path.cwd())]


class Timing:
    def __init__(self):
        self.reports = []

    def pytest_runtest_logreport(self, report):
        self.reports.append(
            {
                "nodeid": report.nodeid,
                "stage": report.when,
                "seconds": report.duration,
                "outcome": report.outcome,
            }
        )


timing = Timing()
started = time.perf_counter()
status = pytest.main(["tests/unit/_social_cost_probe.py", "-q", "--tb=short"], plugins=[timing])
path = Path(os.environ["SOCIAL_COST_OUTPUT"])
if path.exists():
    data = json.loads(path.read_text())
    data["pytest_reports"] = timing.reports
    data["process_wall_seconds"] = time.perf_counter() - started
    data["exit_code"] = int(status)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
raise SystemExit(status)
