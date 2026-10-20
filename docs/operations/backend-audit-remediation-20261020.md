# 后端审查修复进度

源基线 `1d831151`，测试基线提交 `3848afef`。依据[五轮报告](backend-five-pass-audit-20261020.md)、[SQL审计](sqlite-query-scan-audit-20261020.md)和[测试记录](backend-audit-tests-20261020.md)实施。用户已授权修复、两轮终审、PR、合并及Bot-only上线；每项完成即在本表记录，不以本地测试代替CI、合并或生产验收。

修复沿原Person、Conversation、来源、执行和回执，优先删除旧链及不相关判断。状态“已修/已验”只对应代码和所列验证；没有证据的容量观察单独写明保留理由，不冒充已修故障。

## 行为与算法

| 项目 | 内容 | 负责 | 状态 / 验证 |
| --- | --- | --- | --- |
| F01 | 原正文保留 | 主审 | 已修/定向已验；正文/提及/人物/增量上下文31 passed，删除旧猜测正则与调用 |
| F02 | 已知插件结果与辅助audit分开 | B | 已修/定向已验；与F03/P09及原claim组合30 passed，实际结果不被audit重试推翻 |
| F03 | Embedding部分成功不重复购买 | B | 已修/定向已验；与F02/P09组合30 passed，按实际Provider批量claim；interval3600仍连续排空批次，成功批不重购 |
| F04 | 旧插件资料及已返回结果不套新run资格 | B | 已修/定向已验；三处资料gate已收，含长历史/人物合并15 passed，新run原资格保留 |
| F05 | Vision按实际原件复用 | C | 已修/定向已验；与F07组合12 passed（Windows），hash复用保留 |
| F06 | 编辑提及复用原生快照 | A | 已修/定向已验；与F08/F09/P01最终原App/ACP/编辑/历史组92 passed；旧中间态红例已补修转绿 |
| F07 | Unicode搜索与分页原文坐标 | C | 已修/定向已验；与F05组合12 passed（Windows），原文span与next_offset保留 |
| F08 | 已认证App设备保原人物 | A | 已修/定向已验；公开App解绑链与原来源组合31 passed |
| F09 | 历史读取不套新发送资格 | A | 已修/定向已验；Social原回执/委托/原source组合31 passed |
| F10 | schema本地资源解析及普通数据边界 | C | 已修/定向已验；schema/实际目录28 passed及nested local resource回归，删除枚举和任意defs提升 |
| F11 | 插件临时媒体删除保恢复线索 | B | 已修/定向已验；unlink失败保恢复线索，首次接纳expiry/hash同writer复核，cleanup/publication屏障及原receipt已验；旧expiry三种合法原串CAS反例先红后绿，原0128不改fixture及邻近68 passed |
| F12 | 重置上下文不取消原表情收藏 | C | 已修/定向已验；真实TG与shared 21 passed（Linux），edit/forget保留 |
| P01 | 附件定点授权 | A | 已修/定向已验；event引用与effect原domain_ref定点查找，含真实Work/effect引用与8→512增长9 passed；0143回填/guard已验 |
| P02 | 入口/Work视图批读 | A | 已修/定向已验；Home/同步批读，含原SSE/文件/Work/Person 90 passed |
| P03 | ACP空增量不拉尾页 | A | 已修/定向已验；ACP空增量不查尾，真实App/ACP/历史组合92 passed |
| P04 | 群恢复只依赖实际平台和候选 | A | 已修/定向已验；群恢复移除无关SELF门槛，真实入口/历史组合92 passed |
| P05 | 调度器有界选择会话head | 主审 / C | 已修/定向已验；会话keyset轮转、原最早eligible单项；积压/lease/recovery/环绕及邻近Linux66 passed |
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

## 交付检查

| 阶段 | 状态 / 证据 |
| --- | --- |
| 测试基线 | 已提交3848afef；main同步核对1d831151 |
| 迁移 | 0143冻结；artifact refs原件回填与触发器、实际索引、Session死列删除；非空Session/消息/原人物/Work/回执保留及guard组合14 passed |
| 定向回归 | 原51个strict xfail已全部移除并转真实回归；各分区实际Linux/Windows定向通过，Ruff全仓绿、Linux632源Mypy绿；不把重叠组累加为独立总数 |
| 第一轮终审 | 已完成；A审root/C、B审A/C、C审B、主审核迁移/事实/索引/消费者；发现并补修F11并发重复发布原回执优先，新增3条barrier先红后绿 |
| 第二轮终审 | 已完成；C复核F11修复/调度恢复，B复核冻结迁移/API7/原数据，A复核CI/正文/媒体/Schema及3处当前fixture；无剩余阻断。D08前后渲染25430B逐字相等，F01正文恢复为明确有意变化 |
| PR / CI / main | 主仓PR #487；6d62c544集成716 passed零skip、checks绿；完整unit发现3项：两条旧正文strip断言已按F01纠正，旧expiry原串CAS实际缺口已修。Native完成编译但被runner的可写/opt阻止，workflow已按原合同修正，重跑中。两个独立插件已过CI合并 |
| 镜像 / 升级 / Bot替换 | 精确6d62c544 direct镜像已在本地预构建；线上2.54GB一致备份integrity ok/FK0，0143仅在独立副本演练中；未替换Bot |
| 线上验证 / 清理 | 尚未执行；上线后核自然流量及实际引用，再清旧备份、部署临时文件、废镜像和可重建缓存 |

## CI 补修

- 原生任务实际构建了固定 worker/binding/launcher，AppArmor与原namespace/IPC/parent-death验证已运行，最后Host配置检查拒绝runner的可写 `/opt`。同一官方runner镜像配置明确设为0777；仅在CI将该祖先改root:root/0755并输出前后stat，运行时检查与隔离不变。离线真实UID权限反例先拒绝、修后通过；90项原生执行仍以新CI为准。
- 首次完整unit为3102 passed、3 failed。两条旧Social测试仍要求删普通 `#62052>` 正文，现保留原不确定ACK/原事件/不重发断言，只纠正正文期望（正文/原Social组16 passed）。另一条冻结0128回归揭露旧expiry读取为datetime再绑定后格式改变；最终删除CAS改为保原stored string，不改到期资格、不重写数据。新增三种旧格式先3红，原0128原fixture及清理/发布回执组68 passed。
- API7的3处当前fixture改用公开版本常量，原48项通过；历史迁移版本原文保留。
