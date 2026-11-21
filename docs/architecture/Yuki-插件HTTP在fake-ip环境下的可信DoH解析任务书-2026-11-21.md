# Yuki 插件 HTTP 在 fake-ip DNS 环境下的可信 DoH 解析任务书（2026-11-21）

状态：**第一阶段第四版。方向已由用户决定（§0），仍未实现任何代码。** 剩余待决见 §6。待决确认后，在同一分支按顺序：先写 E2E（§7），在基线上跑红，再实施。

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

路径省略 `src/yuki/`，行号以 `2aeeac84` 为准。依赖源码指本机 `.venv` 中的 httpx 0.28.1、httpcore 1.0.9，以及 Python 3.12.16。

## 0. 用户决定与版本

| # | 决定 | 轮次 |
|---|---|---|
| D1 | 显式配置的可信 DoH 解析；**每一跳**检查**全部**地址；连接固定到已检查的 IP；**任何失败都拒绝** | 第二版 |
| D2 | 按域名经代理降为备选、不采用；不接受白名单域名经代理后可能连到内网 | 第二版 |
| D3 | `network.http.unrestricted` **完全保持现状**；**未启用 DoH 时**，allowlisted 的行为和 Bot 的启动条件也与基线相同（§2.4） | 第二、三、四版 |
| D4 | 不放行 fake-ip 段 | 第二版 |
| D5 | SOCKS 不纳入 | 第二版 |

版本记录：

- **第一版** `61a93673`：推荐代理方案。
- **第二版** `5ed210bc`：
  - 改为推荐 DoH；
  - 更正威胁模型；
  - 新增安全审查。
- **第三版** `db7e402f`：
  - 写出 DNS 判定表、统一截止时间、CA 来源；
  - E2E 增加记录项。
- **第四版**（本版，处理 Codex 对 `db7e402f` 的复审）：
  - **启动条件**：未启用 DoH 时启动条件不变；区分“DoH 配置写错”与“所选 CA 读不到”，分别说明对启动的影响（§2.4）。
  - **DNS 判定**：明确终结否定、转介（referral）、未完成别名，并给出错误优先级（§3.3）。
  - **地址范围**：DoH 路径限定为公网**单播**。Python 3.12 把组播判为 global，已实测（§3.4）。
  - **保证边界**：写清透明代理和 NAT64 下 Host 保证到哪里为止（§3.10）。
  - **隔离**：CA 信任、bootstrap IP、Host/SNI 与连接池四方面互相隔离（§3.5）。
  - **重定向**：写明请求正文与凭据的去留（§3.8）。
  - **E2E 修正**：
    - 慢读基线改用公网解析，使基线真正走到读响应体这一步；
    - TLS 失败后第二候选地址零拨号；
    - 并发 DNS 查询按多重集记录；
    - 增加伪造 Host 与合法 Host 的验证（§7）。

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
| F9 | 现有超时是**按单次操作**计算的，见 §3.7 | 同左 |
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
2. **IP 字面量**：不做 DNS 查询，地址必须通过 §3.4 的“公网单播”检查。
3. **域名**：
   - 向配置的 DoH **并发**查询 A 和 AAAA，等两个查询都结束或截止时间到（§3.3）；
   - 按 §3.3 判定本跳的地址集合；
   - 集合中**每个**地址都必须通过 §3.4；
   - 任何一步失败就拒绝本次请求。被拒时目标零连接，也不尝试任何其他解析来源。
4. **连接**：
   - 只连接已检查集合中的地址（§3.6）；
   - Host 头、SNI 和证书主机名都用逻辑域名；
   - 整次请求共用同一个截止时间（§3.7）。

配置了 DoH 时，这条路径上系统解析器**零调用**。

### 2.2 配置【推荐，Q1】

```dotenv
PLUGIN_HTTP_DOH_URL=https://dns.example/dns-query
PLUGIN_HTTP_DOH_BOOTSTRAP=203.0.113.53,2001:db8::53
```

| URL 形式 | bootstrap | DoH 连接的 TLS 身份 |
|---|---|---|
| 主机名，如 `https://dns.example/dns-query` | **必填** | 证书必须匹配该主机名；SNI 为该主机名 |
| IP，如 `https://1.1.1.1/dns-query` | **不得填写** | 证书必须含该 IP 的 IP SAN。按 RFC 6066 不发 SNI【假设：以 E2E 记录为准】 |

- 只接受 `https://`。
- bootstrap 地址须是单播 IP 字面量。组播、未指定地址（`0.0.0.0`/`::`）、广播都视为写错配置。
- bootstrap **不要求**是公网地址：它是所有者显式提供的出口，不是插件输入，可以指向局域网里自建的 DoH。
- 不内置默认 DoH 服务商，不自动探测 fake-ip。

