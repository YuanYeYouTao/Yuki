"""Host firewall for the environment's egress bridge.

The Manager runs on the host network namespace and applies these rules itself, in the
iptables backend Docker uses (the one holding DOCKER-USER). Code in the environment may
reach public addresses, never the host or private networks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

BRIDGE = "yuki-egress0"
EXECD_PORT = 44772
PRIVATE = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
)

Command = Callable[..., Awaitable[tuple[int, bytes]]]


async def apply(command: Command) -> str:
    """Install the rules idempotently; return the iptables binary Docker uses."""
    for binary in ("iptables-nft", "iptables-legacy"):
        code, _ = await command(binary, "-w", "10", "-S", "DOCKER-USER")
        if code == 0:
            break
    else:
        # Docker's nftables firewall backend has no DOCKER-USER chain.
        raise RuntimeError("network_guard_unsupported")

    async def run(*rule: str) -> int:
        return (await command(binary, "-w", "10", *rule))[0]

    async def required(*rule: str) -> None:
        if await run(*rule):
            raise RuntimeError("network_guard_missing")

    # Nothing from the environment reaches the host, except replies to the Manager's
    # own execd connections; the reply rule must stay above the drop.
    if await run("-C", "INPUT", "-i", BRIDGE, "-j", "DROP"):
        await required("-I", "INPUT", "1", "-i", BRIDGE, "-j", "DROP")
    reply = (
        *("-i", BRIDGE, "-p", "tcp", "--sport", str(EXECD_PORT)),
        *("-m", "conntrack", "--ctstate", "ESTABLISHED", "-j", "ACCEPT"),
    )
    while not await run("-D", "INPUT", *reply):
        pass
    await required("-I", "INPUT", "1", *reply)
    await run("-N", "YUKI-EGRESS")
    await required("-F", "YUKI-EGRESS")
    for cidr in PRIVATE:
        await required("-A", "YUKI-EGRESS", "-d", cidr, "-j", "DROP")
    await required("-A", "YUKI-EGRESS", "-j", "RETURN")
    if await run("-C", "DOCKER-USER", "-i", BRIDGE, "-j", "YUKI-EGRESS"):
        await required("-I", "DOCKER-USER", "1", "-i", BRIDGE, "-j", "YUKI-EGRESS")
    return binary
