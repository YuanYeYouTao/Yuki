# 删除导向重构集成记录（2026-10-08）

本记录覆盖原任务书 68 项及追加 DEP-02、RES-03，共 70 项。任务索引的勾选表示实现及对应定向验收已完成；生产部署和最终全量结果单独记录，不能用勾选代替上线证明。

## 集成边界

- 以 main `aecc09d621067b7782c2b89e919fe2a7b2c9e5fa` 与 Pi `e7bc7d3275b09ddc5363bbb1eb9d2ee255f276ed` 合并输入实施，保留现行开发合同。
- 用户确认只有当前 main 系谱 0096 生产数据库需要升级，无需保留 Pi 0097/0098 数据库。0097 为 main 语音退役，0098 为 MCP 状态退役，0099 为 summary representation 冻结；不重写历史已部署 revision。
- MainAgentContract revision 16、SDK 3.3、context layout 2、Code API v1 分属各自 owner。实际 Provider profile 保持既有独立 revision。
- 旧 accepted / unknown / dispatched Social 与 Work 回执按原身份查询；新目录无语音、MCP、run_python 或 SDK LLMFacade。旧 active_seconds 只读，原预算、请求、任务和投递 ID 不重置。
- Code Mode 可关闭，关闭时不构造新 Code 引擎，旧调用安全拒绝或按历史回执恢复；终端能力仍是直接工具。启用时使用 pinned 原生 Monty worker、固定 launcher 及 AppArmor/seccomp 隔离。

## 分项证据

- [API、文件、终端、Control、Automation、SDK、CLI](deletion-api-tools-20261008.md)
- [Runtime、恢复、身份和首次上下文布局](deletion-runtime-20261008.md)
- [数据库、Memory、上下文和 Artifact](deletion-data-context-20261008.md)
- [Code 原生隔离合同与真实服务器 canary](../../deploy/security/README.md)
- [真实 enforce 探针原始结果](deletion-codemode-isolation-20261008.json)

最终联合定向检查：语音表退役、历史 receipt 恢复、合同升级、Social 长键及 Code 合同合计 126 passed；真实 Linux Code runner 11 passed；Chat / Automation / Plugin SDK 三类入口 20 passed。纯终端/文件真实 Linux 矩阵和 200MiB 附件、停机 bootstrap 锁见 API 证据。历史 0081、0091、0096 的真实旧 producer 写入原事实后升级路径分别通过。

`ruff check .`、`ruff format --check .`、Linux 目标 `mypy src`（683 个文件）与 `release_validate --tag v3.9.0` 已执行。源码冻结后的全量回归、最终镜像及生产迁移验收尚在进行，完成后在本记录追加确切结果。

## 删除量口径

相对 Pi 固定输入的源码提交 `bcb43a74` 统计（包含 main 集成，非全部归因于新重构）：产品 `src` 新增 4689 / 删除 12010，净减少 7321 行；迁移新增 270 / 删除 27；scripts 新增 125 / 删除 122；tools 新增 49 / 删除 1299。测试搬迁、冻结历史迁移 SQL 和必要保护不能冒充产品净删除。最终提交可直接用上述固定输入复算。

## 生产准备与回滚边界

现生产库为 0096，旧 run_python 全终态、无旧插件执行 cohort；两运行插件仅迁 API 3.3 并按原精确权限重新批准。Manager 保留原 jobs.sqlite3、home、workspace、completion outbox 和 quarantined 记录。部署只替换 Bot 及其所需 Manager 代码，不重建 SnowLuma，不发送额外 QQ 测试消息。

在线只读 SQLite 一致快照已完成并 quick_check 通过；后续在网络禁用、无生产数据挂载的容器中对独立副本做迁移和原列数据哈希对比。正式升级须在停 Bot 后另建数据库及相关文件恢复点；0097 删除能力数据的回退依赖整份冷备，不宣称 downgrade 可以恢复已删除事实。


## 末尾复审补漏（合并前）

