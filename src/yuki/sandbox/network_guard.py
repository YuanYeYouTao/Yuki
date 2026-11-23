"""Host firewall for the environment's egress bridge.

The Manager runs on the host network namespace and applies these rules itself, in the
iptables backend Docker uses (the one holding DOCKER-USER). Code in the environment may
reach public addresses, never the host or private networks, except the one TCP endpoint of
the proxy the deployment configured.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

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


async def apply(command: Command, proxy: tuple[tuple[str, ...], int] | None = None) -> str:
    """Install the rules idempotently; return the iptables binary Docker uses.

    ``proxy`` is the configured proxy's IPv4 addresses and TCP port; it is the only
    exception to the host and private drops, and goes away once it is no longer given.
    """
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
    # The proxy on this host (INPUT) or beyond it (DOCKER-USER), above both drops.
    await run("-N", "YUKI-PROXY")
    await required("-F", "YUKI-PROXY")
    addresses, port = proxy or ((), 0)
    for address in addresses:
        await required(
            *("-A", "YUKI-PROXY", "-d", f"{address}/32", "-p", "tcp"),
            *("--dport", str(port), "-j", "ACCEPT"),
        )
    for chain in ("INPUT", "DOCKER-USER"):
        while not await run("-D", chain, "-i", BRIDGE, "-j", "YUKI-PROXY"):
            pass
        await required("-I", chain, "1", "-i", BRIDGE, "-j", "YUKI-PROXY")
    return binary


def check_stage(proxied: bool, facts: dict[str, Any]) -> str:
    """Which leg of a request from the environment failed, from curl's own facts."""
    exit_code, connect = facts["exitcode"], facts["http_connect"]
    if exit_code == 0:
        return "ok"
    if not proxied:
        return "dns_failed" if exit_code == 6 else "direct_failed"
    if exit_code == 5:
        return "proxy_unresolved"
    if connect == 0 and exit_code in {7, 28}:
        return "proxy_unreachable"
    if connect == 200:
        return "remote_failed"
    if connect >= 500:
        return "proxy_upstream_failed"
    if connect:
        return "proxy_refused"
    return "proxy_failed"
