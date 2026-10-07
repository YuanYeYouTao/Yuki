# Yuki 删除导向重构编码任务书

版本 3.0 · 2026年10月7日设计基线 · 2026年10月8日实施记录

历史设计基线保留全部67个既有ID，新增PAR-01后为68项。实施期间按用户要求追加DEP-02（默认direct构建）与RES-03（内存增长检查），当前共70项。原设计的固定源码和离线反例继续作为背景；当前实现与验证状态以任务索引、文末实施记录及运维证据为准，索引勾选不等于生产上线。用户转来的第三方部署报告作为线索独立核验，不照搬其中的新缓存、恢复或健康框架要求。

## 固定基线与证据读法

- main：aecc09d621067b7782c2b89e919fe2a7b2c9e5fa
- Pi：e7bc7d3275b09ddc5363bbb1eb9d2ee255f276ed；2026-10-07 16:28 UTC复核远端未变
- Monty：3f9d6ef413fb951e5b80113b7088d535bd028fcb，精确源码1.0.1；第二轮组件测量为官方1.0.0
- 源码定位为“显式分支:仓库相对路径:行号”；main/Pi表示两树该段均核对，Monty以独立仓库为根
- 先用静态AOCI职责导航，再追源码和真实caller；本轮无可用AOCI工具/CLI服务入口，不声称正式服务认证
- 已复现指隔离现状，验收指实施后目标；通过probe往往代表旧问题被证明，不是修复已通过
- 第一份第三方案例部署SHA未知；第二份声明本地878bc9348faf2b8ac55205d53737d5b13240646d对应Pi e7bc7d32，未独立核验。所称md/json仍未提供；两份报告的资源/延迟均不属于用户机器，不与独立测量合算

## 编码前置与发布条件分开

P0/P1/P2为优先批次，不是漏洞等级。每项只列实际编码前置；共享文件需要协调而非等待整包完成，真实迁移和公开升级条件另列。

- 整项硬前置：INT-04需INT-02；ID-07需ID-02；RUN-05完整终态重构需RUN-06
- 条件子项：RUN-08仅删除restart signal链需API-02；APP-07仅删冗余attempt guard需APP-06 parity，新鲜性修复可先做
- INT-03负责合同设计和最终集成，不是所有任务的开工许可证；未知0097谱系只阻塞相关迁移和发布
- 每项内部仍先迁完真实caller，再删旧对象；最终全部在完整main/Pi集成树完成恢复、schema和发行验收

## 统一完成要求

1. 列出真实删除的文件、函数、字段、writer和最后caller；记录唯一剩余owner、历史reader及新增/删除/净diff，不用新manager/wrapper/永久双写替代删除
2. 当前授权、source/privacy/generation、有效租约/fence/CAS、原效果ID、unknown、累计预算、协议配对和不可变交付保持
3. 新接纳、原在途执行、历史只读分别退出；先停旧新接纳，原ID结算或明确退役，最后删执行解释；不能清历史或重做旧效果来凑“兼容清零”
4. Code保留低频工具仓、终端常驻；优先单worker共存验证。第三方压力不能套到用户机器；64MiB配置也不能放行目标环境或自动全局禁Code；optional off仍须全部获准工具可达且同一loop
5. Host状态与首轮资料在prepare_request首次序列前定稿；旧exact journal、handoff事实和后续工具轮次不重排；公共projection仅在真实持久合同改变时升级
6. 本版不报虚构性能收益；分别量SQL、物化/编码、writer、调度与IO、整树资源和自然端到端延迟

## 本版主要修正

- FILE-01、SDK-01、CLI-01、AUTO-01、TERM-01补齐附件writer、批准版本和旧accepted cohort真实退出；200MiB附件与停机bootstrap保留
- CTX-06最终边界为首次prepare_request，含restore/rebase及首轮steer，不能提前在activate定稿；不让MainAgentContract吞并prompt/provider owner
- APP-07修复prepared短路；RUN-07修正repr来源指纹；RUN-06补失租后有效owner结算；CTL-02保持旧receipt先于新校验
- CTX-05删除普通signal撤销失败退避及内容误判，复用已有517纯DB重备；APP-02纠正/livez已存在的现场归因
- PAR-01仅合并同tick参与准备，保留hydration后anchor与所有source边界

## 任务索引

- [x] INT-01 · 已完成（源码及定向回归；集成验收、部署另记） · P0 · 消除重复0097身份
- [x] INT-02 · 已完成（源码及定向回归；集成验收、部署另记） · P0 · 删语音接口，但不能删已发生效果的读取能力
- [x] INT-03 · 已完成（源码及定向回归；集成验收、部署另记） · P0 · 合同版本不能回退，Code Mode与SDK分开演进
- [x] INT-04 · 已完成（源码及定向回归；集成验收、部署另记） · P0 · 长Social调用键与退役voice前置顺序
- [x] ID-01 · 已完成（源码及定向回归；集成验收、部署另记） · P0 · 删除QQ正数-only门槛，收拢平台引用格式判别
- [x] ID-02 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 删除旧入站旁路和第二套去重 owner
- [x] ID-03 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 按九个构造入口删除身份副本
- [x] ID-04 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 删除 synthetic scope 整数和内部影子身份链
- [x] ID-05 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 删Social文件分支的重复回放预检，保留唯一早返回与原子claim
- [x] ID-06 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 缩小“所有发送异常=uncertain”的范围，只在真实可能派发后保留未知（有条件修正）
- [x] ID-07 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 保留canonical回复三值判别，删除只限“不再可达”的入站前fallback
- [x] ID-09 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 迁当前执行账号并删除旧账号永久存活门
- [x] RUN-01 · 已完成（源码及定向回归；集成验收、部署另记） · P0 · 删除 mandatory start 门禁，不再让“先汇报”成为工作许可证
- [x] RUN-02 · 已完成（源码及定向回归；集成验收、部署另记） · P0 · 删除等待条件中的聊天正文副本
- [x] RUN-03 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 整删 Work 展示计量的强制生命周期
- [x] RUN-05 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 迁现有终态入口并删除礼仪恢复和重复止损分叉
- [x] RUN-06 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 统一完成与失败退出结算并保留有效租约 CAS
- [x] RUN-07 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 复用恢复输入并修正来源指纹
- [x] RUN-08 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 删除无消费接口和 receipt_key 恒等转换
- [x] RUN-09 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 从生产核心移出测试专用状态和合成frame
- [x] RUN-10 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 资源政策与正确性约束分开，删除不一致的隐藏门
- [x] DEP-01 · 已完成（源码、direct 镜像与生产内核隔离镜像验收；部署另记） · P0 · 保留 Code 低频工具仓并完成单 loop 可选模式
- [x] CTX-01 · 已完成（源码及定向回归；集成验收、部署另记） · P0 · 允许部分摘要，取消摘要覆盖率对业务的强耦合
- [x] CTX-02 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 删除派生统计对成功发言、记忆变更回执的同步依赖
- [x] MEM-01 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 删 Memory 预取旧路径与独占写的重复会话状态，复用执行权限和持久回执
- [x] CTX-03 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 短状态删双重容量门，保留原CAS及明确共享语义
- [x] CTX-04 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 删前置字符硬拦，统一真实请求硬容量；保持确定性prefix
- [x] CTX-05 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 删除 Rollup 内容误判和覆盖失败退避的重复写
- [x] MEM-02 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · Memory rebuild收尾只依赖既有事实回执，避免统计失败变成重跑模型
- [x] MEM-03 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 清理仍暴露的旧Memory执行接口与过时文档，给出真实退出条件
- [x] API-01 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 直接删除死内部 registry/result DTO
- [x] API-02 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 删除动态 schema-rebuild 死链，只保留执行授权投影
- [x] API-03 · 已完成（终审补齐 final 领域原回执分类；114+56 回归及差分通过，部署另记） · P1 · 退出活执行路径的字符串结果猜测，历史解码仅留在原回执入口
- [x] RES-01 · 已完成（源码及定向回归；集成验收、部署另记） · P0 · 合并结果预算，删除“读回以后再摘要”的多层循环
- [x] RES-02 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 删除“派生artifact发布失败升级成业务失败”路径
- [x] FILE-01 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 迁附件不可变快照后删除可变 artifact 写入口
- [x] TERM-01 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 删除旧一次性 Manager 引擎和 run_python 新提交链
- [x] TERM-02 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 终端能力常驻并冻结工具合同
- [x] CTL-01 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 删薄别名、合并合同来源，拒绝用万能dispatch替代授权
- [x] DB-01 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 删除两条没有真实 caller 的运行时兼容面
- [x] DB-02 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 合并UserProfile兼容类；删artifact空catch
- [x] ART-01 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · image artifact删除第三轮授权读取，保留I/O前后两轮
- [x] DB-03 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 媒体UPSERT用RETURNING替代写后按同一唯一键重查
- [x] ART-02 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · GC保留deleting，但合并每条文件后的独立DELETE事务
- [x] DB-04 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 删SQLAlchemyError→STATE_MISMATCH泛化，诊断删文本locked猜测
- [x] CTL-02 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 控制面上传预检移出writer，并删第二次解码
- [x] DB-05 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · web来源入账删writer内URL/标题准备与无界历史物化
- [x] MIG-01 · 已完成（0058/0060/0088 原停点结构及升级保留事实，Windows/Linux 各 3 项通过） · P1 · 冻结历史迁移，不删历史兼容升级逻辑
- [x] APP-01 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 删空的 application ProviderRegistry，而非另建 registry
- [x] APP-02 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 删 ApplicationModule 空契约与纯兼容 exports；健康模块须区分
- [x] APP-03 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 退役无 TOML 的 LLM_* 路由；保留 v3 TOML 的 environment indirection
- [x] APP-04 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 把旧 provider 注入缩到测试，不让业务双签名永久存在
- [x] APP-05 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 删除旧 delta 接口；旧 journal hydrate 不在此删除清单
- [x] APP-06 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 合并 DeepSeekResponses 与 JSONHTTP 的 transport，保留物理 attempt 语义
- [x] APP-07 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 恢复每次派发的新鲜校验并删除纯配置重复
- [x] APP-08 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 删搜索缓存对业务成功的强依赖（可独立实施）
- [x] APP-09 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 资源关闭 owner 收口，消除首错中断后续清理
- [x] APP-10 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 结构化校验/修复：留契约，删调用分支而非删校验
- [x] RUN-11 · 已完成（源码及定向回归；集成验收、部署另记） · P0 · 新业务输入删除过期重复结果历史
- [x] CTX-07 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 移除 native 来源索引阻断已成功收尾
- [x] AUTO-01 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 退役 generated 和 yuki.generate 双名
- [x] AUTO-02 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 统一 Automation 提交编排和显式输入
- [x] SDK-01 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 删除 LLMFacade 并统一主 Agent 运行合同
- [x] API-04 · 已完成（源码及定向回归；集成验收、部署另记） · P0 · 删除 Automation facade 错误参数镜像
- [x] CTL-03 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 删除外部回执压扁和重复映射
- [x] CLI-01 · 已完成（源码及定向回归；集成验收、部署另记） · P2 · 迁在线插件管理并删除 CLI 直写数据库
- [x] CTX-06 · 已完成（源码及定向回归；集成验收、部署另记） · P0 · 运行状态置于当前真实发言之前
- [x] PAR-01 · 已完成（源码及定向回归；集成验收、部署另记） · P1 · 合并同轮参与反馈准备并保留原回执事实

- [x] DEP-02 · 已完成 · P0 · 默认 direct 构建与生产部署，不加载 Code Mode
- [x] RES-03 · 已完成 · P1 · Yuki 内存增长检查与可复现泄漏优化

## 详细任务

### INT-01 消除重复0097身份

- 实施批次：P0
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：Alembic唯一冻结迁移图
- 真实调用方：Alembic upgrade、release/setup、schema guard
- 编码前置：无
- 协调边界：生产谱系只阻塞相关迁移/发布；迁移图调查与任何独立代码删除并行。
- 实施或发布条件：实施前须确认部署数据库谱系；迁移与破坏性删除另行审批
- 固定源码：Pi:migrations/versions/0097_reconcile_mcp_retirement.py:12–15；Pi:migrations/versions/0098_freeze_summary_representation.py:6–18；main:migrations/versions/0097_retire_speech_output.py:154–217

- 证据/来源：Pi:migrations/versions/0097_reconcile_mcp_retirement.py:12–15 是0096→0097 MCP退役；Pi:migrations/versions/0098_freeze_summary_representation.py:6–18 是0097→0098 summary字段；main:migrations/versions/0097_retire_speech_output.py:14–17 又占0097。实际调用者Alembic upgrade、release/setup与schema验证；Pi:scripts/verify_monty_packaging.py:116硬编码预期0098，相关test直接upgrade0097
- 多余状态/错误：文件名不同，Git不报冲突；两个同名revision会使Alembic图歧义，且数据库仅记“0097”无法说明究竟执行过哪套DDL。不能通过两head merge revision解决“同一个ID有两个意义”
- 最小方案：先取得所有要支持数据库的**来源提交+alembic_version+冻结schema形状**。若证明Pi0097/0098从未用于任何需保留DB，保留已进入main的语音0097，Pi MCP改0098（down0097）、summary改0099（down0098），同步head期待、导入名、fixtures/文档。不要重写0048–0096
- 若已有Pi0097/0098数据库：禁止只改文件名/盲stamp/自动按版本字符串猜来源。需要独立、一次性的显式源谱系升级方案，分别认证main0097、Pi0097、Pi0098形状、保留数据和执行事实后转换到一个不碰撞的新唯一revision；具体revision图必须在知道已部署谱系后确定，不建立永久运行时“多schema兼容器”。无法认证来源应停在运维升级前，而非向日常业务加门禁
- 唯一owner：Alembic冻结迁移图；退出：所有受支持DB处于同一目标schema，一次性转换脚本/支持期限明确；不变量：已有event/effect/receipt/预算不重置，summary原表示不被重新标注，语音事实删前一致冷备
- 验收：空库→目标、0096→目标、main0097→目标、已存在Pi分叉（如有）→目标；异常shape/FK/view/trigger/pending生成失败零写入；事务故障完整回滚；head唯一；保留ASR/音频历史/共享artifact；失败迁移不触发Bot启动
- 风险/依赖：最高；实施前需要真实部署谱系与破坏性删除授权。此调查未读取生产DB，因此不能宣称可直接重编号

### INT-02 删语音接口，但不能删已发生效果的读取能力

- 实施批次：P0
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：现有Social receipt与Work journal decoder
- 真实调用方：旧持久Work、未决Social receipt、插件和Automation旧schema
- 编码前置：无
- 协调边界：main已有代码可整合；旧事实读取不依赖先访问生产库。
- 实施或发布条件：只读旧事实保留；发布和旧能力退出须清点未决调用
- 固定源码：main:src/qq_ai_bot/runtime/work_delivery.py:130–279；main:src/qq_ai_bot/runtime/work_journal.py:102–115；main:src/qq_ai_bot/social/service.py:810–900

- 证据：main:src/qq_ai_bot/social/service.py:810起退役voice检查；main:src/qq_ai_bot/runtime/work_delivery.py:130–279 旧计划；main:src/qq_ai_bot/runtime/work_journal.py:113–115；main:docs/plugin-development/api-3.2-migration.md:27–36
- caller：旧持久Work、未决Social receipt、plugin callback、Automation旧schema；不是新工具目录的普通调用
- 最小方案：采用main已做的删除和狭窄旧数据解码；新请求不含voice，旧已派发参数先核原payload并回原receipt；旧未派发语音明确结束该能力，无自动换TTS/改发文字。优先把同一读取分支收敛，不另建万能版本adapter
- 唯一owner：真实效果由原Social/Work receipt拥有；退役数据表示由现有journal decoder拥有。保留accepted/unknown，禁止decode失败推翻accepted
- 退出条件：所有可恢复旧Work/脚本/回执的受支持保留期清查结束；历史只读receipt可能长期存在，因此不能仅以“最近没调用”删读取路径。不能为退出兼容而删除用户历史
- 反例：accepted AUDIO+已删文件仍回成功；unknown AUDIO不能再发；无receipt AUDIO不能执行；旧image仅默认退役字段可读，非默认字段不被静默丢弃；ASR录音正常

### INT-03 合同版本不能回退，Code Mode与SDK分开演进

- 实施批次：P0
- 本版变化：第三轮独立复核后修正
- 唯一职责所有者：现有MainAgentContract及独立SDK、prompt和provider版本owner
- 真实调用方：主Agent、子Agent、旧journal恢复、插件manifest
- 编码前置：无
- 协调边界：改为合同设计/末尾集成验证责任；不得充当65项的统一开工许可证。SDK、layout、model profile各保留现有owner。
- 实施或发布条件：合同设计可并行；最终集成manifest/layout/profile联验；实际数据库谱系仅为相关发布前置；实际部署SHA、镜像与health command另核，不把第三方部署声明与冻结树视为同一版本
- 固定源码：Pi:src/qq_ai_bot/services/main_agent_contract.py:65–130；main:src/yuki_plugin_sdk/api.py:1–32

Pi:src/qq_ai_bot/services/main_agent_contract.py:92–119 的现有MainAgentContract包含tools、direct_names、code_api、plugin_contracts及revision；main同处保留其语音退役变更。源码没有FrozenMainAgentContract，不创建同名第二对象。不能机械选main版本13或Pi版本15覆盖另一分支，整合后使用新的未发布revision，最终数字以实施树为准。

MainAgentContract只拥有mode和有序工具声明/执行目录。提示词稳定前缀属于原PromptComposer/projection，provider属于既有executor.pin/profile_revision；Pi:src/qq_ai_bot/services/agent_runner.py:246–270 的work_contract已有profile、runtime LLM/web和system引用。沿这些owner引用稳定revision，私有Work合同加入CTX-06布局版本；公共projection只在实际持久表示或选取合同变更时升级，不把全prompt/provider复制进manifest。

采用main SDK3.2的当前严格manifest；SDK-01的LLMFacade退出使用后续独立SDK破坏性版本。主Agent合同、SDK、context布局、模型profile不强行合成一个新schema。固定声明不授权限，实际派发仍核当前授权。

此项负责集成合同设计与最终联合验收，不是其它私有删减的统一开工许可证。DEP-01、TERM-02、CTX-06和公开工具收口按共享文件协调，未知0097数据库谱系只阻塞相应迁移/发布。

验收整合后的manifest、mode/tool order、plugin指纹、send schema与语音退出、layout/profile revision一致；已提交旧tools/opaque不原地修改；未决原调用先核receipt，合法新链不改Work/预算/源身份；普通chat不被强制变为Work。

### INT-04 长Social调用键与退役voice前置顺序

- 实施批次：P0
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：Invocation完整操作身份与Social存储键
- 真实调用方：Social execute、Code子调用、退役voice回放
- 编码前置：INT-02
- 协调边界：真正需要INT-02的退役voice逻辑作为合并输入；同包原子落地亦可。
- 实施或发布条件：保留既有短键及原payload；不迁改历史效果键
- 固定源码：Pi:src/qq_ai_bot/social/source_keys.py:6–22；Pi:src/qq_ai_bot/capabilities/invocation.py:84–107；Pi:src/qq_ai_bot/social/service.py:809–921

- 证据：merge-tree的social.execute冲突：Pi:src/qq_ai_bot/social/service.py:809附近先 `replace(context, call_id=social_call_key(...))`；main:810起先识别voice旧receipt。Pi:src/qq_ai_bot/social/source_keys.py:6–22 保持≤128旧键，超长哈希；Pi:src/qq_ai_bot/capabilities/invocation.py:84–107保持≤256原调用键
- 最小方案：保留Pi键规范化，再按main逻辑读取原退役receipt与hash，只有确定是未发送的退役请求才拒绝。不能ours/theirs选择；不要把social存储键反推Work owner，也不改已有短键
- owner/不变量：Invocation持完整操作身份，Social仓库只拥有存储表示；同一父operation+子ordinal必须同键，参数改变必须idempotency_conflict，unknown仍unknown
- 退出：短旧键已持久化，保留确定性表示本身并非无谓兼容；不能为了统一格式重写效果键。验收长度边界128/129、UTF-8、父子调用、同调用不同payload、语音accepted/unknown回放

### ID-01 删除QQ正数-only门槛，收拢平台引用格式判别

- 实施批次：P0
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：OneBot平台编号语法；Social内部来源归属
- 真实调用方：reply_to_event_id、OneBot reply segment
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：有符号ASCII整数；正号是否接受需协议用例；不取消内部ID授权
- 固定源码：Pi:src/qq_ai_bot/social/service.py:640–658；Pi:src/qq_ai_bot/adapters/onebot/sender.py:118–122

- 固定证据：Pi:src/qq_ai_bot/social/service.py:640–658；Pi:src/qq_ai_bot/adapters/onebot/sender.py:118–122；M对应Social:641–659仍有相同限制。真实caller：Social send_message的reply_to_event_id（Pi:1034–1040），普通OutboundMessage.reply_to_message_id的sender。生产调查已证实负号真实输入，不是假想平台格式
- 多余分支/错误：`.isdigit()`把“-123”判为不可用，Social抛reply_event_unavailable；sender抛ValueError并包装发送失败。布尔、空串、非整数当然仍不合法；isdigit还接受部分Unicode数字而int/OneBot语法并不完全等价
- 最小删除/复用：删“必须正数”的政策，只保留OneBot有符号ASCII整数格式约束；优先复用现有adapter解析位置，小函数即可，不建跨域ID框架。Social预检可以调用同一纯解析；不得把所有字符串都int()后默默接受空白、指数、小数或bool
- 唯一owner：OneBot边界拥有平台message编号语法；Social仍拥有内部event可见性和目标归属
- 保留不变量：内部event_id必须来自context.visible_event_ids、同Conversation、同sender account/目标路由；布尔不能充当ledger整数；synthetic file:/social-operation: 不可QQ引用；不能去“找最新一条”
- 兼容/退出：无DB迁移，原QQ编号文本保持；正负号规范是否接受+号需按既有协议实现决定并固定用例，不猜测全平台支持
- 验收/反例：负整数和正整数都能生成正确reply segment；同数字不同Conversation/Presence不可用；canonical UUID、空串、小数、指数、Unicode非ASCII数字、文件占位与bool拒绝；Social预检失败必须是确知未发送，不把它存成unknown
- 依赖/风险：与工作start门禁独立，不能只改此处就宣布完整解除永久start阻塞；不得实际调用QQ做测试

### ID-02 删除旧入站旁路和第二套去重 owner

- 实施批次：P1
- 本版变化：第二轮补强原任务
- 唯一职责所有者：CanonicalIngressResolver 和 CanonicalIngressUnitOfWork
- 真实调用方：唯一产品 container 装配；Processor.handle；仓内入站 fixtures；维护循环
- 编码前置：无
- 协调边界：CanonicalIngress现已产品装配；不等主合同或DB drop。
- 实施或发布条件：运行代码退出不依赖drop旧表；历史迁移保持冻结
- 固定源码：main:src/qq_ai_bot/services/processor.py:608–699；main:src/qq_ai_bot/persistence/event_repository.py:911–953；main:src/qq_ai_bot/services/deduplication.py:19–32；main:src/qq_ai_bot/persistence/scoped_event_uow.py:800–855

#### B1. 已闭合的产品调用链

`main:src/qq_ai_bot/container.py:629` 是唯一产品 MessageProcessor 构造；构造参数同时注入 canonical_ingress/canonical_uow。另一仓内构造是 `main:tests/conftest.py:258`。`_handle_admitted` 仅由 Processor.handle 内部调用；没有另一产品/测试调用需要其 `_UNRESOLVED_ADMISSION` 二次解析入口。

当前主链是 pre_admit → canonical_uow.append_inbound/append_new_generation → canonical receipt/chat_events → admitted event id。并行旧链是：

`main:src/qq_ai_bot/services/processor.py:762` admitted=None → DeduplicationService.claim → ProcessedEventRepository.claim → processed_events → 按平台号查 ledger 尝试修缺口 → ScopedEvent.append_inbound/append_new_generation_command。

`DeduplicationService.claim` 无别的产品调用。`ProcessedEventRepository.claim` 仅该服务调用；cleanup_expired仅container的维护循环。故注入必填后不是“暂时没用”，而是**整个状态owner失去产品写入与消费**。

#### B2. 逐caller迁移表

| caller（main；Pi同符号，行号见inventory） | 替换 / 删除动作 | 为什么不新增机制 |
|---|---|---|
| `main:src/qq_ai_bot/container.py:629`、`main:tests/conftest.py:258` | canonical依赖必填；测试使用现有 napcat_registry/CanonicalIngressResolver/CanonicalIngressUnitOfWork | 产品装配已具备，测试补齐真实接纳 |
| `main:src/qq_ai_bot/services/processor.py:608–699` | pre_admit一次；删正常执行的可选判断/None旁路、`_UNRESOLVED_ADMISSION` 二次入口；失败仍是明确 drop | 唯一caller已传admitted，无需延迟解析协议 |
| `main:src/qq_ai_bot/services/processor.py:761–778,894–925` | 删旧claim/gap repair；只调canonical append/new-generation；`yuki_account_ids/primary_alias/author_kind`直接取已接纳值 | canonical receipt原子去重仍在，不能把重复当新消息 |
| `main:tests/support/main_agent_wire_cases.py:533`、`main:tests/unit/test_commands_and_chat.py:796` | 替换 scoped_events.append_inbound fixture | 不再以旧旁路验证产品主链 |
| `main:tests/unit/test_rollup_chat_wakeup_wire.py:102`、`main:tests/unit/test_plugin_facades.py:138,219` | 替换 EventLedgerRepository.append_inbound 测试入口 | 产品中此repository wrapper仅转旧writer，无新消费者 |
| `main:tests/unit/test_derived_text_transactions.py:148`、`main:tests/unit/test_conversation_rollup_370.py:1065,1068,1706`、`main:tests/unit/test_rollup_commit_snapshot.py:101` | `/new` fixtures走 canonical_uow.append_new_generation；保留ASR跨generation/并发commit断言 | 不把同事件升代幂等性删掉 |
| `main:src/qq_ai_bot/persistence/event_repository.py:314–322` | 以上测试迁完，删 append_inbound wrapper | 全产品唯一内部caller是被删除的writer转发 |
| `main:src/qq_ai_bot/persistence/scoped_event_uow.py:129–154,281–342` | 删除两个真人入口 | 保留 append_external/append/派生正文事务 |
| `application/modules/conversation.py`、`application/modules/persistence.py`、`container.py` | 删除DeduplicationService/ProcessedEventRepository bundle字段、构造、转接、cleanup条目 | 不替换为新manager |
| `main:src/qq_ai_bot/services/deduplication.py:19–32`、`main:src/qq_ai_bot/persistence/event_repository.py:911–953`、`main:src/qq_ai_bot/persistence/models.py:1723–1731` | 删除类及导出；配置TTL字段同步退出 | 原表可暂时留空壳，不要求破坏性migration才能删运行代码 |

**不要误删**：`handle`中pre_admit抛错或group recovery提前返回时，finally的诊断投影仍可能没有admitted；保留这一局部None及`_observation_canonical_refs`的无主体保护，它不再允许进入业务旧链。`build_event_key` 仍被 Processor多条命令/观测/审计路径传递（main:760,1091,1267,1509,2024），本轮保留其函数；`processed_events` 在SelfReflection Run里同名计数不是此表/服务，不能匹配名字批量删除；cleanup loop还清Web/media/emoji，不能停整个维护循环。

