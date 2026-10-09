# 旧关系评分系统删除与生产清退回执

核验时间：2026-10-10T00:43:54+08:00（Asia/Taipei）。[任务书](../architecture/Yuki-旧关系好感度系统彻底删除任务书-2026-10-09.md) R00–R23全部完成。

实现 [PR #278](https://github.com/YuanYeYouTao/Yuki/pull/278) 已合并；部署源码 `0046d3949ce0b8fe009086fbbbf0082cb10ee317`，镜像 `ghcr.io/yuanyeyoutao/yuki-qqbot:ops-0046d394`。镜像压缩传输SHA-256为 `6166527c80a9191717b53d049d55784f822e91d8994302ca1387461f296c6c54`，本地与服务器一致。未创建正式Release。

最终 [Linux CI](https://github.com/YuanYeYouTao/Yuki/actions/runs/37957673031) 1074测试通过/49跳过，Ruff、mypy、前端测试和构建通过。主镜像在隔离本地源代码分离部署目录完成启动、0103和重建持久化核验，再上传生产。两组新的gpt-6.1-sol/high子智能体独立终审无源码阻塞。

## 数据库和旧Work

生产0102正常升级0103。三张评分表、评分专属索引、七项relationship override、自动化两表的context.include_relationship均不存在；SDK3.4、九模型任务、direct镜像无Monty。只移除真实模型配置的评分路由，其余Gemini routes、profiles、搜索连接保持。Memory自身confidence和真实relation事实保留。

切换前13条挂起Work已先只读取证：[创建、暂停记录时间和原因](work-suspension-evidence-20261010.md)。没有suspended_at，暂停记录更新时间不能当历史第一次暂停时间，缺少原异常细节的记录仍注明未知；没有盲目续跑。

停止旧Bot写者后清退812条全部旧Work，Work-owned journal/effects/inputs/waits/children/scopes/recovery/budgets/delivery/protocol/media引用均清零，旧ID在新库交集为0。263个相关外部沙箱执行清理前均已完成；共享环境、原执行事实与已发布文件保留。run55/89收尾uncertain并保留原usage，原自动化77的cancelled和85的completed状态不被复活；job594取消后解除Work引用，独立发送回执保留。

清退事务内保护表核验如下；启动后正常新事件可继续增加记录，未把全库长期为零作验收条件。

| 表 | 清退前 | 清退后 |
| --- | ---: | ---: |
| chat_events | 88199 | 88199 |
| persons | 58 | 58 |
| spaces | 8 | 8 |
| memory_facts | 2506 | 2506 |
| memory_fact_relations | 523 | 523 |
| model_context_observations | 736 | 736 |
| model_context_selections | 22632 | 22632 |

SQLite quick_check=ok、foreign_key_check无异常。0102一致性SQLite备份和配置原件保存在服务器私密ops目录；备份在停Bot前完成，不声称包含备份完成后全部新消息。未降stamp、补回评分表或恢复旧库覆盖新数据。

## 生产配置与运行状态

实际66个Compose文件及.env中的退役环境声明已清理。首次启动发现operator文件位于config挂载，原webui-config目录扫描没有覆盖，旧权限导致拒绝启动。停止Bot后额外备份实际文件并删除control.relationship.read/mutate，目标parser通过；原账户、凭据、角色与其他权限保持。新Bot前向重建恢复，最终健康检查通过。

两个原批准插件更新API3.4，重新批准限于“原已批准权限与当前申请权限的交集”，保持原enabled状态，没有新增授权或自动批准其他插件。最终两个插件运行；原webui操作员声明通过目标parser。

生产Bot healthy、restart=0、healthz status=ok、database=ok、OneBot连接。SnowLuma容器ID、镜像及StartedAt完全不变，QQ登录状态未操作。没有向QQ发送额外测试消息；本回执证明部署、schema、配置与连接，真人自然对话验收另以新请求证据判断。

同一健康快照的Memory consistency仍为false，显示8项contested facts、25项active contested facts；SELF有70个actionable事件，processing=0，Dream记录71个failed clusters。这些状态没有因评分退役被清空，也不在本次任务中宣称修复。另有缺失manifest的旧网易音乐插件安装记录，disabled/invalid，保持该事实；两个当前批准插件均running。实际安装表无退役relationship权限残留。

## 删除统计

实现变更167文件，+734/-44768行，净减少44034行。运行时/前端/插件删3727、测试747、迁移234、脚本/配置569、Markdown文档156、失效JSON/TXT库存39335。永久源码只有实际删除和必要0103存量升级；生产Work清退脚本是私密一次性操作，未加入长期协调框架。
