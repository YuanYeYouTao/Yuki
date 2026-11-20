# #668 控制回执与 Work control 输入：失败情况（先于代码写出）

基线：`origin/main` 9d5242ea。控制（steer / respond / cancel / detach）带 `work_id` 时，
回执由 `ExternalAgentRepository.response()` 在一个 writer 事务里写入 `result_json`；
`control_settled()` 之后先开 reader 准备，再在**另一个** writer 事务里写
`external-agent:control:{id}` 输入。调用点：

- `service._write`：来源或连接在发送前已变，登记拒绝回执（无设备重发）。
- `service._settle`：设备回执（`receive_events`，ACK 在返回后；断开则设备重发）；
  也被 `_read_original` 的日志读回调用（无重发）。
- `service._release`：设备离线时显式 detach（无设备参与，无重发）。

## 交错时序

| # | 时序 | 基线结果 |
|---|------|----------|
| T1 | 回执事务提交 → 读者读回执 → 读者读 control 输入 → control 输入事务提交 | 读者看到“回执已记录、Work 无 control 输入”；测试 `scalar_one()` 报 `NoResultFound`（#668 原始现象，`no_record` 参数）。 |
| T2 | 回执事务提交 → 进程结束 / 该任务被取消 / 第二个 writer 抛错（`_release`、`_write` 拒绝、`_read_original` 路径） | 回执是终态，`response()` 读侧直接返回；`_get` 只在 `pending` 时读回设备；`control_settled` 再无调用者。Work 永远收不到 control 输入。 |
| T3 | 同 T2，但路径为设备回执（`receive_events`） | 未 ACK，设备重连后重发回执 → `_settle` 再跑 `control_settled`。可恢复，但仍有 T1 窗口。 |
| T4 | reader 准备 wait 绑定 → 他人改 wait → writer `external_action_current` 不成立 | `continue` 重读重试，不永久丢失；修复后在 writer 内直接读，不再有准备/核验错位。 |

## 修复后必须仍成立

| # | 要求 |
|---|------|
| R1 | 任一提交点上，带 `work_id` 的控制回执与它给 Work 的 control 输入同时存在或同时不存在（委派执行者的 Work 例外，见 R4）。 |
| R2 | 同一回执重复到达（设备重发、读回）不再写第二条输入，也不改已提交回执（读侧短路 + `source_key` 唯一）。 |
| R3 | Work 正等这次控制针对的 run（`SandboxTaskRun.status == waiting`）时，同一事务里 `signal_external_action` 唤醒它。 |
| R4 | 委派执行者的 Work：成功的 `respond` 调 `delegations.resolved`；失败的控制只告诉父 Work，release 自己的 deny（`release_reference`）不告诉父 Work；不写 control 输入。 |
| R5 | Work 已终态、代际不符或发起人不存在时，不写输入（`_input` 原有判断不变）。 |
| R6 | writer 事务内只做按主键 / 索引的点查，不等待设备、模型、网络，不扫描历史。 |
| R7 | 不加 sleep、重试或补写兜底；删除 `control_settled` 的 reader 准备与 `continue` 循环。 |

## 复现方法（同一复现修复前后都跑）

`tests/integration/test_external_agent_bridge.py::test_detach_receipt_commits_with_the_works_control_input`
包装 `database.immediate_session`：首个提交后已能读到该 detach 回执的 writer，在提交后暂停其调用协程
（即“两次写入之间”）。detach 在独立任务里执行；测试等到回执可读，立即按原测试查询读 control 输入，
并把读到的状态写进产物。`offline` 参数随后取消被暂停的任务，模拟进程在此结束，再 drain 后复查。
产物目录：`YUKI_DETACH_ARTIFACT_DIR`（默认 pytest `tmp_path`）。

## 对应测试

- R1：`test_external_agent_bridge.py::test_detach_receipt_commits_with_the_works_control_input[offline|no_record]`。
- R2、R3、R4：`tests/integration/test_external_agent_control_settlement.py`（先在基线通过；分别删掉
  run 唤醒、改成对已结束 run 也唤醒、不告诉父 Work、成功控制也告诉父 Work 四种改动，各自只让对应测试失败）。
- R5：`_input` 原判断未改；R6、R7：见修复提交的 diff。

## 结果（各参数串行 20 次）

`summary.json` 汇总；`baseline/`、`fixed/` 各存第 1 次运行的完整产物。

| 源码 | 参数 | 两次写入之间 `scalar_one()` | 之后（settled + get） |
|------|------|------------------------------|------------------------|
| 9d5242ea（基线） | offline | 20/20 `NoResultFound` | 20/20 仍 0 条输入 |
| 9d5242ea（基线） | no_record | 20/20 `NoResultFound` | 20/20 1 条输入 |
| 39659cd9（修复） | offline | 20/20 一行 | 20/20 1 条输入 |
| 39659cd9（修复） | no_record | 20/20 一行 | 20/20 1 条输入 |

复现：

```bash
git checkout 9d5242ea -- src   # 基线源码（测试来自 a1d0f4a7 或之后）
for i in $(seq 1 20); do
  YUKI_DETACH_ARTIFACT_DIR=/tmp/yuki-668/run-$i uv run pytest -q -p no:randomly \
    "tests/integration/test_external_agent_bridge.py::test_detach_receipt_commits_with_the_works_control_input"
done
git checkout HEAD -- src       # 回到修复源码再跑同一循环
```
