"""AT-SPI for Yuki's own computer through ctypes: standard library only, like the driver.

Mounted next to ``yuki_computer.py`` and imported only by its long-lived driver. GTK (and
Qt with its bridge) register every window on the session D-Bus that ``desktop_service.py``
starts; ``libatspi`` reads them (task book docs/architecture/Computer-Use任务书-2026-11-15.md
§6.7, P0 item 3). An X window is matched to its application through ``_NET_WM_PID`` and to
its frame by geometry. Chromium's web content is not exposed (its a11y stays off); web pages
use ``browser``.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from dataclasses import dataclass
from typing import Any

P = ctypes.c_void_p
# AtspiCoordType: screen coordinates are X root pixels, the driver's input space.
SCREEN = 0
STATE_DEFUNCT, STATE_SHOWING = 6, 25
# Each D-Bus call waits at most this long: a hung app must not stall the driver.
CALL_MILLISECONDS = 1000
CONTAINER_ROLES = frozenset(
    {"filler", "panel", "scroll pane", "viewport", "layered pane", "split pane", "unknown"}
)
PRESS_ACTIONS = ("click", "press", "activate", "jump")


class Unavailable(Exception):
    """No AT-SPI here (no bus, no library) or the window's application is not registered."""


class Gone(Exception):
    """The node no longer exists."""


class GError(ctypes.Structure):
    _fields_ = [("domain", ctypes.c_uint32), ("code", ctypes.c_int), ("message", ctypes.c_char_p)]


class Rect(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
    ]


ERROR = ctypes.POINTER(ctypes.POINTER(GError))
SIGNATURES: dict[str, tuple[Any, list[Any]]] = {
    "atspi_init": (ctypes.c_int, []),
    "atspi_set_timeout": (None, [ctypes.c_int, ctypes.c_int]),
    "atspi_get_desktop": (P, [ctypes.c_int]),
    "atspi_accessible_get_child_count": (ctypes.c_int, [P, ERROR]),
    "atspi_accessible_get_child_at_index": (P, [P, ctypes.c_int, ERROR]),
    "atspi_accessible_get_role_name": (P, [P, ERROR]),
    "atspi_accessible_get_name": (P, [P, ERROR]),
    "atspi_accessible_get_process_id": (ctypes.c_uint, [P, ERROR]),
    "atspi_accessible_get_state_set": (P, [P]),
    "atspi_state_set_contains": (ctypes.c_int, [P, ctypes.c_int]),
    "atspi_accessible_get_component_iface": (P, [P]),
    "atspi_accessible_get_action_iface": (P, [P]),
    "atspi_component_get_extents": (ctypes.POINTER(Rect), [P, ctypes.c_int, ERROR]),
    "atspi_component_get_accessible_at_point": (
        P,
        [P, ctypes.c_int, ctypes.c_int, ctypes.c_int, ERROR],
    ),
    "atspi_component_grab_focus": (ctypes.c_int, [P, ERROR]),
    "atspi_action_get_n_actions": (ctypes.c_int, [P, ERROR]),
    "atspi_action_get_action_name": (P, [P, ctypes.c_int, ERROR]),
    "atspi_action_do_action": (ctypes.c_int, [P, ctypes.c_int, ERROR]),
}


class Library:
    def __init__(self) -> None:
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            raise Unavailable("no session D-Bus")
        try:
            self.atspi = ctypes.CDLL("libatspi.so.0")
            self.glib = ctypes.CDLL(ctypes.util.find_library("glib-2.0") or "libglib-2.0.so.0")
            self.gobject = ctypes.CDLL(
                ctypes.util.find_library("gobject-2.0") or "libgobject-2.0.so.0"
            )
        except OSError as error:
            raise Unavailable(str(error)) from error
        for name, (result, arguments) in SIGNATURES.items():
            function = getattr(self.atspi, name)
            function.restype, function.argtypes = result, arguments
        self.glib.g_free.argtypes = [P]
        self.glib.g_error_free.argtypes = [ctypes.POINTER(GError)]
        self.gobject.g_object_unref.argtypes = [P]
        self.gobject.g_object_ref.argtypes = [P]
        self.gobject.g_object_ref.restype = P
        if self.atspi.atspi_init() > 1:
            raise Unavailable("atspi_init failed")
        self.atspi.atspi_set_timeout(CALL_MILLISECONDS, CALL_MILLISECONDS * 10)

    def call(self, name: str, *arguments: Any) -> Any:
        error = ctypes.POINTER(GError)()
        value = getattr(self.atspi, name)(*arguments, ctypes.byref(error))
        if error:
            message = (error.contents.message or b"").decode(errors="replace")
            self.glib.g_error_free(error)
            raise Gone(message)
        return value

    def text(self, name: str, node: Any) -> str:
        pointer = self.call(name, node)
        if not pointer:
            return ""
        value = ctypes.cast(pointer, ctypes.c_char_p).value or b""
        self.glib.g_free(pointer)
        return value.decode(errors="replace")

    def state(self, node: Any, state: int) -> bool:
        states = self.atspi.atspi_accessible_get_state_set(node)
        if not states:
            return False
        try:
            return bool(self.atspi.atspi_state_set_contains(states, state))
        finally:
            self.gobject.g_object_unref(states)

    def extents(self, node: Any) -> tuple[float, float, float, float] | None:
        component = self.atspi.atspi_accessible_get_component_iface(node)
        if not component:
            return None
        try:
            rect = self.call("atspi_component_get_extents", component, SCREEN)
        finally:
            self.gobject.g_object_unref(component)
        if not rect:
            return None
        value = (rect.contents.x, rect.contents.y, rect.contents.width, rect.contents.height)
        self.glib.g_free(ctypes.cast(rect, P))
        return value


