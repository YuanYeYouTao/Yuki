# 后端审查修复进度

源基线 `1d831151`，测试基线提交 `3848afef`。依据[五轮报告](backend-five-pass-audit-20261020.md)、[SQL审计](sqlite-query-scan-audit-20261020.md)和[测试记录](backend-audit-tests-20261020.md)实施。用户已授权修复、两轮终审、PR、合并及Bot-only上线；每项完成即在本表记录，不以本地测试代替CI、合并或生产验收。

修复沿原Person、Conversation、来源、执行和回执，优先删除旧链及不相关判断。状态“已修/已验”只对应代码和所列验证；没有证据的容量观察单独写明保留理由，不冒充已修故障。

## 行为与算法

| 项目 | 内容 | 负责 | 状态 / 验证 |
| --- | --- | --- | --- |
| F01 | 原正文保留 | 主审 | 已修/定向已验；正文/提及/人物/增量上下文31 passed，删除旧猜测正则与调用 |
| F02 | 已知插件结果与辅助audit分开 | B | 已修/定向已验；与F03/P09及原claim组合30 passed，实际结果不被audit重试推翻 |
| F03 | Embedding部分成功不重复购买 | B | 已修/定向已验；与F02/P09组合30 passed，按实际Provider批量claim；interval3600仍连续排空批次，成功批不重购 |
| F04 | 旧插件资料及已返回结果不套新run资格 | B | 已修/定向已验；原资料资格分离及TTL残留补齐22 passed，已接纳结果跨自然期限/expire_due仍保存返回；新run实际资格、原owner及真实删除保留 |
| F05 | Vision按实际原件复用 | C | 已修/定向已验；与F07组合12 passed（Windows），hash复用保留 |
| F06 | 编辑提及复用原生快照 | A | 已修/定向已验；与F08/F09/P01最终原App/ACP/编辑/历史组92 passed；旧中间态红例已补修转绿 |
| F07 | Unicode搜索与分页原文坐标 | C | 已修/定向已验；与F05组合12 passed（Windows），原文span与next_offset保留 |
| F08 | 已认证App设备保原人物 | A | 已修/定向已验；公开App解绑链与原来源组合31 passed |
| F09 | 历史读取不套新发送资格 | A | 已修/定向已验；Social原回执/委托/原source组合31 passed |
| F10 | schema本地资源解析及普通数据边界 | C | 已修/定向已验；schema/实际目录28 passed及nested local resource回归，删除枚举和任意defs提升 |
| F11 | 插件临时媒体删除保恢复线索 | B | 已修/定向已验；删除不可变字段CAS及读取完成后的第二TTL门槛。首次接纳核真实存在/owner/期限；cleanup按原pending引用串行后在writer外unlink，失败保记录。71项媒体/通知/runner组合通过，原0128及合法旧时间格式保留 |
| F12 | 重置上下文不取消原表情收藏 | C | 已修/定向已验；真实TG与shared 21 passed（Linux），edit/forget保留 |
| F13 | 精确私有恢复仍接纳新公共输入 | A | 已修/定向已验；真实native的7红以Host原入口同样复现，最终51 passed/8 native deselected，保16种协议/缓存对照、摘要与后续普通激活、晚到事件、原内部会话及无历史读取资格；最终native待本PR CI |
| P01 | 附件定点授权 | A | 已修/定向已验；event引用与effect原domain_ref定点查找，含真实Work/effect引用与8→512增长9 passed；0143回填/guard已验 |
| P02 | 入口/Work视图批读 | A | 已修/定向已验；Home/同步批读，含原SSE/文件/Work/Person 90 passed |
| P03 | ACP空增量不拉尾页 | A | 已修/定向已验；ACP空增量不查尾，真实App/ACP/历史组合92 passed |
| P04 | 群恢复只依赖实际平台和候选 | A | 已修/定向已验；群恢复移除无关SELF门槛，真实入口/历史组合92 passed |
| P05 | 调度器按空闲名额选择会话head | 主审 / C | 已修/定向已验；删除把capacity当候选检查次数的门槛，沿会话keyset轮转至填满名额或一圈结束；7个在跑/1空槽且前方候选deferred的反例先红后绿 |
| P06 | 定点run沿父链确认归属 | 主审 | 已修/定向已验；定点规模及sandbox/多owner/原settlement/实际恢复邻近118 passed |
| P07 | 工作区发布移出历史TTL维护 | C | 已修/定向已验；规模/I/O组合6 passed，维护沿原startup/60秒owner |
| P08 | ASR/视频阻塞文件I/O退出Host loop | C | 已修/定向已验；I/O/ASR 26 passed，新增取消清理回归 |
| P09 | Dream空增量与重复向量处理 | B | 已修/定向已验；空分区跳过、向量单次归一化/normalized dot、CPU退出loop；含邻居/调度组合60 passed |
| P10 | 等价自动化一次完整遍历 | B | 已修/定向已验；去OFFSET循环，含尾部匹配及owner/name/max_runs 3 passed |
| P11 | 归档维护不遍历已归档历史 | 主审 / C | 已修/定向已验；未归档候选向上找root，保活动后代；规模/生命周期/原资源组合Linux66 passed |