删除两个旧入口后，Processor的`_scoped_events`也没有真实用途（main:382,900,918,923），连同构造参数/装配一起删。`scoped_append_repairs`只有该旧gap分支写入，`main:src/qq_ai_bot/conversation/rollup/metrics.py:21`字段与`main:src/qq_ai_bot/conversation/rollup/worker.py:95`health投影一起退出，不能留永久0指标伪装恢复能力。

泛型`EventLedgerRepository.append`和ScopedEvent.append不可由此整体收紧为仅outbound：`main:src/qq_ai_bot/memory/quality/runner.py:278`还用`fixture.direction`注入隔离质量事件，另有大量测试直接使用。该fixture入口不等于线上真人接纳，需要先迁quality runner/test seed才能进一步删generic writer的inbound分支；此项本轮未升级为可直接删除。

#### B3. 附带但确定的穿透删除

`ScopedEventLedgerUnitOfWork._append_on_session`（main:800–855，56行）没有独立行为，仅原样传参给 `_append_canonical`；唯一caller为同类append的 session!=None分支:223。此caller直接调用 `_append_canonical` 后可**立即删除**56行wrapper，不需等待入站迁移，不新增适配层。借用外部session不自行commit的合同完全保留。

#### B4. Durable/public退出标准与代码规模

- canonical_events/chat_events/receipts/冻结迁移0048与后继迁移保留；旧processed_events不是效果回执，不用于恢复重发。代码退出不需要drop旧数据。
- `ProcessedEventRepository` 是Host内部导出，不是 yuki_plugin_sdk合同；仓内插件未import。若有部署定制直接import内部包，作为该定制的明确升级项，不设置永久兼容fallback。
- 删除processed_event_ttl_seconds须同步Settings domain与配置引用；已保存未知配置的加载容忍度需定向测，不删除别的保留政策。
- 可精确指认的定义体：26+62+9（EventLedger wrapper）+14+43+9=163行；加56行纯转发为219行。还有Processor分支、导出、TTL和装配净删量未计算。**219是已定位定义体毛删除，不是最终净diff**；fixture迁移及必要校验新增单列，不计“无成本净减”。
- 验收退出：src内旧类/方法零引用，SDK公开合同不变；跨Presence相同平台号、canonical duplicate、并发重复、新代次幂等、旧receipt损坏拒绝、external/outbound派生事务全部走真实owner。

### ID-03 按九个构造入口删除身份副本

- 实施批次：P2
- 本版变化：第二轮补强原任务
- 唯一职责所有者：ToolActor、已接纳事件、原 Work execution 各自拥有真实事实
- 真实调用方：chat两处、work_resume两处、subagent、automation、plugin main_turn、command_adapter、declaration_only
- 编码前置：无
- 协调边界：先在本任务迁九构造/replace链；不要求旧入站类先消失。
- 实施或发布条件：actor Person与会话target不可混同；只删除同owner的副本
- 固定源码：main:src/qq_ai_bot/services/chat.py:1240–1265；main:src/qq_ai_bot/services/chat.py:1996–2025；Pi:src/qq_ai_bot/capabilities/invocation.py:149–166；Pi:src/qq_ai_bot/services/main_agent_backend.py:717–729

`Pi:src/qq_ai_bot/capabilities/invocation.py:149–166` + 唯一构造 `main:src/qq_ai_bot/services/main_agent_backend.py:717–729`：

- `actor_user_id`、`trigger_message_id`、`provider_metadata`没有该类型产品reader。`CapabilityDescriptor.provider_metadata`是另一个类型，main:src/qq_ai_bot/capabilities/runtime.py:181的synthetic判断不能误认为此context有reader；PluginInvocation.actor_user_id也是不同类型。删三字段及唯一writer。
- `conversation_key`只有workspace/service.py:144用，改取`invocation.runtime.conversation_key`可删副本。
- `execution_id`只供execution_key属性，唯一writer等于runtime.effective_execution_id；明确ToolRuntime类型并改直接读取后删该副本和两级getattr fallback。保留missing_internal_execution_anchor语义。
- 不要求整类强行消失：InProcessToolProvider绑定仍需要ToolRuntime与operation_id，Invocation.context.runtime是AgentRuntime，二者不是可直接同一对象。保留两字段已有结构优于又新增桥接manager。
- 两个hash消费者workspace、automation/SDK无Work路径严格比对旧键字节；改字段承载不改变键格式。

#### D3. ToolRuntime九个构造点的真正迁移计划

| 产品构造（main位置；Pi见inventory） | 真实owner / 必须保留的差别 | 删除重复投影的方式 |
|---|---|---|
| `main:src/qq_ai_bot/services/chat.py:1240` | inbound + admitted turn；actor=真实入站人 | 构造时从inbound绑定既有ToolActor；turn event必须同源；actor_user/group/mention/bot/presence副本改读actor |
| `main:src/qq_ai_bot/services/chat.py:1996` | plugin external event，**无Person actor**；person_id是会话target | 不伪造ToolActor。保留canonical event/target字段，明确这是event-less-person source；不能用actor.person取代target |
| `main:src/qq_ai_bot/services/work_resume.py:285` | SELF来源恢复结果 + 原Work execution | 现有actor已完整，删同值user/group/bot/presence副本；Work execution和initiative来源仍不同 |
| `main:src/qq_ai_bot/services/work_resume.py:463` | 旧真人event + 原Work id | 从恢复结果绑定actor（execution_id保持Work id）；不以最新账号/消息重建原source |
| `main:src/qq_ai_bot/services/subagent_execution.py:283` | worker隔离执行id，读取父来源；SELF或Person | 保留worker execution与权限缩水，不继承主发送；actor一次构造，不把父execution顶替child |
| `main:src/qq_ai_bot/automation/handlers.py:207` | 无入站，Person或SELF scheduled actor；原run/step | 已有ToolActor，重复actor字段可收敛；automation_run/源scene核验仍在；memory读取target不等于发起人 |
| `main:src/qq_ai_bot/plugin_host/main_turn.py:282` | 已核验inbound；插件执行id与read grant | actor取inbound，执行id独立；read_scope/read_target_id是真授权范围，不删成普通发送场景 |
| `main:src/qq_ai_bot/plugin_host/command_adapter.py:186` | tools_closed的真实消息callback | actor取message；关闭工具和SDK invocation真实身份仍保留 |
| `main:src/qq_ai_bot/services/main_agent_contract.py:44` | declaration_only，无执行主体 | 不要求新造actor；固定manifest构建与执行检查分开 |

这些构造之外还有`dataclasses.replace`链，尤其chat的source_runtime替换和MainAgentBackend._request_runtime的权限关闭，实施须一起迁；AST构造清单不是replace覆盖率证明。

**按真实事实区分可删字段**：actor_user_id/current_group_id/mentioned_user_ids可在真实actor入口改读actor；但plugin external/declaration_only需仍允许无actor。scope_type/conversation_id/person_id/space_id不都属于actor，不应为了删字段扩大ToolActor职责。`inbound`仍承载正文、回复、附件和source provenance，不能把它“压扁到统一上下文”后重建假消息。`turn_snapshot`的generation/token和Work execution是不同生命周期，保留。

退出标准：九入口新构造都不能产生同一事实两套可写值；无event真实execution可执行；SELF不含真人；plugin external不能获得person权限；恢复不改Work/operation/source ID；读取边界与当前权限在实际执行时仍复核。达到后删effective_*优先级分支和逐次require_actor重建，不保留永久双写字段。

### ID-04 删除 synthetic scope 整数和内部影子身份链

- 实施批次：P1
- 本版变化：第二轮补强原任务
- 唯一职责所有者：canonical conversation_id 与 generation
- 真实调用方：Rollup active/settlement、DTO/repository、三处turn snapshot、通知、ContextAssembler、generation_matches、EffectGate
- 编码前置：无
- 协调边界：现有UUID与generation足够；与ID-02同文件协调，不以整套入站删除为前置。
- 实施或发布条件：内部synthetic退出无需等待公开alias ABI；旧durable字节不改
- 固定源码：main:src/qq_ai_bot/conversation/hydrate.py:307–312；main:src/qq_ai_bot/conversation/rollup/service.py:89–109；Pi:src/qq_ai_bot/conversation/rollup/service.py:89–109；main:src/qq_ai_bot/services/effect_gate.py:24–80

#### C1. 新的实证，不再只是“理论hash碰撞”

`main/Pi:src/qq_ai_bot/conversation/hydrate.py:307–312`：SHA256截断后模`2^31−1`再用1替0。已用两个合法UUID触发同值301662140：

- `00000000-0000-4000-8000-0000000000fc`
- `00000000-0000-4000-8000-000000003521`

离线探针调用**真实** `ConversationRollupService.summarize_candidate`，第一会话fake模型等待时，第二会话因同`(scope_id,generation)`抛 `rollup_scope_already_executing`。main/Pi均复现。`ensure_required_coverage:398–399`的查询同样命中第一会话模型；完整取消路径本探针没跑，故只报查找同owner及源码上的错误取消风险，不冒充真实生产取消。

这直接否定了把synthetic视为无害显示值的判断。换更长hash不是方案，canonical UUID已是唯一owner。

#### C2. 完整迁移闭包

| 生产消费者 | 现存权威替代 | 可删表示 |
|---|---|---|
| `main:src/qq_ai_bot/conversation/rollup/service.py:89–90,109,398–399` 与 `main:src/qq_ai_bot/conversation/rollup/worker.py:145` 的active/settlement键 | Claim/Candidate已有conversation_id；状态读取携带同canonical id | synthetic键；两dict键改为`(conversation_id,generation)`，不新增锁/owner表 |
| `conversation/rollup/models.py` 的ScopeState.id、RollupState.scope_id、Claim.scope_id、Candidate.scope_id | Claim/Candidate本来就有conversation_id；ScopeState/RollupState改携实际id | 4种整数投影；optional conversation_id变为canonical运行必须有值，旧test fixture迁移 |
| `main:src/qq_ai_bot/conversation/rollup/repository.py:1094,1131,1279,1767` | 相邻代码已持有conversation.id/row.conversation_id | 删除所有synthetic构造；overlay构造不再接scope整数 |
| 同文件:763,1359,1367,1505,1675,1747 | 结果projection用原claim/candidate canonical id | 只拷贝scope整数的流水参数 |
| `ConversationTurnSnapshot`（`main:src/qq_ai_bot/conversation/scope.py:11–100`） | 新快照字段承载已接纳canonical Conversation id；generation/token仍保留 | scope_id正数校验和相等校验；不删除generation/coordinator fence |
| 快照3个产品构造：`main:src/qq_ai_bot/services/processor.py:937`、`main:src/qq_ai_bot/services/work_resume.py:202`、`main:src/qq_ai_bot/plugin_host/background_turns.py:317` | message.conversation_id / recovered.conversation_id / QueuedCanonicalContext.conversation_id | 注入synthetic，要求非空实际id |
| `main:src/qq_ai_bot/plugin_host/notification_repository.py:1095`及QueuedCanonicalContext.scope_id | 同对象已有conversation_id | synthetic字段与搬运 |
| `main:src/qq_ai_bot/services/context_assembler.py:386,946,1142,1730,2010`、rollup repository:1174 | 实际canonical id+generation核验 | prompt_scope_id与scope_id匹配参数；retain transport来源复核直到其真实授权caller迁完 |
| `services/chat.py`与`processor.py`调用`generation_matches` | scope repository直接按canonical id读generation | `generation_matches(scope_id,...)`的无用首参（函数现已`del scope_id`）；内部通过alias反查再找owner可退出 |
| `main:src/qq_ai_bot/services/effect_gate.py:24–28,75–80` | permit调用者只使用async-with临界区，没有读取返回EffectPermit | **直接删EffectPermit/uuid及yield值**，保留lock内validate；不是删除效果闸 |
| `main:src/qq_ai_bot/services/prompt_composer.py:368–376` | prompt snapshot用实际conversation id | scope_id摘要字段退出新写；旧诊断只读，不改历史hash判定 |

注意：当前 EffectGate锁按**scope_key**，不是scope_id；不能宣称上面已复现“两个普通聊天共锁”。本反例是Rollup active/settlement。更不能仅凭整数碰撞删除其他并发隔离。

#### C3. 为何本包不必等SDK大迁移

synthetic数是运行投影，不是canonical数据库主键；canonical Rollup表与claim SQL已经以conversation_id为键。故删上述整数无需重写历史执行ID或drop alias表。snapshot的event/initiative二选一、generation、coordinator_version保留。

别名退出另分任务：`runtime_conversation_key`目前兼顾coordinator和SDK的固定primary alias。SDK `conversation_key`、Work source/journal、Memory receipt以及workspace request键均有真实/持久消费者。可以先让内部锁与turn读UUID，但不能同时把旧source_key/receipt key重写成UUID而使恢复认为新操作。

**本轮裁决**：synthetic整数链“先迁上述真实caller后全删”；EffectPermit返回对象与generation_matches无消费参数“直接删”；SDK alias与旧durable字节“确需保留到独立ABI/reader退出”。不再将三个难度混成一个无限期候选。

规模：核心hash函数6行、EffectPermit5行+构造6行可明确删除；主要收益是8类DTO/调用处不再携带错误身份，字段替换不是可虚报的净删行。暂不给synthetic整包净行数。所有目标行已在inventory定位，最后以净diff列新增/删除。

### ID-05 删Social文件分支的重复回放预检，保留唯一早返回与原子claim

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：SocialOperationRepository及统一回放入口
- 真实调用方：send_message的文本、image、file分支
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：入口PREPARED之后的并发完成/文件失效反例先通过
- 固定源码：Pi:src/qq_ai_bot/social/service.py:902–921；Pi:src/qq_ai_bot/social/service.py:1057–1074

- 证据：Pi:src/qq_ai_bot/social/service.py:902–921对所有send_message提前读取非PREPARED receipt、核hash并回结果；:1057–1074 artifact分支再做几乎相同读取；:_effect 1317–1332/1444–1455还需处理并发赢家。main:908附近与1055附近也保留双预检
- caller/数据：文本、image、file send_message；第二预检原本为了过期artifact不应阻止已送达回放。入口早返回已承载同一目的，但并发receipt状态可能在await后变化
- 最小方案：保留入口统一回放、删除artifact专属重复块；原子prepare/claim兜底不能删。实施前用并发测试检查在入口读PREPARED、另一个执行完成、当前artifact已失效时能否正确回receipt；如失败，只把现有统一回放放到必要位置，不新增平行replay框架
- owner：SocialOperationRepository持有receipt和payload一致性；SocialService一个共用回放入口负责展示；参数/route/artifact准备不再各造回放协议
- 异常影响：已送达回放不应因目标改路由/删除文件/联系人停用变成可重试失败；payload变更仍抛idempotency_conflict
- 兼容/退出：不改schema/效果键；通过所有成功/unknown/并发回放后直接删，无长期双读开关
- 验收：已送达file+caption分别核receipt不重传；已删artifact仍回原发送事实；参数修改失败；prepared并发只一claim赢家；unknown任何路径不重发。风险中，需与main retired voice早分支一起收敛

### ID-06 缩小“所有发送异常=uncertain”的范围，只在真实可能派发后保留未知（有条件修正）

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：Social真实dispatch边界及原receipt
- 真实调用方：Social claim→connection check→gateway call→ledger
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：必须证明失败确在出网前；历史unknown不重新分类
- 固定源码：Pi:src/qq_ai_bot/social/service.py:1457–1529；Pi:src/qq_ai_bot/adapters/onebot/sender.py:95–104

- 证据：Pi:src/qq_ai_bot/social/service.py:1457–1529中try首行validate_prepared_connection，随后_call、解析、落账，任何BaseException都finish UNCERTAIN。两次connection检查与claim间可发生连接变化；第二检查在网络调用之前失败仍归unknown。Pi:src/qq_ai_bot/adapters/onebot/sender.py:95–104已有dispatched分类且只在未派发同Presence换连接；main:src/qq_ai_bot/runtime/work_delivery.py:76–108已区分dispatched真假
- 具体多余负担：明确没有进入网络调用也被锁成unknown，不是“安全性越多越好”；但超时/断连、发送成功而落账失败确实必须unknown
- 最小方案：复用已有dispatch边界/错误字段，在明确pre-dispatch失败路径记录FAILED/未执行，而不是外加通用错误层；真实网络调用前设派发标识，所有边界不明仍unknown。不能依据异常类型名字猜未发送
- owner：Social effects owns dispatch boundary；Connection检查负责当前接入有效性；Work据真实outcome决定是否可修正，不反查平台找“有没有发”
- 不变量：claim先于外部效果；原效果ID不重建；网络调用后任何结果不明不自动重试；确认成功后通知/诊断失败不降级。取消异常仍传播
- 兼容退出：仅改变新发生且证明未派发的分类；历史unknown不批量改failed，不根据当前没有平台消息推断旧未发送
- 验收：claim后网关预检失败无call且failed；_call开始后抛错unknown；成功但SQLite commit失败unknown；成功落账后notify失败仍succeeded；CancelledError在前后边界分类各自正确
- 风险/依赖：中高；先核_call及gateway是否可能在raise前已经出网，此处仅静态定位，不宣称全调用链证明完毕

### ID-07 保留canonical回复三值判别，删除只限“不再可达”的入站前fallback

- 实施批次：P2
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：Ingress canonical回复解析；Responder触发策略
- 真实调用方：入站回应策略与未落库历史引用
- 编码前置：ID-02
- 协调边界：其拟删fallback可达性结论确实要在ID-02强制canonical接纳后重核。
- 实施或发布条件：无本地引用是否回应属于产品策略；不改变False明确拒绝
- 固定源码：Pi:src/qq_ai_bot/identity/ingress.py:341–379；Pi:src/qq_ai_bot/domain/messages.py:177–188

- 证据：Pi:src/qq_ai_bot/identity/ingress.py:341–379输出true/false/None；Pi:src/qq_ai_bot/domain/messages.py:177–188 canonical false阻止账号fallback，None才允许reply_sender_user_id/yuki_account_ids。真实caller在policy分类，部分消息在完整入账之前需要判断是否回应
- 候选而非立即删除：`None`并非“兼容旧身份”的同义词，也代表引用历史在本地不存在。若删除所有fallback会改变未存储历史回复的回应策略；若把False也fallback则可把ambiguous/真人引用错误认成Yuki
- 最小方案：ID-02完成后确认所有生产policy在canonical pre-admit之后，删只服务旧无canonical注入测试的fallback场景；保留产品确实需要的“无本地证据”接入提示，明确它只决定回应触发，不能作为Person/Conversation/权限/已送达证明。不增加新的身份恢复服务
- owner：IngressResolver解析本地canonical回复；Responder policy只做交互触发策略。未知可不回应，是产品选择；不能改成最新消息搜索
- 退出/验收：确认未落库引用产品预期，再更新测试；双keeper必须false/ambiguous；唯一真人keeper不能被平台self声称覆盖；唯一Yuki keeper取其内部id；无历史引用不得产生伪internal id。风险中，涉及用户体验而非删除安全护栏

### ID-09 迁当前执行账号并删除旧账号永久存活门

- 实施批次：P2
- 本版变化：第二轮补强原任务
- 唯一职责所有者：Automation canonical creator与原DelegatedAuthority
- 真实调用方：管理、执行、creator模板与活动route解析
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：历史creator模板和原grants保留；明确改收件人需业务决定
- 固定源码：Pi:src/qq_ai_bot/automation/service.py:897–945；Pi:src/qq_ai_bot/automation/executor.py:886–929；Pi:src/qq_ai_bot/automation/authority.py:21–37；Pi:src/qq_ai_bot/automation/control_context.py:37–141

#### 已确认的矛盾

`main:src/qq_ai_bot/automation/service.py:799–823`创建按Person所有active账号算permission；管理`:897–945`要求原creator_user_id仍active、只以原号算permission。`main:src/qq_ai_bot/automation/executor.py:886–929`重复同门。canonical Person仍是owner时，合法换绑定会blocked，不是owner真的转移。`main:src/qq_ai_bot/automation/control_context.py:37–141`已有可信owner/scene/route解析，可复用，不建第二身份服务。

#### 为什么不能“改成当前账号”后立即合并

完整消费者现已定位：

1. `main:src/qq_ai_bot/automation/executor.py:194–224`同时把旧号写入AuthorityContext与`$creator_user_id`模板；`:406`继续进入CapabilityExecutionContext。
2. `main:src/qq_ai_bot/automation/handlers.py:119,125,214,217,234,248,333,401,425,430,482`用creator做配置/时区、ToolActor、sandbox source、非管理员只读target限制。
3. `main:src/qq_ai_bot/plugin_host/automation_adapter.py:179–217`对creator与AuthorityContext、delegation bot/group做匹配；`main:src/qq_ai_bot/plugin_host/facades.py:737–753`继续按delegated.creator_user_id与invocation.actor_user_id比较，换号不迁此处会拒绝或错误解释超级管理员来源。
4. `main:src/qq_ai_bot/automation/validator.py:255–261,331–344`将`$creator_user_id`/current_speaker和静态投递目标绑定创建者。模板不是纯日志字段，不能改已有脚本收件人。
5. `automation/executor.py:_begin_execution:738–797`取当前permission的allowed；`execute`再读旧DelegatedAuthority。不能看到当前账号是superuser就把actor_is_superuser直接透传为原授权扩大；原grant和当前审批仍须交集。

#### 可直接写入任务书的步骤

- 沿现有ControlAutomationContext取得当次可信owner route与permission，私聊/群路由歧义照旧拒绝；只用于本次执行身份，不回写历史creator_user_id、created_from_message_id、source event或旧run。
- 在现有CapabilityExecutionContext/AuthorityContext内明确“当前执行账号”与“原creator/template事实”；不用新增万能context层。Agent/Plugin/tool权限消费当前执行身份，并继续核原canonical Person及grant上限；模板`$creator_user_id`仍是原存量语义，除非用户按原编辑流程明确改脚本。
- Plugin facade不再以旧账号字符串相等冒充canonical owner相等，但保留原delegation、schema、插件批准和canonical owner验证；QQ相关配置/时区究竟按owner还是按当前绑定，沿既有canonical service入口，不借operator身份。
- 上述真实caller迁完，删除management/executor两处“原号必须仍active”和重复单账号权限计算。Person disabled/无active binding/scene generation失效/route歧义仍blocked。

**裁决**：不是立即机械删三行；是“先迁一个完整执行上下文链后删两门”，无需等待所有SDK字段退役。原DelegatedAuthority与模板字段是旧durable reader，确需保留。当前查证未跑换绑定SQLite回放，权限扩大反例需定向加测，不能宣称通过。

验收：A→B同Person可继续；A重绑他人不转任务；管理者C不接管owner；B变superuser不得扩大旧grants；显式模板A不静默发B；当前权限撤销仍停；原未知效果不重发；静态、agent、plugin三种路径分别覆盖。

### RUN-01 删除 mandatory start 门禁，不再让“先汇报”成为工作许可证

- 实施批次：P0
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：既有授权、预算、效果回执；交流由当前任务指令决定
- 真实调用方：AgentRunner直调/Code/control与TurnExecution
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：允许删除通用礼仪门；用户明确先确认的要求继续有效
- 固定源码：Pi:src/qq_ai_bot/services/work_reporting.py:102–164；Pi:src/qq_ai_bot/services/agent_runner.py:1170–1185；Pi:src/qq_ai_bot/runtime/work_control.py:336–341

**真实链**：`AgentRunner._execute_tool_batch_impl:1176` / `_execute_control_call:946` / `_code_host:891–892` → `work_reporting.before_work_tool:102–136` → 没有 start 回执就返回未执行 → `TurnExecution.finish_tool_turn:1814` → `start_feedback_updates:139–164` 首次写 `start_feedback_given`，第二次抛 `WorkNoProgress(work_start_not_delivered)` → Runner 527 → supervisor 97/151 将整个 Work suspended 并计划 notice。读取、子任务派发同样被礼仪门阻断。

**删除对象**：整段 before_work_tool；start_feedback_updates；所有上述接线、start_feedback_given 新写入、对应 WorkNoProgress 文案与 allowlist 项；Code contract 中这两个 start error 的关闭映射。删除为“开始说明”保留的 `validate_work_report:336–341` 唯一性硬门及 `update:829–834` 内 start 派生检查，不再增设新 start 分类器。交流机会继续走既有非阻断 stage/input 提示，若模型无需再报告，不建补偿任务。

**剩余 owner**：真实权限/预算/unknown 仍由 InvocationService、WorkSession、领域执行器核验；是否/何时说话由模型和用户指令决定。保留 report→原 Work/事件/目标关联核验及 Social 真实发送回执，不能用“start 未确认不阻断读”放行未知发送后的其他副作用。

**兼容**：旧 checkpoint 的 `start_feedback_given` 可忽略读，停止写；不为删 JSON 派生键新增迁移。已 suspended 的旧任务不会因升级自动重跑；用户/原授权恢复入口接续原 Work。已未知的 start 发送仍保留原 effect，不能发一条新 start 当重试。

**验收**：interactive 无 start 时首次只读/已授权业务/子任务按原权限执行；模型选择发 start 时仍只有原调用一次效果；原发送 unknown 不能借此绕过 generic fence；停止/取消/新输入场景均不创建开始报告补偿状态；恢复读旧标志不再暂停。

### RUN-02 删除等待条件中的聊天正文副本

- 实施批次：P0
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：chat_events正文；wait条件仅引用事实
- 真实调用方：WorkWait match_event、普通入站、插件通知事务
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：呈现同时迁账本读面；隐私删源不回填旧text
- 固定源码：Pi:src/qq_ai_bot/runtime/work_wait.py:491–514；Pi:src/qq_ai_bot/runtime/work_wait.py:320–345

**真实链/证据**：`WorkWaitRepository.match_event:491–495` 将 `event.content[:7000]` 放进每个 matched；同文件 `_deliver:337`、部分 all 命中 `:514` 再以 `bounded_json(...,8192)` 保存。普通消息入口 `Pi:src/qq_ai_bot/services/chat.py:843–849` 在 stage_work_input 直接调用，无局部降级；插件 `Pi:src/qq_ai_bot/plugin_host/notification_repository.py:462–466` 在原事件发布事务内调用。独立 SQLite 探针：1 个合法 conversation 条件、3000 中文字符消息已入账；match_event 抛 ValueError，wait=active、Work=waiting_external、Work inputs=0。多条 all 或一条事件命中多个条件也放大复制。

**删除对象**：matched 中 text；Work signal payload 内再嵌入同正文的存储责任。不要把 8192 改成另一个更大常数，不引入 SignalTextStore。matched 仅存原 event_id、kind、实际匹配时间/原 run 状态等事实；呈现时沿既有账本/输入准备路径按当前授权读取原事件。多条件命中同事件，只引用一次正文。

**剩余 owner**：原 `chat_events` 是正文真源；waits 只持有一次性条件与命中；inputs 是一次性恢复信号。原三者的投递/入队同事务、source key 去重、generation/主体/插件授权保留。

**兼容**：旧带 text 的 conditions 仍可读取但新写不复制；不能在读不到已隐私删除事件时复活旧 text。失效源只保留“原事件正文不可读”的有界事实，不猜授权、不发第二个 Agent 轮。

**验收**：3000/7000 中文、8 条 any/all、多条件同事件、重复事件、部分满足重启、隐私删除、插件事务 rollback/retry、外部 event 不双开工作；Work ID/预算不变。增加条件结构上限测量，但容量只限制新增条件元数据，不限制已合法入账正文的唤醒。

**依赖/规模**：需与 context/input owner 一起改呈现，主体/插件入口负责人复核；删存储副本，净行数不作虚估（必要正文读取还要对接）。

### RUN-03 整删 Work 展示计量的强制生命周期

