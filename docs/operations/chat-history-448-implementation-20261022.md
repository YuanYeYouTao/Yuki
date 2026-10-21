# #448 实施与交付记录（2026-10-22）

任务书：[聊天记录检索增强](../architecture/Yuki-448聊天记录检索增强任务书-2026-10-16.md)。
基线 main `62bbacec`，最终任务书 `8e3721de`；实现分支 `codex/issue-448-chat-history`，独立工作树。

当前处于实现后严格复审及验证，尚未提交产品、提 PR、合并或部署。原工作目录中的本地修改未动。
用户最新收口：全部任务完成、严格终审通过后提 PR，本轮不合并、不上线；人格提示词重构由另一个会话处理。

## 逐项进度

| 任务 | 本地进度与验证 | 待完成 |
| --- | --- | --- |
| T01 | 完成：三工具合一、双 manifest/Code 投影及入口查询删除；原恢复回归通过 | PR |
| T02 | 完成：同快照页/邻文与 Client/Plugin 共用内核，保各自信封 | PR |
| T03 | 完成：原人物族、真实授权/floor/ACP 及永久原回执锚，含删除 missing 与普通锚范围 | PR |
| T04 | 完成：原作者/提及/ASR、稳定媒体编号与两种时间；observed_at 统一 UTC，真实重读回归通过 | PR |
| T05 | 完成：完整 OpenCC 字面匹配及独立 current ASR，语义与真实 FTS 回归通过 | PR |
| T06 | 完成：ID/N+1/方向/邻文；原生 AUTOINCREMENT 升级及删除后不复用回归、真实快照演练通过 | PR |
| T07 | 完成：唯一 FTS、稳定原作者 tag、一次 MATCH 及精确核验；坏查询反证已归档 | PR |
| T08 | 完成：必要作者/日期及现有 ID/媒体索引；稀疏、宽范围与最坏成本实测 | PR |
| T09 | 完成：真实入站/出站/外部事件/编辑/ASR/遗忘同步维护，源准备锁外 | PR |
| T10 | 完成：离线分段升级、三阶段中断、完整镜像能力与实际快照保真通过 | PR |
| T11 | 完成：direct/真实 Code run/resume、原子回执、Artifact、Memory 来源及迟到发布/清退；保原已发效果 | native worker CI |
| T12 | 完成：统一声明、API 7.1 可选字段、7.0 加载兼容、Fake、示例与当前模块文档 | PR |
| T13 | 完成：Linux 完整词元、实际 writer、磁盘/RSS/WAL/持锁/心跳和物理取消均有实验 | 真实生产延迟不在离线验收内 |
| T14 | 完成：D01–D12 删除与行为回归核对；全量初跑问题修复并精确复跑，五轮终审完成 | PR CI |

## 实施中确认的修订

- 结构条件逐行重新执行 MATCH 在密集作者数据上出现近二次成本，已撤销该查询形状。作者范围加稀有词也不能退化为全范围原文转换；在唯一 FTS 中追加稳定原作者的少量词元，不复制整套 gram、不存当前 root/权限。
- `SocialOperation.event_id` 是可置空的存活 FK，不能兼作永久原事件事实；保存已证明的原事件/Conversation，原件删除后只报告缺失，不抓取后来同号事件。
- `chat_events` 需要 SQLite 原生 AUTOINCREMENT，保证末尾或整表删除后的新事件不复用已发出的 ID；不新增应用层编号服务。
- 搜索来源引用只作证据，不进入 Main 激活事件集合。清退复用原隐私事务和实际持久消费者，不另建提交协调器。
- Plugin API 7.1 只增加可选参数/字段；同主版本的已支持次版本可加载，7.0 插件不因不必要的精确相等比较被拒绝。

## 实现后的五轮严格终审

| 轮次 | 实际范围与修正 |
| --- | --- |
| R1 广查 | Main/Social/个人/Client/Plugin、direct/Code/恢复、配置/目录/测试全链；收掉旧两工具和双声明，不清空历史回执。 |
| R2 细查 | 原 Person 族、账号阶段、原作者与真实回执；补永久原锚和原生事件序列。修 observed_at 重读时区不一致，保 Memory 原指纹。 |
| R3 再广查 | 搜索复制到 Code、Work、Memory 与 Artifact 的真实入口；补 Code 共同 capture 与 accepted 子回执重读传播、非 Work audit 原读取隐私。 |
| R4 深查 | 完整候选循环、writer、迁移和 SQLite；拒绝逐点 MATCH 及每页重物化 FTS IN 的近二次方案，核真实库全部 DDL/FK/索引/触发器保真。 |
| R5 反向终审 | 删空页造成的过期依赖；实际读取不能覆盖原 audit 隐私事实。修 operation 搜索忽略筛选、Fake 检索 ASR 标签及文档旧双声明。原失败测试精确复跑全部通过。 |

