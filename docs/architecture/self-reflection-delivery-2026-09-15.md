# Self Reflection 交付记录（2026-09-15）

## 已验证

- 57 项定向测试通过：Responses 适配器、结构化拒绝/降级、请求重试计数、配置、
  自省引用/所有权、持久 mutation、取消恢复、失败范围和扫描。
- 最后针对同一周期失败批次不可重复领取、取消恢复再测 2 项通过；未跑全量 pytest。
- Ruff 通过，mypy 604 个源文件通过。测试收集预算随 5 个新增测试更新为 931。
- 冻结生产 SQLite 副本从 0060 迁移到 0061：原事件 48047、记忆 1677、run 455、
  result mapping 60 保持不变，外键检查无错误。历史模型调用导入预算账本 640 条。
- **模拟模型**的生产副本调度：9 批处理 1503 事件，可执行积压 1469 -> 0；
  无真实记忆生成、无对外发送。该结果不是线上吞吐量或模型质量验收。
- 真实 DeepSeek Flash Responses 探针：完整 SelfReflectionOutput schema、零工具，
  HTTP 200、completed、Pydantic 验证通过，输入 1436 / 缓存 1280，输出 33，0.55 秒。
  探针使用合成输入，不包含生产消息；真实源数据生成另在上线验收进行。

## 部署阶段

首次部署保持 drain=false。代码与迁移交付后仍须验收真实管理员 manual cycle、
最终 Social 回执、真实积压下降和跨过固定调度窗口。未经这些验收不宣称整份任务书完成。
镜像、备份位置与线上结果在部署后补充。回滚保留新记忆、预算及回执表。


## 第一阶段线上证据

PR #96 已合并，main `07423409e52fb2919cad4d108c25924b737dd90c`。
镜像 `ghcr.io/yuanyeyoutao/yuki-qqbot:reflection-0742340`；源清单 622 文件逐一校验。
备份 `/opt/yuki-qqbot/backups/pre-self-reflection-20260914T171038Z`。

2026-09-15 01:12（上海）健康检查：0061、status=ok、QQ connected、Bot restart=0；
自省 enabled/running=true，drain=false。扫描与最新消息均为内部事件 53693。
主合同 174 工具，revision `b6861fe01db3fc9a459f9d714236ebbbfbc0fffa9c638075ccb966ecc52113f4`。
Manager PID 和 QQ、RSS、代理、持久环境容器 ID 与更新前一致。

使用新镜像与冻结的生产副本执行真实自省：100 事件、1 次 Provider 请求、43.54 秒，
通过 schema 与原 mutation 校验，2 项提案/2 项写入**副本**。没有写入线上记忆或发送群消息。
这补充了真实源数据兼容性验证，仍不替代真实管理员命令及跨调度窗口验收。

收尾补齐 `/ai memory doctor` 的六类自省积压与流量指标，并在成功提交后释放临时
输入输出快照，避免长期重复存储原始上下文；失败恢复快照与永久回执继续保留。


## 最终镜像与待验收项

最终代码 main `3b4edda260e5071e8147d39c686276e92bfa2139`（PR #96–#99），
服务器镜像 `ghcr.io/yuanyeyoutao/yuki-qqbot:reflection-3b4edda`。
服务器构建结果 622 个源文件与提交清单逐一一致。GHCR 上传因现有 token scope 不足
被拒绝；服务器使用已传入的源码/镜像构建交付，不能把该 tag 宣称为已发布到 GHCR。

最终备份 `/opt/yuki-qqbot/backups/pre-reflection-finalize-20260914T172428Z`。
01:26（上海）检查：health=ok、QQ connected、自省/自动化 Worker 正常、restart=0，
schema=0061，扫描与最新消息均为 53693；主工具合同仍为 174 项原 revision。
受保护容器 ID 与 Manager PID 均未改变。

CI 首轮 930 通过、1 个回滚再升级测试失败，原因是 0061 保留字段后重复加列。
已补幂等保护，仅首次建表导入历史预算；该失败用例定向重跑通过。生产副本连续两次
回滚再升级后请求 640、run 463、cycle 1、记忆 1677 均不变，quick_check 正常。
最终主提交 CI 在本记录写入时仍运行，不能声明全绿。收尾自省回归 7 项通过。

线上真实 manual cycle 尚未登记，actionable=1469；drain 仍关闭。请真实超级管理员
在数字生命研究所发送 `/ai memory self-reflection run`。已登记本任务每小时跟进，
检查真实开始/最终回执与凌晨 4 点固定窗口；符合任务书阶段门槛后再开启 drain。
尚未证明真实长期流入超过 4000 events/day 时的排空能力、真实最终报告与跨窗口去重。
这些待验收项完成前，任务书整体不标记完成。


## 真实 manual 验收与 drain 开启

2026-09-15 01:34（上海），manual cycle `sr_db3498655a58482bb111c57555c1eaf3`
completed/delivered：9 批全部成功，处理 1508 事件/52844 字符，28 项提案、10 项写入，
9 次实际请求，312.36 秒。actionable 1469 -> 0，retry/isolated 均为 0，
另有 44 条未到期事件。未耗尽输出预算，实际输出最高 7791 tokens。
记忆正文仅供用户在本地查阅，不提交公开仓库。

用户明确要求后开启 drain；配置备份在服务器 pre-drain-enable 时间戳目录。
只重建 Bot，原数据库不回滚，Manager、QQ、RSS 和持久环境保持原实例。
仍需观察凌晨 4 点固定窗口；不把当前空闲状态当成长期吞吐量验收。
