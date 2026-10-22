# 聊天记录读取

Main 使用 `query_chat_history(view=recent/search/around)`。Social、个人入口、Client、Plugin
共用 `EventLedgerRepository.query_history/history_around` 和原事件事实提取，各接口只组自己的信封。
查询使用同一只读快照，没有 DML；原作者、Conversation、授权与已发生回执沿共同开发约束。

## 范围与页

显式查询读取 Yuki 实际参与并已入账、当前真实来源获准读取的完整原账本，覆盖各平台。
目标认内部 ID，Binding、Person、Space 和 Conversation 使用同一读取授权，不要求 App/ACP 来源。
当前 actor、委托范围、插件批准、keeper 及隐私沿原合同；每条结果保留原 Conversation、平台和作者来源。
`/ai new` 只清理聊天上下文，不删除永久账本或截断显式查询；自动上下文的 starts_after、
FrozenFragments 与 generation 继续沿原上下文和原执行合同管理。
recent 未指定 target 时浏览当前 Conversation；search 未指定 target 时一次检索所有当前获准的已入账
Conversation，不按平台名单枚举。显式 Person、Space 或 Binding 可选择多个真实入口，直接在该集合内
查询，不要求模型逐个挑选候选。around 未指定 target 时由内部 event_id 定位原 Conversation。
结果的 sources 给出可读会话名与来源目录；每条事件保 conversation_id、platform、scope 和 thread_id。
跨入口命中作为有来源的工具证据返回，不拼接各 Conversation 的自动上下文。
人物筛选使用原作者 Person 的当前同人族；账号转移、改名、解绑或无账号不改写旧作者。

`operation_id` 沿原发送回执定位。`SocialOperation.event_id` 是存活 FK，原事件和 Conversation 另存永久事实；
原件删除后返回 missing 与原效果状态，不能用 recent 或后来消息代替。回执继续核原来源归属，
另传锚点按其真实 Conversation 和当前读取授权定位，不借回执扩大委托范围。

单会话与跨会话页均按内部整数事件 ID 排序，用 N+1 真实命中证明 has_more，next_cursor 指最后返回的命中。
recent 向较小 ID、oldest 向较大 ID 前进；删除页尾不妨碍续读。页间实时变化可改变后页，不另设
签名、epoch 或静态导出协议。SQLite 原生 AUTOINCREMENT 保证事件 ID 在删末尾或清空后仍不复用。
limit 为正整数，没有 50/100 条经验上限；before/after 配置只作缺省值，不截断显式请求。

邻文沿各锚点原 Conversation、event_kind 和读取授权，按锚点 ID 分别有界读取。邻文不继承
关键词、作者、日期和媒体筛选；与 matches 去重后标记 context，不扫描远隔锚点之间的整段。
Artifact 的 offset 只翻阅一次调用已经保存的载荷，聊天 cursor 才查询下一批原事件。

## 字面匹配与索引

完整字段按 NFKC、casefold、NFKC、Unicode 空白折叠处理；基础表示与完整 OpenCC t2s 表示
分别匹配。正文和每个 current ASR 片段独立，不匹配回复 ASR、JSON 键或拼接片段。
all/any/phrase 与 exclude 均为字面语义，最终精确核验。原文、原提及、两种时间和稳定附件编号
原样呈现；ASR-only 命中返回实际转录证据。附件过期不删消息元数据，也不重新下载或调用模型。

唯一 `chat_history_fts` 使用 Conversation 前缀的完整 1/2/3-gram、contentless-delete、detail=none。
每事件最多追加两个稳定原作者词元（Person、author_kind），不复制 gram，不存当前 root、权限
或 generation。文本和作者条件合成一次 MATCH；人物族只在本页读快照解析一次，原 SQL 再核真实作者。
日期和媒体条件用现有或必要普通索引缩小候选，再用同一 matcher。没有逐 rowid 重启 MATCH、
常态补缺查询、扫描预算、缓存结果旁路或运行时索引就绪服务。

ID 页可 seek 后早停；全文成本取决于真实倒排与精核文字，日期窗口可能需要局部排序。
仅 exclude、全部 gram 假阳性，或很宽的日期/媒体范围叠加稀有词，最坏仍核完整合法候选集合。
实验见[查询及维护测量](experiments/chat-history-448-2026-10-22/README.md)，合成读数不冒充线上延迟。
CPU 转换退出事件循环；取消会中断底层 SQLite 并等待连接清理，独立 OpenCC 本机调用不能硬抢占。

## 写入与升级

真实入站、出站、外部事件、编辑、ASR 和遗忘在首次写入前准备非命名 gram，按实际裁定的
Conversation 在原事务中同步替换 FTS。纯资料、视觉摘要与当前路由变化不重建文字索引；删除由
固定触发器清退。来源复核和 BUSY_SNAPSHOT 重备沿原事务，writer 内不做 OpenCC 或历史扫描。

升级需停业务 writer。0144 准备最终结构、原回执来源和原生事件序列，0145 发布完成版本。
两者之间锁外分页转换、短事务直接装载最终索引。中断时只重建未完成派生结构；完成后正常启动
不扫全库、不再回填。`init-db` 与支持的 Alembic upgrade 目标复用该入口；`--sql` 无法执行数据
转换，不能伪造完成 head。部署镜像要求真实 SQLite contentless-delete 能力，冻结旧桥和 Memory FTS 保留。

## 复制结果与隐私

Host 用实际 source_event_ids 与读取时隐私代次跟踪复制结果，独立于模型载荷及激活事件集合。
组合调用、Work 证据、Memory 工具回执和 Artifact 保同一来源；遗忘按这些原事件清退跨会话副本，
并沿各消费者原提交边界核验，迟到发布不能复活正文。查询不加入 Main 的输入依赖，也不新增跨消费者锁。
旧已付请求、旧调用键、预算和真实发送回执保持原事实；退役工具只读原 accepted receipt，不注册新执行别名。
