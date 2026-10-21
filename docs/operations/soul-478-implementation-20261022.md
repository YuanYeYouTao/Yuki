# #478 单 SOUL 与角色名称实施记录

日期：2026-10-22。任务与原证据见[任务书](../architecture/Yuki-478-SOUL人格边界与角色名称统一重构任务书-2026-10-22.md)。本页保留实现提交前审查和验证记录；最终CI、联合合并、生产切换及清理结果见[交付记录](https://github.com/YuanYeYouTao/Yuki/pull/490#issuecomment-6326508861)，各认实际结果。

## 当前范围

后端从 `main@62bbacec` 独立实施，复用桌面分支的必要角色资料/授权和文件发布语义，没有整体合入电脑与执行器等无关功能。参与库另提 [PR #24](https://github.com/Institute-of-Agent-Life/Yuki-Semantic-Participation/pull/24)，宿主固定实现提交 `438f204f9ebe2b8c1b698b68c0f1b3ac8b4facef`。独立 Desktop 已完成单 SOUL 编辑及真实 Electron 场景验证，公开合同固定后端 `06b9378f07a565b67122eebd79e080e0ecf4835e`；旧草稿采用显式选源导出，不保留运行时旧 key 分发或导入。客户端 [PR #2](https://github.com/Institute-of-Agent-Life/Yuki-Desktop/pull/2) 已提交，五平台 CI 已通过。

用户已授权本轮及 #448 相关 PR 准备后联合合并上线。下文的未决状态均为提交前快照，不能替代上方交付记录；本次验证未调用付费模型或发送真实平台测试消息。

## 已落源码

| 范围 | 实际变化 |
| --- | --- |
| 人格来源 | `SOUL_FILE` 默认 `character/SOUL.md`，只读一份原件；`config/SOUL.md` 只供明确初始化。旧配置源、路径 fallback、占位符和双文件运行入口退出 |
| 角色资料 | `character_profile` 保存当前名字，显式初始化仅创建缺失角色；改名按原请求回执和真实 profile revision 发布当前内存投影，无 TTL/刷新队列 |
| 装配 | 最小角色身份独立于可空 SOUL；只做精确 `{{name}}` 替换。Main、SELF、自动化、子 Agent、自省与 SELF Dream按职责消费同源资料 |
| 模块分工 | 普通事实提取、人物 Dream、视觉和独立插件会话保持自己的职责，不全局扮演角色；角色审美与工具/Work/统一发送规则分开 |
| 名称消费者 | 历史渲染不再依赖当前角色名；提及/SELF目录用事实名称或中性称谓；固定工具说明、权限与能力目录删除角色名副本 |
| JEV | 新观测读取当前名字和别名参考，问句使用中性 Agent 称谓；评分键、状态和 Controller 语义不变，不因改名重置或重发 |
| SOUL 控制 | 个人/管理 API、CLI、SDK使用单 SOUL 原件。保真实 owner/operator、原 UUID、文件 CAS 与写入/生效事实；旧双文件 API不保留 |
| 安装部署 | 可写角色目录、模板显式初始化、空原件保留、私人资料不入镜像/发行包；见[升级说明](soul-deployment-20261022.md) |

新增迁移采用 `0147_character_soul`，避免与未合桌面分支的不同内容 `0146` 碰撞。已在本分支整合 #448 PR #489，角色迁移接到其最终 `0145`，最终链为 `0143→0144→0145→0147`，不能只按版本数字 stamp。原桌面独立分支库的直接升级尚未验证，不将本次 main 升级演练冒充该来源已支持。

## 实现后审查与修正

1. 固定工具描述中的动态角色名会在部署冻结后停留旧值。直接改为通用业务称谓，改名不重建 manifest。
2. 空/无变量 SOUL 仍需真实名称。增加最小角色身份参考，不强迫正文写变量或通过姓名一致性检查。
3. 普通工具往返保持旧前缀还不够：Compact adopt 回调原先会重读热更新后的 SOUL，改变同激活公共整理输入。现捕获一次既有静态贡献并复用；回归同时验证模型请求和公共整理源。
4. 角色 PATCH 的可选字段采用真实 partial schema：遗漏不改，明确不合法类型沿协议返回；原请求 UUID 与统一回执查询一致。
5. 删除保存前“目录可写探测”门槛，直接执行原子文件发布并报告实际结果。
6. 文件已替换但确认丢失时，SOUL受控文件命令按原 UUID/原 payload及实际文件hash恢复；不扩成未知外部效果自动重发。
7. 冲突与失败回执只保存必要 revision/written 事实，SOUL正文不复制进普通诊断审计；获准编辑者通过原读接口取当前正文。
8. #448 统一历史接口新带入 `bot_name="Yuki"` 缺名回退，合流时直接删除参数与覆盖块，沿原事件名或中性类型；不新增名称修补机制。工具合同正常升至27，改名本身不改变冻结声明。
9. 新 owner grant 的外键若不级联，会阻止原 Person 遗忘。直接用真实 Person 外键级联删除授权，保角色主体不变；无账号人物的实际遗忘回归通过。
10. 插件原始 QQ 发送漏传预备的搜索 terms，会持 writer 等线程归一化。将这一步移到 claim/发送前，原确认与事件/索引结算仍在原事务；回归检查锁外准备及原回执不重发。
11. 仅改 SOUL 会改变整体 system hash，原已付响应的发送尾部曾被当作合同变更丢弃。Main 恢复兼容性现只认既有 INVARIANT 技术规则、模型与固定工具合同，人格仍保存在原请求正文；5项实际持久恢复回归覆盖普通/opaque尾部、未知派发、合法新业务人格和真实技术变更。
12. 个人 SSE 原来不报告角色资料、owner与SOUL变化，桌面第二设备无法及时刷新。复用原 commit_hints/sync 差分，SOUL事实只对 owner 提供，不读文件、不新增轮询或缓存。
13. SOUL首次读失败归入原unknown恢复，保原UUID/receipt；不读旧路径。公开OpenAPI导出显式固定LF，避免平台换行导致真实合同hash不一致。

## 提交前验证记录

以下为不同时间/范围的实际运行，存在重叠，不将数量相加冒充一套全量结果。

- 参与库完整 257 项通过，Ruff、Mypy 和两个 CI 通过；PR尚未合并。
- 历史、原生提及与 normalizer 63 项通过。
- SOUL、Memory、实际装配、插件及权限目录相关 79 项通过。
- 原子文件、真实 HTTP/SQLite、owner撤销、SDK确认丢失与审计正文回归 5 项通过。
- 安装/部署/平台相关 58 项通过、4 项按POSIX依赖跳过；发行包 5 项通过。后续部署、HTTP环境与材料存储完整相关组116项通过。
- Host参与、人物合并、投影与前台准备 29 项通过；HTTP环境与材料存储 72 项通过。
- 主代理组合 SOUL Settings、角色 API、实际 Main/后台请求及平台装配 32 项通过。
- 与 #448 合流后，SOUL、历史接口与参与生命周期55项通过；真实独立进程SOUL接口已覆盖配对、两设备撤权与空正文。
- 全源 Linux 目标 Mypy检查642文件通过，Ruff通过；后续改动仍按最终head重核。
- Desktop实际Electron两项场景通过：配对、空SOUL保存、Main强杀后草稿恢复；另一设备改名即时刷新、SOUL冲突保本地草稿和原CAS、撤销owner后隐藏编辑器。
- 5项持久恢复新回归及关联边界组97项通过；历史名回归3项、插件锁外准备回归1项、角色含遗忘和SSE回归组通过。
- 新operator初始化与示例补明角色读写能力，既有operator不自动扩权；部署/操作者相关53项通过。
- 真实空数据库完成冻结迁移及显式角色初始化，重复初始化保持同一ID、名称和revision。未用测试 `create_schema()` 代替这项演练。
- 最终合流纳入飞书/Lark [PR #491](https://github.com/YuanYeYouTao/Yuki/pull/491)，保持实际内部身份、原 Presence 和统一 Social 回执；容器测试显式初始化角色，未恢复旧 Settings 字段。飞书相关81项、角色/平台组合30项通过。
- 生产143一致副本真实迁移到147用时112.75秒，13组原字段逐行hash及数量不变。迁移后9个变更/新FTS影子表定点quick_check、FTS integrity-check、原外键定义和实际12条literal检索通过；整库迁移后quick_check因I/O耗时被中止，未完成，不能记为通过。另准备上线前143完整验证备份。

Linux旧head集成752通过、5个旧断言/未初始化角色的测试失败，均已修正；旧unit3184通过、22个迁移准备错误已定位为测试session选源时序。改为session显式选定公开测试SOUL，并使0145测试区分冻结终点与当前head；31项迁移/启动回归中原4个head断言修正后相关18项通过。未添加运行时默认或旧字段。Windows整套集成723通过，另有已纠正断言、长路径和POSIX依赖失败，不能冒称全绿。

最终完整测试及 CI 正在收尾，不能用局部通过代替最终合流结果。新名称和人格会改变相应请求前缀；同配置字节、固定工具与公共历史稳定不等于已实测线上缓存命中率。

## 任务覆盖与当前交付

| 任务 | 实现与证据 |
| --- | --- |
| S01 | main/桌面/参与库固定基线、0143不同谱系与本次147链已核，未整体引入桌面产品后端 |
| S02–S03 | `config.py`、SOUL模板和CharacterRepository：唯一原件、空人格、精确变量、当前名称投影 |
| S04–S06 | PromptComposer与历史/提及/目录全消费者、参与库PR24；实际Main/SELF/自动化/子Agent与后台请求回归 |
| S07 | 角色/SOUL个人与管理API、CLI/SDK、公开API6和原请求回执；原文件写入/生效分开 |
| S08 | Desktop单编辑器、明确导出旧草稿、固定合同与实际Electron验证；客户端PR #2及五平台CI已通过 |
| S09 | 安装/挂载/打包与显式升级已实现；真实线上143一致副本联合升级演练进行中 |
| S10 | 开发约束、现行Main/Compact/API文档、README中英和升级说明已更新 |
| S11 | 四轮计划复审及实现后交叉审查完成；最终全量CI、联合合并和线上仍待实际结果 |

## 联合交付边界

后端[PR #490](https://github.com/YuanYeYouTao/Yuki/pull/490)、历史[PR #489](https://github.com/YuanYeYouTao/Yuki/pull/489)、飞书/Lark[PR #491](https://github.com/YuanYeYouTao/Yuki/pull/491)、Desktop[PR #2](https://github.com/Institute-of-Agent-Life/Yuki-Desktop/pull/2)与参与库[PR #24](https://github.com/Institute-of-Agent-Life/Yuki-Semantic-Participation/pull/24)联合交付。目标Host4.0还需删除实际启用插件的旧版本上限，见Kun[PR #4](https://github.com/Institute-of-Agent-Life/Yuki-Kun-Game/pull/4)和GitHubMonitor[PR #2](https://github.com/Institute-of-Agent-Life/Yuki-GitHub-Monitor/pull/2)；其代码与权限不变，新manifest沿原控制面按原批准集合重新核准，不手改hash。生产切换按现有 Bot-only、备份及引用核对流程执行，保原数据库、文件、工作区、回执与无关服务；健康和自然流量证据另记。