- 实施批次：P1
- 本版变化：第三轮区分展示writer与失租结算缺口
- 唯一职责所有者：Work激活负责settle；原Work budget负责费用；旧active_seconds仅展示读取
- 真实调用方：activation/root/resume/context preparation/heartbeat→WorkControl→checkpoint；UI读旧列
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：Automation cursor active_seconds参与真实timeout，完整保留
- 固定源码：main:src/qq_ai_bot/runtime/work_activation.py:135–213；Pi:src/qq_ai_bot/runtime/work_activation.py:135–213；main:src/qq_ai_bot/runtime/lease_heartbeat.py:18–83；main:src/qq_ai_bot/runtime/work_repository.py:553–617

owner：Runtime激活/持久展示。删 `WorkControl.meter_active_time`及metered_at，仅保留旧列读；删 activation `meter_active_time`参数及root/resume caller接线、心跳meter参数/调用/`work_heartbeat_meter_failed`错误码、context-preparation强制调用，删 checkpoint的active_seconds增量参数及SQL增量写（迁完下列唯一caller后）。

`main/Pi:src/qq_ai_bot/runtime/work_control.py:344–351`，`main:src/qq_ai_bot/runtime/work_activation.py:99,141,173,187–188`，`main:src/qq_ai_bot/services/work_resume.py:122`，`main:src/qq_ai_bot/runtime/context_preparation.py:130`，`main:src/qq_ai_bot/runtime/lease_heartbeat.py:18–83`，`main:src/qq_ai_bot/runtime/work_repository.py:553–556,603`。读取仅 `main:src/qq_ai_bot/persistence/control_work_query.py:198`、`main:src/qq_ai_bot/persistence/control_activity_query.py:531`，schema guard/表定义保留兼容；可显示历史累计/统计不完整。不增加新计时队列、重试表或迁移擦旧数。

**新反例**：正常体已经 return confirmed-result，finally有效lease仍先 await meter。OSError不在SQLAlchemyError/WorkConflict catch中，覆盖return；SQLAlchemyError进入catch后跳过settle并release，调用方得到成功但状态仍running。这证明“给meter加catch”仍可能跳过settle；直接删除这段额外writer才消除两种失败。

**严格排除**：`main/Pi:src/qq_ai_bot/automation/executor.py:254,272,365–369` 的cursor active_seconds。它跨激活累计并从script timeout扣除，必须持久化；只有 uses_runtime_budget 时 timeout=None，不能因此删其它DSL路径。root/automation budget计数、未知commit不退款也不删。

验收：成功体+展示计量故障不得影响settle/return；取消+真实lease失败仍阻断；Automation两次恢复后的剩余timeout=原timeout−累计活跃时间，不能重置。升级不改原budget/effect/journal key。

失租的WorkRecoveryDeferred未落账另由RUN-06收口；删除meter可减writer但不能代替该修复。原租约SQL-time/fence校验仍必需，不给旧owner补写权。


### RUN-05 迁现有终态入口并删除礼仪恢复和重复止损分叉

- 实施批次：P2
- 本版变化：第三轮补齐礼仪链最后消费者
- 唯一职责所有者：WorkControl一次裁决和supervisor.settle；有限累计model预算或唯一窄止损
- 真实调用方：internal-final、root/child wait/need_input、finite与NULL model_limit Work、普通chat
- 编码前置：RUN-06
- 协调边界：统一完成判定由RUN-06产出；RUN-01开始门删除并非结束门删除的代码前置。
- 实施或发布条件：NULL model_limit且无真实等待的Work保留一个窄付费止损；不得改成yield自排队
- 固定源码：Pi:src/qq_ai_bot/services/turn_execution.py:1493–1546；Pi:src/qq_ai_bot/services/turn_execution.py:1896–1919；Pi:src/qq_ai_bot/runtime/work_supervisor.py:260–290

#### R5-A 内部final/礼仪模板分支：迁入现有一次终态判定后删

`Pi:src/qq_ai_bot/services/turn_execution.py:1467–1547`；`main:src/qq_ai_bot/services/agent_runner.py:1635–1715`：mention占位纠正、interactive exit、response_feedback、empty-final自造次数/补问；其中interaction `Pi:src/qq_ai_bot/services/work_reporting.py:236–261`以final_feedback_given跨段一次机会判WorkNoProgress。删除该函数、接线、communication键allowlist（`Pi:src/qq_ai_bot/runtime/work_repository.py:617`）、activation_outcome错误理由及暂停文案。旧JSON字段忽略读，不做数据清洗迁移。

**替代owner不是新manager**：已有 `WorkControl._control(action=complete)`及`work_supervisor.settle`。内部final到达时：先处理真实pending输入；按当前children/unknown/delivery事实判一次完成；满足则完成，不满足且存在owned pending则waiting_external；模型已明确need_input/wait则原状态；其它情况End并让现有无ending→suspended结算。不要把完成失败receipt继续无限喂回模型，不把内部final自动发QQ，不猜“需要用户回答”的问题，不新造wait条件。

**为什么现在可落地**：新探针证明existing settle(None, no yield)就是suspended而无recover_failure/新notice；无需WorkNoProgress持久episode及一轮“改口”才能停住。完成判定尚需按RUN-06收敛，所以分类“迁一个终态入口后删”，不是先删再任其Continue。保留真实final receipt判据；今后是否取消interactive final标签是产品策略改变，不能把任意start/progress送达当最终完成。

mention占位不能在**发送边界**放行；但这里检查的是不会自动发送的内部final，撤掉重试/异常不增加QQ副作用。contains_internal_capability_payload仅有两处内部反馈的字符串marker读者，随该链删除；不虚构新的发送安全分类器。真实permission catalog、当前授权和出站消息验证保留。SELF NO_REPLY、普通chat silent-final照原合同End，返回调用方completion_payload仍以caller合同校验。

#### R5-B 已有 wait/need_input：迁root caller一个分支即可删后续启发式

`Pi:src/qq_ai_bot/services/turn_execution.py:1850–1869`目前只有child/return_to_caller在成功wait/need_input后立即End；普通root会继续循环。`main/Pi:src/qq_ai_bot/runtime/work_control.py:855–932`已有完整注册条件/owned-run/明确reason验证。

让所有**已成功paired且 ending为waiting_user/waiting_external** 的caller在该处结束激活（后来的pending输入仍由最终CAS保活）。这样不用再等空final、重复结果或礼仪规则决定“是不是该停”。wait必须来自模型明确控制或持久owned execution，不能因相同只读结果自动安排时间轮询。`Pi:src/qq_ai_bot/runtime/work_wait.py:525–580`只认原发送问题的原人物回复；`:337–388`命中一次信号才queued，`Pi:src/qq_ai_bot/runtime/work_repository.py:1167–1187`ready输入才唤醒。

time wait不是天然止损：模型若不断登记“马上唤醒”，仍可付费自排队；没有有限累计预算或用户明确反复调度授权时，不能把重复检测替换成周期time wait。

#### 新输入边界另见 RUN-11

#### R5-D 有限累计model budget：明确可替换的范围

`main:src/qq_ai_bot/runtime/work_budget.py:15–105；Pi:src/qq_ai_bot/runtime/work_budget.py:15–103`每真实模型request都扣root，worker也共享；finite **model_limit**能给“模型不调用工具/只返回拒绝/反复同结果”提供跨激活终点。仅finite tool_limit不足：控制调用或无tool模型仍能付费，段max_model_requests亦不足（`Pi:src/qq_ai_bot/services/turn_execution.py:1940–1957`段耗尽即yield queued）。

在**原持久root或automation-run model_limit确为有限**且所有物理retry/compaction计费保持的路径，可直接删除repeated fingerprint硬停，预算owner接管；不允许把NULL偷偷改默认数/恢复时补限额来宣称删除。当前新budget insert两限额均NULL（Pi:src/qq_ai_bot/runtime/work_budget.py:40–41,83）；旧migration保留有限值，不代表新任务有总上限。产品若明确新任务有限预算，随后才可整删跨段repeats持久字段。

#### R5-E 无有限累计模型预算且没有真实wait/new-input停止条件：保留一个窄止损

此处不能现在删除：`Pi:src/qq_ai_bot/services/turn_execution.py:1896–1903`的无限循环停止作用。将其限制为当前输入epoch的连续、非pending同结果；已有pending结果本来被`:1809–1810`排除，不能把它再计作可删除收益。保留不等于继续保留start/final礼仪错误族、整套no_progress_recovery及多种额外LLM纠正轮。

无Work普通chat已有单activation max_model_requests终点，`no_progress_recovery`和stop_before_tools可删除，只用该现有有限段预算；这与Work耗尽后queued的行为不同。长期无限Work在删掉有限/等待等caller后，只留此一个真实付费自排队保护。全部caller都迁完前，WorkNoProgress类及`repeated_tool_results`理由仍有真实读者，不能宣称整类已死。

#### 收齐实际礼仪链的最后消费者

删除Pi:src/qq_ai_bot/services/main_agent_backend.py:992–1065 的response_feedback及其默认/委托签名；连同_unsent_final_feedback_count、_send_message_attempted的save/set/restore、_capability_was_used及仅供它们使用的marker helper一起退出。删除UnsentFinalResponseError及processor/activation_outcome旧异常分支。has_visible_effects仍有其它真实读者、SELF-tail/finalize和真实发送记账保留。普通chat的stop_before_tools还须删除Pi:src/qq_ai_bot/agent_core/loop.py:109–112、Pi:src/qq_ai_bot/agent_core/model_boundary.py:66及fixture adapter接线。原持久暂停notice已经保存最终文本，不为删除异常构造器重写旧recovery JSON。


### RUN-06 统一完成与失败退出结算并保留有效租约 CAS

- 实施批次：P1
- 本版变化：第三轮补齐失租后有效owner结算，仍复用原恢复行
- 唯一职责所有者：现有WorkControl完成裁决与WorkRepository最终CAS
- 真实调用方：内部final、显式complete、caller恢复、supervisor
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：跨await的新输入/子结果/取消仍重核；不增CompletionManager
- 固定源码：Pi:src/qq_ai_bot/services/turn_execution.py:412–446；Pi:src/qq_ai_bot/runtime/work_control.py:933–962；Pi:src/qq_ai_bot/runtime/work_repository.py:473–507

**重复链**：`TurnExecution.revalidate_caller_completion:412–446`、take_boundary_inputs `:500–529`、空响应 `:801–819`、settle_final `:1444–1464` 重复 complete 提议/清理；final `:1554–1576` 先 reconcile→background→refresh→两次 unresolved，再 `_control(complete):933–945` 再扫孩子/reconcile/unresolved/facts，退出 `work_supervisor.settle:260–290` 再 refresh/background，失败 `recover_failure:114–122` 再判。`WorkControl.reconcile_completed_children:491` 已 refresh，而 `WorkSession.restore:400–401` 马上又 refresh。

**删除对象**：Runner/TurnExecution 中重建完整完成条件的代码；恢复时的 caller_completion_pending_result 保留一次原提议读入，统一调用现有 WorkControl 的同一 complete 判定；删除无消费的 refresh、重复 children终态过滤（unfinished 已在SQL精确过滤）和重复 JSON outcome 推断。不是加 CompletionManager。WorkControl 返回现有类型化结论/原 receipt，TurnExecution仅决定 next request/end；supervisor按已判定事实完成提交；最终writer只核必要当前SQL事实，不把全部完成判定、历史JSON/文件准备或跨域扫描塞进锁内。当前transition并未原子重核所有child/effect，不能超额宣称已有保证。

**不可删的复核**：UI/模型预判不是 writer authority。`WorkRepository.transition:473–494` 检查后来 pending输入必须留；新子结果/取消/source变更跨 await仍需重核。避免把“读一次”实现成跨模型 await缓存事实。`ending`、`final_delivery`、`completion_delivered` 三状态逐消费者归并：completion_delivered目前artifact分支只被测试读，answer非interactive同样不参与决策，可局部变量化/删无用查询，但 interactive真实final回执判据若按产品要求保留必须继续读取持久事实。

**验收**：相同 complete 入口（显式、内部final、caller恢复）对全部事实给同结果；pending在空读后到达仍保留；artifact+caption异目标、SELF NO_REPLY、return_to_caller、未决child、unknown、控制停止恢复均保留。查询计数应下降，但不以少SQL取代正确性。

#### 失租退出必须交回有效 owner

main/Pi:src/qq_ai_bot/services/work_resume.py:92–100 将WorkRecoveryDeferred与已结算WorkActivationHandled一同return。失租围栏正确拒旧owner写入，但尚未落账的running又被scheduler接管。两树真实SQLite scheduler→resumer→activation探针连续5次得到5个owner，Work仍running、无recovery记录、模型/工具预算均0。这是未结算热循环，不是5次付费模型调用。

删除deferred与handled共用的“已完成”分支。激活已退出且原lease失效时，交现有WorkResumer准备失败入口，以重新取得的有效lease复用原supervisor recovery结算；旧controller不能写。写前核原Work/generation、非终态、当前revision、失败activation/fence及是否已被替代/结算，事务内_assert_lease不动；取消、新输入/新进展、generation改变或终态不能被旧失败覆盖。用既有activation/revision/fence事实核同失败一次，不建RecoveryManager、队列或第二episode表。

进程重启若原异常身份只在栈内已丢失，不能伪称知道原错或“没有发生效果”。明确现有orphan-running接管入口，以原journal/receipt/执行状态保守结算并用同SQL CAS；只修catch不能声称跨进程闭环。未知或accepted效果、原operation/Work/投递ID、root/child预算均保留，新owner只结算，不启动模型/工具或重发通知。

默认保留原分类：heartbeat lease expired非retryable，新有效owner的组件探针为suspended/attempt1；gateway_disconnected仍沿原2/10/30秒和第四次暂停。不能把所有WorkConflict改可重试，也不能重置旧attempts。meter删除可减少触发面，但不会单独补上结算缺口。

目标验收：旧owner写/续租全拒；竞争新owner仅当前fence可写；同失败重复不双增attempt；晚到输入/取消/新generation与新进展不被覆盖；退出前后崩溃；带accepted/unknown/terminal-run和非零预算；新owner再次失租/BUSY不假报成功。现状及组件probe不代表目标竞态修复完成。


### RUN-07 复用恢复输入并修正来源指纹

- 实施批次：P2
- 本版变化：第三轮复核来源指纹原语与准备重复
- 唯一职责所有者：ProtocolRecoveryPreparation.snapshot与WorkSession
- 真实调用方：单次activation预选与正式restore
- 编码前置：无
- 协调边界：复用现有ProtocolRecoveryPreparation.snapshot，容量重构不是前置。
- 实施或发布条件：复用不可变数据而非缓存授权；派发前fresh guard保留
- 固定源码：Pi:src/qq_ai_bot/runtime/context_preparation.py:53–82；Pi:src/qq_ai_bot/runtime/work_session.py:150–168

**真实链**：`context_preparation.select_protocol_recovery:53–82` 先 `WorkJournal.load` 文件hydrate+source guard；ContextAssembler `_protocol_recovery_context:232–267`只消费guard/read_version；随后 `TurnExecution.activate:333–338` 创建 WorkSession，`restore:150–166` 重读来源并再次journal.load。相同一次激活可重复读文件、JSON和实际读集，但没有把第一份snapshot传给最终恢复owner。

**删除对象**：无消费的第一次完整解码或第二次相同内容hydrate；复用现有 `ProtocolRecoveryPreparation.snapshot`，让同一 activation 的 WorkSession接管它，而非新 cache/service。准备snapshot不是授权，原当前权限/lease/source/privacy检查仍在真实dispatch和writer发布点执行；中间源变化须撤销候选，不以“已经读过”跳过。

**验收**：exact dispatched、paid compaction、provider pause、root paired rebase、child long history各只选一个恢复输入；介于预选和dispatch的generation/privacy变化仍拒绝；无guard的旧记录仍保守，不用当前fresh guard为旧签名背书。与CTX-04协同实施，避免重复改恢复输入。

#### 完整来源指纹与同一准备范围

main/Pi:src/qq_ai_bot/runtime/work_source_guard.py:113–124,145–152 读取选中event IDs的整行，不是全库扫描；main/Pi:src/qq_ai_bot/runtime/work_source_guard.py:162,255–281 使用repr(Row)及含Row集合的repr作hash。独立SQLite/SQLAlchemy2.0.51探针中，1001字符只改中间X/Y，Row值不同而repr及两种现行hash相同。只能证明编码原语缺陷，未复现完整授权穿透或第三方案例现场延迟根因。

用明确字段次序、类型与完整值的确定性序列化替代repr；第一步保持现有全部依赖，不偷缩成正文hash。已有journal存旧guard指纹，沿现有snapshot/contract边界窄格式演进，不能把旧值标成新算法或失败后宽松放行。旧hash无法证明的场景遵守原source revision/隐私，必要时合法换链；原效果与预算不改。

同activation可复用不逸出的immutable选中读集/已编码结果，删相同snapshot下无消费的再次hydrate/deepcopy/serialize。真实dispatch与writer的source/privacy/generation/lease/CAS仍新鲜；全部依赖版本推进未逐writer证明前，不能加scalar revision、TTL或跨tick缓存授权。验长字段中部/类型/引用/媒体/owner/隐私变化、无关新增事件、旧guard格式恢复和CAS失败零发布。


### RUN-08 删除无消费接口和 receipt_key 恒等转换

- 实施批次：P2
- 本版变化：第二轮补强原任务
- 唯一职责所有者：WorkSession admission、原effect/journal
- 真实调用方：WorkControl、checkpoint、receipt_key、InvocationService
- 编码前置：无
- 子项前置：仅删除restart signal链需API-02
- 协调边界：整包内restart信号清除依赖API-02删除设置者；receipt_key/charge_tools等独立子项立即可做。
- 实施或发布条件：receipt_key三个活caller迁operation_id后直接删；历史ID字节不改
- 固定源码：Pi:src/qq_ai_bot/runtime/work_session.py:1378–1388；Pi:src/qq_ai_bot/services/main_agent_backend.py:717–729；Pi:src/qq_ai_bot/automation/creation_key.py:14–19

**可立即删除（源码+调用检索已交叉核验）**：
- `WorkControl.charge_tools:590–594`：src内无调用者；正式调用已由 WorkSession→admit_dispatch原子计量，不再提供可另扣工具预算入口
- `WorkControl.corrections:146/555`：仅声明、重置和测试读取，没有执行含义
- `WorkRepository.checkpoint(evidence=...) :554,590–598`：src和测试没有该关键字真实调用；删除把 execution_evidence再写checkpoint的可选入口，事实只从effects读；不要误删位置参数 payload 的测试
- `AgentRunner._code_host` 的 remaining_calls参数及 Code各级纯转发：真正Code段 allowance只用 runtime.max_tool_calls + control.tools_started/_inflight；直调 coordinator 的remaining_calls仍保留
- `consume_provider_chain_restart` 返回值在 `TurnExecution:677` 被丢弃，层层透传只清旧flag；与API-02一起删除整个无消费信号链，不能仅删消费却遗留设置者

**有真实历史消费者，条件退出**：

- `execute(invocation=None):1644–1656` 生产InvocationService显式传，历史/test host调用仍用fallback；先把测试fake迁到正式Invocation合同，再删反向构造身份的入口
- `record_effect` / `effect_evidence` 对旧 outcome缺失保守读取不能立即删；需要支持数据库谱系/保留期清单和旧行fixtures，旧unknown不能被迁成未执行
- `CodeDriver._resume:346–353` old accepted lifecycle read、snapshot output_ref缺失兼容保留到原active compositions完成/明确retire；它们是持久格式兼容，不是死接口

#### Pi统一操作身份后的完整删除链

权威链：`Pi:src/qq_ai_bot/services/main_agent_backend.py:444`接受Invocation；`:506`取operation_id；`:717–723`是src内唯一ToolInvocationContext构造，call_id无条件设该operation_id；`Pi:src/qq_ai_bot/capabilities/provider.py:453–459`设/复原current_invocation。实际读取者只有以下三处：

| reader | 当前多余转换 | 替换（只在Pi整合后） |
|---|---|---|
| `Pi:src/qq_ai_bot/services/agent_tools.py:970–973` | Work存在时receipt_key(call_id)再hash | 原分支直接hash(invocation.call_id)，不改变无Work旧request编码 |
| `Pi:src/qq_ai_bot/plugin_host/facades.py:3075–3082` | Work存在时receipt_key(tool.call_id) | 同分支直接tool.call_id；无Work`execution_key:call_id`与direct callback随机identity原样保留 |
| `Pi:src/qq_ai_bot/automation/creation_key.py:14–19` | Work存在时receipt_key(invocation.call_id) | 同分支直接invocation.call_id；source_key与外层hash不变 |

然后删 `Pi:src/qq_ai_bot/runtime/work_session.py:1378–1388` 的11行method、相关无用途的反向解析知识。`call_key`仍供Work配对等真实caller，不能一并删。

离线探针覆盖短/长direct key及各自child（6例），真实receipt_key都是恒等。仓内没有receipt_key测试直接caller。不是“先更新旧数据库才可删”：它是调用时适配，不是旧snapshot reader。原snapshot/operation_id/效果回执照读，原ID字节一个不改。main未有Pi Invocation，不可提前在main单独套此删除。

### RUN-09 从生产核心移出测试专用状态和合成frame

- 实施批次：P2
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：现有agent_core loop与真实ChatResponse
- 真实调用方：生产run_agent_loop；测试合成frame
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：精确确认EventStream无实际消费者；测试搬迁不计净产品收益
- 固定源码：Pi:src/qq_ai_bot/agent_core/state.py:1–51；Pi:src/qq_ai_bot/agent_core/model_boundary.py:76–132

`agent_core/state.py` 51行 AgentState/reduce 的生产引用仅 `__init__` 导出；`Pi:src/qq_ai_bot/agent_core/model_boundary.py:76–132` Frame/collect_response明确仅合成frame。真实Provider返回完整ChatResponse，TurnExecution未使用它们。EventStream生产run_agent_loop默认创建队列，但唯一生产调用不传监听器也不drain；这不等于任何用户实时流式能力。

删除产品的未消费state模块/合成帧API，将必要fixture放tests/support或直接测试真实ChatResponse不完整状态；删默认无消费的事件积存，若保留测试事件参数则不在生产默认持有所有response对象。事件投影不是journal，不能用它替代真实回执。provenance文档同步说明参考行为，不能以Pi对齐维持无调用生产抽象。该包不拆掉三个核心边界，不改变partial不执行及agent_end取消语义的测试覆盖。

可退出产品约108行（51+57）外加导出；测试搬迁不是仓库总净删除，需分开报告。若产品确实有已承诺即将接入的事件消费者，先给出真实调用证据后再决定EventStream部分，不虚构消费者。

### RUN-10 资源政策与正确性约束分开，删除不一致的隐藏门

- 实施批次：P2
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：实际交付集合和原预算/调度owner
- 真实调用方：complete artifact、只读lookup/task_control
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：先明确8产物政策及只读批次；资源限额不随意全部放开
- 固定源码：Pi:src/qq_ai_bot/runtime/work_control.py:955–962；Pi:src/qq_ai_bot/services/turn_execution.py:1554–1576

- artifact complete：WorkControl:955–962 `1 <= len(selected) <= 8`；TurnExecution:1571 `[-8:]` 在自动complete时静默只选最后8个。这是交付范围政策，不是“全部产物已交付”的证据。删硬8及静默截尾，沿原artifact集合/完整请求容量处理；9+产物成功及缺其中1份的负例都需测，不能把前8份当全任务完成
- WorkBudget child保留8/8、SubagentScheduler全局>=2、每根8/排队8、Code pending16/total suspensions1024是资源/调度政策。当前有限root预算不能因重构退款或放宽；若放宽仅适用明确新授权预算。Code当前超过pending上限已返回paired code failure，不是把全Work暂停，不能复报旧问题
- `tool_lookup`纯批最多10、所有task_control action独占批（包括get/list）是调度简化政策。可把只读目录/查询从硬全批拒绝退出，沿已有调用序/容量执行；由现有工具目录及调用批次负责人统一实现，保持写/发送/终态控制屏障。不要新增“低频动作分类状态”
- Code memory写后Host停止、未知效果停止、合法新输入中断、权限关闭不是任意限额，保留。memory独占观察合同若要改变属于记忆组语义决策，不在本包偷偷放行
- soft compaction容量已在TurnExecution:1140–1160/1251–1255允许当前真实请求仍能装入时延后；#262隐藏pending字符串/数量上限已去掉。不把已解决项重新列成删除工作

### DEP-01 保留 Code 低频工具仓并完成单 loop 可选模式

- 实施批次：P0
- 本版变化：第三轮修正合同owner边界
- 唯一职责所有者：MainAgentContract的mode/tool view；原PromptComposer与executor pin；唯一loop及Code capacity
- 真实调用方：主Agent、SELF、Automation、child、恢复、打包与完整工具目录
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：默认由实际部署选择或同发行物测量确定；用户服务器资源操作另按范围授权
- 固定源码：Pi:src/qq_ai_bot/services/main_agent_contract.py:65–130；Pi:src/qq_ai_bot/codemode/limits.py:12–60；Pi:src/qq_ai_bot/codemode/engine_monty.py:270–285

当前目标是保留Code作为低频工具仓，终端能力常驻；先验证单worker按需启动和完整业务共存。未测是未知，不能据此把默认强制设为direct。默认由同发行物的资源验证或明确部署选择冻结，本次没有改生产默认。

#### 唯一执行主干和完整 optional 关闭

MainAgentContract只冻结mode和有序tool view及其稳定revision；提示词仍由现有PromptComposer负责，provider/profile仍由既有executor.pin/profile_revision负责，Work合同引用这些稳定revision。不要把全部配置复制到一个大合同对象。code模式用固定紧凑直调集合加execute_code/lookup，低频工具由Code访问；direct模式声明当前作用域全部受支持工具，不附Code入口，不生成ScriptApi，不导入binding、不验证或启动worker。主Agent、SELF、Automation、child及恢复共用AgentRunner、TurnExecution、agent_core loop、InvocationService、WorkSession，禁止复活旧Runner。子Agent仍只得原批准子集，关闭Code不改变权限。

停用时停止新接纳，原pending composition沿现有停用收尾，保留work_id、operation_id、子intent、accepted/unknown和累计预算。已发生副作用只查询原回执；未派发明确结束；父调用只配对一次partial/disabled结果，然后合法新链处理剩余目标。修掉_code_host字符串unavailable绕过Driver worker=None收尾的捷径。不能为关闭启动VM，也不能换新direct ID重发。旧reader只覆盖原事实，待pending排空再删执行桥。

同Dockerfile可提供direct/codemode target，共用发布校验。direct target不依赖专用Monty构建物；共享bwrap用途先核，不另复制发行流程。纯direct清洁环境验普通、SELF、Automation、child、恢复及低频工具全可达；schema token和Host内存成本按完整请求量，不暗中隐藏能力。

#### 精确限额和本轮组件测量

审计基线：Pi `e7bc7d3275b09ddc5363bbb1eb9d2ee255f276ed`；main `aecc09d621067b7782c2b89e919fe2a7b2c9e5fa`；Monty 精确源码 `3f9d6ef413fb951e5b80113b7088d535bd028fcb`，workspace/binding 版本 1.0.1。

`Pi:src/qq_ai_bot/codemode/limits.py:12–28,54–60` 把 64 MiB 传入 `max_memory`。其真实含义：

