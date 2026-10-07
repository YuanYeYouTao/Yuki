# 分段恢复修复后的真实 API 对照

2026-10-05，测试分支 `codex/pi-codemode-experiment`，修复起点 `c932655c`。

本页保留当时的八项历史测量、失败和费用。后续已定位分段结果交接及测试装配原因，
当前修复与新测量见 [分段结果交接修复](pi-codemode-handoff-repair.md)，不改写下面的原结果。

**已确认的产品和测试缺陷分别修复，但最终恢复任务仍只有 1/8 完整完成。新版 Code
Mode 为 1/2，另外三组为 0/2；新版两份输入的文件均正确且没有重复写入，另一份没有
结束原 Work。此次结果不支持“恢复已稳定”或“新版普遍又快又好又省”的结论。**

## 修复归属

- 产品运行时：等待队列第 17 个 future 返回类型化 `code_limit_wait_queue`，由已有
  driver 配对回执并结算未派发 child；保留此前已执行的原回执，丢弃该 VM。没有提高
  上限或放宽权限。实际付费样本出现该错误后仍继续运行，没有变成整项 Work 暂停。
- 产品工具合同：task_control 说明明确所有直接 action（包括 get/list/update）独占工具
  批次；后端原检查保留，声明内容 hash 自然更新。模型仍可能违反说明，拒绝保留。
- 测试身份：夹具在接纳前绑定 canonical 合成人物；每段新控制器复制并重绑同一读取
  身份。回归确认合法便签跨两段出现在实际模型材料中，原 Work 和预算保留。最终八个
  真实样本没有保存便签，所以本轮没有付费便签可见性样本，不能把该回归说成真实模型
  已主动使用便签。
- 测试停止规则：原来的新路径计数会误截仍在推进的 VM。新增“同一原 operation 的
  snapshot_revision 跨激活增长”；新建不同父操作或静止检查点不算恢复进展。便签只
  比较语义 payload，不把 revision/call_id 变化当成新进展。原暂停/错误和连续十段无
  进展停止仍保留；不改变答案、一次性写入、Work 正式完成的验收条件。
- 报告：旧便签只有首次引用错误被拒，第二份合法便签保存成功但因测试缺身份不可见。
  原解释重复计算了同一拒绝。历史 JSON 不改，HTTP 错误历史附调用 ID 并标明不应跨
  请求累加。原始混合任务 5/6 对 3/6 的恢复部分受装配缺口影响。

## 最终测量

|对照组|完整完成|两次尝试峰值费用估算|两次尝试总耗时（秒）|HTTP|
|---|---|---|---|---|
|历史循环 / 直接调用|0/2|$0.064686|106.89|75|
|历史循环 / Code 可用|0/2|$0.102967|197.73|99|
|新版循环 / 直接调用|0/2|$0.064418|100.14|71|
|新版循环 / Code 可用|1/2|$0.054506|97.71|50|

耗时列包含失败停止时间，是试验消耗，不是任务完成速度。唯一成功样本为
`new/code/repeat=0`：14.15 秒、6 HTTP、42 次业务调用、9 次激活，费用估算 $0.005471。
12 份审计文件各写一次，链条/合计正确，原 Work 为 completed。

`new/code/repeat=1`：83.56 秒、44 HTTP、110 次业务调用、22 次激活，费用估算
$0.049035。文件全部正确、全部必需输入读过、报告回读过、零重复写入；停止时没有活跃
代码父操作，Work 为 queued，模型没有调用 complete，所以仍算失败。曾调用隔离范围外
工具，另一次代码遗漏 `import asyncio` 返回 NameError；这些都是配对回执，不是 Host
裸异常。后期反复读取起点、链表和目录，没有保存进度便签。现有机制不会自动从正确
文件推断整个目标完成；工作材料/模型跨段收尾仍不稳定，这部分尚未修复。

四个 direct 样本均只完成约 8% 内容，反复读取且没有保存便签。两个历史 Code 样本均
未完成；保留的历史主迭代源码没有 `_resume_compositions` 或 CodeCompositionYield
接线，曾留下检查点不再跨段增长的旧代码父操作。此组是“历史主迭代 + 共同新调用/
Code 内核”的实验组合，不能当作旧生产应用的完整能力测量，更不能把它的续跑失败归到
新版 Monty 驱动上。新旧共享内核的队列超限缺陷与历史主迭代缺少接线是两件事。

