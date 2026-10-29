# 本机 Connector 闲置采样（2026-10-29）

这是一组本地受控前后样本，不是线上验收或资源 SLA。各 16 个原始采样点保存在
[修改前 JSON](local-device-idle-before-2026-10-29.json) 和
[修改后 JSON](local-device-idle-2026-10-29.json)，复现脚本为
[`scripts/benchmark_local_connector_idle.py`](../../scripts/benchmark_local_connector_idle.py)。

## 条件与方法

Windows、本机 CPython 3.12.13、当前开发依赖；运行真正的 Connector CLI 子进程，
连接测试专用 TLS WebSocket 回环服务。实例列表为空，没有 Agent、
浏览器、模型、Shell 工作或生产凭据。预先准备当前日志 3 MiB 和两份各 4 MiB 的保留
日志，共 11 MiB；它们只是测试数据，不包含用户日志。

修改前来源为 `origin/main` 的 `02ce928ee35a4e891082cd40edd96a79f348d963`，通过
`git archive` 提取到 `.cache/idle-before-2026-10-29`，未切换或回退工作树。
修改后来源为本任务实现后的工作树，已撤回自研浏览器 CLI，并包含本机终端输出轮转。
旧配置没有完整访问字段；新版样本显式开启该项。
两轮均未派发操作，使用同一解释器、依赖、TLS fixture、日志数据形状与采样间隔。

收到 hello 后等待 1 秒，随后每秒读取一次该解释器的 OS CPU 时间、Working Set RSS、
Private Bytes 和进程 IO，同时记录测试服务收到的应用 payload 字节与日志总字节。
启动、准备数据和校验阶段不计入采样。Windows venv 启动器不是执行解释器，脚本直接
运行同版本实际解释器并复用当前虚拟环境依赖，避免把启动器的内存误当 Connector。

## 实际结果

| 指标 | 修改前 | 修改后 |
| --- | --- | --- |
| 测量时长 | 15.120 秒 | 15.143 秒 |
| 进程 CPU 时间增量 | 0 秒 | 0 秒 |
| 单核 CPU 比例计算值 | 0% | 0% |
| RSS 范围 | 41.500 MiB，无增长 | 40.727 MiB，无增长 |
| Private Bytes | 28.387 MiB，无增长 | 27.352 MiB，无增长 |
| OS 进程读／写字节增量 | 0／0 | 0／0 |
| WSS 应用 payload 收／发增量 | 0／0 | 0／0 |
| 日志总量与增长 | 11 MiB，增长 0 字节 | 11 MiB，增长 0 字节 |

CPU 为 OS 计时器内未观察到增量。两份样本没有显示闲置 CPU、应用流量或日志增长退化。
单次进程 RSS 相差不到 1 MiB，不能归因为产品内存优化，更不能按它估算并发上限。

本轮支持的结论是：在无原生实例、无实际工作的短暂闲置条件下，没有发现循环扫描保留
日志、持续应用轮询或日志增长。不能由此声称 CPU 绝对为零，也不能推算开启 Codex、
Claude、浏览器或实际进程后的内存。

15 秒没有覆盖默认 20 秒 WebSocket ping 周期；应用 payload 计数不包含 ping 控制帧、
TLS/TCP/WebSocket 封装，不是整机网络字节。RSS 是 Windows 当前工作集，受 OS 调度
影响。日志样本验证闲置时的增长，没有测试高流量轮转。断线重连、积压输出和活跃任务
不属于这个样本，不能将本次结果延伸成这些场景的性能承诺。

复现命令（测试环境需主项目 dev 依赖）：

```powershell
.venv/Scripts/python.exe scripts/benchmark_local_connector_idle.py --output .cache/local-device-idle-before-2026-10-29.json --seconds 15 --connector-source .cache/idle-before-2026-10-29/packages/yuki-agent-connector/src
.venv/Scripts/python.exe scripts/benchmark_local_connector_idle.py --output .cache/local-device-idle-2026-10-29.json --seconds 15 --full-access
```
