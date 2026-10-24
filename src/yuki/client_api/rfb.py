"""RFB 3.8 client stream filtering for the pinned noVNC wire protocol.

Only headers and the protocol-bounded SetEncodings list are buffered. Clipboard
bodies are forwarded or skipped incrementally, including signed 32-bit lengths.
"""

from __future__ import annotations

import struct

CAPS = 1 << 24
REQUEST = 1 << 25
PEEK = 1 << 26
NOTIFY = 1 << 27
PROVIDE = 1 << 28


class RfbProtocolError(ValueError):
    pass


class RfbInputFilter:
    def __init__(self) -> None:
        self.holder = False
        self._handshake = [12, 1, 1]
        self._header = bytearray()
        self._remaining = 0
        self._forward = False
        self._release_pending = False
        self._keys: set[int] = set()
        self._extended: set[tuple[int, int]] = set()
        self._pointer: tuple[int, int] | None = None

    def set_holder(self, holder: bool) -> bytes:
        if self.holder and not holder:
            self._release_pending = True
        self.holder = holder
        return self._release() if not self._remaining else b""

    def _release(self) -> bytes:
        if not self._release_pending:
            return b""
        self._release_pending = False
        output = bytearray()
        for key in sorted(self._keys):
            output.extend(struct.pack(">BBHI", 4, 0, 0, key))
        for key, code in sorted(self._extended):
            output.extend(struct.pack(">BBHII", 255, 0, 0, key, code))
        if self._pointer is not None:
            output.extend(struct.pack(">BBHH", 5, 0, *self._pointer))
        self._keys.clear()
        self._extended.clear()
        self._pointer = None
        return bytes(output)

    def close(self) -> bytes:
        self._release_pending = True
        # An incomplete forwarded body has no following message boundary. Close
        # that upstream connection; never place key bytes inside its clipboard.
        return self._release() if not self._remaining else b""

    def _need(self) -> int:
        if self._handshake:
            return self._handshake[0]
        if not self._header:
            return 1
        kind = self._header[0]
        if kind in {250} or kind not in {0, 2, 3, 4, 5, 6, 150, 248, 251, 255}:
            raise RfbProtocolError("unsupported_rfb_message")
        if kind == 2:
            return (
                4 if len(self._header) < 4 else 4 + 4 * struct.unpack_from(">H", self._header, 2)[0]
            )
        if kind == 6:
            if len(self._header) < 8:
                return 8
            length = struct.unpack_from(">i", self._header, 4)[0]
            return 12 if length <= -4 else 8
        if kind == 248:
            return 9 if len(self._header) < 9 else 9 + self._header[8]
        if kind == 255:
            if len(self._header) < 2:
                return 2
            if self._header[1] != 0:
                raise RfbProtocolError("unsupported_qemu_message")
            return 12
        return {0: 20, 3: 10, 4: 8, 5: 6, 150: 10, 251: 8}[kind]

    def _start(self) -> bytes:
        header = bytes(self._header)
        self._header.clear()
        if self._handshake:
            phase = len(self._handshake)
            if (
                (phase == 3 and header != b"RFB 003.008\n")
                or (phase == 2 and header != b"\x01")
                or (phase == 1 and header not in (b"\x00", b"\x01"))
            ):
                raise RfbProtocolError("unsupported_rfb_handshake")
            self._handshake.pop(0)
            return header
        kind = header[0]
        self._forward = self.holder or kind not in {4, 5, 6, 251, 255}
        if kind == 2:
            count = struct.unpack_from(">H", header, 2)[0]
            encodings = struct.unpack_from(f">{count}i", header, 4)
            filtered = [value for value in encodings if value not in {-316, -309}]
            return struct.pack(">BBH", 2, header[1], len(filtered)) + struct.pack(
                f">{len(filtered)}i", *filtered
            )
        if kind == 6:
            length = struct.unpack_from(">i", header, 4)[0]
            self._remaining = abs(length)
            if length <= -4:
                flags = struct.unpack_from(">I", header, 8)[0]
                # The actual noVNC capabilities response is 0x1f000001. Caps
                # must win over Notify/Provide, which here declare capabilities.
                self._forward = (
                    self.holder
                    or bool(flags & CAPS)
                    or (bool(flags & (REQUEST | PEEK)) and not flags & (NOTIFY | PROVIDE))
                )
                self._remaining -= 4
        if kind == 251:
            self._remaining = 16 * header[6]
        if self._forward and kind == 4:
            key = struct.unpack_from(">I", header, 4)[0]
            self._keys.add(key) if header[1] else self._keys.discard(key)
        if self._forward and kind == 255:
            down, key, code = struct.unpack_from(">HII", header, 2)
            self._extended.add((key, code)) if down else self._extended.discard((key, code))
        if self._forward and kind == 5:
            self._pointer = struct.unpack_from(">HH", header, 2)
        return header if self._forward else b""

    def feed(self, data: bytes) -> bytes:
        output = bytearray()
        position = 0
        while position < len(data):
            if self._remaining:
                size = min(self._remaining, len(data) - position)
                if self._forward:
                    output.extend(data[position : position + size])
                position += size
                self._remaining -= size
                if not self._remaining:
                    output.extend(self._release())
                continue
            output.extend(self._release())
            need = self._need()
            size = min(need - len(self._header), len(data) - position)
            self._header.extend(data[position : position + size])
            position += size
            # A header can reveal its own additional length (encodings, flags,
            # fence payload). Recalculate before committing any of its bytes.
            if len(self._header) == self._need():
                output.extend(self._start())
        return bytes(output)