- API-03：直调协调、结果缓存、TurnExecution 和 Code 子调用里仍有从展示 JSON 推断 executed / pending / committed 的消费点。迁为既有 typed outcome 与原 durable evidence；展示内容不决定执行状态，原 ID 与预算不退款。历史缺失或错型 outcome 的保守判据另行覆盖。
- MEM-01：Runner 与 Code 中重复的 memory 独占批次门退出；direct 同批 send_message 必须先观察 Memory 回执的门、Code 真正 Memory 写入之后的 Host stop、unknown 围栏和每次授权保留。死 locator 异常、无 caller 的 score 兼容别名和旧图片禁写 docstring 同步清理。
- AUTO-01：无执行 caller 的 automation_text_generation 路由枚举及安装/示例/检查链退出。旧 TOML 明确拒绝；部署仅移除该条路由，不重写其它模型配置或历史 telemetry task。
- APP-08：上一代 decoder 的 provider_summary=None 省略分支退出；实际生产旧 SHA 964dadef 的 WebSearchResponse 已有该字段，不需要该分支维持当前回退读取。
- 现行 help、SDK 迁移入口、运行时政策和发布草案同步；历史任务书和旧部署报告仍标明其历史范围，不把原证据改写为当前通过。

第三轮 Windows 全量结果为 4669 passed、257 skipped、1 failed（Social 并发测试在 2 秒准备等待中超时）；该完整文件不修改测试或超时后复验 9 passed。Windows/POSIX/native 跳过由 Linux 专项矩阵补充。WebUI 在 CI 的原 5 秒默认预算下全通过；本机重负载下 timeline 单项超时，单独以 15 秒进程测试预算复验 4 passed，不修改产品或仓库测试超时。最后新增源改动另有定向回归，最终 CI 按实际提交核对。

生产一致副本 0096→0099 已通过：135 个保留表原列及行数 SHA256 完全相同，新增 projection 列全 NULL，quick_check=ok、foreign_key_check=0、正式 schema guard 通过。MCP 两表在原 0096 已不存在；退役 speech 表的原行数为 108/1/6/0，原备份文件前后 SHA 相同。证据含初次环境配置缺失导致的零写入失败及修正后完整成功日志，保留在私有运维目录。

部署前语音文件盘点：以原一致数据库记录逐条解析 107 条非空相对路径（6 个参考文件及 101 个生成文件），实际绑定的 `/opt/yuki-qqbot/data/speech` 中均已不存在，原目录只保留 japanese_frontend。此次盘点未删除文件，不能宣称把这些缺失 WAV 纳入了冷备。原表、路径和缺失事实另存私有 manifest，正式冷备仍保存全部现存 data；历史 accepted/unknown 发送回执按原身份保留，不因源 WAV 缺失推翻已发生效果。

终审补改验收：直调 typed / 原回执 144 passed；运行时最后观察边界与隐藏拒绝 19 passed；Code 单元 251 passed，Linux 三入口与 control 30 passed、追加 control 完整 11 passed；历史 outcome 完整回归 85 passed、补充严格边界 22 passed；退役模型路由 44 passed。历史回执展示字节不改，未知不取得重执行资格。

最终历史容器边界再审 90 passed，260 组 Python/SQLite 差分一致。有效 typed 拒绝重放的测试后端补齐真实 side_effecting 布尔角色，联合 72 passed；没有为错型 null 角色放宽生产 unknown 保护。

`bcb43a74` 源码的 direct 镜像确认不含 Monty binding，并通过镜像环境变量 `CODE_MODE_ENABLED=false` 显式关闭 Code；该提交的 Settings 字段默认仍为 true，产品默认 false 是后续最终提交中的修改。codemode 镜像已在实际生产内核通过无 bind mount 的打包隔离探针，worker/launcher 全部来自镜像，见 [打包探针原件](deletion-codemode-packaged-20261008.json)。任务索引 68 项源码及专项验收均已标记；最终 CI、上线仍单独记录。


## 用户要求的再次终审与追加范围

逐项删除闭包补充见 [16 项主会话实现核对](deletion-final-review-20261008.md)。原 68 项保留原 ID，用户追加 DEP-02 默认 direct 发行/部署和 RES-03 内存检查优化，现共 70 项。任务索引以对应实现、验收和部署边界逐项更新。

