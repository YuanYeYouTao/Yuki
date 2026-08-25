# SnowLuma Provider 部署与切换

> 本文对应 Yuki 3.8.0 待发布代码。`v3.8.0` Release 与镜像出现前，只用于源码构建或
> 预发布环境验证，不代表 3.7.1 正式安装包已经包含 SnowLuma。

SnowLuma 与 NapCat 都是 Yuki 的正式 QQ/OneBot v11 Provider。它们只负责连接 QQ；Yuki 的
Presence、Conversation、Memory 和路由保存在 Bot 自己的身份与数据层，不属于任何 Provider。
因此，同一个 QQ 按正确流程切换 Provider 时会继承原 Presence，也不会重置会话或记忆。

## 选择与首次登录

重新运行 `install.sh` 或 `install.ps1`，在 QQ Gateway 页面选择 NapCat、SnowLuma 或二者。
旧部署没有 `COMPOSE_PROFILES` 时按 NapCat 处理。选择二者只适用于不同 QQ：同一个 QQ 只能有
一条活动连接。

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

## 安全切换同一个 QQ

正式切换必须重新运行安装器并修改 Gateway 选择。安装器生成
`data/setup/gateway-action.json`，严格按以下顺序执行：

1. 停止并移除被取消选择的旧 Provider。
2. 确认旧容器和旧连接已经消失。
3. 启动目标 Provider 与 Bot。
4. 验证容器和 Bot 健康后删除 action 文件。

停止或验证失败时，安装器不会启动新 Provider，action 文件会保留供下次重试。不要在旧
Provider 仍在线时手工启动同 QQ 的新 Provider；Adapter 和 Registry 都会以
`provider_conflict` 拒绝新连接，旧连接不受影响。

Provider 切换只产生新的 GatewayConnection 和 ConnectionGeneration，不应改变
ConversationGeneration、RouteGeneration、Memory 或 Presence。Yuki 不承诺切换 Provider 能降低
腾讯账号风控风险。

## 不同 QQ 与 SnowLuma 多账号

NapCat 的 QQ A 与 SnowLuma 的 QQ B 可以同时在线。一个 SnowLuma 容器也可以运行多个 QQ；为
每个额外账号配置独立 HOME，例如：

```dotenv
SNOWLUMA_EXTRA_QQ_HOMES=/app/qq-accounts/qq-b,/app/qq-accounts/qq-c
SNOWLUMA_SHM_SIZE=2gb
```

这些路径必须位于 `/app/qq-accounts/` 下，且不能重复。每个 HOME 对应一个 QQ，不能让多个 QQ
共享 HOME。多账号会显著增加内存占用，至少把默认 1 GB 共享内存提高到 2 GB，并按实际账号数
继续评估。

## 故障恢复

- 查看状态：`docker compose ps --all bot napcat snowluma`
- 查看 SnowLuma 日志：`docker compose logs --tail 200 snowluma`
- 查看 Bot 日志：`docker compose logs --tail 200 bot`
- 若 `data/setup/gateway-action.json` 仍存在，修复停止失败、端口或登录问题后重新运行安装器；
  不要手工删除文件并强行同时启动两个 Provider。
- noVNC 与 WebUI 默认只绑定 `127.0.0.1`。不要把 VNC、OneBot HTTP/WS 或 WebUI 直接暴露到公网。
- `/app/data`、`/app/.config`、`/app/.local/share` 与额外账号 HOME 都是持久登录数据，不要提交
  Git，也不要在升级时删除。

SnowLuma 的 Provider 私有 action 不属于 Yuki 跨 Provider 合同。不支持的 action 应由底层
OneBot 调用明确失败，调用方不能把失败当成成功。

实现约束以 SnowLuma 官方文档为依据：

- [Docker 部署要求](https://snowluma.github.io/en/guide/deploy/docker.html)
- [OneBot 配置结构](https://snowluma.github.io/guide/configuration.html)
- [API 兼容目录](https://snowluma.github.io/api/index.html)
