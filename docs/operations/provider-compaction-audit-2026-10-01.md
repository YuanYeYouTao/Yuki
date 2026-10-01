# 长任务重构的代理逐跳审计（2026-10-01）

本报告记录上线前自然流量与能力核查，不代表新 Harness 已部署或缓存改善已验收。自然请求审计只读；另有一次微小、无工具和 QQ 发言的结构输出能力请求，没有保温流量或大窗口探测。

## 当前有效路径

Yuki `model_invocations` 中同一真实群聊轮 `86d7713f46d4458f9cb056e0b912ed9c` 使用连接 `connection_2d4ba9aad301`、Gemini native、`gemini-3.8-flash`。Bot 使用宿主可达地址 `192.0.2.1:8045`；当前 `yuki-antigravity-tunnel.service` 为 active，其两个监听地址转发到 `antigravity-server` 的 loopback 8045。AGM 日志实际映射为 `gemini-3.8-flash-tiered`，最终发送方法为 Cloud Code `streamGenerateContent`。

重新读取 AGM 容器镜像配置 ID 为 `sha256:9a5fae056a0b923282860e2ca614ecf284e9a3b913adc4c0d04e2af3b430b523`，标签 `antigravity-manager:gemini-request-correlation-v4.8.4`，数据挂载仍为 `/opt/antigravity-manager/data`。独立现行补丁源 checkout `agm-request-correlation` 的 HEAD 是 `d316d27b026c68ed037acb2fa6bb79884f8a6643`。镜像没有源码 OCI label；这里结合现行发送点日志与源码核对，不冒充可复现镜像源码证明。

## 同一请求的严格关联

取内部源事件 **78943** 的有界索引 trace，未按 QQ 号或消息正文重建所有权。以完整 JSON 的键排序规范化 SHA-256 与代理入站请求比较；诊断中私有签名按现行 `execution_trace.payload` 的原值哈希/字节数引用同样处理后比较。签名本身未输出。

| Yuki trace / invocation | AGM UUID | input / output / cached | 最终发送次数 |
| --- | --- | --- | --- |
| 46179 / 50832 | a2c6b952-443d-4548-a9dc-617d1fe86025 | 35057 / 139 / 32158 | 1 |
| 46199 / 50834 | 27294c1e-9e13-4920-accf-68c2302ff67e | 35565 / 61 / 32155 | 1 |
| 46210 / 50835 | 1c5e5d8e-e2c9-4373-b167-02c6449bb681 | 35910 / 17 / 32151 | 1 |

对应完整规范化请求摘要分别为 `d6f2c3c44c64625c5841ef832120efc7970b389282502f21b70505cb5f470634`、`7c602100d11bab8f78f59fdcbcd8da7b07bcc61847259b5030110e6c81e1dcb2`、`222fcb210cb47ad5aa3560fedcaf27db4e6fc0607c465fa8409412f8f33d7345`。时间仅用于筛候选，三条均由完整请求匹配确定。

AGM 最终序列化点以 SHA-256(`agm.proxy-log-id.v1\0` + UUID 二进制 16 字节) 关联；三条摘要分别为 `466cde015d659338efe55ad09f1b27ed7e223abfbdcf279ebe2d61ff7a1db78d`、`1d1cdd93fc885238f0a2a1a13eadd63c0faf341743a2ccc4fb85f9141b30eb55`、`714a13ab58aa3ef2f46508bca84965dcf5832507016616cdac7c94479d6987a3`，每条均找到唯一发送点。该点位于最终 `serde_json::to_vec` 后、HTTP POST 前；它覆盖代码发送尝试，不是 Google 服务器抓包，也不覆盖 HTTP 库内部不可见重试。

三条 account 与 session 域分隔哈希一致，日志也显示 sticky account 复用。未发现这三次请求的代码级上游 fallback/账号切换。不能将这个小样本推论为全时段无重试或账号变更。

