"""Record installed package versions; executed inside the actual environment."""

import json
import os
import subprocess
from pathlib import Path

packages = {
    "chromium": "chromium",
    "Xvnc": "tigervnc-standalone-server",
    "openbox": "openbox",
    "socat": "socat",
    "xdotool": "xdotool",
    "Noto CJK": "fonts-noto-cjk",
    "Noto Emoji": "fonts-noto-color-emoji",
    "ImageMagick": "imagemagick",
}
manifest = {
    "image_version": os.environ.get("YUKI_ENVIRONMENT_IMAGE_VERSION", "desktop-1"),
    "desktop": True,
    "tools": [
        {
            "name": name,
            "version": subprocess.check_output(
                ["dpkg-query", "-W", "-f=${Version}", package], text=True
            ).strip(),
        }
        for name, package in packages.items()
    ],
}
Path("/opt/yuki-runtime/manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
