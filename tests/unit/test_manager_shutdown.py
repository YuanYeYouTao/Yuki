"""Real Manager entry point closes live Unix/RFB streams on SIGTERM."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX Manager")

BOOT = """
import asyncio,sys
from pathlib import Path
from tests.support.native_computer_manager import NativeManager
from yuki.sandbox import persistent
from yuki.sandbox.manager import main
root=Path(sys.argv[1])
async def recover(self):
    self.ready=True
NativeManager.recover=recover
persistent.PersistentManager=lambda *args,**kwargs: NativeManager(root)
sys.argv=['manager','--root',str(root/'manager'),'--workspace',str(root/'artifacts'),
          '--socket',sys.argv[2],'--image','native-fixture','--persistent-home',str(root/'home')]
asyncio.run(main())
"""


async def test_sigterm_closes_watch_rfb_and_unfinished_request_without_killing_role(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    child = tmp_path / "rfb-pid"
    docker = binary / "docker"
    docker.write_text(f"""#!{sys.executable}
import os,sys
from pathlib import Path
Path({str(child)!r}).write_text(str(os.getpid()))
os.write(1,b'RFB 003.008\\n')
while os.read(0,65536): pass
""")
    docker.chmod(0o755)
    with tempfile.TemporaryDirectory(prefix="ym-stop-", dir="/tmp") as short:
        socket = Path(short) / "m.sock"
        log = (tmp_path / "manager.log").open("wb")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            BOOT,
            str(tmp_path),
            str(socket),
            env={**os.environ, "PATH": str(binary) + ":" + os.environ["PATH"]},
            stdout=log,
            stderr=log,
        )
        clients = []
        try:
            async with asyncio.timeout(5):
                while not socket.exists():
                    assert process.returncode is None, (tmp_path / "manager.log").read_text()
                    await asyncio.sleep(0.01)
            for method in ["control_watch", "display_stream", None]:
                reader, writer = await asyncio.open_unix_connection(str(socket))
                clients.append((reader, writer))
                if method:
                    writer.write(json.dumps({"method": method, "args": {}}).encode() + b"\n")
                    await writer.drain()
                    header = json.loads(await asyncio.wait_for(reader.readline(), 5))
                    if method == "control_watch":
                        assert header["control"]["holder"] == "none"
                    else:
                        assert header == {"ready": True}
                        assert await asyncio.wait_for(reader.readexactly(12), 5) == b"RFB 003.008\n"
                else:
                    writer.write(b'{"method":')
                    await writer.drain()
            assert child.exists()
            process.terminate()
            assert await asyncio.wait_for(process.wait(), 5) == 0
            for reader, _writer in clients:
                assert await asyncio.wait_for(reader.read(), 2) == b""
            with pytest.raises(ProcessLookupError):
                os.kill(int(child.read_text()), 0)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            for _reader, writer in clients:
                writer.close()
                await writer.wait_closed()
            log.close()
