"""Root-only opt-in Squid parent via the existing Mihomo instance.

Usage: python3 connect-host-proxy.py
Keeps the new listener private; only Squid can use the bridge inlet. Re-run
after recreating the egress network/container if its addresses have changed.
The parent proxy must enforce public destinations too (including DNS results).
"""

import datetime
import ipaddress
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

import yaml


def run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def main() -> None:
    if os.getuid() != 0:
        raise SystemExit("root is required")
    info = json.loads(run("docker", "inspect", "yuki-sandbox-egress"))[0]
    network = info["NetworkSettings"]["Networks"]["yuki-sandbox-egress"]
    gateway = str(ipaddress.IPv4Address(network["Gateway"]))
    client = str(ipaddress.IPv4Address(network["IPAddress"]))
    config = Path(
        next(m["Source"] for m in info["Mounts"] if m["Destination"] == "/etc/squid/squid.conf")
    )
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = Path("/etc/yuki-sandbox") / ("pre-upstream-" + stamp)
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(config, backup / "squid.conf")
    hook = Path("/etc/yuki-sandbox/upstream-firewall.sh")
    if hook.exists():
        shutil.copy2(hook, backup / hook.name)
    hook.write_text(
        "#!/bin/sh\nset -eu\n"
        f"iptables -w 10 -C INPUT -i yuki-egress0 -s {client} -d {gateway} "
        "-p tcp --dport 17897 -j ACCEPT 2>/dev/null || "
        f"iptables -w 10 -I INPUT 1 -i yuki-egress0 -s {client} -d {gateway} "
        "-p tcp --dport 17897 -j ACCEPT\n"
    )
    hook.chmod(0o700)
    mihomo = Path("/opt/mihomo-host/config/config.yaml")
    shutil.copy2(mihomo, backup / "mihomo.yaml")
    old_mihomo = mihomo.read_text()
    data = yaml.safe_load(old_mihomo)
    listeners = data.setdefault("listeners", [])
    listeners[:] = [item for item in listeners if item.get("name") != "yuki-sandbox-public"]
    listeners.append(
        {
            "name": "yuki-sandbox-public",
            "type": "http",
            "listen": gateway,
            "port": 17897,
            "rule": "yuki-sandbox-public",
        }
    )
    # Force resolution before routing; block private destinations at the parent too.
    cidrs = (
        "0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 "
        "192.0.2.0/24 192.168.0.0/16 198.18.0.0/15 198.51.100.0/24 "
        "203.0.113.0/24 224.0.0.0/4 240.0.0.0/4"
    ).split()
    blocks = [f"IP-CIDR,{cidr},REJECT" for cidr in cidrs]
    blocks += [
        f"IP-CIDR6,{cidr},REJECT"
        for cidr in ("::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8")
    ]
    data.setdefault("sub-rules", {})["yuki-sandbox-public"] = blocks + data["rules"]
    mihomo.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))

    def reload_mihomo():
        request = urllib.request.Request(
            "http://127.0.0.1:9090/configs",
            method="PUT",
            data=json.dumps({"path": "/root/.config/mihomo/config.yaml"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + str(data.get("secret", "")),
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            assert response.status == 204

    marker = "# YUKI HOST UPSTREAM\n"
    original = config.read_text()
    updated = original.split(marker)[0].rstrip() + "\n" + marker
    updated += f"cache_peer {gateway} parent 17897 0 no-query default name=host_upstream\n"
    updated += "never_direct allow all\n"
    # Write in place: Squid's bind mount must continue to see the same inode.
    config.write_text(updated)
    try:
        run("docker", "exec", "yuki-sandbox-egress", "squid", "-k", "parse")
        run(
            "docker",
            "exec",
            "mihomo-host",
            "/mihomo",
            "-t",
            "-d",
            "/root/.config/mihomo",
            "-f",
            "/root/.config/mihomo/config.yaml",
        )
        run(str(hook))
        reload_mihomo()
        run("docker", "exec", "yuki-sandbox-egress", "squid", "-k", "reconfigure")
    except BaseException:
        config.write_text(original)
        mihomo.write_text(old_mihomo)
        reload_mihomo()
        run("docker", "exec", "yuki-sandbox-egress", "squid", "-k", "reconfigure")
        raise
    print(f"Squid parent enabled; configuration backup: {backup}")


if __name__ == "__main__":
    main()
