# Telegram 接入

Yuki 以 Telegram Bot 身份接入，用 Bot API 的 long polling（`getUpdates`）收消息：Bot 主动连接
`api.telegram.org`，不需要公网回调地址，也不新开监听端口。网络沿标准代理配置，见
[HTTP 代理说明](http-proxy.zh-CN.md)。

## 1. 用 BotFather 创建 Bot

1. 在 Telegram 里打开 [@BotFather](https://t.me/BotFather)，发送 `/newbot`。
2. 按提示填写显示名称和用户名（用户名须以 `bot` 结尾）。
3. BotFather 回复的 token（形如 `123456789:AA…`）就是这个 Bot 的全部控制权。不要贴进聊天、
   截图或提交到仓库；泄露后在 BotFather 用 `/revoke` 换一个新的。

## 2. 让 Bot 在群里看到普通消息

新 Bot 默认开启 Privacy Mode：在群里只收到 @ 它的消息、回复它的消息和 `/` 命令，看不到普通聊天，
也就无法理解群聊、自然参与。需要时：

1. 向 BotFather 发送 `/setprivacy`，选择这个 Bot，再选 **Disable**。
2. 把 Bot 移出已加入的群，再重新拉进来；Telegram 只在 Bot 入群时应用新的隐私设置。

把 Bot 设为群管理员也能收到全部消息。只在私聊使用时不需要改。

## 3. 已设置过 webhook 时先删除

Telegram 不会同时给 webhook 和 `getUpdates` 推送消息：Bot 设置过 webhook（例如以前接过别的服务）
时，Yuki 的 long polling 会一直失败。Yuki 不会替你删除 webhook，先自行检查并删除：

```sh
read -rs TOKEN    # 粘贴 token 后回车，不会显示，也不进 shell 历史
curl -s "https://api.telegram.org/bot$TOKEN/getWebhookInfo"   # "url":"" 表示没有 webhook
curl -s "https://api.telegram.org/bot$TOKEN/deleteWebhook"
unset TOKEN
```

同一个 token 同一时间只能有一个程序在 long polling；另一个程序也在用它收消息时，两边会互相挤掉。

## 4. 在 Yuki 中填写

在 Yuki Desktop「设置 → 平台」中填写 Bot token。连接状态与最近的错误原因也在这里查看。

之后在 Telegram 私聊 Bot 即可对话。要让 Yuki 认出这个 Telegram 账号就是你，在私聊发送
`/ai link <绑定码>`；绑定码由你已配对的设备签发。账号归属与合并规则见
[Client API](../architecture/client-api.md#配对与账号绑定)。