## 已有SQL问题

| 项目 | 内容 | 负责 | 状态 / 验证 |
| --- | --- | --- | --- |
| S01 | 入站名称只查本次规范键 | A | 已修/定向已验；本次名称规范键批查，点查/引用邻近57 passed |
| S02 | 私聊Account定点查归属阶段 | A | 已修/定向已验；Account定点归属阶段，点查/引用邻近57 passed |
| S03 | Presence/群路由从真实候选驱动 | A | 已修/定向已验；真实Presence候选驱动，点查/引用邻近57 passed |
| S04 | 插件Job按原Work可调度候选查询 | B | 已修/定向已验；原Work驱动热查、启动及60秒原终态对账；paused/terminal历史50→2000及恢复组合25 passed |
| S05 | Memory到期范围索引 | B / 主审迁移 | 已修/定向已验；实际到期谓词范围索引，50→2000无关历史与正向回归通过 |
| S06 | Reflection实际谓词候选索引 | B / 主审迁移 | 已修/定向已验；claim/retry/report原谓词索引，50→2000无关终态历史与paid报告通过 |
| S07 | CodeMode固定JSON路径命中既有索引 | C | 已修/定向已验；实际EXPLAIN/VM通过，Linux消费者组65 passed无skip/xfail |
| S08 | 投影容量只遍历活跃views | 主审 | 已修/定向已验；active覆盖索引，真实publication/淘汰及16→640失效历史通过；0143/guard组合14 passed |

## 删除与策略整理

| 项目 | 内容 | 负责 | 状态 / 处理依据 |
| --- | --- | --- | --- |
| D01 | 后台runner故障范围收窄 | B | 已修/定向已验；单runner失败不移除全插件，实际双runner/普通tool 2 passed；初始化及明确stop保留 |
| D02 | 九个旧裸号helper及两层require包装 | A | 已删；九个裸号helper及admin/common两包装，保真实隐私消费者；邻近原Person/入口回归通过 |
| D03 | gateway doctor声明与能力一致 | A | 已修；doctor仅声明现行SnowLuma诊断，TG真实能力保留 |
| D04 | operator/目录搜索64经验限制 | A | 已删；operator数量/目录搜索64经验cap；真实并发8保留 |
| D05 | TurnCoordinator长期小状态 | 主审 | 复核保留；state保原Conversation版本防旧token复活，无现网泄漏证据；不加TTL/第二代次机制 |
| D06 | artifact总量核算与发布锁 | 主审 | 复核保留；SUM在reader、文件发布锁与SQLite writer分开，真实全局容量与并发发布一致性不能由漂移计数替代 |
| D07 | 独立PluginSession无效字段/交集链 | B | 已删/定向已验；API7.0、session.v2移除未生效字段/交集链，保真实max_model_requests；外部插件当前SDK 99+21 passed |
| D08 | EventPromptIndex前驱及无消费者Bot推断 | 主审 | 已删/定向已验；索引/assembler/history透传整链删除，58 passed，真实入站回复判定保留 |
| D09 | Gemini payload内局部查找和历史复用 | C | 已修/定向已验；协议及付费恢复66 passed，Linux消费者65 passed；保opaque、签名和原配对 |
| D10 | Work观察JSON/hash重复处理 | 主审 | 复核保留P3观察项；原物理去重有效，mutable观察仍追加真实results；重构不可变快照超出此已证7.45ms成本，不加对象ID缓存 |
| D11 | 加速索引不作为启动门槛 | 主审 | 已删新增及旧的全部纯性能索引启动拒绝链，净减300余行；删除全部非唯一索引的反例先红后绿，startup/0142/0143组合21 passed；迁移、实际查询计划与规模测试保留 |
| D12 | 群恢复不依赖无关全平台状态 | C | 已删/定向已验；无关同平台connect/disconnect/revision真实3红后修，恢复及跨平台46 passed；只核所选阶段/连接、目标revision、原actor及回执，原多候选处理保留 |