1. Monty `Monty:crates/monty-runtime/src/main.rs:12–20` 安装 `LimitedAllocator` 作为 worker 全局 Rust allocator，适用于 subprocess 和 CLI
2. `Monty:crates/monty-alloc/src/lib.rs:16–38,46–118` 对每次 alloc/realloc/dealloc 的请求字节增减计数，hard ceiling 是 baseline + budget
3. `Monty:crates/monty-types/src/resource.rs:33–74,475–486,740–744`：soft 检查读取 `LIVE_MEMORY - BASELINE_MEMORY`。64 MiB 是执行检查点的 soft limit；普通非 type-check worker 的 hard budget 加 **4 MiB**，所以为 **baseline + 68 MiB**；type-check 加 32 MiB，即 baseline + 96 MiB，且类型检查器常驻结构可能计入 baseline
4. `Monty:crates/monty-runtime/src/subprocess.rs:55–61,105–117` 在每个请求处理后按当前 session 预算重新 arm；配置、恢复、reset 也影响当前预算。无 session / 无 max_memory 不等于一直保留 64 MiB cap
5. `Monty:crates/monty-alloc/README.md` 与 `Monty:docs/limitations/resource_limits.md:54–123` 明确：这是请求字节，**不是 resident bytes**；包括经此 allocator 的解析/编译结果、协议帧、解码、snapshot 等分配，不应把这些统统重复列为 64 MiB 之外

首版把全部解析器及native堆重复加在64MiB之外，表述过宽；本版统一采用上述global allocator口径。确实额外或口径不同的包括：

- worker baseline；映射二进制/共享库；真正驻留的线程栈；直接 mmap；allocator元数据/碎片；并非全部都由 `max_memory`覆盖
- 16 MiB worker stack是虚拟预留，不表示始终额外实占16 MiB（上游 `Monty:crates/monty-runtime/src/subprocess.rs:27–45`）
- Host 中 binding、Python对象、Host侧frame/工具结果/快照bytes副本、持久化封装及缓存，不属于 worker全局allocator
- bwrap/launcher及子工具进程树、文件页缓存/cgroup记账也需要完整发行物测量

64 MiB不是启动时预分配，也不是总RSS硬限，更不是总增量实测。Pi checkout未开启type_check（`Pi:src/qq_ai_bot/codemode/engine_monty.py:270–285`），不要把type-check 32 MiB headroom无条件加到当前路径。

#### 2. 找到的合法测量路线与版本限制

官方 `RELEASING.md` 确认三个PyPI分发：`pydantic-monty` metapackage、`pydantic-monty-client` binding、`pydantic-monty-runtime` worker。README支持 `Monty` → pool → checkout → feed_run；Makefile支持独立 `bench-pool`。这些接口可测可信字面脚本，不必冒充Yuki生产启动，也不必伪造root-owned目录。

本轮查询官方PyPI JSON：client/runtime/metapackage可见1.0.0与1.1.0，**没有1.0.1发行记录**；`client/1.0.1/json`返回404。没有声称全互联网不存在该artifact，也没有用第三方镜像或未知二进制替代。精确Pi builder需要Rust1.96、CPython3.12、固定源码与项目patch；当前云端未安装rustc/cargo，未为了资源问答引入整套构建链。精确native1.0.1仍待在合法构建环境产出。

已使用官方PyPI1.0.0的CPython3.12 Linux x86_64 wheels，在独立 `/tmp` venv安装，不动共享Python或项目。官方wheel URL、SHA256与release metadata保存在证据；下载内容核对PyPI SHA256，再将已安装9项非dist-info文件逐一与wheel字节比对，全部一致。

- client wheel SHA256：`8cbb089ed7029ab453171048fbd6e208a6c2614bc126a8a124808d4dc67bf2e0`
- runtime wheel SHA256：`d4e9bf8fcbd394ec17c97197c2399e4cd19d8ed3faca2e7c3f1a18fce06d0358`
- 上游v1.0.0的resource.rs与固定3f9d6ef仅在 `fetch_update` → `try_update` 一处不同；所核对soft/hard/baseline计量相同。subprocess.rs字节相同。**这不证明两版本所有行为相同**

官方来源仅用于证据：PyPI `https://pypi.org/pypi/pydantic-monty-client/json`、`https://pypi.org/pypi/pydantic-monty-runtime/json`；上游仓库pydantic/monty，ref如上。直接网络个别请求超时后使用官方GitHub connector读取固定文件，无安全拒绝绕过。

#### 3. 实际测量与范围

时间：2026-10-07 14:55:03–14:55:16 UTC。Linux6.18.44 / x86_64 / CPython3.12.14。官方同步 `Monty` API、min_processes=0、max_processes=1、max_checkouts_per_worker=1，type_check=false，64MiB max_memory、10秒feed、15秒request timeout。Pi实际用AsyncMonty；同步组件结果不直接等同完整异步Host。

父采样器每约20ms读取测试Host与所有后代 `/proc/*/smaps_rollup`，553份采样；采样器本身不计入测试树。通过PPid发现树，不只计worker。每阶段停350ms，稳态表采用标记后100–250ms窗口中位数，避免把下一动作算入前一阶段。峰值表来自完整采样，**是观察到的峰值下界，短于采样间隔的瞬态可能漏过**。RSS重复计共享页；PSS更适合归因。并未测同机业务共存、CPU SLO或cgroup峰值。

| 阶段 | 整树稳态RSS MiB | 整树稳态PSS MiB | 相对冷Python增量PSS MiB |
|---|---:|---:|---:|
| 冷Python（已导入测量标记用标准库） | 7.87 | 5.61 | 0 |
| 首次import binding，未建pool | 17.68 | 15.33 | 9.73 |
| 建pool，尚无worker | 19.03 | 16.67 | 11.06 |
| 首次checkout空闲，1worker | 24.08 | 20.90 | 15.29 |
| 首次10万项整数计算完成 | 27.27 | 24.09 | 18.48 |
| 持有63MiB字符串，3轮范围 | 90.53–98.42 | 87.34–95.23 | 81.73–89.62 |
| 持有约7.94MiB snapshot bytes，3轮范围 | 44.67–52.50 | 41.48–49.31 | 35.87–43.70 |
| 第一轮worker/pool关闭后Host | 28.06 | 25.69 | 20.08 |
| 第三轮关闭后最终Host空闲 | 20.26 | 17.89 | 12.28 |

首次worker自身RSS4.36MiB、PSS3.55MiB；首次checkout的**整树**PSS增4.23MiB，其中还含Host开销，不能把4.23全算worker。63MiBpayload阶段worker自身PSS约69.73MiB，Host约17.61MiB；完整增量当然超过字符串大小。

轻计算为 `sum(i*i for i in range(100000))`，只返回标量。63MiB字符串只返回长度，不把63MiB巨值传回Host；这是接近64MiB配置上限的正常有界工作集，**没有为了“实占恰好64MiB”制造OOM或攻击性耗尽**。生产业务多为工具编排，不应把此上界附近负载冒充典型请求。

释放该字符串后，构造 `8MiB - 64KiB` payload，在可信checkpoint外部函数边界执行官方snapshot.dump。实际dump分别8,325,335 / 8,325,335 / 8,325,337 bytes，约7.94MiB，小于Pi8MiB上限。它是**接近最大合法体积**的snapshot，不是已证明所有合法snapshot中的最坏内存形状。序列化期间整树采样峰PSS57.05–57.41MiB，RSS60.25–60.60MiB；没有执行Yuki ProtocolStore封装、数据库写入，不能据此封顶生产checkpoint总峰。

三轮均正常完成（exit0、无stderr），每轮worker关闭后仅测试Host存活，最终也无worker。Host余量未立即回冷态，说明不能承诺“子进程结束就归零”；三轮不足以证明长时泄漏不存在。首次测试因Monty不支持del语句而退出，保留失败记录，改为官方可执行的赋值None；采样器初稿因该环境不提供task/children未取得数据，改用PPid扫描，未伪造第一轮数字。

#### 4. 历史证据与落地判断

Pi历史 `Pi:docs/architecture/pi-codemode-evidence/p09-linux-isolation.json`：旧Linux arm64 sleeping worker RSS4040KiB≈3.95MiB，anon648KiB、file3392KiB；无Host/PSS/繁忙场景，非当前版本。其量级与本次组件轻载worker接近，但架构、版本、发行物均不同，不能合成生产保证。

建议：

1. 保留Code入口，优先1worker按需启动，不为“可能额外64MB”先删除有用能力
2. 当前Pi必须同时 `CODE_MODE_MAX_WORKER_PROCESSES=1`、`CODE_MODE_FOREGROUND_RESERVED_PROCESSES=0`；1/1非法。唯一槽会被后台持有，需保留有界等待/失败语义，不能动态改schema
3. 正常工具编排与接近上限内存负载分开测；Host只读业务并发另有限制，1worker不等于所有子工具串行
4. 若同发行物同业务负载完整增量约64MiB且忙时余量够，保留并启用合理；若完整峰值约80–100MiB也未必不能接受，仍看实际余量、OOM/PSI/延迟，不以本次参考值直接给用户机器判死刑
5. 确有压力时先有界接纳、降低同时运行量或选择启动期optional off；没有证据要求恢复旧runner或永久全局direct
6. 精确验收补上Pi1.0.1、launcher/bwrap、AsyncMonty Host、正常工具、snapshot store、重复取消/超时/退出后的cgroup与PSS；用户服务器只在另获授权后采现有监控或执行约定测量，本轮不触碰

本轮不把Rust构建OOM、磁盘镜像大小、RLIMIT_AS虚拟地址上限混作运行RSS。外部构建仍合理，但不能拿编译成本否定Code运行能力。

两份新报告均为用户转来的第三方部署案例。其PSI、换页、延迟和部署配置不代表用户机器，不能据此判用户1.6GB环境已无余量或必须停Code；官方组件增量也不能代替目标环境的真实共存测量。


### CTX-01 允许部分摘要，取消摘要覆盖率对业务的强耦合

- 实施批次：P0
- 本版变化：第三轮统一partial加残余；ordinary配对和media闭包同项迁移
- 唯一职责所有者：现有payload.refs覆盖；parent_sources完整来源依赖
- 真实调用方：summary publisher、history selection、reader、artifact refs release
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：四类消费者同一变更；不能只删全引用raise
- 固定源码：Pi:src/qq_ai_bot/conversation/observations.py:288–300；Pi:src/qq_ai_bot/conversation/observations.py:649–678；Pi:src/qq_ai_bot/services/history_projection.py:425–450；Pi:src/qq_ai_bot/conversation/projections.py:475–486

本任务统一采用“真实部分摘要加未覆盖原件”。scope observation保留遗漏observations；ordinary执行尾保留遗漏portable records的完整配对闭包。共享summarize_records如实返回refs，不要求模型逐源引用，也不把遗漏等同允许丢事实。

#### Scope observation摘要允许部分覆盖

1. 删除Pi:src/qq_ai_bot/conversation/observations.py:677–678的referenced==allowed门，以及scope-summary“每个来源必须引用”的提示词要求；共享helper如实返回真实refs，不伪造补齐，不发纠错LLM强求全引用。
2. Pi:src/qq_ai_bot/services/history_projection.py:425–450只替换实际引用来源，未引用原observations及所带chat事件原样保留且不重复，并计完整request容量。
3. Pi:src/qq_ai_bot/conversation/observations.py:288–300递归coverage按每层payload.refs展开；Pi:src/qq_ai_bot/conversation/projections.py:475–486仅释放实际覆盖来源的artifact owner。不能把完整parent_sources当全覆盖，也不加covered列或影子cache。
4. parent_sources和summary_key仍绑定模型实际读过的全部原输入及版本。34读入、20引用的provenance仍34；未引用但被读过的源撤销也使派生候选失效。owner/actor/read_scope、generation、隐私、prompt source revision、父版本DAG与发布CAS完整保留。
5. 空候选不覆盖任何原件；unknown ref、无来源fact、空text或schema错误不发布；重复ref规范成集合而不重复释放。复用原paid candidate/summary_key，不新增修复状态机或将已付费模型因单纯发布冲突重跑。

分页全过程的原observations全集仍是parent_sources/summary_key；previous_refs仅上一页输出，不是已读全集。后页A/B→B/C时A原件必须回来；超大单record各片段都读完才发布，片失败保完整原件。顶层引用旧summary仍逐层按其自身refs展开，未覆盖原件/artifact继续保留。

保留已有hard_fits可直接容纳原完整请求的分支；不要再造fallback。subset摘要+残余仍超硬限时保留事实并明确capacity，不标内部执行错误、不重跑终端。

#### Ordinary执行尾保留未覆盖原轮次

Pi:src/qq_ai_bot/services/ordinary_compaction.py:45–74当前整段替换tail，须与共享helper同项迁移。复用现有records和ordinary_working_summary envelope，组装真实摘要加未覆盖portable records，不新增SummaryManager、缓存或持久schema。调用或结果任一遗漏，就保留对应原轮次必要的完整portable调用/回执单元；原ID、次序各一份，不重放旧native协议，不复制opaque/reasoning。

媒体不能仅保留旧计数：从仍在本地tail的原ChatMessage带回获准media，并沿原来源重新校验。摘要、leftovers、evidence、media共同进入完整request容量估计；确实fit且变小才采用合法新链。否则保留已发生事实，合法旧request fit时沿已有fallback继续，真超限明确capacity；不追加纠错LLM、不重发工具或消息。

旧whole-tail替换caller尚未迁完时，allrefs仅是临时防丢保护；同项迁完后删除，不能成为长期覆盖硬门。source/权限/opaque失效仍按原边界拒绝，不当可选候选失败吞掉。

#### 普通chat与第三方案例验收

原独立34→20探针仍验最终20来源摘要+14原件。新增active=None普通chat的39→33摘要+6原件、39→16摘要+23原件；这些数字来自用户转来的第三方部署报告，本次没有复测其现场。对方声明本地878bc9348faf2b8ac55205d53737d5b13240646d对应Pi e7bc7d32/合同15、full77/direct40，均未独立核验，不与固定源码统计混算，也不是用户机器数据。

再验分页丢旧ref、单record分片、嵌套summary、原snapshot带chat一份、空候选零覆盖、unknownref不发布、source删除（包括已读未引源）、actor/scope拒绝、CAS失败零发布、publication冲突只重备及硬容量拒绝。ordinary-tail分别验部分refs后的遗漏原轮次闭包、call/result原ID与次序一次、media来源重验、摘要加leftovers的完整容量、原请求fit可继续和真超硬限不派发，原效果不重做。

本项validator、selection、reader coverage、artifact释放四处原子修改；其它Context任务可独立准备，共享选取/容量接口联合回归。现状probe与第三方案例都不代表目标实现或自然摘要质量验收。

### CTX-02 删除派生统计对成功发言、记忆变更回执的同步依赖

- 实施批次：P1
- 本版变化：第二轮补强原任务
- 唯一职责所有者：原send/mutation receipt；独立派生统计
- 真实调用方：ChatService完成、Memory session归因与embedding调度
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：不吞预算/强审计/真实journal写失败；finally关闭session
- 固定源码：Pi:src/qq_ai_bot/services/chat.py:1396–1420；Pi:src/qq_ai_bot/memory/mutation/service.py:3138–3153

- 真实caller：`Pi:src/qq_ai_bot/services/chat.py:1337–1346`普通完成、`1955–1963`Work续跑 -> `_finish_memory_turn:1396–1420` -> session.on_delivery_confirmed:482–532；await归因完成后才session.close，Work才save delivered。
- 合成注入 RuntimeError("diagnostic unavailable")，异常原样上抛、close调用0次（reproduce-results.json）。这是完整局部调用复现；没有伪称端到端Work suspended复现。
- 删除目标：把归因准备/持久统计从业务完成的必经await去掉，复用已存在诊断隔离/后台归因路径；close以finally完成。已确认send、journal/预算/强制审计仍是强事实，不可按统计降级。不要再包一个通用retry框架。
- 现有正确先例：turn_execution:643–650隔离confirm_memory_exposure，agent_tools:2812–2829隔离read-outcome，session.close:537–548隔离cleanup。沿用这些边界，不重复发明持久诊断状态。
- 同类漏口：Pi:src/qq_ai_bot/memory/mutation/service.py:3138–3153，`get_fact`在try外，提交后的embedding调度也只捕获部分错误；调用点1494、1858在receipt已提交后。删除调度失败影响mutation返回的依赖，返回原receipt；embedding队列可独立修补。取消仍按原规则传播，不能误造新mutation。
- 反例：注入归因DB锁/序列化失败、embedding get_fact异常，业务send/mutation receipt仍可查询且不重复执行；finally释放session；诊断标缺样；journal落库失败仍需停止。

#### 收尾调用闭包

`main/Pi:src/qq_ai_bot/services/chat.py:1369–1397` `_finish_memory_turn`先await on_delivery_confirmed再close；`main:src/qq_ai_bot/memory/runtime/turn_session.py:482–532`每handle写skip或enqueue，直到末尾才设_delivery_reported。失败会让外层退出栈close走interrupted skip，反写“归因未完成”，不能被误读成“QQ未送达”。close本身`:534–548`已有局部异常隔离，**不要新增第二层通用close wrapper**。

可删的是“在模型/QQ成功返回路径上逐条派生归因完成”的前置义务；保留冻结曝光、可信原身份/隐私、mutation已提交事实。长期真值、receipt_gated写确认和可丢metrics不是同一数据。工具结果/render/archive likewise把原effect receipt作为唯一业务事实，在唯一呈现owner结束；不在Runner吞整段WorkSession异常。该项复用第一版记忆/工具定向证据，不重复计缺陷和删行。

### MEM-01 删 Memory 预取旧路径与独占写的重复会话状态，复用执行权限和持久回执

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：现有Memory mutation receipt和执行授权
- 真实调用方：session、backend独占标志、memory_change
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：多条合法变更政策需在实施范围中明确；每次仍核来源/目标/权限
- 固定源码：Pi:src/qq_ai_bot/memory/runtime/turn_session.py:296–366；Pi:src/qq_ai_bot/memory/runtime/state.py:175–190；Pi:src/qq_ai_bot/memory/mutation/service.py:1497–1529

#### 已确认旧面

`Pi:src/qq_ai_bot/services/chat.py:1536–1557`直接empty_retrieval；没有产品caller调用TurnMemorySession.prefetch，仍有stage_prompt_selection(1572/1911)空曝光搬运。`Pi:src/qq_ai_bot/memory/runtime/turn_session.py:153–160,296–348,356–366,431–460`保留预取token/result/intent/staging/confirmed；contract:186–211仍说自动inject/首轮deferred；state:38–44/419–432保留PREFETCH阶段。`Pi:src/qq_ai_bot/memory/runtime/finalizer.py:116–172 finalize_mutation_text`没有产品caller，main_agent_backend.finalize:1076–1088只处理NO_REPLY/尾标记。不是仅凭文件名断言遗留，已检查实际入口、具体赋值与调用。

执行任务：删无caller prefetch方法、只服务它的字段/阶段/自动注入contract分支、无caller deterministic finalizer；保留finalizer中仍被session调用的tool-result解析直至session后续合并。删除空stage调用及token参数（confirm_prompt_exposure:428直接del token），保留工具结果真正进入后续模型请求后的pending/confirmed曝光区别。搜索服务的自动消费者如果别的调用者仍使用不能整体删：QueryPlane是Plugin/Admin共用边界。

#### 重复状态与反人类限制

真实写链：backend:609–614请求exclusive -> agent_tools.memory_change:2415–2477独立核验真实内部事件/actor/presence -> mutation.service提交receipt -> backend:880–903解析结果、session.observe写状态并可能_tools_closed。session:550–576又重建假的MemoryMutationResult；state:175–190规定终态后再attempt异常。合成NO_CHANGE后第二次写抛IllegalMemoryTransitionError。成功/无变化之后不同合法目标都受本次调查状态机限制，不是来源授权必需。

Pi还在mutation/service:1258、1295、1497–1529按同trigger事件持久one-write限制，即重启后仍消耗；不能只删内存门使行为一半放开。此门是产品政策，receipt幂等键和Work effect key才是重复效果保护。

执行任务：在用户确认“允许同一请求合法多条记忆变更”的当前重构范围内，删exclusive-write仅一条/locator恰好一次的状态机与backend parallel flags；每次操作沿现有Agent预算、当前权限、Memory selector及receipt幂等执行。保留同一effect重放返回原回执、不执行第二次；删除目录/阶段造成的工具开关，固定声明继续稳定。不要新建另一套session状态。

`Pi:src/qq_ai_bot/memory/runtime/resolver.py:124/159–160`把image_present当禁写条件；它是场景政策而非写来源证明。可删除“仅因附件有图”禁写，仍要求文字声明/获准证据、明确目标及原作者权限；图片本身不自动成为可信证据。不删除真实来源验证。

验收：一个用户请求记住两条不同事实、先noop再纠正另一条、两次读定位后合法写不触发状态机崩溃；重复同effect只有一个receipt；有图片但明确文字事实可写；模糊人物不得猜写；Plugin/worker仍无主Agent写权限；固定工具前缀/contract revision不随读写阶段变化。MEM-01需要Memory授权、主执行循环和工具合同同时迁移，不能局部放权。

### CTX-03 短状态删双重容量门，保留原CAS及明确共享语义

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：ShortState单表CAS及真实request容量
- 真实调用方：composer snapshot、ShortState.execute/update
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：删除隐藏512字节门；3槽/24h政策保留；共享语义不变
- 固定源码：Pi:src/qq_ai_bot/workspace/short_state.py:26–28；Pi:src/qq_ai_bot/workspace/short_state.py:89–145

真实链：container:344–347装配MainAgentContract；main_agent_turns:143 snapshot -> composer短状态贡献；backend:521/1162 -> ShortState.execute -> update。`ShortState.inject:141–145`产品无caller（测试有），是第二条旧注入接口，应删及改测试沿真实composer。

`Pi:src/qq_ai_bot/workspace/short_state.py:26–28/89–94`三个槽、每条300字符；112固定24h；116–124又要求整个untrusted JSON envelope<=512 UTF8字节。隔离200 ASCII成功、200中文失败，虽二者都满足300字符schema；不是模型硬上下文用满。

删目标：去掉“完整信封512字节”隐含第二容量门，三槽×每条统一可见长度上限已经有限；若产品要总量应单一语义且在工具schema说明，实际prompt容量仍由统一请求测量处理，不再近似字节=token。3槽/24h可保留为明确产品政策，是否扩大需产品取舍，不把所有常量都当bug。

保留：同一slot expected_revision CAS、empty/expired保留revision、读不续期不写库、失败原子回滚、untrusted数据不能授权。当前全局共享是明示合同（17），不能假称private Memory；不把所有Memory塞short_state，也不加分区副本/影子缓存解决。

验收：同长度中英文行为一致；3条各合法长度不会因隐藏信封开销意外失败；过期stale writer仍失败；snapshot异步线程读只SELECT，无写锁；真实composer前缀不变，新状态只追加在当前动态信封。

### CTX-04 删前置字符硬拦，统一真实请求硬容量；保持确定性prefix

- 实施批次：P1
- 本版变化：第三轮补完整request容量分支和现有fallback验收
- 唯一职责所有者：完整ChatRequest统一硬容量与固定prefix
- 真实调用方：context assembler、prompt composer/compiler、main_agent_turns
- 编码前置：无
- 协调边界：本任务完整请求计量；CTX-01覆盖集修复为共同回归而非所需新接口。
- 实施或发布条件：不删必需来源/当前触发/协议配对来凑容量
- 固定源码：Pi:src/qq_ai_bot/services/context_assembler.py:1387–1432；Pi:src/qq_ai_bot/services/main_agent_turns.py:305–348

真实链：context_assembler._fit_metadata:1387–1432按contribution cost选，再序列化超额循环降预算；composer:282–293 window_tokens*3 + plugin字符额 - history/current，再compiler._select:104–111可能直接抛PromptCapacityError；main_agent_turns:305–348才按完整ChatRequest tokens（含tools/native/images/settings）算soft/hard。

这是两层策略预算和最终硬容量混杂。assembler已有required_json在真实capacity够时保留required的补丁（1405–1412），不应再叠更多例外。删compiler前置“经验字符预算不足即业务失败”依赖；可选贡献按既有优先级整理，必需贡献交给唯一完整请求测量决定。不要删除必须保留的来源、current trigger、协议配对来凑容量。

`PromptStability.SESSION`在models:33声明、compiler:55–56立即拒绝且写死3.7.0；产品无有效SESSION贡献。删这个不可用公开枚举/相关metrics字段和_stability_rank分支，而非扩建会话prompt框架。

与RUN-07共用的删除项：context_preparation:52–74先load journal并校验guard，ContextAssembler:240–262只用guard/version，TurnExecution:334又WorkSession.restore:163–168重新load。复用同activation已取得的immutable snapshot，派发前仍检查live来源/lease；不得把授权结果缓存，实现统一归RUN-07。

前缀稳定不能删：compiler静态稳定排序、dynamic只进当前user信封(131–140)，FrozenFragments的实际请求选择，projection CAS的source/version/epoch，以及history_projection:266–308保留原selected rollup并补缺聊天。按真实messages/tools/settings比较，不能用hash相同或缓存命中率冒充验收。

验收：软目标超限、硬请求可容仍执行；中文/媒体/长tool schema请求按统一尺；不注入无关数据保温；同链所有旧messages逐项相同；新的privacy/scope边界正确换链；硬溢出明确capacity结果。

#### 同快照纯准备只保留必要副本

第三方案例现场大elapsed不能直接等同JSON或锁等待。main:src/qq_ai_bot/conversation/projections.py:685–724 已复用PreparedHistory.previous_snapshot/previous_item_count，不再新建PrefixCache；main:src/qq_ai_bot/conversation/projections.py:374–460 已按256-key批查，不误报逐条N+1。沿现有prepare边界只保留一份immutable选中数据/编码结果，删其无消费的再次deepcopy和序列化；对外可变返回仍保持隔离，完整请求估算包括media/tools/settings。分别记录SQL/物化次数、编码字节与CPU、writer和事件循环/IO等待，不预报毫秒收益。


#### 完整请求容量的两个失败分支

另一第三方案例报告3个send_message分片已confirmed后，2refs全覆、摘要正文4256→1158字符，仍报no_capacity_improvement；未提供完整before/final/input_budget，不能判根因，更不是RAM限额。Pi:src/qq_ai_bot/services/ordinary_compaction.py:100–103用同码表示candidate超硬预算或candidate不比before小。沿该原测量边界各算一次同一capacity ruler下的完整请求估计before_tokens、candidate_tokens及input_budget，不把估计冒充Provider真实token用量，记固定candidate_hard_overflow/candidate_not_smaller分支码，可附消息/tools/native/evidence计数及原turn/chain；不记正文或SQL参数，不另建诊断系统。

Pi:src/qq_ai_bot/services/turn_execution.py:1251–1255已有原request fit时放弃失败候选继续的分支，复用它。验合法原请求before≤budget且仅可选候选质量/收益失败→原请求继续；before>budget且candidate仍超→零新主请求且保留已确认效果；before>budget而candidate≤budget且变小→新链合法采用；来源/权限/opaque失效不按可选失败吞。正文缩小不能替代完整请求测量，不靠更短文本或加模型预算猜修复。


ordinary候选的容量必须包含CTX-01保留的完整portable配对残余、原evidence和重新获准media；只量摘要正文会低估。采用条件是同一完整request估计下fit且变小，不增加长期allrefs门。候选不满足时沿既有合法旧request fit fallback或明确capacity，原效果保持。


### CTX-05 删除 Rollup 内容误判和覆盖失败退避的重复写

- 实施批次：P2
- 本版变化：第三轮吸收独立退避与质量反例
- 唯一职责所有者：原semantic checkpoint与独立emergency overlay
- 真实调用方：rollup service/renderer、分页与输出判别
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：旧prose持久格式未清点前不删其reader；截尾不算语义覆盖
- 固定源码：Pi:src/qq_ai_bot/conversation/rollup/service.py:268–304；Pi:src/qq_ai_bot/conversation/rollup/renderer.py:113–125

