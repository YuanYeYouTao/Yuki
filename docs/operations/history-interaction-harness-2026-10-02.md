# 历史快照与长任务交互 Harness 交付记录

## 核查基线与授权

实现基线为 `513a263519f1c4d237840ffe5f27ea0697797db5`，分支
`codex/history-interaction-harness`。用户授权最后全面核查、任务书小修、实施、验证、
PR 合并及上线；具体能力验收由用户完成。本记录随进度更新，不把设计或测试冒充部署。
任务定义见[修复任务书](../architecture/history-snapshot-and-work-reporting-taskbook.md)。

三个子 Agent 分别核查历史与实际协议、工作与发送回执、执行循环与验收。
直接复用 PreparedHistory/FrozenFragments、原 effect/Social 回执、steer、CAS 和统一 Runner；
局部调整实际请求来源、checkpoint communication 子路径和执行/收尾边界。
不增加第二 runtime、汇报线程、逐阶段台账、逐输入回复决定或定时群发器。

## 上线前自然流量与前缀基线

2026-10-02 03:48（Asia/Taipei）只读检查：Bot 仍运行
`ghcr.io/yuanyeyoutao/yuki-qqbot:ops-ea446d6`，OCI revision `ea446d6`，数据库 `0088`。
从容器标签读取完整 Compose 列表，SnowLuma ID 仍为
`9e7a1a89696eae3922e4b40daee295edebc0fb60e18c7c9bf5a15efe7a1c8b85`。
AGM 仍为 `antigravity-manager:gemini-request-correlation-v4.8.4`，运行中。
本轮没有切路由或追加测试模型/QQ 请求。

Bot 只读最近最多 5000 条 invocation，并限定上一版部署后成功 `chat_agent`：

| 指标 | 数值 |
| --- | --- |
| 请求/Profile | 59 / `connection_2d4ba9aad301` |
| cache 已知 / 未知 | 53 / 6 |
| 全部 input | 2,264,476 |
| cache 已知样本 input | 2,042,270 |
| cached / 已知未缓存 input | 1,793,068 / 249,202 |
| 输入加权已知命中率 | 87.80% |
| 未知全按 miss 的保守下界 | 79.18%，不是观测到的实际 miss |

现存 prompt_projections 全为失效占位；符合 runtime 开启时跳过普通投影提交的 H1 缺口，
不能据此认定所有 cache miss 都由 H1 引起。AGM 最近 2000 条另有大量 cache NULL，
其窗口含不同任务，不与 Bot 直接比较命中率。

按这些 invocation 的原 runtime_turn_id，使用已有索引逐轮读取最多 32 条 provider_start；
筛选实际带 task_control 声明的 Gemini 主请求，共得到 59 条、19 个 turn。
比较完整 system/tools/native 配置和按 role 展平的全部 contents parts，
已有 trace 对签名/媒体的哈希引用按原表示比较，不输出正文或签名。
同轮 40 对续接均保留完整旧 parts 前缀，40 对静态设置也保持；跨轮的相邻 Conversation 样本不是
actor/read-scope 匹配样本，不能把它当作所有跨轮必须完全相同的断言，尤其不能共享私有工具尾部。

新工具合同引起一次显式新链是预期成本。后续技术验证必须比较完整实际请求，
而非只比较 stable_prefix_hash 或前 64 项。上线后只观察自然流量，分别报告已知命中、
缺失计量、未缓存 input 和必要新链；没有可比样本时不声称缓存改善。

## 实施、验证与部署

本地实现及合同已完成，尚未提交/合并或部署。主 Agent 在最终收束代码上独立运行
15 个相关套件：233 passed（226.97s）；覆盖新历史/沟通/游标/实际 wire 及原 Work、
交付、输入准备、来源守卫、压缩、子并行、主入口和固定工具面。
全仓 Ruff、format（1056 文件）、Linux 平台 mypy（683 源文件）、release_validate v3.9.0
和 diff 检查通过。Windows 原生 mypy 的 POSIX API 报错另按 Linux 目标验证，
没有修改不相关平台文件或放宽规则。SQLite schema 仍为 0088，无新迁移。

最后任务书逐项核对补出的实际修复：旧 consumed 输入不倒追催答、journal 与提醒标记
同事务发布、纯沟通不自证 state_change、非法/其他目标发送不免除阶段机会、每个输入
查原发送见证而不以 256 条展示页代替完整性、pause replay 延后新尾部，通用 Runner
保留原工具声明的对象及顺序。最新 head CI、PR/合并和实际部署另补，不能由这些本地
结果推断已上线。

| 任务书项 | 本轮技术证据 | 边界 |
| --- | --- | --- |
| T01–T03 | Gemini runtime 开/关×HTTP 重试；普通投影/真实 Work 恢复、私有隔离、SQLite 重开、CAS/来源守卫 | 准备/派发不证明模型阅读或平台投递 |
| T04 | 同 actor 选集和插件上下文收窄、源删除后重开、Profile/固定合同变化；原 generation/容量回归 | 选集收窄不是完整 ACL 实模端到端；普通图片投影组合未独立全演 |
| T05–T06 | 真 Runner 调用次序/并行 READ/委派围栏、未执行不计费、失败/unknown、真实 Social typed 结果后保存失败及原 call 无重发 | 原生服务端工具不能由本地逐次拦截 |
| T07–T09 | 阶段与新输入合并、quiet/legacy、新旧水位与断点恢复、保存前后崩溃；原输入/CAS/命令恢复回归 | 不设逐输入结清；连续 steer 跨所有压缩/重启组合及报告语义质量待行为验收 |
| T10–T12 | 明确退出有限反馈、final/状态修改证据区别、原 artifact 附言和子任务/前台容量回归 | 不宣称模型语义目标已达成或 QQ 体验已验收 |
| T13 | Chat、DeepSeek/OpenAI Responses、Claude 实际发送：reply 后原 ID/budget 继续业务和显式退出；完整 Gemini 前缀 | Claude 仅已知 cache_control 标记移动单列；其余内容/工具/系统严格比较 |
| T14 | 9 项/跨 Work/未 staged/平台字符串/错误目标在副作用前拒绝、>256 回执、原子失败与重开、typed 保存失败 | 新元数据复用原 privacy 清理，非独立第三账本；无真实群测试 |
真人 QQ 交互、阶段汇报质量和长任务语义效果由用户验收。
