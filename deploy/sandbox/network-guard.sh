#!/bin/sh
# Root-owned, idempotent firewall additions limited to dedicated sandbox bridges.
set -eu
test "$(id -u)" = 0
if [ ! -e /proc/sys/net/bridge/bridge-nf-call-iptables ]; then
    modprobe br_netfilter
fi
test "$(sysctl -n net.bridge.bridge-nf-call-iptables)" = 1
ipt() { iptables -w 10 "$@"; }
ensure() { table="$1"; shift; ipt -C "$table" "$@" 2>/dev/null || ipt -I "$table" 1 "$@"; }
# Code must never access the host, even on a Docker internal network.
ensure INPUT -i yuki-sandbox0 -j DROP
ensure INPUT -i yuki-egress0 -j DROP
# Replies to the host Manager's authenticated execd connections only.
# NEW connections from the environment to the host still hit the DROP above.
ensure INPUT -i yuki-sandbox0 -p tcp --sport 44772 -m conntrack --ctstate ESTABLISHED -j ACCEPT
# Only the proxy port on the internal bridge is usable by job containers.
ipt -N YUKI-SANDBOX 2>/dev/null || true
ipt -F YUKI-SANDBOX
ipt -A YUKI-SANDBOX -d 172.30.251.2 -p tcp --dport 3128 -j ACCEPT
ipt -A YUKI-SANDBOX -j DROP
ensure DOCKER-USER -i yuki-sandbox0 ! -s 172.30.251.2 -j YUKI-SANDBOX
# Defense in depth below Squid destination ACLs, including DNS re-resolution.
ipt -N YUKI-EGRESS 2>/dev/null || true
ipt -F YUKI-EGRESS
ipt -A YUKI-EGRESS -d 1.1.1.1 -p udp --dport 53 -j RETURN
ipt -A YUKI-EGRESS -d 8.8.8.8 -p udp --dport 53 -j RETURN
for cidr in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.0.2.0/24 192.168.0.0/16 198.18.0.0/15 198.51.100.0/24 203.0.113.0/24 224.0.0.0/4 240.0.0.0/4; do
    ipt -A YUKI-EGRESS -d "$cidr" -j DROP
done
ipt -A YUKI-EGRESS -p tcp -m multiport --dports 80,443 -j RETURN
ipt -A YUKI-EGRESS -j DROP
ensure DOCKER-USER -i yuki-egress0 -j YUKI-EGRESS