- `Pi:src/qq_ai_bot/conversation/rollup/service.py:301–304 extractive`仅转调emergency，renderer:113–125 extractive_compact仅转调truncate_conversation_tail，产品无caller；删除这些兼容接口及只验证别名的测试/导出。`ensure_extractive_coverage:421`仍由ensure_required_coverage:413真实调用，不是同类死代码，改名时需caller一并改，不能盲删。
- service:268–283两次not text/len>limit，合并同一输出校验；保留incomplete/tool-call/unknown-source检查（264–289）。`lowered.startswith('provider error')`把正文内容当异常，是可删除的内容启发式；真正Provider失败走status/LLMError。data:image/base64防blob可改成清晰输出格式约束，不能滥拒合法讨论这些字符串。
- service:166–179先2048固定预留、非ASCII×2、batch_max_characters切源，后面真实请求校验；可复用ordinary measured paging，先移除不必要固定前置拒绝，不加另一摘要框架。需要保留每块原引用及previous_summary，不能让分页按已保留refs缩窄历史事实权利。
- semantic摘要和emergency截尾不是重复事实源：一个语义覆盖承诺，一个明确临时extractive overlay；不能合并成“截尾也算语义覆盖”。canonical账本为唯一原件。实际迁移旧prose仍可能存在，需读已支持的持久schema再删compat parser，不能从无新写caller推断无旧数据。

验收：无变化job只读，无候选不调模型；输出合法包含“provider error”的讨论不被误拦；超大来源分页完整；empty/incomplete不推进semantic水位；emergency不覆盖semantic checkpoint；generation/lease丢失拒绝提交。

#### 删除普通signal覆盖失败退避

main/Pi:src/qq_ai_bot/conversation/rollup/repository.py:205–234 的MODEL_FAILURE已写failure_count与退避；main/Pi:src/qq_ai_bot/conversation/canonical_rollup.py:41–45 的force_existing却无条件把next_attempt_at置now，普通消息main/Pi:src/qq_ai_bot/persistence/scoped_event_uow.py:1136–1137传true。真实函数离线probe把failure171→172、原960秒退避在后到signal后变约0.002秒，failure仍172。不能推断第三方案例现场172次都由此造成。

删这条通用signal覆盖deadline的写，保留原job.next_attempt_at唯一退避owner；新消息推进signal_revision，policy-ineligible park有新合格来源仍可醒，generation与显式运维重试各按原规则。验failure→消息与消息→failure、重复signal、park→合格来源、期限后最新source、generation变化。无需冷却表或第二retry worker。

#### 将质量原因与数据库提交分开

main/Pi:src/qq_ai_bot/conversation/rollup/service.py:258–289 的model_completed在内容/schema/source校验之前；main/Pi:src/qq_ai_bot/conversation/rollup/errors.py:38–42 将多种ValueError压为model_quality。已有日志/last_error记录固定无正文subreason及scope/claim关联，区分容量准备、Provider未完成、非法candidate、source/lease变化，不加完整prompt日志。探针中合法JSON只因讨论data:image/误拒，支持删除字符串启发式；tokens少于预算不能证明摘要合法，也不自动加额度。

main/Pi:src/qq_ai_bot/conversation/rollup/repository.py:922–934 已为确切SQLITE_BUSY_SNAPSHOT517做最多3次完整rollback后的纯DB重备，复用原summary；不能另造paid-cache。普通BUSY5若需重备，只在明确rollback、原lease/source仍有效时复用现有commit owner且有界。未知commit、source改变、取消/失租不盲重跑模型，也不新增跨重启摘要状态机。


### MEM-02 Memory rebuild收尾只依赖既有事实回执，避免统计失败变成重跑模型

- 实施批次：P2
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：原proposal ID与mutation receipt
- 真实调用方：rebuild process_commit_once、worker重入
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：先区分record_model_usage预算事实与展示统计；补故障注入
- 固定源码：Pi:src/qq_ai_bot/memory/rebuild/service.py:715–842

真实链：显式control/admin -> rebuild service.plan/start -> worker:43–58只轮询EXTRACTING/COMMITTING；重启pause(25)，不自动resume，这是显式审批/耗费政策，暂不删。rebuild不是普通Memory请求的新状态副本，而是用户可review后再commit的长操作。

service.process_commit_once:796–823把processor.process和record_model_usage置于同一try；后者异常可进入fail_proposal(826–842)，而processor可能已提交mutation。843之后才finish_proposal。此为代码确认的重试分类风险，尚未端到端故障注入，不能声称已观察重复效果。现有mutation idempotency能够保护持久事实，但不自动证明模型不会重复收费。

删目标：统计持久化不要决定proposal成功；以现有mutation receipt/原proposal ID恢复，receipt已完成就先完成proposal；模型usage计入预算的必要部分仍必须可靠，纯latency/展示计数可丢。不要直接把整个record_model_usage都当诊断删除，先逐字段区分预算承诺。worker异常后下轮重入只能重备纯DB/核原receipt，不能再次调用classifier/consolidator。

保留来源：commit 715 fingerprint、748 eligibility、758–777重新验证主体scope，privacy/trusted_sources及同快照写围栏。看似重复的preparation/commit核验覆盖外部等待竞争，不是冗余。保留提取/审核/提交区别，不新增shadow run。

验收：processor已提交、usage失败、finish_proposal失败、ack lost、源删除/owner变更等故障；只一个事实效果、同receipt ID、不重复模型；取消/重启仍遵守显式resume；空tick不建run不写状态。

### MEM-03 清理仍暴露的旧Memory执行接口与过时文档，给出真实退出条件

- 实施批次：P2
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：统一Memory query plane与原调用恢复
- 真实调用方：agent_tools旧get_*_memories dispatch、持久journal
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：旧未决调用清查完才删执行名；完成回执仍原ID查询
- 固定源码：Pi:src/qq_ai_bot/services/agent_tools.py:1134–1158；Pi:src/qq_ai_bot/services/agent_tools.py:2478–2508

memory-v2-retrieval明确三个get_*_memories只为旧回执执行兼容保留；Pi:src/qq_ai_bot/services/agent_tools.py:1134–1158仍有真实dispatch。因此不能用新声明无这些名字就宣称死接口可立即删。实施先枚举受支持持久journal里是否存在尚未解决的旧名call；已完成旧receipt仅按原call_id回放，不需要再执行旧接口。未完成旧call如仍受支持，则复用原有参数转换到统一search/query plane且保留原call/effect键；确认无此合法调用者后删除三个dispatch branch、声明残余、专用方法与测试。禁止造平台消息ID或更换执行键来伪造兼容。此项依赖runtime旧journal恢复合同，不加永久多版本router。

文档也要直接删除过时规则，而非再叠补丁：memory-change“实施说明”还说仅user_message、唯一写能力、后端deterministic最终正文；现行resolver允许AUTONOMOUS_GROUP，固定工具合同与backend.finalize已不同。第三方事实文档仍说不得名称解析，而agent_tools:2478–2508已有named_member的受控群内解析及歧义返回。以当前授权实现和最新契约共同核定更新，不能仅凭旧文档收紧或凭新分支放宽人物权限。

验收：旧已完成call回放无需旧执行接口；旧未知call不重跑、不丢回执；当前search读权限、strict日期、empty正常返回、truncated/exhaustive语义完整；文档不再同时宣称固定工具与动态独占写工具。

### API-01 直接删除死内部 registry/result DTO

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：ToolProviderRegistry及ToolExecutionResult
- 真实调用方：旧CapabilityRegistry/Result仅定义重导出
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：精确类导入零caller；不删同名其他领域类
- 固定源码：Pi:src/qq_ai_bot/capabilities/registry.py:1–23；Pi:src/qq_ai_bot/capabilities/results.py:17–24

- 证据：P `Pi:src/qq_ai_bot/capabilities/registry.py:8–23` 的 CapabilityRegistry 只有定义和 `Pi:src/qq_ai_bot/capabilities/__init__.py:34,52` 重导出；与仍活跃的 `admin/capabilities.py`、`automation/registry.py` 同名类型不是同一个对象。P `Pi:src/qq_ai_bot/capabilities/results.py:17–24` 的 CapabilityResult 只有定义和包重导出；automation 的同名 result 有真实调用者，不能一起删。
- 现状成本：平行名字暗示还有另一套目录/结果合同，掩盖真正 owner。
- 删除：整份 `capabilities/registry.py`、两个旧包导出、死 CapabilityResult。唯一 owner：ToolProviderRegistry/DescriptorRegistrySnapshot/UnifiedToolCatalog（`Pi:src/qq_ai_bot/capabilities/catalog.py:117–159`）及 ToolExecutionResult。
- 最后调用者：仓内 src/tests/plugins/examples/docs 未找到该具体旧类的调用，不能由此声称未知外部私有Host import为零；SDK公共面不导出这两类。
- 验收：全仓精确 import/动态字符串扫描、包导入、tool catalog重复名拒绝、固定合同测试。无需新增兼容管理器、弃用包装或替代文件。

### API-02 删除动态 schema-rebuild 死链，只保留执行授权投影

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：MainAgentContract拥有声明；runtime仅授权投影
- 真实调用方：backend、TurnExecution、subagent restart透传
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：删除无生产setter链；保留撤权及合法换链
- 固定源码：Pi:src/qq_ai_bot/capabilities/runtime.py:141–162；Pi:src/qq_ai_bot/capabilities/exposure.py:25–90

- 证据：P `Pi:src/qq_ai_bot/capabilities/runtime.py:141–162` 的 can_rebuild_provider_chain/rebuild_after_schema_conflict 仅测试调用；生产只消费不会被生产置真的 restart flag。`Pi:src/qq_ai_bot/services/main_agent_backend.py:198–204`、`Pi:src/qq_ai_bot/services/subagent_execution.py:124–125`、`Pi:src/qq_ai_bot/services/turn_execution.py:677` 继续传播它。P `Pi:src/qq_ai_bot/capabilities/exposure.py:25–90` 另存 declared schemas/append_only/conflict/had_side_effect，主模型声明真实owner已经是 MainAgentContract。
- 删除顺序：先删无调用生产的 rebuild/can_rebuild setter链、flag和空consume委托；再证明各内部消费者只需要 callable IDs/冻结revision后，删除与 MainAgentContract 重复的声明ledger分支。不要一次把整个TurnCapabilityRuntime删掉。
- 唯一 owner：MainAgentContract拥有声明；TurnCapabilityRuntime只投影当前授权/检验参数，不拥有换前缀权。
- 不变量：撤权即刻阻断执行；schema变化需明确新链；不能以“没副作用”暗换既有provider前缀。
- 验收：有/无副作用、memory权限变动、插件批准撤销/热替换、worker子集、旧revision恢复拒绝；删掉只为旧rebuild实现自证的测试，改测真实主合同。

### API-03 退出活执行路径的字符串结果猜测，历史解码仅留在原回执入口

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：现有ToolExecutionResult；SDK单一Host映射
- 真实调用方：binding、agent_tools、runtime持久结果读取
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：活路径先迁类型；旧receipt原格式只读不重解释
- 固定源码：Pi:src/qq_ai_bot/capabilities/binding.py:20–47；Pi:src/qq_ai_bot/capabilities/results.py:411–497；Pi:src/qq_ai_bot/services/agent_tools.py:3194–3303

- 证据：P `Pi:src/qq_ai_bot/capabilities/binding.py:20–47` 接受 Awaitable[object] 并通过 normalize_legacy_result猜信封；P `Pi:src/qq_ai_bot/capabilities/results.py:411–497` 对ok/committed/retryable用bool()、data缺省吸收余项、裸字符串默认成功。P `Pi:src/qq_ai_bot/services/agent_tools.py:3194–3303` 仍先编码JSON，binding再解析，backend最终又编码。
- 合成证据：`{'ok':'false','mutation_committed':'false'}` 被归一成两个True。这证明宽松归一合同危险，不证明真实当前调用者已产生此形状。
- 删除：将core/admin活调用及provider binding统一返回现有ToolExecutionResult；删handler对象返回/JSON往返/兼容别名error-vs-error_code；插件SDK PluginResult在唯一Host边界显式映射，不能要求插件导入私有Host类型。
- 最后调用者清单：binding；agent_tools记忆预算；services/turn_execution；runtime/work_control、work_repository、work_session都有normalize调用。后面三者含历史持久回执读取，不应为删helper而重解释或改写旧效果。先逐个迁活路径，最后把只读历史解码收窄到实际旧格式，不建universal adapter。
- 验收：成功/失败/unknown/committed/media/evidence_state/private grounding矩阵；布尔错型拒绝；已确认写入不得变成失败或被重派；SDK契约测试和已有日志恢复测试。

### RES-01 合并结果预算，删除“读回以后再摘要”的多层循环

- 实施批次：P0
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：唯一最终模型结果信封预算
- 真实调用方：artifact reader→chat fit→backend→ToolResultBudgeter
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：按真实序列化一次定页；VM data与模型短receipt分开
- 固定源码：Pi:src/qq_ai_bot/tool_results/artifacts.py:804–905；Pi:src/qq_ai_bot/services/chat.py:647–660；Pi:src/qq_ai_bot/capabilities/results.py:211–299

调用链：P `Pi:src/qq_ai_bot/tool_results/artifacts.py:512–535,804–905,1079–1081` 先用compact JSON和固定512余量选页 → P `Pi:src/qq_ai_bot/services/chat.py:647–660` 只有text/string进入最终信封fit → P `Pi:src/qq_ai_bot/services/main_agent_backend.py:815–849` 用当前tooling/agent预算调用ToolResultBudgeter → P `Pi:src/qq_ai_bot/capabilities/results.py:211–244,270–299,534–556` 禁止套娃artifact后只能再摘要。

`evidence/tools/synthetic_results.py`和`evidence/tools/synthetic_results.log` 纯内存可复现：

1. 与仓库一致的base字段、1729-key对象、默认24000字符：仓库页23880字符，最终信封24014；下游返回 `ok=true,truncated=true`，但value/handle/path/next_offset都没了
2. 6000字符string，24000字符预算，非默认item_limit=5：初页完整且fit，通用item剪裁把data削成五个元字段，value和游标丢失，仍ok=true

删除/合并：
- 工具实现只产生完整有界数据或真正分页数据，不负责模型摘要；唯一模型信封预算owner使用真实序列化、字符+escaped字节+条目语义一次定页
- 删 `_fits_json_budget` 的“compact减512足够代表下游”假设，与 `_fit_artifact_page_result` 的二次算法合并；不要为object/array/search再加三套特殊fit
- artifact页不得进入通用 `_bounded_payload` 变形；条目预算应控制业务value，不截信封元数据。不能放下至少一个原子值时给明确错误/更深path指引，保留原handle和next cursor语义
- P `agent_tools._result:3221–3233` 仍可在shared budgeter前丢完整值；`_web_result:3247–3303` 会先改写sources、截relevant_content、甚至pop来源，后续archive已不再是原结果。删除这些展示层前置裁剪，保留领域检索的真实候选/分页边界以及记忆完整fact不拆碎的不变量
- 删除重复hardcoded 49152时用既有持久回执上限事实源，不创建另一个“通用预算管理服务”

唯一owner：工具结果层管理模型receipt；工作区/仓库管理data与页；Code VM拿执行所需data和自己的传输上限，不直接依赖主模型短receipt。#262仅workspace_read rehydrate应保留到此分离完成，不扩展成逐工具if表。

验收：默认及配置item_limit、ASCII/中文/引号反斜线、对象/数组/string/search、空页/EOF/超大原子值、最小预算、禁用artifact、Work分段恢复均重建原值或明确未完成；最终预算遵守且游标必须前进；无套娃artifact/无原操作重跑。与RES-02及DEP-01的VM data/receipt分离配套。

### RES-02 删除“派生artifact发布失败升级成业务失败”路径

- 实施批次：P1
- 本版变化：第三轮明确必要容量失败不属于可选展示失败
- 唯一职责所有者：原accepted/unknown效果；结果层仅呈现
- 真实调用方：ToolResultBudgeter、CodeDriver、WorkSession fallback
- 编码前置：无
- 协调边界：既有ToolExecutionResult/原receipt已可表达效果；结果统一及预算重构同组协调，不阻止先移除可选归档异常升级。
- 实施或发布条件：可选展示可降级；用户指定交付artifact不可假成功
- 固定源码：Pi:src/qq_ai_bot/capabilities/results.py:127–176；Pi:src/qq_ai_bot/capabilities/results.py:221–228；Pi:src/qq_ai_bot/codemode/driver.py:875–989；Pi:src/qq_ai_bot/runtime/work_session.py:1827–1866

- P `Pi:src/qq_ai_bot/capabilities/results.py:127–176` 媒体发布已隔离失败，而文本 `:221–228` 直接await archive；P `Pi:src/qq_ai_bot/services/main_agent_backend.py:836–849` 无本地隔离。合成已确认：ok=True/mutation_committed=True的结果遇fake disk-full，render直接抛OSError。
- 现错边界：本实验证明发布异常传播，不宣称已真实重复发送。同一调用链还存在Work已接受fallback仍raise、Code Mode归档派生失败。
- 删除：不要为归档故障制造第二条失败业务结果。唯一效果owner仍是原accepted/unknown回执；结果层返回“业务效果已知、完整资料暂不可读”的有界receipt，缺失artifact显式标明。原始权限/来源/取消/真正unknown不是可吞异常。
- 验收：disk full/坏文件/GC竞争/取消/授权撤销/原effect已知与未知；成功不重做、unknown只查原ID、诊断失败不阻塞。与runtime条目共用设计，不各建补偿队列。

#### Runtime收尾边界

**真实链**：Code `_dispatch_child:680–706` 已得到原 child receipt；`_settle:875–887` → `_result_view:989` 或 `_bounded_result:928–931` → `MainAgentBackend.archive_code_result:410–435` → artifact store。这里的 OSError/容量错误无隔离，`_drive` 只捕获 `_Stop/CodeEngineUnavailable`，于是 Runner 527 → supervisor 151 暂停整个 Work。fake 探针两归档入口均确认向外抛。

`WorkSession.execute:1827–1866` 已捕获 typed outcome 时会写 accepted fallback（含 result_unavailable/replay_forbidden），随后仍无条件 raise。现有 `Pi:tests/unit/test_work_effect_results.py:227–250` 明确测试的是“抛但保留事实”，不是“主循环可继续”。通用 results.render 也存在同一条边界；合并一个实施包，不报告成三个独立缺陷。context组 CTX-02 对 `_finish_memory_turn`/Work续跑归因收尾的派生失败另有探针；该尾部也应遵循“事实先定、诊断不反裁决”，但归因必须保留的事实与可丢诊断由CTX-02按阶段负责，不能在Runtime总catch里一律吞。

**删除对象**：可选展示/归档失败升级成运行故障的路径；重复的 tool/Code/parent preview 截断与归档责任由RES-01确定的唯一呈现职责承接。Code driver 只汇总原 operations/outcome，持久原事实成功后给模型明确有界“结果原文暂不可用”回执。删除第二次可选 archive；不得增加万能 catch wrapper。

**保留**：原 journal/effect durable commit 失败必须阻断；actual output 无法保存不伪装 complete；权限、privacy/generation、取消、原业务 unknown 仍传播。不能用 broad except Exception 吞整个 WorkSession 调用。若 archive 是任务本身指定交付而非展示副本，它仍是业务结果，不能降级为诊断。

**验收**：子 mutation accepted→展示 OSError 时 paired 结果仍返回、effects不降级、业务不重跑、下一模型有原证据；归档缺失明确暴露；原 effect writer失败/unknown/取消仍停止。与RES-01的预算信封及页读取回归联测，同一变更不重复计算收益。

#### 已送达后仍需必需压缩的边界

第三方转发案例的3个confirmed发送分片属于原发送事实，后续必要压缩失败不能撤销或重发它们；分片transport receipt与observation sent_messages条数是不同口径，3与1不单列缺陷。已成功发送也不是整轮目标完成证明；完整请求仍超硬容量时继续阻断新主请求，不超窗派发。该错误不属于本任务可忽略的可选preview/archive失败；按CTX-04既有fit fallback或明确capacity结果处理，不新增补偿/重试框架。


### FILE-01 迁附件不可变快照后删除可变 artifact 写入口

- 实施批次：P2
- 本版变化：第三轮独立复核后修正
- 唯一职责所有者：FileWorkspace可变文件；WorkspaceStore不可变快照
- 真实调用方：模型workspace工具、control.workspace.mutate、UI历史下载
- 编码前置：无
- 协调边界：保留immutable snapshot+checkout真实附件导入，删Store.write可变路径；不得用4MiB upload替换200MiB能力。
- 实施或发布条件：保留200MiB附件能力和真实checkout；新control合同退出旧写，历史快照与必要隐私删除保留
- 固定源码：main:src/qq_ai_bot/workspace/service.py:110–133；main:src/qq_ai_bot/workspace/store.py:390–464；main:src/qq_ai_bot/persistence/control_workspace.py:225–250

现有可变artifact仍有真实附件caller，不能整删后才发现入口失效。main/Pi:src/qq_ai_bot/workspace/service.py:110–133 的 save_from_inbound_attachment 经 WorkspaceStore.write 和 checkout 导入环境；main/Pi:src/qq_ai_bot/conversation/media_service.py:40 允许200MiB，main/Pi:src/qq_ai_bot/workspace/files.py:23 的 workspace_upload仅4MiB。改成upload会静默缩小现有能力。

**唯一事实和删除闭包**：FileWorkspace继续拥有可变path/CAS；WorkspaceStore.snapshot拥有不可变artifact快照；ToolArtifact仍是短期工具结果。附件改用现有 snapshot(fd, name, artifact_id=...) 再经现有checkout，保留200MiB、流式/文件描述符路径和来源/权限边界。main/Pi:src/qq_ai_bot/workspace/store.py:390–464 已写 artifact_snapshots；不要新建附件表、兼容service或第二导入owner。

**稳定身份必须核输入**：snapshot在同ID存在时直接返回旧项（main/Pi:src/qq_ai_bot/workspace/store.py:400–409），不能把request_id机械映成UUID就当幂等完成。复用原操作身份时必须核对该操作绑定的来源、bytes/hash和name；同请求同内容复用原snapshot及import回执，同请求不同内容明确冲突，不另建artifact再让checkout兜底。两树离线探针均证明4MiB+1附件首入成功；原request重入创建第二个可变artifact后checkout冲突（2 artifacts、0 immutable），而upload拒绝过大。snapshot新行为仍待实现验收。

**控制面退出**：main:src/qq_ai_bot/control_plane/surface.py:46 的 mutate_workspace，经 main:src/qq_ai_bot/persistence/control_management.py:199–224 和 main:src/qq_ai_bot/persistence/control_workspace.py:225–250 仍公开edit/upload/delete。新版本取消旧artifact edit/upload；live编辑迁现有mutate_environment_file，显式发布走workspace_publish。删对应surface/capability/command/service/port/parser、management旧写校验、WorkspaceService/Tools/Store.write可变分流，以及 main:src/qq_ai_bot/persistence/control_workspace.py:49–70 的逐项pop旧schema补丁。最后一个附件/控制caller迁完才删write；checkout有真实导入caller，保留。

历史read/download/send_message.artifact_id及必要隐私删除保留；delete live path不能冒充删除snapshot。旧saved_import_failed只按原artifact/import事实展示和结算，不再生成两个可变事实。前端历史快照页与环境编辑页按各自owner保留，新合同明确拒绝旧公开mutation，无永久shim。

**验收**：0/4MiB/4MiB+1/200MiB边界、重复同request、同request换内容/名字、stream失败、checkout失败恢复、同名快照与live不串写、原snapshot内容不变、file CAS冲突、无环境仍读历史、未知写查原ID不重做、隐私撤销不绕过。保留现有边界不等于本轮已完整测试200MiB。

Automation还有真实公开caller：main:src/qq_ai_bot/social/automation.py:102–139,146–181 登记workspace能力，并使用workspace:{run_id}:{step_id}。旧workspace.write schema和已存DSL须版本化迁移，新写改path，旧已接纳artifact编辑只查原效果，不静默改解释、不重写原hash。附件的稳定invocation/request身份必须提前到writer前；main:src/qq_ai_bot/services/agent_tools.py:989–992 与 Pi:src/qq_ai_bot/services/agent_tools.py:1026–1028 未显式传request_id，不能沿用附件早返回前uuid4分支。


### TERM-01 删除旧一次性 Manager 引擎和 run_python 新提交链

- 实施批次：P2
- 本版变化：第三轮区分旧独立jobs与persistent run_python三类退出
- 唯一职责所有者：唯一PersistentManager；FileWorkspace工作文件；WorkspaceStore显式快照
- 真实调用方：persistent继承的公用方法、client、CLI启动、部署验证、sandbox测试、旧jobs/outbox
- 编码前置：无
- 协调边界：FileWorkspace和workspace_publish已存在；FILE-01/DEP-01是同发行物联测而非旧引擎删除所需新机制。
- 实施或发布条件：正式版本迁移旧部署；历史run只结算不重做；不永久保留旧CLI fallback
- 固定源码：main:src/qq_ai_bot/sandbox/manager.py:143–173；main:src/qq_ai_bot/sandbox/manager.py:206–500；main:src/qq_ai_bot/sandbox/manager.py:532–544；main:src/qq_ai_bot/sandbox/persistent.py:1090–1105

`deploy/sandbox/yuki-sandbox.service` 已传 `--persistent-home`；`main:src/qq_ai_bot/sandbox/manager.py:532–544` 却仍允许参数缺失时启动旧 Manager。`PersistentManager(Manager)` 真正继承 DB/outbox、command、finish/get/wait、socket serve，与原 request 查询；覆写自己的 recover/worker/handle，`main:src/qq_ai_bot/sandbox/persistent.py:1101` 只把余下查询/outbox方法回落到 super.handle。因此不能直接删整文件，但可以删旧产品引擎而非只退 run_python 的名称。

**删行候选**（源码跨度，不是净 diff）：`manager.py` 旧 `recover:143–173`、`docker_args:252–313`、`stage_workspace:314–358`、`execute:359–448`、`cleanup:449–460`、`worker:461–500`，AST合计274行函数正文跨度；另删 handle 的 run_python新接纳 `:206–250`、CLI旧 fallback/旧 shutdown分支、LABEL/python-v1旧配置引用。把仍用的少量 DB/outbox/socket方法直接归到唯一 PersistentManager 或现有适当owner，删除旧基类身份，不新建 LegacyManager 抽象保留它。

**真实剩余调用者**：persistent继承上述公用方法；`tests/unit/test_sandbox.py`、`tests/support/sandbox_completion_cases.py`、`tests/unit/test_workspace_storage_latency.py` 实例化旧Manager；部署离线检查 `deploy/sandbox/validate_runtime.py` 等要用唯一persistent fake/集成 fixture。部署 systemd 不是旧模式用户。旧显式 CLI 无 persistent-home 是公开启动契约，下一版本强制 required 并给迁移错误，不继续 fallback。

**配套退 run_python**：与TERM-01合并而非另计一个换名任务。`client.py` 旧schema/序列化、persistent的code.py/input拷贝/自动export_python删除，新Python工作走terminal_exec+workspace_publish；保留终端执行限额。旧jobs.sqlite3、completion outbox、environment_jobs 的原run必须可查询/结算，shared get/ack不能删；排空旧接受任务后移除自动导出分支。workspace已发布快照不随任务清理删除。

**验收**：旧启动参数拒绝且不创建容器；fake Docker捕获不存在 yuki-python 新提交；persistent命令/终端/包/服务仍工作；unknown只查原ID；重复ack、outbox背压、重启及容器generation变化；旧jobs读取和workspace快照访问；不得通过复跑产物生成来迁移。

