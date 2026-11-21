# Yuki 插件 HTTP 在 fake-ip DNS 环境下的可信 DoH 解析任务书（2026-11-21）

状态：**第一阶段第三版。方向已由用户决定（§0），仍未实现任何代码。** 剩余待决见 §6。待决确认后，在同一分支按顺序：先写 E2E（§7），在基线上跑红，再实施。

- 对应 issue：[#678](https://github.com/YuanYeYouTao/Yuki/issues/678)
- draft PR：[#679](https://github.com/YuanYeYouTao/Yuki/pull/679)
- 分支：`docs/issue-678-plugin-http-proxy-taskbook`
- 基线：origin/main `2aeeac84`

标记约定沿用前几份任务书：

| 标记 | 含义 |
|---|---|
| 【实证】 | 源码、依赖源码、现有文档或本机命令查到的事实 |
| 【issue】 | #678 给出的生产现象，本阶段**没有**到生产只读复核 |
| 【假设】 | 还没查实，需要在实施或 E2E 中确认 |
| 【决定】 | 用户已定 |
| 【推荐】 | 尚待用户确认的建议 |

路径省略 `src/yuki/`，行号以 `2aeeac84` 为准。依赖源码指本机 `.venv` 中的 httpx 0.28.1 和 httpcore 1.0.9。

## 0. 用户决定与版本

| # | 决定 | 轮次 |
|---|---|---|
| D1 | 显式配置的可信 DoH 解析；**每一跳**检查**全部**地址；连接固定到已检查的 IP；**任何失败都拒绝** | 第二版 |
| D2 | 按域名经代理降为备选、不采用；不接受白名单域名经代理后可能连到内网 | 第二版 |
| D3 | `network.http.unrestricted` **完全保持现状**，包括地址检查、NAT64 缺口、超时和 CA，见 §3.4、§3.6、§3.7 | 第二、三版 |
| D4 | 不放行 fake-ip 段 | 第二版 |
| D5 | SOCKS 不纳入 | 第二版 |

版本记录：

- **第一版** `61a93673`：推荐代理方案。
- **第二版** `5ed210bc`：
  - 改为推荐 DoH；
  - 更正 `subscription-monitor` 的威胁模型；
  - 新增安全审查。
- **第三版**（本版）：
  - 按 D3 把 NAT64 修正限定在 DoH 路径，并明确透明代理和 NAT64 的限制（§3.4、§3.9）；
  - 写全 A/AAAA、CNAME、NXDOMAIN/NODATA 的判定表与失败行为（§3.3）；
  - 新增整次请求统一截止时间（§3.6）；
  - 写明 `trust_env=False` 下的 CA 事实和 IP 形式 DoH URL 的证书（§3.2）；
  - E2E 增加拨号 IP、Host、SNI、请求次数和敏感头的逐项记录（§7）；
  - 新增权限和 DoH 可达性（§2.3、§3.8）。

## 1. 现象与原因

### 1.1 现象【issue】

`github-monitor` 从 2026-10-22 04:05 起没有成功轮询过 GitHub，状态却一直显示“运行中”。手动执行 `sync ... baseline` 清掉 `github_cursor_missing_from_overlap` 后，每次请求都报 `PluginPermissionError`。

部署环境：

- Mac 上的 Clash Verge，开了 TUN 和 `enhanced-mode: fake-ip`（`198.18.0.1/16`）；
- 容器内 `api.github.com` 解析为 `198.18.0.83`，`github.com` 解析为 `198.18.0.91`。

环境侧已把 `+.github.com` 加入 `fake-ip-filter`，之后 GitHub 恢复。这说明在该部署里，**直连 GitHub 的真实公网 IP 是通的**。

### 1.2 拒绝发生在哪里【实证】

| # | 事实 | 依据 |
|---|---|---|
| F1 | 插件 HTTP 设 `trust_env=False`，不读 `HTTP(S)_PROXY`、`ALL_PROXY` | `plugin_host/http_client.py:64-68` |
| F2 | 每一跳（含重定向）先查白名单，再用系统 `getaddrinfo` 解析；只要有一个地址 `not is_global`，就抛 `PluginPermissionError`。解析结果为空也走同一个错误 | `http_client.py:212-228,298-301` |
| F3 | 校验通过后，把 URL 主机换成第一个已校验的 IP 直连，Host 头和 SNI 保留原域名 | `http_client.py:229-240,115-119` |
| F4 | 本机 Python 3.12.16 把 `198.18.0.0/15` 判为 `is_global=False` | 标准库 `ipaddress` |
| F5 | `unrestricted` 只是把白名单置为 `None`，地址检查完全相同 | `http_client.py:262-265` |
| F6 | 现行文档承诺 unrestricted 不代表可以访问 Host 内网 | `docs/plugin-development/networking.md`、`security.md:31` |
| F7 | 插件与 Bot 同进程；Plugin API 是“访问治理，不是 Python/OS 沙盒” | `docs/plugin-development/security.md:5` |
| F8 | 仓库里没有 DNS 报文库（`uv.lock` 中无 `dnspython`） | `uv.lock`、`pyproject.toml:14` |
| F9 | 现有超时是**按单次操作**计算的，见 §3.6 | 同左 |
| F10 | `trust_env=False` 时，httpx 只用 certifi 的 CA，不读 `SSL_CERT_FILE`/`SSL_CERT_DIR`，见 §3.2 | 同左 |

F9 的具体事实：

- 解析阶段被 `asyncio.timeout(timeout_seconds)` 包住；
- 连接、读、写、连接池等待各自使用 `httpx.Timeout(timeout_seconds)`；
- 读响应体的循环没有总时限，重定向也会逐跳重置计时；
- 依据：`http_client.py:71-73,117,151,219`。

F10 的具体事实：

- httpx 在 `trust_env=False` 时，只用 `certifi.where()`；
- `trust_env` 会一路传到 transport 的 `create_ssl_context`；
- 依据：httpx `_config.py:34-40`、`_transports/default.py:153`、`_client.py:685-705`。

结论：F2 的检查本身没错，错在**解析来源**。

### 1.3 威胁模型【实证】

由 F7，这道地址检查防不了恶意插件。它防的是**可信插件替人取了不该取的地址**（SSRF）。

`subscription-monitor` 的实际情况：

- `monitor` 命令仅限超管（`plugins/subscription-monitor/subscription_monitor/plugin.py:45-56`、`plugin_host/command_adapter.py:202`）；
- 唯一的出站请求是 `GET subscription.url`（`polling.py:236`）；
- feed 条目里的链接只出现在摘要里，不会被请求（`polling.py:66-70`）。

因此 SSRF 的输入来源是：

- 超管配置的 URL；
- feed 服务器返回的重定向；
- feed 域名的 DNS。

allowlisted 插件只能影响路径和查询串。主机能否越出白名单，取决于两件事：

- 重定向：每跳都查白名单；
- 被批准域名自身的 DNS。

D1 防的正是后一种情况。

### 1.4 同类路径【实证 + 假设】

`services/media_resolver.py:439-461` 和网页读取（`web/page_extractor.py:54`）也是“本地解析 → 校验全部地址”。由此推断，它们在 fake-ip 环境下同样会被拒，但**未经生产复核**。本 PR 不处理，见 Q7。

## 2. 方案：显式配置的可信 DoH【决定 D1】

### 2.1 每一跳的行为

对第一个 URL 和每次重定向的目标，依次执行：

1. **URL 检查**：URL 解析、scheme、凭据、IDNA、白名单，沿用现有逻辑。
2. **IP 字面量**：不做 DNS，地址必须通过 §3.4 的检查。
3. **域名**：
   - 向配置的 DoH **并发**查询 A 和 AAAA；
   - 按 §3.3 判定这一跳的地址集合；
   - 集合中**每个**地址都必须通过 §3.4；
   - 任一步失败都拒绝。拒绝时目标零连接，也不尝试其他解析来源。
4. **连接**：
   - 只连已检查集合内的地址（§3.5）；
   - Host 头、SNI 和证书主机名都用原域名；
   - 整次请求受同一个截止时间约束（§3.6）。

配置了 DoH 时，系统解析器在这条路径上**一次都不调用**。

### 2.2 配置【推荐，Q1】

```dotenv
PLUGIN_HTTP_DOH_URL=https://dns.example/dns-query
PLUGIN_HTTP_DOH_BOOTSTRAP=203.0.113.53,2001:db8::53
```

URL 支持两种形式：

| URL 形式 | bootstrap | TLS 身份 |
|---|---|---|
| 主机名，如 `https://dns.example/dns-query` | **必填**，连接这些 IP | 证书须匹配该主机名；SNI 为该主机名 |
| IP，如 `https://1.1.1.1/dns-query` | 省略（填写即启动失败，避免两处来源冲突） | 证书须含该 IP 的 IP SAN；按 RFC 6066 不发送 SNI【假设：实施时以 E2E 记录的 SNI 为准】 |

配置规则：

- 只接受 `https://`；
- 配置非法时**启动失败**，不静默禁用；
- bootstrap 是所有者显式提供的出口配置，不是插件输入，因此**不要求**它是公网地址，允许自建的局域网 DoH；
- 不内置默认服务商，不自动探测 fake-ip；
- 修改配置后需要重建 Bot 容器（与代理变量相同）。

本机证书实测【实证，2026-11-21，在开发机上执行，不是生产容器】：`1.1.1.1`、`8.8.8.8`、`9.9.9.9`、`223.5.5.5`、`1.12.12.12` 的 443 端口证书都含对应的 IP SAN，证书链校验通过。这只说明 IP 形式在证书层面可行。**生产容器能否到达这些地址未核**（§3.8）。

### 2.3 权限与作用范围

| 插件批准的权限 | 行为 |
|---|---|
| 仅 `network.http.allowlisted` | 配置了 DoH：走 §2.1。未配置：与现在**逐字节相同** |
| 含 `network.http.unrestricted`（不论是否同时有 allowlisted） | **完全不变**（D3）：`allowed_hosts is None`（`http_client.py:262-265`），使用系统解析器，以及现有的地址检查、超时和 CA |
| 无 HTTP 权限 | 不变，仍抛 `PluginPermissionError`（`http_client.py:279-280`） |

其他约束：

- DoH 是部署级配置。插件看不到它，不能选择、覆盖或关闭它；
- 不改 Manifest、SDK 或 Plugin API 版本；
- `.env` 与 CA 文件需要在 Bot 容器内可读。不可读时按“配置非法”处理，启动失败。

## 3. 安全审查

### 3.1 Bootstrap

不用系统 DNS 解析 DoH 主机名，原因有二：

- fake-ip 环境下会自我依赖；
- 那样 DoH 连接的目的地就由本地 DNS 决定了。

多个 bootstrap IP 的处理：

- **仅在 TCP 连接失败时**，按配置顺序换下一个 IP；
- TLS、HTTP 或报文错误**不换 IP**，直接拒绝，不把认证失败当作可用性问题处理；
- 所有尝试共用 §3.6 的截止时间。

### 3.2 认证与 CA

**服务端认证（必须）：**

- 证书链和主机名（或 IP SAN）都要严格校验；
- 不提供 `verify=False`，不回退明文 `http://`、DoT 或 53 端口。

**CA 来源。** 依据 F10，现有插件客户端只信任 certifi，忽略 `SSL_CERT_FILE` 和 `SSL_CERT_DIR`。可选做法：

| 选项 | DoH 连接 | 目标连接 | 说明 |
|---|---|---|---|
| C1【推荐】 | 用 `httpx.create_ssl_context(trust_env=True)` 构造独立 SSL 上下文：读 `SSL_CERT_FILE/DIR`，没有则用 certifi。客户端仍保持 `trust_env=False`，所以不会读代理变量 | 保持现状，只用 certifi | 网页读取已有同样做法（`web/http_transport.py:196-197,302`）。DoH 是所有者的基础设施，可能使用自签或私有 CA；目标是公网站点 |
| C2 | 同 C1 | 也改为读 `SSL_CERT_FILE/DIR` | 改变 allowlisted 目标的信任集合。若该 CA 是透明 TLS 解密代理的 CA，信任会扩展到它（§3.9 T2） |
| C3 | 只用 certifi | 只用 certifi | 自建 DoH 必须使用公共 CA 签发的证书 |

**信任边界（写入文档）：**

- Host 信任 DoH 运营者和所选 CA 集合；
- 不做 DNSSEC 验证，也不以 AD 位作为条件；
- DoH 运营者能看到所有插件查询的主机名。

**客户端凭据：** 第一阶段不支持（Q2）。

**DoH 请求本身：**

- 只发协议必需的头（`Accept`/`Content-Type: application/dns-message`，加 `Host`）；
- 不带插件传入的任何 Header、Authorization、Cookie 或 Proxy-Authorization；
- 不跟随重定向；
- 响应体超过 65535 字节（DNS 报文的协议上限）即拒绝。

### 3.3 A/AAAA、CNAME、NXDOMAIN、NODATA

协议采用 RFC 8484 `application/dns-message`，报文用 `dnspython` 解析（Q3）。**每个查询**先按下表归类：

| 归类 | 条件 |
|---|---|
| **错误** | 以下任一：HTTP 非 200；Content-Type 不符；报文无法解析；QR 未置位；opcode 不是 QUERY；question 与请求不一致；TC=1；RCODE 不是 NOERROR 或 NXDOMAIN；CNAME 链断开、有环、或同一 owner 同时有 CNAME 和地址记录；超时或连接失败 |
| **地址** | NOERROR，且从 qname 沿 answer 中的 CNAME 走到链尾，链尾 owner 有本 qtype 的记录 |
| **NODATA** | NOERROR，链（可以为空）合法，但链尾没有本 qtype 的记录 |
| **NXDOMAIN** | RCODE 为 NXDOMAIN（按 RFC 6604，指的是链尾不存在，前面可以有 CNAME） |

CNAME 规则：

- answer 中 owner 不在链上的记录**一律忽略**，既不检查也不连接；
- 链长不设经验上限，由报文 65535 字节上限和环检测兜底；
- CNAME 目标名不按白名单检查。白名单约束的是逻辑主机，最终地址另经 §3.4 检查；
- A 和 AAAA 的链各自独立验证，允许不同（例如 CDN 轮换）。

两族结果的合并：

| A ＼ AAAA | 地址 | NODATA | NXDOMAIN | 错误 |
|---|---|---|---|---|
| **地址** | 两族全部地址都检查；任一不通过则拒绝 | 只用 A 的地址 | **拒绝**（两族自相矛盾） | **拒绝** |
| **NODATA** | 只用 AAAA 的地址 | **拒绝**（无地址） | 拒绝 | 拒绝 |
| **NXDOMAIN** | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| **错误** | 拒绝 | 拒绝 | 拒绝 | 拒绝 |

失败时返回的结果【推荐，Q6】：

| 情况 | 插件看到的结果 |
|---|---|
| 有地址，但未通过 §3.4 | `PluginPermissionError`（与现有类别一致） |
| 表中其余“拒绝” | `PluginResult(ok=False, error_code="http.dns_failed")` |
| 截止时间已到 | `http.request_failed`（现有码） |

注意：现有系统解析路径在“结果为空”时返回 `PluginPermissionError`（F2），而 DoH 路径的“无地址”归为 DNS 失败。两条路径的这一差异写入文档；按 D3，系统解析路径不改。

TTL 缓存：第一阶段不缓存，每一跳都重新查询（Q4）。

### 3.4 地址检查与 NAT64

DoH 路径的规则：沿用“任一地址非 `is_global` 即拒绝”，**另加一条**：地址落在 NAT64 知名前缀 `64:ff9b::/96` 内时，取出内嵌的 IPv4；该 IPv4 非 global 则拒绝。

依据【实证】：本机 Python 3.12.16 对下列地址的 `is_global` 判定为：

- `64:ff9b::7f00:1`（内嵌 `127.0.0.1`）：**True**，这是需要补的缺口；
- `64:ff9b:1::a00:1`、`::ffff:10.0.0.1`、`2002:7f00:1::1`：False，已被现有判定拦住。

**限制（写入文档）：**

- **N1**：只识别知名前缀。运营商自选的 NAT64 前缀（RFC 6052 NSP）无法从地址本身识别，内嵌私网 IPv4 的 NSP 地址会通过。本 PR 不为此新增配置。
- **N2**：DNS64 解析器会合成 AAAA。由于同时查询 A，只要 A 中有私网地址，整跳就会拒绝。
- **N3**：按 D3，**系统解析路径不加这条检查**。它涵盖 unrestricted，以及未配置 DoH 的 allowlisted，`64:ff9b::7f00:1` 在那里仍被视为公网。这是已记录的现状缺口，不在本 PR 修复。

### 3.5 固定 IP、多地址与请求次数

连接顺序与续试【推荐，Q6】：

- A 记录在前、AAAA 在后，各自按应答顺序；
- **只有 TCP 连接失败**才续试同一已检查集合中的下一个地址；
- 续试不重新解析，不换解析来源；
- 一旦 TCP 建立，后续任何失败（TLS、证书、读写、超时）都直接返回，不换地址、不重发，避免重放非幂等请求。

由此可以导出 E2E 要断言的请求次数：

| 对象 | 每跳次数 |
|---|---|
| DoH | 恰好 A、AAAA 各一次；仅 bootstrap 的 TCP 失败会增加连接次数，不增加成功的 HTTP 请求 |
| 目标服务器 | 至多一次 HTTP 请求 |

### 3.6 统一截止时间【推荐，Q5】

**现状（F9）：** 每个操作各自计时，重定向会重置计时，读响应体没有总时限。对方每隔不到 `timeout_seconds` 发一个字节，就能让一次插件请求持续很久。

**DoH 路径的规则：**

- 在 `SafeHttpClient.request()` 开始时确定**一个**截止时间：`deadline = 开始时刻 + timeout_seconds`。是否在拿到并发信号量之后才开始计时，见 Q5。
- 以下各步都只用剩余时间，剩余 ≤ 0 即失败：
  - 所有跳的 DoH 查询，包括 bootstrap 续试；
  - 目标的 TCP 连接、TLS、请求发送、响应头；
  - **读完整个响应体**；
  - 所有重定向。
- 不新增配置项。现有设置 `plugin_http_timeout_seconds`（默认 15.0，`config.py:304`；装配见 `application/modules/plugins.py:108`）在 DoH 路径上的含义改为“每次插件请求的总时限”。

按 D3，unrestricted 保持按操作计时。未配置 DoH 的 allowlisted 也不变，以满足 §2.3 的“逐字节相同”。

### 3.7 重定向

- 每一跳重新执行 §2.1 全流程，并扣减同一个截止时间。
- 下一跳的主机不在白名单时，在**发出 DoH 查询之前**就拒绝，DoH 服务看不到这个名字。
- 下一跳是 IP 字面量时，不查 DoH，直接按 §3.4 检查。
- 跨源（scheme、host、port 三元组任一不同）时，剥离 Authorization、调用方 Header 和 Body。
- 其余沿用现状：303 改为 GET；最多 5 次重定向；允许 `https → http` 降级（跨源会剥离凭据）。

### 3.8 DoH 可达性

- DoH 连接不读代理变量，直接连 bootstrap IP 或 URL 中的 IP。在 TUN 环境下这些连接由 TUN 接管，按代理规则转发；TLS 端到端校验保证应答的完整性和服务器身份。
- **只能经显式代理出网**的部署里，DoH 不可达，所有 allowlisted 请求都会以 `http.dns_failed` 或 `http.request_failed` 失败。这是失败关闭，不回退（L1）。
- 不加启动探测，也不把探测结果作为健康门槛；启动只校验配置格式（§2.2）。
- 不可达时，每次请求失败并记录类别。看门狗目前看不到这类失败，与 Q8 同源。
- 运维文档给出一次性的人工核对方法：在 Bot 容器内用 bootstrap IP 发一个 DoH 查询。只是文档说明，不是程序门槛。
- 生产能否到达所选 DoH 尚未核实。§2.2 的证书实测是在开发机上做的【假设：生产可达性】。

### 3.9 透明代理与部署限制

| # | 限制 | 状态 |
|---|---|---|
| L1 | 需要直连公网 IP 的出口（TUN 或真实路由）。只能经显式代理出网的部署不受支持，如实失败（§3.8） | 【issue】：TUN 下直连 GitHub 真实 IP 可用 |
| T1 | 透明代理如果改写目的地，会破坏 IP 固定。例如 Clash/Mihomo 的 `sniffer` 开启 `override-destination` 时，会用嗅探到的 SNI/Host 替换我们拨号的 IP，再用代理自己的 DNS 重新解析，可能连到别处。Host 的保证只到 Bot 进程的 socket，无法检测这种改写 | 【假设】：生产 sniffer 配置未核，见 §8 |
| T2 | 透明 TLS 解密代理：如果它的 CA 在 Host 的信任集合里，DoH 应答和目标身份的保证都会扩展到该代理。C1 下目标只信任 certifi，DoH 信任所有者配置的 CA | 【推论】 |
| T3 | 我们拨的是真实 IP，fake-ip 的反向映射不会介入。代理按 IP 规则把连接转发给代理节点时，节点连的仍是该 IP | 【推论】 |
| T4 | DoH 连接本身经过 TUN，内容受 TLS 保护；`dns-hijack` 只作用于 53 端口 | 【推论】 |
| L4 | 误把 fake-ip DNS（例如 Clash 自己的 DoH）配置为可信 DoH 时，答案落在 `198.18.x`，全部请求被拒。这是失败关闭，不是绕过 | 【推论】 |
| L5 | DoH 服务看到的是它自己的位置，不是 Bot 出口的位置。CDN 可能返回非最优节点，只影响延迟 | 【推论】 |

## 4. 备选方案与不选理由

- **P. 按域名经代理**（第一版的推荐）【不选，D2】：
  - 由代理解析域名，Host 看不到最终地址；
  - 被批准域名指向内网，或代理规则 `DIRECT` 到局域网时，会连进内网，代理 `192.168.5.2` 本身就在局域网里；
  - 防护效果取决于部署者的代理规则，无法由代码或 E2E 证明；
  - `0177.0.0.1`、`2130706433` 这类写法会被 `ipaddress` 当作主机名（实测），交给代理后怎么解释无法控制。
- **B. 放行 `198.18.0.0/15`**【不选，D4】：
  - fake-ip 环境下所有主机名都拿到这一段的地址，检查对**所有**名字失效；
  - 等于把代理软件的可配约定写死在代码里；
  - 会误放真实网络中的 `198.18.x`；
  - 与 `sandbox/network_guard.py:24` 把该段列为私网相矛盾。
- **C'. DoH 加经 HTTP 代理 CONNECT 已校验的 IP**【本阶段不做】：
  - 能覆盖 L1“只能经代理出网”的部署；
  - 本部署用 TUN 直连即可；
  - 会引入代理选择和隧道清理，且代理侧仍有 T1 风险。
- **S. SOCKS**【不纳入，D5】。
- **D. 逐个域名加 `fake-ip-filter`**【现状临时处理】：保留为未配置 DoH 时的做法，也是 unrestricted 在 fake-ip 环境下唯一的办法。

## 5. 看门狗与 README

- **插件卡在 gap 上无人知道**：建议另开 issue。依据：看门狗的 `components()`（`container.py:996-1024`）和 `loops()`（`container.py:986`）都没有插件自报的状态。
- **github-monitor**：源码不在本仓库，本 PR 无法修改它的 README。

## 6. 剩余待决

| # | 问题 | 推荐 |
|---|---|---|
| Q1 | 配置形态 | 主机名 URL 必须填 bootstrap；IP URL 不得填 bootstrap，且证书须含 IP SAN（§2.2） |
| Q2 | DoH 客户端凭据 | 第一阶段不支持 |
| Q3 | 新增 `dnspython` 依赖 | 接受。自写 DNS 报文解析风险更高 |
| Q4 | DoH 结果的 TTL 缓存 | 第一阶段不做，每跳都查 |
| Q5 | 统一截止时间（§3.6） | 只用于 DoH 路径；从拿到并发信号量后开始计时，排队等待不计入网络时限 |
| Q6 | 错误码、连接顺序与续试（§3.3、§3.5） | 新增 `http.dns_failed`；A 在前；仅 TCP 失败时续试 |
| Q7 | 网页读取和媒体下载的同类问题 | 另开 issue |
| Q8 | 看门狗报告插件需要人工处理 | 另开 issue |
| Q9 | CA 来源（§3.2） | C1：DoH 读 `SSL_CERT_FILE/DIR`，目标保持只用 certifi |

## 7. E2E：先写的失败情况与记录项（实施阶段第一步，现在未写）

### 7.1 测试环境

不访问外网，不连生产。

- **DoH 服务**：进程内 loopback TLS DoH，由临时 CA 签发证书。分两种证书：
  - 主机名证书，用于主机名 URL；
  - 含 `127.0.0.1` IP SAN 的证书，用于 IP URL。
- **目标服务**：loopback TLS 目标服务，同一临时 CA 签发。
- **拨号记录**：测试用的 httpcore 网络后端，记录每次 `connect_tcp(ip, port)`，并把预设的“公网”测试 IP 转发到对应的 loopback 服务。这是唯一的注入点，解析、检查和 TLS 都走真实代码。
- **系统解析器**：注入一个对所有名字都返回 `198.18.0.x` 的实现，并计数。
- **代理**：设置 `HTTPS_PROXY`，指向一个记录型代理。
- **入口**：全部经真实 `BoundHttpFacade` 发起。
- **CA 注入**：按 Q9 的决定。C1 下 DoH 的 CA 经 `SSL_CERT_FILE` 注入；目标的 CA 经现有 `SafeHttpClient(client=...)` 构造参数注入。另加一条装配断言，检查生产装配（`application/modules/plugins.py:107`）的 CA 来源。

### 7.2 每个情况都要记录的项

| 记录项 | 来源 |
|---|---|
| 结果：`ok`/`error_code`/异常类型 | 插件看到的结果 |
| 每次拨号的 `(ip, port)`，按发生顺序 | 网络后端 |
| DoH 收到的每个 `(qname, qtype)`、HTTP 请求数、收到的 SNI（IP URL 时应为“无”）、请求头**名称**集合 | DoH 服务 |
| 每个目标收到的 HTTP 请求数、SNI、`Host` 头，以及 `Authorization`、`Cookie`、`Proxy-Authorization`、调用方自定义头**是否出现**（只记布尔值，不记值）、Body 长度 | 目标服务 |
| 系统解析器调用次数 | 注入的解析器 |
| 代理收到的连接数 | 记录型代理 |

### 7.3 情况列表

| # | 情况 | 期望 | 基线 `2aeeac84` |
|---|---|---|---|
| E1 | 配置主机名 DoH；allowlisted 域名；A 和 AAAA 都是公网 | 成功；拨号 IP ∈ 应答集合；目标 SNI 和 Host 为原域名；DoH 恰好 A、AAAA 各 1 次；目标 1 次；系统解析器 0 次；代理 0 次；DoH 请求头里没有插件头 | **红**（`PluginPermissionError`） |
| E2 | 同 E1，但 DoH 配置为 IP URL | 成功；DoH 的 SNI 为“无”，证书按 IP SAN 校验 | 不适用 |
| E3 | IP URL，证书不含该 IP；主机名 URL，证书主机名不符；证书由不受信 CA 签发 | 拒绝；不换 bootstrap；目标 0 次 | 不适用 |
| E4 | 两族合并表（§3.3）的 16 格逐格覆盖，包括“地址 + NXDOMAIN”的矛盾组合 | 与表一致；拒绝时目标 0 次 | 不适用 |
| E5 | 归类为“错误”的每种条件，分别出现在 A 侧和 AAAA 侧 | `http.dns_failed`；系统解析器 0 次；代理 0 次 | 不适用 |
| E6 | CNAME 链尾为私网；链有环；同一 owner 既有 CNAME 又有 A；answer 中夹带不在链上的私网 A；NXDOMAIN 前带 CNAME | 前三种拒绝；夹带的记录被忽略，拨号中不出现；最后一种按 NXDOMAIN 归类 | 不适用 |
| E7 | AAAA 为 `::1`、`fc00::1`、`::ffff:10.0.0.1`、`64:ff9b::7f00:1` | 拒绝；目标 0 次 | 不适用 |
| E8 | DoH 返回 3xx；DoH 响应体超过 65535 字节 | 拒绝，不跟随 | 不适用 |
| E9 | 重定向到：非白名单域名 / 私网 IP 字面量 / 应答为私网的白名单域名 | 前两种：DoH 记录中没有该名字；第三种在第二跳拒绝；首跳目标都恰好 1 次 | 起点即红 |
| E10 | 同一域名两跳之间 DoH 答案由公网变为私网（rebinding） | 第二跳拒绝；第一跳拨号 IP 为当时应答中的地址 | 起点即红 |
| E11 | 跨源重定向（两个主机都在白名单） | 第二跳目标上 Authorization、Cookie、调用方头均不出现，Body 长度 0；同源跳仍保留 | 起点即红 |
| E12 | 调用方传入 `Cookie`、`Proxy-Authorization`、`Host` | 目标和 DoH 都没有收到；`Host` 为原域名 | 绿（现有剥离） |
| E13 | 目标证书与原域名不符 | 拒绝；该地址的拨号恰好 1 次，不续试 | 起点即红 |
| E14 | 第一个已检查地址的 TCP 被拒 | 按 Q6 续试下一个已检查地址；DoH 不重查；代理 0 次 | 不适用 |
| E15 | 目标以低于单次超时的间隔逐字节发送响应体，总时长超过 `timeout_seconds` | DoH 路径返回 `http.request_failed`；只记录结果码，不记录耗时 | **基线返回成功**（证明 F9） |
| E16 | DoH 逐字节慢发应答；bootstrap 首个 IP 的 TCP 挂起直到超时 | 共用截止时间，失败；不续试到超出截止 | 不适用 |
| E17 | **未配置** DoH，系统 DNS 为 fake-ip | 仍然拒绝；系统解析器被调用 | 绿 |
| E18 | 同时批准 unrestricted 和 allowlisted，且配置了 DoH | 完全走现状路径：系统解析器被调用，DoH 0 次；E15 的场景下行为与基线相同 | 绿 |
| E19 | allowlisted 的公网 IP 字面量 | DoH 0 次；直接拨该 IP | 绿 |
| E20 | 启动配置非法：`http://` URL、主机名 URL 缺 bootstrap、IP URL 填了 bootstrap、bootstrap 不是 IP、CA 文件不可读 | 启动失败，不静默禁用 | 不适用 |

### 7.4 产物

- 目录：`docs/architecture/evidence/issue-678-plugin-http-doh/`。
- 每个情况一行 JSON，字段即 §7.2 的记录项。
- 端口在记录前替换为角色名（如 `doh`、`target-a`），不含时间戳；排序后写出，重复运行**字节一致**。
- 同目录保存基线红跑的输出，其中 E1 和 E15 必须为红。

## 8. 生产只读复核（本阶段未执行）

- Bot 日志中，2026-10-22 之后 `github_poll_failed error_category=PluginPermissionError` 的出现情况；
- 同期网页读取和媒体下载的 `private_url` 次数（§1.4）；
- Clash 配置中 `sniffer` 与 `override-destination` 是否开启（T1）；
- 在 Bot 容器内，所选 DoH 的 bootstrap IP 能否建立 TLS（§3.8）。只读，不改配置。

## 9. 实施时需同步删改的文档

- **`docs/plugin-development/networking.md`、`security.md:31`**：写明以下内容：
  - DoH 路径的解析来源和信任边界；
  - 截止时间；
  - NAT64 的 N1–N3；
  - unrestricted 不变。
- **`docs/operations/http-proxy.zh-CN.md`**：写明以下内容：
  - 插件 HTTP 仍然不走代理；
  - DoH 配置项；
  - CA 来源；
  - 可达性的人工核对方法；
  - L1、T1–T4、L4、L5。
- **`.env.example`**：两项配置的注释示例。
- **测试**：`tests/` 中目前没有覆盖插件 HTTP 的测试，没有冻结测试需要删改。

## 10. 本阶段边界

本阶段只修改本任务书：

- 未写测试，未改代码，未加依赖；
- 未连生产，未改部署配置；
- 分支只追加提交，原 draft PR #679 不合并。