开发机实测【实证，2026-11-21，非生产容器】：`1.1.1.1`、`8.8.8.8`、`9.9.9.9`、`223.5.5.5`、`1.12.12.12` 的 443 端口证书均含对应的 IP SAN，且证书链校验通过。生产环境能否连通这些地址**未核**（§3.9）。

### 2.3 权限与作用范围

| 插件已批准的权限 | 行为 |
|---|---|
| 仅 `network.http.allowlisted` | 配置了 DoH：走 §2.1。未配置：与基线逐字节相同 |
| 含 `network.http.unrestricted`（无论是否同时有 allowlisted） | **完全不变**：`allowed_hosts is None`（`http_client.py:262-265`），沿用系统解析器以及现有的地址检查、超时和 CA |
| 无 HTTP 权限 | 不变（`http_client.py:279-280`） |

DoH 是部署级配置：插件看不到它，也不能选择或关闭它。Manifest、SDK 和 Plugin API 版本都不改。

### 2.4 启动条件与 CA 读不到的影响【推荐，Q10】

| 情形 | 启动 | 运行时 |
|---|---|---|
| **未设置** DoH 变量 | **与基线相同**：不构造任何 DoH 对象，不新增任何读文件或检查。即使 `SSL_CERT_FILE` 读不到，启动结果也与基线一致（基线本身是否失败，取决于网页读取是否被装配，见下注），E25 记录基线结果作对照 | 与基线相同 |
| 设置了 DoH 变量但**写错**（非 https、主机名 URL 缺 bootstrap、IP URL 填了 bootstrap、bootstrap 不是单播 IP 字面量、只设了两项中的一项） | **启动失败**。与 `Settings`（`config.py:59`，pydantic `BaseSettings`）对其他非法设置的处理相同；DoH 是显式开启的，写错应在启动时暴露，而不是静默关闭 | — |
| DoH 配置正确，但 C1 选中的 CA **读不到**（`SSL_CERT_FILE` 不存在或不可读，或 `SSL_CERT_DIR` 目录不可用） | **不影响启动** | allowlisted 每次请求都失败关闭，返回 `http.dns_failed`，日志类别为 `doh_ca_unavailable`；unrestricted 不受影响 |

“CA 读不到不影响启动”的理由【实证】：

- `ssl.create_default_context(cafile=不存在)` 在**构造时**就抛 `FileNotFoundError`，而 `capath=不存在` 构造时不报错，要到握手时才失败（本机 Python 3.12.16 实测）。
- 因此若在启动时构造，同为“CA 读不到”，FILE 会阻止启动，DIR 却不会，行为不一致。
- 统一在首次 DoH 连接时构造，并按请求失败关闭：两种情况结果一致，Bot 的聊天等核心功能也不会因插件能力受影响。

上表第一行的注：基线里网页读取在装配 `DirectPageExtractor` 时就会构造带 `trust_env=True` CA 的 SSL 上下文（`application/modules/web.py:296,310`；`web/http_transport.py:196-197`），但装配本身受条件控制，所以基线遇到 `SSL_CERT_FILE` 读不到时是否启动失败，取决于网页配置。本 PR 不改变这一点。

## 3. 安全审查

### 3.1 Bootstrap

- 不用系统 DNS 解析 DoH 主机名，原因有二：fake-ip 环境下会自我依赖；DoH 的目的地也不应由本地 DNS 决定。
- 多个 bootstrap 地址时，**只有 TCP 连接失败**才按配置顺序换下一个。TLS、HTTP 或报文出错，**下一个 bootstrap 零拨号**，直接拒绝（E3）。
- 所有尝试共用 §3.7 的截止时间。

### 3.2 认证与 CA

**服务端认证（必须）**：

- 严格校验证书链，以及主机名或 IP SAN；
- 不提供 `verify=False`；
- 不回退到明文 `http://`、DoT 或 53 端口。

**CA 来源**：基线插件客户端设了 `trust_env=False`，所以只用 certifi（httpx `_config.py:34-40`、`_transports/default.py:153`）。

| 选项 | DoH 连接 | 目标连接 |
|---|---|---|
| C1【推荐，Q9】 | 独立的 `httpx.create_ssl_context(trust_env=True)`：读 `SSL_CERT_FILE`/`SSL_CERT_DIR`，否则用 certifi。客户端仍是 `trust_env=False`，不读代理 | **保持只用 certifi** |
| C2 | 同 C1 | 也读 `SSL_CERT_FILE/DIR` |
| C3 | 只用 certifi | 只用 certifi |

C1 中两类 CA 的隔离见 §3.5 I1。

**信任边界（写入文档）**：

