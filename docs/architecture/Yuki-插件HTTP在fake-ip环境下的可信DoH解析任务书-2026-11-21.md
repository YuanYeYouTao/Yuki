# Yuki 插件 HTTP 在 fake-ip DNS 环境下的可信 DoH 解析任务书（2026-11-21）

状态：**第一阶段第二版：方向已由用户决定（§0），仍未实现任何代码**。剩余待决见 §6。确认后在同一分支先写 E2E（§7），在基线上跑红，然后再实施。对应 [#678](https://github.com/YuanYeYouTao/Yuki/issues/678)，draft PR [#679](https://github.com/YuanYeYouTao/Yuki/pull/679)。分支 `docs/issue-678-plugin-http-proxy-taskbook`，基线 origin/main `2aeeac84`。

标记约定沿用前几份任务书：
- 【实证】：源码、现有文档或本机命令查到的事实；
- 【issue】：#678 给出的生产现象，本阶段**没有**到生产只读复核；
- 【假设】：还没查实，需要在实施或 E2E 中确认；
- 【决定】：用户已定；
- 【推荐】：尚待用户确认的建议。

路径省略 `src/yuki/`，行号以 `2aeeac84` 为准。

## 0. 用户决定（2026-11-21）与版本变更

| # | 决定 |
|---|---|
| D1 | 推荐方案改为：**显式配置的可信 DoH 解析**；每一跳检查全部地址；连接固定到已检查的 IP；任何失败即拒绝 |
| D2 | “按域名经代理”降为备选，不采用。**不接受**白名单域名经代理后可能连到内网 |
| D3 | `network.http.unrestricted` 保持现状 |
| D4 | 不放行 fake-ip 段 |
| D5 | SOCKS 不纳入 |

相对第一版（`61a93673`，原文件名 `Yuki-插件HTTP在fake-ip环境下经代理访问任务书-2026-11-21.md`）的变更：
- 推荐方案由“代理”改为 DoH，代理方案的不选理由见 §4；
- 更正 `subscription-monitor` 威胁模型的描述（§1.3）；
- 新增 DoH 安全审查（§3），覆盖 bootstrap 与认证、A/AAAA/CNAME、重定向、部署限制和不静默回退；
- 新增 NAT64 地址分类缺口（§3.4）；
- E2E 按新方案重写（§7）。

## 1. 现象与原因

### 1.1 现象【issue】

`github-monitor` 从 2026-10-22 04:05 起没有成功轮询过 GitHub，状态一直显示“运行中”。手动 `sync ... baseline` 清掉 `github_cursor_missing_from_overlap` 后，每次请求都报 `PluginPermissionError`。

部署是 Mac 上的 Clash Verge，开了 TUN 和 `enhanced-mode: fake-ip`（`198.18.0.1/16`）。容器内 `api.github.com` 解析为 `198.18.0.83`，`github.com` 解析为 `198.18.0.91`。

环境侧已把 `+.github.com` 加入 `fake-ip-filter`，之后 GitHub 恢复。这说明在该部署里，**直连 GitHub 真实公网 IP 是通的**【issue】。

### 1.2 拒绝发生在哪里【实证】

| # | 事实 | 依据 |
|---|---|---|
| F1 | 插件 HTTP `trust_env=False`，不读 `HTTP(S)_PROXY`/`ALL_PROXY` | `plugin_host/http_client.py:64-68` |
| F2 | 每一跳（含重定向）先查白名单，再用系统 `getaddrinfo` 解析；结果中只要有一个地址 `not is_global`，就抛 `PluginPermissionError("plugin HTTP target resolved to a non-public address")` | `http_client.py:212-228,298-301` |
| F3 | 通过后把 URL 主机换成第一个已校验的 IP 直连，Host 头和 SNI 保留原域名，避免二次解析 | `http_client.py:229-240,115-119` |
| F4 | Python 把 `198.18.0.0/15` 判为 `is_global=False`（本机 3.12.16 实测） | 标准库 `ipaddress` |
| F5 | `unrestricted` 只是把白名单置为 `None`，地址检查完全相同 | `http_client.py:262-265` |
| F6 | 现行文档承诺 unrestricted “不代表可以访问 Host 内网……Host 安全底线仍然适用” | `docs/plugin-development/networking.md`；`docs/plugin-development/security.md:31` |
| F7 | 插件与 Bot 同进程；Plugin API 是“访问治理，不是 Python/OS 沙盒” | `docs/plugin-development/security.md:5` |
| F8 | 仓库没有 DNS 报文库：`uv.lock` 中无 `dnspython`；现有依赖为 `httpx[socks]` | `uv.lock`、`pyproject.toml:14` |

结论：F2 的检查本身没错，错在**解析来源**。fake-ip 环境下系统 DNS 不给真实地址，所以凡是不在 filter 里的域名都会被拒。

### 1.3 威胁模型（更正第一版）【实证】

由 F7 可知，这道地址检查防不了恶意插件，因为恶意插件可以直接 `import socket`。它防的是**可信插件替人取了不该取的地址**。

第一版写的是：“聊天里有人订阅 `http://192.168.5.1/` …… 就是本 issue 要守住的 SSRF”。这一句**有误**。核对 `subscription-monitor` 源码后：

| 事实 | 依据 |
|---|---|
| 权限列表确有 `network.http.unrestricted`、`plugin.config.write`、`command.register` | `plugins/subscription-monitor/plugin.toml` |
| 唯一的命令 `monitor`（add/remove 订阅等）注册为 `PermissionLevel.SUPERUSER` | `plugins/subscription-monitor/subscription_monitor/plugin.py:45-56` |
| Host 只在 `superuser` 为真时放行 SUPERUSER 命令 | `plugin_host/command_adapter.py:202` |
| 订阅 URL 校验只查 scheme、凭据和 fragment，明写“DNS and redirect checks remain owned by the Host HTTP facade” | `subscription_monitor/config.py:40-51` |
| 插件唯一的出站请求是 `GET subscription.url`；条目里的链接只出现在摘要文本中，不会被请求 | `subscription_monitor/polling.py:236`、`:66-70` |
| `capabilities/`、`executor/` 中没有调用插件命令的入口，主 Agent 工具不能代替用户执行 `monitor add` | 对 `plugin_command`/`command_adapter` 的 grep，范围仅限这两个目录 |

因此 unrestricted 插件的 SSRF 输入来自三处，**都不是普通聊天用户**：
- 超管配置的 URL（可能写错，或被诱导填入内网地址）；
- feed 服务器返回的**重定向**（第三方控制）；
- feed 域名自身的 **DNS**（第三方控制，可能指向内网或做 rebinding）。

allowlisted 插件的主机由 Manifest 固定并经所有者批准。插件代码和输入只能影响路径和查询串；主机能否跨出白名单，取决于重定向（每跳查白名单）和被批准域名自己的 DNS。

### 1.4 同类路径：网页读取与媒体下载【实证 + 假设】

`services/media_resolver.py:439-461` 也是“本地解析 → 全部 `is_global` → 改成 IP”，网页读取固定 `allow_private_urls=False`（`web/page_extractor.py:54`）。由此推断，fake-ip 环境下本机网页读取和媒体 URL 下载也会以 `private_url` 被拒。这一点**未经生产复核**【假设】。不在本 PR 范围内，见 Q7。

## 2. 推荐方案：显式配置的可信 DoH 解析【决定 D1】

### 2.1 行为

只作用于 allowlisted。unrestricted 按 D3 不变，见 §2.3。

**每一跳（首个 URL 和每次重定向）依次执行：**
1. URL 解析、scheme、凭据、IDNA、白名单（现有逻辑不变）。
2. 主机是 IP 字面量时，不做 DNS，地址必须通过 §3.4 的检查。
3. 主机是域名时，向**配置的 DoH 服务**查询 A 和 AAAA，按 §3.3 校验报文和 CNAME 链，得到这一跳的完整地址集合。
4. 集合为空，或其中**任一**地址不通过 §3.4，拒绝。拒绝时不连接目标，也不尝试其他解析来源。
5. 连接固定到集合中**已检查**的地址；Host 头、SNI 和证书校验都使用原域名（沿用 F3）。

**配置与回退：**
- 未配置 DoH：行为与现在逐字节相同，仍用系统解析器。fake-ip 环境下继续被拒，见 E14。
- 配置了 DoH：系统解析器在插件 HTTP 路径上**完全不被调用**（E1 断言调用次数为 0）。

### 2.2 配置形态【推荐，Q1】

建议用一个 URL 加一组 bootstrap IP，例如：

```dotenv
PLUGIN_HTTP_DOH_URL=https://dns.example/dns-query
PLUGIN_HTTP_DOH_BOOTSTRAP=203.0.113.53,2001:db8::53
```

- URL 必须是 `https://`，带主机名和路径；启动时校验，非法则启动失败，而不是静默禁用。
- bootstrap 是该主机名对应的 IP，Host **不会**用系统 DNS 去解析 DoH 主机名（§3.1）。
- bootstrap 是所有者显式提供的出口配置，不是插件输入，因此**不要求**它是公网地址。这样允许所有者使用局域网内自建的 DoH（例如 AdGuard Home）。
- 不提供“自动探测 fake-ip”或“自动选 DoH 服务商”，也不内置默认服务商。

与 AGENTS.md“不新增替代机制”的关系：这是一个新配置项，但它只替换**解析来源**，不新增第二条连接路径，现有检查和固定 IP 逻辑原样复用。是否接受这一新增，已由 D1 决定。

### 2.3 unrestricted 保持现状的后果【决定 D3，记录后果】

unrestricted 继续用系统解析器。fake-ip 环境下 `subscription-monitor` 对不在 `fake-ip-filter` 中的 feed 域名仍会失败，所有者需要在环境侧逐个加 filter。

如果以后要让 unrestricted 也走 DoH，只需把 §2.1 应用到 `allowed_hosts is None` 的分支，安全性质相同。这是另一个决定，不在本 PR 内。

## 3. 安全审查

### 3.1 Bootstrap：DoH 主机名怎么解析

| 做法 | 结论 |
|---|---|
| 用系统 DNS 解析 DoH 主机名 | **不采用**。fake-ip 下会得到 `198.18.x`，形成自依赖；同时让 DoH 连接的目的地由本地 DNS 决定 |
| URL 直接写 IP（如 `https://1.1.1.1/dns-query`） | 可用，但证书必须含 IP SAN。各服务商是否提供【假设，未核】。§2.2 的“主机名 + bootstrap IP”覆盖这种情况：主机名留空即可，是否允许见 Q1 |
| 主机名 + bootstrap IP【推荐】 | 连 bootstrap IP，SNI 和证书校验用 URL 主机名。IP 只决定 TCP 目的地，身份由 TLS 证明 |

**多个 bootstrap IP：** 只在 TCP 连接失败时按配置顺序试下一个。TLS 失败、HTTP 错误或报文错误**不换下一个 IP**，直接拒绝，避免把认证失败当成可用性问题处理。

### 3.2 认证

**服务端认证（必须）：**
- 严格 TLS 证书校验，证书链加主机名，CA 来源与现有 httpx 一致（`trust_env` 只用于 `SSL_CERT_FILE/DIR`；实现时核对 httpx 在 `trust_env=False` 时是否仍读这两个变量【假设】）。
- 不提供 `verify=False`、明文 `http://`、DoT 或 53 端口回退。

**信任边界（写入文档）：**
- Host **信任 DoH 运营者**给出的答案。
- Host 不做 DNSSEC 验证，也不以 AD 位作为条件。
- DoH 运营者能看到所有插件查询的主机名，这是隐私代价。

**客户端认证：** 第一阶段**不支持**向 DoH 发送凭据（Bearer、mTLS）【推荐，Q2】。如果所有者的自建 DoH 需要认证，再单独设计凭据存储与脱敏。

**DoH 请求本身：**
- 不跟随重定向；
- 不读代理环境（与 D2 一致）；
- 不带 Cookie；
- 响应体按 DNS 报文协议上限 65535 字节截断即拒绝；
- 解析加连接共用现有每跳 `timeout_seconds`（`http_client.py:219`），不新增经验阈值。

### 3.3 报文：A / AAAA / CNAME

- **协议：** RFC 8484 `application/dns-message`，POST 或 GET 二选一，在实施时定。`application/dns-json` 不是标准，不采用。
- **报文库：** 需要新增 `dnspython` 依赖（F8）。自写解析器要处理压缩指针、截断和越界，风险更高。是否接受新依赖见 Q3。
- **每个域名查两次：** qtype A 和 AAAA 都要查。两次之间的组合规则：
  - 任一查询 SERVFAIL、REFUSED、超时、HTTP 非 200、Content-Type 不符、报文无法解析、TC 位置位，或 question 与请求不一致：**整跳拒绝**。理由是 D1 要求检查**全部**地址，拿不到其中一族的答案时不能宣称已检查全部。
  - 两族都 NXDOMAIN：拒绝。
  - 一族 NOERROR/NODATA、另一族有地址：可以，地址集合只来自有答案的一族。
  - 两族都 NODATA：集合为空，拒绝。
- **CNAME 链：**
  - 从 qname 起，在 answer 节逐个跟随 CNAME，直到找到 owner 等于链尾的 A/AAAA。
  - answer 中 owner 不在链上的记录**忽略**，防止服务器夹带无关地址；这些记录也不会被连接。
  - 链有环，或链尾没有地址记录：拒绝。
  - 是否另发查询去追链尾，由实施时对递归 DoH 的行为确认【假设：主流递归 DoH 会在一次应答中给出完整链】；无论如何，链尾地址同样必须过 §3.4。
  - CNAME 目标名**不**按白名单检查。白名单约束的是逻辑主机，CDN 的 CNAME 属正常情况；安全性由最终地址检查保证。
  - DNAME 按 dnspython 的合成 CNAME 处理【假设】。
- **缓存：** 第一阶段不缓存，每跳都查。按 TTL 缓存是事实驱动的优化，可以以后加，但会让 E11 的 rebinding 断言更复杂。见 Q4。

### 3.4 地址检查

沿用“任一非 `is_global` 即拒绝”。另有一处现状缺口【实证】：Python 3.12.16 把 `64:ff9b::7f00:1`（NAT64 知名前缀内嵌 `127.0.0.1`）判为 `is_global=True`。在有 NAT64 网关的网络里，这个地址会被翻译到内嵌的 IPv4。

本机实测的其他分类：
- `::ffff:10.0.0.1`、`2002:7f00:1::1`、`64:ff9b:1::a00:1`、`fc00::1`、`fe80::1`、`::1`、`100.100.100.100`、`198.18.0.1`：均为 False；
- `2606:4700:4700::1111`、`192.0.0.9`：为 True。

推荐：对 `64:ff9b::/96` 取出内嵌 IPv4 再检查，非 global 则拒绝【推荐，Q5】。这也影响现有系统解析路径和 unrestricted；如果改，两条路径共用同一个检查函数，而不是只修 DoH 路径。

### 3.5 固定 IP 与多地址

- 只连接 §3.4 检查过的地址。SNI、Host 头和证书主机名都是原域名，证书不匹配就拒绝（E12）。
- 多地址时的连接顺序见 Q6。【推荐】A 记录在前、AAAA 在后，各按应答顺序。
- TCP 连接失败时，可以在**同一已检查集合**内试下一个地址；不重新解析，不换解析来源。
- 只要 TCP 已建立，后续任何失败都直接返回错误，不再换地址。原因有两点：防止重放非幂等请求；保持“失败即拒绝”。

### 3.6 重定向

- 每跳重新执行 §2.1 全流程：白名单 → 字面量或 DoH → 全地址检查 → 固定 IP。
- 跳到非白名单主机：在**发出 DoH 查询之前**拒绝，DoH 服务看不到这个名字（E10 断言）。
- 跳到 IP 字面量：不查 DoH，直接按 §3.4 检查。
- 现有规则不变：
  - 跨源（scheme、host、port 三元组）剥离 Authorization、调用方 Header 和 Body；
  - 303 改 GET；
  - 最多 5 次重定向。
- 现状允许 `https → http` 降级重定向，此时因跨源会剥离凭据。本 PR 不改这一点，仅记录。

### 3.7 不静默回退

配置了 DoH 后，以下任何失败都**直接拒绝**。不回退系统 DNS，不走代理，不换明文 DNS，不跳过检查：
- DoH 连接、TLS、HTTP、报文、超时；
- 地址检查；
- 目标连接。

错误映射沿用现有两类：
- 地址不通过：`PluginPermissionError`；
- 解析或连接失败：`PluginResult(ok=False, error_code="http.request_failed")`。是否为 DoH 失败单独给 `error_code`（如 `http.dns_failed`）见 Q6。

日志只记录类别，不写 URL、查询名或 DoH 响应原文。这与现有 `redact_httpx_url` 一致（`http_client.py:129`）。

### 3.8 部署限制（写入运维文档）

| # | 限制 | 状态 |
|---|---|---|
| L1 | 需要直连公网 IP 的出口。TUN 环境下成立：加入 filter 后直连 GitHub 真实 IP 已工作。**只能经显式代理出网**的部署不受支持，此时如实失败，不会改走代理 | 【issue】 |
| L2 | 透明代理改写目的地会破坏 IP 固定：例如 Clash/Mihomo `sniffer` 开启 `override-destination` 时，会用嗅探到的 SNI 域名替换目的 IP，并用**代理自己的 DNS** 再解析。固定 IP 的保证只到 Bot 进程的 socket 为止，生产 sniffer 配置未核 | 【假设】 |
| L3 | DoH 服务本身必须在该网络可达。例如大陆网络直连 `1.1.1.1`/`8.8.8.8` 的 DoH 可能不可用；经 TUN 时由 Clash 按 IP 规则转发。可达性未核 | 【假设】 |
| L4 | 若配置的 DoH 实际是 fake-ip DNS（例如误填 Clash 自己的 DNS），答案落在 `198.18.x`，所有请求被拒，属于失败关闭而非绕过 | 【推论】 |
| L5 | DoH 服务看到的是自身位置，不是 Bot 出口位置。CDN 可能返回非最优节点，影响延迟，不影响正确性 | 【推论】 |
| L6 | 配置修改需重建 Bot 容器，同代理变量的现有规则（`docs/operations/http-proxy.zh-CN.md`） | 【实证】 |

## 4. 备选方案与不选理由

### P. 配置了代理时按域名经代理（第一版推荐）【不选，D2】

- 由代理解析域名，Host 只看到名字，看不到最终地址。被批准域名的 DNS 指向内网，或代理规则 `DIRECT` 到局域网时，请求会连进内网（代理 `192.168.5.2` 本身就在局域网）。所有者明确不接受这一点。
- 防护效果取决于部署者的代理规则，代码和 E2E 都无法证明生产配置。
- 非规范 IPv4 写法（`0177.0.0.1`、`2130706433`，`ipaddress` 当作主机名，本机实测）交给代理后怎么解释无法控制。

### B. 放行 fake-ip 段 `198.18.0.0/15`【不选，D4】

- fake-ip 下 Clash 对所有不在 filter 中的名字都返回 fake IP，`127.0.0.1.nip.io`、`router.lan` 都能通过检查，再由 TUN 映射回域名去连。地址检查在该环境对**所有**主机名失效。
- fake-ip 段在 Clash 中可配（本部署是 `/16`），写死就是把代理软件的约定硬编码进 Host。
- 会误放真实网络中的 `198.18.x`。
- 与 `sandbox/network_guard.py:24` 把该段列为 PRIVATE 相矛盾。

### C'. DoH + 经 HTTP 代理 CONNECT 已校验 IP【本阶段不做】

做法是先 DoH 检查，再让代理 CONNECT 到**已固定的 IP**（网页读取现有的做法，`web/http_transport.py:199-236`），这样能覆盖 L1“只能经代理出网”的部署。不做的原因：
- 本部署 TUN 直连可用，不需要；
- 代理侧仍可能有 L2 式的目的地改写；
- 会把代理选择、`NO_PROXY` 和隧道清理一起引入插件路径，扩大本 PR 范围。

需要时另开 issue。

### S. SOCKS【不纳入，D5】

### D. 只在环境侧逐域名加 `fake-ip-filter`【现状临时处理】

保留为文档中的可选做法：未配置 DoH 时的手段，也是 unrestricted 在 fake-ip 下唯一的办法（§2.3）。

## 5. 看门狗与 README（与第一版相同）

- **插件卡在 gap 上一个月无人知道：** 建议另开 issue。看门狗 `components()` 只汇总网关、Dream、Embedding（`container.py:996-1024`），`loops()` 中只有 `plugin_background_turns`（`container.py:986`），没有插件自报的“需要人工处理”事实。可先评估让插件用已有的 `notification.publish`（`yuki_plugin_sdk/permissions.py:50`）自行通知；这需要 github-monitor 源码确认【待核查】。
- **github-monitor 源码与 README：** 不在本仓库（`plugins/` 只有 `subscription-monitor`），本 PR 无法修改。

## 6. 剩余待决

| # | 问题 | 推荐 | 理由 |
|---|---|---|---|
| Q1 | 配置形态：URL + bootstrap IP；是否也允许纯 IP 的 DoH URL | URL（主机名）+ 必填 bootstrap；也允许纯 IP URL，此时 bootstrap 可省略，证书须含该 IP | §3.1；两种都不碰系统 DNS |
| Q2 | 是否支持 DoH 客户端凭据 | 第一阶段不支持 | §3.2；无已知需求，凭据存储需另行设计 |
| Q3 | 是否新增 `dnspython` 依赖 | 是 | §3.3；自写 DNS 报文解析风险更高 |
| Q4 | DoH 答案是否按 TTL 缓存 | 第一阶段不缓存 | §3.3；先保证每跳检查可证，性能需要时再加 |
| Q5 | NAT64 `64:ff9b::/96` 内嵌地址检查，是否同时修现有系统解析路径和 unrestricted | 是，两条路径共用一个检查 | §3.4；这是“全部地址必须公网”规则的正确性问题，不是新限制 |
| Q6 | 多地址连接顺序与 TCP 失败后是否试下一个已检查地址；DoH 失败是否单独给 `http.dns_failed` | A 在前；仅 TCP 失败时在已检查集合内续试；给单独错误码 | §3.5、§3.7 |
| Q7 | 网页读取和媒体下载（§1.4）是否另开 issue | 另开 | 不同模块、不同承诺 |
| Q8 | 看门狗报插件“需人工处理”是否另开 issue | 另开 | §5 |

## 7. 先写的失败情况与 E2E 设计（实施阶段第一步，现在未写）

**测试环境（不访问外网、不连生产）：**
- **DoH 服务：** 进程内 loopback TLS DoH 服务，由临时 CA 签发证书，按情况返回预设报文，并记录收到的每个查询（名字、qtype）。
- **网络后端：** 测试用 httpcore 网络后端记录每次 `connect_tcp(ip, port)`，并把预设的“公网”测试 IP 转发到 loopback 目标 TLS 服务。这样 E2E 能断言**实际连接的 IP**，同时目标地址仍能通过 global 检查。这是唯一的注入点；解析和检查走真实代码。
- **系统解析器：** 注入一个对所有名字返回 `198.18.0.x` 的实现，模拟 fake-ip，并记录调用次数。
- **代理环境：** 设置 `HTTPS_PROXY` 指向一个记录型 loopback 代理，用来证明插件 HTTP 从不碰代理。
- **入口：** 全部经真实 `BoundHttpFacade`（插件看到的入口）发起。

| # | 情况 | 期望 | 基线 `2aeeac84` 预期 |
|---|---|---|---|
| E1 | 配置 DoH；allowlisted 域名；DoH 返回公网 A+AAAA | 成功；实际连接 IP ∈ 应答集合；SNI/Host 为域名；系统解析器 0 次；代理 0 次 | **红**：`PluginPermissionError` non-public |
| E2 | A 中一个公网、一个私网 | 拒绝；目标 0 连接 | 不适用（基线无 DoH），实施后断言 |
| E3 | A 公网，AAAA 为 `::1` / `fc00::1` / `::ffff:10.0.0.1` / `64:ff9b::7f00:1`（后者按 Q5） | 拒绝 | 同上 |
| E4 | CNAME 链尾为私网；链断；链有环；answer 夹带不在链上的私网 A | 前三种拒绝；夹带的记录被忽略且不被连接 | 同上 |
| E5 | DoH 返回 SERVFAIL / 超时 / 非 200 / 错误 Content-Type / 无法解析 / TC=1 / question 不符；以上各在 A 或 AAAA 一侧 | 拒绝（`http.request_failed` 或 Q6 定的码）；系统解析器 0 次；代理 0 次 | 同上 |
| E6 | DoH 证书不受信 / 主机名不符 | 拒绝；不换 bootstrap IP | 同上 |
| E7 | DoH 返回 3xx | 不跟随，拒绝 | 同上 |
| E8 | 两族 NXDOMAIN；两族 NODATA | 拒绝 | 同上 |
| E9 | A 有地址，AAAA NODATA | 成功 | 同上 |
| E10 | 重定向到：非白名单域名 / 私网字面量 / 应答为私网的白名单域名 | 前两种在发出 DoH 查询前拒绝（DoH 记录中无该名字）；第三种在第二跳拒绝 | 起点即红 |
| E11 | 同一域名两跳间 DoH 答案从公网变私网（rebinding） | 第二跳拒绝；第一跳连接 IP 为当时应答 | 同上 |
| E12 | 固定 IP 上的目标证书不匹配逻辑域名 | 拒绝 | 同上 |
| E13 | 第一个已检查地址 TCP 拒绝连接 | 按 Q6：续试下一个已检查地址或直接失败；不重新解析，代理 0 次 | 同上 |
| E14 | **未配置** DoH，系统 DNS 为 fake-ip | 仍拒绝（证明未放行 fake-ip，D4） | 绿 |
| E15 | 配置 DoH 的 unrestricted 插件 | 仍走系统解析器（D3）；fake-ip 下拒绝；DoH 0 次 | 绿 |
| E16 | 跨源重定向（两个都在白名单） | 第二跳不带 Authorization、调用方 Header 和 Body | 起点即红 |
| E17 | allowlisted 公网 IP 字面量 | 不查 DoH，直接连接该 IP | 绿 |
| E18 | 启动配置：`http://` DoH URL、主机名 URL 缺 bootstrap、bootstrap 非 IP | 启动失败，不静默禁用 | 不适用 |

**产物：**
- 位置：`docs/architecture/evidence/issue-678-plugin-http-doh/`；
- 内容：每个情况一行 JSON，字段为情况号、结果码、DoH 收到的（名字、qtype）列表、实际 `connect_tcp` 的 IP 列表、系统解析器次数、代理记录数；
- 排序写出，不含时间戳和随机端口，重复运行字节一致；
- 同目录保存基线红跑输出。

## 8. 生产只读复核（本阶段未执行）

- Bot 日志中 2026-10-22 之后 `github_poll_failed error_category=PluginPermissionError` 的出现情况（只读 grep）；
- §1.4 假设：同期网页读取和媒体下载的 `private_url` 次数；为零时还需确认同期是否有请求，不能直接推断不受影响；
- L2：Clash 配置中 `sniffer` 及 `override-destination` 是否开启（只读查看，不修改）。

## 9. 实施时需同步删改的文档

- `docs/plugin-development/networking.md`、`security.md:31`：
  - 写明 allowlisted 在配置 DoH 时的解析来源和信任边界（§3.2）；
  - unrestricted 仍用系统解析器（D3）。
- `docs/operations/http-proxy.zh-CN.md`：
  - 插件 HTTP 仍不走代理（D2）；
  - 新增 DoH 配置与 L1–L6。
- `.env.example`：新增两项配置的注释示例。
- `tests/` 中目前没有引用 `SafeHttpClient`、`BoundHttpFacade` 或其拒绝文案的测试（于 `2aeeac84` grep），没有需要删改的冻结测试；§7 将是这条路径的第一组测试。

## 10. 本阶段边界

只修改本任务书（含改名）。未写测试，未改代码，未加依赖，未连生产，未改部署配置。分支只追加提交到同名远端分支，原 draft PR #679 不合并。