## 前缀、包装及计量

三次最终发送的外层均为 project/request/model/userAgent/requestId/requestType/enabledCreditTypes；内层均为 systemInstruction/tools/generationConfig/safetySettings/sessionId/contents。没有意外字段、旧顶层 thinkingConfig、session thinking ID 或固定 thinkingBudget，thinkingLevel 为 LOW。

contents 数依次 87、89、91；最终发送点的 system、tools、generationConfig、safetySettings、sessionId 进程加盐摘要相同，前 1/8/32/64 条 contents 摘要相同。Yuki 客户端静态字段与旧 contents 逐项摘要也保留前缀。代理日志的 `upstream_request_body` 是早期、可简化的 Forwarded 预览，其字段值哈希不等于最终发送点；不能据此把预览变化认定为实际 wire 改写。

Yuki `provider_response` 的 Gemini usageMetadata、Yuki 三条 invocation、AGM request_logs 的列和保存的规范化 response.usage 在表内数字完全一致，总数为 input+output，缓存字段均明确存在。Yuki 物理请求均为 1、unknown usage 为 0。此样本未出现 thoughtsTokenCount，不能据它验收思考 token 合计；现行补丁源码将 candidate+thoughts 计入 output，并单独保留 reasoning，历史定向证据见 [供应商切换记录](provider-cutover-worklist-2026-09-29.md)。

本轮三次已知输入加权缓存比例约 90.54%。它证明同链续接正常的一个样本，不能代表跨聊天轮、不同账号、压缩新链或所有最近流量。汇总统计及 unknown 口径见 [成本模拟](context-cost-simulation-2026-10-01.md)。本轮无需重新部署已修复 cache missing/zero 与 thinking 计量的代理补丁。

## 容量、countTokens 与显式缓存能力

- 现行源确实提供 Gemini `:countTokens` 和 `/countTokens`，转发到 Cloud Code `v1internal:countTokens`。服务器保留的原同请求实测证明其结果只覆盖该样本 contents：加入/去除 system 都是 18383，真实 generation input 为 20927；公共 API 的 generateContentRequest 包装被内部接口拒绝。不能用它为包含 system/tools 的完整请求计量，也不能给差值套固定修正公式。本轮沿实际完整请求保守估算与可选 Profile 上限，不新增失真的 token 计数器。
- `/v1beta/models` 的 inputTokenLimit=128000/outputTokenLimit=8192 是代理源里的统一常量；单模型 GET 仅返回名称。这不是 Google 实际模型容量证明。历史 session 累计超过 1048576 的恢复逻辑也不是单请求可用上限。没有通过大请求做边界探测；96k 聊天/128k Work 是可热更的政策预算，不是已认证的上游极限。
- 当前 Gemini native handler/server 没有 cachedContents 创建/查询/删除路由，也未在本样本携带 cachedContent；仓库中的 cache_manager 名称不能证明实际 Gemini native 路径采用显式缓存。当前仅依赖上游隐式缓存，不启用未支持的显式缓存或额外保温流量。
- 为避免新辅助摘要只靠提示词输出 JSON，沿当前真实连接与现有 Gemini serializer 做一次独立小型格式验证：`response_format=json_schema` 对应 responseMimeType=application/json 和 responseJsonSchema，使用 Work 摘要 schema（含 $defs 与本地有界字段），无工具、LOW、输出上限512、客户端无重试。请求 completed，返回与指定 JSON 对象逐字段一致，input71/output42/cache未知，没有写入业务数据库或发送 QQ。现行 AGM 非 image 路径保留这两个 generationConfig 字段，Cloud Code 在本次实际接受；它证明输出格式能力，不能证明真实摘要语义正确、任意 schema 都受支持或缓存改善。

上线后仍需在自然流量中分同链/新轮/压缩新链重复相同口径观察，验证新 continuation 前缀政策；缓存改善与摘要质量不由这一份上线前报告代替。
