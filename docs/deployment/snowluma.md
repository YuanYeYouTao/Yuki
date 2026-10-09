# SnowLuma Provider 部署

Yuki 不再支持 NapCat，当前附带的 QQ/OneBot v11 Provider 为 SnowLuma。现有 GatewayProvider、
Catalog、Registry 与 OneBot 合同继续保留，其他网关实现沿这些既有接口接入，无需新建抽象层。
Yuki 的 Presence、Conversation、Memory 和路由保存在 Bot 自己的身份与数据层，不属于任何 Provider。
更换网关实现不复制业务 owner、重置会话或迁移记忆。

## 选择与首次登录

运行 `install.sh` 或 `install.ps1`，在 QQ Gateway 页面选择 SnowLuma；也可以不选择附带网关。
未选择时不隐式启用其他 Provider。向导只负责配置，不停止、启动服务或自动切换连接。
同一个 QQ 只能有一条活动连接，Adapter 与 Registry 会拒绝重复连接，新连接不会挤掉旧连接。

首次部署确认配置后，沿实际 Compose 参数检查并启动服务，再完成登录：

```bash
docker compose config --quiet
docker compose up -d
```

已有部署先核对下一节的旧配置处理；安装器保留现有 Compose、`.env`、插件与数据。

SnowLuma 启动后：

1. 打开 `http://127.0.0.1:6081`，用 `.env` 中的 `SNOWLUMA_VNC_PASSWORD` 进入 noVNC。
2. 在 QQ 窗口扫码登录。
3. 打开 `http://127.0.0.1:5099` 检查 SnowLuma WebUI 与账号状态。
4. 按界面要求自行阅读并确认 SnowLuma、QQ 的协议和隐私事项。Yuki 安装器不会代替操作者接受。
5. 运行只读契约检查：

   ```bash
   docker compose exec bot qq-ai-bot-cli gateway doctor --provider snowluma
   ```

doctor 只报告 Yuki 依赖的 OneBot v11 核心 action、反向 WebSocket 路径和静态能力合同，不发送
消息、不调用 Provider 私有 API，也不读取 token、Cookie 或 QQ 登录数据。实际连接的 Provider、
ConnectionGeneration 和能力会由运行时 Registry 投影给控制面。

## noVNC 与 WebUI 监听地址

默认值只允许本机访问：

```dotenv
SNOWLUMA_NOVNC_BIND_ADDRESS=127.0.0.1
SNOWLUMA_WEBUI_BIND_ADDRESS=127.0.0.1
```

只有明确需要从远端访问时，才把对应变量设为 `0.0.0.0`。公网绑定前必须同时做到：

- 为 noVNC 设置不可复用的强 `SNOWLUMA_VNC_PASSWORD`，并确认 SnowLuma WebUI 自身的访问认证。
- 用主机防火墙或云安全组只允许可信源地址；不要对全网开放。
- 优先通过 VPN 或带 TLS 与额外认证的反向代理访问，不通过明文公网传输登录操作。
- 绝不暴露 VNC 原始端口、OneBot HTTP/WS、access token、Cookie 或 QQ 登录目录。

修改 bind address 后重新创建 SnowLuma 容器，并从非可信网络验证端口确实不可达。Yuki 安装器
不会替你配置云防火墙、TLS 或 SnowLuma 的账户认证。

## 旧部署配置处理

安装器不会替换已有的 managed Compose 文件，环境合并也会保留未知字段。仅删除源码模板、
重新运行安装器，不会从旧部署的 Compose 或 `.env` 中清除 NapCat 支持。

按[3.9.0 升级草案](../upgrade-3.9.0.md#旧-napcat-部署配置)，先确认实际项目与全部 Compose
覆盖文件，再用既有配置编辑方式去掉旧 `napcat` service、专属挂载、`NAPCAT_*` 环境项及
`COMPOSE_PROFILES` 中的 `napcat`。其他扩展 profile、共享 social transfer 和 SnowLuma 配置须保留。
旧 `data/setup/gateway-action.json` 没有执行消费者，核对目录后可以清理该遗留标记；它不是待执行
的切换或恢复任务。

若实际部署仍运行旧网关，停用前单独核对容器、QQ 账号和授权范围，确认旧连接已从 Registry 注销
后再连接同一 QQ。配置编辑不等于容器已停止，也不自动取得删除 QQ HOME、备份或插件的授权。
保留历史 `ingress_provider` 和内部身份；重新连接只改变连接事实，不复制记忆或重建业务 owner。

## SnowLuma 多账号

一个 SnowLuma 容器可以运行多个不同 QQ；为每个额外账号配置独立 HOME，例如：

```dotenv
SNOWLUMA_EXTRA_QQ_HOMES=/app/qq-accounts/qq-b,/app/qq-accounts/qq-c
SNOWLUMA_SHM_SIZE=2gb
```

这些路径必须位于 `/app/qq-accounts/` 下，且不能重复。每个 HOME 对应一个 QQ，不能让多个 QQ
共享 HOME。多账号会显著增加内存占用，至少把默认 1 GB 共享内存提高到 2 GB，并按实际账号数
继续评估。

## 故障恢复

- 不同 QQ 往返切换后，如果旧群没有响应，先区分群 `enabled` 设置与路由 `paused` 状态。无需按
  两账号共同群交集删除或关闭旧群；账号不在该群时保持不可达即可。历史暂停可由超管在目标群
  单独发送 `/ai on` 恢复，不能用普通聊天自动解除管理员暂停。恢复会检查当前账号确实在群内，
  有多个候选且无法确定接入账号时拒绝抢占。不会清空 Conversation、Rollup 或 Memory。
- 查看状态：`docker compose ps --all bot snowluma`，沿用部署时的全部 Compose 参数。
- 查看 SnowLuma 日志：`docker compose logs --tail 200 snowluma`
- 查看 Bot 日志：`docker compose logs --tail 200 bot`
- noVNC 与 WebUI 默认只绑定 `127.0.0.1`。若使用可配置 bind address 暴露远程访问，必须遵守
  上述强密码、防火墙和可信来源边界；VNC 与 OneBot HTTP/WS 始终不得暴露。
- `/app/data`、`/app/.config`、`/app/.local/share` 与额外账号 HOME 都是持久登录数据，不要提交
  Git，也不要在升级时删除。

SnowLuma 的 Provider 私有 action 不属于 Yuki 跨 Provider 合同。不支持的 action 应由底层
OneBot 调用明确失败，调用方不能把失败当成成功。

实现约束以 SnowLuma 官方文档为依据：

- [Docker 部署要求](https://snowluma.github.io/en/guide/deploy/docker.html)
- [OneBot 配置结构](https://snowluma.github.io/guide/configuration.html)
- [API 兼容目录](https://snowluma.github.io/api/index.html)