- Host 信任 DoH 运营者，以及 DoH 连接所用的 CA 集合；
- 不做 DNSSEC 校验，也不以 AD 位作为条件；
- DoH 运营者能看到所有插件查询的主机名。

**客户端凭据**：第一阶段不支持（Q2）。

**DoH 请求本身**：

- 只发协议必需的头：`Accept`/`Content-Type: application/dns-message` 和 `Host`。
- 不带插件传入的任何 Header、Authorization、Cookie 或 Proxy-Authorization。
- 不跟随重定向。
- 响应体超过 65535 字节（DNS 报文的协议上限）即拒绝。
- 查询报文设置 RD=1。

### 3.3 DNS 判定：A/AAAA、CNAME、终结否定、转介、未完成别名

报文格式为 RFC 8484 `application/dns-message`，用 `dnspython` 解析（Q3）。

**第一步：每个查询先过报文合法性检查，任一不满足即归为“错误”：**

- HTTP 200；
- Content-Type 正确；
- 能解析成 DNS 报文；
- QR=1，opcode=QUERY；
- question 与请求一致；
- TC=0；
- RA=1（应答来自递归服务）；
- RCODE 为 NOERROR 或 NXDOMAIN；
- 截止时间前收到完整应答。

**第二步：沿 CNAME 链确定链尾。**

- 从 qname 出发，沿 answer 节中 owner 等于当前名的 CNAME 一路走下去，到没有 CNAME 的名为止，这个名就是**链尾**。
- 链有环，或同一 owner 同时有 CNAME 和其他记录，归为“错误”。
- answer 中 owner 不在链上的记录一律忽略：不检查，也不连接。
- 链长不设经验上限，由报文上限和环检测兜住。
- CNAME 目标不按白名单检查：白名单约束的是逻辑主机，最终地址另由 §3.4 检查。

**第三步：归类。**

| 归类 | 条件（均以链尾为准） |
|---|---|
| **地址** | NOERROR，且链尾有本 qtype 的记录 |
| **NODATA（终结否定）** | NOERROR，链尾没有本 qtype 的记录，且 authority 节有一条 SOA，其 owner 是链尾本身或链尾的祖先域（RFC 2308） |
| **NXDOMAIN（终结否定）** | RCODE=NXDOMAIN，指的是链尾不存在（RFC 6604，链尾前可以有 CNAME），且 authority 节有满足同样条件的 SOA |
| **转介 → 错误** | NOERROR，没有本 qtype 记录，authority 节有 NS 而没有 SOA。这说明对方没有替我们完成递归，不能当作“没有地址” |
| **未完成别名 → 错误** | NOERROR，answer 中有 CNAME，链尾既没有本 qtype 记录，也没有满足条件的 SOA。递归服务本应追完整条链；不另发查询去追链尾 |
| **其他无证据否定 → 错误** | 没有本 qtype 记录，也没有 SOA 或 NS。这类应答无法证明“没有”，就不当作“没有” |

要求 SOA 是为了让“没有地址”这个结论有证据，不是新增限制。缺少证据的否定一律视为错误，从而落入失败关闭。

主流递归 DoH 在 NODATA 和 NXDOMAIN 应答中都会附带 SOA【假设：实施前须对所选 DoH 的真实应答做一次只读核对。若某家服务不附带 SOA，按本规则会对仅有单族地址的域名失败关闭，届时需由所有者决定换服务商还是改规则】。

**第四步：合并两族结果**（与第三版相同）。

| A ＼ AAAA | 地址 | NODATA | NXDOMAIN | 错误 |
|---|---|---|---|---|
| **地址** | 两族地址全部检查 | 只用 A 的地址 | 拒绝（结果矛盾） | 拒绝 |
| **NODATA** | 只用 AAAA 的地址 | 拒绝（没有地址） | 拒绝 | 拒绝 |
| **NXDOMAIN** | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| **错误** | 拒绝 | 拒绝 | 拒绝 | 拒绝 |

**错误优先级（确定性）【推荐，Q6】**：

- 两个查询都等到结束或截止时间，**不因一方先失败而取消另一方**。这样 DoH 侧收到的请求数是确定的（E9 断言）。
- 一跳出现多个失败时，只报优先级最高的一个：

| 优先级 | 情况 | 插件看到的结果 |
|---|---|---|
| 1 | 某族归为“地址”，且其中有地址未通过 §3.4 | `PluginPermissionError` |
| 2 | 任一族为“错误”；或两族合并为“拒绝”（矛盾、无地址、NXDOMAIN） | `PluginResult(ok=False, error_code="http.dns_failed")` |
| 3 | 截止时间到时仍有查询没有结束 | `http.request_failed` |