#### 部署语义迁移边界

旧Manager为逐job只读容器，PersistentManager为长期可写环境；必须在版本升级说明中明确这项真实变化、输入准备、执行时限、TTY和显式workspace_publish。确认受支持旧部署的迁移路线是发布前置，不能反过来把私有旧引擎永久保留。保持普通Agent原sandbox.run权限与调用主体，不能借WebUI operator权限执行。已接受run按原ID查回；旧自动发布只覆盖原任务收尾，排空后删。

#### 三类已接纳执行分别收尾

main:src/qq_ai_bot/sandbox/manager.py:143–155 的旧jobs无environment_jobs；main:src/qq_ai_bot/sandbox/persistent.py:339–345 只将它们标迁移/中断。必须确认旧容器真实已停止并记录原结果，DB failed不证明外部代码停止。现有persistent run_python若dispatched=false，仍经main:src/qq_ai_bot/sandbox/persistent.py:447–469,496–507 的prepare_spec和staging；若已dispatched成功，main:src/qq_ai_bot/sandbox/persistent.py:377–425,551–576 仍需export_python按原run/path发布输出。

默认先停run_python新接纳，在旧版排完或明确退役上述cohort，再删staging/export与旧独立引擎。不得先删除恢复函数又承诺在途原run可恢复；确需跨版本时只临时保留该cohort必要收尾，不能开新admission或另造LegacyManager。jobs原request/payload_hash/result、get/get_by_request/list/ack和completion outbox还供新终端复用，长期reader保留，不按24小时或“近期无调用”猜删除期限。未知原效果不重跑，不重新生成输出冒充原产物。


### TERM-02 终端能力常驻并冻结工具合同

- 实施批次：P1
- 本版变化：第二轮补强原任务
- 唯一职责所有者：部署时冻结的工具合同
- 真实调用方：terminal exec/read/write/control与environment_status
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：用户目标已明确，固定声明集合在新合同内实施
- 固定源码：Pi:src/qq_ai_bot/codemode/tool_visibility.py:14–59；Pi:docs/architecture/tool-kernel.md:1–75

Code的目标是低频工具仓。terminal_exec、terminal_read、terminal_write、terminal_control和environment_status保持固定直调，终端输入、中断和退出不依赖VM。当前Pi exec/read/status已直调，write/control仍隐藏，本任务把后二者迁入固定集合并同步tool-kernel文档。

低频能力继续从Code可达，不按lookup动态扩schema；direct模式自然声明全部获准终端能力。冻结新合同revision，旧已提交请求原样恢复，合法新链才用新集合。权限、工作环境owner、原request/run_id和预算不变；未知执行查原run，不因目录变动重发。

验收：exec/read/write/control/status在Code停用或worker耗尽时仍经原Host路径可用；write/control使用现有主体和sandbox授权；主Agent、child、SELF、Automation工具子集正确，不能因常驻把原无权者升级。

### CTL-01 删薄别名、合并合同来源，拒绝用万能dispatch替代授权

- 实施批次：P2
- 本版变化：第二轮补强原任务
- 唯一职责所有者：既有control surface明确合同；query/command执行授权
- 真实调用方：HTTP名单/分派、frontend api method、config aliases
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：不以getattr万能分派替代白名单；前端生成/校验复用现有合同
- 固定源码：Pi:src/qq_ai_bot/admin/config_owners.py:13–30；Pi:src/qq_ai_bot/control_plane/surface.py:31–144；Pi:src/qq_ai_bot/webui/http.py:45–87；Pi:frontend/src/api.ts:1–24

- 可立即删：P `Pi:src/qq_ai_bot/admin/config_owners.py:13–30` storage_scope_id两个同值property，真实最后调用者只有 `Pi:src/qq_ai_bot/admin/config_service.py:1948,1950`，直接用person_id/space_id；保留canonical解析本身
- `control_plane/contracts.py`和`__init__.py`是重导出面，不是重复DTO。不要为了“DTO去重”把它们错误记作第二套类型；可迁内部import至唯一模块、移除仅内部薄重导出，但公开入口若有外部调用须有退出条件
- 真重复维护：P `Pi:src/qq_ai_bot/control_plane/surface.py:31–144`手工method→capability；`Pi:src/qq_ai_bot/webui/http.py:45–87`再列special-method减集，后面巨型if手工字段校验；frontend `main:frontend/src/api.ts:1–24,121–136`使用泛型Row和任意method字符串。建议复用现有surface为单份明确审定method/request/response合同，生成/验证前端类型与已知method调用，删重复方法名单和页面局部字段猜测；不要增加compatmanager或任意getattr万能转发
- 保留：query/command service身份/当前权限校验；Pi:src/qq_ai_bot/control_plane/wire.py:44–89拒绝未审DTO/ORM并保存原request ID；exact type在此是信任边界而非“强制异常都可删”
- 验收：frontend所有query/command字符串与surface逐项匹配、错字段/错DTO拒绝、元信息与正文权限独立、CSRF/凭据轮换、unknown不变成失败重发、read/download snapshots；浏览器交互本次调查未跑

#### 方法目录删除闭包

**重复派发删除**：`surface._METHODS`、`main:src/qq_ai_bot/webui/http.py:45–87` 手工special减集、`:266–529` 巨型参数分派与Service方法的method_capability字符串重复维护。把已审request shape/parser与handler归入现有surface明确定义，删HTTP第二份名单/重复字段解析，并校验所有命令/查询都经过原授权服务。只允许显式descriptor绑定，禁止任意getattr暴露未登记方法。`wire.py` reviewed DTO拒raw dict/ORM是边界，不当作多余序列化删掉。query服务含正文/外部ID二次权限，不能把整个query_service当薄代理删除。

### DB-01 删除两条没有真实 caller 的运行时兼容面

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：Database及原定义Repository模块
- 真实调用方：包根旧导出、MediaAnalysisRepository.save_analysis
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：repositories.py仍32个src导入不可直接删；保留子模块导入
- 固定源码：Pi:src/qq_ai_bot/persistence/__init__.py:1–59；Pi:src/qq_ai_bot/persistence/media_repository.py:190–193

**对象与调用证明**
- `Pi:src/qq_ai_bot/persistence/__init__.py:1–59`：TYPE_CHECKING、`__all__`、`__getattr__`、`__dir__`整套旧包根导出。AST 遍历 src/tests/scripts/migrations 的所有 Python，唯一产品 `from qq_ai_bot.persistence import ...` 是本文件自引用 repositories；测试只导入 `sqlite_diagnostics`/`diagnostic_writer` 子模块。这些正常子模块导入不依赖 `__getattr__`。
- `Pi:src/qq_ai_bot/persistence/media_repository.py:190-193`：`MediaAnalysisRepository.save_analysis(**values)`只转发save。全仓同名调用在 emoji/lifecycle 与 test_emoji_claim_fence，但其 receiver 是另一种 `EmojiRepository`，不是 MediaAnalysisRepository；真正视觉路径 `Pi:src/qq_ai_bot/services/vision_service.py:549,694`调用save。
- `persistence/repositories.py`本身有32个src导入文件，不能宣称无caller直接删除；若要取消barrel，应一次机械改显式模块导入再删除，不另造新barrel。

**删除/剩余owner**：包根保留空package/docstring；Database在database.py，仓库/Record在原定义模块；save保持唯一媒体写入口。不触及任何DB表、迁移或索引。
**异常后果**：目前只是多余入口/懒加载间接依赖，无已复现业务错误。删除后未知旧名字自然ImportError；这是退出旧内部API，不改SDK合同。
**验收与反例**：在隔离进程导入metadata、database、application组装，确认无循环；上述3个子模块导入测试仍通过。不能把package改成 eager repositories导入，反而复活文档明确的ORM循环。再扫动态import/getattr字符串及仓内插件；仓外未授权直访私有ORM不构成保留SDK承诺。

### DB-02 合并UserProfile兼容类；删artifact空catch

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：PeopleRepository.observe；artifact原事务
- 真实调用方：UserProfileService及装配、artifact注册
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：先迁真实UserProfile caller；删空catch不能加失败unlink
- 固定源码：Pi:src/qq_ai_bot/persistence/people_repository.py:1120–1148；Pi:src/qq_ai_bot/tool_results/artifacts.py:246–292

- `Pi:src/qq_ai_bot/persistence/people_repository.py:1120-1148` `UserProfileRepository(PeopleRepository).upsert`是完整转发。它**有caller**：Pi:src/qq_ai_bot/application/modules/persistence.py:45,60,122及services/user_profiles.py:20,76,176；tests/conftest和profile测试也实例化。先把装配/类型改为PeopleRepository、service.upsert改observe、测试同步，再删整个子类和barrel旧导出。剩余owner是PeopleRepository.observe，参数语义不变，无schema迁移。
- `Pi:src/qq_ai_bot/tool_results/artifacts.py:246-292`的try/except BaseException只裸raise。删try/except及缩进，保留“提交可能已经成功、由orphan维护回收”的解释在事务旁。**不得**据此加失败unlink；取消可发生在commit成功之后。
- 验收：profile空值缓存、expected_person_id不匹配、bot身份、未知名片不覆盖原值；artifact注册失败/提交后取消仍留文件、已注册ref绝不被回滚清理误删。现有 `test_tool_artifact_orphans.py`和profile测试为回归入口，本次调查未全跑。

### ART-01 image artifact删除第三轮授权读取，保留I/O前后两轮

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：artifact read前与最终post-I/O授权
- 真实调用方：read_tool_artifact image出口
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：保留最靠近返回的一次post-I/O guard；session退出有await
- 固定源码：Pi:src/qq_ai_bot/tool_results/artifacts.py:380–483

- `Pi:src/qq_ai_bot/tool_results/artifacts.py:380-398`第一轮；`424-441`通用I/O后轮；`467-483`image专属I/O后轮。两轮后检之间主要是JSON解码、image与manifest构造，但异步session退出也可能await；不能把这段描述为绝对无竞争。第三轮另开会话并执行三次SELECT。
- 真实链：ToolResultBudgeter媒体归档 → read_tool_artifact/validate_media → ToolArtifactRepository.read。合成1张有效图片实测9 SELECT：tool_artifacts/canonical_conversations/execution_trace_state各3次；未过期，无work保护查询。
- 删除/合并：统一保留一次最终post-I/O（必要时post-decode）guard，所有文本/manifest/image出口共用；删第二份相同条件块以及已被398守卫覆盖的443-444 access=None拒绝。不加缓存、不去掉privacy/generation/actor/scope/expiry/deleting。
- 剩余owner：`_authorized`及read单个最终guard；读前防未授权读盘、读后防文件I/O期间reset/forget。依赖不可变artifact文件名/内容。
- 验收：有效image从9降到6 SELECT；过期受保护work、外人scope、generation变化、privacy erase、GC tombstone、文件损坏均原样拒绝/保留。用读文件barrier在I/O中改变generation/隐私必拒；不得只保留读前检查。多进程并发不存在“最后一次查询后永远不会变”的保证，不以不断加同样检查构造伪原子性。
- 收益：删除整块重复复核，每image少3 SELECT和1会话；实测只证明当前成本，尚未实现/验收新数值。

### DB-03 媒体UPSERT用RETURNING替代写后按同一唯一键重查

- 实施批次：P2
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：UPSERT唯一约束及RETURNING
- 真实调用方：vision媒体save、emoji save_many
- 编码前置：无
- 协调边界：媒体UPSERT不调用PeopleRepository或DB-02 artifact清理。
- 实施或发布条件：commit成功后才返回；不改source_event_id与累计字段
- 固定源码：Pi:src/qq_ai_bot/persistence/media_repository.py:175–188；Pi:src/qq_ai_bot/persistence/media_repository.py:385–430

- `Pi:src/qq_ai_bot/persistence/media_repository.py:175-188`执行UPSERT后SELECT同一复合键再硬throw“did not return a row”；emoji save_many `385-430`对每个key重复此模式。真实caller：Pi:src/qq_ai_bot/services/vision_service.py:549,662,694。
- 删除：两处重复键条件SELECT与人为不可能分支；原UPSERT加RETURNING模型/所需列，一次接收结果。SQLite RETURNING已在仓内artifact GC使用，不增适配层。
- 不能删：media首次source_event_id归属不变的set_排除、完整unique key、JSON禁止内联像素、emoji已累计hit_count/created_at不被更新。
- 净收益：每媒体保存少1查询，每N个emoji key少N查询；少两份唯一键副本及硬throw。remaining owner为SQL UPSERT唯一约束及现有_record。
- 验收：insert与冲突update返回完整Record，原source_event_id、emoji created_at/hit_count保持；两个并发写入按最后提交语义不重复行。RETURNING结果只在commit成功后返回给caller，commit失败不能返回成功。
- 成本：测N=1/4/32；记录SQL次数、首DML至真实commit，非网络benchmark。不为小DTO转换新增线程协议。

### ART-02 GC保留deleting，但合并每条文件后的独立DELETE事务

- 实施批次：P2
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：现有deleting tombstone与ref保护
- 真实调用方：artifact cleanup每页至多128
- 编码前置：无
- 协调边界：GC原tombstone可局部批删；与CTX-01 ref释放同组验收，不增加新GC/coverage机制。
- 实施或发布条件：只批删成功unlink且deleting的原handle；坏路径不盲删
- 固定源码：Pi:src/qq_ai_bot/tool_results/artifacts.py:611–661；Pi:src/qq_ai_bot/tool_results/artifacts.py:70–127

- `Pi:src/qq_ai_bot/tool_results/artifacts.py:611-661`候选每页≤128；631-641一次写事务标记deleting；642-650锁外逐文件unlink；651-660每文件另开BEGIN IMMEDIATE/DELETE/COMMIT。
- 删除对象：逐row writer lifecycle。先收集成功unlink的原handle，在一次短writer里按 `handle IN (...) AND deleting=true`批删；仍≤128，不增加持久状态。每页从1+N个writer可变成2个（只统计该cleanup页，不含orphan读）。不增加跨页事务。
- 保护依赖：`add_refs:101-127`拒绝deleting；`_protected:70-98`检查live/recent work、root和refs；schema.py引用FK CASCADE。`Pi:src/qq_ai_bot/conversation/observation_schema.py:38,67,95`负责释放observation refs，不能把ref表当缓存删；summary和work_note由各自领域owner管理。
- 正确反例：unlink失败的row不删；进程在unlink后DB提交前退出，下轮deleting会继续；并发加ref在tombstone前成功则保护本次调查不被选中、tombstone后明确拒；已deleted文件不触发重执行。
- 无效relative_path当前continue且永远保留deleting；需显式管理修复/隔离事实，不能“为干净表”盲删异常记录，也不能访问root外路径。此类坏数据未在真实库验证。
- 验收入口：`Pi:tests/unit/test_work_effect_results.py:139`、`test_tool_artifact_orphans.py`、`Pi:tests/unit/test_context_note_artifact_lifecycle.py:289`；检查无候选零writer，128页中某文件失败，最后ref释放、active/root work保护和7日终态窗口。不重新跑#263。
- 收益：删除逐文件写会话/commit开销；保留一份现有tombstone协议，不换GC框架。真实等待耗时未测，不声称延迟下降多少ms。

### DB-04 删SQLAlchemyError→STATE_MISMATCH泛化，诊断删文本locked猜测

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：已有系统错误边界及SQLite可核验code
- 真实调用方：control command→HTTP；sqlite诊断
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：5xx不授权重跑；原request receipt查证继续保留
- 固定源码：Pi:src/qq_ai_bot/persistence/control_command.py:1109–1110；Pi:src/qq_ai_bot/persistence/control_command.py:1169–1170；Pi:src/qq_ai_bot/persistence/sqlite_diagnostics.py:317–338

**控制命令错误**
- `Pi:src/qq_ai_bot/persistence/control_command.py:1109-1110`任何SQLAlchemyError变ControlCommandError(STATE_MISMATCH)；`1169-1170`恢复路径同样吞cause。WebUI `Pi:src/qq_ai_bot/webui/http.py:95,179-183`映射state_mismatch到409。
- 调用链：ControlCommandAdapter._execute → database.immediate_session/mutate/commit →广义catch→HTTP409。磁盘IO/SQL拼写/内部驱动故障和真实状态冲突不可区分，用户被误导“冲突重试”；这不是已证明可以重发外部效果。
- 删除广义转换，让未识别DB故障到已有系统错误处理；边界日志仅记录类别/可核验code，响应不得含SQL参数。保留明确_CachedFailure、_map_integrity及原request receipt查证；不删savepoint（领域拒绝之前可能已有部分写入，必须回滚再记failure）。冻结ProblemCode不随意增新枚举；若要调整transport5xx呈现，限定现有HTTP边界，不建通用错误框架。
- 验收：合成disk I/O/code10、missing table/code1不返回409；BUSY/code5、LOCKED/code6分开，不盲重试；真实expected_revision和known唯一约束仍409；original request_id查回receipt不重复mutate；commit确认丢失走原查证，不由“5xx”推导可重跑。

**SQLite诊断**
- `Pi:src/qq_ai_bot/persistence/sqlite_diagnostics.py:317-338`即使有明确非BUSY/LOCKED code，只要异常字符串含locked仍记`sqlite_write_contended`。隔离执行 `SELECT * FROM locked_missing_table`得到sqlite_errorcode=1却contended=true（probe已验证）。这是观测误归类，不是运行时重试（必须区分）。
- 删除`"locked" in str(error)`fallback，只有可核验primary code 5/6报告对应类别；未知code记unknown/database_failure。已有 `Pi:src/qq_ai_bot/runtime/activation_outcome.py:139-156`与`Pi:src/qq_ai_bot/runtime/lease_heartbeat.py:47-48`正确区分，**不改成同样文本启发**。
- 收益：删两处广义业务冲突转换及一个文本启发分支，保留异常源。依赖：与control边界测试联动；无需新SQL分类框架。

### CTL-02 控制面上传预检移出writer，并删第二次解码

- 实施批次：P1
- 本版变化：第三轮保留旧回执优先于新请求验证
- 唯一职责所有者：调用局部纯准备；writer当前授权/receipt
- 真实调用方：control upload/arguments与external阶段
- 编码前置：无
- 协调边界：既有ExternalControlExecutor足够前移纯准备；FILE-01删除旧artifact上传后只保留environment上传相关工作，避免删/改同一死路径两次。
- 实施或发布条件：纯decode前移；并发撤权和revision仍writer复核
- 固定源码：Pi:src/qq_ai_bot/persistence/control_external.py:117–172；Pi:src/qq_ai_bot/persistence/control_workspace.py:98–120；Pi:src/qq_ai_bot/persistence/control_workspace.py:225–228

- `Pi:src/qq_ai_bot/persistence/control_external.py:117`先BEGIN IMMEDIATE，`171-172`调用validate_external；`Pi:src/qq_ai_bot/persistence/control_management.py:212,231`进入`control_workspace.upload/arguments`；`Pi:src/qq_ai_bot/persistence/control_workspace.py:98-104,108-120`做base64解码/大对象复制。后续external阶段`Pi:src/qq_ai_bot/persistence/control_workspace.py:225-228`又upload解码一次。
- 边界数据：artifact上传上限640KiB（Pi:src/qq_ai_bot/persistence/control_workspace.py:32），environment上传上限4MiB（Pi:src/qq_ai_bot/workspace/files.py:23），不是“小型必要JSON”。HTTP更低限制若存在会缩小实际可达范围，但不改变锁内纯准备重复这一事实；不声称实际观察到4MiB生产请求。
- 删除/合并：在首次writer前做纯格式/大小/解码准备一次，用本调用的局部值传给执行（复用已有tuple/dict，不落新表、不增任务状态）；writer仍先按原request和payload hash读取既有receipt，再做新请求的当前授权、revision和意图注册。纯准备可在writer前计算，但任何准备异常先保存在本调用局部值；只有锁内确认不是合法旧receipt回放后才抛出。旧已接纳请求不能因今天schema/尺寸规则变了而在回放前被新校验拒绝。准备本身绝不授予执行权限。若结构需小幅变更，可先将纯验证前移、证明重复解码边界后再合并，不把不必要DTO设计成项目。
- 验收：0/640KiB/超限与非法base64；4MiB environment upload按实际HTTP入口上限测试；事件hook证明decode发生在BEGIN前；并发撤权/revision改变仍拒；已存在receipt不得重新上传，且旧payload按当前规则已不合法时仍先按原hash回放；同ID不同payload仍冲突。外部网络/文件执行目前位于226之后、无session存活，该正确边界保留。
- 收益：移除锁内纯准备调用、一次重复artifact decode，消除大型字符串/字节的重复生命期；持锁ms需隔离测，不虚报。

### DB-05 web来源入账删writer内URL/标题准备与无界历史物化

- 实施批次：P2
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：原web provenance事务及维护owner
- 真实调用方：agent_tools与chat web来源保存
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：严格max_runs还是有界维护需确认；来源不是可丢遥测
- 固定源码：Pi:src/qq_ai_bot/persistence/web_repository.py:66–130

- `Pi:src/qq_ai_bot/persistence/web_repository.py:66-94`归属查证先于flush（正确）；但flush后`95-119`逐source normalize URL、split/join title；`120-130`offset(max_runs).all()将所有旧run id物化，再拼IN删除。caller：Pi:src/qq_ai_bot/services/agent_tools.py:2983-3003及chat.py:1429-1445。
- 删除/合并：URL规范化、去重和截断在首次DML之前一次准备；run/source入账保留同事务。删“每次保存同时清理全量旧history”的耦合，优先用现有维护owner按有界候选回收；若产品必须严格max_runs，把SQL子查询删除留在同事务但明确可能大删除，不能谎称有界。方案二选一需先确认max_runs是否严格承诺，不增加第二份水位。
- 安全：联网来源不是纯可丢诊断；trigger_event_id/execution_id及canonical归属不能弱化，不能在source保存失败后伪造有来源回答。过期删除不能清掉仍被当前回复引用的run。
- 触发范围：来源N、该conversation历史H、max_runs骤减；默认常态可能仅删1条，不夸大。测N=1/20/100与H=10/1000/10000，SQL计划/返回ID量和writer真实commit分开。max_runs变化/历史积累是大扫描反例。
- 收益：删重复锁内正文转换和history id列表（选定有界维护路径时），不新增缓存；保留现有index `ix_web_search_runs_conversation_created`（Pi:src/qq_ai_bot/persistence/models.py:1880）。本次调查仅静态确认，未做大性能benchmark。

### MIG-01 冻结历史迁移，不删历史兼容升级逻辑

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：冻结历史revision各自DDL
- 真实调用方：Alembic旧库升级
- 编码前置：无
- 协调边界：冻结0061/0059/0089可独立实施；唯一head与生产谱系留发布门。
- 实施或发布条件：只移除可变runtime依赖；不删除旧升级/预算保留逻辑
- 固定源码：Pi:migrations/versions/0061_self_reflection_cycles.py:13–24；Pi:migrations/versions/0089_context_observation_sources.py:24–33

- `Pi:migrations/versions/0061_self_reflection_cycles.py:13-24`直接导入当前ORM两个Model并create，违反冻结迁移不依赖可变ORM；运行定义 `Pi:src/qq_ai_bot/persistence/models.py:1226-1252`会随产品变。应删除该runtime ORM依赖，迁移只声明0061当时拥有的表结构；允许少量必要冻结DDL增加，这是减依赖，不冒充净删行。
- 0059导入signals/work_recovery_schema、0089导入PROJECTION_TRIGGERS_CURRENT也要固定到当时合同，尤其0089把CURRENT作为旧触发器校验指纹（:24-33）并在downgrade还原；不能以后更新CURRENT把旧库合法升级变成unexpected legacy trigger。0056 work_schema_v1、0060 automation_budget_schema明确版本冻结，并非名字里有旧版就删。只引用Base不等于整个可变ORM表都可用；本次调查未证明每个schema导入已产生现存升级失败。
- 不能删：0059 reconcile_legacy_progress累计MAX预算、归属歧义suspend；0060旧Host owner缺失suspend与creation_call唯一围栏；0089存在observations/selections/refs就拒绝downgrade；0097退休旧MCP状态；0098 representation冻结与INT-01统一处理。它们真实caller是Alembic升级链，runtime rg零引用不是无caller证据。
- 验收：离线空库→head，至少0058/0060/0088旧库→head；每个历史revision停点结构与原合同一致、后续schema变化不影响旧停点。迁移只动自身对象；保留原预算/回执/来源/文件引用。不可为净删把线上库重建成当前ORM或恢复旧备份。

### APP-01 删空的 application ProviderRegistry，而非另建 registry

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：显式application bundle依赖
- 真实调用方：runtime_foundation创建空ProviderRegistry、container.freeze
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：不删活ToolProviderRegistry
- 固定源码：Pi:src/qq_ai_bot/application/provider_registry.py:1–66；Pi:src/qq_ai_bot/application/modules/runtime_foundation.py:48–71

**源码/真实 caller**：`Pi:src/qq_ai_bot/application/provider_registry.py:1–66`；`Pi:src/qq_ai_bot/application/modules/runtime_foundation.py:8,48–71`；`Pi:src/qq_ai_bot/container.py:729`。全仓 src/tests/docs/scripts 符号搜索只找到创建、保存、freeze，没有生产 register/get/names；`services/main_agent_backend.py` 的 `_provider_registry` 是 **ToolProviderRegistry**，不是此类，不得连带删除。

**动作**：整删 66 行 ProviderRegistry；删 RuntimeFoundationBundle 的该字段、构造参数/成员及 container 的 freeze；删除只服务它的 `Pi:src/qq_ai_bot/runtime/errors.py:22–27` 两类异常。TurnAuthorityFactory/真实工具注册表原样保留。

**owner / 影响**：应用模块已通过显式构造参数和 immutable bundles 持有依赖；不需要新的 owner。无持久状态、无模型协议迁移。外部 Python 私有导入无法由仓库证明为零，应作为内部 API 退役在变更说明明确，不为假想调用方维持永久 wrapper。

**验收/反例**：容器构建、热模型切换、主工具合同冻结仍正常；确保 `ToolProviderRegistry` 未删。删除类和引用后 import/静态检查应没有残留。最终净删除量以实现后的diff记录。

### APP-02 删 ApplicationModule 空契约与纯兼容 exports；健康模块须区分

- 实施批次：P1
- 本版变化：第三轮纠正现场health归因，保留已存在端点分工
- 唯一职责所有者：现有LifecycleRegistry与必要package
- 真实调用方：ApplicationModule导出、包re-export
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：model_runtime lazy exports及health实际聚合保留
- 固定源码：Pi:src/qq_ai_bot/application/module.py:1–11；Pi:src/qq_ai_bot/application/__init__.py:1–6

- `Pi:src/qq_ai_bot/application/module.py:1–11` 的 ApplicationModule 只在 `Pi:src/qq_ai_bot/application/__init__.py:4,6` re-export，无使用/继承/类型注解。删文件和对应导出，保留实际 LifecycleRegistry；最终净删除量以实现后的diff记录。
- `Pi:src/qq_ai_bot/observability/__init__.py:1` 仅历史 docstring，src/tests 无该包导入。可删整个空包，但只有 1 行，不夸大收益。
- `Pi:src/qq_ai_bot/llm/__init__.py:1–37`、`Pi:src/qq_ai_bot/vision/__init__.py:1–35`、`Pi:src/qq_ai_bot/time/__init__.py:1–6`、`Pi:src/qq_ai_bot/emoji/__init__.py:1–25` 是聚合导出。对 src/tests 的 `from ... import` 搜索没有类型消费者；唯一 `from qq_ai_bot.emoji import db_models` 是 `Pi:src/qq_ai_bot/persistence/metadata.py:19`，属于子模块加载，应保留 package 本身。建议删未用 re-export，保留必要包标记。docs/scripts/examples 的字面导入补查亦无命中（export-extra-callers.txt）；仍需安装包 smoke 与动态导入检查后才能记为确定可删规模，本次调查未验证外部 Python 用户。
- **不能称 health.py 为空**：`Pi:src/qq_ai_bot/health.py:68–150` 实际聚合数据库、ASR、语音、memory、work 等；`main.py` 注册健康端点。`Pi:src/qq_ai_bot/application/lifecycle.py:89–101` health 将单组件异常转成明确 error_category；不是吞掉业务异常。`Pi:src/qq_ai_bot/model_runtime/__init__.py:1–67` lazy exports 有真实消费者且注明 SQLAlchemy startup 顺序，不纳入“纯兼容全部删”。

