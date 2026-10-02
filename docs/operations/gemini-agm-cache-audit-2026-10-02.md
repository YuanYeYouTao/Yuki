# Gemini 与 AGM 缓存及协议边界核查

2026-10-02 用户要求核查缓存命中率及 AGM 请求差异。本轮只读取自然流量、已持久化诊断
和部署 metadata，不发送模型或 QQ 测试请求，也没有修改 AGM、路由或 Provider 配置。
Yuki 容量计量修复及等长缓存窗口见[交付记录](compact-capacity-accounting-2026-10-02.md)。

## Yuki 普通首请求

采样截至台北 14:14:52.198654，起点为 `ops-8ca2e17` 实际启动 05:55:23。
最近 2000 invocation 中选取 32 turn，每 turn 最多 32 trace；根据实际
`turn_start → model_start → provider_start.operation_id → provider_response` 与内部源事件
核对 canonical actor，取得 28 个普通初始请求，排除 4 个非普通/已接纳 Work turn。
14 turn 达到 trace 上限；首请求已证，后续尾部未全部检查。

同 actor/conversation/generation 的下一次可验证首请求共有 21 对：路由及完整固定设置、
工具声明 21 对均相同；16 对保留完整旧 role/parts 前缀，5 对存在差异。
这是 next verified sample，可能跨缺样或排除项；历史 read-scope/epoch 未知，不称为全量同范围验收。
签名与媒体使用 trace 中保存的脱敏摘要引用，不声称原字节已独立重放。

定向查证 5 对差异：

- 2 对从第 0 part 改变，正常 summary 改为 `Incomplete emergency conversation view`，
  源覆盖从内部事件 79066 到 79519，实际摘要表示变化已证。
- 3 对在 part 266/270/274 改变，动态 envelope 变为普通历史包装，旧事件正文保留。
  同 epoch 合同要求完整冻结 envelope 保留，显式新链可重新从 raw 构建；当前投影留下
  `capacity/revision=10` metadata，但后续失效已清除内容，不能证明这几轮的历史 epoch 或原因。
  不按时间硬配可替换投影行，也不把这些差异直接判为同 epoch 违规。

28 个首请求的同 operation 物理响应中，input 全部已知，cache 19 已知、9 未知。
已知 cache 样本 input **708,836**、cached **616,830**、未缓存 **92,006**，加权命中率
**87.0201%**；未知 cache 请求 input 为 **391,174**。NULL 不当作 0。
这不是等长窗口逻辑 chat 样本；invocation 没有 operation ID，不把同 turn 的多条用量硬配。
28 响应均没有可用 `responseId`，不能与 AGM log ID 强行关联。

## AGM 计量与最终请求形状

AGM 为 `antigravity-manager:gemini-request-correlation-v4.8.4`，运行健康；本轮未重启。
采样截至台北 14:16:11，最近 500 行覆盖 Yuki 旧版启动后窗口，窗口没有被行上限截断。
这是混合客户端/任务的网关样本，模型映射为 `gemini-3.8-flash → gemini-3.8-flash-tiered`。

145 次成功调用：cache 已知 74、未知 71；已知样本 input **2,662,634**、cached
**2,305,609**，加权命中率 **86.5913%**。未知 cache 的 input 为 **1,053,208**，其中
25 次 input 不少于 20k；不能把缺失都解释成短辅助请求，也不能与 Yuki 样本直接比较。
规范化记录的 usage 与 **AGM 数据库**字段一致，没有 input/cache 不一致；这不是独立
上游原始响应证据。缺失与显式零的区别继续保留。

146 条 `final_upstream_shape` 有 62 session。84 对同 session 相邻观察中，完整
system/tools/generation/safety hash 都稳定，首项前缀 84 对稳定；前 8 项在可比较的
83 对中也稳定。较长前缀仍有变化：32 项为 77 同/4 变，64 项为 40 同/4 变，128 项为
4 同/2 变；13 次 contents 数减少、71 次非减少。任务、分支与压缩原因未关联，不能仅凭
前几项或 hash 把它升级为完整链/语义验收。

145 个 recorded log ID hash 与 146 个 shape join hash 没有精确匹配。当前 correlation
补丁的 join 算法未定位，不能猜测关联或按时间将 cache 数值配给某个最终 shape。
这些最终形状 hash 按进程加盐，只能在该进程内比较。唯一 HTTP 400 为 `INVALID_ARGUMENT`，
没有可归因于缓存的已知 metadata，不能据此解释 miss。

## 入站与上游诊断副本的差异

有界比较最近 32 条同记录入站与上游诊断副本：30 条 system/tools/toolConfig 不同，
32 条 generationConfig 不同，21 条 contents parts 不同。副本可能位于最终归一化之前，
不能直接称为最终 HTTP 请求体；这也不独立证明缓存失效。

其中 6 对进一步结构核对：system 正文逐 part 及拼接均相同，仅补 `role=user`；工具
名称、数量、说明保留，schema 有别名转换、类型大写及不支持约束清洗。回执角色归一化
与 thinking 锚点处理存在对应源码路径；本轮未逐条证明签名/回执丢失，也不宣称转换完全无损。

输出参数存在需要继续核验的语义风险：诊断副本中 `maxOutputTokens` 从 8192/16384
变为 65536，`AUTO/ANY` 的 toolConfig 被移除，thinking 则有大小写与 includeThoughts
转换。原 v4.8.4 wrapper 的自适应输出上调及模型 cap 可解释该副本，但目前完整部署补丁
源码未取得；最终 wire 日志只记录生成设置 hash、thinking 枚举及预算 flag，不含输出上限数值。
因此这几条实际 final 输出上限仍未知，不能把副本观察冒充最终参数缺陷已证。

## 适配设计建议

建议后续增加显式 Gemini 网关方言/能力配置与离线合同 fixture；这是一项设计建议，
现有 Yuki 没有因此新增 AGM 方言字段、Provider 类或配置迁移。

- 明确网关对输出上限、thinking、tool choice、usage 缺失和原签名的实际保证。
- 核对入站、最终上游请求及返回原生 usage，使用真实请求关联，不能只比诊断副本或短前缀。
- 若最终请求核验确认客户参数被不当覆盖，优先在 AGM 修正；必要的网关差异停在模型协议适配层，复用原
  Runner、持久身份、执行授权和恢复机制，不做反向参数猜测或内容路由。

目前没有证据将缓存问题归因于 AGM 每次改写静态前缀，也没有证明新建 Provider 会提高
命中率。Google 的[缓存说明](https://ai.google.dev/gemini-api/docs/generate-content/caching?hl=en)
将共同前缀和相近请求时间作为提高隐式命中机会的建议，并不保证每次命中；该公开政策
也不能自动视为 AGM 所用上游端点的完整承诺。

所有本地机器证据保存在 ignored `.cache`，不进入生产配置或 PR 正文：
`harness-initial-prefix-ops8ca2e17-current.json`、`harness-initial-mismatch-ops8ca2e17.json`、
`agm-cache-presence-20261002.json`、`agm-wire-transform-20261002.json`。
