# 控制面人物授权与命令重构实施记录

日期：2026-10-18。基线：`main a12f9dd1`。对应[任务书](../architecture/Yuki-控制面人物授权与管理命令重构任务书-2026-10-18.md)。本次授权为实施和提 PR，不合并、不部署，不发送真实 QQ/TG 测试消息、不购买真实模型请求。

## 实现对应

| 单元 | 实际落点 |
| --- | --- |
| T01 | `persons.is_superuser`、迁移0136、当前原actor/root有效授权、合并资格与账号转移分离；迁移 Chat、Work、Automation、Plugin Host、Memory 消费者，删除启动 QQ 名单权源 |
| T02 | `admin/operations/{base,identity,config,groups,plugins,models,work}.py` 和只读 `admin/queries.py`；身份服务由容器独立注入，原 owner/回执继续负责实际效果 |
| T03 | `services/commands/` 按领域解析和呈现；显式 `/ai admin`，自助/会话/link/forgetme独立；删除图阻写、PrivateAccess包装、旧裸管理别名和 Processor 后备构造 |
| T04–T05 | Person/Binding/Presence enable/disable、资格grant/revoke、恢复与有限分页发现；Work来源分类及原management_view；App/ACP退役入口重启不连接且不阻塞其他入口 |
| T06 | PluginManager分开已批准定义与运行实例，停用保留配置schema；Loader读取真实新源码，避免同秒同大小旧pyc；模型同密钥别名可轮换 |
| T07–T09 | App/ACP统一个人行为与合同23，保原App namespace/配对事实；标准prompt接原waiting Work；SDK按真实turn结束；删除设备GET活跃度DML与0137字段 |
| T10–T11 | 完整doctor、平台健康分列和环境unknown；安装operator policy、只读升级候选导出、开发约束和现行文档更新 |

聊天管理请求用原事件canonical UUID和内部event主键关联原控制回执，保存原revision。已有UUID、hash、operation ID和reader保原语义；未知效果不重派发，不为改中文提示再次执行。Emoji修改沿现有外部意图/效果回执；Memory修正沿原mutation回执，固定原事件operation ID。

没有新增RBAC平台、命令总线、授权缓存、完成台账或恢复表。当前权限核验放在真实领域读写/新效果处；普通派生索引、后台worker和已经付费的结算不被人物管理资格整体封锁。角色变化不修改模型已提交工具声明或历史payload。

## 五轮复审

| 轮次 | 实际发现与处理 |
| --- | --- |
| R1 人物与凭据 | 原actor与当前root均启用；同人合并只OR双方当前资格，撤销不从旧alias复活；修正可选Person关联operator被错误套用人物角色门槛，保其原显式capabilities |
| R2 命令与回执 | 配置使用原writer/变更历史，修复change ID和生效方式漏报；Emoji doctor改纯读，显式repair负责修复；普通聊天管理重放保原revision，adopt旧请求不复活后来的unadopt |
| R3 恢复与客户端 | 补Work原来源/暂停与操作投影；修复失败配置history rollback的null崩溃；离线插件schema和同别名轮换；标准stdio跨冷Runtime接续原Work |
| R4 执行与删除 | 身份改名/遗忘/转移真实writer前当前核权；群new在generation writer核权、手工群compact在新model admission核权；Emoji媒体await后、Memory跨人查询/付费批量队列按真实效果复核；删除Preference独立后备writer、旧lockout字段和无用角色传参；修正原内部引用ID在接纳时被覆盖的旧缺口，按原conversation keeper还原引用正文 |
| R5 逐项反查 | 停用App/ACP Presence会阻塞整个Bot重启，已修为保退役状态并跳过连接；清理旧SUPERUSERS活动配置和插件授权文档；删除新增的升级说明前置拒绝，操作说明不变成额外门槛；帮助和能力目录按真实只读/修改语义投影 |

三路独立审查和主审交叉核对后，对发现项补针对回归。次数和测试数量不证明线上没有问题；以下只记录实际本地/CI证据。

## 验证与交付

- 真实SQLite/ASGI管理跨审：`test_admin_control_review.py` 9项通过，覆盖operator可选人物关联、合并撤销重放、writer前撤权、只读SQL分页、CLI、Work、doctor和config原writer。
- 真实个人整链：`test_personal_control_convergence.py` 13项通过，含两个官方stdio进程跨冷Runtime续接、原journal/source/预算/效果回执保留、第二UUID重放不重复调用及App/ACP退役重启。
- 管理生命周期、0137升级/降级保设备撤销和业务原件、平台健康分列均有实际领域回归；插件离线schema、Emoji长await撤权/unknown/旧请求、人物生命周期与Memory重建各有专门回归。
- 新增测试使用假模型/假平台，不以桩权限恒真替代真实Person与SQLite事实；Monty可选原生组件及真实供应商usage、缓存命中、线上延迟不在本次本地验收范围。

全仓 Ruff 通过；Linux 目标 mypy 648 个源码文件通过；`git diff --check` 通过。上下文装配/压缩/发送观察/缓存usage与原command receipt组合77项通过，历史媒体/私有原件升级30项通过。人物/Memory/Automation等大定向组172项通过，重建真实排队撤权与原paid恢复42项通过，setup/export（含真实0119/0128只读导出）43项通过；这些组有重叠，不累加为总数。整库回归与最终PR/CI结果在交付时另记，当前不宣称整库或CI已绿。本地实现不表示已经合并或上线；生产候选导出、实际赋权和部署均未执行。升级操作见[具体步骤](person-admin-control-plane-upgrade.md)。

贡献归属：人物/账号/历史边界、删除优先和命令重构方向由维护者 YuanYeYouTao 确定；本次实现、回归与交叉复审由 Codex 按该任务书完成。
