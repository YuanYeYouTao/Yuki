# Yuki Benchmark

这里在本机运行 Yuki 自己的 Main/Work、Provider、固定工具合同、预算和执行回执。
Python 核心运行在评测机，终端操作进入每道题的真实容器；不启动 QQ、生产 Bot 或 Sandbox
Manager 服务，也不委托 Codex、Claude Code 或其他 coding agent 解题。

首批固定 `harbor==0.1.44` 与 `terminal-bench@2.0`，用于和 Diana 的历史实验做同题对照。
这是有意保留的历史基线：截至 2026-10-20，Terminal-Bench 官网已发布 4.0，不能把这里的
2.0 结果称作最新版成绩。SWE-bench 使用官方 `swebench==5.0.2` 评分，先用 Lite 的 dev
split 验证接入，再决定正式样本。两个 benchmark 都使用同一个 Yuki runner。

这是隔离的单任务场景，当前配置关闭 Code Mode、子代理、插件、自动化与独立 Web
工具，网络访问经任务容器终端。Main、Work 和真实工具实现仍复用生产代码；报告同时列出
有效设置与工具合同，不把这个场景的成绩扩大为完整 QQ 社交能力评测。

## 本机环境

终端 supervisor 使用 Linux 的 PTY、信号与 `/proc`。Windows 上请在本机 WSL2 Linux
内运行 Python 3.12 和 Linux Docker；Docker daemon 必须在该发行版可达。这里不需要服务器。
本次本机使用 WSL 的 Debian 发行版；在 PowerShell 运行 `wsl -d Debian` 后执行以下命令。
题目容器没有合适的 Python 时，只上传本机供 helper 使用的标准库 runtime，不替换题目
原有的系统解释器或重新安装其依赖。

```bash
cd /path/to/Yuki-QQbot
uv venv --python 3.12 "$HOME/.cache/yuki-benchmark/venv"
source "$HOME/.cache/yuki-benchmark/venv/bin/activate"
uv pip install --python "$VIRTUAL_ENV/bin/python" -e '.[dev]' -r benchmarks/requirements.txt
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
python --version
docker version
python -c 'from importlib.metadata import version; print(version("harbor"))'
python -m pytest tests/benchmark -q
```

以上安装、CLI 帮助、离线测试不需要模型密钥。Harbor 的数据集下载、oracle、镜像准备会
使用网络与磁盘，但不请求被测模型。下文 `harbor run` 的 Yuki agent 以及 SWE 的 `run`
会调用实际配置的付费模型；准备完成与真实跑分分别记录。

Harbor 0.1.44 的自定义适配器参数是
`--agent-import-path benchmarks.terminalbench:YukiAgent`，任务筛选是 `-t/--task-name`。
不要直接抄用新版文档的 `-a module:Class` 或 `-i`。基准依赖安装在同一个 Python 环境，
命令从仓库根目录执行，确保 `benchmarks` 和实际 Yuki 源码可导入。

## 本次本机已准备

2026-10-20 已下载 Terminal-Bench 2.0 全部 89 题元数据，固定仓库 revision
`69671fbaac6d67a7ef0dfec016cc38a64ef7a77c`；只为 `regex-chess` 与 SWE Lite dev 的
`marshmallow-code__marshmallow-1343` 两个选定题目准备并验证镜像。前者约 211 MB，后者
约 3.57 GB，均按本机 Docker 显示记录；全部 SWE 镜像尚未准备。

原始证据在 `$HOME/.cache/yuki-benchmark/runs/terminal-prepare/`，包括 `tasks.json`、
`dataset-spec.json`、`selection.json` 和 `prepare-summary.json`。TB 无模型 probe 的原
Supervisor echo 成功，官方 verifier 正常运行且按未解题判 reward 0。SWE 选题已保存在
`$HOME/.cache/yuki-benchmark/runs/swe-dev-smoke/manifest.json`，固定 HF revision
`b0dde1093fe417d83b7184254edf8199c1f0dff5`；无解 probe patch 经官方 grader 判 unresolved，
没有评测错误。这些是接入验证，不是 Yuki 解题成绩。

