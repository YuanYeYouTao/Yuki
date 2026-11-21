# Yuki 插件 HTTP 在 fake-ip DNS 环境下经代理访问任务书（2026-11-21）

状态：**第一阶段：调研与待决，未实现任何代码**。等用户对 §5 的 Q1–Q6 作出决定后，再在同一分支先写 E2E（§6）、在基线上跑红，再实施。对应 [#678](https://github.com/YuanYeYouTao/Yuki/issues/678)。分支 `docs/issue-678-plugin-http-proxy-taskbook`，基线 origin/main `2aeeac84`。

标记约定沿用前几份任务书：【实证】是源码或现有文档里查到的事实；【issue】是 #678 给出的生产现象，本阶段**没有**到生产只读复核；【假设】是还没查实、需要在实施或 E2E 中确认的判断；【推荐】是对待决问题的建议，尚未批准。路径省略 `src/yuki/`，行号以 `2aeeac84` 为准。

## 1. 现象与原因

### 1.1 现象【issue】

`github-monitor` 自 2026-10-22 04:05 起没有成功轮询过 GitHub，状态一直“运行中”。手动 `sync ... baseline` 清掉 `github_cursor_missing_from_overlap` 后，每次请求都是 `PluginPermissionError`。部署是 Mac 上的 Clash Verge，TUN + `enhanced-mode: fake-ip`（`198.18.0.1/16`），容器内 `api.github.com → 198.18.0.83`、`github.com → 198.18.0.91`。环境侧已把 `+.github.com` 加入 `fake-ip-filter`，只修好了 GitHub。

### 1.2 拒绝发生在哪里【实证】

| # | 事实 | 依据 |
|---|---|---|
| F1 | 插件 HTTP 客户端 `trust_env=False`，不读 `HTTP(S)_PROXY`/`ALL_PROXY` | `plugin_host/http_client.py:64-68` |
| F2 | 每一跳（含重定向）先查白名单，再本地 `getaddrinfo`；结果中**任一**地址 `not is_global` 即抛 `PluginPermissionError("plugin HTTP target resolved to a non-public address")` | `http_client.py:212-228,298-301` |
| F3 | 通过后把 URL 主机替换成第一个已校验 IP 直连，Host 头和 SNI 保留原域名（防 DNS rebinding 的二次解析） | `http_client.py:229-240,115-119` |
| F4 | Python 对 `198.18.0.0/15`（RFC 2544 基准测试段）判为 `is_global=False`；本机实测 `ipaddress.ip_address('198.18.0.83').is_global == False` | Python 标准库 |
| F5 | `unrestricted` 只是把白名单置为 `None`，其余地址检查完全相同 | `http_client.py:262-265` |
| F6 | 现行文档承诺：“`network.http.unrestricted` ……也不代表可以访问 Host 内网……Host 安全底线仍然适用” | `docs/plugin-development/networking.md`；`docs/plugin-development/security.md:31` |
| F7 | 插件与 Bot 同进程，API 是“访问治理，不是 Python/OS 沙盒”，插件源码视为本地可信代码 | `docs/plugin-development/security.md:5` |
| F8 | `docs/operations/http-proxy.zh-CN.md` 明写“插件通用 HTTP……继续沿各自现有传输设置”，即插件 HTTP 当前**有意**不走代理 | 该文件“路由与范围”节 |

所以 issue 的原因判断成立：插件 HTTP 是唯一一条“自己解析 + 直连”的插件出站路径，fake-ip 下所有非 filter 域名都被 F2 拒绝。

### 1.3 威胁模型由此确定【实证 + 推论】

由 F7，地址检查**不是**防恶意插件的边界（恶意插件可直接 `import socket`）。它防的是**混淆代理（confused deputy）**：可信插件替用户去取一个用户给的 URL。`subscription-monitor` 就是这种：`network.http.unrestricted` + `plugin.config.write` + `command.register`，订阅地址来自命令配置（`plugins/subscription-monitor/plugin.toml`）。聊天里有人订阅 `http://192.168.5.1/`、`http://127.0.0.1.nip.io:9090/configs`（Clash 外部控制器）这类地址，就是本 issue 要守住的 SSRF。`allowlisted` 的主机由 Manifest 固定并经所有者批准，用户只能影响路径和查询串。

### 1.4 同类路径：网页读取与媒体下载【实证 + 假设】

`services/media_resolver.py:439-461` 也是“本地解析 → 全部 `is_global` → 改写成 IP”，网页读取固定 `allow_private_urls=False`（`web/page_extractor.py:54`）。区别是它**经代理 CONNECT 到已校验的 IP**（`web/http_transport.py:199-236,262-275`）。因此推断：fake-ip 环境下**本机网页读取和图片/媒体 URL 下载也会以 `private_url` 被拒**【假设：未在生产复核，需用 §7 的只读日志查询确认】。issue 说“其他出站请求不受影响”只对模型、Tavily 等按域名交给代理的路径成立。是否一并处理见 Q5。

## 2. 关键事实：走代理后“谁解析域名”决定了还剩什么防护

| 路由 | 谁解析 | Host 能看到的 | 能保留的检查 |
|---|---|---|---|
| 直连（现状） | Host 本地 DNS | 真实或 fake IP | 白名单、IP 字面量、解析结果全部 global、固定 IP 防 rebinding |
| 代理 + CONNECT 域名 | 代理 | 只有域名 | 白名单、IP 字面量；**失去**“解析结果是否内网” |
| 代理 + CONNECT 已校验 IP（网页读取现状） | Host 本地 DNS | 真实或 fake IP | 同直连；fake-ip 下与直连同样被拒 |

代理 `192.168.5.2` 本身在局域网里，并且 Clash 规则里通常有 `DIRECT`（LAN、GEOIP CN 等）【issue + 一般配置常识，具体规则未核】。所以“按域名交给代理”后，一个最终解析到内网/环回的域名会被代理（或其 DIRECT 规则）连过去，连到的是代理所在主机或它的局域网。

非规范 IPv4 写法：`0177.0.0.1`、`2130706433`、`0x7f.1` 在 `ipaddress` 里都抛 `ValueError`（本机实测），即被当成**主机名**；经代理时由代理的解析器决定是否当作 IP【假设：Clash/Go 解析器的行为未测】。`::ffff:127.0.0.1` 能被识别为非 global（实测）。

## 3. 备选方案

### A. 配置了代理时，插件 HTTP 按域名经代理（CONNECT host:port / 明文 HTTP 绝对形式）【推荐用于 allowlisted】

- 代理选择复用标准变量与 `urllib.request.getproxies()/proxy_bypass()`，与网页读取一致（`web/http_transport.py:199-208`）；不新增 Yuki 专属开关，不改 F8 以外的代理语义。
- 每跳：解析 URL → 白名单（不变）→ 按原始 host:port 判 `NO_PROXY` →
  - **命中 NO_PROXY 或未配置代理**：走现有直连路径，本地解析 + 全部 global + 固定 IP，逻辑不变。`bot`、`snowluma`、私有模型主机正是 NO_PROXY 里的名字，它们继续被现有检查拒绝。
  - **走代理**：不做本地 DNS；主机是 IP 字面量时必须 `is_global`；否则以域名 CONNECT。HTTPS 照常按目标域名校验证书。
- 重定向、跨源剥离 Authorization/调用方 Header/Body、禁止调用方设置 `Proxy-Authorization`、5 跳上限、响应体积全部沿用。
- 代理不可达/协议错误如实返回 `http.request_failed`，不回退直连（与 `http-proxy.zh-CN.md` 一致）。
- 实现上可复用 `web/http_transport.py` 的代理池与 TLS 后端，或直接给 httpx 传 `proxy=`；选择在实施时定，不影响本书决定。SOCKS：httpcore 1.0.9 经 `socksio` 的 `SOCKS5CommandRequest.from_address` 发送请求地址（`httpcore/_async/socks_proxy.py:89`），域名时应为远端解析【假设：未读 socksio，E2E 只覆盖 HTTP CONNECT，SOCKS 是否纳入见 Q6】。

**残余风险（必须写进文档）**：allowlisted 域名本身的 DNS 指向内网（域名持有者变更、被接管或 rebinding）时，代理会连过去；直连模式下这会被 F2 拒绝。代价是：白名单从“域名 + 解析结果”降为“只信任域名持有者”。由 §1.3，用户无法改主机，这个风险只来自被批准域名本身，概率未知。

### B. 不走代理，把 fake-ip 段（198.18.0.0/15）视为允许【不推荐】

- 等于把判断交给本地 DNS：fake-ip 模式下 Clash 对所有不在 filter 的名字都返回 fake IP，`127.0.0.1.nip.io`、`router.lan`、任何指向内网的公网域名都会得到 `198.18.x` 并通过检查，再由 TUN 里的 Clash 映射回域名去连。地址检查在该环境下对**所有**主机名失效，比 A 更宽（A 至少对 NO_PROXY 名字和 IP 字面量仍拦截）。
- fake-ip 段是 Clash 的可配项（本部署是 `198.18.0.1/16`），写死 `/15` 是把某个代理软件的约定硬编码进 Host，违反“不写死”。
- 非 fake-ip 环境里真实出现的 `198.18.x`（基准测试网络、部分运营商/实验网）会被放行。
- `sandbox/network_guard.py:24` 把 `198.18.0.0/15` 列为 PRIVATE 丢弃，B 会让同一项目对同一网段给出相反判断。

### C. Host 自带可信解析器（DoH/指定 DNS）先查真实 IP，再交代理【不推荐】

- 新增一套 DNS 机制和配置，违背 AGENTS.md“不新增替代机制”；
- 检查与连接不是同一次解析（Host 查一次、代理再查一次），rebinding 仍然成立，安全收益不确定；
- 外部 DoH 在部分网络本身就要走代理，引入循环依赖。

### D. 只靠环境侧：每个插件域名加入 `fake-ip-filter`【现状临时处理，不作为修复】

能用，但每新增一个插件域名都要改 Clash，且对 unrestricted（主机不可预知）无效。保留为文档里的可选环境配置。

## 4. unrestricted 走代理后的内网问题

A 对 unrestricted 能保留的只有“IP 字面量必须 global”和“NO_PROXY 名字走直连检查”。剩下的缺口是**解析到内网的主机名**（`*.nip.io`/`sslip.io` 类、LAN 主机名、恶意公网域名），而这正是 §1.3 的用户输入场景。可选：

| 选项 | 内容 | 好处 | 代价 |
|---|---|---|---|
| U1【推荐】 | unrestricted 不走代理，保持现有直连检查 | 文档 F6 的承诺原样成立，不加新机制；只是不扩展到 unrestricted | fake-ip 环境下 unrestricted 插件（如 `subscription-monitor`）仍失败，需环境侧 D 或真实 DNS；**这是功能缺口，需用户接受** |
| U2 | unrestricted 也走 A，并把 F6 改写为“经代理时内网防护由代理规则负责”，在 `http-proxy.zh-CN.md` 给出 Clash 对 Bot 来源 `IP-CIDR … REJECT` 的示例 | fake-ip 下全部可用；不新增 Host 机制 | Host 不再能证明 unrestricted 不碰内网；防护是否生效取决于部署者的代理规则，代码和 E2E 都无法验证生产配置 |
| U3 | unrestricted 走 A，另加主机名规则（拒 `localhost`/`.local`/`.internal`/`.lan`/单标签名，与 Manifest `_normalize_host` 同规则，`plugin_host/manifest.py:220-240`） | 复用已有规则挡住一部分 LAN 名字 | 挡不住 `127.0.0.1.nip.io` 与任意恶意公网域名，仍须接受 U2 的残余；容易被误认为“已防住” |
| U4 | 配置了代理时拒绝 unrestricted 请求 | 最严 | 比 U1 更差：非 fake-ip 但有代理的部署也被拒，属于新增限制 |

推荐 U1 的理由：它是“不改变”而不是新增限制；把 unrestricted 的内网防护交给代理规则（U2）是一个**安全语义变更**，应由所有者显式选择，而不是随本 issue 顺带发生。若所有者更看重 fake-ip 下 subscription-monitor 能用，选 U2，并接受“Host 不再保证”的文档改写。

## 5. 待决问题

| # | 问题 | 推荐 | 理由 |
|---|---|---|---|
| Q1 | 配置代理时，allowlisted 是否改为按域名经代理（方案 A）？ | 是 | 修好 fake-ip；保留白名单、字面量检查、NO_PROXY 直连检查；残余风险仅限被批准域名的 DNS（§3A） |
| Q2 | “allowlisted 只认白名单、由代理解析”是否足够？ | 对用户输入的 SSRF 足够；对“被批准域名指向内网”不足，接受并写入文档 | §1.3：用户改不了主机；重定向每跳仍查白名单 |
| Q3 | unrestricted 怎么办？ | U1（不走代理，保持现检查） | §4；U2 是安全语义变更，需所有者明确选择 |
| Q4 | 是否允许 fake-ip 段？ | 否（方案 B 不选） | §3B 四条：检查对所有主机名失效、写死代理软件约定、误放真实 198.18、与 `network_guard` 矛盾 |
| Q5 | 网页读取/媒体下载（§1.4）是否同一 PR 处理？ | 本 PR 不改，先按 §7 只读确认是否真的被拒，再另开 issue | 同类问题但不同模块和不同承诺（网页读取 URL 全来自对话，等价于 unrestricted），应单独决定 |
| Q6 | SOCKS 代理（`socks5://`/`socks5h://`）是否纳入 E2E？ | 纳入实现，E2E 先只覆盖 HTTP CONNECT 与明文 HTTP；SOCKS 写成【假设】待补 | 生产用 HTTP 代理【issue】；控制本轮范围 |
| Q7 | 插件停在 gap 上一个月无人知道：本 PR 处理还是另开 issue？ | 另开 issue | 见下 |
| Q8 | github-monitor README 的命令写法？ | 本仓库无此插件源码，本 PR 无法修改；请告知插件源码位置 | `plugins/` 只有 `subscription-monitor`；全目录未搜到 `github_cursor_missing_from_overlap` |

Q7 依据【实证】：看门狗的 `components()` 只汇总网关、Dream、Embedding（`container.py:996-1024`），`loops()` 里只有 `plugin_background_turns` 的循环健康（`container.py:986`），没有任何插件自报的“需要人工处理”事实。gap 是插件领域内的状态，Host 无法推断。删除导向下有两条路：(a) 插件自己用已有 `notification.publish`（`yuki_plugin_sdk/permissions.py:50`）通知所有者，不改 Host；(b) SDK 新增“需关注”状态并接入看门狗 components。推荐先评估 (a)，需要 github-monitor 源码才能确认它是否已有该权限【待核查】。与本 issue 的网络修复无代码交集，合在一起会让 PR 同时改安全语义和看门狗。

## 6. 先写的失败情况与 E2E 设计（实施阶段第一步，现在未写）

环境：进程内 loopback HTTP 代理（记录收到的 CONNECT 目标与绝对形式请求）、临时 CA 签发的目标 TLS 服务、注入的本地解析器对所有名字返回 `198.18.0.x`（模拟 fake-ip）。不访问外网、不连生产。经真实 `BoundHttpFacade`（插件看到的入口）发请求。

| # | 情况 | 期望 | 基线（2aeeac84）预期 |
|---|---|---|---|
| E1 | allowlisted 域名，配置代理，本地 DNS 为 fake-ip | 成功；代理只看到 `CONNECT api.example.test:443`；本地解析器**未被调用** | 红：`PluginPermissionError` non-public |
| E2 | 明文 `http://` allowlisted 域名经代理 | 成功；代理看到绝对形式 URL、Host 头为原域名 | 红 |
| E3 | 非白名单域名 | `PluginPermissionError`；代理零记录 | 绿（现状已拒） |
| E4 | 非 global IP 字面量：`127.0.0.1`、`[::1]`、`192.168.5.2`、`198.18.0.1`、`[::ffff:127.0.0.1]`、`100.64.0.1`（unrestricted 下） | 拒绝；代理零记录 | 绿 |
| E5 | allowlisted 起点重定向到：非白名单域名 / 内网字面量 / `localhost` | 拒绝；代理只记录第一跳 | 起点就红（E1 原因），实施后应为“首跳成功、二跳拒绝” |
| E6 | 跨源重定向（两个都在白名单） | 第二跳不带 Authorization、调用方 Header 与 Body | 起点红 |
| E7 | 目标在 `NO_PROXY`（如 `bot`），解析为私网 | 走直连路径并以 non-public 拒绝；代理零记录 | 绿 |
| E8 | 未配置代理，fake-ip DNS | 仍拒绝（证明没有放宽 B） | 绿 |
| E9 | 代理不可达 | `http.request_failed`，不回退直连（直连目标零连接） | 不适用（基线不走代理） |
| E10 | 经代理但目标证书不匹配域名 | `http.request_failed` | 不适用 |
| E11 | 调用方设置 `Proxy-Authorization` | 不发给代理/目标 | 绿 |
| E12 | unrestricted + 代理 + fake-ip，主机名目标 | 按 Q3 决定：U1 → 仍拒绝；U2 → 经代理成功 | 红/绿视决定 |
| E13 | 非规范 IPv4 主机（`0177.0.0.1`、`2130706433`） | 按 Q3：U1 → 走直连检查，解析失败或非 global 被拒；U2/U3 → 记录代理收到的原文，作为文档里的残余证据 | — |

产物：`docs/architecture/evidence/issue-678-plugin-http-proxy/`，每个情况一行 JSON（情况号、结果码、代理收到的目标列表、解析器调用次数），排序后写出，不含时间戳和随机端口，重复运行字节一致；同目录保存基线红跑输出。

## 7. 生产只读复核（本阶段未执行）

- 插件拒绝计数：Bot 日志中 `github_poll_failed error_category=PluginPermissionError` 在 2026-10-22 之后的出现情况（只读 grep）。
- §1.4 假设：同期日志里网页读取/媒体下载的 `private_url` 次数。若为零，需要再看这段时间是否根本没有网页读取请求，不能直接据此判断不受影响。

## 8. 实施时需同步删改的文档

- `docs/plugin-development/networking.md`：改写“DNS 解析”一句，说明经代理时由代理解析；写明 §3A 残余风险；按 Q3 改写 unrestricted 段。
- `docs/plugin-development/security.md:31`：同上。
- `docs/operations/http-proxy.zh-CN.md` “路由与范围”：删去“插件通用 HTTP……继续沿各自现有传输设置”中关于插件的部分，补充插件 HTTP 的代理路由；可选环境配置 D 作为说明。
- `tests/` 中没有引用 `SafeHttpClient`、`BoundHttpFacade` 或其拒绝文案的测试（`git grep` 于 `2aeeac84`），因此当前没有冻结“插件 HTTP 不读代理”的测试需要删改；§6 的 E2E 将是这条路径的第一组测试。

## 9. 本阶段边界

只新增本任务书。未写测试、未改代码、未连生产、未改任何部署配置；分支只推送到同名远端分支并开 draft PR。
