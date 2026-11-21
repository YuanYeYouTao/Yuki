# #673 超管放弃范围外 failed Work：先红后绿证据

同一份最终测试文件 `tests/integration/test_superuser_abandon_failed_work_e2e.py`
（`summary.json` 的 `test_file_blob` / `test_file_sha256`）分别在三份 `src/yuki/runtime/work_control.py` 上运行，
其余源码都是本提交的版本：

| 目录 | `work_control.py` 来源 | 结果 |
|---|---|---|
| `baseline-db7b5c26/` | 基线 `db7b5c26`（#673 之前） | 6 failed：每个场景都在第一次跨范围放弃处得到 `work_not_found_or_not_authorized` |
| `previous-44d928a9/` | PR 第一版实现 `44d928a9` | 4 passed，2 failed：S5 在 Work 中调用被放行（line 947）；S6 R1 读后被真实 resume 抢先时返回 `superuser_abandon_failed_only` 而不是 `version_conflict`（line 991） |
| `fixed/` | 本提交 | 6 passed；`superuser-abandon-failed-work-e2e.json` 为本次运行产物，连续两次运行逐字节相同 |

每个目录里：`junit.xml`（pytest `--junitxml`）、`pytest-summary.txt`（每个用例的 PASSED/FAILED 与计数）、
`work_control.blob`（所用 `work_control.py` 的 git blob id，可用 `git cat-file -p <blob>` 取回）。
`summary.json` 是从三份 junit 提取的每个用例结果、失败行号和工具结果中出现的错误码。
`junit.xml` 中的本机绝对路径已替换为 `<repo>`、`<tmp>`、`<home-path>`，其余内容未改。

复现（不改动工作树以外的任何东西；跑完还原 `work_control.py`）：

```bash
f=src/yuki/runtime/work_control.py
cp $f /tmp/work_control.py
git show db7b5c26:$f > $f   # 或 44d928a9
uv run pytest tests/integration/test_superuser_abandon_failed_work_e2e.py -q
cp /tmp/work_control.py $f
```

局限：这是在本机（macOS）用最终测试对旧实现的回放，不是 CI 上的运行记录；CI 只运行本提交的实现。
测试与旧实现的失败位置会随测试文件改动而变，以 `test_file_blob` 对应的版本为准。
