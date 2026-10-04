# AGM 虚构续接消息修复

状态：源码补丁与独立协议探测已完成；Rust 验证、合并、生产部署分别记录，不以探测代替 QQ 验收。

## 查明的问题

生产 AGM 4.9.0 的末尾防御会为所有 `model` 尾轮追加 `user: "ok go on"`。
一条同记录的实际诊断样本中，Yuki 原请求尾部是纯 `user/functionResponse`；AGM 将它
归一化为 `model/functionResponse`，再追加假用户消息。Yuki 当前代码没有生成此短句。
诊断副本能够证明该阶段的注入，不能独立证明最后的物理报文或具体 QQ 发言因果。

该防御的上游[引入说明](https://github.com/lbjlaq/Antigravity-Manager/commit/026d409715dd8ce4f3fedfd18a3e2b4376f0298a)
是兼容 `Requests ending with a model turn are not supported`，不是已经查证的防封号机制。
2026-10-04 核对的官方最新版 4.9.1 仍有此逻辑。

非流式 Gemini 请求也会被 AGM 转为内部流式并经过 auto-heal。原实现遇到仅思考的空输出时
追加“继续 / Continue.”，最多再请求一次；第二次为空或失败时伪造 `task ready` 成功正文。
最近一小时的有界日志捕获过一次触发、一次续接 200 和一次再次空输出的伪造正文。
该计数没有关联 Yuki 执行 ID，不据此归因某条 QQ 消息。

## 最小修复

[补丁](../../deploy/antigravity/v4.9.0-protocol-placeholder.patch)基于官方
`6e8b982aee7e3d3d53501825431142eea9a3f9ab`，只修改 AGM 两个 Rust 文件：

- 纯工具回执尾轮不追加任何用户轮；回执及随行媒体、角色、顺序和签名原样保留。
- 普通模型正文或未完成工具调用的尾轮保留既有兼容处理，使用
  `[Protocol placeholder: no additional user input.]`，明确不是新的用户发言。
- 真实非空 `·`、`(no content)` 保留，不再当成空内容覆盖；空结构仍做必要补齐。
- 自愈使用同一非指令占位，删除语言猜测；失败、读流中断或再次空输出返回流错误，
  不伪造成功正文。空终止帧在错误前不发布，防止下游提前认定成功。

保留既有一次自愈，不新增恢复循环、持久状态、业务路由或 QQ 文本过滤器。
不修改 Yuki 的显式发送、Work 收口、预算、回执或已提交检查点。

## 独立真实协议探测

[脱敏记录](evidence/agm-tool-tail-canary-20261004.json)包含七次纯合成 CloudCode Daily 请求，
使用实际映射模型 `gemini-3.8-flash-low`，不执行返回的工具或发送 QQ：

| 形态 | 结果 |
| --- | --- |
| 普通用户请求、真实签名 dummy 工具调用 | 各 200 |
| 原工具调用与纯 `user/functionResponse` | 200，结束，无新增工具调用 |
| 原工具调用与纯 `model/functionResponse`，不补用户轮 | 200，结束，无新增工具调用 |
| 工具回执后补明确协议占位 | 200，无新增工具调用 |
| 普通 `model` 正文尾轮，不补用户轮 | 400，明确末尾 model 不被支持 |
| 同正文尾轮补明确协议占位 | 200，返回 `NO_NEW_INPUT` |

此前裸别名 `gemini-3.8-flash` 的独立对照返回 404，立即停止；没有覆盖失败证据或盲目重试。
本轮合计八次物理请求，零工具执行、零 QQ 发送、零凭据刷新。七次主探测的 cache 字段
均缺失，不能记为零，也不能据此宣称缓存命中率改善。
这些请求使用 urllib 而非 AGM 的 rquest；媒体与多回执组合尚仅有离线覆盖，不外推所有模型。

## 构建与上线边界

[构建文件](../../deploy/antigravity/Dockerfile.protocol-placeholder)固定编译环境、源码基线和
当前官方 4.9.0 运行镜像 digest；仅覆盖后端二进制，复用原前端与运行依赖。
构建执行 Rust 格式检查、Clippy、两个相关模块测试和锁定依赖的 release 编译。
补丁 hash 写入镜像标签；构建与部署记录同时核对二进制、镜像和实际 Compose 文件。

复现时先取得官方固定基线，在未修改的工作树上 `git apply --check` 再应用补丁。
从 Yuki 仓库根目录构建，`PATCH_SHA256` 使用补丁文件的实际 SHA256：

```powershell
$agmPatchSha = (Get-FileHash deploy/antigravity/v4.9.0-protocol-placeholder.patch).Hash.ToLowerInvariant()
docker build --build-arg USE_MIRROR=false --build-arg "PATCH_SHA256=$agmPatchSha" `
  -f deploy/antigravity/Dockerfile.protocol-placeholder `
  -t antigravity-manager:protocol-placeholder-v4.9.0 .cache/agm-v4.9.0
```

模型探测是本次手工验收，不作为常驻测试或构建步骤。

上线只替换 AGM 镜像，保留账号、当前数据、路由、API Key、代理与 Yuki 配置。
停写后保存完整私有数据副本并验证 SQLite/WAL；回退使用原镜像与当前数据，绝不恢复旧数据库
或重放真实会话。当前服务没有可证明的请求排空接口，不能把重启描述成无中断上线。
业务停止、重复发送和自然 QQ 效果仍以真实执行回执与后续观察验收。

补丁和构建文件派生自 [Antigravity-Manager](https://github.com/lbjlaq/Antigravity-Manager)，
遵循其 [CC-BY-NC-SA-4.0 许可](https://github.com/lbjlaq/Antigravity-Manager/blob/6e8b982aee7e3d3d53501825431142eea9a3f9ab/LICENSE)。
这是本部署的局部补丁，不是上游已合并或官方已发布的修复。
