# 混合资料布局上线记录

2026-10-12。本文补充实际交付、生产迁移和新版自然窗口；[655条受控测量报告](README.md)及其三份CSV保留原归档，不改成上线后的实测。

## 提交、CI与镜像

| 项目 | 已确认事实 |
| --- | --- |
| 实现 | [PR305](https://github.com/YuanYeYouTao/Yuki/pull/305)已合并，main提交977dc5ad |
| 许可证 | [PR306](https://github.com/YuanYeYouTao/Yuki/pull/306)已合并，main提交e30c15b4；AGPL-3.0-only |
| 本次上线源码 | e30c15b4cfaf6ffb29a5e2f4dd404b046f498789；977→e30没有src/migrations/tests/tools变动 |
| CI | [38260379606](https://github.com/YuanYeYouTao/Yuki/actions/runs/38260379606)，e30c15b4，completed/success |
| 本地构建与服务器装载 | 两端image ID相同：sha256:52fb83741180b494bb6e1b134da668dd6a3b3b984b45f34663428367e7bef346 |
| 镜像源码/许可 | 642个已安装source文件与63个migration逐文件SHA匹配；OCI revision/license、安装包License-Expression/License-File、实际LICENSE与checkout相符 |
| Bot启动 | UTC2026-10-12T14:31:54.525213207Z，ops-e30c15b4，schema0108 |
| 健康核验 | UTC14:41:43：Docker healthy/restarts0、status/database ok、OneBot连接、原两个插件运行、Memory consistency healthy、Work运行且最后错误为空 |
| SnowLuma | 容器ID及UTC2026-10-09T05:10:47.99332112Z启动时刻与部署前一致，没有重启；其未配置Docker health，不能把null写成healthy |

Code Mode继续为false。部署只替换Bot；原Work业务状态/所有权/预算/私有协议合同没有改写。已确认send回执的事件去重及worker固定Memory提示属于明确的辅助改动，不能笼统声称所有Work相关文件完全没有变化。

## 实际生产0108迁移

停机一致0107快照与迁移完成、Bot启动前的0108数据库分别取只读证据；以原ID、整行/逐列SHA和0108实际SQL计算预期。独立审计39项全部通过。下面是本次实际生产数量，不能套用旧隔离快照的275/872演练数字。

| 对象 | 实际结果 |
| --- | --- |
| 受影响来源 | 142份；全部因4个既有Work owner的私有执行依赖保留，物理删除0，原件整行SHA不变 |
| 公共selection | 183条受影响：146条更新保留、37条删除；总数2353→2316，另外2170条无关行不变 |
| 公共projection | 120条；原保留列不变，revision各+1；派生历史/map为[]/{}，不移植旧完整快照 |
| canonical owner | 18个受检owner的revision均不变，不把公共selection清理伪装为新业务事件 |
| 业务原件 | 12个私有业务表、43个Chat/Memory表SHA前后精确一致；Chat92409、Memory事实2559、Work204/journal196、protocol refs169022/objects14617、预算193/效果204、Code runs711/continuations686、artifact37 |
| 空表边界 | private session/messages、artifact refs在该快照为空；不冒称非空恢复，也不拿SQL行hash证明未采集的协议文件字节 |
| 旧配置 | 3个退休预算DB key原本不存在，实际删除0；其余14行不变。Compose中4项退休环境参数已移除，其余Bot及所有非Bot配置语义相同 |
| 触发器与外键 | 14个实际trigger符合0108定义，其中12个SQL hash变化、2个原样；schema0107→0108，foreign_key_check始终0 |

此次公共退出不会重写真实聊天、notes、Memory事实或已付费原件；保留原件不能写成全库删除。两个插件挂载manifest只删除退休的limits.prompt_characters；沿原认证Control API批准并恢复原6/9项权限和enabled状态，5次变更均有确认audit回执。NetEase原禁用状态未变，没有直写业务库绕过审批。

## 上线后的首个自然窗口

UTC **[2026-10-12T14:31:54.525213Z, 2026-10-12T14:41:34Z)**。Yuki与代理使用同一边界；Yuki从实际停机最大trace ID191650、invocation ID64507之后读取，只查PK后缀/时间索引。原UTC string和源码UTC写入已核实，不以PowerShell展示转换后的+08值重算查询。

| 观察对象 | 数量 | 可以得出的结论 |
| --- | ---: | --- |
| Yuki model context / invocation | 0 / 0 | 该窗没有模型请求样本；普通Main、SELF、SDK、自动化、Memory模型入口都没有新增可计量样本 |
| 语义观察trace | 12 | start/end/result各4；它们实际存在且均在原窗，不是时区漏筛或没有任何新事件；不能当模型调用或QQ回复 |
| AGM代理请求 | 0 | marked/unmarked各0；不是已知Provider发生cache miss |
| input/cache/output/thought | 未知 | 无样本时不填写0 token、不推算费用或平均耗时 |
| 缓存率与节省额 | 未知 | 无缓存分母，不能写0%，也不能从受控实验推算生产收益 |
| 真正新增的稳定资料份数 | 未知 | 需要实际请求、view/selection/source连接；库存、旧历史重读和健康检查不能代替注入频率 |

两份完整采集文件均有完成标记/UTC与SHA证据；只读补查原PK后缀确认模型账0、新语义trace12。没有伪造真人、发QQ测试消息、重发原效果、修改路由或增加付费保温。

稳定有效场景仍首次/变化冻结；有效short_state仍按原文字差量；必要Work/SELF及执行事实沿原Host当前区；空槽、revision包装与默认冗余producer退出。无用途高频字段直接不供，不新增通用current-only层、分类器或按轮数/时长的整理。显式SDK requested_context保留原合同，本窗没有此入口样本。

E07、V10、V14仍未勾选：PR/CI/镜像/迁移/启动健康均已有独立证据，本首个窗口没有自然模型样本；用户随后要求/ai new，重置后的非空窗口单列于后文。真实QQ客户端接收及完整非空资料变化/入口覆盖仍待验，不能把HTTP200写成全部QQ验收。

## 代码行数更正与本地测试

按实际提交 **git diff --numstat 24da979..e30c15b4** 分组；下表互斥，Git行数不是token或费用。之前报告把工作副本+847/-1562标作src+migrations，口径有误，以下最终提交数字更正该表述，原测量记录不改。

| 范围 | 文件 | 新增行 | 删除行 | 净变化 |
| --- | ---: | ---: | ---: | ---: |
| src | 53 | 846 | 1561 | -715 |
| migrations | 1 | 250 | 0 | +250 |
| tests | 19 | 2710 | 207 | +2503 |
| tools | 4 | 1740 | 63 | +1677 |
| docs | 24 | 2648 | 56 | +2592 |
| 根LICENSE | 1 | 662 | 19 | +643 |
| 其他根文件/前端/元数据 | 14 | 26 | 25 | +1 |
| 全部 | 116 | 8882 | 1931 | +6951 |

产品src+migrations合计 **+1096/-1561，净删465行**；src自身净删715行，0108新增250行单列。新报告、CSV、测试和手动实验工具不能算作运行时新增框架。本文件及后续纯文档交付不在e30统计中。

最终源码追加241个不同pytest实例通过：资料/存储124、Work/Provider107、SDK/插件10；全仓Ruff、mypy Linux641文件及本次格式/compile/diff检查通过。两个真实本地PID恢复、90份wire字节相等及198份原paid请求SHA复核已分列记录；它们不是生产QQ。三协议72次生产来源重放与总655保存请求的数字、缓存覆盖、计时及费用反例仍见[正式报告](README.md)。受控费用等价不是真实账单或生产节省。

原请求、数据库、opaque、鉴权和QQ文本仍仅在私有ignored材料。提交只包含匿名汇总及测量文件；原655条/571组/8行自然CSV的SHA保持不变。

## 用户追加：全部会话执行/ai new

用户明确要求全部会话开启新上下文。UTC14:56:04–14:58:13，沿现有operations.reset_conversations离线入口执行批次runtime-fresh-20261012-1455；只停止并重新启动Bot原镜像，未伪造QQ入站或向群发送命令。

18个canonical Conversation全部generation恰好+1，各自starts_after等于原last_event；18条原批次回执的prior/new/floor逐个相符。实际apply回执newly_reset18/already_reset0；后续只读preview already_reset18/newly_reset0，没有重复apply。8项原对账与独立逐会话重算通过，FK0。原SELF eligible集合为空，实际rebound0；204个Work原已全部终态（completed183/cancelled8/failed13），本次没有新增取消8个的事实。

旧公共observations/selections和Rollup按原/ai new合同退出；真实Chat92430、Memory事实2559数量前后相同。UTC15:00:51只读核查旧generation非空projection0、旧generation selection0；当时各1条新observation/selection属于新generation，不能写成全库永久0或删除全部长期知识。全局ShortState/插件当前有效资料仍沿现有producer首份供给，按需检索真实旧事实仍可用。

142份来源的原件保留证明仅指前面的0108迁移阶段；之后用户要求/ai new，按原合同推进generation和执行fence，并退出旧scope的公共来源。原私有合同源码未改，不等于这次重置没有推进fence或旧执行可以跨新generation重放。

UTC15:00:00复核：Bot原容器与原image，StartedAt14:58:13.432616944Z，Docker healthy/restarts0、OneBottrue、plugins2、Memory consistency healthy、Work active/wait0且最后错误为空；SnowLuma原ID/启动时刻/镜像不变。重置后新自然请求另开窗口，不与首个空窗或旧generation作相同输入费用对照。

迁移和重置后，确认本次临时文件没有容器挂载、打开文件或systemd引用，删除一致0107临时备份及其WAL/SHM、已装载镜像的传输包，共1803345135字节（约1.68GiB）。实际Bot数据、挂载、已装载image和当前Compose覆盖文件保留；这是一次性运维清理，没有增加长期备份或清理任务。

## 重置后的真实群聊窗口

UTC **[2026-10-12T14:58:13.432616Z, 2026-10-12T15:01:46Z)**。采到同一真实user_message激活的两次74工具Main，generation23，无Work；0私有Work compaction/Memory入口。两份provider_response实际HTTP200、两份model_end completed，有原响应数字，不是只捕到prepared。原路由是Gemini；本窗没有DeepSeek生产请求，两协议DeepSeek受控实测仍按655档案单列。

| 该自然窗口 | 已知数字/边界 |
| --- | --- |
| Main物理响应 | 2×HTTP200，0已观察HTTP失败；原逻辑调用2次 |
| 请求/实际返回模型 | gemini-3.8-flash / gemini-3.8-flash-n，原名字分别记录；返回别名不用于证明实际收费档位 |
| input/cache/miss | 33189 / 24086 / 9103，覆盖2/2，加权缓存率72.5722378% |
| raw文本输出 | 合计36；不能把只已知一条的thought补成第二条0 |
| thought | 已知小计175，覆盖1/2；归一completion合计211不证明缺失thought实际为0 |
| 名义输入等价 | 按已核Gemini Flash miss/cache单价0.75/0.075 USD每百万计算0.008633700 USD；实际账单、完整模型费未知 |
| 资料新增与重读 | scene首份source/selection各1；第二请求重读同一基线，新发布0。没有退休relationship、derived dynamic snapshot、默认身份refs或三空槽包装 |
| 发送与收尾 | 首次模型toolcall1，send_message实际success1/error null；后续模型0 toolcall/completed，runtime观测sent_messages1/error null |
| 发送续轮 | 原functionResponse回执ok/mutation_committed/status均成功；第二wire以user functionResponse承接，没有自己outbound普通文本echo |
| 时延 | runtime原整轮20.746秒，工具审计0.467280秒；不叫纯HTTP或QQ客户端接收耗时，不能与探针响应头耗时混比 |
| 精确operation计时 | model_phases logical两次12.302107/4.546833秒；await httpx.send(stream=False)两次11.890111/4.415165秒。不是响应头指标或近似连接的invocation/AGM耗时 |
| 客户端确认 | 未采到真人QQ端接收确认；模型看到成功回执/后端入账与用户实际收到分别记录 |

根代理另只读核真实发送工具审计及其回执内部event对应的outbound/yuki/social_tool/keeper账本，与原入站同canonical会话。精确连接依据是原回执event_id；caused_by_event_id实际为空，不靠相邻编号或平台ID补造因果。两次模型调用与invocation的逐条operation连接仍按原collector覆盖记录，不能从接近时间硬配计时或费用。

这是重置后的一个会话、一轮、两个请求，不与旧版71.4214%、受控三协议或首个空窗直接算收益；新generation、历史长度、热度和任务均不同。稳定资料首份1/续轮新增0是本样本的真实表达频率，不外推全部会话或所有变化状态。原请求/回答只留私有材料，公开逐响应数字见[新窗口CSV](postdeploy-after-reset.csv)。E07/V10/V14保留未勾：媒体、非空short_state变更/清空、SELF/自动化/显式SDK及完整真人验收仍缺自然样本。
