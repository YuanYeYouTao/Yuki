# HTTP 代理配置

Yuki 使用标准 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY`，也接受对应小写名称。
不需要 Yuki 专属代理开关。模型、Tavily 和本机网页读取可复用同一份配置；未配置代理时沿系统代理发现和正常网络路由工作。

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

图片、音频、平台文件、ASR、Telegram、插件通用 HTTP 和个人客户端继续沿各自现有传输设置。
持久工作环境仍使用独立 Squid 隔离出口，配置见[持久环境](persistent-environment.zh-CN.md)，不继承 Bot 的 `NO_PROXY`。
代理变量可能含认证信息，勿公开 `.env`、完整环境或 `docker compose config` 输出；诊断时只记录脱敏错误类别。
