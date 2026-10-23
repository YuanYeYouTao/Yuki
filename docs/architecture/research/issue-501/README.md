# #501 隔离复现附件

配套报告：[操作身份统一必要性与影响调研](../../Yuki-501操作身份统一必要性与影响调研-2026-10-23.md)。

后续施工与验收统一见[删减修复任务书](../../Yuki-501原调用身份与回执恢复删减修复任务书-2026-10-23.md)。本目录继续保存修复前的研究证据，不维护另一份待办。

基线固定为 `8aef3f0bb2969856e014e052717ef742b1c91461`。这些是调研附件，**没有修复运行代码，也不加入默认 `tests` 收集**。它们依赖该提交的测试 fixture，未来源码演进可能需要调整。

## 文件及结果

| 文件 | 用途 | 基线预期 |
| --- | --- | --- |
| [memory_recovery_probe.py](memory_recovery_probe.py) | F1：多次修正后重入；add/delete 对照；F2：领域 commit 后缺 Work T3 | 两项正确行为断言失败，一项对照通过 |
| [external_control_probe.py](external_control_probe.py) | F3：原生 steer 有领域回执，公开 get 可读，Work 返回无 ID 的 unknown | 通过；断言的是缺口存在，不能当修复通过 |
| [probe-output.txt](probe-output.txt) | 归档后对上述两个脚本联合运行的原始输出 | **2 failed / 2 passed / 1 warning，3.04s** |

归档脚本仅做格式化、增加说明和未使用局部变量改名，保留原探针行为；归档后已再次执行。源码未变。脚本 Ruff 检查与格式检查通过。导入 fixture 的 F401/F811 在脚本中局部说明，这是 pytest 按名称注入所需。

输出中的人物、事件、操作 UUID 与正文均由测试临时生成，不是生产数据。没有模型调用、QQ 发送或真实设备连接；原生 succeeded 是仓库接收的模拟响应。Memory 的连接池 dispose 和 native 的未写 T3 构造的是持久中断边界，不是实际进程 kill 测试。

## 独立运行

使用已经准备好依赖的 Python 3.12 环境，从固定源码树执行。下面是本轮实际路径；换机器时只替换三个路径。不要从旧工作目录导入 `yuki` 或 `tests`。

```powershell
$issue501Source = 'D:/Code/My_Github_Repo/Yuki-issue-501-research'
$issue501Evidence = 'D:/Code/My_Github_Repo/Yuki-QQbot/docs/architecture/research/issue-501'
$issue501Python = 'D:/Code/My_Github_Repo/Yuki-QQbot/.venv/Scripts/python.exe'
Set-Location -LiteralPath $issue501Source
git rev-parse HEAD
$env:PYTHONPATH = "$issue501Source/src;$issue501Source"
& $issue501Python -c "import yuki; print(yuki.__file__)"
& $issue501Python -m pytest -p tests.conftest `
  -o addopts= -o asyncio_mode=auto `
  --rootdir=$issue501Evidence --confcutdir=$issue501Evidence `
  "$issue501Evidence/memory_recovery_probe.py" `
  "$issue501Evidence/external_control_probe.py" `
  -q -s --tb=short --disable-warnings
```

当前基线两条缺陷断言应返回失败，因此 pytest 非零退出是预期的调研结果。不能把它改成 skip/xfail 后声称问题已经解决。将来实施时，F1/F2 应转为通过正确行为的正式回归；F3 应把“确认 unknown 缺口”的断言改为“能读原控制回执、没有再次派发”，不能照搬其当前通过状态。

这组附件只证明四个限定场景。任务书仍需补真实来源/归属冲突、隐私删除、缺失/未知回执、其他 native 控制动作、预算与 Code/worker 对应边界，不能拿这四项代替完整实施验证。
