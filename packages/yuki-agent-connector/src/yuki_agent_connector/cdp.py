"""Long-lived Chrome DevTools Protocol driver; standard library only.

One browser-level WebSocket stays open for the driver's lifetime. Every page and
out-of-process iframe is auto-attached as a flat session, so dialogs, file
choosers, downloads and new tabs are observed as they happen instead of being
reconstructed per call. The driver knows nothing about who may act: the caller
passes ``before_input``, which runs before every state-changing protocol call.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import select
import socket
import struct
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast


class Failure(Exception):
    """A business failure; ``result`` is the complete tool result."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(result["error"])
        self.result = result


@dataclass(frozen=True)
class Image:
    data: bytes
    media_type: str
    width: int
    height: int


def image_scale(width: int, height: int, max_dimension: int, max_pixels: int) -> float:
    """Scale from source pixels to the returned image; input coordinates use its inverse."""
    return min(1.0, max_dimension / max(width, height), math.sqrt(max_pixels / (width * height)))


class WebSocket:
    """Minimal RFC 6455 client for one loopback CDP connection."""

    def __init__(self, url: str, timeout: float) -> None:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname or "127.0.0.1", parts.port or 80
        self.socket = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.socket.sendall(
            (
                f"GET {parts.path or '/'} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        received = bytearray()
        while b"\r\n\r\n" not in received:
            chunk = self.socket.recv(65536)
            if not chunk:
                raise ConnectionError("CDP closed during the WebSocket handshake")
            received += chunk
        head, _, rest = bytes(received).partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if status.split(b" ", 2)[1] != b"101":
            raise ConnectionError(status.decode(errors="replace"))
        self.buffer = bytearray(rest)
        self.fragments = bytearray()
        # Every later wait is bounded by select(); the socket itself stays blocking.
        self.socket.settimeout(None)

    def send(self, text: str) -> None:
        self._send(0x1, text.encode())

    def _send(self, opcode: int, payload: bytes) -> None:
        size = len(payload)
        header = bytearray([0x80 | opcode])
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", size)
        mask = os.urandom(4)
        masked = (
            int.from_bytes(payload, "big") ^ int.from_bytes((mask * (size // 4 + 1))[:size], "big")
        ).to_bytes(size, "big")
        self.socket.sendall(bytes(header) + mask + masked)

    def receive(self, deadline: float) -> str | None:
        """One complete text message, or None once the deadline passes first."""
        while True:
            frame = self._frame()
            if frame is None:
                remaining = max(0.0, deadline - time.monotonic())
                if not select.select([self.socket], [], [], remaining)[0]:
                    return None
                chunk = self.socket.recv(1 << 20)
                if not chunk:
                    raise ConnectionError("CDP WebSocket closed")
                self.buffer += chunk
                continue
            final, opcode, payload = frame
            if opcode == 0x9:
                self._send(0xA, payload)
            elif opcode == 0x8:
                raise ConnectionError("CDP WebSocket closed")
            elif opcode in (0x0, 0x1, 0x2):
                self.fragments += payload
                if final:
                    text = self.fragments.decode()
                    self.fragments = bytearray()
                    return text

    def _frame(self) -> tuple[bool, int, bytes] | None:
        # A frame is consumed only once complete, so an expired wait never
        # leaves the stream half-read.
        buffer = self.buffer
        if len(buffer) < 2:
            return None
        size, offset = buffer[1] & 0x7F, 2
        if size == 126:
            if len(buffer) < 4:
                return None
            size, offset = struct.unpack_from("!H", buffer, 2)[0], 4
        elif size == 127:
            if len(buffer) < 10:
                return None
            size, offset = struct.unpack_from("!Q", buffer, 2)[0], 10
        if len(buffer) < offset + size:
            return None
        payload = bytes(buffer[offset : offset + size])
        final, opcode = bool(buffer[0] & 0x80), buffer[0] & 0x0F
        del buffer[: offset + size]
        return final, opcode, payload

    def close(self) -> None:
        try:
            self._send(0x8, b"")
        finally:
            self.socket.close()


class _Expired(Exception):
    """The request's wait budget ran out."""


class _Interrupted(Exception):
    """A JavaScript dialog opened on the tab being acted on."""


class _ProtocolError(Exception):
    pass


@dataclass
class _Session:
    id: str
    target: str
    tab: str
    parent: str | None
    # Backend node IDs handed out for the session's current document. They are
    # unique only within one renderer process, and a navigation may move the
    # target to a new process that counts from 1 again, so an ID from an
    # earlier document could name an unrelated element of the new one.
    issued: set[int] = field(default_factory=set)


@dataclass
class _Tab:
    id: str
    session: str
    info: dict[str, Any]
    owner: str | None = None
    used: int = 0
    loading: bool = False
    # A main-frame navigation was asked for and has not started loading yet.
    requested: bool = False
    # Loader of the main frame's committed document, and of the navigation
    # this request started and waits to see committed. Loading events carry no
    # loader, and the previous document's stop can arrive after a new start.
    loader: str | None = None
    expected: str | None = None
    stops: int = 0
    dialog: dict[str, Any] | None = None
    dialog_session: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Frame:
    session: _Session
    nodes: dict[str, dict[str, Any]]
    root: dict[str, Any]
    owners: dict[int, _Frame]


_DIALOG_ACTIONS = {"accept_dialog", "dismiss_dialog"}
_DROPPED_ROLES = {"InlineTextBox", "LineBreak", "ListMarker"}
_FLAGS = ("expanded", "selected", "disabled", "focused", "required")
_NODE = re.compile(r"(?:([0-9a-f]{8})_)?e(\d+)")
_MODIFIERS = {
    "alt": 1,
    "option": 1,
    "ctrl": 2,
    "control": 2,
    "meta": 4,
    "cmd": 4,
    "command": 4,
    "shift": 8,
}
_KEYS: dict[str, tuple[str, int, str | None]] = {
    "Enter": ("Enter", 13, "\r"),
    "Tab": ("Tab", 9, None),
    "Backspace": ("Backspace", 8, None),
    "Delete": ("Delete", 46, None),
    "Escape": ("Escape", 27, None),
    " ": ("Space", 32, " "),
    "ArrowLeft": ("ArrowLeft", 37, None),
    "ArrowUp": ("ArrowUp", 38, None),
    "ArrowRight": ("ArrowRight", 39, None),
    "ArrowDown": ("ArrowDown", 40, None),
    "Home": ("Home", 36, None),
    "End": ("End", 35, None),
    "PageUp": ("PageUp", 33, None),
    "PageDown": ("PageDown", 34, None),
    "Insert": ("Insert", 45, None),
    **{f"F{number}": (f"F{number}", 111 + number, None) for number in range(1, 13)},
}
_KEY_NAMES = {
    "Return": "Enter",
    "BackSpace": "Backspace",
    "Esc": "Escape",
    "Space": " ",
    "Left": "ArrowLeft",
    "Up": "ArrowUp",
    "Right": "ArrowRight",
    "Down": "ArrowDown",
}
# Resolves once the page has handled what was already queued: a frame when the
# page renders (rAF does not run in hidden pages), then one task. Message
# events are not subject to background timer throttling.
_ROUND_TRIP = (
    "new Promise(resolve => { const channel = new MessageChannel();"
    " channel.port1.onmessage = () => resolve();"
    " const post = () => channel.port2.postMessage(0);"
    " document.hidden ? post() : requestAnimationFrame(post); })"
)
_PROBE = (
    "function() { if (!this.isConnected) return 'stale';"
    " return this instanceof HTMLInputElement && this.type === 'file' ? 'file' : 'text'; }"
)
_SELECT_CONTENT = (
    "function() { if (this instanceof HTMLInputElement || this instanceof HTMLTextAreaElement)"
    " { this.select(); return; }"
    " const range = document.createRange(); range.selectNodeContents(this);"
    " const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range); }"
)
_FIND_OPTION = (
    "function(text) { if (!this.isConnected) return {stale: true};"
    " if (!(this instanceof HTMLSelectElement)) return {index: -1, options: []};"
    " const options = [...this.options];"
    " return {index: options.findIndex(option => option.label === text"
    " || option.value === text || option.text.trim() === text),"
    " options: options.map(option => option.label)}; }"
)
_CHOOSE_OPTION = (
    "function(index) { this.options[index].selected = true;"
    " this.dispatchEvent(new Event('input', {bubbles: true}));"
    " this.dispatchEvent(new Event('change', {bubbles: true})); }"
)


class Browser:
    """One Chromium instance reached through its loopback DevTools endpoint."""

    def __init__(self, endpoint: str, *, downloads: str, timeout: float) -> None:
        # The debugger is on loopback; an environment proxy must never carry it.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(endpoint.rstrip("/") + "/json/version", timeout=timeout) as response:
            address = json.load(response)["webSocketDebuggerUrl"]
        self.socket = WebSocket(address, timeout)
        self.downloads = downloads
        self._sequence = 0
        self._waiting: int | None = None
        self._response: dict[str, Any] | None = None
        self._deadline = time.monotonic() + timeout
        self._watch: _Tab | None = None
        self._before: Callable[[], None] = _no_check
        self._inputs = 0
        self._clock = 0
        self._targets: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, _Session] = {}
        self._tabs: dict[str, _Tab] = {}
        self._frames: dict[str, str] = {}
        self._downloads: dict[str, tuple[str, str]] = {}
        os.makedirs(downloads, exist_ok=True)
        self._call("Target.setDiscoverTargets", {"discover": True})
        self._call(
            "Browser.setDownloadBehavior",
            {"behavior": "allowAndName", "downloadPath": downloads, "eventsEnabled": True},
        )
        # New targets wait for the debugger, so interception and dialog events
        # are in place before their first script runs.
        self._call(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
        )

    def close(self) -> None:
        self.socket.close()

    def pump(self, timeout: float = 0.0) -> None:
        """Handle events that arrive within ``timeout`` seconds; never blocks longer."""
        deadline = time.monotonic() + timeout
        while (text := self.socket.receive(deadline)) is not None:
            self._dispatch(json.loads(text))

    def tabs(self) -> list[dict[str, Any]]:
        return [
            {
                "id": tab.id,
                "url": tab.info.get("url", ""),
                "title": tab.info.get("title", ""),
                **({"work_id": tab.owner} if tab.owner is not None else {}),
            }
            for tab in self._tabs.values()
        ]

    def execute(
        self,
        args: dict[str, Any],
        *,
        owner: str | None,
        before_input: Callable[[], None],
        max_dimension: int,
        max_pixels: int,
        wait_seconds: float,
    ) -> tuple[dict[str, Any], Image | None]:
        self._deadline = time.monotonic() + wait_seconds
        self._before = before_input
        self._inputs = 0
        try:
            self.pump()
            return self._execute(args, owner, max_dimension, max_pixels)
        except Failure as failure:
            if self._inputs and failure.result["error"] != "node_stale":
                failure.result["status"] = "unknown"
            raise
        except _Expired:
            raise Failure(
                {
                    "error": "timeout",
                    "message": "瀏覽器在等待時間內沒有回應。",
                    **({"status": "unknown"} if self._inputs else {}),
                }
            ) from None
        except _ProtocolError as error:
            raise Failure(
                {
                    "error": "cdp_error",
                    "message": str(error),
                    **({"status": "unknown"} if self._inputs else {}),
                }
            ) from None
        except OSError as error:
            raise Failure(
                {
                    "error": "browser_unavailable",
                    "message": f"無法連線到瀏覽器：{error}",
                    **({"status": "unknown"} if self._inputs else {}),
                }
            ) from None
        finally:
            self._watch = None
            self._before = _no_check

    # Request handling

    def _execute(
        self, args: dict[str, Any], owner: str | None, max_dimension: int, max_pixels: int
    ) -> tuple[dict[str, Any], Image | None]:
        action = args["action"]
        if action == "open" and not args.get("tab"):
            tab = self._new_tab(owner)
        else:
            tab = self._resolve(args.get("tab"), owner)
        self._clock += 1
        tab.used = self._clock
        if tab.dialog is not None and action not in _DIALOG_ACTIONS:
            raise Failure(
                {
                    "error": "dialog_open",
                    "message": "此分頁有尚未處理的對話框；請先 accept_dialog 或 dismiss_dialog。",
                    "dialog": tab.dialog,
                    "tab": self._info(tab),
                }
            )
        if action in _DIALOG_ACTIONS and tab.dialog is None:
            raise Failure(
                {"error": "no_dialog", "message": "此分頁目前沒有對話框。", "tab": self._info(tab)}
            )
        self._watch = tab
        try:
            if action == "close":
                self._input()
                self._call("Target.closeTarget", {"targetId": tab.id})
                # Closed means gone: a later call must not reach a dying target.
                # A beforeunload dialog keeps it open; that is reported instead.
                while tab.id in self._tabs:
                    if tab.dialog is not None:
                        raise _Interrupted
                    self._receive()
                return {"tab": {"id": tab.id, "closed": True}, "events": []}, None
            if action == "snapshot":
                return self._report(tab, args.get("text") or None, settled=False), None
            if action not in _DIALOG_ACTIONS:
                # A background tab renders no frames, so its input and captures
                # stall; acting on a tab shows it, as a person would switch to it.
                self._call("Page.bringToFront", {}, tab.session)
            if action == "screenshot":
                image = self._screenshot(tab, max_dimension, max_pixels)
                report = self._report(tab, None, settled=False)
                return {**report, "width": image.width, "height": image.height}, image
            # A navigation an earlier request saw asked for but never saw start
            # (cancelled, 204) is not this action's to wait for.
            tab.requested, tab.expected = False, None
            stops = tab.stops
            self._act(tab, action, args)
            self._settle(tab, stops)
        except _Interrupted:
            # The action opened a dialog; it is reported as an event.
            pass
        return self._report(tab, None, settled=True), None

    def _act(self, tab: _Tab, action: str, args: dict[str, Any]) -> None:
        text = args.get("text")
        node = args.get("node")
        if action in _DIALOG_ACTIONS:
            params: dict[str, Any] = {"accept": action == "accept_dialog"}
            if action == "accept_dialog" and text is not None:
                params["promptText"] = text
            self._input()
            self._call("Page.handleJavaScriptDialog", params, tab.dialog_session)
            tab.dialog = None
        elif action == "open":
            self._input()
            try:
                started = self._call(
                    "Page.navigate", {"url": args.get("url") or "about:blank"}, tab.session
                )
            except _Expired:
                # The server has not answered within the budget: a slow load,
                # reported as loading rather than as a failed call.
                tab.requested = True
                return
            # The reply can precede the page's loading events; until the new
            # document commits, the navigation is still to come.
            if started.get("loaderId") and not started.get("isDownload"):
                if tab.loader != started["loaderId"]:
                    tab.expected = started["loaderId"]
        elif action in ("click", "hover"):
            x, y = self._point(tab, self._required(tab, node))
            self._mouse(tab, "mouseMoved", x, y)
            if action == "click":
                self._mouse(tab, "mousePressed", x, y)
                self._mouse(tab, "mouseReleased", x, y)
        elif action == "type":
            self._type(tab, node, text or "")
        elif action == "press":
            target = tab.session
            if node is not None:
                session, backend = self._element(tab, node)
                self._input()
                self._call("DOM.focus", {"backendNodeId": backend}, session.id)
                target = session.id
            self._press(tab, target, text or "")
        elif action == "select":
            self._select(tab, self._required(tab, node), text or "")
        elif action == "scroll":
            self._scroll(tab, node, text)
        else:
            raise Failure({"error": "invalid_arguments", "message": f"未知的動作：{action}"})

    def _required(self, tab: _Tab, node: str | None) -> str:
        if node is None:
            raise Failure(
                {
                    "error": "invalid_arguments",
                    "message": "此動作需要 node（快照中的節點 ID）。",
                    "tab": self._info(tab),
                }
            )
        return node

    def _type(self, tab: _Tab, node: str | None, text: str) -> None:
        # Text and keys for an element go to its own frame's session: the
        # browser learns of focus moving into an out-of-process frame only
        # asynchronously, so the page session could still deliver elsewhere.
        target = tab.session
        if node is not None:
            session, backend = self._element(tab, node)
            kind = self._function(session, backend, _PROBE)
            if kind == "stale":
                raise self._stale(tab)
            if kind == "file":
                self._input()
                self._call(
                    "DOM.setFileInputFiles",
                    {
                        "files": [line for line in text.split("\n") if line],
                        "backendNodeId": backend,
                    },
                    session.id,
                )
                return
            self._input()
            self._call("DOM.focus", {"backendNodeId": backend}, session.id)
            self._input()
            self._function(session, backend, _SELECT_CONTENT)
            target = session.id
        if text:
            self._input()
            self._call("Input.insertText", {"text": text}, target)
        else:
            self._key(target, "Backspace", 0)

    def _press(self, tab: _Tab, target: str, combo: str) -> None:
        *modifiers, key = combo.split("+") if combo != "+" else ["+"]
        mask = 0
        for modifier in modifiers:
            if modifier.strip().lower() not in _MODIFIERS:
                raise Failure(
                    {
                        "error": "invalid_arguments",
                        "message": f"未知的修飾鍵：{modifier}（可用 ctrl、shift、alt、meta）",
                        "tab": self._info(tab),
                    }
                )
            mask |= _MODIFIERS[modifier.strip().lower()]
        self._key(target, key, mask)

    def _key(self, target: str, key: str, modifiers: int) -> None:
        key = _KEY_NAMES.get(key, key)
        if key in _KEYS:
            code, virtual, text = _KEYS[key]
        elif len(key) == 1:
            upper = key.upper()
            code = "Key" + upper if "A" <= upper <= "Z" else "Digit" + key if key.isdigit() else ""
            virtual, text = (ord(upper) if code else 0), key
        else:
            code, virtual, text = key, 0, None
        if modifiers & ~8:
            # Ctrl/Alt/Meta chords are shortcuts, not text.
            text = None
        event: dict[str, Any] = {
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": virtual,
            "modifiers": modifiers,
        }
        self._input()
        self._call(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown" if text else "rawKeyDown",
                **event,
                **({"text": text, "unmodifiedText": text} if text else {}),
            },
            target,
        )
        self._input()
        self._call("Input.dispatchKeyEvent", {"type": "keyUp", **event}, target)

    def _select(self, tab: _Tab, node: str, text: str) -> None:
        session, backend = self._element(tab, node)
        found = self._function(session, backend, _FIND_OPTION, text)
        if found.get("stale"):
            raise self._stale(tab)
        if found["index"] < 0:
            raise Failure(
                {
                    "error": "option_not_found",
                    "message": f"找不到選項：{text}",
                    "options": found["options"],
                    "tab": self._info(tab),
                }
            )
        self._input()
        self._function(session, backend, _CHOOSE_OPTION, found["index"])

    def _scroll(self, tab: _Tab, node: str | None, text: str | None) -> None:
        if node is not None:
            session, backend = self._element(tab, node)
            self._input()
            self._scroll_into_view(tab, session, backend)
            return
        viewport = self._call("Page.getLayoutMetrics", {}, tab.session)["cssVisualViewport"]
        width, height = viewport["clientWidth"], viewport["clientHeight"]
        if text in (None, "", "down", "up"):
            # One page step keeps a little of the previous view for orientation;
            # it is the size of a step, not a completion condition.
            delta = height * (-0.8 if text == "up" else 0.8)
        else:
            try:
                delta = float(cast(str, text))
            except ValueError:
                raise Failure(
                    {
                        "error": "invalid_arguments",
                        "message": "scroll 的 text 須為 down、up 或像素數。",
                        "tab": self._info(tab),
                    }
                ) from None
        self._input()
        self._call(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": width / 2, "y": height / 2, "deltaX": 0, "deltaY": delta},
            tab.session,
        )

    def _mouse(self, tab: _Tab, kind: str, x: float, y: float) -> None:
        params: dict[str, Any] = {"type": kind, "x": x, "y": y}
        if kind != "mouseMoved":
            params.update(button="left", buttons=1 if kind == "mousePressed" else 0, clickCount=1)
        self._input()
        self._call("Input.dispatchMouseEvent", params, tab.session)

    def _point(self, tab: _Tab, node: str) -> tuple[float, float]:
        session, backend = self._element(tab, node)
        self._input()
        self._scroll_into_view(tab, session, backend)
        try:
            quads = self._call("DOM.getContentQuads", {"backendNodeId": backend}, session.id)
        except _ProtocolError:
            raise self._stale(tab) from None
        if not quads["quads"]:
            raise self._stale(tab)
        quad = quads["quads"][0]
        x, y = sum(quad[0::2]) / 4, sum(quad[1::2]) / 4
        # An out-of-process frame reports its own viewport; add each owner's
        # content box in its parent until the top page, where input is dispatched.
        while session.parent is not None:
            parent = self._sessions[session.parent]
            owner = self._call("DOM.getFrameOwner", {"frameId": session.target}, parent.id)
            content = self._call(
                "DOM.getBoxModel", {"backendNodeId": owner["backendNodeId"]}, parent.id
            )["model"]["content"]
            x, y = x + content[0], y + content[1]
            session = parent
        return x, y

    def _scroll_into_view(self, tab: _Tab, session: _Session, backend: int) -> None:
        try:
            self._call("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend}, session.id)
        except _ProtocolError:
            raise self._stale(tab) from None

    def _element(self, tab: _Tab, node: str) -> tuple[_Session, int]:
        match = _NODE.fullmatch(node)
        if match is None:
            raise self._stale(tab)
        prefix, backend = match.group(1), int(match.group(2))
        if prefix is None:
            session = self._sessions[tab.session]
        else:
            candidates = [
                value
                for value in self._sessions.values()
                if value.tab == tab.id
                and value.parent is not None
                and value.target.lower().startswith(prefix)
            ]
            if len(candidates) != 1:
                raise self._stale(tab)
            session = candidates[0]
        if backend not in session.issued:
            raise self._stale(tab)
        try:
            self._call("DOM.describeNode", {"backendNodeId": backend}, session.id)
        except _ProtocolError:
            raise self._stale(tab) from None
        return session, backend

    def _function(self, session: _Session, backend: int, declaration: str, *arguments: Any) -> Any:
        handle = self._call("DOM.resolveNode", {"backendNodeId": backend}, session.id)["object"]
        try:
            result = self._call(
                "Runtime.callFunctionOn",
                {
                    "objectId": handle["objectId"],
                    "functionDeclaration": declaration,
                    "arguments": [{"value": value} for value in arguments],
                    "returnByValue": True,
                },
                session.id,
            )
        finally:
            # The driver outlives pages; do not leave a handle per action behind.
            self._send("Runtime.releaseObject", {"objectId": handle["objectId"]}, session.id)
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise _ProtocolError(details.get("exception", {}).get("description") or details["text"])
        return result["result"].get("value")

    def _stale(self, tab: _Tab) -> Failure:
        return Failure(
            {
                "error": "node_stale",
                "message": "節點已不存在或不可見；請依新快照重新選擇。",
                "tab": self._info(tab),
                "snapshot": self._snapshot(tab, None),
            }
        )

    def _input(self) -> None:
        self._before()
        self._inputs += 1

    def _settle(self, tab: _Tab, stops: int) -> None:
        """Wait until the page has handled the action and any navigation it began."""
        try:
            try:
                self._call(
                    "Runtime.evaluate",
                    {"expression": _ROUND_TRIP, "awaitPromise": True},
                    tab.session,
                )
            except _ProtocolError:
                # The document that ran the action navigated away or closed.
                pass
            while (
                _pending(tab) and tab.stops == stops and tab.dialog is None and tab.id in self._tabs
            ):
                self._receive()
        except (_Expired, _Interrupted):
            pass

    def _report(self, tab: _Tab, needle: str | None, *, settled: bool) -> dict[str, Any]:
        """Tab, events since the last request on it, and a snapshot when one can be read.

        No snapshot while a dialog blocks the page, nor after an action whose
        navigation is still loading (it would describe a half-built page); the
        snapshot action itself reads whatever is there and says ``loading``.
        """
        snapshot: str | None = None
        try:
            if tab.id in self._tabs:
                # Title changes reach target events late; read the current values.
                tab.info = self._call("Target.getTargetInfo", {"targetId": tab.id})["targetInfo"]
            unfinished = tab.loading or tab.expected is not None
            if tab.id in self._tabs and tab.dialog is None and not (settled and unfinished):
                snapshot = self._snapshot(tab, needle)
        except _Interrupted:
            pass
        except _Expired:
            if not settled:
                raise
        result: dict[str, Any] = {
            "tab": self._info(tab),
            "events": [self._render_event(event) for event in tab.events],
        }
        tab.events.clear()
        if snapshot is not None:
            result["snapshot"] = snapshot
        return result

    def _render_event(self, event: dict[str, Any]) -> dict[str, Any]:
        if event["type"] != "new_tab":
            return event
        opened = self._tabs.get(event["tab"])
        info = opened.info if opened else self._targets.get(event["tab"], {})
        return {
            "type": "new_tab",
            "tab": {"id": event["tab"], "url": info.get("url", ""), "title": info.get("title", "")},
        }

    def _info(self, tab: _Tab) -> dict[str, Any]:
        return {
            "id": tab.id,
            "url": tab.info.get("url", ""),
            "title": tab.info.get("title", ""),
            "loading": _pending(tab),
        }

    def _new_tab(self, owner: str | None) -> _Tab:
        self._input()
        target = self._call("Target.createTarget", {"url": "about:blank"})["targetId"]
        while target not in self._tabs:
            self._receive()
        tab = self._tabs[target]
        tab.owner = owner
        # The attach setup was sent without waiting. A navigation that moves the
        # page to another process before its renderer enabled Page events would
        # never report its commit or load, so wait for that enable to finish
        # (a repeated enable answers after the first).
        self._call("Page.enable", {}, tab.session)
        return tab

    def _resolve(self, identity: str | None, owner: str | None) -> _Tab:
        if identity:
            tab = self._tabs.get(identity)
        else:
            owned = [tab for tab in self._tabs.values() if owner is not None and tab.owner == owner]
            tab = max(owned, key=lambda value: value.used) if owned else None
        if tab is None:
            raise Failure(
                {
                    "error": "tab_not_found",
                    "message": "找不到這個分頁；可用 open 開新分頁或從清單選擇。",
                    "tabs": self.tabs(),
                }
            )
        return tab

    def _screenshot(self, tab: _Tab, max_dimension: int, max_pixels: int) -> Image:
        metrics = self._call("Page.getLayoutMetrics", {}, tab.session)
        css = metrics["cssVisualViewport"]
        # Device pixels are the source image; the clip is in CSS pixels and
        # Chromium multiplies its scale by the device scale factor.
        width, height = (
            metrics["visualViewport"]["clientWidth"],
            metrics["visualViewport"]["clientHeight"],
        )
        scale = image_scale(width, height, max_dimension, max_pixels)
        shot = self._call(
            "Page.captureScreenshot",
            {
                "format": "jpeg",
                "quality": 80,
                "clip": {
                    "x": css["pageX"],
                    "y": css["pageY"],
                    "width": css["clientWidth"],
                    "height": css["clientHeight"],
                    "scale": scale,
                },
            },
            tab.session,
        )
        data = base64.b64decode(shot["data"])
        actual_width, actual_height = jpeg_size(data)
        return Image(data, "image/jpeg", actual_width, actual_height)

    # Snapshot

    def _snapshot(self, tab: _Tab, needle: str | None) -> str:
        frame = self._document(self._sessions[tab.session])
        title = frame.root.get("name", {}).get("value", "")
        if title:
            # The document's own title; the browser's copy can lag behind it.
            tab.info = {**tab.info, "title": title}
        lines: list[tuple[int, str]] = []
        self._render_children(frame, frame.root.get("childIds", []), 0, lines)
        if needle is not None:
            lines = _filter(lines, needle.lower())
        title, url = tab.info.get("title", ""), tab.info.get("url", "")
        return "\n".join(
            [f"page: {title} — {url} [{tab.id}]", *("  " * depth + line for depth, line in lines)]
        )

    def _document(self, session: _Session) -> _Frame:
        """A session's root document with every frame it owns inlined under its owner.

        Same-process frames are read from this session by frame ID; each
        out-of-process frame is read from its own session. Owner elements are
        keyed by backend node ID, which is unique within the renderer process.
        """
        tree = self._call("Page.getFrameTree", {}, session.id)["frameTree"]
        owners: dict[int, _Frame] = {}
        root = self._tree(session, tree["frame"]["id"], owners)
        children: list[tuple[str, _Session | None]] = [
            (frame, None) for frame in _descendants(tree)
        ]
        children += [
            (child.target, child)
            for child in list(self._sessions.values())
            if child.parent == session.id
        ]
        for frame_id, child in children:
            try:
                owner = self._call("DOM.getFrameOwner", {"frameId": frame_id}, session.id)
                owners[owner["backendNodeId"]] = (
                    self._tree(session, frame_id, owners)
                    if child is None
                    else self._document(child)
                )
            except _ProtocolError:
                # The frame detached while the page was being read.
                continue
        return root

    def _tree(self, session: _Session, frame_id: str, owners: dict[int, _Frame]) -> _Frame:
        nodes = {
            node["nodeId"]: node
            for node in self._call(
                "Accessibility.getFullAXTree", {"frameId": frame_id}, session.id
            )["nodes"]
        }
        root = next(node for node in nodes.values() if "parentId" not in node)
        return _Frame(session, nodes, root, owners)

    def _render_children(
        self, frame: _Frame, identities: list[str], depth: int, lines: list[tuple[int, str]]
    ) -> None:
        run: list[str] = []
        for kind, value in self._effective(frame, identities):
            if kind == "text":
                run.append(value)
                continue
            if run:
                lines.append((depth, "- text: " + _text(run)))
                run = []
            child_frame, node = value
            self._render(child_frame, node, depth, lines)
        if run:
            lines.append((depth, "- text: " + _text(run)))

    def _effective(self, frame: _Frame, identities: list[str]) -> list[tuple[str, Any]]:
        """Children after dropping layout-only nodes and promoting their children."""
        result: list[tuple[str, Any]] = []
        for identity in identities:
            node = frame.nodes.get(identity)
            if node is None:
                continue
            role = node.get("role", {}).get("value", "")
            name = node.get("name", {}).get("value", "")
            if role in _DROPPED_ROLES:
                continue
            if role == "StaticText" and not node.get("ignored"):
                result.append(("text", name))
            elif node.get("ignored") or (role in ("generic", "none") and not name):
                result.extend(self._effective(frame, node.get("childIds", [])))
            else:
                result.append(("node", (frame, node)))
        return result

    def _render(
        self, frame: _Frame, node: dict[str, Any], depth: int, lines: list[tuple[int, str]]
    ) -> None:
        role = node.get("role", {}).get("value", "")
        name = str(node.get("name", {}).get("value", ""))
        value = node.get("value", {}).get("value")
        children = self._effective(frame, node.get("childIds", []))
        backend = node.get("backendDOMNodeId")
        child = frame.owners.get(backend) if backend is not None else None
        if child is not None:
            children += self._effective(child, child.root.get("childIds", []))
        line = "- " + role
        if name:
            line += " " + json.dumps(name, ensure_ascii=False)
        if value not in (None, ""):
            line += " value=" + json.dumps(str(value), ensure_ascii=False)
        properties = {
            item["name"]: item.get("value", {}).get("value") for item in node.get("properties", [])
        }
        checked = properties.get("checked")
        if checked in ("true", True):
            line += " checked"
        elif checked == "mixed":
            line += " checked=mixed"
        line += "".join(" " + flag for flag in _FLAGS if properties.get(flag) in (True, "true"))
        if role == "heading" and properties.get("level") is not None:
            line += f" level={properties['level']}"
        if backend is not None:
            line += f" [{self._node_id(frame.session, backend)}]"
        if children and all(kind == "text" for kind, _ in children):
            text = _text([text for _, text in children])
            if text and text not in (name, str(value)):
                line += ": " + text
            lines.append((depth, line))
            return
        lines.append((depth, line))
        run: list[str] = []
        for kind, item in children:
            if kind == "text":
                run.append(item)
                continue
            if run:
                lines.append((depth + 1, "- text: " + _text(run)))
                run = []
            self._render(item[0], item[1], depth + 1, lines)
        if run:
            lines.append((depth + 1, "- text: " + _text(run)))

    def _node_id(self, session: _Session, backend: int) -> str:
        session.issued.add(backend)
        if session.parent is None:
            return f"e{backend}"
        return f"{session.target[:8].lower()}_e{backend}"

    # Protocol

    def _send(self, method: str, params: dict[str, Any], session: str | None = None) -> int:
        self._sequence += 1
        message: dict[str, Any] = {"id": self._sequence, "method": method, "params": params}
        if session is not None:
            message["sessionId"] = session
        self.socket.send(json.dumps(message))
        return self._sequence

    def _call(
        self, method: str, params: dict[str, Any], session: str | None = None
    ) -> dict[str, Any]:
        # A JavaScript dialog blocks the page's renderer: no reply would come
        # until it closes, so stop waiting as soon as one opens.
        # Browser-level commands (no session) are answered by the browser process.
        dialog = (
            self._watch is not None
            and session is not None
            and method != "Page.handleJavaScriptDialog"
        )
        if dialog and cast(_Tab, self._watch).dialog is not None:
            raise _Interrupted
        if time.monotonic() >= self._deadline:
            raise _Expired
        self._waiting = self._send(method, params, session)
        self._response = None
        try:
            while self._response is None:
                self._receive()
                if dialog and cast(_Tab, self._watch).dialog is not None:
                    raise _Interrupted
        finally:
            self._waiting = None
        response = self._response
        if "error" in response:
            raise _ProtocolError(response["error"].get("message", str(response["error"])))
        return cast(dict[str, Any], response.get("result", {}))

    def _receive(self) -> None:
        text = self.socket.receive(self._deadline)
        if text is None:
            raise _Expired
        self._dispatch(json.loads(text))

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "id" in message:
            if message["id"] == self._waiting:
                self._response = message
            return
        method, params = message["method"], message.get("params", {})
        session = self._sessions.get(message.get("sessionId", ""))
        if method.startswith("Target."):
            self._target_event(method, params, message.get("sessionId"))
        elif method.startswith("Browser.download"):
            self._download_event(method, params)
        elif session is not None and session.tab in self._tabs:
            self._page_event(self._tabs[session.tab], session, method, params)

    def _target_event(self, method: str, params: dict[str, Any], parent: str | None) -> None:
        if method in ("Target.targetCreated", "Target.targetInfoChanged"):
            info = params["targetInfo"]
            self._targets[info["targetId"]] = info
            if info["targetId"] in self._tabs:
                self._tabs[info["targetId"]].info = info
        elif method == "Target.targetDestroyed":
            self._targets.pop(params["targetId"], None)
            self._tabs.pop(params["targetId"], None)
            self._frames.pop(params["targetId"], None)
        elif method == "Target.attachedToTarget":
            self._attached(params, parent)
        elif method == "Target.detachedFromTarget":
            session = self._sessions.pop(params["sessionId"], None)
            if session is not None and session.parent is None:
                self._tabs.pop(session.tab, None)

    def _attached(self, params: dict[str, Any], parent: str | None) -> None:
        identity, info = params["sessionId"], params["targetInfo"]
        target = info["targetId"]
        if info["type"] == "page" and parent is None:
            self._sessions[identity] = _Session(identity, target, target, None)
            tab = _Tab(target, identity, info)
            opener = self._tabs.get(info.get("openerId", ""))
            if opener is not None:
                # Ownership follows the opener only for listing and default
                # resolution; it never limits which tab a Work may use.
                tab.owner = opener.owner
                opener.events.append({"type": "new_tab", "tab": target})
                opener.stops += 1
            self._tabs[target] = tab
            self._frames[target] = target
        elif info["type"] == "iframe" and parent in self._sessions:
            owner = self._sessions[parent]
            self._sessions[identity] = _Session(identity, target, owner.tab, parent)
            self._frames[target] = owner.tab
        else:
            self._send("Runtime.runIfWaitingForDebugger", {}, identity)
            return
        # Sent without waiting: commands on a session run in order, so anything
        # the request sends later runs after this setup.
        self._send("Page.enable", {}, identity)
        self._send("Page.setInterceptFileChooserDialog", {"enabled": True}, identity)
        self._send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
            identity,
        )
        self._send("Runtime.runIfWaitingForDebugger", {}, identity)

    def _page_event(
        self, tab: _Tab, session: _Session, method: str, params: dict[str, Any]
    ) -> None:
        if method == "Page.frameStartedLoading" and params["frameId"] == tab.id:
            tab.loading, tab.requested = True, False
        elif method == "Page.frameStoppedLoading" and params["frameId"] == tab.id:
            tab.loading, tab.requested = False, False
        elif method == "Page.frameRequestedNavigation" and params["frameId"] == tab.id:
            tab.requested = True
        elif method == "Page.frameAttached":
            self._frames[params["frameId"]] = tab.id
        elif method == "Page.frameDetached":
            self._frames.pop(params["frameId"], None)
        elif method == "Page.frameNavigated":
            frame = params["frame"]
            if frame["id"] == session.target:
                session.issued.clear()
            if frame["id"] == tab.id:
                tab.loader = frame["loaderId"]
                if tab.expected == frame["loaderId"]:
                    # Committed; it loads until its own stop arrives.
                    tab.expected, tab.loading = None, True
                self._navigated(tab, frame["url"] + frame.get("urlFragment", ""))
        elif method == "Page.navigatedWithinDocument" and params["frameId"] == tab.id:
            self._navigated(tab, params["url"])
        elif method == "Page.javascriptDialogOpening":
            tab.dialog = {
                "type": "dialog",
                "dialog_type": params["type"],
                "message": params["message"],
                **(
                    {"default_prompt": params.get("defaultPrompt", "")}
                    if params["type"] == "prompt"
                    else {}
                ),
            }
            tab.dialog_session = session.id
            tab.events.append(tab.dialog)
            tab.stops += 1
        elif method == "Page.javascriptDialogClosed" and tab.dialog_session == session.id:
            tab.dialog = None
        elif method == "Page.fileChooserOpened":
            tab.events.append(
                {
                    "type": "file_chooser",
                    "node": self._node_id(session, params["backendNodeId"]),
                    "multiple": params["mode"] == "selectMultiple",
                }
            )
            tab.stops += 1

    def _navigated(self, tab: _Tab, url: str) -> None:
        if tab.events and tab.events[-1]["type"] == "navigated":
            tab.events.pop()
        tab.events.append({"type": "navigated", "url": url})

    def _download_event(self, method: str, params: dict[str, Any]) -> None:
        if method == "Browser.downloadWillBegin":
            tab_id = self._frames.get(params["frameId"])
            if tab_id is None or tab_id not in self._tabs:
                return
            self._downloads[params["guid"]] = (tab_id, params["suggestedFilename"])
            state = "inProgress"
        elif params["state"] == "inProgress" or params["guid"] not in self._downloads:
            return
        else:
            state = params["state"]
        tab_id, suggested = (
            self._downloads[params["guid"]]
            if state == "inProgress"
            else self._downloads.pop(params["guid"])
        )
        tab = self._tabs.get(tab_id)
        if tab is None:
            return
        if method == "Browser.downloadWillBegin" and params["frameId"] == tab.id:
            # The requested navigation became this download; no page will load.
            tab.requested = False
        tab.events[:] = [
            event
            for event in tab.events
            if not (event["type"] == "download" and event["guid"] == params["guid"])
        ]
        tab.events.append(
            {
                "type": "download",
                "guid": params["guid"],
                "suggested_filename": suggested,
                "path": os.path.join(self.downloads, params["guid"]),
                "state": state,
            }
        )
        tab.stops += 1


def _no_check() -> None:
    return None


def _pending(tab: _Tab) -> bool:
    return tab.loading or tab.requested or tab.expected is not None


def _descendants(tree: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for child in tree.get("childFrames", []):
        result.append(child["frame"]["id"])
        result += _descendants(child)
    return result


def _text(parts: list[str]) -> str:
    return " ".join(" ".join(parts).split())


def _filter(lines: list[tuple[int, str]], needle: str) -> list[tuple[int, str]]:
    """Matching lines plus the ancestor path of each."""
    keep: set[int] = set()
    path: list[int] = []
    for index, (depth, line) in enumerate(lines):
        while path and lines[path[-1]][0] >= depth:
            path.pop()
        path.append(index)
        if needle in line.lower():
            keep.update(path)
    return [line for index, line in enumerate(lines) if index in keep]


def jpeg_size(data: bytes) -> tuple[int, int]:
    """Width and height from a JPEG's start-of-frame segment."""
    offset = 2
    while True:
        marker = data[offset + 1]
        length = struct.unpack_from("!H", data, offset + 2)[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack_from("!HH", data, offset + 5)
            return width, height
        offset += 2 + length
