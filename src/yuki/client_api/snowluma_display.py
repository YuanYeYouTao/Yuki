"""Open the operator-configured QQ display; the VNC password stays on the server."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from fastapi import WebSocket

from yuki.client_api.rfb import RfbProtocolError

VERSION = b"RFB 003.008\n"


def configured() -> bool:
    """The bundled service or one deployed elsewhere: only its configured address counts."""
    return bool(os.environ.get("SNOWLUMA_VNC_HOST"))


@asynccontextmanager
async def screen(
    socket: WebSocket,
) -> AsyncIterator[tuple[None, asyncio.StreamReader, asyncio.StreamWriter]]:
    host = os.environ["SNOWLUMA_VNC_HOST"]
    port = int(os.environ.get("SNOWLUMA_VNC_PORT", "5900"))
    async with asyncio.timeout(10):
        reader, writer = await asyncio.open_connection(host, port)
    try:
        async with asyncio.timeout(15):
            if await reader.readexactly(12) != VERSION:
                raise RfbProtocolError("unsupported_snowluma_rfb")
            writer.write(VERSION)
            count = (await reader.readexactly(1))[0]
            if 2 not in await reader.readexactly(count):
                raise RfbProtocolError("snowluma_vnc_auth_required")
            writer.write(b"\x02")
            challenge = await reader.readexactly(16)
            password = os.environ.get("SNOWLUMA_VNC_PASSWORD", "").encode()[:8].ljust(8, b"\0")
            # VNC reverses each key byte's bits. Repeated DES keys give single DES.
            key = bytes(int(f"{byte:08b}"[::-1], 2) for byte in password)
            encrypt = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
            writer.write(encrypt.update(challenge) + encrypt.finalize())
            if await reader.readexactly(4) != bytes(4):
                raise RfbProtocolError("snowluma_vnc_auth_failed")
            # This side already authenticated the paired device and one-use display ticket.
            await socket.send_bytes(VERSION)
            if await socket.receive_bytes() != VERSION:
                raise RfbProtocolError("unsupported_rfb_handshake")
            await socket.send_bytes(b"\x01\x01")
            if await socket.receive_bytes() != b"\x01":
                raise RfbProtocolError("unsupported_rfb_security")
            await socket.send_bytes(bytes(4))
        yield None, reader, writer
    finally:
        writer.close()
        await writer.wait_closed()