审查者交叉确认没有剩余已确认 P1/P2；这不是所有负载下无问题的保证。没有增加 query budget、ready 状态、重试次数、权限框架或新执行兼容别名。原来不存在的 history.v1 Feature 不补造兼容。人格与系统提示词没有修改。

## 删除与行为覆盖

D01–D03：旧工具、个人工具模块、双 manifest/入口 SQL、重复账本读取与反向 DTO 依赖退出；原 accepted reader 保留。
D04–D09：总 around 帽、50/100/400 经验限制、非空词门槛、旧 FTS/LIKE 分支、本人当前入口拒绝退出。
D10：旧运行时 FTS 与三触发器退出，冻结桥、ASR 来源失效和 Memory FTS 保留。
D11：时间游标、影子交换及就绪状态等未实施方案不引入；不伪称从产品删除了原不存在的链。
D12：旧数量帽、双声明及整行 SELECT * 断言迁为真实行为/原字段保真；不删有效权限、paid/recovery、原效果和媒体测试。

| 验收项 | 主要证据 |
| --- | --- |
| A01–A07、A25/A34/A44/A45 | history query repository、personal/canonical history、audit identity、platform/ACP、Plugin minor compatibility 与原恢复组 |
| A08–A13、A28/A31/A32 | literal matcher、真实 FTS 与 repository；负 offset/Z/+08/无 offset、微秒半开端点 |
| A14–A19、A24/A30/A35–A37 | repository、history interfaces、ClientViews；实时 ASR/keeper/floor、远隔邻文、稳定附件、ACP 成功出站、60 条页和删除尾锚续查 |
| A20–A23、A26/A33/A42/A46 | search index、result sources、真实 Code run/resume、原 operation 锚、tool audit、原 paid/effect 与 Artifact 组 |
| A27/A38/A39/A41/A43 | [完整运行时实验](../architecture/experiments/chat-history-448-2026-10-22/README.md)及取消/无 DML 行为测试；明确 FTS 内部工作与长文本调度限制 |
| A29/A40 | 0145 冻结迁移、中断回归、实际快照升级和 DDL 保真审查 |

## 验证结果

- Linux CPython 3.12.15 / SQLite 3.46.1：全仓 Ruff、mypy（635 个源文件）通过。改动 Markdown 链接与 diff 空白检查通过。
- 单元全量初跑：3,110 passed、50 failed、20 skipped。42 项由新 observed_at 的 naive/aware 差异引起，另含 1 项旧 limit 帽、2 项双声明参数、2 项 SELECT * 新列断言，以及 3 项验证容器缺 git。分别修正数据表示、测试和环境后，**原 50 个失败原样选择复跑：50 passed**。未删除行为测试或忽略真实失败。
- 集成全量初跑：736 passed、3 failed（同一 observed_at 原因），修复后的涉及组 16 passed。新真实 Code 源链另为 5 passed，3 个真实 native worker 参数留原 CI native 作业执行；普通模拟引擎不冒充真实 native 验收。
- Memory 相关 59、另 7 文件 50，来源/效果/原 audit 25，以及迁移 13、API 19、接口/Client 26 等均为定向组，计数重叠不相加。原 50 失败复跑的覆盖以独立 XML 为准。
- direct 完整 Docker 镜像构建通过，SQLite contentless-delete 能力及 OpenCC t2s 实跑通过；标签 `yuki:448-preflight` 明确为未提交验证镜像，没有发布或上线。
- 生产库只读快照 108,419 个事件，从 0143 升至 0145。9 类原字段行哈希逐项不变，integrity=ok、FK=[]；103,559 个索引事件、原生序列 114,063，12 个真实原文定点字面查询通过。FTS 页约 33.14 MiB。摘要见[快照演练数据](../architecture/experiments/chat-history-448-2026-10-22/migration_validation_results.json)。
- 升级在原生产镜像加新源码/OpenCC 的无网络隔离容器执行，未挂活动业务库。DDL 独立对照：565 对象中 554 完全不变，剩余全为计划内差异；全部原 FK/索引/CHECK 与非旧 FTS 触发器保留。大表 dbstat LIKE 统计在已完成保真校验后停止，改按实际 FTS shadow 表定点统计。

查询最坏成本、长文本的持锁与 GIL 间隔均按实测记录，没有用长度帽或空中间页伪装提速。实验不是线上缓存命中率或自然平台延迟的证据。

## 验证与交付边界

定向测试、合成规模实验、完整质量检查、PR/CI、合并、镜像、停写迁移、Bot 替换、上线检查和清理分别记录。本次只做离线测试与隔离快照演练，没有发送真实 QQ/TG 测试消息。
本次交付范围到 PR。已只读取生产库的一致快照作隔离演练，不运行第二个主动 Bot，不替换生产镜像或修改人格提示词。

本地 Windows 的完整 mypy/测试可能涉及 Linux 专属终端接口；Linux 镜像/CI 负责实际平台结论，不用忽略错误或删行为测试制造通过。
