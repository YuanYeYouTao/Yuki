# #675 先红后绿证据

同一份最终测试文件 `tests/integration/test_watchdog_failed_work_e2e.py`（blob 与 SHA-256 见 `summary.json`）分别在两处运行：

| 运行 | 源码 | 结果 |
|---|---|---|
| `baseline-c528f322/` | origin/main `c528f322` 的独立临时 worktree（源码未改，只复制进这份测试文件） | 7 failed：每个场景都停在“看门狗没有开 `work_failed:*` 问题”的那条断言（行号见 `summary.json`） |
| `fixed/` | 本分支源码（`application/watchdog.py`、`runtime/work_repository.py`、`runtime/work_supervisor.py` 的 blob 见 `summary.json`） | 7 passed |

`fixed/watchdog-failed-work-e2e.json` 是 E2E 产物：所有内部 ID 都换成了稳定标签，同一提交连续两次运行逐字节相同（`two_runs_byte_identical`）。

复现：

```bash
uv run pytest tests/integration/test_watchdog_failed_work_e2e.py -q -m "not native_worker"
# 产物：test-results/watchdog-failed-work-e2e.json，results_sha256 应与 summary.json 一致
```

限制：这是在本机用最终测试文件对基线源码的回放，不是 CI 记录。S2（模型主动结束不报）在基线上失败于末尾的对照断言（基线根本没有这项检查，对照组的运行时失败也就没有被报告），并不是因为基线误报了模型主动结束。基线不会误报，因为它不报任何 failed Work。