- API-03 补齐原生 final 领域回执与工具结果类别的区别：原传输 accepted 或明确未派发失败按原 kind/state/严格布尔读取；unknown、错型、缺事实与普通 tool 同形 payload 不获豁免。原 CI 隐私测试的消息派发始终为零，修正的是结算误报。完整 lifecycle 114 passed，三组退休/恢复 56 passed；两次独立 SQL/Python 差分 1600 与 2664 组均一致。历史字节及原 ID 不改写。
- MIG-01 补齐精确 0058/0060/0088 停点：固定 Pi 原 Git 产生结构与当前同停点一致，原 Work/source/journal、预算与上限、accepted/unknown 效果及附件字节升级 0099 后保持。Windows 与 Linux 各 3 passed，原结构指纹跨平台一致。
- 当前能力清单重新从源码生成，75 项且连续两次结果相同；移除已退役 run_python/set_voice_preference，纳入真实 Host lookup_tools，现行文档不再宣称旧 Memory 执行入口可用。原历史 P10 快照未改写。
- 旧 benchmark 只在实验内显式适配已删除接口，仍加载固定原 loop/reporting，报告记录三种源/适配 SHA。12 个 assembly 场景完整覆盖：旧 5、新 7，另 1 项新进度分类单测；共 13 项定点通过，不冒称 13 个 assembly 或整文件全量。
- CI efa3d3c7 为 4739 passed、239 skipped、9 failed：8 项旧展示断言已按 typed 合同修正，完整 Work communication 22 passed；另 1 项 final 领域结算误报由上述回执修正闭合。该次 CI 本身仍记录为失败；最终源码另跑 CI。

共享主机测试期间出现高负载与换页压力，SSH 延迟和 Bot 健康超时；两只本次测试容器停止后 SSH 恢复，Bot 回到 healthy，Bot/SnowLuma 的原容器 ID、启动时间及重启计数未变。Manager 期间发生自动重启。未把未完成的服务器补验记为通过；原测试日志保留，剩余定点验收在本地 Linux 完成。后续不在约 1.6 GiB 内存的主机上叠加重测试、保留大 tmpfs 与镜像解包。

部署准备再审补齐：冷备先验证原 Manager 作业终态，停 Bot/Manager 后仅按原 ID 暂停持久环境，核写入容器后复制并恢复原暂停；不重跑任务。切换前复验 overlay、候选源码及安装源码的完整哈希集合；断线按原意图和实际状态恢复，不盲重发。生产实际执行和最终健康仍待下节记录。

## 最终冻结源码与删除交付

最终源码 `f4f483d70c3fdb0302b2bece9f6220971ef3f955`。相对前一镜像完整smoke提交 `d768f1baeaa568dfb1ee91f3a41b0aafe1ea0f61`，仅3个测试文件变更（+8/-4），产品src不变。以下统计按精确提交计算，不覆盖本报告前文的历史轮次结果。

| 固定基线 | 全树默认Git shortstat | 产品src新增/删除/净变化 |
| --- | --- | --- |
| main `aecc09d621067b7782c2b89e919fe2a7b2c9e5fa` | 858 files changed, 542861 insertions(+), 11215 deletions(-) | +11690 / -8450 / +3240 |
| Pi `e7bc7d3275b09ddc5363bbb1eb9d2ee255f276ed` | 654 files changed, 32909 insertions(+), 20563 deletions(-) | +4847 / -12066 / -7219 |

src行数使用关闭rename检测的numstat；文件删除清单使用Git -M检测后的D状态，排除被识别为移动的文件。相对Pi净减7,219行，相对main净增3,240行必须并列；main引入Pi的新实现/文档/证据不算删除收益。测试搬迁、冻结SQL及文档不计产品行数。

### 集中删除文件清单

两个基线都存在、最终删除的6个产品路径（也是相对main的全部D文件）：

- `src/qq_ai_bot/application/module.py`
- `src/qq_ai_bot/application/provider_registry.py`
- `src/qq_ai_bot/capabilities/registry.py`
- `src/qq_ai_bot/memory/runtime/command_plane.py`
- `src/qq_ai_bot/memory/runtime/finalizer.py`
- `src/qq_ai_bot/observability/__init__.py`

另19个相对Pi删除的产品路径在固定main已经不存在，是集成继承main语音退役的闭包，不能全部记为本轮新删：

- `src/qq_ai_bot/admin/config_specs_speech.py`
- `src/qq_ai_bot/application/modules/speech.py`
- `src/qq_ai_bot/speech/__init__.py`
- `src/qq_ai_bot/speech/admin.py`
- `src/qq_ai_bot/speech/cache.py`
- `src/qq_ai_bot/speech/db_models.py`
- `src/qq_ai_bot/speech/delivery.py`
- `src/qq_ai_bot/speech/genie_client.py`
- `src/qq_ai_bot/speech/language.py`
- `src/qq_ai_bot/speech/models.py`
- `src/qq_ai_bot/speech/paths.py`
- `src/qq_ai_bot/speech/preference_repository.py`
- `src/qq_ai_bot/speech/preference_service.py`
- `src/qq_ai_bot/speech/profiles.py`
- `src/qq_ai_bot/speech/provider.py`
- `src/qq_ai_bot/speech/repository.py`
- `src/qq_ai_bot/speech/service.py`
- `src/qq_ai_bot/speech/style_resolver.py`
- `src/qq_ai_bot/speech/text_normalizer.py`

