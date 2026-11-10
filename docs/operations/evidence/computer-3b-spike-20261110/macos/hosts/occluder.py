"""Opaque floating window over x,y,w,h (AppKit coordinates) so the target is occluded."""

import sys

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSColor,
    NSFloatingWindowLevel,
    NSMakeRect,
    NSTextField,
    NSWindow,
    NSWindowStyleMaskBorderless,
)

x, y, w, h = map(float, sys.argv[1:5])
app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    NSMakeRect(x, y, w, h), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
)
win.setTitle_("Spike occluder")
win.setBackgroundColor_(NSColor.darkGrayColor())
win.setLevel_(NSFloatingWindowLevel)
win.setIgnoresMouseEvents_(True)
label = NSTextField.labelWithString_("Yuki 3b spike occluder (test window)")
label.setFrameOrigin_((10, h / 2))
label.setTextColor_(NSColor.whiteColor())
win.contentView().addSubview_(label)
win.orderFrontRegardless()
app.run()