- 只有归为“地址”的应答里的地址，才参与优先级 1 的判断。归为“错误”的报文里即使夹带私网地址，也只算优先级 2，因为这些地址未被采信。
- 现有系统解析路径在“结果为空”时报 `PluginPermissionError`（F2），与 DoH 路径不同。这一差异写入文档；按 D3，系统解析路径不改。

**TTL 缓存**：第一阶段不缓存，每跳都重新查询（Q4）。

### 3.4 地址检查：公网单播与 NAT64

**DoH 路径的地址条件**：地址必须同时满足：

1. `is_global` 为真；
2. `is_multicast` 为假。

此外，若地址落在 `64:ff9b::/96`（NAT64 知名前缀），还要取出内嵌的 IPv4，按同样两条再检查一遍。

本机 Python 3.12.16 实测【实证】：

| 地址 | `is_global` | `is_multicast` | DoH 路径结论 |
|---|---|---|---|
| `224.0.0.1`、`233.252.0.1`、`239.255.255.250` | **True** | True | 拒绝（组播） |
| `ff02::1`（链路本地组播）、`ff05::2`、`ff0e::1` | **True** | True | 拒绝（组播） |
| `64:ff9b::7f00:1`（内嵌 `127.0.0.1`） | **True** | False | 拒绝（内嵌地址不是公网） |
| `64:ff9b:1::a00:1`、`::ffff:10.0.0.1`、`2002:7f00:1::1`、`fc00::1`、`fe80::1`、`::1`、`::`、`0.0.0.0`、`255.255.255.255`、`100.64.0.1`、`198.18.0.1`、`2001:db8::1` | False | — | 拒绝 |
| `2606:4700:4700::1111`、`2001:4860:4860::8888`、`192.88.99.1` | True | False | 通过 |

由此可知，标准库的 `is_global` **不等于“可以作为单播目的地的公网地址”**，组播必须单独排除。上表的分类是对 3.12.16 的实测；不同 Python 补丁版本可能不同，E8 会把实际分类写入产物。

**不改的部分（D3）**：系统解析路径（unrestricted，以及未启用 DoH 的 allowlisted）**不加**组播和 NAT64 检查。`224.0.0.1`、`ff02::1`、`64:ff9b::7f00:1` 在这些路径上仍会通过。这是已记录的现状缺口（N3、N4），不在本 PR 修复。

### 3.5 隔离：CA、bootstrap、Host/SNI、连接池

| # | 隔离要求 | 违反时的后果 |
|---|---|---|
| I1 | **CA 信任隔离**：DoH 的 SSL 上下文只用于 DoH 连接，目标连接只用 certifi（C1）。两者是不同的 `SSLContext` 对象，不共享 | 只为 DoH 配置的私有 CA 会被用来信任任意目标站点 |
| I2 | **bootstrap 隔离**：bootstrap IP 只用于连接 DoH 服务，不进入目标地址集合，也不会因为与目标同 IP 而跳过检查；DoH 应答中的地址也从不用来连接 DoH 服务本身 | 混淆两类信任来源 |
| I3 | **Host/SNI 隔离**：DoH 连接用 DoH 主机名（IP URL 时不发 SNI）；目标连接的 SNI、证书主机名和 Host 头都用该跳的逻辑域名。插件传入的 `Host` 一律丢弃（`http_client.py:22,115`） | 域前置（domain fronting），或拿一个主机名的证书去服务另一个主机名 |
| I4 | **连接池隔离**：DoH 与目标使用两个独立的 httpx 客户端。目标连接**不跨请求、不跨逻辑主机复用**：基线已带 `Connection: close`（`http_client.py:116`）；实施时把目标客户端的 keepalive 设为 0，确保 IP 相同时也重新握手 | 池按 `(scheme, IP, port)` 复用时，为 DoH 主机名或另一逻辑主机验证过的 TLS 连接，会被拿去承载另一主机名的请求 |

E2E 的 E16–E18 分别验证：目标解析到的 IP 恰好等于 bootstrap IP；两个逻辑主机解析到同一 IP；以及目标证书由“只为 DoH 配置的 CA”签发。

### 3.6 固定 IP、多地址与请求次数【推荐，Q6】

**连接顺序**：A 在前，AAAA 在后，各自保持应答中的顺序。

**换地址规则**：

- **只有 TCP 连接失败**，才换同一已检查集合中的下一个地址。不重新解析，也不换解析来源。
- TCP 一旦建立，之后的 TLS、证书、读写或超时失败都直接返回。**下一个候选地址零拨号**（E14），也不重发。这样可以避免重放非幂等请求。

**每一跳的请求次数**：

| 对象 | 次数 |
|---|---|
| DoH 服务 | A 和 AAAA 各恰好一次 HTTP 请求（bootstrap 的 TCP 失败只会增加拨号次数） |
| 目标服务 | 至多一次 HTTP 请求 |

