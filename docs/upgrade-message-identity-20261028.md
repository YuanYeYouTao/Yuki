# 消息身份与本地投递升级

本次开发 schema 为0155，Plugin API为9.0，个人Client HTTP API仍为7。主Agent固定工具声明没有因本次修改增加工具或参数。

升级前停止旧Bot及其他业务writer，备份主库、配置、实际插件和工作文件；使用同一数据库执行 `yuki init-db`，完成后启动新代码。0155在原库中调整两张表，不重编业务ID，也不清历史、Compact、Work或模型请求。原生扩展仍使用0153引入的固定Vec1构建。

`chat_events.platform_message_id`原样改名为可空 `origin_key`；真实可引用的平台消息号存到新增可空 `native_message_id`。原接入receipt列改名 `ingress_key`，保存完整凭据。旧数据缺少真实引用证明时native留空，原信封继续保留。Memory原来源hash编码不变，原wire键 `platform_message_id`仍取原信封值。

本地发送和通知直接发表到原账本，与所属operation/outbox成功回执一起提交。App不再调用成功noop，不再生成伪平台号。远端文件可能真实成功而没有聊天消息号；内部回复关系保留，协议不编码虚构引用。恢复查询原业务ID，不根据平台列猜原执行。

0155仅在原canonical关联、唯一keeper、接收Account和协议namespace都吻合时补回被截断的接入键。若完整唯一键确有冲突，迁移报告具体receipt并回滚本次事务。应检查原关联资料后处理实际冲突，不能删历史receipt、stamp版本或重放消息绕过去。

SDK9删除 `CurrentMessage.message_id`，使用可空且不截断的 `native_message_id`；业务引用继续使用内部event ID。插件需适配调用、声明 `plugin_api = "9.0"`，再按实际manifest重新发现和批准。8.x及更早插件需要迁移，Host不替插件伪造批准、hash或权限。自动化旧authority JSON、版本及冷来源列仍保留，新运行时不再携带无用平台来源字段。详见[SDK9迁移](plugin-development/api-9.0-migration.md)。

桌面端配套改动保持Client API7：事件/附件索引与精确artifact/version仍是原下载来源。图片、音视频和PDF使用桌面已有app协议中的受控资源流，Range读取已验证缓存；临时附件的回收时间不是新的权限截止线。实际后端PR、桌面PR和插件PR分别交付，部署后端不会自动安装桌面新版本。

执行与验证记录见[实施报告](operations/message-identity-delivery-implementation-20261028.md)。本轮范围是源码和PR，没有生产部署。
