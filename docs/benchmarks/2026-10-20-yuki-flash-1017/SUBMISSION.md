# 将结果提交到公开榜单

[评测报告](README.md) · [English summary](SUMMARY.en.md)

按 2026-10-21 查到的官方入口整理。本目录是结果报告；完整补丁、测试输出和原生运行记录仍保存在本地原始评测目录。

## SWE-bench Lite

官方采用两部分材料：提交者自己的公开结果仓库，以及 [`SWE-bench/experiments`](https://github.com/SWE-bench/experiments#-leaderboard-participation) 中指向它的条目。

| 材料 | 本次对应内容 |
| --- | --- |
| 预测结果 | 300 题的 `all_preds.jsonl` 和逐题预测补丁 |
| 评分材料 | 每题 `report.json`、`patch.diff` 与压缩测试输出 |
| 原始轨迹 | 每题运行时生成的模型与工具执行记录，按 instance ID 命名 |
| 入口条目 | `metadata.yaml`、系统说明、Logo、resolved IDs 与分类统计 |

官方当前的 `swebench` CLI 提供以下流程；`-s lite` 明确选择本次的 300 题集合：

```sh
swebench submit package <run_id> -s lite --trajs <trajectory-dir> -o ./submission
swebench submit publish ./submission --dry-run
swebench submit register ./submission -s lite --dry-run
```

先核对打包内容、填写 metadata 和系统说明，再公开结果仓库并向 experiments 提 PR。发布与注册都支持 `--dry-run`。CLI 是否提供这些子命令以所安装版本为准；本次原评测固定 `swebench==5.0.2`，提交工具可在独立环境准备。

现有 300 题结果不需要为了整理材料重新调用模型。额外的官方核验标记有独立流程：维护者按公开运行说明抽样复核。

参考：[官方提交页](https://www.swebench.com/submit.html)、[结果仓库与材料格式](https://github.com/SWE-bench/experiments#-leaderboard-participation)、[原始轨迹要求](https://github.com/SWE-bench/experiments#-reasoning-traces)。

## Terminal-Bench 2.0

Harbor Hub 可以上传已有 job，供浏览和分享，再按对应榜单的入口申请收录。当前 Harbor 的公开示例是：

```sh
harbor upload <job-directory>
```

默认上传为私有；公开提交时再选择相应共享方式。原评测固定 Harbor `0.1.44`，上传工具与旧 job 的兼容性应在独立环境核对。

TB 2.0 的官方审核要求通过题提供 ATIF 轨迹。Yuki 原生轨迹需要按真实记录转换，并与 task、工具执行及最终 reward 对应。六道超时后验证通过的题保留两种状态与原时间记录，沿同一材料提交。

旧版 2.0 当前接受新条目的入口、重复运行次数和审核流程，需要以该版本维护者的确认结果为准。上传到 Hub 与被官方榜单收录是两个步骤。若需补独立重复实验，应另存每次结果；本次单轮分数保留原样。

参考：[Harbor 结果分享](https://www.harborframework.com/news/job-result-sharing)、[TB 2.0 的 ATIF 与审核要求](https://www.tbench.ai/news/leaderboard-integrity-update)、[共同时间限制](https://www.tbench.ai/news/leaderboard-integrity-and-timeouts)。

## 当前材料进度

SWE Lite 已有预测、评分和原生记录，下一步是按提交格式整理公开结果仓库。TB 已有原 job 和运行记录，下一步是 ATIF 导出及旧版提交口径核对。本次 4.0.0 文档 PR 不执行榜单上传、外部提交或新增模型运行。
