"""Build the pinned SQLite extension; never compile while importing Yuki."""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class NativeBuildMetadata:
    architecture: str
    platform_tag: str
    macosx_target: str | None = None


def native_build_metadata() -> NativeBuildMetadata:
    architecture = platform.machine().lower()
    raw_platform = sysconfig.get_platform()
    if sys.platform == "darwin":
        _, target, _ = raw_platform.split("-")
        major, minor = (int(value) for value in target.split(".")[:2])
        if architecture == "arm64":
            major, minor = max((major, minor), (11, 0))
        return NativeBuildMetadata(
            architecture,
            f"macosx_{major}_{minor}_{architecture}",
            f"{major}.{minor}",
        )
    return NativeBuildMetadata(
        architecture, raw_platform.replace("-", "_").replace(".", "_")
    )


def build_vec1(
    root: Path, output: Path, *, metadata: NativeBuildMetadata | None = None
) -> Path:
    metadata = metadata or native_build_metadata()
    root, output = root.resolve(), output.resolve()
    source = root / "vendor" / "sqlite-vec1"
    output.mkdir(parents=True, exist_ok=True)
    suffix = ".dll" if sys.platform == "win32" else ".dylib" if sys.platform == "darwin" else ".so"
    library = output / f"vec1{suffix}"
    compiler = os.environ.get("CC") or ("cl" if sys.platform == "win32" else "cc")
    msvc = Path(compiler).name.lower() in {"cl", "cl.exe", "clang-cl", "clang-cl.exe"}
    x86 = metadata.architecture in {"amd64", "x86_64", "x64"}
    variants = ("SCALAR", "AVX2") if x86 else ("native",)
    objects: list[Path] = []
    for variant in variants:
        target = output / (variant.lower() + (".obj" if msvc else ".o"))
        if msvc:
            command = [
                compiler,
                "/nologo",
                "/O2",
                "/DNDEBUG",
                f"/I{source}",
                "/c",
                str(source / "vec1.c"),
                f"/Fo{target}",
            ]
            if x86:
                command.append(f"/DVEC1SIMD={variant}")
            if variant == "AVX2":
                command.append("/arch:AVX2")
            command.append("/D_WIN32_WINNT=0x0600")
        else:
            command = [
                compiler,
                "-O3",
                "-DNDEBUG",
                "-I",
                str(source),
                "-c",
                str(source / "vec1.c"),
                "-o",
                str(target),
            ]
            if sys.platform != "win32":
                command.append("-fPIC")
            else:
                command.append("-D_WIN32_WINNT=0x0600")
            if x86:
                command.append(f"-DVEC1SIMD={variant}")
            if variant == "AVX2":
                command.extend(("-mavx2", "-mfma"))
            if sys.platform == "darwin":
                command.extend(
                    (
                        "-arch",
                        metadata.architecture,
                        f"-mmacosx-version-min={metadata.macosx_target}",
                    )
                )
        subprocess.run(command, check=True, cwd=output)
        objects.append(target)
    if msvc:
        command = [compiler, "/nologo", "/LD", *map(str, objects), "/link", f"/OUT:{library}"]
    else:
        command = [
            compiler,
            "-dynamiclib" if sys.platform == "darwin" else "-shared",
            *map(str, objects),
            "-o",
            str(library),
            "-lm",
        ]
        if sys.platform == "win32":
            command.append("-static-libgcc")
        else:
            command.append("-pthread")
        if sys.platform == "darwin":
            command.extend(
                (
                    "-arch",
                    metadata.architecture,
                    f"-mmacosx-version-min={metadata.macosx_target}",
                )
            )
    subprocess.run(command, check=True, cwd=output)
    return library


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("src/yuki/_native"))
    args = parser.parse_args()
    print(build_vec1(Path(__file__).resolve().parents[1], args.output))


if __name__ == "__main__":
    main()
