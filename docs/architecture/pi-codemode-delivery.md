# Pi 与 Code Mode 开发交付记录

2026-10-04；目标与不变量见 [设计合同](pi-codemode-design.md)。

## 授权与 Git

- 本地实现和隔离测试已授权；本地依赖安装及指定 Monty 编译已单独授权。
- 远端创建 `codex/pi-codemode-experiment` 并拉取已单独授权且实际完成。
- HEAD/远端测试分支/远端 main 均为 `8204b28ebc8939213dae60dbab94ab1c16d1263a`。
- 开始时工作区干净；原主工作树 `main` 未切换、未修改。
- 当前代码提交、代码推送、PR、合并、付费 API、真实消息、生产访问及部署均未执行。
- 共享 Git 存储存在既有 AppleDouble `._pack-…idx` 索引报错；fetch/push/分支追踪
  实际成功。没有删除或修复无关 Git 元数据。

## P00：基线与清单，已完成

已核对现行 development-contract、Tool Kernel、主 Runtime、Provider、child 与持久环境边界；
按精确 SHA 获取 Pi/Monty。基线迁移 head 为 `0091_ordinary_turn_admissions`；生产版本未读取。

新增隔离声明导出器和 X02 回归：实际 manifest 每行映射 descriptor/binding，缺少映射失败，
没有输出 schema 的项明确为 null。部署相关插件/MCP 与生产清单不冒充已采集。
入口映射见设计合同 E01–E14。macOS 上 SQLAlchemy async 缺 greenlet，已改用现有
SQLAlchemy 的 `asyncio` extra（`pyproject.toml`/`uv.lock`）。

## P01：显式调用身份，已完成

- `MainAgentBackend.begin_batch` 与 `_batch` 按“名称＋参数”回找身份已删除；Backend 只暴露
  `execute_call(Invocation)`。Runner 仅为仍实现 `begin_batch` 的自定义/测试 backend 保留兼容
  调用，P10 删除。
- 身份为 `chain:request_sequence:provider_call_id`（过长时为全元组 SHA256），同参不同 call
  各自执行，跨响应重用 call_0 互不冲突，同响应重复 ID 在派发前拒绝。
- 所有来源的 Social 回执 `call_id` 现为 Host operation ID，不再是响应内 Provider call ID
  （此前仅 SELF 如此）。`test_gemini_web_search_main_turn` 的断言随合同更新。
- `_mutation_identity` 的单次写授权只限 `memory_change` 与管理写，不再合并合法同参发送。
- 未知 descriptor 的缓存分类改为保守副作用。

## P02：原 effect 内持久化，进行中

已实现：T2 将根预算扣除与 `dispatch_started` 合并为同一短事务；T3 保存结果并拒绝冲突结果；
`0092_invocation_effect_indexes` 为版本化 invocation 增加父调用、子序号、引擎调用 JSON 索引，
存在新事实时拒绝 downgrade。3.9.0 未发行，其 README/发行/升级文档基线已同步为 `0092`。

已写未测：`ProtocolStore` 的 `CodeSnapshotBinding`/`get_code_snapshot` 与
`WorkRepository.publish_code_boundary`（T1 父快照＋子意图）。尚无调用者和 fixture，
不宣称通过。未开始：restore 的未结算 composition 分流、late receipt、artifact 发布失败窗口、
独立下游事实日志的子进程强杀测试。

## P04 前置：Monty 原生 worker 编译

固定 `3f9d6ef` 在 Rust 1.96.0 上编译 `monty` crate 失败（`string_cache.rs:98`，定长数组
`&mut` 不是迭代器）。最小补丁 `vendor/patches/monty-3f9d6ef-string-cache-iterator.patch`
改为 `iter_mut()`，不改语义。打补丁后 `cargo +1.96.0 build --release -p monty-runtime`
成功；二进制 `monty`（含 `subprocess` 协议子命令）sha256
`2db64324459259fa291fe24d03518677b5f44f5eabcdb2a9435b00482d2d9430`，仅本机
aarch64-apple-darwin。冒烟只跑了列表推导脚本，不是 sandbox 验收；产物在系统临时目录，
P09 需改为可复现构建。

## 验证记录（2026-10-04）

```sh
uv run --frozen ruff check src tests scripts migrations   # 通过
uv run --frozen ruff format --check src tests scripts migrations   # 通过
uv run --frozen mypy   # 705 文件无错误（修复 _unresolved_clause 的 bool/ColumnElement 混用后）
uv run --frozen pytest -q tests   # 第二轮：2758 通过、1 跳过（需生产备份路径）
```

第一轮全量：2727 通过、7 失败、24 错误。失败为本次删除旧 `execute/begin_batch` 后未迁移的
4 个测试辅助、Social call_id 合同变化 1 项、迁移 head 推进后 3.9.0 基线文档 2 项；错误为
WebUI 前端资源未构建（`npm ci && npm run build`，产物已 gitignore）。均已修复并定向重验。

## 后续依赖

P02 剩余项完成后进入 P03（Pi 循环移植）与 P04（Monty 驱动）。P05–P10 未开始。
P11 真实外部与生产验收待单独授权。代码提交、推送、PR 尚未执行。
