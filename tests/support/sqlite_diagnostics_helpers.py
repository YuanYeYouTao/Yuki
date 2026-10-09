"""Writer attribution covers real commit/rollback tails without affecting transactions."""

import json


def holders(caplog):
    return [
        json.loads(record.getMessage().split("holders=", 1)[1])
        for record in caplog.records
        if record.getMessage().startswith("sqlite_write_contended holders=")
    ]