Yuki 原生 runner 还完成了本地 mock HTTP 与真实 Docker 的联调：两次本地请求、真实终端
命令和消息回执，该探针付费请求为零。随后真实校准中，SWE dev 首题获官方 resolved；
TB 的 8192 与 32768 输出额度分别耗尽，原尝试和评分均保留，不能把重跑挑选为单次成绩。
这些只代表单题校准，没有全集验收。
当时宿主 C 盘可用约 45 GiB；WSL 报告的是虚拟文件系统容量，不能据此承诺全量镜像空间。

## Terminal-Bench 2.0

该版本的官方全集是 89 题。先运行 oracle 检查本机容器、网络与评分脚本，再运行少量
Yuki 任务。oracle 是题目参考解的环境验证，不是 Yuki 的成绩。

```bash
harbor run -d terminal-bench@2.0 -a oracle -n 1 -k 1 -l 1 \
  --jobs-dir data/benchmarks/terminal-oracle
```

复制 [DeepSeek 配置示例](deepseek.example.toml) 到本机忽略目录，设置 `env_file` 为实际
本机密钥文件的 Linux 绝对路径；文件只读取 `api_key_env` 指定的键，不导入生产库、工作区
或其他部署配置。已有命名环境变量也可以提供密钥。示例不包含密钥：

本次机器当前的私有配置放在 `$HOME/.config/yuki-benchmark/deepseek-full-capacity.toml`，其密钥文件是同目录
`deepseek.env`；两者不属于仓库。已有环境直接激活 venv 并指定该配置，不必重新安装或覆盖：

```bash
source "$HOME/.cache/yuki-benchmark/venv/bin/activate"
export YUKI_BENCH_CONFIG="$HOME/.config/yuki-benchmark/deepseek-full-capacity.toml"
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
```

本次用户选择最新 Flash 1017，通过官方 `deepseek-flash` 标识调用。别名可能随上游变化，
运行时仍保存请求里的原始标识、实际响应和运行日期，不把别名永久等同于某个快照。

准备好独立配置后，设置配置路径。密钥从配置引用的环境变量读取，不放进 Harbor
`--ak`、模型名、终端命令或提交到 Git。Harbor 的 `--model` 必须与配置里的 `llm_model`
逐字一致；适配器只核对标签，不按它另选模型或协议。

本机第一题可直接使用已固定的原始题目缓存，避免重新下载数据集。以下命令会开始真实
付费推理；每种配置使用独立日志目录，保留此前的尝试：

```bash
source "$HOME/.cache/yuki-benchmark/venv/bin/activate"
export YUKI_BENCH_CONFIG="$HOME/.config/yuki-benchmark/deepseek-full-capacity.toml"
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
harbor run \
  -p "$HOME/.cache/yuki-benchmark/runs/terminal-prepare/tasks/XyJK3D4eSbPWgNt9Bw4RyU/regex-chess" \
  --agent-import-path benchmarks.terminalbench:YukiAgent \
  --model deepseek-flash -n 1 -k 1 \
  --jobs-dir "$HOME/.cache/yuki-benchmark/runs/terminal-yuki-full-capacity-smoke"
```

在其他机器准备新的小样本时，仍可按官方版本名运行：

```bash
harbor run -d terminal-bench@2.0 \
  --agent-import-path benchmarks.terminalbench:YukiAgent \
  --model deepseek-flash -n 1 -k 1 -l 3 \
  --jobs-dir data/benchmarks/terminal-smoke
```

`-l 3` 是接入小样本，不能称作 Terminal-Bench 2.0 全集分数。正式跑 89 题时去掉 `-l`，
保留官方每题环境和超时，不覆盖 CPU/内存、改测试或挂载含答案的数据集目录：

```bash
harbor run -d terminal-bench@2.0 \
  --agent-import-path benchmarks.terminalbench:YukiAgent \
  --model deepseek-flash -n 1 -k 1 \
  --jobs-dir data/benchmarks/terminal-full
```

一次全量是 89 次任务尝试；`-k 5` 是 445 次独立尝试，须另外记录重复次数和统计方法。
环境错误、超时、模型错误、主动失败、未知结果及未运行的题都要保留。不能去掉失败题后
把剩余成功率当成官方全集成绩，也不能挑多次里最好的一次冒充单次成功率。

