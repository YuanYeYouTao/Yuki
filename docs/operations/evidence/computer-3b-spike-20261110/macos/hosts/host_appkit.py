"""Spike test host: an AppKit (or WKWebView) window that logs every event it receives.

Never activates itself. Usage: host_appkit.py <tag> <x> <y> <logfile> [--web] [--two]
--two opens a second window (to check per-window delivery). Coordinates are AppKit
screen coordinates of the window's bottom-left (display is 1920x1080, origin bottom-left).
"""

import json
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
    NSScrollView,
    NSTextField,
    NSTextView,
    NSTimer,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)

tag, X, Y, LOG = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), Path(sys.argv[4])
WEB = "--web" in sys.argv
TWO = "--two" in sys.argv


def log(**fields):
    fields["t"] = time.time()
    fields["active"] = bool(NSApplication.sharedApplication().isActive())
    with LOG.open("a") as out:
        out.write(json.dumps(fields) + "\n")


class Pad(NSView):
    def drawRect_(self, rect):
        NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.85, 0.3, 1.0).set()
        from AppKit import NSRectFill

        NSRectFill(self.bounds())

    def acceptsFirstMouse_(self, event):
        return True

    def acceptsFirstResponder(self):
        return True

    @objc.python_method
    def _ev(self, name, event):
        p = event.locationInWindow()
        log(
            ev=name,
            win=int(event.windowNumber()),
            x=p.x,
            y=p.y,
            key=bool(self.window().isKeyWindow()),
            chars=event.characters() if name == "keyDown" else None,
            dy=event.scrollingDeltaY() if name == "scroll" else None,
        )

    def mouseDown_(self, event):
        self._ev("mouseDown", event)
        self.window().makeFirstResponder_(self)

    def rightMouseDown_(self, event):
        self._ev("rightMouseDown", event)

    def keyDown_(self, event):
        self._ev("keyDown", event)

    def scrollWheel_(self, event):
        self._ev("scroll", event)


class Delegate(NSObject):
    def pressed_(self, sender):
        self.count += 1
        sender.setTitle_(f"Pressed {self.count}")
        log(ev="buttonAction", count=self.count, win=int(sender.window().windowNumber()))

    def note_(self, notification):
        obj = notification.object()
        num = int(obj.windowNumber()) if hasattr(obj, "windowNumber") else None
        log(ev=str(notification.name()), win=num)

    def poll_(self, timer):
        cmd = LOG.with_suffix(".cmd")
        if not cmd.exists():
            return
        words = cmd.read_text().split()
        cmd.unlink()
        target = {"A": windows[0], "B": windows[-1]}[words[1]] if len(words) > 1 else None
        if words[0] == "mini":
            target.miniaturize_(None)
        elif words[0] == "demini":
            target.deminiaturize_(None)
        elif words[0] == "move":
            target.setFrameOrigin_((float(words[2]), float(words[3])))
        elif words[0] == "set":
            target.contentView().subviews()[0].setTitle_(" ".join(words[2:]))
        elif words[0] in ("space", "unspace"):
            sl = {}
            bundle = objc.loadBundle(
                "SkyLight", {}, "/System/Library/PrivateFrameworks/SkyLight.framework"
            )
            objc.loadBundleFunctions(
                bundle,
                sl,
                [
                    ("SLSMainConnectionID", b"i"),
                    ("SLSGetActiveSpace", b"Qi"),
                    ("SLSSpaceCreate", b"Qii@"),
                    ("SLSAddWindowsToSpaces", b"vi@@"),
                    ("SLSRemoveWindowsFromSpaces", b"vi@@"),
                    ("SLSSpaceDestroy", b"viQ"),
                ],
            )
            cid = sl["SLSMainConnectionID"]()
            wid = [int(target.windowNumber())]
            if words[0] == "space":
                self.home = sl["SLSGetActiveSpace"](cid)
                self.space = sl["SLSSpaceCreate"](cid, 1, None)
                sl["SLSAddWindowsToSpaces"](cid, wid, [self.space])
                sl["SLSRemoveWindowsFromSpaces"](cid, wid, [self.home])
                log(ev="space", home=self.home, space=self.space)
            else:
                sl["SLSAddWindowsToSpaces"](cid, wid, [self.home])
                sl["SLSRemoveWindowsFromSpaces"](cid, wid, [self.space])
                sl["SLSSpaceDestroy"](cid, self.space)
        elif words[0] == "hide":
            NSApplication.sharedApplication().hide_(None)
        elif words[0] == "unhide":
            NSApplication.sharedApplication().unhideWithoutActivation()
        elif words[0] == "front":
            target.orderFront_(None)
        log(ev="cmd", words=words, frame=str(target.frame()) if target else None)

    # WKScriptMessageHandler
    def userContentController_didReceiveScriptMessage_(self, controller, message):
        log(ev="web", data=json.loads(message.body()))


def make_window(delegate, x, y, title):
    style = (
        NSWindowStyleMaskTitled
        | NSWindowStyleMaskClosable
        | NSWindowStyleMaskMiniaturizable
        | NSWindowStyleMaskResizable
    )
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(x, y, 420, 320), style, NSBackingStoreBuffered, False
    )
    window.setTitle_(title)
    window.setReleasedWhenClosed_(False)
    content = window.contentView()
    if WEB:
        from WebKit import WKUserContentController, WKWebView, WKWebViewConfiguration

        config = WKWebViewConfiguration.alloc().init()
        controller = WKUserContentController.alloc().init()
        controller.addScriptMessageHandler_name_(delegate, "log")
        config.setUserContentController_(controller)
        web = WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, 420, 320), config)
        page = Path(__file__).with_name("page.html")
        web.loadFileURL_allowingReadAccessToURL_(
            objc.lookUpClass("NSURL").fileURLWithPath_(str(page)),
            objc.lookUpClass("NSURL").fileURLWithPath_(str(page.parent)),
        )
        content.addSubview_(web)
    else:
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
        text.setString_("\n".join(f"line {i}" for i in range(300)))
        scroll.setDocumentView_(text)
        scroll.setHasVerticalScroller_(True)
        content.addSubview_(scroll)
        pad = Pad.alloc().initWithFrame_(NSMakeRect(10, 10, 400, 90))
        content.addSubview_(pad)
    center = NSNotificationCenter.defaultCenter()
    for name in (
        "NSWindowDidBecomeKeyNotification",
        "NSWindowDidResignKeyNotification",
        "NSWindowDidBecomeMainNotification",
        "NSWindowDidMiniaturizeNotification",
        "NSWindowDidDeminiaturizeNotification",
    ):
        center.addObserver_selector_name_object_(delegate, "note:", name, window)
    window.orderFront_(None)
    log(ev="window", win=int(window.windowNumber()), title=title)
    return window


app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
delegate = Delegate.alloc().init()
delegate.count = 0
center = NSNotificationCenter.defaultCenter()
for name in (
    "NSApplicationDidBecomeActiveNotification",
    "NSApplicationDidResignActiveNotification",
):
    center.addObserver_selector_name_object_(delegate, "note:", name, app)
windows = [make_window(delegate, X, Y, f"Spike {tag} A")]
if TWO:
    windows.append(make_window(delegate, X + 40, Y - 40, f"Spike {tag} B"))
NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
    0.3, delegate, "poll:", None, True
)
log(ev="ready", pid=__import__("os").getpid())
app.run()
