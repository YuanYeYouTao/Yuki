"""Record the selected browser mode and installed package versions."""

import json
import subprocess
import sys
from pathlib import Path

mode, *packages = sys.argv[1:]
manifest = {
    "desktop": mode == "headed",
    "browser_mode": mode,
    "tools": [
        {
            "name": package,
            "version": subprocess.check_output(
                ["dpkg-query", "-W", "-f=${Version}", package], text=True
            ).strip(),
        }
        for package in dict.fromkeys(packages)
    ],
}
Path("/opt/yuki-runtime/manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
