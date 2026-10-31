# 模型配置与旧来源迁移（2026-10-31）

本轮把模型连接、用途路由和非聊天连接统一到已有 v3
`webui-config/model_profiles.toml`，密钥仍保存在同目录私有
`model_profiles.secrets.json` 或显式声明的环境变量中。迁移不启动 Agent、插件、Gateway
或模型工作者，不调用付费上游，不修改人物、设备、Work、原生会话或已提交请求的身份。

## 一次性离线迁移

`yuki admin model migrate-legacy` 是本机维护命令，不调用正在运行的管理 HTTP API，
不要求领取另一份 operator token。它沿现有 SQLite Application 排他锁运行，Bot 运行时不能
同时执行；配置写入复用 `ConfigFileService.save()` 和原 bundle revision CAS。

1. 备份原 TOML、sidecar 和一致的 SQLite 数据库快照；数据库在 WAL 模式时不能只复制主文件。
2. 停止 Bot，保留数据库、其他服务及挂载。使用包含本轮完整实现的精确镜像／源码，保留原
   `.env` 和已激活的全局重启配置来源。可以先在备份挂载副本上运行同一命令，验证后再在停机
   窗口内应用其结果；不需要为了迁移先启动一轮新的 Bot。
3. 在部署根目录执行：

   ```sh
   yuki admin model migrate-legacy
   ```

   已知预期 revision 时可显式传 `--revision <revision>`；否则命令读取本次快照 revision，
   随后仍由原 writer 检查 CAS。版本冲突或无法确认结果时返回非零，不能当作迁移成功。
4. 检查退出码、JSON `ok/revision/migrated/migrated_profiles/consumed_env_fields`，以及保存后 bundle 的有效性、
   连接启停、地址、模型和已保存密钥状态。输出不包含密钥或旧配置值。
   非聊天连接如实需要重启，`apply_mode` 为 `restart`。
   首次成功保存并清退数据库旧行后，从实际 `.env`、Compose 或进程环境来源中退休
   `consumed_env_fields` 列出的旧聊天变量。命令只报告变量名，不自动编辑任意用户文件。
5. 若使用挂载副本，确认原文件仍是备份时版本，再应用经过验证的配置与数据库结果，保留原
   权限及所有权。只启动 Bot，分别验证健康、实际配置加载和自然业务流量；离线迁移成功不证明
   真实供应商请求或账单已经验收。

现有 `[embedding]`、`[vision]`、`[asr]`、`[search]` 段逐段保留，包含明确的
`enabled=false`；不会从旧环境值补洞或覆盖它们。缺失段按现有启动解析读取 Settings 与已激活的
全局重启覆盖。ASR 直接沿既有完整连接借用规则解析，不另外按品牌或模型名选择凭据。
缺失的 `[search]` 段同时迁入当前真实 `WEB_MODE`（或原 `WEB_ENABLED` 解析结果），包括
`disabled`，不会因为保存 Tavily Key 或搜索后端而意外开启联网。已有段缺少 `mode` 时保持
旧模式来源；只有用户显式选择模式才解除这一间接来源。
缺少原功能所需地址或凭据时保留实际关闭状态，不回写空密钥；原有别名不被不同密钥覆盖。
已有段、别名和实际参数会保留。消费的旧聊天环境来源退休后，再次执行不会用旧来源覆盖用户
后续的 Profile 编辑；仍保留这些旧变量时，不能宣称重跑无条件幂等。

bundle 保存确认后，命令在一个短 SQLite writer 内按原行 ID、key、scope 和 version
清退本次消费的旧聊天覆盖（包括已识别的 `NULL` 空覆盖）；原审计和控制回执全部保留。
文件保存与旧行清退不是同一原子事务。若旧行并发变化或清退无法确认，返回非零、
`partial=true` 和已保存的 bundle revision，不声称整体成功，也不在 writer 中等待文件 I/O。
此时先核对已保存 revision、该 bundle 和旧行，再决定如何处理实际环境来源及显式重试；
不能把未知结果当作完全失败后盲目重跑。

## 旧聊天参数

Main 不再把构造时的 `LLM_TEMPERATURE/LLM_MAX_OUTPUT_TOKENS/LLM_THINKING_ENABLED`
作为每次调用的默认值注入。普通请求使用所选 Profile 的默认值，调用者明确传入的单次参数
仍由原 `TaskModelExecutor` 处理。旧 `llm.*` 重复覆盖入口及无用的指纹字段退出。

迁移通过原始 Settings 的 `model_fields_set` 识别 `.env` 或进程环境实际显式提供的
`LLM_TEMPERATURE/LLM_MAX_OUTPUT_TOKENS/LLM_THINKING_ENABLED`，不把 Settings 构造默认值
反盖 Profile。实际全局数据库覆盖按原优先级高于显式环境来源。
旧三项 registry 不接受 `NULL`：此类旧行原本会被 resolver 忽略，继续使用环境或构造默认，
不能将它误称为 executor 已收到 `None`。迁移忽略这类无效值，有显式环境来源时保留该值；
没有显式来源时保留 Profile，不生成“无限输出”或额外 Profile。
真正改变参数的旧值迁入 `chat_agent` 与
`plugin_agent_session` 对应 Profile。如果该 Profile 同时服务其他用途或搜索，迁移仅克隆
配置数据并重指这两条 Agent 路由，原 Profile 和其他用途保持原参数。思考值按原解析后的
Profile 比较；迁入的明确思考选择会解除该目标 Profile 的旧 `thinking_mode` 间接覆盖，
未消费的 Profile 模式保持原样。没有运行时 fallback 链。

人物／群覆盖不能无损折叠为本轮的全局模型路由。发现非空的 group/user 旧值时，命令在写入前
返回 `scoped_model_overrides`，只列 key、scope、count，不输出值，也不改文件或数据库。
维护者先核实并明确处理这些真实配置，不自动将某个人物／群的值提升为全局值。

旧 `LLM_MODEL` 的显式 profile 环境引用仍是有效来源。有真实持久化的全局 `llm.model`
重启覆盖时，迁移把该引用当前解析到的模型写入对应 Profile 并解除这一间接引用；没有该覆盖时
保留原环境引用。旧 plugin session 的 `model`、原请求及历史回执不会删除或改写。

## 下一版本的退出条件

本版本仍保留非聊天连接“段缺失时读取旧 Settings”的过渡路径，以便尚未迁移的既有部署正常启动。
不能宣称所有外部用户部署均已迁移，也不在启动时自动写文件、自动改 Key 或重跑迁移。

已知生产、维护者本机和随包模板完成迁移并核对实际加载后，下一版本再删除非聊天连接的
旧缺段读取路径、对应旧身份配置说明及无用字段。并发、超时、帧数、限流、保留期限等行为
设置仍有实际用途，应保留。

显式 `base_url_env/model_env/api_key_env/reasoning_effort_env` 是 Profile 的正常来源合同，
不等同于被退役的隐式覆盖。仍被 Profile、安装向导或旧会话使用的 Settings／密钥引用不能
随迁移批量删除；按实际引用核对后再清理。历史审计、控制回执、原 Work 和向量索引保持原事实，
不因为配置迁移删除或重新执行。
