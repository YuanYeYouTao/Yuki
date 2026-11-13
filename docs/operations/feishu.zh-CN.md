# 飞书与 Lark 接入

Yuki 通过企业自建应用机器人接入飞书或 Lark。入站使用长连接，发送和媒体使用对应平台的 OpenAPI；Bot 只需能主动连接平台，不需要公网回调地址或新增监听端口。

此接入随当前开发源码提供，3.9.0 镜像不包含。先完成正常的模型、数据库和持久环境配置，再增加下面的应用配置。

## 创建应用与开通权限

1. 在[飞书开放平台](https://open.feishu.cn/app)或 [Lark 开放平台](https://open.larksuite.com/app)创建企业自建应用，启用机器人能力。
2. 设置应用可用范围，让需要使用 Yuki 的账号可以打开机器人，并将机器人加入目标群。
3. 在事件订阅中选择使用长连接接收事件，订阅 `im.message.receive_v1`。本轮接收消息事件，不订阅卡片交互等额外事件。
4. 申请消息权限，发布应用，并完成所在组织要求的管理员批准。权限变更也需要按平台流程生效。

| 使用场景 | 权限 |
| --- | --- |
| 接收单聊 | `im:message.p2p_msg:readonly` |
| 接收群内 @ | `im:message.group_at_msg:readonly` |
| 理解群内普通聊天并自主参与 | `im:message.group_msg` |
| 以机器人身份发消息 | `im:message:send_as_bot` |
| 读取获准消息和消息中的资源 | `im:message:readonly` |
| 撤回消息 | `im:message:recall` |
| 上传图片和文件资源 | `im:resource` |
| 真人成员探针和群路由消歧 | `im:chat.members:read` |

平台接口允许部分权限互相覆盖，表中按用途列出，具体以控制台对应接口要求为准。权限名称可对照[官方 SDK 权限表](https://github.com/larksuite/cli/blob/main/skills/lark-im/SKILL.md#权限表)。机器人是否在群通过 `members/is_in_chat` 查询，真人成员探针才按原生成员列表核对；已有确定入口的正常发送不先拉完整名册。普通回复不需要整个企业通讯录权限。

只有群 @ 权限时，Yuki 可以回答 @、处理任务和交付结果；她看不到未推送的普通群聊天。自然参与需要全量群消息权限。Yuki 不在接入层另加只响应 @ 的筛选。

群命令可以先通过客户端真实 @ Yuki，再输入 `/ai admin ...`。命令读取会识别开头的 Bot 提及，历史仍保存原消息。直接打字写 `@Yuki` 不会产生原生提及，也不能让平台向 Bot 推送消息。

## 配置与启动

应用凭据只保存在部署目录的 `webui-config/platforms.toml`（App ID）与同目录私有的
`platforms.secrets.json`（App Secret，0600），不写入 `.env`。三种填写方式任选其一：

- Yuki Desktop：**设置 → 平台**，在飞书或 Lark 卡片填写 App ID 与 App Secret 后保存。
- 安装向导：`yuki setup` 的「聊天平台」页选择飞书或 Lark，再填写两项。
- 管理 API：`POST /admin/v1/platforms/apply`，平台为 `feishu` 或 `lark`，见[管理 API](../architecture/admin-api.md#聊天平台凭据)。

平台键决定域名：`feishu` 使用 `https://open.feishu.cn`，`lark` 使用 `https://open.larksuite.com`；
应用 ID 与 Secret 必须属于所选平台。两项须成对保存，只填一项会在保存时被拒。保存时先用新凭据
取 tenant token 并读取机器人身份，通过后才替换正在运行的连接，无需重启 Bot；被拒时原连接不变，
返回的 `problem.details.error_category` 说明原因。长连接被平台拒绝（如未开长连接权限）后连接停止，
在平台侧修正后再保存一次即可重新连接。

旧版写在 `.env` 的 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_DOMAIN` 已不再读取。升级时
`install.sh` 会运行 `yuki setup platforms-migrate`，把 `.env` 或容器环境中的旧值一次性写入平台文档；
已有平台文档时不覆盖。之后可从 `.env` 删除这些旧行。

仅接入飞书/Lark、TG 或个人客户端时，将 `COMPOSE_PROFILES` 留空，不需要 QQ 账号或 SnowLuma。模型、数据库和工作环境仍按正常部署说明配置。

源码环境启动：

```bash
uv sync --all-extras
uv run yuki-bot
```

网络沿标准 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` 与 CA 配置，参见[HTTP 代理说明](http-proxy.zh-CN.md)。配置与日志不要公开应用 Secret 或平台访问 token。

## 已有工具怎样工作

飞书、Lark、QQ 和 Telegram 使用同一份平台 Main 工具合同。记忆、历史、终端、任务和自动化共用已有服务，不为飞书复制 Agent。通过人物关联可以共享同一个人的资料和记忆，各个私聊入口、群及话题保留自己的对话历史。

| 功能 | 当前路径 |
| --- | --- |
| 文字、图片和文件 | `send`，由适配器编码为原生消息 |
| 提及与引用 | 保留真实账号提及与原消息关系；业务工具使用内部事件 ID |
| 话题 | 原平台 root/thread 关系映射到独立 Conversation |
| 撤回 | `recall_own_message`，使用原发送 Presence 和原回执 |
| 历史与成员 | 授权内部账本和已观察成员，`coverage=observed` |
| 附件 | 由原接收账号获取字节，进入共同缓存和工作区 |

发送失败保留平台失败；确认丢失保留未知结果，不自动换账号或重复发送。文件、附言和拆分消息各自记录真实结果。

普通图片与已有图库中的图片可以继续发送。飞书原生贴纸不通过普通图片/文件资源接口下载，本轮不自动收藏；仅有原生贴纸标识不等于已经取得图片原件。卡片交互、飞书文档、日历、表格和群管理可以另行扩展。

## 检查运行状态

启动时先验证机器人身份，再注册 Presence 并建立长连接。用 operator 的管理健康查询查看 `feishu` 或 `lark` 平台连接状态，结合 Bot 日志检查长连接重连和错误类别。`GET /admin/v1/platforms` 另给出运行状态、最近错误类别与当前账号。HTTP 服务可用或模型正常，不单独证明机器人具备群可见性和发送权限。

实际接入后，依次检查单聊、群 @、普通群消息、话题、提及与引用、图片文件和自身消息撤回。群普通消息没有进入时，先核对应用发布、可用范围、机器人是否在群和全量消息权限是否已经批准。不要用尚未收到的消息判断历史检索或 Agent 参与行为。

同一消息重投读取原入账记录，不再次触发 Main。当前未接入官方消息编辑推送，因此平台上编辑旧消息不会自动修改 Yuki 已观察的历史。

协议回归使用离线数据；真实应用收发、组织权限和网络连接另行核对。

官方参考：[长连接](https://github.com/larksuite/node-sdk#subscribing-to-events-using-long-connection-mode)、[消息与权限](https://github.com/larksuite/node-sdk/blob/main/docs/channel.md)、[身份范围](https://open.larksuite.com/document/home/user-identity-introduction)。