### 3.7 统一截止时间【推荐，Q5】

**基线（F9）**：每个操作各自计时，重定向时重新计时，读响应体也没有总时限。

**DoH 路径的规则**：

- `SafeHttpClient.request()` 取得并发许可后，确定一个截止时间：`deadline = 当时 + plugin_http_timeout_seconds`（`config.py:304`，默认 15.0）。
- 以下所有步骤都只能用剩余时间，剩余时间 ≤ 0 即失败：
  - 所有跳的 DoH 查询，包括 bootstrap 续试；
  - 目标的 TCP 连接、TLS 握手、发送请求、接收响应头；
  - 读完整个响应体；
  - 所有重定向。

未启用 DoH 的 allowlisted 和 unrestricted 都不变（D3）。

### 3.8 重定向：正文与凭据

**每跳流程**：每一跳都重新执行 §2.1 全流程，并扣减同一个截止时间。

**提前拒绝**：

- 跳到白名单之外的主机：在**发出 DoH 查询之前**拒绝（E10）。
- 跳到 IP 字面量：不查 DoH，直接按 §3.4 检查。

**`Location` 解析**：相对 `Location` 以**逻辑 URL** 为基准解析，不以 IP URL 为基准（`http_client.py:144`）【实证】。

**正文与凭据的去留**。基线规则【实证，`http_client.py:107-114,126,145-147`】：是否“同源”以**第一跳**的逻辑 `(scheme, host, port)` 为准；一旦出现不同源的一跳，此后**所有**跳都不再携带调用方数据（Header、Authorization、Body），即使之后又跳回原来的源也一样。

| 重定向 | 同源 | 跨源（或此前已出现跨源） |
|---|---|---|
| 301、302、307、308 | 保留方法、正文、调用方 Header 和 Authorization | **保留方法**；正文、调用方 Header、Authorization 全部丢弃 |
| 303 | 改为 GET，丢弃正文；Header 和 Authorization 保留 | 改为 GET；全部丢弃 |

**基线的一个副作用**：跨源的 307/308 会以原方法（例如 POST）发出一个**没有正文**的请求。DoH 路径是否保留这一行为见 Q11。推荐保留基线语义：不新增拒绝规则，在文档中说明；E2E 记录目标实际收到的方法和正文长度（E12）。

**重定向响应的正文**：不读取，也不返回给插件。连接在 `finally` 中关闭（`http_client.py:189-191`），也不计入响应体大小上限。

### 3.9 DoH 可达性

- DoH 连接不读代理变量，直接连 bootstrap IP 或 URL 中的 IP。在 TUN 环境下由 TUN 接管，按规则转发；TLS 保证内容完整和服务器身份。
- **只能经显式代理出网**的部署里，DoH 不可达，所有 allowlisted 请求都会失败关闭（L1）。
- 不做启动探测，也不设健康门槛；启动只校验配置格式（§2.4）。
- 运维文档给出一次性人工核对方法：在 Bot 容器内向 bootstrap IP 发一次 DoH 查询。
- 看门狗目前看不到这类失败（与 Q8 同源）。

### 3.10 Host 的保证边界：透明代理与 NAT64

**Host 能保证的（DoH 路径）**：Bot 进程的 socket 只会连接一个满足下列全部条件的地址：

- 由可信 DoH 给出，并按 §3.3 判定有效；
- 通过 §3.4 的“公网单播”检查；
- 是本跳已检查集合中的一员；
- TLS 证书由 certifi 信任，且与逻辑域名匹配。

**Host 不保证的**：socket 之外，数据包去了哪里。

