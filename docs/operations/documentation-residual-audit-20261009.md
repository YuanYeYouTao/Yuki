# 删除残留与文档整理核查（2026-10-09）

审计基线为 main `0ddd7eee`，按[Memory 删除任务书](../architecture/Yuki-Memory-Provider分工与后台收尾审查任务书-2026-10-09.md) D01–D33 复核实际源码、调用者、配置、测试和 CI；三名新的 6.1-sol / high 子智能体分域逐份阅读，再交叉核对重点入口。本文是该日期的交付记录，不替代[现行架构合同](../architecture/README.md)。

## 文档结果

原树 **270 份受版本控制 Markdown** 全部核对：**105 份修改、38 份删除、127 份保留**。逐份路径、处理结果和理由见[审计清单](evidence/documentation-audit-20261009.json)。私有数据、生成文件与本地未提交资料未纳入公开清单；本轮另新增本报告与清单。

- **3.9.0 Release、README 中英、CHANGELOG 和升级指南**：源码 head `0102`、Plugin API 3.3、主工具 manifest 17；Code Mode 已合 main，默认 direct 不带 Monty。核对 v3.8.4 到本基线的 109 个合并 PR，不将源码、提交镜像和正式发行混写。正式 Release 仍为 3.8.4，旧页恢复 tag 对应的 `0072`。
- **架构和开发约束**：开头保留简单的删除优先原则；现行模块删除最低思考、正文 DSML 转工具、隐式付费/漏发纠正、自动Memory注入/归因/强化/意图评分、单 active/key、经验 stale/容量腾位、全覆盖与无进展挂起等旧表述。固定 function 清单和执行授权分开，native 根据可信授权/配置/协议决定声明。
- **操作与插件说明**：同步 API 3.3、模型热应用、实际批准权限、已退役组件与当前 CI。删不存在的 legacy compatibility 开关和旧质量门；旧冻结投递计划只对账/离线补账，不在线续派。
- **历史材料**：删失去接手用途的施工任务书、旧 Code 设计/交接、分阶段Memory方案及重复质量说明。真实测量、失败/费用、原交付、许可来源与旧发行按历史基线保留，不把旧数字换成当前验收或重新制造发布门。

## 源码残留处理

已删除两个无人消费的脚本：`scripts/memory_validation_budget.py`（过时固定验证额度）和 `scripts/export_pi_codemode_retirement.py`（固定源码形状/数量的历史导出）。当前源码、测试、CI、其他脚本和现行 Markdown 无调用；历史 JSON 指纹仅是当时证据，保留原值。

已修五个文件的陈旧说明：Memory maintenance、semantic embedding、runtime state / package 与 observability；可执行逻辑未改。关键词匹配未被当作删除判据：主动查询的 `MemoryQueryIntent`、真实工作者权限子集、Embedding 投影预算、冻结迁移/旧存储列和效果 reader 仍有现行消费者或历史数据责任。

## 尚未修复的真实问题

| 问题 | 已核验原因与当前状态 |
| --- | --- |
| SELF 旧代次领取阻塞 | `self_reflection/repository.py` 选择无当前源的旧 failed/waiting，再以空范围跳过整 scope；旧未完成空洞同时钳住投影水位。227 个旧批次已临时隔离，原错误、attempt、检查点、已提交记忆和回执保留；current generation 20 的 812 条消息得以处理。两轮共236批/2905聊天事件/33写入，新增失败0，有效工具与聊天积压0；已恢复全部临时设置。领取/水位源码未修。 |
| 禁用语义 scope 调度 | `autonomy_repository.list_current_autonomous_scopes` 仍交出禁用空间的旧 owner，语义调度继续 hydration，当前 live 身份校验拒绝。应停止新的无效推进并保留旧回执，不放宽权限或重绑 owner；源码未修。 |
| Provider 健康字段残留 | `config.py:llm_configured` 从旧环境字段推断，`health.py` 直接消费；真实装配只加载 v3 Profile TOML。该字段可能与实际档案配置不一致，配置/协议名也不是端点可用性证明；源码未修。 |

原任务书完成标记保留其真实范围，并补上述缺口。未把临时队列恢复、新文档或历史失败计数归零写成永久修复。

## 验证与交付范围

已通过版本身份校验 `scripts.release_validate --tag v3.9.0`、Alembic 单一 head `0102`、五个注释修改文件的 Ruff/format 和 diff 检查。233 份剩余/新增文档的 569 个本地链接与锚点全部通过，270 份原文档覆盖完整，109 个合并 PR 的祖先关系已核对。删脚本无现行消费者，五个源码文件剥离文档字符串后的可执行 AST 与基线一致。这些是文档与静态检查，不替代真实模型或生产行为验收。

本轮修改文档、删无调用脚本和修注释，没有改变上述恢复/调度/健康行为；没有新的镜像、数据库变更、正式 Release 或生产部署，SnowLuma 未操作。原 `D:/Code/My_Github_Repo/Yuki-QQbot` 的未提交内容保留，实施在原交付工作树的独立 `codex/docs-residual-audit-20261009` 分支完成；PR 与合并状态以本轮实际回执为准。
