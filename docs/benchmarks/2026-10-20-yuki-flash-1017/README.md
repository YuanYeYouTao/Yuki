# Yuki × DeepSeek Flash 1017：SWE-bench Lite 与 Terminal-Bench 实测

本次使用 Yuki 原生 Main / Work / 工具执行链路，在 SWE-bench Lite 的全部 300 道 test 题上解决 **216 题（72.00%）**，在 Terminal-Bench 2.0 的全部 89 题上通过 **66 题（74.16%）**。两个分母均为完整正式题集，未只挑选成功题或用量记录完整的题计算成绩。

实测源码固定为 [313b6b9bdf07d411751db13b0a35f8f634f0035b](https://github.com/YuanYeYouTao/Yuki/commit/313b6b9bdf07d411751db13b0a35f8f634f0035b)，早于 4.0.0。本报告作为 4.0.0 发布材料的历史实测依据，**不是对 4.0.0 tag 的重新评测**。批次于 2026-10-20 开始，最终报告快照为 2026-10-21 01:25:47（UTC+8）。

[English summary](SUMMARY.en.md) · [机器汇总](summary.json) · [来源与保留范围](provenance.json) · [如何提交榜单](SUBMISSION.md)

## 分数与计分口径

| 项目 | SWE-bench Lite | Terminal-Bench |
|---|---:|---:|
| 版本 / split | Lite / test | 2.0 |
| 正式题数 | 300 | 89 |
| 通过 / resolved | 216 | 66 |
| 未通过 | 84 | 22 个 reward0，1 个 rewardnull |
| 通过率 | **72.00%** | **74.16%** |
| 评分器 | swebench 5.0.2 | Harbor 0.1.44 |
| 每题正式模型尝试 | 1 | 1 |
| 用量完整题数 | 300 / 300 | 84 / 89 |

SWE-bench 的成绩使用官方 harness 的最终 resolved 字段；300 题均完成评分，官方 error_instances 为 0。这里的 0 表示评分流程的顶层错误计数，不代表所有测试都没有依赖或环境症状。

Terminal-Bench 的成绩保留原 Harbor reward：66 个 1、22 个 0、1 个 null，官方 mean 为 0.7415730337078652。17 题同时记录 AgentTimeoutError，其中 **6 题 reward1、10 题 reward0、1 题 rewardnull**。异常字段与评分字段均按原回执保留。

### 六题“超时但通过”

| 题目 | 官方 agent 时限（秒） | 记录的 agent 墙钟（秒） | 超出（秒） |
|---|---:|---:|---:|
| compile-compcert | 2400 | 2430.16 | 30.16 |
| largest-eigenval | 900 | 916.81 | 16.81 |
| mcmc-sampling-stan | 1800 | 1811.43 | 11.43 |
| rstan-to-pystan | 1800 | 1829.13 | 29.13 |
| torch-tensor-parallelism | 900 | 915.27 | 15.27 |
| tune-mjcf | 900 | 918.18 | 18.18 |

本次 timeout_multiplier 为 1.0，未针对 Yuki 修改官方 agent / verifier 时限。Harbor 0.1.44 捕获 agent 超时后仍运行 verifier，因此可能同时产生超时异常和通过的 reward；以上六题已经包含在原始 66/89 中。

记录的 agent 墙钟包含取消任务、回执收尾、资源关闭及用量导出。额外 11.43–30.16 秒并非全部可确认为纯清理，后台计算与产物写入的截止时间也未逐题还原。本报告按六题原始 reward1 计通过，并保留相应计时，详见 [timeout-passes.json](timeout-passes.json)。

## 执行方法与固定配置

此次评测沿用 Yuki 的原生 Agent，而非调用另一套编码 Agent 代答。薄适配层负责输入任务、连接官方容器、收集补丁或终端产物；Main / Work 的执行、工具调用、回执和用量来自同一套 Yuki 运行时。

SWE 任务使用指定 base commit 的仓库与题面，最终补丁交给官方评分器。Terminal-Bench 使用 Harbor 的官方任务环境及 verifier。任务容器允许 bridge 网络访问；Agent 的独立 web 工具关闭。评分数据与金标准答案未作为 Agent 输入。

| 配置 | 本次取值 |
|---|---|
| 模型 | DeepSeek Flash 1017，API 别名 deepseek-flash |
| 推理 | thinking 开启，reasoning_effort=high |
| 请求配置 | temperature=0.7，context_window_tokens=1048576，max_retries=2 |
| 单请求输出上限 | 393216 tokens |
| 额外总模型 / 工具次数上限 | 未设置 |
| HTTP 总等待时限 | 未设置有限值 |
| Compact 触发配置 | 1048576 tokens |
| Code Mode / 子 Agent | 关闭 |
| 主机与容器 | Intel Core i9-13900H、32 GB 内存，Windows + WSL2 Linux + Docker，未使用 GPU |
| 任务并发 | SWE 由 2 调至 4；TB 由 1 调至 2 |
| TB agent / verifier 时限 | 保留官方逐题配置，倍率 1.0 |

上述“未设置”指 benchmark 额外的总调用预算和 HTTP 等待限制。模型仍有单请求输出上限；任务中的命令也可能由 Agent 自行指定超时。命令超时与官方整题 agent 超时是两件事。容器冷启动准备没有额外有限超时，未据此放宽官方解题或验证时限。

数据固定信息：

- SWE-bench Lite：SWE-bench/SWE-bench_Lite，test split，revision b0dde1093fe417d83b7184254edf8199c1f0dff5，评分 run ID yuki-lite-test-full-capacity-20261020。
- Terminal-Bench：terminal-bench@2.0，89 个 task 固定至仓库 revision 69671fbaac6d67a7ef0dfec016cc38a64ef7a77c，job terminal-bench-2.0-20261020。
- 评分器固定为 [swebench 5.0.2](https://pypi.org/project/swebench/5.0.2/) 与 [Harbor 0.1.44](https://pypi.org/project/harbor/0.1.44/)。

正式统计排除了校准、无模型环境探针和更早的基础设施失败尝试；未把不同预算的试验混为一组。任务因基础设施中断而恢复时保留已封存结果；未因为官方正常判错而再次生成答案。这里只报告一个正式尝试/题，没有重复 seed 的稳定性估计，也没有同模型下更换 Agent 的对照实验。

## Tokens、缓存与覆盖范围

tokens 为各次 API 调用的累计 usage，包括随对话重复提交的上下文，不是去重后的文本量。缓存命中率统一按同一批已知记录的 sum(cached_input_tokens) / sum(input_tokens) 计算，不取每题百分比的算术平均。

| 口径 | 输入 tokens | 缓存输入 tokens | 非缓存输入（派生） | 输出 tokens | 总 tokens | 缓存命中率 |
|---|---:|---:|---:|---:|---:|---:|
| SWE 全部 300 题，完整 | 463,412,887 | 451,694,208 | 11,718,679 | 5,537,393 | 468,950,280 | **97.47%** |
| TB 用量完整的 84 题 | 183,840,675 | 180,637,952 | 3,202,723 | 2,241,022 | 186,081,697 | **98.26%** |
| TB 全 89 题的已知字段子集 | 207,694,348 | 204,195,584 | 3,498,764 | 2,629,959 | 210,324,307 | **98.32%（已知子集）** |

SWE 300 题共记录 8,290 次物理请求，所有 8,290 行的相关 tokens / cache 字段完整，并与 Work 请求计数匹配。平均每题累计总 tokens 为 1,563,167.6，中位数为 905,123.5；平均 27.63 次物理请求。

TB 共记录 3,094 行 invocation，其中 3,092 行有 tokens / cache 值，已记录物理请求合计 3,092。84 题的完整口径对应 2,796 次物理请求，平均每题累计总 tokens 为 2,215,258.3，中位数为 921,331。**98.32% 不能称为完整覆盖全部 89 题的缓存命中率**。

五题的覆盖问题如下：

| 题目 | Work 逻辑请求数 | invocation 行数 | tokens 已知 / 行数 | 说明 |
|---|---:|---:|---:|---|
| adaptive-rejection-sampler | 44 | 43 | 43 / 43 | 逻辑计数与记录相差 1 |
| largest-eigenval | 73 | 72 | 72 / 72 | 逻辑计数与记录相差 1 |
| torch-tensor-parallelism | 62 | 61 | 61 / 61 | 逻辑计数与记录相差 1 |
| sam-cell-seg | 66 | 67 | 66 / 67 | 另有 1 行 physical0 的本地 WorkspaceError |
| video-processing | 54 | 55 | 54 / 55 | 另有 1 行 physical0 的本地 WorkspaceError |

前三题的计数差尚未对应到可确认的 Provider 请求；后两题另行记录本地失败，physical0。五题保留 `usage_complete=false`，单题完整缓存率为 null，已记录的 token 字段另列。诊断轨迹丢弃与 usage 覆盖分别记录。

## 未通过题的证据分析

这些分类描述本次观察到的主要失败证据，不能把每个测试症状都当成已经隔离验证的代码级根因，也不据此修正成绩。

### SWE-bench Lite：84 题未解决

| 互斥主分类 | 题数 |
|---|---:|
| FAIL_TO_PASS 未全过，未记录 PASS_TO_PASS 失败 | 68 |
| FAIL_TO_PASS 与 PASS_TO_PASS 均记录失败 | 7 |
| FAIL_TO_PASS 全过，但 PASS_TO_PASS 未全过 | 8 |
| 测试输出中断，没有可解析结果 | 1 |
| 合计 | 84 |

官方的 failure 列表可能包含断言失败、ERROR、SKIPPED 或指定测试未出现在可解析输出中。不能把后两类直接说成“代码回归”。例如：

- django__django-13321：局部原测试通过，但官方目标行为仍未覆盖，说明自测成功不等于完整解决问题。
- matplotlib__matplotlib-23913：目标 F2P 全过，P2P 中未出现预期 warning；没有干净基线对照，不能单凭该症状证明补丁导致回归。
- pallets__flask-4992：官方测试补丁无法应用，指定测试没有出现；保留原 unresolved，不把“缺失测试”改写成已观察到的断言错误。
- django__django-11905：结果打印过程报错，没有形成可解析 tests_status；不能把默认的 patch_successfully_applied=false 单独当成补丁应用失败证据。

更多代表及来源行号见 [SWE 失败分析](failures.swe-bench-lite.json)。

### Terminal-Bench 2.0：23 题未通过

| 互斥主分类 | 题数 |
|---|---:|
| 功能或产物合同不匹配 | 8 |
| 官方时限内关键产物或运行未完成 | 5 |
| verifier 启动或依赖环境阻断 | 8 |
| native WorkspaceError 终止 | 1 |
| 没有完成的评分证据 | 1 |
| 合计 | 23 |

8 个验证环境阻断中，4 题有较强的执行链证据：Agent 给 apt 安装设置了 180/300 秒命令超时，命令被终止，之后 verifier 报 dpkg interrupted，curl / uvx 缺失。这暴露了安装失败后的环境恢复问题，不能简单归为外部环境，也不能据此断定核心答案错误。其余为两题 Debian 包 404、一题 apt 连接失败、一题 uvx Bus error。

功能与产物问题的代表包括精确输出中混入解释、部署返回 HTTP404、情感分类输出错误和棋局状态转换不一致；未完成的代表包括缺失所需源码、训练模型或最终输出文件。sam-cell-seg 同时有 native WorkspaceError 和产物 CLI 不接受验证参数的证据，两者的因果关系未被证明。filter-js-from-html 没有完成的 reward，原 null 保留。

[Terminal-Bench 2.1 官方说明](https://www.tbench.ai/news/terminal-bench-2-1)后来修订了 2.0 中的 28 题，其中 13 题与本次 23 个未通过题重合。这仅是版本背景，不能当成本次根因或假定这些题在新版本会通过。完整逐题症状与置信度见 [TB 失败分析](failures.terminal-bench.json)。

## 如何理解这次结果

这次实测说明，Yuki 的原生执行架构能够承载真实仓库修复与多步骤终端任务。失败证据同时显示了交付合同检查、安装失败恢复以及官方时限内收敛仍有不足。结果描述的是“Yuki 固定源码 + 指定 Flash 配置 + 指定题集及评分器”的组合；模型与 Agent 各自的贡献，需要同模型对照实验进一步区分。

## 数据文件

| 文件 | 内容 |
|---|---|
| [summary.json](summary.json) | 分数、配置、累计用量、覆盖范围、版本 |
| [SWE 逐题 JSON](swe-bench-lite.tasks.json) / [CSV](swe-bench-lite.tasks.csv) | 正式 300 题逐题 outcome、用量、覆盖与耗时 |
| [TB 逐题 JSON](terminal-bench.tasks.json) / [CSV](terminal-bench.tasks.csv) | 正式 89 题逐题 reward、异常、native 状态与用量 |
| [SWE 官方回执摘录](evidence/swe-official.json) / [TB 官方回执摘录](evidence/terminal-official.json) | 小型评分字段摘录 |
| [SWE 失败分析](failures.swe-bench-lite.json) / [TB 失败分析](failures.terminal-bench.json) | 互斥分类、代表症状及来源行号 |
| [timeout-passes.json](timeout-passes.json) | 六题超时且 reward1 的原始计时字段 |
| [provenance.json](provenance.json) | 固定源码、数据版本、来源定义与保留范围 |

逐题 JSON 保留 null；CSV 中相应字段为空，不能当成 0。local-source/ 是维护者保留的原始 benchmark 目录的相对来源标识，不含主机绝对路径。公开包只包含汇总与必要字段，不携带 API key、用户身份、题面、金标准答案、补丁正文、完整轨迹、测试源码或数据库。