因此相对Pi实际删除产品文件25个、全树65个；其中全树59个在main基线已不存在。原AgentState移动到tests/support及两个迁移改号不计为D文件。必要历史reader按原Work/operation ID保留，唯一owner与具体退出条件见三个分工报告和终审表。完整机器可读指标见私有final-deletion-delivery-metrics.json。

### 最终direct镜像与smoke证据

- f4本地镜像ID：`sha256:82ed61b72b0231fd43f933f79467deef5778ae7efa08324789d48e15897478bb`，OCI revision与f4完整SHA一致。Docker archive config ID为`sha256:7208b68e877f5ecd6d9cf5c5a5a806b6d09082615935f2c6102d85a8022362de`；服务器实际load后应独立inspect记录，不能把本地index ID冒充server identity。
- 默认target构建和f4实际packaging gate通过：binding、worker、launcher均不存在，Code默认false。私有证据direct-image-f4f483d7-metadata.json及direct-packaging-f4f483d7.json。
- 实际完整source-free smoke运行于d768：CLI/setup验证、fresh0099迁移、direct无native包装、Bot健康、Bot重建后持久数据、NapCat登录挂载重建；日志末行source-free smoke passed。未临时改写smoke配置以绕过校验。
- 独立读取两镜像inspect：全部12个RootFS layers按顺序相同，Config仅OCI revision标签不同。f4复用了d768相同运行文件系统的完整smoke证据，f4自身另过packaging；没有把完整smoke描述为在f4重复执行。此等同性不替代f4最终CI，更不等于生产上线。

### 内存修复与旧生产基线口径

- 修复MemoryRebuildService永久累计取消run ID：仅以弱引用记录实际被本服务取消的活任务。26项相关回归通过，显式取消与外部取消语义仍区分。
- 两个独立本地Python进程各20,000次受控取消：原实现GC后保留20,000个ID、tracemalloc当前3,253,824bytes；修后保留0、当前1,000bytes（含测量列表）。修后四段RSS均93,233,152bytes。详见deletion-memory报告；该测量证明具体增长消除，不是全系统长期无泄漏证明。
- 旧生产采样来自主会话私有memory-baseline-1/2.json，只读进程树合计（不是单一Bot进程或cgroup同口径）：

| UTC | 健康 | RSS KiB | PSS KiB | Swap KiB | 主机MemAvailable KiB |
| --- | --- | ---: | ---: | ---: | ---: |
| 2026-10-07T21:46:04.427087+00:00 | unhealthy | 444956 | 440086 | 321520 | 521912 |
| 2026-10-07T21:47:44.558962+00:00 | healthy | 452044 | 452022 | 294508 | 519696 |

两次采样的原容器StartedAt均为2026-10-06T20:07:53.329766101Z，约一天uptime；主机MemTotal=1,651,684KiB。第一次unhealthy、第二次healthy的真实背景保留，不挑选有利样本。新direct冷启动不得与旧热进程换算RSS/Swap百分比收益；上线后同口径、稳定时长与负载另采样。高conversation基数的版本围栏暂不盲删，短测限制仍保留。

### 最终 CI 与生产执行

源码 `f4f483d7` 的 [Quality CI](https://github.com/YuanYeYouTao/Yuki/actions/runs/37696648000) 五项 job 全部成功：主测试 4800 passed、239 skipped；插件 Echo / GitHub / Subscription 分别 4 / 84 / 32 项、两个 CLI 检查、mypy 683 + 5 文件及 fresh 0099 均通过。原 4c 的 9 个失败是 Code 专项 fixture 未显式启用 Code，修复后 52 项场景回归通过；没有将旧失败/取消 CI 记为成功。

[PR #265](https://github.com/YuanYeYouTao/Yuki/pull/265) 已于 2026-10-07 22:59:06 UTC 合并，merge commit 为 `dd819fe28c6e3c4a5fc9b70531db0f3328942680`，合并树与 f4 完全相同。实际生产执行与验收结果见 [生产验收记录](deletion-production-20261008.md)。