Harbor 在 agent 结束后运行官方 verifier。Yuki 的 `completed`、终端退出码 0、模型最后
说“完成”都不是 pass；以 verifier 的 reward 和完整任务分母计分。agent 产生文件时应
落在题目要求的容器路径，不能只写在 host 的日志目录。
正常交回评分时保留题目需要的后台服务和产物，待 Harbor 评分结束后销毁官方任务容器；
取消或超时则停止本次原运行，保留已经产生的文件和执行证据。
Harbor 0.1.44 默认 `--delete` 会对本任务的 Compose 项目执行
`down --rmi all --volumes --remove-orphans`，因此也可能回收本题原始 source image。
本次 probe 后的 `regex-chess` 原镜像确实由该流程回收，已按记录的原 digest 恢复并补回
题目引用的 `20251031` tag，供首题运行使用；以后按官方流程结束时仍可能需要重新拉取。

## SWE-bench

官方集合及正式 test 分母分别为：

| 数据集 | 正式 test 任务数 | 用途 |
| --- | ---: | --- |
| `SWE-bench/SWE-bench_Lite` | 300 | 首批正式软件修复对照；dev 另有 23 题 |
| `SWE-bench/SWE-bench_Verified` | 500 | 人工核验的正式集合 |
| `SWE-bench/SWE-bench` | 2,294 | 原始完整集合 |