#### 健康端点的部署归因

main/Pi:src/qq_ai_bot/main.py:43–55 已区分无DB的/livez与显式综合/healthz；main:docker-compose.yml:44–52、Pi:docker-compose.yml:46–54 的10秒healthcheck都请求/livez。Memory一致性审计在main/Pi:src/qq_ai_bot/health.py:70 和main/Pi:src/qq_ai_bot/memory/audit.py:203–316。未知第三方案例现场部署不能归因为当前Compose每10秒全域审计。先核镜像SHA、有效health command及实际/healthz caller；若部署或其它monitor误用重端点，迁已有轻端点并保留主动诊断。不再造health/readiness/cache体系，不缓存旧healthy。


### APP-03 退役无 TOML 的 LLM_* 路由；保留 v3 TOML 的 environment indirection

- 实施批次：P2
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：ModelProfileCatalog与ModelRouter
- 真实调用方：ModelRuntimeModule、CLI、setup、quality和fixtures
- 编码前置：无
- 协调边界：现有injected_profiles/parser已存在；APP-04签名收敛共编排而非新设施前置。
- 实施或发布条件：清点TOML后退fallback；LLM_*环境引用仍保留
- 固定源码：Pi:src/qq_ai_bot/model_runtime/profiles.py:116–188；Pi:src/qq_ai_bot/deployment_setup/service.py:457–479

**真实入口**：`Pi:src/qq_ai_bot/config.py:147–166`、`Pi:src/qq_ai_bot/settings_domains.py:81–95`；`Pi:src/qq_ai_bot/application/modules/model_runtime.py:49–63` 和 `Pi:src/qq_ai_bot/cli.py:322–337` 将 opt-in 与十余 legacy 参数传给 `Pi:src/qq_ai_bot/model_runtime/profiles.py:116–188`；测试 `Pi:tests/conftest.py:113–116`、`Pi:tests/support/full_contract_fixture.py:14–17`、生产的离线质量工具 `Pi:src/qq_ai_bot/memory/quality/runner.py:596–599` 仍显式开启 fallback。

**需区分的三件事**：
1. `Pi:src/qq_ai_bot/model_runtime/profiles.py:135–188` 缺文件时构造 main profile 和所有 routes，是可迁移后删除的旧 runtime 路由。
2. `Pi:src/qq_ai_bot/model_runtime/profiles.py:29–39,293–329` / setup `Pi:src/qq_ai_bot/deployment_setup/service.py:256–283` 的 `base_url_env/model_env/api_key_env` 是规范配置，不是 fallback。向导今天仍写 `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL`；删除这些变量会破坏新安装。
3. `Pi:src/qq_ai_bot/model_runtime/profiles.py:225–291` 在 schema v3 下补缺失业务 route，也是仍可触发的兼容转换。应让已有 setup/config 文件升级显式写全 route 后收口为严格 parser；不能仅凭 schema_version=3 推定部署 route 全齐。

**删除顺序**：先把 fake tests/质量工具迁到现有 ModelProfileCatalog + injected_profiles（不另建 test manager）；setup/CLI migration 生成并验证显式 TOML → 记录退出 opt-in 的可核验条件 → 删 runtime fallback、legacy 参数与 compatibility_mode。已有 `Pi:src/qq_ai_bot/deployment_setup/service.py:113–122` 的“旧文件未迁移不覆盖”保护仍保留；它防止配置丢失，并非冗余。

**额外可以直接缩短的路径**：`Pi:src/qq_ai_bot/deployment_setup/service.py:457–479` 为验证字符串创建临时目录、写文件、调用 load，并填一堆不会用到的 legacy 参数。这里已有 `parse_model_profile_catalog(content, environment=...)` (`Pi:src/qq_ai_bot/model_runtime/profiles.py:200–307`)，可直接复用现有 parser，删除临时 I/O 与 fallback 参数（以实际diff核算）。“验证提交”的后续路径不因此变成无需落盘或无需凭据检查。

**owner / 规模**：ModelProfileCatalog + ModelRouter 继续唯一配置 owner；fallback 主体约 65 行，加 callers 以实际diff核算，需实际迁移后核算。route 补全约 67 行是单独条件项，不与 fallback 合并报成已可删。

**反例/验收**：缺 TOML 明确失败；存在 v3 TOML 且只用 LLM_* 引用仍可启动；指定文件缺失不悄悄读旧文件；生成配置包含全部 ModelTask；缺未知 route/profile 必须拒绝；旧配置迁移不覆盖原字节/API key sidecar。

### APP-04 把旧 provider 注入缩到测试，不让业务双签名永久存在

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：既有ModelExecutor及测试injected_profiles
- 真实调用方：chat、relationship、plugin session、memory、emoji构造
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：测试先遵守真实executor合同；不加fake manager
- 固定源码：Pi:src/qq_ai_bot/model_runtime/executor.py:279–362；Pi:src/qq_ai_bot/services/chat.py:387–391

`Pi:src/qq_ai_bot/model_runtime/executor.py:279–362` 的 LegacyTaskModelExecutor/require_model_executor 为测试注入而生，但 `Pi:src/qq_ai_bot/services/chat.py:387–391`、relationship_evaluator:122–126、plugin_sessions:76、memory/worker:96、emoji/replacement:40 仍接受 provider 与 executor 两套依赖。

**方案**：生产服务只接现有 ModelExecutor；测试用现有 pool.injected_profiles 或测试侧 fake ModelExecutor；删除业务 constructor 的 provider/model 备选以及 production LegacyTaskModelExecutor（84 行核心 + 各 caller）。要同时迁移所有服务入口调用方，不能独立改签名造成重复工作。

**保留**：pool 注入点仍有必要，用来测试实际路由/预算/协议与生命周期；不能把测试全改成绕开真实 executor 的万能假实现。`require_model_executor` 中的缺依赖 TypeError 随双入口一起删除，不是在依赖缺失时继续运行。

### APP-05 删除旧 delta 接口；旧 journal hydrate 不在此删除清单

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：有序continuation_items与原journal decoder
- 真实调用方：ChatRequest、Responses、capacity、旧测试
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：只删旧活入参；旧inline和protocol-object journal保留
- 固定源码：Pi:src/qq_ai_bot/domain/messages.py:390–391；Pi:src/qq_ai_bot/llm/protocol_state.py:26–29；Pi:src/qq_ai_bot/runtime/work_journal.py:49–93

**生产闭包**：`Pi:src/qq_ai_bot/domain/messages.py:390–391` 两个旧字段，`Pi:src/qq_ai_bot/llm/protocol_state.py:26–29` 与 `Pi:src/qq_ai_bot/llm/deepseek_responses.py:335–368` 双表示兼容。所有 src 写入点仅显式赋空（work_session:1263–1265、turn_execution:1178–1180/1193–1195/1277–1279、ordinary_compaction:95–97）；非空只见 tests 中老调用。`Pi:src/qq_ai_bot/model_runtime/capacity.py:99–104` 与 executor:571 也为旧字段维持分支。

**持久化证据**：`Pi:src/qq_ai_bot/services/turn_transcript.py:128–148` 输出顺序 items；`Pi:src/qq_ai_bot/runtime/work_journal.py:49–93` 编解码 `{chain_id,messages_count,items,continuation}`；`Pi:src/qq_ai_bot/runtime/protocol_store.py:382–389` 对非 protocol_objects_v1 的旧 journal 原样返回后交给相同 decode。离线 encode/decode 后是 FunctionCallOutput、ChatMessage 顺序，完全不经过旧两字段。

**动作**：迁 tests 到 continuation_items，然后删 ChatRequest 两字段、ordered_delta 的混合判定/fallback，Responses 复用现有 ordered_delta（或直接读唯一字段），删 capacity/清空字段分支。保留 checkpoint 的 provider/protocol/profile 绑定、类型检查与尾部 system/developer 不升格为顶层 instructions；这些仍保护真实 wire 合同。

**范围/验收**：以实际diff核算兼容分支 + 测试迁移；不删 FunctionCallOutput 类型、不删 Responses `_merge_continuation`、不删 journal fallback。对 old inline journal/new protocol object journal 都回放 3 轮工具配对、插入 user/developer、opaque 签名和图片顺序，确保 hash/预算正确。未读取部署中真实 journal，所以最终升级仍需脱敏格式样本覆盖，不声称所有历史格式都已证明无影响。

### APP-06 合并 DeepSeekResponses 与 JSONHTTP 的 transport，保留物理 attempt 语义

- 实施批次：P2
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：现有JSONHTTP physical attempt边界
- 真实调用方：DeepSeek/OpenAI Responses、self-reflection预算hook
- 编码前置：无
- 协调边界：现有JSONHTTP可承接attempt owner，APP-04/05分别是注入和请求字段，不必等其完成。
- 实施或发布条件：先parity验证hook、ReadError、usage与URL；不能直接换complete
- 固定源码：Pi:src/qq_ai_bot/llm/json_http.py:35–194；Pi:src/qq_ai_bot/llm/deepseek_responses.py:63–233

**重复位置**：`Pi:src/qq_ai_bot/llm/json_http.py:35–194` 与 `Pi:src/qq_ai_bot/llm/deepseek_responses.py:63–233,385–417`：client ownership、backoff、Work 辅助预算预留、dispatch guard、attempt计数、trace、HTTP error usage、异常转换均重复。OpenAI Responses subclasses `Pi:src/qq_ai_bot/llm/openai_responses.py:11–34` 也走 DeepSeek base，所以收益不只是一个 vendor。

**现有不等价点（已验证）**：
- Responses 在 `:141–170` 调 before/after_provider_request，真实消费者 `Pi:src/qq_ai_bot/memory/self_reflection/service.py:246–267` 写 initial/repair/transport_retry 的预算回执；JSONHTTP 没有此 hook。不是“重复 telemetry”可随手删。
- Responses 仅 catch/retry ConnectError/Timeout/5xx；JSONHTTP catch/retry TransportError/5xx。MockTransport 抛 ReadError 时前者原样 ReadError，后者 LLMUnavailableError。统一须明确保留/调整错误合同，不把 unknown 变成已知失败后盲重放。
- Responses raw usage 在 parse 前记录；JSONHTTP parse error 从 LLMError.diagnostics 取 usage。要确保 malformed response 仍留已报告数字，而缺失值仍 unknown。
- Responses `_post` 使用绝对 `/responses`；JSONHTTP 的相对路径和 base_url 尾斜杠语义不同。合并时必须锁定 base URL `/v1` 前缀行为，不能顺手“规范化 URL”造成协议路径变化。

**方案**：让 Responses 复用现有 JSONHTTP transport owner，保留其 payload、parse、DSML、continuation 专有逻辑；把物理 before/after hook 放到现有单一 attempt 边界，给具体协议保留路径/usage/render 方法。不要新增 UniversalBackend、第二层 request manager 或 mapper。先加异步 fakeHTTP parity，再删除重复 loop/_post。最终净删除量以实现后的diff记录。

**必须保留的反例**：native search 断连只发 1 次；local-only 5xx 有界重试且每次辅助预算+计量；reserve 拒绝零 HTTP；generation 在排队/重试间失效零新效果；401/400 不删参数重试；reported=0 与 unknown 分开；CancelledError 不改写为业务失败；self-reflection finish 失败属于 durable accounting 故障，不像可丢 trace 一样吞掉。

### APP-07 恢复每次派发的新鲜校验并删除纯配置重复

- 实施批次：P2
- 本版变化：第三轮独立复核后修正
- 唯一职责所有者：现有ModelProfile/prepare配置边界与真实attempt guard
- 真实调用方：web装配、热保存、模型retry
- 编码前置：无
- 子项前置：仅删除冗余attempt guard需APP-06
- 协调边界：每attempt新鲜检查修复独立，不等APP-06；仅删除外层dispatch guard子项须先由APP-06证明所有物理attempt owner覆盖。纯配置重复检查也独立。
- 实施或发布条件：重复调用不证明重复校验；新鲜性修复独立，删除guard须先完成transport parity
- 固定源码：Pi:src/qq_ai_bot/model_runtime/executor.py:1064–1068；Pi:src/qq_ai_bot/application/modules/web.py:205–288

**先纠正新鲜性，再删重复调用**：executor排队后和provider _post都调用check_model_dispatch，不等于每次实际重核。Pi:src/qq_ai_bot/services/turn_execution.py:165–180,218–290 的_PrimaryDispatch，以及summary guard在prepared/admitted后短路。第三轮真实_PrimaryDispatch+JSONHTTP MockTransport探针中，模拟来源撤回后仍有2次HTTP attempt，仅1次validation；ordinary/work summary重复admit也仅各校验1次。这是离线现状反例，不是生产外发事故证明。

沿现有guard/dispatch把“每physical attempt前纯新鲜检查”与“一次性prepare、reserve、projection/journal提交”分开。每次真正派发前核当前source/privacy/generation/取消/租约资格，排队后的admission仍保护此前边界；一次性预算/提交不能随retry重做。不得简单把整个before_model_request移出prepared：现有plugin session回调会预留父预算，plugin main-turn validate_and_commit还会发布projection。逐caller拆其真实职责，不加通用RetryManager、第二预算账或新ID。

验收第一次请求后的撤权/取消/generation变更在retry前零新增HTTP；排队中变更拒绝；每物理attempt原预算记账正确；一次性提交仅一次；summary、ordinary、Work、plugin session和injected fake都覆盖。freshness修复可独立推进；只有打算删“看似重复”的dispatch层时，才先完成APP-06 transport parity和caller证据。

- Profile native-capability 校验 (`Pi:src/qq_ai_bot/model_runtime/models.py:192–208`)、executor:561–567、vendor wire 校验不是相同集合：配置合法、实际请求所需能力、供应商 wire 禁令不同。保留这些真实边界。
- `Pi:src/qq_ai_bot/application/modules/web.py:205–231` 的 prepare 与 `_gemini_bridge:248–255` 重复同一 profile 的 Gemini protocol/key 校验；加上 `Pi:src/qq_ai_bot/web/gemini_bridge.py:55–60` 重复 output_limit。可把纯配置判定留在已有 prepare/ModelProfile 的一个明确位置，private builder 只装配；但 prepare 先预检全部 profiles 再创建 clients 避免半构造泄漏这一语义需保留。建议删除 private builder 中以实际diff核算重复，保留 direct GeminiSearchBridge 的必要构造边界直到外部构造 caller 迁完。
- `Pi:src/qq_ai_bot/application/modules/web.py:281–288` 有 startup-only 缺 search_connection 时猜 chat route，build:344–345 设置 require_explicit=False，而热保存要求显式连接。迁移旧配置显式 search_connection 后可删分支/布尔参数，以实际diff核算；不能移除所有 backend 分支，native/Gemini bridge/Tavily/DeepSeek 是不同已用产品路径。

### APP-08 删搜索缓存对业务成功的强依赖（可独立实施）

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：BridgeState派生缓存；原搜索结果
- 真实调用方：DeepSeek/Gemini bridge搜索成功后写cache
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：只隔离明确cache失败；不追加收费搜索，不吞取消/来源错误
- 固定源码：Pi:src/qq_ai_bot/web/bridge_state.py:18–56；Pi:src/qq_ai_bot/web/deepseek_bridge.py:93–105

**事实**：DeepSeek bridge `Pi:src/qq_ai_bot/web/deepseek_bridge.py:93–105` 与 Gemini `Pi:src/qq_ai_bot/web/gemini_bridge.py:97–116` 直接 await BridgeState.access；`Pi:src/qq_ai_bot/web/bridge_state.py:18–56` 文件/SQLite/JSON 失败向外传播。fake probe 先成功 `_search`，再 cache write 抛 OSError，最终用户拿不到成功结果。这个缓存只保留十分钟/128条派生检索结果，不是计费/权限/效果回执。

**动作**：删除 cache 读写成为搜索前置/成功条件的耦合。在 BridgeState 的既有边界将明确 cache I/O/SQLite/损坏条目转 miss/skip 并做内容无关诊断；不 broad-catch 网络、取消或业务异常，不重发已成功的 search，不另建缓存恢复状态机。也可产品决定彻底删缓存（61行 + 两 bridges key/lock/cache逻辑），但会提高重复搜索费用，未经成本确认不建议默认为整删。

**顺便退役**：`Pi:src/qq_ai_bot/web/bridge_state.py:20–22` 为上一版 decoder 而删除 provider_summary=None 的序列化分支仅 3 行；可在明确不再回滚旧 decoder 后删。不是优先目标。

**验收**：cache 文件权限/损坏/锁冲突/写满：首次搜索返回真实结果；下一次缓存 miss 可正常请求；fallback 不写入 primary key；partial Gemini 不被缓存成完整结果；cache failure 不能触发一次额外收费搜索。不得将“来源为空”从失败改为成功，模型正文 URL 不能充来源。

### APP-09 资源关闭 owner 收口，消除首错中断后续清理

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：各持有者关闭自己资源；现有lifecycle原则
- 真实调用方：ModelClientPool、executor、bridge.close
- 编码前置：无
- 协调边界：close所有权不依赖LegacyTaskModelExecutor移除。
- 实施或发布条件：首错仍关闭其余；保持cancel和pinned provider生命周期
- 固定源码：Pi:src/qq_ai_bot/model_runtime/pool.py:131–142；Pi:src/qq_ai_bot/model_runtime/executor.py:1161–1179；Pi:src/qq_ai_bot/application/lifecycle.py:105–112

**已证实**：`Pi:src/qq_ai_bot/model_runtime/pool.py:131–142` 逐个 await close；fake 注入 first/second，两者中 first 抛错时只看到 first，second 未关，connection pools 更不会关。`Pi:src/qq_ai_bot/model_runtime/executor.py:1161–1179` 的退休 close task gather 抛错亦可跳过剩余 pools。`GeminiSearchBridge.close:315–318`、DeepSeek:283–287 连续关闭资源也有相同顺序风险。

**方案**：复用现有 `Pi:src/qq_ai_bot/application/lifecycle.py:105–112` 的“全部清理后报告”原则，由持有者关闭自己的全部资源，收集异常并保留取消，不加 shutdown manager。ModelClientPool 是 HTTP pools owner；注入 provider 是否属于外部 caller 要按现有测试合同明确，不在合并中偷改 ownership。HotWebSearchProvider 已 gather(return_exceptions=True) 并按 runner pin 等待，是更接近目标的现有实现。

**不夸大为已发生业务结果丢失**：当前证据直接证明是 cleanup 被首错中断；不是所有模型成功返回都会受影响。生命周期本身已回滚/逆序 close、聚合错误并保留 cancellation (`Pi:src/qq_ai_bot/application/lifecycle.py:44–87`)，无需重写它或把所有异常吞掉。container.close 的通知会不会阻止 lifecycle 需以 notification contract 判断，此处未做故障复现。

**验收**：首/中/末 provider close 抛错后其余资源全被尝试；同一对象仅关一次；cancel 与普通 close failure 并发时保留 cancel 主语义；失败不改写已持久 tool/send 成功；热切换中的 pinned 旧 provider 不提前关闭。

### APP-10 结构化校验/修复：留契约，删调用分支而非删校验

- 实施批次：P1
- 本版变化：沿用首版证据并精简表述
- 唯一职责所有者：现有ModelExecutor单一调用签名
- 真实调用方：structured execute四分支
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：只合调用；exactly-one/unknown function/NaN/schema校验保留
- 固定源码：Pi:src/qq_ai_bot/model_runtime/structured.py:214–238；Pi:src/qq_ai_bot/model_runtime/structured.py:352–406

`Pi:src/qq_ai_bot/model_runtime/structured.py:150–160` 限定 repair 0/1；`:214–238` 为是否默认 priority/是否有 conversation_id 分四种 execute 调用，但现有 ModelExecutor protocol (`Pi:src/qq_ai_bot/model_runtime/executor.py:246–255`) 已规定这两个 kwargs 默认值。可合为一次明确 kwargs 调用；需先迁不完整 fake executor，最终净删除量以实现后的diff记录。

`Pi:src/qq_ai_bot/model_runtime/structured.py:352–406` 的 exactly-one emit_result、unknown_function、unexpected_tools、JSON parse_constant 拒绝非有限数、dict 类型和 Pydantic 校验必须保留。模型错调用不能因为“反正都是一个 JSON”被接纳。`Pi:src/qq_ai_bot/memory/dream/service.py:1038` 与 Pi:src/qq_ai_bot/memory/self_reflection/service.py:290 明确使用 validation_retries=1；attribution:265 则为0，各领域的 validate_output 验证不等于 schema 类型检查（来源/候选一致性不在普通 JSON Schema 内）。

repair payload 保留 previous_invalid_result 为不可信数据、8000字符截断 (`Pi:src/qq_ai_bot/model_runtime/structured.py:440–467`)；fallback 只接受两个显式 unsupported_json_schema code (`:239–252`)，不把任意400当可改写请求。budget/incomplete 在 :266–284 直接失败与校验 repair 不是同一种重试。保留这些限制，不引入通用 repair manager。

### RUN-11 新业务输入删除过期重复结果历史

- 实施批次：P0
- 本版变化：第二轮新增独立任务
- 唯一职责所有者：已有输入接纳边界
- 真实调用方：take_boundary_inputs、progress restore、batch fingerprint/threshold
- 编码前置：无
- 协调边界：新输入消费边界已存在；先修派生fingerprint失效，不等RUN-05。
- 固定源码：Pi:src/qq_ai_bot/services/turn_execution.py:455–538；Pi:src/qq_ai_bot/services/turn_execution.py:1804–1813；Pi:src/qq_ai_bot/services/turn_execution.py:1870–1903

`Pi:src/qq_ai_bot/services/turn_execution.py:455–538`新增输入仅pop caller_completion_pending_result，repeats/fingerprint不变；`:1804–1813,1870–1903`用旧指纹继续加数。新探针实际执行take_boundary_inputs确认这一点。改owner为已有输入接纳安全边界：消费**真实新增输入/合法完成信号**后丢弃旧fingerprint/repeats及本段previous_batch_fingerprint，不动Work ID、调用ID、root模型/工具预算、unknown围栏和旧effects。不能每次恢复或空take_inputs都清，以免同一付费空转跨重启永远躲过止损。

这是删除不再适用的派生判断，不是新“进展评分”；不根据词面、相似度或时间判新授权。验收：原用户新问相同查询可执行；只有重启/分段无新输入仍继承止损；原unknown mutation依旧禁止重发。

### CTX-07 移除 native 来源索引阻断已成功收尾

- 实施批次：P1
- 本版变化：第二轮新增独立任务
- 唯一职责所有者：原protocol/journal来源事实和既有来源索引读面
- 真实调用方：chat原native response尾部保存→WebSearchSourceRepository→读取者
- 编码前置：无
- 协调边界：来源读者迁移是本任务内前置，不需先优化DB-05写事务；不能把来源完整性问题变成cache异常吞掉。
- 固定源码：main:src/qq_ai_bot/services/chat.py:1280–1329；main:src/qq_ai_bot/services/chat.py:1399–1419；main:src/qq_ai_bot/persistence/web_repository.py:49–135

`main/Pi:src/qq_ai_bot/services/chat.py:1280–1329`：`_run_agent`返回意味着send_message可能已经确认；`native_tool_events`随后recover为WebSearchResponse，`run_effect(save_native_response)`先于memory和delivered checkpoint。`main/Pi:src/qq_ai_bot/services/chat.py:1399–1419（_save_native_web_response）`直调 `main:src/qq_ai_bot/persistence/web_repository.py:49–135`，身份核验/SQL/flush/prune均可能抛。该 repository保存 bounded来源metadata，不是QQ effect receipt。

删除目标是 **chat成功收尾等待派生来源副本**，不是删除真实citation或web安全：把原native response来源持久事实从已有protocol/journal/response读取，若保留便捷来源表，复用既有派生发布边界；该存储失败只说明来源索引不可用，不重发web请求/QQ。不要把身份校验异常广义吞掉再用新owner重写数据。先核所有 `WebSearchSourceRepository.for_trigger/list`读者确需哪种sourceID，迁至原来源或接受缺样，然后移除此尾部写依赖；本轮未证明全表可删，**不计整表删除收益**。这是静态调用链发现，未用真实QQ复现。

### AUTO-01 退役 generated 和 yuki.generate 双名

- 实施批次：P1
- 本版变化：第三轮补齐registry先校验再恢复的退出顺序
- 唯一职责所有者：TaskSpec compiler 的唯一 agent策略和现有执行器
- 真实调用方：tools/schema、registry、handlers、model delivery、validator、executor、持久旧脚本
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 实施或发布条件：公开新合同break；旧脚本产生新版本，原hash和在途ID不改
- 固定源码：main:src/qq_ai_bot/automation/compiler.py:57–88；main:src/qq_ai_bot/automation/handlers.py:100–101；main:src/qq_ai_bot/automation/registry.py:222–238

**真实链与新证据**：
- `main:src/qq_ai_bot/automation/task_spec.py:13–17` 声明 auto/static/generated/agentic；`main:src/qq_ai_bot/automation/compiler.py:57–88` 所有非 static 均生成唯一 `yuki.agent` 步骤；generated 与 agentic 的差别只残留在 `ExecutionPlan.strategy` 展示值
- `main:src/qq_ai_bot/automation/handlers.py:100–101` 的两个 capability 名都绑定同一个 `self.agent`；`main:src/qq_ai_bot/automation/registry.py:20–28,222–238` 却仍公开独立 GenerateArguments/AgentArguments。其中旧 max_characters 不在 `handlers.agent` 使用，不能伪称仍有独立文本生成引擎
- 新模型工具仍在 `main:src/qq_ai_bot/automation/tools.py:119` 广告 generated；控制面 `service.management_schema:71–89` 把 registry 所有名字公开给 `main:frontend/src/automation-editor.tsx:59–98`，所以旧名仍可被新提交
- 运行期消费者是 `main:src/qq_ai_bot/automation/models.py:104`、`main:src/qq_ai_bot/automation/validator.py:39`、`main:src/qq_ai_bot/automation/model_delivery.py:10`、`main:src/qq_ai_bot/automation/executor.py:180,383,442,454,972` 的双名分类与旧 delivery 防重发分支；仓内显式调用还在 tests/support 与 automation 单元/集成测试。不是插件独立生产生成器

**任务**：新 TaskSpec/DSL admission 版本仅暴露 static/agentic，auto 可作为省略默认的输入便利值，不保留 generated 的运行模式；删除 GENERATED、ExecutionPlan 的 generated 分支、schema 枚举、GenerateArguments 与新 registry 的 yuki.generate 登记、handlers 双名条目。把所有新测试调用迁到 yuki.agent。不要新建 AliasRegistry。

