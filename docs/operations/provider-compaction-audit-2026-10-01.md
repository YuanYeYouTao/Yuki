# 长任务重构的请求与计量核查（2026-10-01）

本报告保留上线前 Yuki 自然流量与能力核查，不代表新 Harness 已部署或缓存改善已验收。
上游代理的源码、服务器和部署资料不属于本仓库。自然请求审计只读；另有一次微小、
无工具和 QQ 发言的结构输出能力请求，没有保温流量或大窗口探测。

## 原请求身份与用量

Yuki `model_invocations` 中同一真实群聊轮 `86d7713f46d4458f9cb056e0b912ed9c`
使用连接 `connection_2d4ba9aad301`、Gemini native、`gemini-3.8-flash`。
按内部源事件 **78943** 读取有界索引 trace，未按 QQ 号或消息正文重建所有权。
完整规范化请求曾与上游代理入站严格关联；签名只按哈希与字节数比较，不输出原值。
时间仅用于筛候选，不代替同请求身份。

| Yuki trace / invocation | input / output / cached | Yuki 物理请求数 |
| --- | --- | --- |
| 46179 / 50832 | 35057 / 139 / 32158 | 1 |
| 46199 / 50834 | 35565 / 61 / 32155 | 1 |
| 46210 / 50835 | 35910 / 17 / 32151 | 1 |

Yuki `provider_response` 的 Gemini usageMetadata 与对应 invocation 数字一致，
总数为 input+output，三次缓存字段均明确存在，unknown usage 为 0。
样本未出现 thoughtsTokenCount，不能据它验收思考 token 合计。
分项与缺失值核查背景见[供应商切换记录](provider-cutover-worklist-2026-09-29.md)。

## 前缀与证据边界

三次请求 contents 数依次 87、89、91；Yuki 客户端静态字段与原 contents 的逐项摘要
保留前缀。外部预览可能简化或重排字段，不能据预览变化认定 Yuki 已提交的 wire 被改写；
发送点诊断也不等于 Google 端原始抓包，不覆盖 HTTP 库内部不可见重试。

本轮三次已知输入加权缓存比例为 96464/106532，约 90.55%。它证明同链续接正常的一个样本，不能代表
跨聊天轮、不同账号、压缩新链或所有最近流量。统计与 unknown 口径见
[成本模拟](context-cost-simulation-2026-10-01.md)。

## 容量与辅助格式验证

- 完整请求容量沿真实 serializer 估算并受可选 Profile 上限约束。历史外部计数接口对
  同一请求返回 18383，实际 generation input 为 20927，加入/去除 system 的计数不变；
  因而不能把该接口当作包含 system/tools 的完整计量，也不能给差值套固定修正公式。
- 模型目录声明与历史 session 累计恢复阈值都不认证单请求实际可用容量。该次 96k 聊天/
  128k Work 是当时可热更的政策预算，没有通过大请求做边界探测，不能当作现行固定上限。
- 本样本未携带 `cachedContent`。创建/管理入口、可用资源名和账单未经完整验证时，
  不能宣称启用了显式缓存，也不添加额外保温流量。
- 沿当时真实连接与 Gemini serializer 做一次小型格式验证：`response_format=json_schema`
  对应 responseMimeType=application/json 与 responseJsonSchema，使用当时 Work 摘要 schema，
  无工具、LOW、输出上限 512、客户端无重试。请求 completed，指定 JSON 对象逐字段一致，
  input=71/output=42/cache 未知；未写业务数据库或发送 QQ。这是一次格式能力样本，
  不能证明真实摘要语义正确、任意 schema 受支持或缓存改善。

上线后的自然流量应分同链、新轮和压缩新链按相同口径观察。缓存改善与摘要质量不由
这一份上线前报告代替。