| # | 边界外的情况 | 说明 | 状态 |
|---|---|---|---|
| L1 | 只能经显式代理出网 | 不支持，失败关闭 | 【issue】TUN 下直连真实 IP 可用 |
| T1 | 透明代理改写目的地（Clash/Mihomo `sniffer` 开启 `override-destination`） | 代理用 SNI/Host 替换我们拨的 IP，再用它自己的 DNS 重新解析，固定 IP 失效。Host 检测不到。部署须关闭对 Bot 流量的目的地改写，否则 D1 的保证不成立 | 【假设】生产配置未核 |
| T2 | 透明 TLS 解密 | C1 下目标连接只信 certifi，解密代理的 CA 不被目标连接信任，连接会失败。若所有者把该 CA 放进 `SSL_CERT_FILE`，**DoH 应答**的完整性就扩展到信任该代理 | 【推论】 |
| T3 | fake-ip 反向映射 | 我们拨的是真实 IP，不涉及。代理按 IP 规则转给出站节点时，节点连的仍是这个 IP | 【推论】 |
| T4 | `dns-hijack` | 只劫持 53 端口，影响不到 443 上的 DoH | 【推论】 |
| N1 | 运营商自选的 NAT64 前缀（RFC 6052 NSP） | 无法从地址本身识别，内嵌私网 IPv4 的 NSP 地址会通过。本 PR 不为此加配置 | 【推论】 |
| N2 | DNS64 | 会合成 AAAA；但同时查询的 A 里若有私网地址，整跳已被拒 | 【推论】 |
| N3 | 系统解析路径的 NAT64 缺口 | `64:ff9b::7f00:1` 仍视为公网（D3） | 【实证】 |
| N4 | 系统解析路径的组播缺口 | `224.0.0.1`、`ff02::1` 等仍视为公网（D3） | 【实证】 |
| L4 | 把 fake-ip DNS 误配成可信 DoH | 应答落在 `198.18.x`，全部请求被拒，属于失败关闭，不会被绕过 | 【推论】 |
| L5 | DoH 的地理位置与 Bot 出口不同 | CDN 可能返回不是最优的节点，只影响延迟 | 【推论】 |

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
| Q1 | 配置形态（§2.2） | 主机名 URL 必须配 bootstrap；IP URL 不得配 bootstrap，且证书须含 IP SAN；bootstrap 只接受单播地址 |
| Q2 | DoH 客户端凭据 | 第一阶段不支持 |
| Q3 | 是否新增 `dnspython` 依赖 | 接受 |
| Q4 | 是否做 TTL 缓存 | 第一阶段不做 |
| Q5 | 统一截止时间（§3.7） | 只用于 DoH 路径；从取得并发许可时开始计时 |
| Q6 | 错误码、优先级、连接顺序与续试（§3.3、§3.6） | 新增 `http.dns_failed`；优先级依次为：权限错误、DNS 失败、超时；A 在前；只有 TCP 连接失败才换地址 |
| Q7 | 网页读取与媒体下载的同类问题 | 另开 issue |
| Q8 | 看门狗报告插件需要人工处理 | 另开 issue |
| Q9 | CA 来源（§3.2） | C1 |
| Q10 | 启动影响（§2.4） | DoH 配置写错则启动失败；所选 CA 读不到不影响启动，请求失败关闭；未设置 DoH 时与基线相同 |
| Q11 | 跨源 307/308 以原方法发出无正文请求（§3.8） | 保留基线语义，只写入文档，不新增拒绝规则 |

## 7. E2E：先写的失败情况与记录项（实施阶段第一步，现在未写）

### 7.1 测试环境

不访问外网，不连生产。

- **DoH 服务**：进程内 loopback TLS DoH，可按情况返回预设报文。有两类证书：主机名证书，以及含 `127.0.0.1` IP SAN 的证书。CA 为 **DoH 专用的临时 CA**。
- **目标服务**：loopback TLS 目标服务，可做虚拟主机（按 Host 返回不同正文），证书由**目标专用的临时 CA** 签发。E18 另用一张由 DoH 专用 CA 签发的目标证书。
- **网络后端**：测试用 httpcore 网络后端，记录每次 `connect_tcp(ip, port)`，并把预设的“公网”测试 IP 转发到对应的 loopback 服务。这是唯一的注入点；解析、检查和 TLS 都走真实代码。
- **系统解析器**：注入一个可计数的实现。默认对所有名字返回 `198.18.0.x`（模拟 fake-ip）；E15 中返回公网测试 IP。
- **记录型代理**：设置 `HTTPS_PROXY` 指向它，用来证明插件请求从未经过代理。
- **CA 注入**：
  - DoH 专用 CA 经 `SSL_CERT_FILE` 注入，对应 C1；
  - 目标专用 CA 经现有的 `SafeHttpClient(client=...)` 参数注入；
  - 另做一条装配断言：生产装配（`application/modules/plugins.py:107`）中，目标客户端只用 certifi，DoH 客户端使用独立的上下文。
- **入口**：全部经真实的 `BoundHttpFacade` 发起。
- **基线运行**：使用同一套测试代码，DoH 环境变量照样设置。基线没有这些设置项，会直接忽略。

### 7.2 每个情况的记录项

| 记录项 | 记录方式 |
|---|---|
| 结果 | `ok`/`error_code`/异常类型；DoH 失败另记日志类别 |
| DoH 查询 | `(qname, qtype)` 的**有序多重集**（排序后写出）。A 与 AAAA 是并发发出的，到达顺序不确定，不记录 |
| DoH 侧明细 | HTTP 请求次数、收到的 SNI（IP URL 时为“无”）、请求头**名称**集合 |
| DoH 拨号 | `(ip, port)` 的有序多重集（两个查询并发拨号） |
| 目标拨号 | `(ip, port)` 按发生顺序记录（同一跳内的拨号是顺序进行的） |
| 目标侧（每个目标服务） | HTTP 请求次数；方法；SNI；`Host` 头原文；`Authorization`、`Cookie`、`Proxy-Authorization` 及调用方自定义头**是否出现**（只记布尔值，不记值）；正文长度；请求路径 |
| 系统解析器 | 调用次数 |
| 代理 | 连接次数 |
| 地址分类 | E8 记录所用 Python 版本，以及对每个测试地址的 `is_global`/`is_multicast` 实际值 |

