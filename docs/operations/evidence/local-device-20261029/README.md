# V02 / V09 两条最小真实组合补证 · 2026-10-29

原执行的后端源：`29cfbc5d1dcb3e737fdb18f170c9f2effb947e86`，Windows 本地，沿项目 `.venv`；该源码tree随后以 `29e8e680d61a720ff4dab1b218ec9d5073d02b53` 合入main。执行时脚本和产物位于 `.cache`，现将[脚本](test_local_access_v02_v09.py)与[原stdout](v02-v09-stdout.txt)随仓库保存；格式和import整理不改测试语义，保留 `pytest_plugins = ["tests.conftest"]` 以从本目录显式复现。

没有改产品、任务书、迁移、原PR源码或CI，没有打开用户完整访问、读取用户Connector配置、联系真实Agent/付费模型或生产QQ。此处持久化是交付文档，不新建运行时记录或验收框架；[32项映射](../../local-device-full-access-acceptance-20261029.md)区分源码、合并与现场交付。

从仓库根目录显式复现：

```powershell
.venv/Scripts/python.exe -m pytest docs/operations/evidence/local-device-20261029/test_local_access_v02_v09.py -q -s --basetemp .cache/v02-v09-docpath-20261029
```

原执行结果：`2 passed, 1 warning in 5.25s`，原始命令中的脚本为 `.cache/test_local_access_v02_v09_20261029.py`，basetemp为 `.cache/v02-v09-isolated-pytest-20261029-run3`；[原stdout](v02-v09-stdout.txt)保留原路径和合成测试IDs，不改写成新记录。warning 是既有测试建库时 `canonical_conversations/conversation_legacy_aliases` 循环 FK 排序警告，未修改/忽略测试断言。隔离SQLite、journal/日志及文件产物仍归原临时目录；持久证据包含复现脚本和原输出，不包含运行时数据库、测试凭据或用户资料。复跑请使用新的basetemp名称，保留已需核对的原样本。

持久化后按上述文档路径再次显式执行，`2 passed, 1 warning in 8.13s`，见[文档路径复跑stdout](v02-v09-docpath-stdout.txt)。脚本Ruff check/format通过；这是同两条组合的复跑，不能与原2项相加为4个独立场景。

## V02

复用原 `tests/integration/test_local_device_transport.py` 的 `actual_connector`、原 `bridge` TLS/WSS/HTTP 和 `python_command`，无 Agent 实例。

- 两个真实独立 cwd 分别含中文、空格、单引号和 `[]/$/#/&`；Python 脚本文件及输入/输出文件也含中文、空格、引号/方括号/$。
- Shell 真实读取各 cwd 独立输入，以实际 cwd 和原输入组成输出文件；测试逐字节核对，两份文件不串。
- 两个原 run 分别经 HTTP详情查到实际 Shell 退出码 `0` / `7` 和 succeeded / failed；主库 completion 保该码，progress不存输出页。
- Windows PowerShell `-Command` 对最后失败的 native 命令默认返回 Shell 码1。第一次测试把 Python 码7直接当 Shell 码7是假设错误；验收命令随后显式 `exit $LASTEXITCODE`，尊重当前 Shell 语义，不修改产品去猜内部程序的退出码。

## V09

复用 `test_subagent_common_capabilities.py::_message_child`、`make_child_executor` 真子Agent装配，向原测试工厂提供 bridge 已存在的 Social env（避免二次建立同一 Social fixture）。Chat 工具执行器与子Agent依赖均接**同一个** SandboxClient / LocalDeviceService，未造新任务执行框架。

1. FakeLLM返回本机 terminal_exec；真实TLS/WSS Connector用原 Person 授权启动实际 Python Shell 脚本。
2. 原 SandboxTask 记录核同 child Work、Person、Conversation 和 device target；原脚本等待测试释放文件，确定性地在运行时登记同 run 的 owned_run wait。
3. 释放实际脚本后，原 completion 通过真实 Connector 事件入库；原 continuation 和 wait signal 各一次，使同 child 从 waiting_external 变 queued，不新建 Work/执行。
4. 恢复同 child，FakeLLM 请求 terminal_read；下一次实际模型请求输入中包含该原脚本真实输出 `child-finished-雪`，再按原 task_control complete 结算。
5. marker证明原 Shell仅启动一次；parent整行与开始前完全相同；child累计 model_requests `2→4`，tool_calls `1→2`，sent_messages `0→0`，未重置预算。

实际隔离 IDs / usage 见原输出的 `V09_REAL_EVIDENCE`。4次LLM调用全部本地FakeLLM，没有模型网络请求；fixture的Bot仅记录一次 `get_group_member_info` 替身读取，无 send，真实QQ调用为0。测试没有打开用户设备完整访问；只有隔离临时 Config 的 full_access=True。

## 边界

两条补足此前矩阵里明确缺少的**Windows隔离真实组合证据**，不改成五目标真机、用户安装、生产或真人问答验收；没有新增全电源状态矩阵、外部CLI维护、性能平台或完成门槛。没有发现新的产品缺陷，不追加源码提交或重跑无关全量CI。