**持久迁移**：TaskSpec 本身不持久，真实持久物是 AutomationScript/版本、授权快照、script hash、run cursor、Work。离线清点旧脚本的 call/引用数据流、非终态 run 与 phase、required_capabilities、Work permission；新 activation 不再接纳旧调用。对未在执行、且仅单个 generate 无下游文本发送依赖的任务，生成一个需审核的新脚本版本；不原地改旧 hash。含 `generate -> ${step.text} -> send` 的旧脚本不得机械换名字后自动发，必须改为 agent 显式 send 的新版本。已接受 Work 用原 ID 结算，禁止换键执行；历史旧版本仍作为数据读取。旧名运行桥只覆盖已接受且可证实的原执行，清点排空后删除，不永久允许新提交。

**验收反例**：generated/agentic/auto 同目标目前编译脚本完全一致，已运行现有 `test_generated_and_agentic_compile_the_same_delivery_contract`，1 passed；实施后应改为旧新提交拒绝测试及唯一策略测试。另测历史静态模型文本尾部永不发送、旧 cursor 不改 hash、不恢复预算、unknown 不新建 Work。删掉 delivery guard 不是本任务目标。

#### 先清点原接纳再删执行定义

main:src/qq_ai_bot/automation/executor.py:738–797 在_begin_execution以registry.list核全脚本call，main:src/qq_ai_bot/automation/executor.py:391 再require定义。先删yuki.generate会使已有phase=agent/cursor.work_id也到不了原handler；保留历史字符串不足以保证执行恢复。默认先在旧版停止新接纳，排完或明确退役已接纳run，再一次删registry定义及旧执行分支。若实际升级需分两版，只在原registry临时保留明确accepted cohort所需定义，待其结算后删，不建AliasRegistry。GenerateArguments与AgentArguments并非同schema，原generate→text→send依赖不能机械改名触发投递；script_version/hash、原cursor和结果按历史读保留。


### AUTO-02 统一 Automation 提交编排和显式输入

- 实施批次：P1
- 本版变化：第二轮新增独立任务
- 唯一职责所有者：现有service的编译后校验提交
- 真实调用方：模型TaskSpec、SDK受审模板、WebUI高级DSL
- 编码前置：无
- 协调边界：可先合并已编译脚本校验提交；编译策略退役/控制surface重组同文件协调。
- 固定源码：main:src/qq_ai_bot/persistence/control_management.py:791–850；main:src/qq_ai_bot/plugin_host/facades.py:2243–2270；main:frontend/src/automation-editor.tsx:59–98

**真实最后消费者**：
1. Agent：`automation/tools.py` → `service.create_task/update_task` → compiler → `service.create/_commit_update`
2. SDK 模板：`main:src/qq_ai_bot/plugin_host/facades.py:2243–2270` 的 `create_from_template` → 受审 builder → `service.create`
3. WebUI：`main:frontend/src/automation-editor.tsx:59–98` 真正编辑任意 steps/capabilities → `mutate_automation` → `main:src/qq_ai_bot/persistence/control_management.py:791–850` → `service.administer_create`；update 走 administer_update。控制面不是 TaskSpec 薄镜像，它真实支持多步 DSL
4. `/ai automation` 的 `services/automation_commands.py` 只做目录/show/状态/history，不是 raw DSL 新建消费者

**明确删除**：控制 create 当前同时接受裸 script 与 `{script, owner_id, conversation_id,max_runs}`；`main:src/qq_ai_bot/persistence/control_management.py:808–819` 的裸 payload 猜形状必须删，control 新版本只收显式结构。update 也统一显式 script 字段，前端同时迁，不留两套解析。`service.py:create` 与 `administer_create`、`_commit_update` 与 `administer_update` 重复做 validate/limit/repository commit：入口先解析真实 actor 或 control context，复用现有 CreationProvenance/authority，把“已编译脚本的校验与提交”收为唯一内部实现；删除第二份规则编排，而不是再包四个转发 Facade。

**不做错误删法**：不能把 TaskSpec 设为唯一合法业务表达并删除 DSL editor，因为 TaskSpec 没有多步工具/模板依赖表达能力，这会取消现有高级自动化功能。保留 DSL 作为统一内部 IR 与一个显式高级输入；TaskSpec 是编译入口而非第二个执行器。具体原始 DSL/API 的名字可一次性变更，但业务能力保留，除非用户另行决定取消。

**迁移/测试**：front editor create/update、SDK受审模板、控制面 owner/self/conversation/max_runs 与创建来源键同时迁。持久脚本无需因输入信封改变改写。验收裸 script 新提交拒绝；两种入口产生等价脚本时 authority/required_capabilities/hash/预算一致；管理员更新不变成管理员代执行；SELF 无私人权限；expected_revision/原 source key 保留；多步 DSL 功能仍可用。

### SDK-01 删除 LLMFacade 并统一主 Agent 运行合同

- 实施批次：P1
- 本版变化：第三轮独立复核后修正
- 唯一职责所有者：AgentFacade.run 和 run_plugin_main_turn
- 真实调用方：SDK Protocol、Host构造/property、testing fake、权限清单与旧Work
- 编码前置：无
- 协调边界：两个facade共用run_plugin_main_turn，不需要AUTO-01先删除yuki.generate；旧approval Work迁移必须显式。
- 实施或发布条件：公开SDK破坏性升级须明确产品选择；旧Work先drain或retire并交付所需全文，再改manifest和approval
- 固定源码：main:src/yuki_plugin_sdk/context.py:127–146；main:src/qq_ai_bot/plugin_host/facades.py:1569–1635；main:src/qq_ai_bot/plugin_host/facades.py:1721–1789

main:src/yuki_plugin_sdk/context.py:127–146 的 LLMFacade，main:src/qq_ai_bot/plugin_host/facades.py:1569–1635 的 _LLMFacade，与 main:src/qq_ai_bot/plugin_host/facades.py:1721–1789 的 AgentFacade.run 最终都进入run_plugin_main_turn。旧llm完成返回str、挂起返回PluginResult，形成两个结果形状。

**删**：新SDK版本退出LLMFacade Protocol、Host _llm构造/property、_LLMFacade、SDK fake、旧llm.generate/llm.generate_with_context新批准声明和文档；context_profile迁入现有agent.run，统一PluginResult。max_characters归调用者展示或唯一结果层，不删除持久完整结果。agent.result/resume和独立agent_sessions仍有不同职责，保留。

**公开升级是产品选择**：仓内业务插件未发现真实llm调用；main:tests/support/main_agent_wire_cases.py:312–413 和 main:plugins/subscription-monitor/tests/test_monitor.py:97,246 是合约/权限断言。外部挂载插件要按独立SDK break改API和manifest，并显式重新批准新权限；不能把旧llm权限静默扩大为agent.run。升级选择和消费者清点是发布条件，私有死代码删减不无限等待假想caller。

**先结清原Work再改批准版本**：当前 source_json 绑定 manifest与approval_revision；重新批准后，旧Work的正常读取/恢复不能假定仍可通过。发布顺序为停止旧新接纳 → 以当时合法入口drain或明确retire既有nonterminal Work → 需要保留供插件读取的全文结果先导出/交付 → 再升级manifest并重新批准。不能先改revision，再放宽校验补救；不新增旧权限旁路、永久兼容facade或第二结果存储。

历史Work、checkpoint与原效果保留。升级后现有operator控制查询只提供状态审计，不冒充checkpoint.sync_result全文接口；保留历史字节不等于旧插件仍有读取授权。若产品决定必须持续向插件暴露历史全文，那是另需明确的窄只读授权合同，不能在本任务偷加。原unknown先查原ID，不能为清空pending重做副作用。

**验收**：新SDK无旧facade；完整与pending返回同形；context_profile不扩大历史权限；来源、group身份、权限交集、递归主Agent拒绝、callback pending原Work、approval_revision/generation撤回均正确。升级矩阵必须覆盖未决Work在批准前后、已终态全文交付、明确retire与未知效果；无自动发正文保持。

### API-04 删除 Automation facade 错误参数镜像

- 实施批次：P0
- 本版变化：第二轮新增独立任务
- 唯一职责所有者：AutomationService既有actor合同
- 真实调用方：Plugin _AutomationFacade._manage的pause/resume/cancel三条路径
- 编码前置：无
- 协调边界：本任务自身caller迁移/反例为原子工作；原deps只作同组协调，不作为全任务开工前置。
- 固定源码：main:src/qq_ai_bot/plugin_host/facades.py:2285–2294；main:src/qq_ai_bot/automation/service.py:511–570

**薄镜像漂移实证，优先修**：`main:src/qq_ai_bot/plugin_host/facades.py:2285–2294（_AutomationFacade._manage）` 动态取 pause/resume/cancel 后传 `inbound=invocation.inbound`，而 `main:src/qq_ai_bot/automation/service.py:511/533/557（pause/resume/cancel）` 签名要求 `actor`。AST离线检查确认不接受 inbound，实际进入调用会 TypeError。迁成 `actor=ToolActor.from_inbound(...)`，删除动态 getattr/错误关键字镜像并增加三动作参数合同测试。不为这个缺陷新建兼容 inbound 参数。

实施时让 pause/resume/cancel 显式绑定对应现有 service 方法；从已核 invocation.inbound 一次构造 ToolActor，传 actor，不新增 inbound 兼容参数。分别测试三动作、SELF/普通actor、权限拒绝与原receipt；管理成功不改变任务owner。

证据：round2/evidence/interfaces/check_interfaces.log。当前是AST签名矛盾证据，不称生产事故。

### CTL-03 删除外部回执压扁和重复映射

- 实施批次：P1
- 本版变化：第三轮缩至tuple与repack删除，不复制终态
- 唯一职责所有者：现有领域结果和控制ManagementMutation DTO
- 真实调用方：Manager→ControlWorkspace→ControlManagement→ControlCommand→ExternalControlExecutor
- 编码前置：无
- 协调边界：现有控制结果自己的DTO合同，与模型ToolExecutionResult不同；不依赖API-03，不把控制接纳状态当终端最终状态。
- 固定源码：main:src/qq_ai_bot/persistence/control_workspace.py:274–275；main:src/qq_ai_bot/persistence/control_management.py:294–307；main:src/qq_ai_bot/persistence/control_command.py:938–963；main:src/qq_ai_bot/persistence/control_external.py:226–255

**新具体映射链**：Manager result → `main:src/qq_ai_bot/persistence/control_workspace.py:274–275` 压成 `(resource,1,accepted/saved)` → `main:src/qq_ai_bot/persistence/control_management.py:294–307` 重新包装 ManagementMutation → `main:src/qq_ai_bot/persistence/control_command.py:938–963` 构造 _Success/effective_state → `main:src/qq_ai_bot/persistence/control_external.py:226–255` 写控制结果。终端原run pending/最终失败/file expected_version及publish artifact信息在tuple层不再存在；UI后来通过 `read_terminal_submission` 原request查回，`sandbox/control_completions.py` 单独记录完成审计，明确不改原命令结果。

**任务**：ControlWorkspace直接返回已有 ManagementMutation 或等价现有领域结果，删除中间tuple及repack；当前resource_id已是run_id，前端已有原run查询；不把终端最终状态复制进control receipt，也不复制Manager JSON。文件version/发布artifact仅在已有消费者确实需要时扩展现有DTO，禁止为映射对称增加字段。保留“命令接纳成功≠进程成功”的区分；原request到run映射仍唯一，不让UI靠status文本猜成功。完整执行输出留原领域读取，控制receipt只存必要稳定ref，不复制大正文。

**保留的两种事务**：`control_command._execute` 数据库内部原子业务+receipt，与ExternalControlExecutor意图→事务外效果→结算，不是可互相替换的重复引擎。删“重复mapping owner”，不把外部等待塞进writer；unknown资源占用、幂等payload hash、已完成回执读取仍在。对终端接纳已知但控制最终commit失败，要按原request查询Manager恢复关系，不能把unknown换成失败或重提交。

**验收**：Manager返回pending/失败/取消/unknown/final的fake矩阵，control命令accepted不显示任务succeeded；同request不同payload冲突；原run查回；completion重复/冲突；文件version精确保留；任意未登记方法拒绝；content权限拒绝；CSRF/Origin/request头体ID不变。

**离线验证已完成**：`fake_receipt_mapping.py/.log` 直接调用当前 ControlWorkspace.mutate，仅替换transport为内存fake，无socket/DB/业务API。running/succeeded/failed/cancelled 四种Manager结果全部映射为 `(run_id,1,accepted)`；文件的真实version字段映射为 `(environment,1,saved)`。这是映射信息丢失证据，并非证明控制命令接纳语义错误，也未证明UI误报。实现只删除tuple/repack，原可查询run引用保持；不能把accepted改成failed，也不能由此推出需要第二份终端状态。

### CLI-01 迁在线插件管理并删除 CLI 直写数据库

- 实施批次：P2
- 本版变化：第三轮独立复核后修正
- 唯一职责所有者：运行中PluginManager和认证control command
- 真实调用方：在线CLI和apply-pending；停机bootstrap；quickstart与release_smoke
- 编码前置：无
- 协调边界：已存在在线control服务；还须收口apply_pending_plugins在线链，停机bootstrap复用既有排他锁。
- 实施或发布条件：在线管理复用既有认证control；停机bootstrap使用原SQLiteApplicationLock排他，不依赖活Bot
- 固定源码：main:src/qq_ai_bot/cli.py:553–601；main:src/qq_ai_bot/deployment_setup/service.py:632–690；main:scripts/release_smoke.py:283–322

main:src/qq_ai_bot/cli.py:553–601 的approve/enable/disable直接写PluginInstallationRepository；main:src/qq_ai_bot/persistence/control_management.py:335–377 已有运行中PluginManager、actor和expected_revision路径。另一真实writer是main:src/qq_ai_bot/deployment_setup/service.py:632–690 的apply_pending_plugins，不能只删CLI表面留下第二条在线写链。

**在线唯一owner**：保留CLI命令体验；运行中approve/enable/disable/doctor改用已认证control客户端，先query当前revision和显式permissions，再按原request提交。删除CLI与在线apply-pending直接repository写、doctor第二份状态裁决。discover迁真实运行时入口或只读本地扫描；当前control没有现成discover就明确补绑定，不启动第二个Bot获取Manager，也不因能读本地DB伪造全能operator。

**首次停机bootstrap仍需可用**：首次初始化不能要求Bot先在线。保留必要离线setup写入，但只在既有SQLiteApplicationLock排他确认停机后进行；与运行中的manager互斥。不另建长期bootstrap服务或锁表。Repository继续作为原owner的底层，不因入口删除误删它。

main:docs/plugin-development/quickstart.md:107–110、main:scripts/release_smoke.py:283–322 以及版本发布测试是最后caller。release smoke先确认Bot健康后又apply-pending，必须迁在线控制；若走停机bootstrap则先停机并持原锁，不能让两种模式并行。validate/test/docs属于离线开发，继续保留。

**验收**：CLI与WebUI同permissions/revision/request得到同结果；并发enable/disable、manifest改变、撤权与显式权限；实际生命周期启动/停止；首次无Bot bootstrap可运行且锁冲突拒绝；在线setup无直写；release smoke不为只读验证制造真实插件副作用。

### CTX-06 运行状态置于当前真实发言之前

- 实施批次：P0
- 本版变化：第三轮独立复核后修正
- 唯一职责所有者：TurnExecution.prepare_request首次合法新initial定稿；原projection和journal各自拥有事实
- 真实调用方：普通/SELF/plugin/Automation共享激活；Work restore、pending Code resume/rebase、handoff save、首次dispatch
- 编码前置：无
- 协调边界：activate及首轮steer收齐初始化资料，首次prepare_request取序列前定稿；旧exact与handoff早返回不改，无CTX-04/APP-05整包前置
- 实施或发布条件：activate和首轮steer收齐资料，prepare_request取transcript.request前仅首次定稿；旧exact与handoff事实原样
- 固定源码：Pi:src/qq_ai_bot/services/turn_execution.py:1007–1016；Pi:src/qq_ai_bot/services/turn_execution.py:324–410；main:src/qq_ai_bot/runtime/work_session.py:147–294

目标顺序是固定system/persona → 获准公共历史H → Host初始化资料S → 完整当前输入组，仅适用于合法新协议链的首次输入。当前组含原current C及首轮真实接纳的新增输入，保留各自ID、作者、来源与原次序，不假装都是同一真人。main:src/qq_ai_bot/services/main_agent_turns.py:710–749 与 Pi:src/qq_ai_bot/services/main_agent_turns.py:739–751 当前在C后追加Work state。PromptCompiler/PromptComposer原有动态资料已位于current前，不应反向改动。

**最终编排边界**：_run_prepared只捕获原current及compaction brief。activate先完成restore、pending Code恢复/rebase，随后begin/首轮steer继续收集Host提醒、新接纳当前输入与反馈。最终只在Pi:src/qq_ai_bot/services/turn_execution.py:1007–1016 的prepare_request开头、取transcript.request之前，对首次合法新initial一次定稿。先确认收齐首轮Host资料再生成最终S，不在activate提前封口，不增加每轮重排或ContextManager。旧uses_recovery_transcript及后续工具轮次完全不套新布局。

handoff分支的Pi:src/qq_ai_bot/services/turn_execution.py:389–401保存paired后直接return，不进入loop/prepare_request；不延迟或重排该原事实，也不强套新布局。restore/pending Code中的旧paired保存仍归旧链；之后合法rebase才能建立新initial。首次最终dispatched记录及publication沿既有_PreparedRequest.admit完成。

**保留所有事实**：还需包括contract/source changed分支产生的跨模型观察、未配对调用对账及pending Code恢复资料。以原composition精确前缀和Host已知追加段分离H、C与S；S在全部真实恢复材料齐备后捕获一次。不能按文字标签扫描删除、按最后user猜真人、提前捕获后又二次注入。C保留原ChatMessage及作者、全部event、引用、图片、response_item/reasoning；control.current_message和brief仍绑定C。SELF/plugin不借最近真人身份。

**Host来源**：S用由Host固定的source/kind/state_scope envelope；goal等用户或模型文本只在JSON data内，不能覆盖来源或变成指令权。运输role=user无需升为system/developer，也不放入静态persona。可用一段或精确捕获的有序Host块组，不能靠增加假assistant阻止Claude/Gemini合并。

**projection与容量同改**：公开composition.messages并非含S的实际首次请求。沿现有validation闭包和dispatch对象传递最终初始tuple及其精确映射，逐项验证H与原C等于公开composition，验证中间Host段及新增当前输入分别来自本次可信构造/真实take_inputs，次序不变；公共冻结只提交原composition的fragments/current，新增Work输入沿原staged/consumed身份和发布边界，不偷并到公共snapshot。S、opaque及工具回执不入公共冻结。若现有字段无法表达才补最小瞬时事实，不持久化第二owner。同步ordinary initial_messages前缀消费者、projection验证、恢复与compaction；anchor只保留原本应保留的current/任务成员，不能把所有S、unobserved receipt或业务资料永久钉入anchor。

**版本与缓存承诺**：私有Work执行合同必须记录稳定context-layout revision；公共projection仅在实际持久表示、event选取或scope合同改变时升级，不因S私有位置变化主动退役H。合法contract_changed才建立新链；旧epoch、journal、opaque不重排，原Work/预算/source guard/operation ID保持。静态system、mode及有序tool view在其原owner稳定；H可跨普通轮共享。当前H,S,当前输入组中的S不是下一轮公共历史，所以不能保证上一整份HTTP请求是下一轮前缀。Claude/Gemini按实际blocks/parts核验；cache hint位置变动与公共内容变化分别记，不把命中率当正确性。

**同激活工具续接**：首次只注入一次，后续assistant/tool结果按原序追加，C不搬到工具回执后；call_id、thoughtSignature、encrypted_content等保留。旧未决call先原ID对账，unknown不授予重跑。新receipt私有协议事实和其投影为公共事件是两种视图，不能重复渲染为两个相同结果。

**修改与真实协作范围**：main_agent_turns、turn_execution、WorkSession初始化、turn_transcript、projection validation和compaction anchor在本任务内原子修改；CTX-04容量策略与APP-05旧delta删除无需先完成，只须以现有行为联合回归。四adapter不增加按模型/内容排序。更新main:tests/unit/test_work_state_tools.py:98–129、main:tests/unit/test_work_protocol_continuity.py:1419–1457、main:tests/unit/test_gemini_history_prefix.py:210–220 的旧后置断言。

**验收**：普通/批量/图片引用/SELF/plugin/伪装Host文本，四协议首次wire均H,S,完整当前输入组且每个接纳事件仅一次；同activation工具续接/HTTP retry保持原prefix；旧exact journal恢复完全不变；contract/source改变新链、首轮steer的Host反馈与新增当前输入、handoff仅保存后返回、后续新输入、取消和source撤回；projection提交/回滚不含S/opaque；真实全请求容量与compaction不丢必需资料、不把全部初始化资料永久anchor。

第二轮6现状测试与8类×4协议×2布局fixture只证明运输现状/候选组装；第三轮两树纯离线四协议probe证明公共atoms、同链前缀、稳定tool schema和回执单次，但没有运行目标首次请求边界改动、真实模型或测缓存命中。目标实现仍须完成上述矩阵，不能说换顺序保证模型100%正确理解。

### PAR-01 合并同轮参与反馈准备并保留原回执事实

- 实施批次：P1
- 本版变化：第三轮新增独立任务
- 唯一职责所有者：SemanticParticipationService的scope推进与现有participation_feedback
- 真实调用方：dirty scope、驻留scene tick、hydration前后反馈与晚到anchor
- 编码前置：无
- 协调边界：与现有scope推进和hydration原子联测，无全包开工前置
- 实施或发布条件：只合并同tick的重复读取/准备；保留hydrate后anchor，不宣称跨tick零查询
- 固定源码：Pi:src/qq_ai_bot/services/semantic_participation.py:1343–1404；Pi:src/qq_ai_bot/services/participation_feedback.py:168–300

main/Pi的SemanticParticipationService对dirty scope先sync+hydrate，随后所有驻留scene又在_advance_scene sync+hydrate。循环是本轮结束后sleep2秒，并非严格每2秒。participation_feedback按同scope近600秒、updated_at倒序、LIMIT2048读Social metadata，已有scope/updated索引；不是全库正文扫描。

两树隔离probe的20条不变receipt连续30次sync，得到30次SQL和600次observer调用；原effect ID仍有去重，不能说600次业务效果或发送。删除对象是同tick中两套独立取窗口/重复hydrate准备，不是新的幂等性框架。

将direct标记快照与待处理scene汇入现有单次scope推进，成功后按原值清dirty，期间新dirty保留。不能简单删第二sync：第一次在hydrate前，第二次可在hydrate建立SourceRef后补public anchor；原feedback明确允许先未知后绑定。保留receipt→hydration→anchor/source复核顺序，只把本轮已读有界投影作为局部值贯穿，不跨tick缓存授权、不持久化影子receipt/processed表。

仅当语义完全相同才跳过重复committed-effect应用；ordinary admission、late anchor、source撤销、generation、迟到completed-run不能被seen ID吞掉。legacy_allowed/admission还有即时caller，不是当然死代码。只合并dirty路径不会让不变驻留scope的30次SELECT自动归零；跨tick减读先证明所有更新、timestamp tie、迟到receipt和冷加载覆盖，不先建cursor/cache。

验收receipt在hydrate前/后、run终态后迟到、caption/sequence父键、重复tick、同timestamp多行、generation/隐私撤销、2048窗口、冷加载、处理中新增dirty。分别计SELECT、hydrate、observer、snapshot写及语义一致性，不只断言SQL更少。现状probe不提供目标延迟收益。


## 用户追加：默认 direct 与内存验收（2026-10-08）

本节来自执行中的用户追加要求，优先于原提案默认启用 Code Mode 的安排。原 68 项保持原 ID；新增 DEP-02、RES-03，共 70 项。生产服务器约 1.6 GiB 内存，用户提供旧 Yuki 约 500 多 MB 的占用线索；不同时间、RSS/PSS/交换空间和进程树口径不可直接比较。本次测试容器 tmpfs 与镜像加载期间的过载不是产品内存泄漏的证明。

### DEP-02 默认发行和生产采用完全不依赖 Code Mode 的 direct 构建

- 唯一 owner：既有 Docker direct target、Settings 与 MainAgentContract；不另建 Agent loop。
- 默认镜像、Compose、安装示例和发行入口采用 direct；包内没有 Monty binding、原生 worker 或 launcher。原生编译阶段仅在显式构建 codemode 时运行。
- direct 声明当前作用域全部获准工具，仍逐次授权、保留 Work/子任务/终端/Memory/文件等能力；不因关闭 Code 删低频业务工具。
- CODE_MODE_ENABLED 默认 false。可选 codemode 镜像与开关必须显式选择；既有 Code 回执仍按原 ID 读取和安全结束，不能恢复新 worker 或重派旧效果。
- 生产采用 direct，不新增 Code 专用 AppArmor/seccomp 放行。既有专用策略可保留未使用，不影响其他服务。
- 验收：默认构建无需原生构建层；镜像检查 binding 不可导入且 worker/launcher 不存在；默认配置/装配不构造 Code runtime；全工具可达与授权、历史 Code 安全恢复、实际部署健康分别核验。
- Code 启用门：在目标机器余量下单 worker 的常驻/峰值/退出回收与正常 Bot、网关共存证据充分后再单独决定；隔离探针不等于容量验收，不自动启用。

### RES-03 检查并修复可复现的 Yuki 内存持续增长

- 唯一 owner：各现有缓存、会话、队列、任务、客户端池及子进程生命周期；不新增通用内存管理框架。
- 先建立旧生产的只读内存基线，分别记录 Bot RSS/PSS/Swap、进程树、主机可用内存与负载；不采集含消息/凭据的堆转储，不发送额外 QQ 测试消息。
- 离线检查长期 dict/set/list、缓存容量和过期回收、后台任务 finally、异常/取消后的会话与连接释放、worker/子进程树退出。区分合理保留、一次峰值、Python 分配与 OS RSS，不把单次 GC 后 RSS 不降当作泄漏。
- 对可复现增长做有界重复工作负载：预热、稳定输入与高基数输入分开，比较多轮活对象/保留分配及峰值，核查取消/失败/关闭路径。仅修已经证明的生命周期缺陷；不为压 RSS 任意缩历史、预算或 unknown 回执保留期。
- 允许直接删除完成后无消费者的临时状态；有业务 fence/generation 含义的状态不得盲目 LRU。保留原执行身份、迟到回执保护和 current source 边界。
- 验收：记录发现、排除理由、真实修复与针对性回归，提交 direct 镜像运行资源证据；明确短测不能证明长期无泄漏，不报告虚构百分比收益。不在小内存生产机并行跑重测试或叠加大 tmpfs 与镜像解包。


## 最终执行收口（2026-10-08）

70 项任务索引已逐项标记。最终源码 f4f483d7 的完整 CI 为 4800 passed / 239 skipped，PR #265 已合并；生产采用默认 direct 镜像，Monty binding / worker / launcher 均不随包部署，75 个工具全部直接声明。生产唯一 0096 库升级 0099，135 张保留表按记录的退役配置排除规则完成原列 / 行数哈希核对；Manager 原 628 个任务及原持久文件 / manifest 内容保留。Bot、DB、OneBot、Manager 和两个原插件均核验，SnowLuma 未重启。

DEP-02 以默认构建、包装、模式合同及真实上线证据关闭；RES-03 以真实取消集合增长修复、26 项回归、20,000 次离线比较及部署后同口径短时资源采样关闭。此处不声称长期无泄漏、Code 容量验收或真人 QQ 自然收发已完成；既有语义参与 CanonicalIdentityError 告警也保留说明。详见 [最终集成记录](../operations/deletion-integration-20261008.md)、[终审](../operations/deletion-final-review-20261008.md)、[内存检查](../operations/deletion-memory-20261008.md) 和 [生产执行记录](../operations/deletion-production-20261008.md)。
