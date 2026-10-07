"""Reproduce the AMD64 bubblewrap additions to the pinned Moby deny-default policy."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "deploy" / "security"
BASE_SHA256 = "6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6"


def generate() -> bytes:
    original = (ROOT / "moby-default-seccomp.json").read_bytes()
    if hashlib.sha256(original).hexdigest() != BASE_SHA256:
        raise ValueError("pinned Moby seccomp baseline changed")
    policy = json.loads(original)

    def allow(name: str, index: int | None = None, value: int = 0) -> None:
        rule = {"names": [name], "action": "SCMP_ACT_ALLOW", "includes": {"arches": ["amd64"]}}
        if index is not None:
            rule["args"] = [{"index": index, "value": value, "op": "SCMP_CMP_EQ"}]
        policy["syscalls"].append(rule)

    # All six isolation namespaces plus cgroup namespace and SIGCHLD.
    allow("clone", 0, 0x7E020011)
    # bwrap enters a second user namespace after mounting and dropping caps.
    allow("unshare", 0, 0x10000000)
    # Exact mount flags observed from the pinned launcher on Debian bubblewrap.
    # SLAVE propagation; tmpfs; recursive root bind (legacy magic); recursive
    # bind; readonly library/worker remount; devpts; PRIVATE oldroot.
    for flags in (0x8C000, 6, 0xC0EDD000, 0xD000, 0x209027, 10, 0x4C000):
        allow("mount", 3, flags)
    allow("umount2", 1, 2)  # MNT_DETACH only
    allow("pivot_root")  # AppArmor restricts the two paths; seccomp cannot dereference pointers.
    return (json.dumps(policy, indent=2) + "\n").encode()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = ROOT / "yuki-codemode-seccomp.json"
    expected = generate()
    if args.check:
        if target.read_bytes() != expected:
            raise SystemExit("generated seccomp policy is stale")
    else:
        target.write_bytes(expected)


if __name__ == "__main__":
    main()