### 7.3 情况列表

“基线”一列是 `2aeeac84` 在同一测试下的预期结果。

| # | 情况 | 期望 | 基线 |
|---|---|---|---|
| E1 | 主机名 DoH；allowlisted 域名；A 和 AAAA 都是公网 | 成功。目标拨号 ∈ 应答集合；SNI、Host 为逻辑域名；DoH 多重集为 `{(name,A),(name,AAAA)}`；目标请求 1 次；系统解析器 0 次；代理 0 次；DoH 请求头中没有插件头 | **红**（`PluginPermissionError`） |
| E2 | 同 E1，但 DoH 用 IP URL | 成功；DoH 侧 SNI 为“无” | 不适用 |
| E3 | 首个 bootstrap 上 DoH 的 TLS 失败：证书缺 IP SAN、主机名不符、CA 不受信 | 拒绝；**第二个 bootstrap 零拨号**；目标零拨号 | 不适用 |
| E4 | 首个 bootstrap 的 TCP 连接被拒 | 改拨第二个 bootstrap 后成功 | 不适用 |
| E5 | §3.3 合并表的 16 格逐格测试 | 结果与表一致；被拒时目标零拨号 | 不适用 |
| E6 | 第一步每一项合法性条件，在 A 侧、AAAA 侧各单独违反一次（HTTP 非 200、Content-Type、无法解析、QR、opcode、question、TC=1、RA=0、SERVFAIL/REFUSED、超过 65535 字节、3xx） | `http.dns_failed`；不跟随 3xx；系统解析器 0 次；代理 0 次 | 不适用 |
| E7 | 链与否定：带 SOA 的 NODATA；带 SOA 的 NXDOMAIN（含 CNAME 后的 NXDOMAIN）；转介（只有 NS）；未完成别名；无证据否定；SOA 的 owner 不是链尾的祖先；CNAME 环；同一 owner 同时有 CNAME 与 A；answer 中夹带不在链上的私网 A | 前两种按否定归类；夹带的 A 被忽略，且不出现在拨号中；其余都按“错误”处理；DoH 恰好收到 A、AAAA 各一次 | 不适用 |
| E8 | A 或 AAAA 为 §3.4 表中各地址，包括组播、`64:ff9b::7f00:1` 和公网单播 | 结果与 §3.4 一致；产物写入实际的分类值 | 不适用 |
| E9 | 错误优先级：①A 私网 + AAAA SERVFAIL；②A SERVFAIL + AAAA 挂起超过截止时间；③A 公网 + AAAA 挂起超过截止时间；④A 为“错误”报文，其中夹带私网地址，AAAA NODATA | ① `PluginPermissionError`；② `http.dns_failed`；③ `http.request_failed`；④ `http.dns_failed`。四种情况下 DoH 都恰好收到 A、AAAA 各一次 | 不适用 |
| E10 | 重定向到：白名单外的域名；私网 IP 字面量；白名单内但 DoH 应答为私网的域名 | 前两种：DoH 多重集中不出现新名字；第三种在第二跳拒绝；三种情况首跳目标都恰好 1 次 | 首跳即红 |
| E11 | 同一域名的两跳之间，DoH 应答由公网变为私网（rebinding） | 第二跳拒绝；第一跳的拨号 IP 为当时应答中的地址 | 首跳即红 |
| E12 | 重定向的正文与凭据：①同源 307 带正文和 `auth_secret`；②跨源 307；③跨源之后又 302 回原源；④303；⑤相对 `Location` | ①第二跳保留方法、正文和 Authorization；②第二跳保留方法，正文长度为 0，无 Authorization 和调用方头；③第三跳仍然全部剥离；④改为 GET，正文长度为 0；⑤按逻辑 URL 解析，目标收到正确路径，Host 为逻辑域名 | 首跳即红 |
| E13 | 固定 IP 上的目标出示另一个白名单域名的合法证书 | 拒绝；该地址恰好拨号 1 次 | 首跳即红 |
| E14 | 候选地址：①第一个地址 TLS 失败（握手失败或证书不符）；②第一个地址 TCP 被拒 | ①**第二个候选地址零拨号**；②改拨第二个地址，DoH 不重查，代理 0 次 | 不适用 |
| E15 | 慢读：系统解析器与 DoH **都返回同一个公网测试 IP**；目标按小于单次超时的间隔逐字节发送正文，总时长超过截止时间 | DoH 路径返回 `http.request_failed`。只记录结果码，不记录耗时 | **红**：基线忽略 DoH，走系统解析，能一直读到正文结束并返回成功。这证明了 F9 |
| E16 | 目标域名的 DoH 应答恰好等于 bootstrap IP | 目标连接单独拨号并单独握手，SNI 为目标域名；DoH 连接没有被复用 | 首跳即红 |
| E17 | 两个白名单域名解析到同一 IP，先后各请求一次 | 拨号 2 次；目标侧两次的 SNI 和 Host 分别是两个域名；虚拟主机返回各自的正文 | 首跳即红 |
| E18 | 目标证书由**只为 DoH 配置的 CA**（`SSL_CERT_FILE`）签发 | 拒绝（I1） | 首跳即红 |
| E19 | Host：①插件传入伪造的 `Host`（另一个白名单域名或任意名）；②URL 带非默认端口 | ①目标收到的 Host 和 SNI 都是逻辑域名，伪造值未出现；②Host 为 `name:port`，SNI 为不带端口的 `name` | ①首跳即红 |
| E20 | 插件传入 `Cookie`、`Proxy-Authorization`、`Authorization` 头，并使用 `auth_secret` | 只有 `auth_secret` 生成的 Authorization 到达同源目标；DoH 侧这些头一个都没有 | 首跳即红 |
| E21 | DoH 逐字节慢发应答；首个 bootstrap 的 TCP 挂起 | 共用同一截止时间，超时即失败，不会在截止后续试 | 不适用 |
| E22 | **未启用** DoH，系统 DNS 返回 fake-ip | 仍然拒绝；系统解析器被调用 | 绿 |
| E23 | 同时批准 unrestricted 与 allowlisted，并启用 DoH；另含 unrestricted 请求 `224.0.0.1` 字面量 | 走基线路径：系统解析器被调用，DoH 0 次；组播字面量的结果与基线**相同**（直接记录基线结果，不预设） | 绿 |
| E24 | allowlisted 的公网 IP 字面量 | DoH 0 次，直接拨该 IP | 绿 |
| E25 | 启动：①未设置 DoH，`SSL_CERT_FILE` 读不到；②各种 DoH 配置写错；③DoH 配置正确，但 `SSL_CERT_FILE` 或 `SSL_CERT_DIR` 读不到 | ①启动结果与基线逐项相同；②启动失败；③启动成功，allowlisted 请求返回 `http.dns_failed`，日志类别为 `doh_ca_unavailable`，unrestricted 不受影响 | ①即为基线的记录 |