数量来源：[Lite 官方说明](https://www.swebench.com/lite.html)、
[Verified 数据集](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified)、
[SWE-bench 官网](https://www.swebench.com/)。dev 成绩与正式 test 成绩分开，不按此表的
test 分母计算 dev 成功率。

先用 Lite dev 的一题准备镜像。`prepare` 固定 Hugging Face revision，保存不含解决补丁与
评分答案的 manifest；`--pull` 才拉取选定题目声明的镜像，不默认创建几百个容器。

```bash
python -m benchmarks.swebench prepare \
  --dataset SWE-bench/SWE-bench_Lite --split dev \
  --instance-id marshmallow-code__marshmallow-1343 \
  --revision b0dde1093fe417d83b7184254edf8199c1f0dff5 \
  --output "$HOME/.cache/yuki-benchmark/runs/swe-full-capacity-dev-smoke" --pull
```

以下 `run` 会请求配置中的真实模型。它在原题容器里恢复题目的 base commit，交给同一个
Yuki runner，提取 `git diff` 写入 `predictions.jsonl`。已提交的 prediction 会保留；遇到
未知或中断的现场时需明确加 `--resume`，沿原容器、数据库、Work 和调用键继续，不重新
发放预算或提交已发生的终端命令。仅 prediction 写入中断时，只补收原 patch。

```bash
python -m benchmarks.swebench run \
  --output "$HOME/.cache/yuki-benchmark/runs/swe-full-capacity-dev-smoke" --config "$YUKI_BENCH_CONFIG"
python -m benchmarks.swebench eval \
  --output "$HOME/.cache/yuki-benchmark/runs/swe-full-capacity-dev-smoke" --run-id yuki-full-capacity-dev-smoke --workers 1
```

需要续接原未结束现场时：

```bash
python -m benchmarks.swebench run --resume \
  --output "$HOME/.cache/yuki-benchmark/runs/swe-full-capacity-dev-smoke" --config "$YUKI_BENCH_CONFIG"
```

`eval` 使用固定 revision 的原始评测数据和官方 Docker harness 验证 patch，不调用被测
模型。原始 grader 数据写在独立的 `evaluation/` 下，不能挂载给 agent；官方报告与测试
日志也在这里。空 patch、无法评分和模型执行失败保留原事实，不代填参考解。
本机安装的 5.0.2 实际输出汇总在 `evaluation/<model_name>.<run_id>.json`，逐题日志在
`evaluation/logs/run_evaluation/<run_id>/<model_name>/<instance_id>/`。
评分正常结束后只清理本次实验已定稿且有原归属标签的任务容器；未定稿的续接现场保留。
需要显式执行同样的清理时可运行 `python -m benchmarks.swebench cleanup --output <原输出目录>`。

扩大至 Lite 正式 test 全集时，用新的输出目录并明确切换 split；准备全量元数据不等于
立即调用 300 次模型：

```bash
python -m benchmarks.swebench prepare \
  --dataset SWE-bench/SWE-bench_Lite --split test \
  --output data/benchmarks/swe-lite-test
# 明确开始全量调用后再运行：
python -m benchmarks.swebench run \
  --output data/benchmarks/swe-lite-test --config "$YUKI_BENCH_CONFIG"
python -m benchmarks.swebench eval \
  --output data/benchmarks/swe-lite-test --run-id yuki-lite-test --workers 1
```

Verified 或完整集合使用同样流程替换 dataset，并使用新的输出目录。可用重复的
`--instance-id` 分批准备/运行样本，先从低并发观察资源和成本。最终报告列出实际选定、
已尝试、已生成 patch、已评分、resolved、失败和未运行数量；子集与缺题不能冒充全集。
官方 `% resolved` 以指定正式集合的完整分母计算，同时单列基础设施错误，不把它们
悄悄从分母移除。

## 预算、分段与证据

Benchmark 的 Main 不设置模型调用或工具调用次数上限，根 Work 仍完整累计实际调用；
模型单次输出采用 `/models` 声明的真实上限，不再沿用本地 8K/32K 额度。当前 Flash
返回最大输出 393216、联合窗口 1048576 tokens，私有 `full-capacity-model.json` 保存了
查询时间与原始声明。TOML 的 `llm_timeout_seconds = inf` 移除本地 HTTP 超时，
`context_compact_trigger_tokens` 采用模型窗口，实际容量不足时仍沿原 Main Compact 继续。
终端输出按原游标分页，完整输出和工具产物可继续读取，分页不终止任务。

工具调用键、累计请求、内部 ID 与原执行回执保持不变；遇到未知结果先查原任务，不
重新提交命令。Harbor 的官方每题时限、容器规格与 verifier 保持官方合同；SWE 默认
不附加解题超时，显式 `--timeout-seconds` 才设置。不同预算配置作为独立实验报告。

每题输出包含独立数据库、原模型请求及工具回执、Yuki 结果和 usage。Harbor 的
`agent/yuki/` 保存共用 runner 的输出；`AgentContext.metadata.yuki` 保留实际源码 commit、
原始模型标识、Work 状态和 usage coverage。Harbor 原有配置、agent/verifier 日志和 reward
分别保留；原生 Yuki 轨迹不冒充尚未实现的 ATIF。

usage 计入真实物理模型调用。缺失 token 或缓存字段保留 `null`；缺样时已知部分与缺样
请求数一起展示，Harbor 汇总 token 保留未知，不能把已知子集当成总量。当前不按公开单价
猜测实际美元账单；模型费用、容器/GPU费用和墙钟时间应分别计量。先用小样本实测，再
按任务数和重复次数估算全量，不用过时教程的固定金额承诺成本。

每个 task 使用独立状态，不能把其他题的答案、失败后的人工提示或上一次任务记忆带入。
允许按题目网络条件检索一般文档；不查任务专用答案、原 Issue/PR 的解决补丁或 verifier。
题目材料、patch、隐藏评分材料不能整体挂进 agent 可见的 host 目录。

模型请求和数据库可能含题目正文、模型原响应、终端内容及路径。结果目录按本机私有材料
保存；分享前检查内容，配置和密钥不进入请求日志或 Git。

## 官方参考

- [Terminal-Bench 2 数据集与历史运行说明](https://github.com/harbor-framework/terminal-bench-2)
- [Terminal-Bench 版本目录](https://www.tbench.ai/benchmarks)
- [Harbor 0.1.44 发行包](https://pypi.org/project/harbor/0.1.44/)
- [SWE-bench 官方评测指南](https://www.swebench.com/SWE-bench/guides/evaluation/)
- [SWE-bench 官方提交说明](https://www.swebench.com/SWE-bench/guides/experiments/)

本目录提供实现和本机验证入口；只有实际保存的运行产物才证明某题已执行。离线测试通过、
CLI 能启动、oracle 通过、Yuki 真实调用、官方评分、全集覆盖和公开排行榜提交分别报告。
本机准备与 probe 不请求付费模型；真实推理与官方评分保存到各自独立的运行目录。
