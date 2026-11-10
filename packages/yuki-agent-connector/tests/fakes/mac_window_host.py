"""A macOS test window that records every event it receives (3b spike host, trimmed).

Usage: python mac_window_host.py <tag> <x> <y> <log>
Opens two overlapping AppKit windows, "Yuki test <tag> A" and "... B" (B on top, covering
A's lower right), each with a button, a text field, a scroll view and a custom-drawn pad that
has no accessibility element. It never activates itself. Every event, button action and
activation change goes to <log> as JSON lines; <log>.cmd takes "mini A" / "mini B".
"""

import json
import os
import sys
import time
from pathlib import Path

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSMakeRect,
    NSNotificationCenter,
    NSObject,
    NSRectFill,
    NSScrollView,
    NSTextField,
    NSTextView,
    NSTimer,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskTitled,
)

tag, X, Y, LOG = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), Path(sys.argv[4])


def log(**fields):
    fields["t"] = time.time()
    fields["active"] = bool(NSApplication.sharedApplication().isActive())
    with LOG.open("a") as out:
        out.write(json.dumps(fields) + "\n")


class Pad(NSView):
    def drawRect_(self, rect):
        NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.85, 0.3, 1.0).set()
        NSRectFill(self.bounds())

    def acceptsFirstMouse_(self, event):
        return True

    def acceptsFirstResponder(self):
        return True

    @objc.python_method
    def _event(self, name, event):
        point = event.locationInWindow()
        log(
            ev=name,
            win=int(event.windowNumber()),
            x=point.x,
            y=point.y,
            clicks=int(event.clickCount()) if "Down" in name else None,
            chars=event.characters() if name == "keyDown" else None,
            dy=event.scrollingDeltaY() if name == "scroll" else None,
        )

    def mouseDown_(self, event):
        self._event("mouseDown", event)
        self.window().makeFirstResponder_(self)

    def mouseUp_(self, event):
        self._event("mouseUp", event)

    def mouseDragged_(self, event):
        self._event("mouseDragged", event)

    def mouseMoved_(self, event):
        self._event("mouseMoved", event)

    def rightMouseDown_(self, event):
        self._event("rightMouseDown", event)

    def keyDown_(self, event):
        self._event("keyDown", event)

    def scrollWheel_(self, event):
        self._event("scroll", event)


class Delegate(NSObject):
    def pressed_(self, sender):
        log(ev="buttonAction", win=int(sender.window().windowNumber()))

    def note_(self, notification):
        target = notification.object()
        number = int(target.windowNumber()) if hasattr(target, "windowNumber") else None
        log(ev=str(notification.name()), win=number)

    def poll_(self, timer):
        command = LOG.with_suffix(".cmd")
        if not command.exists():
            return
        words = command.read_text().split()
        command.unlink()
        window = {"A": windows[0], "B": windows[1]}[words[1]]
        if words[0] == "mini":
            window.miniaturize_(None)
        log(ev="cmd", words=words)


def make_window(delegate, x, y, title):
    style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(x, y, 420, 320), style, NSBackingStoreBuffered, False
    )
    window.setTitle_(title)
    window.setReleasedWhenClosed_(False)
    window.setAcceptsMouseMovedEvents_(True)
    content = window.contentView()
    button = NSButton.alloc().initWithFrame_(NSMakeRect(10, 270, 160, 32))
    button.setTitle_("Press me")
    button.setTarget_(delegate)
    button.setAction_("pressed:")
    content.addSubview_(button)
    field = NSTextField.alloc().initWithFrame_(NSMakeRect(180, 274, 220, 24))
    field.setPlaceholderString_("field")
    content.addSubview_(field)
    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(10, 110, 400, 150))
    text = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 385, 150))
    text.setString_("\n".join(f"line {index}" for index in range(300)))
    scroll.setDocumentView_(text)
    scroll.setHasVerticalScroller_(True)
    content.addSubview_(scroll)
    content.addSubview_(Pad.alloc().initWithFrame_(NSMakeRect(10, 10, 400, 90)))
    for name in ("NSWindowDidBecomeKeyNotification", "NSWindowDidMiniaturizeNotification"):
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            delegate, "note:", name, window
        )
    window.orderFront_(None)
    log(ev="window", win=int(window.windowNumber()), title=title)
    return window


app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
delegate = Delegate.alloc().init()
for name in (
    "NSApplicationDidBecomeActiveNotification",
    "NSApplicationDidResignActiveNotification",
):
    NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        delegate, "note:", name, app
    )
windows = [
    make_window(delegate, X, Y, f"Yuki test {tag} A"),
    make_window(delegate, X + 40, Y - 40, f"Yuki test {tag} B"),
]
NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
    0.2, delegate, "poll:", None, True
)
log(ev="ready", pid=os.getpid())
app.run()