最终 295 HTTP，所有用量已返回，费用合计 $0.286578。按 [DeepSeek 官方公开峰值
费率](https://api-docs.deepseek.com/quick_start/pricing/)计算：每百万 token 缓存命中
$0.006、未命中 $0.30、输出 $1.20；实际账单未核验。相同输入但直接组均未完成，没有
可用于声称完成速度快多少倍的成功配对。

## 诊断轮与费用不隐藏

- `long-tasks-recovery-retest.json`：首次重测八项，1/8 完成，291 HTTP，$0.303000。
  运行时/身份修复已装配，但路径停止规则仍误截在推进的程序；不并入最终完成率。
- `long-tasks-recovery-retest-v2.json`：中间观察器把新父操作也计成进展，历史循环不断
  新建而不是续原程序，因此中止。4 项测量已返回，另一个 in_progress，303 HTTP。
  已知用量 $0.333164，未返回用量保守留额 $0.062388；不是完整四组对照，不汇总完成率。
- 三轮新增已知峰值用量 $0.922742，加中止轮未知留额 $0.062388，本轮暴露上界估算
  $0.985131。包含此前 P11/探索/原正式轮的累计上界 $1.939117，旧预算未归零；未知用量
  仍是未知，没有伪装成零费用。

## 验证与范围

运行时代码修复后，真实 worker 全量：3303 passed / 1 skipped，670.48 秒；跳过项需要
未提供的私有生产备份。ruff check、format（1138 文件）、mypy（722 源文件）通过。
之后仅改变测量观察器及其回归，产品运行时代码未再改变；77 项受影响测试通过，18.03
秒，静态检查再通过。全量次数与后续定向次数分别记录，没有声称全量包含后加的两项。

新回归在修复前失败：一段程序先同路径读 70 次后完成工作，在第 55 个调用被误截。
修复后相同断言通过，97 业务调用、2 个 HTTP、文件正确且原 Work 结束；没有删除或
放宽旧断言。另一个回归拒绝把新/静止父操作当成恢复进展。

最终付费执行器 `8 passed in 506.03s` 表示八次测量正常返回，独立任务验收仍是 1/8。
原生 worker SHA256 和全量/定向验证范围见 regression.json；报告包含 harness/runtime
hash、每个输入 hash、每次完整冻结声明 hash。四组输入与声明均一致。没有费用、累计
模型/工具上限；单段五次业务调用和每请求输出 32768 上限保持。只是同进程的新激活，
未模拟 OS 崩溃，未评估小时/天尺度或正常生产 32 次配额的失败概率。

本次本地开发、真实 API、测试分支提交/推送已授权；生产访问、真实发送、PR、合并、
发布与部署均未执行。付费工作区/SQLite 自动清理，worker/binding 留作继续验证。

最终命令（原文件已经存在，后续重跑必须改 output 文件名）：

```sh
PYTHONPATH=. YUKI_MONTY_BINARY="$PWD/.venv/bin/yuki-monty-worker" \
  uv run --frozen python scripts/benchmark_long_tasks.py \
  --credentials /Volumes/huawei/项目实战/deepseek.md \
  --authorize-paid --unlimited-cost --max-output-tokens 32768 --repeats 2 \
  --prior-report docs/architecture/pi-codemode-evidence/p11-deepseek-initial.json \
  --prior-report docs/architecture/pi-codemode-evidence/p11-deepseek-retest.json \
  --prior-report docs/architecture/pi-codemode-evidence/long-tasks-initial.json \
  --prior-report docs/architecture/pi-codemode-evidence/long-tasks-comparison.json \
  --prior-report docs/architecture/pi-codemode-evidence/long-tasks-unlimited.json \
  --prior-report docs/architecture/pi-codemode-evidence/long-tasks-recovery-retest.json \
  --prior-report docs/architecture/pi-codemode-evidence/long-tasks-recovery-retest-v2.json \
  --case old/direct/resumed_work/0 \
  --case old/direct/resumed_work/1 \
  --case old/code/resumed_work/0 \
  --case old/code/resumed_work/1 \
  --case new/direct/resumed_work/0 \
  --case new/direct/resumed_work/1 \
  --case new/code/resumed_work/0 \
  --case new/code/resumed_work/1 \
  --output docs/architecture/pi-codemode-evidence/long-tasks-recovery-final.json
```

证据：[最终逐次记录](pi-codemode-evidence/long-tasks-recovery-final.json)、[结构化汇总](pi-codemode-evidence/long-tasks-recovery-final-summary.json)、[验证范围](pi-codemode-evidence/long-tasks-recovery-regression.json)。
