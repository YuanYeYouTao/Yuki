# HTTP 代理配置

Yuki 使用标准 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY`，也接受对应小写名称。
不需要 Yuki 专属代理开关。模型、Tavily、本机网页读取和 Telegram 的 API/平台文件下载可复用同一份配置；未配置代理时沿系统代理发现和正常网络路由工作。

## 本机启动

可直接沿用启动进程的标准代理环境变量，也可把下面内容写入启动目录的 `.env`，将 `PROXY_PORT` 替换为代理软件实际 HTTP 或混合端口：

```dotenv
ALL_PROXY=http://127.0.0.1:PROXY_PORT
NO_PROXY=localhost,127.0.0.1,::1,your-private-model-host
```

`ALL_PROXY` 是 HTTP/HTTPS 的后备项；如需分别配置，可改用 `HTTP_PROXY` 和 `HTTPS_PROXY`。
`HTTP_PROXY` 单独设置不会代理 HTTPS 请求；`HTTPS_PROXY=http://…` 表示经 HTTP 代理建立 HTTPS 隧道，地址方案不需要改成 `https://`。
确有 SOCKS 服务时可按实际地址使用 `socks5://` 或 `socks5h://`，不要把普通 HTTP 端口直接换成 SOCKS 协议。

Bot、普通管理 CLI 从当前启动目录读取 `.env`；`yuki setup --deployment-root /absolute/deployment` 从指定部署根读取。
启动只导出标准代理与 `SSL_CERT_FILE`、`SSL_CERT_DIR`，不把 `.env` 中其它 Bot 密钥导出给子进程。
同一代理项的大小写作为一组：进程已设置任一拼写时保留进程配置，包括显式空值；否则文件中小写优先。
进程已设置任一 CA 项时保留整组 CA 配置，避免文件中的 `SSL_CERT_FILE` 覆盖进程的 `SSL_CERT_DIR`。
无环境代理时，Windows/macOS 沿 Python 的系统代理发现；仅透明代理时继续使用系统网络路由。

## Docker Compose

把标准代理写入部署根既有 `.env`，或由启动 Compose 的 Shell 提供：

```dotenv
ALL_PROXY=http://host.docker.internal:PROXY_PORT
NO_PROXY=localhost,127.0.0.1,::1,bot,snowluma,your-private-model-host
```

随包 Compose 仅对 Bot 透传这四组变量，并将 `host.docker.internal` 映射到宿主网关。
宿主代理需要监听容器可达的接口；容器中的 `127.0.0.1` 是容器自身，不会被程序自动替换。
Compose 合并 Shell 和 `.env` 后，小写优先，双别名写入同一值并保留显式空值；不保证 Shell 大写值覆盖文件小写值，避免在两处用不同拼写配置同一项。
未设置 `NO_PROXY` 时，随包 Compose 默认绕过本机与内置服务名；显式值完整替换默认值，请保留健康检查、管理 API 和实际私有模型入口需要的地址。

旧部署的 Compose 不会被安装器自动替换，升级时需核对上述变量和 `extra_hosts`。
修改代理或 CA 配置后重新创建 Bot 容器；只修改 `.env` 而不重建容器，不会改变已启动进程。
CA 文件或目录须挂载到容器内可读路径，不能直接使用仅存在于宿主机的路径；无需在 Yuki 管理配置中保存证书或代理凭据。

## 路由与范围

请求协议决定使用哪项代理；`NO_PROXY` 按每次请求的原始域名和端口匹配，重定向后重新判断；不能依赖 DNS 解析后的 IP 命中域名规则，不承诺 CIDR 网段匹配。
本机网页读取仍验证公网目标、固定连接 IP、TLS 证书和下载容量；绕过代理不会放宽这些校验。
网页读取保持匿名，不保存站点 Cookie 或登录会话；代理认证独立处理，不发送给目标站点。
代理不可达、协议错误或证书错误如实失败，不自动改协议、直连或关闭证书校验。
经 HTTP 代理的 HTTPS 隧道在 TLS 建立前失败或被取消时，Bot 与管理 CLI 立即关闭这条代理连接并归还连接池；代理恢复后无需重启进程。

其他图片、音频、平台文件、ASR、插件通用 HTTP 和个人客户端继续沿各自现有传输设置。
持久工作环境仍使用独立 Squid 隔离出口，配置见[持久环境](persistent-environment.zh-CN.md)，不继承 Bot 的 `NO_PROXY`。
代理变量可能含认证信息，勿公开 `.env`、完整环境或 `docker compose config` 输出；诊断时只记录脱敏错误类别。

## 插件 HTTP 与 Clash fake-ip

插件通用 HTTP 会校验解析出的地址并固定连接获准的公网 IP；它不使用 `HTTP_PROXY`、`HTTPS_PROXY` 等环境代理来绕过这一检查。如果 Bot 使用的 DNS 返回 fake-ip，插件请求可能被拒绝。不要通过放行 fake-ip 或关闭地址校验来解决。

在可控制 DNS 的部署中，可将插件实际访问的域名加入 Clash/Mihomo 的 `dns.fake-ip-filter`，使其返回真实地址。例如，使用 `fake-ip-filter-mode: blacklist` 时，现场 GitHub 问题可参考：

```yaml
dns:
  fake-ip-filter-mode: blacklist
  fake-ip-filter:
    - '+.github.com'
```

请合并到现有配置，保留已有规则，不要覆盖整份 DNS 配置；其他插件域名按实际请求和重定向目标逐一检查。不同 Clash 内核、filter 模式和 DNS 路由可能不同，先核实配置实际生效；`whitelist` 模式的匹配含义不同，不能照搬此例。

验证时在 **Bot 实际运行的网络与 DNS 环境**（例如 Bot 容器）解析每个相关域名，确认结果不再是 fake-ip，并检查返回地址是否均为允许访问的公网地址；再执行对应插件的实际请求，确认成功，必要时检查重定向目标。解析正常不保证目标网络可达；部署仍需具备到目标的路由。排查时不要公开凭据、请求正文或完整代理配置。更多插件 HTTP 限制见[插件网络访问](../plugin-development/networking.md)。
