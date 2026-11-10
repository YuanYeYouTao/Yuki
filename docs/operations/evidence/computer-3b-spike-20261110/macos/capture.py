"""Capture one window by CGWindowID with ScreenCaptureKit and with CGWindowListCreateImage.

Plain process: no NSApplication, no run loop; completion handlers arrive on SCK's queue.
Usage: capture.py <window-id> <name>
"""

import json
import sys
import time

import Quartz
import ScreenCaptureKit as SCK
from probe import IMG, dump, mean_luma, save_png, wait

wid, name = int(sys.argv[1]), sys.argv[2]
# Without a prior CoreGraphics call, SCContentFilter(desktopIndependentWindow:) aborts the
# process: "Assertion failed: (did_initialize), function CGS_REQUIRE_INIT".
Quartz.CGMainDisplayID()
result = {"wid": wid, "name": name}

t0 = time.perf_counter()
content, error = wait(
    lambda done: (
        SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            True, False, done
        )
    )
)
result["shareable_ms"] = round((time.perf_counter() - t0) * 1000, 1)
result["shareable_error"] = str(error) if error else None
match = [w for w in content.windows() if w.windowID() == wid] if content else []
result["in_shareable"] = bool(match)
if match:
    w = match[0]
    f = w.frame()
    result["sck_window"] = {
        "title": w.title(),
        "onScreen": bool(w.isOnScreen()),
        "active": bool(w.isActive()) if hasattr(w, "isActive") else None,
        "frame": [f.origin.x, f.origin.y, f.size.width, f.size.height],
        "layer": int(w.windowLayer()),
    }
    filt = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(w)
    scale = float(filt.pointPixelScale())
    rect = filt.contentRect()
    result["filter"] = {
        "pointPixelScale": scale,
        "contentRect": [rect.origin.x, rect.origin.y, rect.size.width, rect.size.height],
    }
    cfg = SCK.SCStreamConfiguration.alloc().init()
    cfg.setWidth_(int(rect.size.width * scale))
    cfg.setHeight_(int(rect.size.height * scale))
    cfg.setShowsCursor_(False)
    cfg.setIgnoreShadowsSingleWindow_(True)
    t0 = time.perf_counter()
    try:
        image, err = wait(
            lambda done: (
                SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
                    filt, cfg, done
                )
            )
        )
        result["sck_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        result["sck_error"] = str(err) if err else None
        if image is not None:
            path = IMG / f"{name}-sck.png"
            save_png(image, path)
            result["sck_image"] = {
                "path": str(path),
                "size": [Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)],
                "luma_nonblack": mean_luma(image),
            }
    except TimeoutError as e:
        result["sck_error"] = f"timeout {e}"

t0 = time.perf_counter()
try:
    image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        wid,
        Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageNominalResolution,
    )
    result["cgwl_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    if image is not None:
        path = IMG / f"{name}-cgwl.png"
        save_png(image, path)
        result["cgwl_image"] = {
            "path": str(path),
            "size": [Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)],
            "luma_nonblack": mean_luma(image),
        }
    else:
        result["cgwl_image"] = None
except Exception as e:
    result["cgwl_error"] = repr(e)

print(dump(f"capture-{name}", result))
print(json.dumps(result, indent=1, default=str))