@dataclass(eq=False)
class AtspiNode:
    """One node of a window; holds its own reference until ``release``."""

    library: Library
    pointer: Any
    depth: int
    role: str
    name: str
    visible: bool
    container: bool

    def bounds(self) -> tuple[float, float, float, float] | None:
        if self.library.state(self.pointer, STATE_DEFUNCT):
            raise Gone(self.name)
        return self.library.extents(self.pointer)

    def press(self) -> bool:
        action = self.library.atspi.atspi_accessible_get_action_iface(self.pointer)
        if not action:
            return False
        try:
            names = [
                self._action_name(action, index)
                for index in range(self.library.call("atspi_action_get_n_actions", action))
            ]
            chosen = next((names.index(name) for name in PRESS_ACTIONS if name in names), None)
            if chosen is None:
                return False
            return bool(self.library.call("atspi_action_do_action", action, chosen))
        finally:
            self.library.gobject.g_object_unref(action)

    def _action_name(self, action: Any, index: int) -> str:
        pointer = self.library.call("atspi_action_get_action_name", action, index)
        if not pointer:
            return ""
        value = ctypes.cast(pointer, ctypes.c_char_p).value or b""
        self.library.glib.g_free(pointer)
        return value.decode(errors="replace").lower()

    def focus(self) -> bool:
        component = self.library.atspi.atspi_accessible_get_component_iface(self.pointer)
        if not component:
            return False
        try:
            return bool(self.library.call("atspi_component_grab_focus", component))
        finally:
            self.library.gobject.g_object_unref(component)

    def release(self) -> None:
        if self.pointer:
            self.library.gobject.g_object_unref(self.pointer)
            self.pointer = None


class Accessibility:
    """The session's AT-SPI registry, opened once by the driver."""

    def __init__(self) -> None:
        self.library = Library()

    def _application(self, pid: int) -> Any:
        library = self.library
        desktop = library.atspi.atspi_get_desktop(0)
        try:
            for index in range(library.call("atspi_accessible_get_child_count", desktop)):
                application = library.call("atspi_accessible_get_child_at_index", desktop, index)
                if not application:
                    continue
                try:
                    if library.call("atspi_accessible_get_process_id", application) == pid:
                        return application
                except Gone:
                    pass
                library.gobject.g_object_unref(application)
        finally:
            library.gobject.g_object_unref(desktop)
        raise Unavailable(f"process {pid} has not registered with AT-SPI")

    def _frame(self, pid: int, rect: tuple[int, int, int, int]) -> Any:
        """The application's top-level node that covers the X window ``rect`` most."""
        library = self.library
        application = self._application(pid)
        best, overlap = None, 0.0
        try:
            for index in range(library.call("atspi_accessible_get_child_count", application)):
                child = library.call("atspi_accessible_get_child_at_index", application, index)
                if not child:
                    continue
                extents = library.extents(child)
                area = 0.0
                if extents is not None:
                    width = min(rect[0] + rect[2], extents[0] + extents[2]) - max(
                        rect[0], extents[0]
                    )
                    height = min(rect[1] + rect[3], extents[1] + extents[3]) - max(
                        rect[1], extents[1]
                    )
                    area = max(0, width) * max(0, height)
                if area > overlap:
                    if best is not None:
                        library.gobject.g_object_unref(best)
                    best, overlap = child, area
                else:
                    library.gobject.g_object_unref(child)
        finally:
            library.gobject.g_object_unref(application)
        if best is None:
            raise Unavailable("no AT-SPI frame covers this window")
        return best

    def nodes(self, pid: int, rect: tuple[int, int, int, int]) -> list[AtspiNode]:
        """The window's subtree in tree order, its frame first."""
        library = self.library
        found: list[AtspiNode] = []
        stack = [(self._frame(pid, rect), 0)]
        while stack:
            pointer, depth = stack.pop()
            try:
                role = library.text("atspi_accessible_get_role_name", pointer)
                found.append(
                    AtspiNode(
                        library,
                        pointer,
                        depth,
                        role,
                        library.text("atspi_accessible_get_name", pointer),
                        library.state(pointer, STATE_SHOWING),
                        role in CONTAINER_ROLES,
                    )
                )
                count = library.call("atspi_accessible_get_child_count", pointer)
            except Gone:
                library.gobject.g_object_unref(pointer)
                continue
            children = []
            for index in range(count):
                try:
                    child = library.call("atspi_accessible_get_child_at_index", pointer, index)
                except Gone:
                    continue
                if child:
                    children.append((child, depth + 1))
            stack.extend(reversed(children))
        return found

    def at(self, pid: int, rect: tuple[int, int, int, int], x: int, y: int) -> dict[str, str]:
        """``{role, name}`` of the deepest node of the window under the root point."""
        library = self.library
        node = self._frame(pid, rect)
        try:
            while True:
                component = library.atspi.atspi_accessible_get_component_iface(node)
                if not component:
                    break
                try:
                    child = library.call(
                        "atspi_component_get_accessible_at_point", component, x, y, SCREEN
                    )
                finally:
                    library.gobject.g_object_unref(component)
                if not child:
                    break
                library.gobject.g_object_unref(node)
                node = child
            return {
                "role": library.text("atspi_accessible_get_role_name", node),
                "name": library.text("atspi_accessible_get_name", node),
            }
        finally:
            library.gobject.g_object_unref(node)