### 7.4 产物

- 存放位置：`docs/architecture/evidence/issue-678-plugin-http-doh/`。
- 格式：每个情况一行 JSON，字段为 §7.2 的记录项。
- 可复现性：
  - 端口替换为角色名；
  - 不含时间戳；
  - 多重集排序后再写；
  - 重复运行字节一致。
- 同目录保存基线红跑的输出：E1 和 E15 必须为红，E22–E25 的基线结果作为对照。

## 8. 生产只读复核（本阶段未执行）

- Bot 日志中，2026-10-22 之后 `github_poll_failed error_category=PluginPermissionError` 的出现情况；
- 同期网页读取和媒体下载的 `private_url` 次数（§1.4）；
- Clash 配置中 `sniffer` 与 `override-destination` 是否开启（T1）；
- 在 Bot 容器内，所选 DoH 的 bootstrap IP 能否建立 TLS（§3.9）。只读，不改配置。


## 9. 实施时需同步删改的文档

- **`docs/plugin-development/networking.md`、`security.md:31`**：
  - DoH 路径的解析来源与信任边界（§3.2）；
  - 公网单播规则（§3.4）；
  - 截止时间（§3.7）；
  - 重定向时正文和凭据的去留（§3.8）；
  - 保证边界（§3.10），以及 unrestricted 和未启用 DoH 的路径不变、N3/N4 现状缺口。
- **`docs/operations/http-proxy.zh-CN.md`**：
  - 插件 HTTP 仍不走代理；
  - DoH 配置项、启动影响（§2.4）和 CA 来源；
  - 可达性的人工核对方法；
  - T1 对 sniffer 的部署要求。
- **`.env.example`**：两项新配置的注释示例。
- **测试**：`tests/` 中目前没有覆盖插件 HTTP 的测试，无需改动冻结的测试。

## 10. 本阶段边界

本阶段只修改本任务书：

- 未写测试、未改代码、未加依赖；
- 未连生产、未改部署配置；
- 分支只追加提交，原 draft PR #679 不合并。
