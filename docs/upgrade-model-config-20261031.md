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
4. 检查退出码、JSON `ok/revision/migrated/migrated_profiles`，以及保存后 bundle 的有效性、
   连接启停、地址、模型和已保存密钥状态。输出不包含密钥或旧配置值。
   非聊天连接如实需要重启，`apply_mode` 为 `restart`。
5. 若使用挂载副本，确认原文件仍是备份时版本，再应用经过验证的配置与数据库结果，保留原
   权限及所有权。只启动 Bot，分别验证健康、实际配置加载和自然业务流量；离线迁移成功不证明
   真实供应商请求或账单已经验收。

现有 `[embedding]`、`[vision]`、`[asr]`、`[search]` 段逐段保留，包含明确的
`enabled=false`；不会从旧环境值补洞或覆盖它们。缺失段按现有启动解析读取 Settings 与已激活的
全局重启覆盖。ASR 直接沿既有完整连接借用规则解析，不另外按品牌或模型名选择凭据。
缺少原功能所需地址或凭据时保留实际关闭状态，不回写空密钥；原有别名不被不同密钥覆盖。
迁移后的再次执行按已有段、别名和实际参数收敛，不无条件重写文件。

bundle 保存确认后，命令在一个短 SQLite writer 内按原行 ID、key、scope 和 version
清退本次消费的旧聊天覆盖（包括已识别的 `NULL` 空覆盖）；原审计和控制回执全部保留。
文件保存与旧行清退不是同一原子事务。若旧行并发变化或清退无法确认，返回非零、
`partial=true` 和已保存的 bundle revision，不声称整体成功，也不在 writer 中等待文件 I/O。
此时先核对该 bundle 和旧行，再显式重试；不重复创建 Profile 或用旧值盖回迁移后的用户编辑。

## 旧聊天参数

Main 不再把构造时的 `LLM_TEMPERATURE/LLM_MAX_OUTPUT_TOKENS/LLM_THINKING_ENABLED`
作为每次调用的默认值注入。普通请求使用所选 Profile 的默认值，调用者明确传入的单次参数
仍由原 `TaskModelExecutor` 处理。旧 `llm.*` 重复覆盖入口及无用的指纹字段退出。

迁移只使用实际持久化的全局 `llm.temperature/llm.max_output_tokens/llm.thinking_enabled`
覆盖，不把 Settings 的构造默认反盖 Profile 的显式配置。`NULL` 没有提交明确参数，保持原
Profile；不生成“无限输出”或额外 Profile。真正改变参数的旧值迁入 `chat_agent` 与
`plugin_agent_session` 对应 Profile。如果该 Profile 同时服务其他用途或搜索，迁移仅克隆
配置数据并重指这两条 Agent 路由，原 Profile 和其他用途保持原参数；没有运行时 fallback 链。

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
