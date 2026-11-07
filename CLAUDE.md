# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

项目级硬约束在 AGENTS.md（删除优先、禁止防御性编程、身份/授权/回执规则、测试方式），必须遵守：

@AGENTS.md

## 常用命令

需要 Python 3.12、uv 和 C 编译器（Vec1 SQLite 扩展随包由 `hatch_build.py` / `scripts/build_vec1.py` 构建）。

```bash
uv sync --all-extras --managed-python      # 安装依赖（含 dev）
uv run ruff check .                        # lint（line-length 100）
uv run mypy --platform linux src           # strict mypy，包为 yuki 与 yuki_plugin_sdk
uv run pytest tests/unit -q -m "not native_worker"         # 与 CI 一致的单元套件
uv run pytest tests/integration -q -m "not native_worker"  # 集成套件
uv run pytest tests/unit/test_xxx.py::test_name -q         # 单个测试
uv run yuki init-db                        # alembic upgrade head（迁移在 migrations/versions）
uv run yuki --help                         # 管理 CLI：plugin / model / runtime / memory 等
uv run yuki-bot                            # 启动服务（NoneBot2 + FastAPI）
```

- pytest 使用 `asyncio_mode = "auto"`，`pythonpath` 已含 `src` 与 `packages/yuki-agent-connector/src`。
- `native_worker` 标记需要真实 Monty Code Mode worker（`scripts/build_monty_distribution.sh`），只在 CI 的 native job 跑；`qwen_embedding_integration` 需要真实 DashScope API。
- 有头桌面测试（`test_computer_*` 等）在 CI 里跑在 `deploy/sandbox/Dockerfile.environment-desktop` 镜像中，需要 `YUKI_TEST_CHROMIUM`。
- 部分测试需要 `ffmpeg`/`ffprobe`。
- `packages/yuki-agent-connector` 是独立发布的本机连接器，有自己的 `uv.lock`，在该目录下单独 `uv sync` / `ruff` / `mypy src` / `pytest`。

## 架构概览

开始改架构、运行时、工具或持久化前，先读 `docs/architecture/development-contract.md` 和 `docs/architecture/README.md` 中对应模块的现行文档（中文）。任务书和日期报告是当时的基线，不代表现行实现；源码与现行文档不符时直接删改，不叠加矛盾补丁。

**依赖方向**：传输适配 → 应用运行时 → 领域服务/执行器 → 仓库或外部适配器。插件只经 `yuki_plugin_sdk` 使用能力，不直访 ORM、数据库或 Host 私有对象。

**入口与装配**：`src/yuki/main.py` 是 NoneBot2/FastAPI 入口（`/livez`、`/healthz`），`src/yuki/container.py` 的 `ApplicationContainer` 装配全部服务；`src/yuki/cli.py` 是 `yuki` 管理 CLI。Docker 启动走 `scripts/start.sh`（`yuki init-db` 后 `exec yuki-bot`）。

**身份模型**（`identity/`、`docs/architecture/canonical-runtime.md`）：Person、Account、Space、Conversation、GatewayConnection 各自是独立事实；`AccountAssignment` 是账号归属的唯一来源，Binding/Presence 只是其投影。平台消息 ID 只用于去重、引用、撤回和投递关联，业务沿内部事件 ID 传递。

**多平台接入**：`adapters/onebot`（QQ via SnowLuma）与 `gateway/providers`（Telegram、飞书/Lark 等）只做原生协议转换；`client_api/` 是个人设备 API（配对、SSE、附件、Work 接续），`acp_adapter/` 是 ACP 服务，`external_agents/` 对接本机 Codex/Claude Code/ACP Agent（经 `packages/yuki-agent-connector`）。

**统一主 Agent**：聊天、SELF 自主行动、定时任务、插件和长任务共用同一主 Agent loop（`agent_core/loop.py`），提示词装配在 `prompting/`，人格只来自 `character/SOUL.md`。`runtime/` 管 Work（任务/激活/预算/journal/父子工作者/投递回执），恢复按原执行 ID 和持久回执处理，不重置预算、不重发。

**工具**：`capabilities/`、`executor/`、`codemode/`（Tool Kernel）。部署冻结一份完整工具清单；direct 模式直接声明全部工具，可选 Code Mode（Monty 沙箱）复用同一清单和执行器。权限在执行处核验，目录查询不改已提交声明。

**模型**：`llm/` 实现 Chat Completions、Responses、Claude Messages、Gemini 协议方言；`model_runtime/` 管 Profile 与用途路由。不按内容隐式切换 Provider 或增加付费调用。

**记忆与上下文**：`memory/`（按事实 ID 的独立事实、提取/SELF/Dream 可分别配模型）、`conversation/`（每个 Conversation 一份公共历史与 compact 检查点）、`tool_results/`、`workspace/`、`sandbox/`（单独部署的持久 Linux 环境，由 Manager 管生命周期）。

**持久化**：单个 SQLite（`persistence/database.py` 的 `Database`）。writer 通过 FIFO 排队 + `BEGIN IMMEDIATE`，`read_session()` 并行只读。写事务内不得等待模型/网络/文件 I/O，也不扫描历史；查证和身份解析在 reader 中先做完。迁移在 `migrations/versions/`（编号顺序，冻结迁移不依赖可变 ORM），当前 schema 版本见 `docs/architecture/README.md`。

**插件**：`plugin_host/` + `src/yuki_plugin_sdk/`（Plugin API 9.0），示例在 `examples/plugins` 与 `plugins/`，文档在 `docs/plugin-development/`。

## 交付约定

- 删除功能时一并核对装配、配置（`.env.example`、`config/`）、提示、文档、测试与 CI（`.github/workflows/quality.yml`）。
- 汇报时区分本地修改、验证、提交、推送、合并和上线。
- 提交和 PR 不添加 Claude 共同作者或 "Generated with Claude Code" 署名。
