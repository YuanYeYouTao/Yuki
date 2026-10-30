# 通过 Tailscale 连接 Yuki

服务器上的 Yuki、电脑上的 Desktop 和 Agent Connector 沿原服务运行。Tailscale 提供一条可选私有网络，服务器用 Serve 将原 Bot 发布为 HTTPS 地址，电脑不必一直开着 SSH 转发终端。原 HTTPS、SSH loopback 和本机部署仍可使用。

本文说明安装与配置方法，不代表已经替使用者安装、登录或完成实际网络验收。Connector 改址命令需要 `0.4.1` 或更新的配套实现；源码、构建产物、正式 Release 和本机已装版本分别核对。

## 1. 两端安装并登录

在**运行 Yuki 的服务器宿主机**和**使用 Desktop 的电脑**分别安装官方 Tailscale，加入获准互通的同一 tailnet。本人完成登录及系统授权；组织 tailnet 的设备接纳和访问规则由有权限的管理员处理。Tailscale 不装进 Bot 容器，也不由 Desktop 或 Connector 安装包捆绑。

Linux 服务器按 [Tailscale 官方 Linux 安装说明](https://tailscale.com/docs/install/linux)选择适合发行版的方法。官方脚本示例为：

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

已安装并登录的服务器先查看现状，不需要重装或重新执行登录。新安装的 `tailscale up` 会给出登录链接，由本人打开并完成认证；也可按官方页面使用发行版软件源安装。

Windows 电脑从 [Tailscale 官方下载页](https://tailscale.com/download/windows)安装并登录；其他系统使用[官方下载入口](https://tailscale.com/download)。确认服务器和电脑出现在预期的 tailnet 中。登录凭据、配对 token 和原生 Agent 登录文件不发给 AI 或聊天窗口。

## 2. 在服务器确认 Bot 端口和现有映射

以下检查及下一节 Serve 命令均在**运行 Yuki 的服务器**执行。不要在使用者电脑执行后，把电脑自己的 loopback 当成远程 Bot。

先从当前部署的 Compose 文件、实际容器端口发布或原进程监听，确认 Bot 的宿主地址。容器内的 `8080`、宿主发布端口和电脑旧 SSH 转发端口是三个不同位置；本文出现的 `18767` 只是示例，必须替换为现场实际端口。Compose 部署可沿原 project 和原 `-f` 文件组合只读查询 `port bot 8080`，不为查端口另起一份部署。

在服务器确认实际 `http://127.0.0.1:<Bot宿主端口>/livez` 和 `/client/v1/meta` 可达，记录当前 Tailscale 与 Serve 状态：

```sh
tailscale status
tailscale serve status
tailscale serve status --json
```

保留当前映射、HTTPS 端口、路径和目标。已有映射正好指向原 Bot 时直接复用；已有其他服务占用所需端口或根路径时，选择另一可用 HTTPS 端口。不要覆盖原映射，也不要运行全局 `tailscale serve reset`。原 Caddy、SSH 和其他服务继续按其原配置运行。

## 3. 在服务器以根路径发布整个 Bot

确认实际目标和空闲 Serve 端口后，在服务器执行下面的模板，替换所有占位符：

```sh
sudo tailscale serve --bg --https=<可用HTTPS端口> http://127.0.0.1:<Bot宿主端口>
```

例如，**仅当核实 Bot 的宿主端口确为 `18767`，且 Serve 的 `443` 未被其他映射使用时**：

```sh
sudo tailscale serve --bg --https=443 http://127.0.0.1:18767
```

使用根路径代理整个 Bot，让 `/client/v1/` 的 HTTP/SSE、`/agents/v1/` 的 HTTP/WSS，以及文件、电脑等当前产品使用的路径走同一入口。不只代理 `/client/v1/`，也不靠重定向补 API 路径；已有前缀部署继续沿原代理去前缀合同，不另造一套路径。

命令输出会显示真实地址，例如 `https://<服务器DNS名>.ts.net`；非 `443` 端口要保留 `:端口`。以输出为准，不能把短主机名或 `100.x` 地址直接拼成证书不匹配的 HTTPS URL。此地址是整个 Yuki 的 base URL，填写时不附加 `/client/v1` 或 `/agents/v1/connect`。

Serve 仅供 tailnet 内获准设备访问；本指南不使用向公网开放的 Funnel。`--bg` 配置由 Tailscale 保存，重启后恢复，不需要 Yuki 再建后台恢复循环。具体语法和关闭方式见 [Serve CLI 官方说明](https://tailscale.com/docs/reference/tailscale-cli/serve)。

首次启用时，Serve 可能给出官方网页同意链接。本人查看并同意所需 HTTPS 配置；管理控制台的 DNS 页面管理 [MagicDNS](https://tailscale.com/docs/features/magicdns) 和 [HTTPS 证书](https://tailscale.com/docs/how-to/set-up-https-certificates)。申请公开 TLS 证书时，机器名和 tailnet DNS 名会出现在证书透明度日志，按官方页面说明选择合适名称。Serve 自动处理其 HTTPS 证书，无需额外导出私钥。

同一 tailnet 不表示所有访问都已允许；检查管理控制台中的[访问规则](https://tailscale.com/docs/features/access-control)，让实际使用者能够访问服务器选定的 HTTPS 端口。保留原策略，不为接入清空其他规则。Serve 的流量与权限说明见 [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)。Tailscale 身份头不替代 Yuki 自己的配对、Authorization 或设备权限。

## 4. Desktop 配对与原连接改址

新客户端在 Desktop 选择「連接另一個 Yuki」，使用上一步真实 HTTPS base URL 和本人正常取得的配对码／链接，沿原流程配对。不需要先连接电脑 Connector，也不需要 Desktop 检查本机 Tailscale CLI。

已有客户端在原 Yuki 的「設定 → 連線」修改地址，保留原登录、Person、Binding、Conversation 和请求回执。同一服务换线不通过删除连接或重新配对实现。地址不变只是网络恢复时，沿原「重新連接」入口接回。

`CLIENT_PUBLIC_URL` 可设为该实例真实可达的 base URL，帮助新配对链接填写地址。它只影响生成的配对链接，不建立 DNS、TLS 或代理，也不自动改写已安装客户端和 Connector 的地址。需要使服务器配置生效时沿原部署方式只处理相应服务，不为接入全量重建 Compose 或回退数据库；不设置时仍可用原码加明确地址。

## 5. 原 Connector 独立改址

Desktop 客户端和电脑 Connector 各自保存地址。一边使用新 Tailscale 地址、另一边继续使用原 SSH loopback 或普通 HTTPS 地址，仍可连接同一 Yuki；任一边保存都不自动改另一边，也不要求两处 URL 相等。

配套 Desktop 在「設定 → 這台電腦」修改原 Connector 地址。独立安装使用 `0.4.1` 的命令；自定义安装先带上原 `--data-dir`／`--config`，不要新建资料目录：

```sh
yuki-agent-connector server --url https://<服务器DNS名>.ts.net
```

这里同样填 HTTP(S) base URL，实际端口和路径前缀保留。CLI 保存为相应 WS(S) endpoint；远端仍使用 HTTPS/WSS，Tailscale IP 不成为放宽 TLS 的例外。

保存只更新原配置 URL，保留原 token、`device_id`、instance、args、env、原生 owner、`full_access`、journal 和服务来源，供下次 Connector 启动使用，不启动、停止或重连服务。CLI 不连接目标或验证远端身份，需填写同一原 Yuki 的实际新地址。若原服务停止，保存后继续停止。保存成功不等于新 WSS 已接通，旧连接仍在线也不能证明新线路成功。

需要现在用新地址启动时，明确关闭再开启原「背景連接」，或操作同一安装的用户服务：

```sh
yuki-agent-connector service stop
yuki-agent-connector service start
```

关闭背景连接会真正停止 Connector，可能取消它持有的本机 Shell；这不是无影响的热迁移。原生 Codex daemon、Claude／ACP 进程及原生 owner 的生命周期分别处理。改址不要求先完成所有任务；原已提交操作和未知结果继续按原 ID 与回执查证，不重派任务。

`device_id` 是首次配对时由 Yuki 服务端 `uuid4()` 生成的随机设备身份，与 token 一同保存在原配置中，不是 MAC、网卡、IP、主机名或 Tailscale 节点 ID。保留原配对配置换线仍是原设备；删除配置后重新配对会生成新 ID。新 Connector 沿现有 journal 初始化升级同一设备的旧 `[url, device_id]` 归属，保留原操作和回执，不清 journal，也不把另一设备或另一台 Yuki 的凭据搬过来。完整语义见[本机 Agent 连接器](local-agent-connector.zh-CN.md)。

## 6. 分别验证实际结果

从使用者电脑通过新的真实地址核对以下结果，各自记录证据；安装成功、地址保存和原连接在线不能代替它们。

| 范围 | 实际验证 | 能说明什么 |
| --- | --- | --- |
| HTTP 与 TLS | 新 base URL 的 `/livez`、`/client/v1/meta`，证书和角色资料符合预期 | 新线路能到达原 Bot 的公开 HTTP 入口 |
| 原客户端身份 | 原登录下的 `/client/v1/me` 及已有聊天入口，核原 Person、Binding、Conversation | 客户端沿原身份访问同一 Yuki |
| SSE | 用原登录打开 `/client/v1/stream`，确认 `text/event-stream`、原初始 sync 和后续实际事件 | HTTP 长连接没有被代理缓冲或截断；没有后续事件时只记录已收到的事实 |
| Connector WSS | 明确用新地址启动原 Connector，确认 `/agents/v1/connect` 的 Upgrade、hello/catalog 与原设备/实例目录 | 新线路的 WebSocket 与原电脑身份均有效；HTTP 成功不代表此项通过 |
| 文件与电脑 | 只读打开本人已有且获准的文件／电脑入口，核其实际 API 和 WebSocket 路径 | 完整产品使用的路径也通过原代理；未配置的能力单独记录未验 |
| 原回执 | 查询已有原 request／operation／execution ID 及原状态 | 地址变化没有另造设备或重派执行；unknown 仍保留 unknown |

原登录凭据继续通过既有授权流程传递，不放入 URL、截图或报告。验证可以读取原事件、目录和回执，不需要发 QQ 测试消息、发起新 Agent 任务或付费模型请求。失败按 DNS、TLS、访问规则、HTTP、SSE、WebSocket 和 Yuki 授权分别定位，不靠清配置或重新配对掩盖。

## 7. 调整或退出本次发布

先重新查看 `tailscale serve status`，确认目标仍是本次添加的端口和根路径。需要停用时，只对该映射使用与原命令对应的 `off`；例如本次确实使用 `443` 时：

```sh
sudo tailscale serve --https=443 off
```

其他端口使用其实际原参数；若原命令含路径等选项，关闭时保留对应选项。不要用全局 `reset` 清掉其他服务。关闭前核仍由本次使用的映射，恢复客户端／Connector 地址时各自保存；不删除原配置、journal、数据库、文件或配对。不再使用的 SSH 转发也先核其他调用者，不能因新入口可用就顺手停止。

## 请 AI 协助配置

可将下面内容复制给能够访问两端环境的 AI，补充自己的服务器连接方式及软件位置；不要在提示中粘贴 token 或登录文件。

```text
请帮我通过官方 Tailscale 将我的电脑连接到服务器上的原 Yuki，并保留现有服务、资料和设备身份。

先阅读仓库现行合同和 docs/operations/tailscale-connection.zh-CN.md，核对 Tailscale 官方 Linux 安装、Windows 下载、Serve、MagicDNS、HTTPS 与访问规则文档。先只读确认两端系统、已装版本/登录、所属 tailnet、原 Bot 部署/实际宿主 loopback 端口、当前 Serve 映射、原 Desktop 与 Connector 的安装和数据目录。18767 只是示例，不直接套用。

我授权在上述范围完成必要安装与配置；已有且适用的安装、服务和映射直接复用。登录、账号选择、HTTPS 网页同意和系统授权交给我本人处理，告诉我实际入口及原因；除此之外，在已授权范围继续推进，不为可逆常规步骤反复确认。不要代登录或索取/输出凭据。

只在运行 Yuki 的服务器宿主配置 Serve --bg，以实际可用的 HTTPS 端口从根路径代理整个原 Bot；保留其他 Serve/Caddy/SSH 和 Compose 服务，不使用 Funnel、全局 reset、容器内安装或全量重建。若目标端口已有其他服务，选择可用端口，不覆盖原映射。不要清数据库、配置、journal、回执或原生登录资料。

Desktop 按原配对/原连接改址流程操作；原 Connector 使用兼容的 0.4.1 server --url 或桌面对应入口，只改原安装 URL，保 device/instance/token/原生 owner/完整访问/服务来源。两处地址独立，不强制相同，也不自动同步，不重新配对。保存不启停服务；如需现在应用，先说明原背景连接开关停启对本机 Shell 的真实影响，再按本次明确授权处理，不承诺热迁移。

分别验证新地址 HTTP/TLS、原身份、SSE、Connector WSS hello/catalog、已配置的文件/电脑路径，以及已有原 ID 的回执。不要发 QQ 测试消息、付费模型请求或新原生执行。不要通过重派任务、换 ID、清数据或盲目重试制造通过。

最后报告实际修改、原配置/映射保留情况、每项验证证据与未验项；源码、Release、安装、地址保存、真实连通和上线分开说明。保留调整/关闭本次单独映射的方法，不清其他服务。
```
