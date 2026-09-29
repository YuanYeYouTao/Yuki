# Gemini 3.8 Flash 显式缓存可行性

调查时间：2026-09-29。范围：Yuki 当前 Gemini GenerateContent 连接、Antigravity Manager（AGM）v4.8.4 的 Cloud Code 转发链；未修改生产配置、发送 QQ 或创建缓存。

## 结论

Google **Gemini Developer API** 的 `v1beta/cachedContents` 支持为官方模型 `gemini-3.8-flash` 创建显式缓存；创建时可包含 `contents`、`systemInstruction`、`tools` 和 `toolConfig`，缓存内容与模型不可变。后续 `generateContent` 使用缓存资源名及新增的动态内容。[官方缓存接口](https://ai.google.dev/api/caching)、[GenerateContent 缓存说明](https://ai.google.dev/gemini-api/docs/generate-content/caching)。这不能证明 AGM 的 Cloud Code `gemini-3.8-flash-tiered` 路径支持同一资源或计费方式；`-tiered` 是 AGM 映射到上游的模型名，不是上述官方示例中的模型 ID。

当前 Yuki 的 `GeminiProvider._build_payload` 每次发送 `systemInstruction`、固定工具声明和完整 `contents`，不创建或引用 `cachedContent`。AGM v4.8.4 的 `server.rs` 仅注册 Gemini 模型查询、生成和 `countTokens` 路由，没有 `cachedContents` 创建、查询、续期或删除路由；Gemini 原生 handler 只转发生成/计数。AGM `cache_manager.rs` 虽有显式缓存名的 Layer 3 结构，生产代码没有登记缓存名的调用；OpenAI mapper 的 `lookup_prefix` 注入分支因而不能形成可用缓存。Gemini 原生路径没有该注入分支。原生 wrapper 会复制入站 JSON，但单独把 `cachedContent` 塞进请求，既没有可引用的真实资源名，也没有 Cloud Code 上游兼容证据，不能作为修复。

自然请求前缀审计有两组同会话样本：固定系统说明、工具和生成设置的 hash 相同，旧代理记录的九次调用中五次缺少缓存量字段。缺失不等于零命中；已报告命中也不能证明整个重复前缀均命中。先用 Google 原始 usage 和账单对照，不能仅按聚合比例判断前缀缺陷。

## 成本与边界

官方 Developer API 显式缓存的默认 TTL 是一小时，创建缓存的输入、复用时的缓存输入，以及缓存存储时长分别影响费用。Gemini 3.8 Flash 标准付费价在 2026 年底前为输入 **$0.75/百万 Token**、缓存输入 **$0.075/百万 Token**、存储 **$0.50/百万 Token·小时**；这些是 Developer API 公价，**不得用来估算 AGM/Cloud Code 账号的实际账单**。[缓存说明](https://ai.google.dev/gemini-api/docs/generate-content/caching)、[官方定价](https://ai.google.dev/gemini-api/docs/pricing)。显式缓存并非免费保温：若固定前缀重复不足，创建与 TTL 存储费会抵消折扣。

缓存应绑定实际凭据/账号、官方模型 ID、完整稳定前缀与工具合同版本。缓存中的系统说明、工具和历史内容不能在复用请求中再次作为动态内容重复发送；续接的工具调用、思考签名和顺序必须保持原链语义。配置热切、工具合同变化、摘要改写、模型变化和缓存过期均应开启新缓存边界；缓存创建失败或资源失效应保留原始完整请求作为降级，不应重试外部副作用。缓存 ID、创建/过期时间、创建及复用的真实 Token 与费用应单独记账。

## 无 QQ 试点的前置条件

1. 使用**直接接入 Google Developer API** 的独立付费凭据与 `gemini-3.8-flash` 官方模型，或先由 AGM 在隔离环境证明 Cloud Code 的缓存创建、引用、过期及账单合同；不能把 Developer API 的缓存名跨账号或跨路径复用。
2. 从相同生产形态请求抽取无敏感内容的合成固定前缀，先用官方 `countTokens` 确认达到模型门槛，再创建短 TTL 缓存。将稳定系统说明、固定工具声明和不变历史置入缓存，动态尾段保留在生成请求中。
3. 用相同账户、模型和前缀执行创建、至少两次复用、过期/换模型负例；逐次比对 `usageMetadata` 的缓存字段存在性与数值、响应质量、工具声明及实际供应商账单。记录缓存创建和存储费，证明净费用下降后才考虑产品开关。
4. 先在隔离连接实现账号隔离、单次创建并发去重、过期与配置版本失效、失败回退及账本口径；完成协议和实际费用验收后再决定是否部署到当前主链。

本调查不将显式缓存计入清单 #10 完成项。现行隐式缓存按 Google 官方建议继续保持长且共同的请求前缀，并以原始请求和上游回执核对；不为命中率加入无关内容。[官方隐式缓存说明](https://ai.google.dev/gemini-api/docs/caching)。
