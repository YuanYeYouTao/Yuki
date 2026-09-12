# Existing host proxy / 复用宿主代理

Setting `HTTP_PROXY` to Squid only selects the sandbox's isolation gateway;
the default Squid configuration still connects directly to public sites.

For the existing `/opt/mihomo-host` deployment, first install the current
`network-guard.sh`, then run `sudo python3 connect-host-proxy.py` on the host
(requires the system PyYAML package). The helper backs up both configurations,
adds a private Mihomo HTTP listener on the egress bridge, validates both proxy
configurations and reloads them. It does not restart Docker or the environment.
Only the trusted Squid container can reach that listener. Its separate rules
reject private destinations before applying the existing routing rules.
Squid uses `never_direct allow all`, so parent failure does not silently bypass it.

Check public HTTPS and a package download from **inside the environment**;
also check that a request through Squid to `http://127.0.0.1/` returns 403.
Re-run the helper if the egress container/network address changes. If Mihomo
subscriptions replace the whole configuration, preserve the sandbox listener
and sub-rules. Programs that ignore HTTP proxy settings still require explicit
proxy support; this is not unrestricted TCP/UDP networking.

`HTTP_PROXY` 指向 Squid，并不意味着已经接上翻墙出口。现有服务器可先更新
`network-guard.sh`，再以 root 运行本目录的 `connect-host-proxy.py`。脚本会备份配置，
给沙箱增加独立的 Mihomo 入口，检查配置并热重载。普通终端用户不持有代理管理凭据。
请在沙箱内验证下载、HTTPS 和内网拦截；网络地址变动后重新运行脚本，订阅更新时保留
新增入口和子规则。不遵守代理环境变量的程序仍需单独设置，不提供任意 TCP/UDP 出口。
