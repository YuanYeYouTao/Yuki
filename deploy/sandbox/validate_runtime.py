"""Validate an explicitly supplied isolated persistent environment socket."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4


async def request(socket: Path, method: str, arguments: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(socket)
    try:
        writer.write(
            json.dumps({"method": method, "args": arguments, "request_id": str(uuid4())}).encode()
            + b"\n"
        )
        await writer.drain()
        return json.loads(await reader.readline())
    finally:
        writer.close()
        await writer.wait_closed()


async def main(socket: Path) -> None:
    status = await request(socket, "environment_status", {})
    assert not status.get("error"), status
    result = await request(
        socket,
        "terminal_exec",
        {
            "command": (
                'python -c "from pathlib import Path; import PIL,openpyxl,pypdf; '
                "Path('/workspace/validation.txt').write_text('verified'); print('ok')\""
            ),
            "timeout_seconds": 30,
        },
    )
    async with asyncio.timeout(90):
        while result.get("pending"):
            result = await request(socket, "get_code_run", {"run_id": result["run_id"]})
    assert result.get("status") == "succeeded", result
    published = await request(socket, "workspace_publish", {"path": "validation.txt"})
    assert published.get("immutable") is True and published.get("artifact_id"), published
    assert (await request(socket, "run_python", {"code": "pass"})).get("error") == "unknown_method"
    print(
        json.dumps(
            {
                "persistent_terminal": "passed",
                "explicit_publication": "passed",
                "retired_python_entry": "rejected",
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated-manager-socket", type=Path, required=True)
    arguments = parser.parse_args()
    asyncio.run(main(arguments.isolated_manager_socket))
