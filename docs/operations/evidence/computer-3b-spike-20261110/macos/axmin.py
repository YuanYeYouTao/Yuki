"""Set AXMinimized of window <pid> <wid> to <0|1>; report front/cursor before and after."""

import json
import sys
import time

import ApplicationServices as A
import objc
from probe import snapshot

b = objc.loadBundle(
    "HIServices",
    {},
    "/System/Library/Frameworks/ApplicationServices.framework/Frameworks/HIServices.framework",
)
f = {}
objc.loadBundleFunctions(b, f, [("_AXUIElementGetWindow", b"i@o^I")])
pid, wid, flag = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3] == "1"
err, wins = A.AXUIElementCopyAttributeValue(A.AXUIElementCreateApplication(pid), "AXWindows", None)
win = next(w for w in wins if f["_AXUIElementGetWindow"](w, None)[1] == wid)
before = snapshot(wid)
e = A.AXUIElementSetAttributeValue(win, "AXMinimized", flag)
time.sleep(1.0)
after = snapshot(wid)
print(
    json.dumps(
        {
            "err": e,
            "front": [before["front"], after["front"]],
            "cursor_moved": before["cursor"] != after["cursor"],
        }
    )
)
