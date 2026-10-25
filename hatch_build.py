"""Ship an SQLite extension, independent of the CPython ABI, in each platform wheel."""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name != "wheel":
            return
        root = Path(self.root)
        module = runpy.run_path(str(root / "scripts" / "build_vec1.py"))
        metadata = module["native_build_metadata"]()
        platform_tag = metadata.platform_tag
        output = (
            root / "src" / "yuki" / "_native"
            if version == "editable"
            else root / ".cache" / "vec1" / platform_tag
        )
        library = module["build_vec1"](root, output, metadata=metadata)
        build_data["pure_python"] = False
        build_data["tag"] = f"py3-none-{platform_tag}"
        build_data["force_include"][str(library)] = f"yuki/_native/{library.name}"
        build_data["force_include"][str(root / "vendor" / "sqlite-vec1" / "NOTICE")] = (
            "yuki/_native/NOTICE"
        )
