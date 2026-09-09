# 生成模型最低思考合同

本合同按用户最新决定覆盖原“后台 Flash 关闭思考”的配置。版本 3.8.1、schema 0051 不变。

## 执行下限

所有 `ModelTask` 共用执行层：主 Agent、插件/自动化 Agent、自动化文字生成、Rollup、
提取、主体关系分类、自省、Dream、attribution、关系评估、表情替换和其他结构化任务。
请求最终发给 Provider 前固定 `thinking_enabled=true`，effort 至少为 `low`。

- 省略、`none`、`minimal` 提升为 low；旧关闭开关不再有效。
- Profile 与当前请求若有更高档位，取两者较高值；不能用请求 low 降低 Profile high。
- 结构化任务不再强行关闭思考；普通执行与注入 Provider 的兼容执行入口同样执行下限。
- 仅调整请求参数，不把 reasoning 文本转为正文、记忆或日志，不改变 Main Agent 上下文结构。
- Profile 必须明确声明 `reasoning` 能力。缺失声明时配置校验失败，不自动伪造能力，
  不换模型，也不在 Provider 拒绝参数后自动退回无思考。
- 能力声明并非上游支持的证明；实际兼容性仍需受控请求验证。

通用枚举保留以读取历史配置和维护原始协议合同，但应用层不再发出 none/minimal。
直接 Provider 序列化测试仍覆盖原始协议；它不代表业务可以绕过统一执行层。

## 视觉及其他模型

视觉、表情识别和表情选择的所有模式（包括 general/OCR）开启 Qwen 原生思考。
其接口为 `enable_thinking` 与 `thinking_budget`，默认预算保持 6144；没有擅自添加
`reasoning_effort=low`，也不宣称两种 Provider 参数精确等价。删除先无思考、低置信度
再思考的双阶段调用；保留原有有界传输错误重试，不新增评审调用。
旧 `low_confidence_retry_threshold` 仅作配置兼容，不再触发这一已删除的二次调用。

Embedding、语音合成及非生成模型没有该 effort 合同，不发送不存在的思考参数。

## 输出预算与现有配置

思考和最终结果可能共享输出预算。自省、分类及新建 Flash Profile 默认上限调整为
4096；原硬编码 256 的 attribution 和 600 的审计也调整到 4096；Rollup 的 token 上限
至少 4096，较大的字符预算仍保留。Dream 保持 4096。以上是上限，不是强制生成长度。
输出 JSON/正文长度、事实数量、重试次数、工具轮数及验证请求总额不因此增加。

用户已有显式输出预算仍保留，需要上线前逐项核对，不能把新默认值当作线上有效值。
特别是旧自省 2400、分类 1200 与 Flash 2048，不能未经验证直接宣称足够。

示例和安装向导已产生 enabled/low 的 Profile。已有 `config/model_profiles.toml`
若缺少 `reasoning` 声明必须定向更新并保留备份；不能借此重新生成并覆盖其他路由或密钥。
本轮代码不会自动改写用户 `.env` 或未跟踪配置文件，生产调整仍需随验收通过后的部署执行。
旧的 hot 思考开关保留为兼容字段，说明明确其不能关闭思考，运行时 snapshot 返回开启。

## 验收状态

参数/协议、视觉模式与更高档位保留使用离线测试，不消耗真实请求预算。
本轮完整 pytest 800 项通过，Ruff format/check、mypy 通过；Memory quality 19/19、
38/38 门禁及基线比较通过，没有重设基线。发布检查的生产审计和真实 benchmark 仍未通过
本轮完整验收，不能把确定性测试当成上线许可。
此前低思考实验仅证明部分内容更聚焦，不能替代自省语义、Dream、归因或相关性验收。
当前实际请求检查点仍为 83/96；未部署。整体停止点见
[真实验收进度](memory-evidence-acceptance-progress.md)。
