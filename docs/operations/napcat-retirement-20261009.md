# NapCat 退役实施与上线回执（2026-10-09）

Yuki 的 NapCat 可执行支持已退出；原 GatewayProvider / Profile / Catalog / Registry / OneBot 合同保留，当前附带实现为 SnowLuma。本次没有新增网关抽象、数据库迁移或历史来源改写。

## 代码与验证

- [PR #276](https://github.com/YuanYeYouTao/Yuki/pull/276) 已合并至 main，源码 `528e2adc73a42cf25af04771ab864718a840886d`；合并时间 2026-10-09 21:35:19（Asia/Taipei）。PR 树与合并树一致。
- 实现及部署配置 23 个文件：删除 346 行、增加 40 行，净减少 306 行。文档、任务书和必要行为回归另计。
- 本地完整测试 1088 passed / 50 skipped；Linux CI 1089 passed / 49 skipped，Ruff、mypy、前端测试与构建均通过。Code Mode/Monty 可选用例按实际未构建跳过，未为退役修改跳过条件。
- direct 镜像与源码分离部署包完成启动、迁移和重建持久化检查。实际隔离握手验证原 token、两个 SnowLuma WS 路径、断连注销和旧 HTTP/WS 入口退出；没有连接 QQ 或发送验收消息。
- 最终镜像复核 NapCat 模块不存在、现存 Catalog 可用，direct 不含 Monty binding、worker 或 launcher。
- 当前文档 15 份同步，168 个本地链接/锚点通过。旧 Release、日期验收、真实历史 provenance 和私密资料排除保留。

## 生产部署

生产镜像为 `ghcr.io/yuanyeyoutao/yuki-qqbot:ops-528e2adc`，revision label 对应上述完整源码 SHA。本地构建后直接上传服务器，压缩包 SHA-256 两端一致：`0726c0a8d003f02e94fd5beef7fd5bc3488930caa25d1d2f0082d892cf4ed2cf`；没有发布 `v3.9.0` tag、Release 或正式版本镜像。

部署前用 SQLite backup API 取得应用库和 participation 库的一致性备份，quick_check 与外键检查通过，应用 schema 为 `0102`。配置原件与实际 Compose 列表同批保存，应用数据库部署备份仅保留最新一份。

从原 Bot labels 取实际 65 个 Compose 文件，清退原 base 和一个 frozen bot override 内的 NapCat 专属环境、服务与挂载；frozen override 的其他环境和挂载保留，没有整行删除配置。`.env` 的专属键退出，其他 profile 与配置保留。追加 Bot 专用镜像 override 后，只重建 Bot，未对 SnowLuma 执行重建、拉取或重启。

2026-10-09 21:43:52（Asia/Taipei）验收：

| 项目 | 结果 |
| --- | --- |
| Bot | healthy，restart 0 |
| `/healthz` 核心状态 | status=ok，database=ok，OneBot connected |
| 应用 schema | `0102`，未新增或重跑品牌迁移 |
| NapCat | 运行环境和挂载无专属项，包内模块不存在 |
| SnowLuma | 原容器 ID、镜像和 StartedAt 不变，原登录资料保留 |
| Code Mode | disabled，direct 包验证通过 |

线上验收确认服务与连接恢复，不冒充真实 QQ 自然回复验收。本轮未发送 QQ 测试消息。

后续 21:48 起自然请求出现模型服务 503，回执为账号池全部受限，上游明确返回个人额度耗尽。本轮连接健康不能代表 AI 回复可用；该 Provider 额度问题另行核查，没有在网关退役中修改模型路由。

## 服务器清理

- Yuki 仅保留 `ops-528e2adc` 与 `ops-31d12022`；删除较旧的 4f582c06、2ce7a167、f4f483d7 三个镜像及其旧 alias。
- 清除旧备份、已加载的上传包、旧临时构建目录、空闲且空的 speech-runtime volume、APT 缓存；构建缓存实际为 0。
- 在用服务镜像、SnowLuma 的最近两份镜像、Manager 当前引用的 checkpoint、Miniflux 数据卷、数据库、工作区及 QQ HOME 保留。未按 Docker 的 reclaimable 数值直接全删。
- 清理阶段实际释放 `3,044,302,848` 字节（约 3.0 GB / 2.84 GiB）；磁盘 40G 中使用约 20G，可用约 18G，使用率 54%。

私密原件和机器回执位于服务器 `/opt/yuki-qqbot/ops/rollout-napcat-276/`，包括 before/deployment/verified/cleanup；不将其中的原配置或凭据提交仓库。

## 独立未决项

Registry 的公开 connect 重绑定可产生同 Presence 多个 live handle，原 pin 仍可择原连接；正常 Adapter/main 装配不走该分支。本次按任务书保留，未声明修复，也未新增协调器。

生产记忆一致性标记为 false：13:50:48 UTC 按现行原谓词只读核查，部署前备份与当前线上 `superseded_without_chain_count` 均为 **162**，查询耗时分别0.684秒、0.406秒；源码与容器 audit.py 相同。该非零项足以造成 false，证明部署前已存在，但未重扫所有判定项，因此不宣称它是唯一异常或Memory全健康。

完整健康诊断曾超过 5 秒，而 Docker liveness 仍健康；它与数据库可访问、OneBot连接分别记录。本次没有为压标记更改运行时条件、伪造后继、改写记忆或重跑模型任务。
