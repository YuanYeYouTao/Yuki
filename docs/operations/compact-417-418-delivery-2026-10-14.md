# #417 / #418 统一 Compact 实施与验证（2026-10-14）

实施基线为 `2bc2b49705d5f1e768fccc295830583870fc3ff4`，数据库原 head 为 `0118`。
按[任务书](../architecture/Yuki-417-418统一Compact实施任务书.md)的推荐 U1–U5 执行。
本记录区分源码验证、提交、GitHub CI 与上线；PR 合并和部署尚未执行。

## 交付范围

| 部分 | 改动 | PR |
| --- | --- | --- |
| A | 当前 Main 的统一公共／私有 compact 入口、真实容量、手动命令与已付回执 | [#423](https://github.com/YuanYeYouTao/Yuki/pull/423) |
| B + D + E | 公共 projection 检查点，Rollup 读写／装配／配置／控制面清退，迁移 0119 | [#424](https://github.com/YuanYeYouTao/Yuki/pull/424) |
| C | Work 与 ordinary 独立摘要链、brief、staging／anchor 门槛退役 | [#425](https://github.com/YuanYeYouTao/Yuki/pull/425) |
| F | Main 完整能力合同，真实子／孙所有权、来源与插件冷恢复 | [#426](https://github.com/YuanYeYouTao/Yuki/pull/426) |
| G | 现行文档、退休工具／测试消费者、冷恢复修复与整体验证 | `codex/417-418-compact-validation` |

B、D、E 一起提交，因为删读接口与运行时装配、配置、迁移必须同时成立。PR 按依赖堆叠；中间版本用于审查，最终整体版本才是部署候选。

## CI 修复与本地验证

第一轮 #423 的完整 CI 有 11 个消费者仍调用已删除的旧 Runner compact API。
#424／#425／#426 先被退休消费者留下的 Ruff import 错误挡住，尚未执行后续测试。
本次按每个阶段的真实入口适配，保留原来源、预算、工具配对、未知派发和已付恢复验证；没有降低 workflow 检查。

最终分支 Ruff 通过，mypy 605 个源码文件通过；前端 20 项测试及生产构建通过。
第一次整体 pytest 为 1722 passed、63 skipped、1 failed。失败是 Linux 容器 Supervisor 的 `/proc` 扫描在 macOS 上不可用；原测试和生产代码保留。63 个 skip 包含尚未构建的固定 Monty worker／binding，不表示已验证 Code Mode 运行。

随后审查补齐 incomplete 响应在 `response`／`paired` 两个崩溃点的恢复，以及普通 E 与 Work P 的当前资料恢复；相关 55 项定向测试通过。修复后再跑完整 pytest：1727 passed、63 skipped，仅同一个 macOS `/proc` 失败。各 PR 的最新 GitHub Quality 结果在交付时补记。

另有 1 项实际 Main 入口回归通过：公共 compact 响应持久化后中断，沿原内部接纳来源恢复并发布；后续 Main 同时读取公共检查点、新公共纠正和当前 short_state，物理请求为 1 次 compact + 1 次正常 Main。没有从平台消息号重新建立业务所有权。

## 冷恢复与隐私

- 公共 compact 已付响应恢复时，保留未覆盖原文并接入新公共事件；分组重叠用同批单事件呈现，原冻结事件不重渲染。
- 当前激活使用新公共历史和本轮资料；原 Work 的私有检查点与真实未观察回执继续归原任务。已付旧请求中的运行资料只作原件证据。
- Work 的公共 compact 发布失败后，先采用已付检查点，再建立业务恢复输入，避免用未压缩旧历史触发第二次购买。
- 隐私清退擦除普通 compact observation 的正文、actor、读取范围与资料关联。只保留属于该内部执行的已派发、已失效事实，拒绝旧来源重发；0119 对旧库安装同一隐私 trigger。
- 子 Agent 的插件目录在执行依赖装配前初始化；SDK 历史装配不再接收已退休的摘要容量参数。

## 实际 DeepSeek Chat 调用

使用操作者提供的现有 `deepseek-flash` 连接、合成身份与隔离 SQLite。
最终公共 P、私有 E 都使用正式 TaskModelExecutor、Main 的 40 个固定工具声明和同一 8192 输出／思考设置；未执行返回的业务工具。P 通过实际 `/ai compact` 入口，E 通过实际 Main 请求准备与 Work journal。

| 调用 | HTTP | 输入 tokens | cache hit | cache miss | 输出 tokens | reasoning tokens | 结果 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 首次 P，1024 输出额度 | 200 | 10416 | 1536 | 8880 | 1024 | 1024 | 输出截断，没有可用检查点 |
| P，正常 Main 设置 | 200 | 10416 | 10112 | 304 | 801 | 459 | 公共检查点发布与命令回执成功 |
| E，同一 Main 设置 | 200 | 10938 | 10112 | 826 | 1051 | 449 | 私有检查点持久化，原 Work 模型预算累计一次 |

实际共 3 次 HTTP 派发，Provider 重试为 0。前置隔离环境装配失败均未派发 HTTP。
凭证只在进程内读取，没有写入仓库或报告。usage 是这三次受控调用的实测值，缓存命中不证明生产成本节省。

## 后续上线验收

尚未进行生产库副本的在途 Work 来源指纹盘点／迁移演练、实际 QQ 发送／Memory／automation 验收，以及部署和自然流量成本观察。Gemini 经 AGM、DeepSeek Responses 未在本次实际调用中验证。
这些项目按任务书 R01–R06 另记原回执和实际路由；不以本地测试或受控 API 请求替代。