## 交付检查

| 阶段 | 状态 / 证据 |
| --- | --- |
| 测试基线 | 已提交3848afef；main同步核对1d831151 |
| 迁移 | 0143冻结；artifact refs原件回填与触发器、实际索引、Session死列删除；非空Session/消息/原人物/Work/回执保留及guard组合14 passed |
| 定向回归 | 原51个strict xfail已全部移除并转真实回归；各分区实际Linux/Windows定向通过，Ruff全仓绿、Linux632源Mypy绿；不把重叠组累加为独立总数 |
| 第一轮终审 | 已完成；A审root/C、B审A/C、C审B、主审核迁移/事实/索引/消费者；发现并补修F11并发重复发布原回执优先，新增3条barrier先红后绿 |
| 第二轮终审 | 已完成；C复核F11修复/调度恢复，B复核冻结迁移/API7/原数据，A复核CI/正文/媒体/Schema及3处当前fixture；当轮未发现其他阻断；后续完整native及反防御性审查发现的补修见F04/F13/D11。D08前后渲染25430B逐字相等，F01正文恢复为明确有意变化 |
| PR / CI / main | 主仓[PR #487](https://github.com/YuanYeYouTao/Yuki/pull/487)；此前a6b3c242 unit3108/集成716均零skip通过，实际native79 passed/11 failed已据此补修。最终源Ruff、Linux632 Mypy及actionlint通过；完整最终CI及main合并以本PR精确head检查和合并记录为准。两个独立插件已过CI合并 |
| 镜像 / 升级 / Bot替换 | 线上2.54GB一致备份及0143独立副本110业务表/81引用原件对照、全integrity/FK0已通过；旧预镜像不作为最终交付。最终精确镜像、实际迁移与Bot-only替换记录见[本PR交付回执](https://github.com/YuanYeYouTao/Yuki/pull/487) |
| 线上验证 / 清理 | 以[本PR交付回执](https://github.com/YuanYeYouTao/Yuki/pull/487)记录实际健康、原身份/回执、自然流量及同行容器状态；健康确认后按实际引用清旧备份、临时文件、废镜像和可重建缓存，代码通过不推断生产验收 |

## CI 补修

- 原生任务实际构建了固定 worker/binding/launcher，AppArmor与原namespace/IPC/parent-death验证已运行，最后Host配置检查拒绝runner的可写 `/opt`。同一官方runner镜像配置明确设为0777；仅在CI将该祖先改root:root/0755并输出前后stat，运行时检查与隔离不变。离线真实UID权限反例先拒绝、修后通过；90项原生执行仍以新CI为准。
- 首次完整unit为3102 passed、3 failed。两条旧Social测试仍要求删普通 `#62052>` 正文，现保留原不确定ACK/原事件/不重发断言，只纠正正文期望（正文/原Social组16 passed）。另一条冻结0128回归揭露旧expiry读取为datetime再绑定后格式改变；最终删除CAS改为保原stored string，不改到期资格、不重写数据。新增三种旧格式先3红，原0128原fixture及清理/发布回执组68 passed。
- API7的3处当前fixture改用公开版本常量，原48项通过；历史迁移版本原文保留。

## 反防御性追加审查

用户追问新增判断后，独立复查实际diff。确认本轮新增的性能索引缺失拒启属于过度防御；同类旧拒启链一并删除，没有换成warning、自动补索引或新开关。索引缺失只影响速度，真实索引仍在冻结迁移建立，并由查询测试验证。新版本、表列及事实维护触发器沿原结构合同核对。

F04的paid结果跨自然TTL被过滤旧残留已补齐；D12同时删除群恢复对无关全平台连接的依赖，保真实候选、选中阶段撤权与原回执。F13为原生真实执行发现的公共输入缺口，单独补修，不以改测试掩盖。四项provider-wire fixture没有真实接纳的公共事件，恢复应保原调用配对/签名/创建输入，已修对应旧断言；真实新事件接续由F13独立验收。

追加交叉审查已完成root索引门槛删除、B Session资料/执行分界及C群恢复的双视角复核。上下文首版cache miss仅delta冷建会丢公共基线的风险在提交前被指出并返修；还须验证公共摘要/连续历史、下一普通激活、晚到事件选择范围及插件原历史授权，不能以当前ORD出现代替最终验收。

追加两轮终审完成：B/C分别复核最终root、Session、群恢复与公共接续源码；第一轮发现并返修空cache基线、公共晚到选择范围及无历史授权入口，第二轮将Work依赖收窄为实际rendered事件，并修原内部Conversation与scope key同步。raw-scope真实反例先红，最终51 passed/8 native deselected。最终源码无剩余静态阻断；真实native90仍以修后CI为准，不用模拟通过替代。

## 用户追问后的三轮删除复审

用户指出代码量与禁止防御性编程的约束不符，合并和部署暂缓。这次从 `df2bd8da` 实际新增代码重新审，不用通过测试或产品代码净减少证明设计合理。

`df2bd8da` 的 CI 已完成：unit 3120、integration 732、真实 Native CodeMode 90，全部通过且零 skip；它们仅是删改前的基线，不能作为下面新源码的通过记录。

第一轮实际删除：

- F13 的第二套公共准备：`recovery_protocol` 标记、`load_history` 回调、空 PreparedHistory、冷缓存重备、元数据 None 分支及额外恢复 return。恢复沿正常 canonical prepare，原已付协议仍由原 WorkSession 恢复；未派发不发布公共历史。宽组89项通过，真实 Main.compose 的未派发9项通过；两组有重叠。
- F11 的不可变四字段 CAS、时间串补丁、读取完成后第二次期限拒绝；D01 已无异步工作的维护任务、集合及停止壳；F02 两个 caller 的重复审计失败处理；P10 中间匹配ID列表和第二次SQL。
- P05 把空槽数当候选检查上限及末尾重复分组/截断；P08 外层重复临时目录。P11 复用现有 Work 树查询，不保留两套递归。
- F01 未拔干净的旧媒体正文猜测。两条普通文字先真实失败，删除后正文/提及/原身份/增量片段52项通过。
- artifact 引用热触发器及0143回填的重复 DISTINCT；原 INSERT OR IGNORE 与三列主键已经去重。重复原引用、编辑、删除、冻结迁移及启动合同11项通过。0143内容因此变化，旧生产副本演练只对应旧文件 SHA，不冒称新文件已在生产演练。
- 更新现行文档中已经删除的独立插件会话工具能力交集，以及调度候选检查上限。

第二轮换区交叉审查进行中；第三轮终审待完成。每项继续以实际来源、必要授权与原回执证明保留理由，不为行数删除真实并发串行点，也不保留没有当前写入口的假想变化检查。
