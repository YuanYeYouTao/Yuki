# Genie 语音输出彻底移除任务书

日期：2026-10-07（Asia/Taipei）。源码基线：main `25cd6083015924bf80405ca7c346f84a995f6ebb`；应用 3.9.0 未正式发布，Plugin API 3.1，主工具合同 version 12，数据库 head `0096`。

**状态：源码移除和本地验证已完成，正在收尾索引与用户授权的提交。** ASR、历史语音和原执行回执保留；本记录写于提交前，真实提交以 Git 及交付记录为准，未发布、部署或删除服务器文件。原索引基线已用于定位和源码复核，修改完成后单独更新索引。实施仍以[共同开发约束](development-contract.md)、[主 Agent 合同](main-agent-runtime.md)、[Tool Kernel](tool-kernel.md)和[插件兼容政策](../plugin-development/compatibility.md)为准。实施前重新核对主线，避免与并行开发的迁移和 SDK 版本冲突。

## 1. 目标与保留边界

彻底退出当前 Genie 驱动的本地语音输出域，清理合成、声线、偏好、工具参数、SDK、管理入口、Worker、转换器、配置、专属持久化和发布流程。移除前的 `SPEECH_PROVIDER` 只接受 `genie`；保留名为通用 TTS 的空 service/facade 仍会保留已经退出的功能。

- 不新增工具，不将删除改成常驻 disabled 工具、换名接口、Provider 自动替换或未来 TTS 占位框架。
- 保留 Qwen ASR、入站/引用语音、转写、历史检索、FTS/Rollup/Memory 来源与权限；`src/qq_ai_bot/asr/` 不依赖 Genie。
- 保留文本、图片、表情和文件发送，以及通用 artifact、内部事件、原 Work/Automation/控制操作与实际投递回执。
- 历史语音是真实历史；不全库删 `record`、不重写旧聊天、不把未知发送认成失败、不自动重新生成或改发文字。
- `AttachmentKind.AUDIO` 仍用于入站语音。当前未找到非 Genie 主动 QQ 录音发送消费者，出站合成专属 AUDIO 分支可以在旧计划清退后删除；音频作为普通文件上传继续可用。
- 不操作其他开发者的 Pi / Code Mode 分支，不提前移植 Code Mode，不迁移网关或扩大为其他功能清理。

## 2. 证据基线与复查范围

下表保留原任务书的 2026-10-07 线上只读核查记录。**原索引复查未刷新该表；实施期间另一次线上只读调查只核工作汇报故障，不重新证明语音清退条件。** 源码事实以下文固定基线为准，现场事实须在实施前重核。

| 项目 | 核查结果 | 实施意义 |
| --- | --- | --- |
| 线上 Bot | 运行 revision `964dadef`，schema `0096` | 后续部署从实际版本继续，不按应用版本猜测 |
| 语音输出 | `SPEECH_ENABLED=false`，未发现 Genie Worker 容器（包括已停止容器） | 不承诺移除会释放正在运行的 ONNX 模型内存 |
| 残留装配 | `SpeechModule.build()` 仍构建偏好/仓库、合成服务、客户端、声线与管理服务并注册生命周期 | 关闭开关没有完成退出 |
| 普通聊天 | `chat.py:1244,1401` 经 `VoicePreferenceService` 读取人物映射及语音偏好，即使偏好表为空 | 删除装配与读取链，减少实际 SQL/session 成本；提速幅度另测 |
| 线上配置与挂载 | 仍有 `SPEECH_*`、`GENIE_DATA_DIR`、`BOT_VOICE_NAME`；Bot 挂载 `/data/speech`、`/run/yuki-speech` | 模板、实际覆盖文件和运行容器都需核对 |
| 管理权限 | 实际 operator 文件含 `control.speech.read`、`control.speech.mutate` | 启动前显式撤销，避免再次因退役权限阻止启动 |
| 专属表 | profiles/references/generations 均有记录，人物偏好表为空 | 生成表不是纯缓存，先核引用与证据再移除 |

原线上检查只取容器/配置键和表存在性元数据。数据库使用只读连接、主键范围与明确 LIMIT，未读取正文、凭据或平台账号；未合成语音、调用模型、发送 QQ 消息、修改配置或重启服务。尚未核实是否存在未终态语音 Work/发送及其完整引用，属于实施前置检查；不能将“表非空”“偏好表为空”扩大为恢复安全证明。

### 2.1 全量索引检查点

先在 `aoci-yuki-index` 独立 worktree 完成 AOCI `v0.1.0-rc18` 索引，再开始本轮任务书复查。索引和本文所在 `issue257-audit` worktree 的源码 HEAD 均为上述 `25cd608`。

- production managed scope 的可索引对象 **1,235/1,235** 全部建立；Verify 结构有效、治理对齐，Check 通过，Guide `stage=aligned`、`complete=true`、`next_action=none`。
- missing、stale、orphan 均为 0；逐条校验长度配额及文件引用，无发现。
- 20 个二进制或超大图片按工具规则跳过；422 个 observe 对象主要是测试，按依赖回读，不冒称它们均建立了语义条目。SQLite 不作为本次数据库索引后端；ORM/冻结迁移按代码索引，未连接生产数据库。
- 本轮以索引确定职责和调用关系，再读原文件核实；索引覆盖率不等于实现、完整运行分支或线上验收。本文中的行号仅用于固定基线定位。

独立索引读取代理已完成 27/27 块交付，Host 确认和严格 Challenge 10/10 均通过。但同一 root/meta/code SHA 及 composite identity 下，Overview v2 的 `governance_aligned=false` 与随后 Verify/Check/Guide 的治理对齐结果不一致；原因尚未确定。保留收据，不修改工具或重复维护来消除标记。本轮不据此声称当前完整系统认知可靠，任务书结论均回绑实际源码；该投影问题与索引缺失/过期分开记录。

### 2.2 跨域依赖与本轮补齐项

路径默认相对仓库根；`src/qq_ai_bot/` 下模块省略该前缀。实施时逐项检查真实剩余消费者，不能用 `speech/voice/record` 关键词做批量删除。

| 范围 | 关键入口/关系 | 细化后的完成边界 |
| --- | --- | --- |
| 装配与 ORM | `application/modules/__init__.py` → `speech.py`；`persistence/metadata.py:35` → speech models；container 与四个 bundle | 删除实现同时撤去 exports、导入副作用和依赖注入；无表、无目录仍可启动 |
| 工具合同与执行 | `social/tools.py` → `domain/messages.py:311` → `capabilities/provider.py:372`；`social/service.py:224,710,965` | 主合同与 send schema 分别版本化；三个过滤边界均不能吞退役字段 |
| 旧投递恢复 | [work_delivery.py](../../src/qq_ai_bot/runtime/work_delivery.py) → [delivery_intents.py](../../src/qq_ai_bot/runtime/delivery_intents.py) → 原效果回执 | 原序列、hash、key 和预算保持；先对账，再对未派发部分做完整校验 |
| 历史与 ASR | `context_assembler.py:1256`、`persistence/models.py:229`、`asr/` | 历史 record→voice 展示及 ASR 转写保留；删除新合成路径不删旧事实 |
| 插件独立发送 | `plugin_host/facades.py` 的 `_SpeechFacade`、SDK events/permissions/registrar | Social 清理不能替代插件原执行清退；API、插件 manifest 和精确批准一起更新 |
| 配置与迁移 | `config.py:646`、`admin/config_specs_speech.py`、`speech/db_models.py`、冻结迁移 | 保留共享姓名 validator；按冻结形状识别合法 FK/CHECK/index，漂移在首次 DML 前拒绝 |
| 发布与当前文档 | `.github/workflows/release.yml`、[release_validate.py](../../scripts/release_validate.py)、`docs/upgrade-3.9.0.md` | bootstrap/release/finalize 均退出双镜像；四个 release-baseline 与新 head 同步 |

## 3. 清理任务清单

### G01：删除运行时合成域及普通聊天依赖

- [x] 删除整个 `src/qq_ai_bot/speech/`（17 个 Python 文件）和 `application/modules/speech.py`，不留下空 bundle/client/provider。
- [x] 清理 `container.py`、`application/modules/{persistence,conversation,automation,admin}.py` 的 speech 仓库、偏好、服务和管理注入；Automation 中仅保存的死注入直接删除。
- [x] 同步删除 `application/modules/__init__.py` 的 `SpeechBundle/SpeechModule` 导入与 exports，以及 `persistence/metadata.py` 的 speech ORM 导入副作用；无 speech 实现时 import 整个应用和迁移 metadata 均不报错。
- [x] 删除 `container.py` 的 speech startup/close/cleanup/events/health 分支，清理 `health.py` 的 speech 字段与访问。
- [x] 删除 `services/{chat,processor,agent_tools}.py` 的 `VoicePreferenceService`、`_voice_delivery_allowed` 及其参数传播；清理 `social/agent_adapter.py` 和 `SocialContext.voice_delivery_allowed`。
- [x] 删除 `services/prompt_composer.py` 的 `runtime.speech` 动态提示，不改写旧已提交历史和私有协议对象。
- [x] 清理 `chat.py` 的新合成账本构造 helpers 与调用者时保留普通文本、图片和表情记账；`context_assembler.py` 的 `_recent_delivery` 对历史 `record` 标记为 `voice` 的读取继续保留。
- [ ] 无 Worker、socket、声线目录和 speech 表时，Bot 能完成装配、启动、健康检查及普通聊天；前台不再查询偏好表或探测语音目录。

主要证据：`application/modules/speech.py:60`、`services/chat.py:1244,1401`、`speech/preference_repository.py:29`、`container.py:605,1021`、`health.py:39`。

### G02：撤去模型工具和合成发送参数

- [x] 删除 `set_voice_preference` 声明、执行、可用性判断，以及 `capabilities/{namespace,provider,policy}.py` 中实际存在的命名空间、绑定、来源、关键词/规则；不新增说明退役的工具。
- [x] 删除现有 `send_message` 的 `voice` 参数及 `SocialVoice`，仅清理其专属排他/校验分支，保留文本、引用、@、图片、文件、表情合同。
- [x] 删除 `SocialService.speech_delivery`、合成准备、Genie 生成成功记录和新语音账本构造分支；保留统一发送顺序、原路由、授权及未知效果围栏。
- [x] 删除 `OutboundMedia` 的 Genie 独占字段：`spoken_text`、`generation_id`、`voice_profile_id`、`voice_reference_key`、`voice_language`。`local_path`、时长等字段按实际剩余消费者决定，不删除共享图片 DTO。
- [x] 清理无真实非 Genie 消费者的 OneBot 出站 AUDIO/record 合成分支、`delivered_voice_text` 和未使用的新语音交付类型；保留入站 record、历史投影及读取旧回执必要的事实标签。
- [x] 主合同由 version 12 推进至下一可用版本（本基线预计 13），并显式提升 `send_message` 的 `ChatTool.schema_version`。当前默认是字符串 `"1"`，Provider 原样转发，不能只改主合同 revision。新激活使用新固定声明；旧已提交请求和回执不重写，旧未决调用先按 G03 结算，再在合法边界开启新链。
- [x] `social/automation.py` 注册 `social.send_message` 时同步传递该 Action 的新 schema version；当前漏传并继承 `AutomationCapability` 的整数 `1`。不把旧 Automation 暗套新 schema：已有脚本按原执行身份核回执，尚未派发且版本不符的步骤明确拒绝；需要继续的合法文本任务经现有编辑/授权流程建立新脚本版本，不重置旧 run、回执或预算。
- [x] `send_voice`、`send_group_voice` 已是旧工具拒绝测试，不当作现行工具重新实现；保留负向断言，并验证偏好工具及 voice 参数真正消失。

主要证据：`services/agent_tools.py:894,1175`、`social/{tools.py:136,models.py:38,service.py:965,1133}`、`domain/messages.py:203`、`adapters/onebot/sender.py:48`、`services/main_agent_contract.py:68`。

### G03：旧任务、回执和媒体计划收口

这一阶段先于生产删除字段、表和文件。复用现有恢复、核对、暂停和参数错误路径，不增加语音恢复状态机、常驻兼容服务或另一个账本。整合审查补充：主合同 12→13 的变化不得丢弃旧 delivery phase 和冻结计划后进入模型重新生成；投递读取视图保留原 chain/sequence 的效果键和来源核验，新模型合同不取得重派资格。

- [ ] 按可信内部 Work/operation/plugin-execution ID、现有索引和明确 LIMIT 分页检查未终态语音任务；时间只在 ID 范围内筛选，不全库扫描 JSON 正文猜所属账号。
- [ ] 停止接纳新的语音效果；优先在旧服务中核对并清退进行中合成、准备和已派发发送。无法完成的原任务沿现有暂停/取消路径收口，保留原 ID、预算、游标和 accepted/unknown 回执。
- [ ] 成功发送不重发，未知发送不自动失败或退款，确定未派发的旧 voice 调用不自动转换成文字。`_SpeechFacade` 有独立的插件授权、进程内 handle 所有权和发送执行路径，不经过 Social；同时按原插件 execution ID 核对，不能用清退 Social 代替它，也不能用重建 handle 或新 request ID 重发。
- [ ] 保留 `runtime/effect_queries.py:251` 对历史 voice 参数的效果识别边界；不能因为新 schema 没有 voice 就把旧语音成功误认成文字交付完成。
- [ ] 历史 record、转写、工具/控制审计和原协议回执继续可读；没有恢复调用者的字段才删除，不批量改写旧冻结 JSON。

#### G03-A：新调用及确定未派发调用的严格拒绝

`SocialMessage.extra="forbid"` 不足以保护入口：现行 `social/service.py:224–225` 的 `_canonical_send_arguments`、`:710–711` 的 `_send_message_sequence` 和 `:965–970` 的单条 `execute` 都先过滤字段。删去模型中的 `voice` 后，旧 `{text, voice}` 会被吞成纯文本。

- [x] 三个边界均对退役参数和未知字段做严格检查，再进入新发送的 route/prepare/效果登记；只处理消息层字段，保留合同中合法的 `target`、`reply_to_event_id`、`work_report` 等外层字段。
- [x] 按 **key 存在**拒绝 `voice`，覆盖 `null`、`{}`、`false`、错误类型及 `{text, voice}`，不使用值真假判断。主工具、直接 Social、Automation、SELF、插件背景和 SDK 相关入口都须覆盖，不能只验证公开 JSON schema。
- [x] 确定派发前拒绝使用现有参数错误合同，`executed=false`、`mutation_committed=false`；新合成、新发送、路由外部探测和新的发送意图均为零。已付 admission/尝试预算按原合同计量，不笼统退款。
- [x] **已派发调用不适用“统一参数失败”。** 先按原执行身份读 accepted/unknown 等事实并核原参数，保留原 Social `payload_hash`；不能先丢 `voice`、重新 canonicalize 旧请求或写成失败，再匹配原回执。

#### G03-B：旧图片的精确读取兼容

`work_delivery.py:34–46` 的 `asdict` 会把空 Genie 字段也写入图片计划，再以 `repr(values)` 计算 `plan_hash`。当前 `resume_delivery_plan:127–145` 先反序列化整份计划并调用 `plan()`，`:148–160` 才核逐片回执。删除字段后仅过滤 DTO 参数仍不足以保持恢复身份。

| 退役字段 | 允许的旧 IMAGE 默认值 |
| --- | --- |
| `spoken_text` | 精确的字符串 `""` |
| `generation_id` | `None`（JSON `null`） |
| `voice_profile_id` | `None` |
| `voice_reference_key` | `None` |
| `voice_language` | `None` |

- [x] 仅对旧 `kind=IMAGE` 的执行解码副本移除上述精确默认值；字段缺失按旧默认语义兼容。`0`、`False`、错误类型和非默认值均不算空，其他未知字段仍拒绝。
- [x] `local_path`、`duration_milliseconds` 等共享字段另按实际消费者处理，不混入五字段兼容规则。真正 AUDIO 即使 metadata 全空，也不能降格为图片、文本或普通可执行媒体。
- [x] **兼容仅产生本地读取视图。** 原 `delivery_plan` 冻结 JSON、final-plan `plan_hash`、Social `payload_hash`、意图 `payload_json`、片段顺序/段数、原 `effect_key` 和协议对象均不变。不得以新 DTO 重新 `asdict`、覆盖 progress、重算摘要或重新预约已派发意图。
- [x] `delivery_intents.py:64–70` 对原 payload、段数和所属 Work 精确比较；恢复时先按原 key 对账，已发送项无需重新构造 AUDIO DTO。需要发送的剩余项全部通过校验后，才允许原身份下的后续派发；先发文本再遇到退役 AUDIO 不是合法失败边界。

#### G03-C：恢复状态矩阵与验收

| 原事实 | 退役后的处理 | 新增外部效果 |
| --- | --- | --- |
| accepted 且 `transport_accepted=true` | 保留并复用原确认；跳过该片段，不要求重构已发 AUDIO | 该片段为 0 |
| prepared/dispatching/executing/unknown/uncertain 或证据不足 | 维持原事实及围栏，经原核对/暂停路径收口；名称本身不证明未派发 | 0，直到原合同明确允许 |
| 确定未派发的旧 voice/AUDIO | 明确拒绝；不合成、不转文字、不换执行身份 | 0 |
| 已确认 AUDIO + 未派发合法图片/文本 | 已确认部分不重发；剩余部分仅在原恢复资格、当前授权、预算及完整剩余计划通过后续派 | 仅合法剩余部分 |
| 未派发合法图片/文本 + 后续未派发 AUDIO | 任一新效果前拒绝整个不可执行剩余计划，保留既有确认事实 | 0 |
| 取消/generation 失效后的迟到确认 | 只结算已发生事实，不复活 Work、不重授预算、不发后续 | 0 |

验收使用真正旧序列化 fixtures，覆盖五默认值图片、逐字段非默认/错误类型、AUDIO 空 metadata、部分发送、重启、重复恢复和迟到回执。比较恢复前后的冻结计划/摘要/原 ID/预算，记录每个片段实际派发次数；不得只比较新 DTO 或返回字符串。

### G04：删除 SDK、管理与前端功能面

- [x] SDK 删除 `SpeechFacade`、`PluginContext.speech`、`GeneratedSpeechHandle`、FakeSpeechFacade/FakeContext 相关字段和 exports。
- [x] 删除五项权限：`speech.profile.read`、`speech.generate`、`speech.send`、`speech.manage`、`speech.provider.register`；清理高风险集合、Host facade、服务注入，以及 `events.py:78–88` 的全部 11 项 `speech.*` EventName。没有生产者的专属测试 Fake、类型和 exports 一并退出，其他事件不变。
- [x] 删除 `speech.facade.v1`、`speech.tts_provider.v1`，以及没有实际实现消费者的 reserved `TTSProviderRegistration`、`register_tts_provider` 和 extension registry 分支。
- [x] Plugin API 由 3.1 推进至下一版本（本基线建议 3.2），同步保留插件/示例、SDK 验证、发行验证与文档；旧版拒绝在导入前完成，不用兼容 shim。重新批准精确权限，保持原启用意图，不扩权。
- [x] 本基线需同步三个保留插件 `github-monitor`、`subscription-monitor`、`io.github.yuanyeyoutao.kun-game` 和 `examples/plugins/com.example.echo` 的 manifest/代码；重新核实际声明及 Host 所需版本，不只改 SDK 常量。新增 API 迁移说明交代退役能力、批准失效与宿主挂载插件升级。
- [x] 删除 `/ai voice` 的 enum、解析、管理动作、状态/help 和测试合成；删除 `qq-ai-bot-cli speech` 全部 parser/dispatch 与 prompt audit 中的专属语音样例。
- [x] 删除 Control Plane 的 `list_speech_profiles`、`mutate_speech`、SPEECH 资源、DTO、游标、端口、catalog、query/command/management 分支及导出。
- [x] 删除 `control.speech.read/mutate` 现行注册与 WebUI 通用资源页中的音色列表和启停操作，保留其他配置/工具/资源页面。
- [x] 历史控制审计中确有读取用途的 speech operation 分类仅保留读旧事实，不注册成可执行操作。
- [x] 音色管理界面退出后，保留 `context_assembler.py` 的历史语音标签、OneBot normalizer 的入站 AUDIO、`asr/service.py` 的 `no_speech`、`event_prompt.py` 的转写投影、`control_execution_query.py` 和 `frontend/src/chat.tsx` 的受权历史转写展示。

主要文件：`src/yuki_plugin_sdk/{api,context,models,permissions,events,registrar}.py`、`testing/`；`plugin_host/{facades,extension_registry}.py`；`admin/{action_service,capabilities,control_resolution}.py`；`services/{policies,command_service}.py`；`cli.py`；`control_plane/`、`persistence/control_{query,command,management}.py`；`frontend/src/pages.tsx`。

### G05：配置、偏好与数据库退役

- [x] 删除 `SpeechSettings`、`SpeechRuntimeConfig`、snapshot.speech、Settings.speech、专属校验/组装/配置写入字段，以及 `admin/config_specs_speech.py` 和注册。
- [x] 删除仅供 TTS 使用的 `BOT_VOICE_NAME`、`BotIdentity.voice_name`、`Settings.bot_voice_name`；保留人物显示名、别名与普通身份字段。
- [x] `config.py:601–615,630–643` 的 speech 专属 validators、`:835` 域验证、`:915` accessor、`:972` 身份组装，以及 `admin/config_service.py` 的启动映射/路径转换同步清理。`:646` 的混合 `@field_validator("bot_display_name", "bot_voice_name")` 只撤去 voice 字段，保留显示名的非空单行校验。
- [ ] `.env.example` 和实际部署删除 `SPEECH_*`、`GENIE_DATA_DIR`、BOT_VOICE_NAME，包括已过时别名和实际覆盖中的残留；ASR 配置独立保留。明确同时清退模板里的 `SPEECH_AGENT_EFFECTS_ENABLED` 与现行 `SPEECH_AGENT_DELIVERY_ENABLED`；`0063` 已处理旧 runtime key，不修改该冻结迁移。Worker timeout/idle recycle、JP katakana 等专属环境键与实际覆盖文件也逐项核对，不能只删当前 Settings 可读的键。
- [x] 新冻结迁移仅按以下明确键清理共享 `runtime_config_overrides`。实施时另核已废弃的 Genie 专属旧键，确认归属后加入固定清单；不使用 `%speech%`、不删除共享表或管理审计、不在每轮聊天清理。

```text
speech.enabled                  speech.provider
speech.socket_path              speech.root
genie.data_dir                  speech.default_profile
speech.agent_delivery_enabled   speech.default_mode
speech.split_sentence           speech.max_synthesis_characters
speech.queue_max_pending        speech.cache_retention_hours
speech.private_enabled          speech.group_enabled
speech.automation_enabled       speech.plugin_enabled
speech.text_fallback_enabled
```

- [x] 上述 17 个现行键按 `config_key` 精确相等清理其全部既有作用域行，保持其他键、canonical owner 与共享审计不变。旧 `speech.agent_effects_enabled` 等键只在重核归属后纳入固定退役清单，不通过通配符、字符串包含或删除整个配置域猜测归属。
- [x] 移除运行时 speech ORM 注册、persistence bundle，以及 `people_repository.py` 中仅针对 speech 表的隐私清理；其他 Person/ASR/Memory/trace 隐私删除与 generation 规则不削弱。
- [x] 以实施时单一 head 新增迁移（本基线预计 `0097`），退役四张自有表；不编辑旧 `0016/0017/0048/0049` 等冻结迁移和历史 DDL。

| 专属表 | 数据性质 | 退役前置 |
| --- | --- | --- |
| `speech_generations` | request_id、触发事件、生成/已发状态及 WAV 引用，含真实执行事实 | 核原通用回执与文件引用，保全必要专属证据，先 DROP |
| `speech_voice_references` | 参考音频、文本、hash，依赖 profile | generations 之后 DROP，用户自有参考资料先保全 |
| `speech_voice_profiles` | 声线模型配置/校验和 | references 之后 DROP |
| `person_speech_preferences` | 人物语音偏好 | 独立退役，不删除 Person 或其他偏好 |

- [ ] **不能把 generations 当作 MCP 式纯缓存。** 停写一致快照中沿原 request/trigger_event/conversation ID 核 Social、Work、outbound 与 Memory 引用；现有通用事实保留原身份，不重新入账。`sent` 字段不独立证明 QQ 已确认送达。
- [ ] DROP 前将仍需保全的专属元数据和被引用 WAV 连同校验和、原路径、内部 ID 映射保存到一次性私有冷备并验证。没有证明通用回执足够或尚未完成引用保全时，不直接丢弃有事实的表。
- [x] 不新建 Genie 归档表/并行 ledger，不让正常运行时读取冷备。导出、文件 hash 和历史检查在迁移 writer 前完成，事务内不扫描历史或等待文件 I/O。
- [x] **首次 DML/DDL 前完成全部 preflight**，包括 runtime overrides 的删除之前。核验四表全部列、类型、默认值、PK/UNIQUE/CHECK、索引/部分唯一索引与 FK；使用从当前迁移链固定下来的完整形状，不 import 可变 ORM，不先清配置或删一半再发现漂移。
- [x] 现行 head 提交后不能仅换旧镜像；优先向前修补。`0097` 明确拒绝 downgrade，不重建空结构冒充恢复，也不默认回灌旧业务库。

#### G05-A：冻结依赖形状与迁移原子性

Speech 原有合法 FK 共 6 条。`0048_canonical_3_8_baseline.py` 提供主体形状，`0049_canonical_only_bridge.py` 重建了现行 preferences；实施迁移须冻结最终 head 的完整形状，而不是摘抄旧建表片段。

| 子表 → 父表 | 已知动作/退役含义 |
| --- | --- |
| `speech_generations` → `speech_voice_profiles` | DELETE RESTRICT；先删除 generations |
| `speech_generations` → `speech_voice_references` | DELETE SET NULL；仍按 generations 在先的明确顺序 |
| `speech_voice_references` → `speech_voice_profiles` | DELETE CASCADE；不以删 profile 级联代替已核实的退役步骤 |
| `speech_generations` → `chat_events` | DELETE SET NULL；保留聊天账本 |
| `speech_generations` → `canonical_conversations` | DELETE/UPDATE RESTRICT；保留会话 |
| `person_speech_preferences` → `persons` | DELETE/UPDATE RESTRICT；保留人物 |

- [x] 允许且精确核验这些已知内部/出向依赖；拒绝任意外部表指向四张退役表的入向 FK、额外/变形 FK，以及依赖或附着退役表的未知 view/trigger。不相关 view/trigger 保留，不能依据历史已删的 shadow trigger 名称放行新增对象。
- [x] 不照搬 `0096_retire_mcp_metadata.py:119,128,141` 的“任意 default/CHECK/部分索引/FK 都拒绝”。Speech 自身存在这些合法结构；验收必须证明原合法完整形状可迁移，并证明漂移仍拒绝。
- [x] 复用 `migrations/env.py` 的显式 SQLite 迁移事务，将精确配置键删除和 DROP 作为同一原子变更；失败整体回滚，schema version 不前移。不得关闭 FK 检查来绕过依赖核验，也不在 writer 中导出数据、hash 音频或扫描历史引用。
- [x] 漂移反例逐类覆盖列/default/CHECK/index、外部入向 FK、依赖 view/trigger；拒绝后四表、所有 overrides、非语音数据与 schema version 原样，不能只证明其中一张表没删。

`0097_retire_speech_output.py` 已实现，隔离 SQLite 正例、漂移拒绝、原子回滚及 writer 竞争回归通过；没有执行生产迁移，也没有验证生产引用保全。该迁移明确拒绝 downgrade，采用向前修补。

### G06：Worker、工具链、部署包与 CI

- [x] 删除 `services/genie_tts_worker/` 全部 20 个 tracked 文件：IPC、Engine、日语前端、Dockerfile、entrypoint、独立 pyproject/uv.lock、专属测试。
- [x] 删除 `tools/genie_model_converter/` 全部 4 个文件，以及 `data/speech/japanese_frontend/` 的两个 tracked 默认样例；保持 `data/*` 忽略，不将本地模型/音频纳入 Git。
- [x] 清理 `.gitignore:17–23` 的 speech/日语样例放行规则，保留上层 data 忽略，防止删除 tracked 样例后本地模型或音频重新进入 Git。
- [x] 删除 Compose 的 genie-tts-worker service、speech profile、专属 socket volume 和 Bot 两个 speech 挂载；清理 dev build 覆写，其他网关/数据/环境挂载不动。
- [x] 删除 Setup 的语音页、目录创建、profile 探测、开关/声线选择及发布 action；实际 COMPOSE_PROFILES 中移除 speech，同时保留其他用户 profile。
- [x] 根 Bot pyproject/uv.lock 当前没有 Genie/e2k/torch/ONNX 重依赖；不删除共享依赖。Worker/转换器的独立锁随目录删除。
- [x] 删除 `scripts/release_validate.py:76` 的 Worker 1.9.0/目录/lock 硬校验，并同步实际新 Plugin API/schema 基线；保留应用/安装器/tag/head 校验及四份文档的 release-baseline 门禁。
- [x] 删除 Quality 的 Worker build 与 speech-worker job；发布 workflow 的 bootstrap、build、publish、latest、finalize/失败恢复所有分支去掉 Worker 双镜像要求及 `--profile speech pull`。
- [x] 不只删除 Worker build job：同步 `.github/workflows/release.yml:29` 的 `WORKER_IMAGE`、全部双镜像 for 循环、不可变版本发布调用、匿名 pull、latest tag/push/digest 验证、镜像清理列表和“两个镜像包公开”的 bootstrap 提示。保留 Bot 的 OCI revision、不可变 tag、匿名可拉取和 latest digest 核验。
- [x] `scripts/build_release_bundle.py` 不再创建语音模型/声线/cache/词典目录；`release_smoke.py` 删除 Worker 期待，但保留 Bot 重建、QQ、数据/配置/插件/工作区持久性验收。
- [x] 更新 versioned release 测试中的 Worker/日语目录/双 Dockerfile/speech pull 断言，保留包安全、应用镜像和不可变发行资产验证。

根安装器当前没有 Genie 专属实现，不为清理另加兼容分支；仍验证它们依赖的 source-free 部署模板和向导。已经发布的旧 Worker 镜像及发行包不删除或重写。

发布验证必须分别覆盖三条路线：bootstrap 只准备 Bot 包访问；release 只构建/发布/核验 Bot；finalize 从已发布 Bot 版本恢复附件与 latest，不拉取 Worker。先用静态/隔离测试证明三条分支均无 Worker/speech profile 依赖，不以触发真正 Release 作为代码验收。是否改变发行附件数量由实际 bundle 清单决定，不能按镜像减少直接修改附件数量断言。

### G07：文档、说明与历史事实

- [x] 改写中英文 README、help、架构索引、插件文档、现行主 Agent/聊天媒体合同和当前 3.9.0 草案/CHANGELOG：删 Genie 可用能力，说明语音**识别**保留，补升级边界；实际退役 PR 在创建后补充，目前只有本地修改。尤其更新现行合同中“主工具可合成语音”的说明，避免源码已退出而文档仍授予能力。
- [x] 同步 `README.md`、`README.en.md`、`docs/releases/v3.9.0.md`、`docs/upgrade-3.9.0.md` 四份 `<!-- release-baseline: version=... schema=... -->`，schema 取实施时实际单 head。升级指南新增 API、operator、旧执行清退、冷备及数据库向前修补步骤；不提前宣称 3.9.0 已发布。
- [x] 当前发布流程不再列 Worker 1.9.0 或推荐安装其镜像；正式 3.8.4 的真实历史资产另明确归属，不能描述成从未发布。
- [x] 删除 `docs/speech/` 下 12 篇 TTS 专属文档：architecture、automation、genie-worker、model-conversion、offline-setup、operations、planner-integration、plugin-api、qq-record-delivery、reference-styles、troubleshooting、voice-profiles。
- [x] 保留 `docs/speech/recognition.md`，移除与已退役 Genie 开关的比较句，保留 ASR 来源/配置/预算/引用/错误语义。
- [x] 若旧 qq-record 文档有共享合同内容，迁至现行媒体/平台文档后再删；不留下死 TTS 文档供普通功能引用。
- [x] 旧 Release/CHANGELOG/迁移/日期验收记录保留历史原义。指向已删文档的历史链接改为对应 tag 的文件；不全局替换 Genie/SPEECH/voice/record。
- [x] 当前功能面无 Genie imports、可调用入口、可编辑配置、启动依赖、打包/CI 需求或死链接。关键词残留仅允许在冻结迁移、历史证据、旧回执必要读取和明确退役测试/说明中，逐项有实际用途。

## 4. 实施顺序与现场清理

### 4.1 实施门槛

| 门槛 | 通过证据 | 未通过时 |
| --- | --- | --- |
| 源码边界 | G01/G02/G04/G06/G07 删除清单及剩余消费者说明；新主合同、send/Automation schema、SDK/API 与当前文档一致 | 保留待实施状态，不制作退出版本发布声明 |
| 恢复安全 | G03 状态矩阵、旧 fixtures、原 hash/ID/预算/派发次数回归；进行中语音执行的有界现场清单 | 不删生产字段、事实表或 WAV，不重发/重生成补验收 |
| 数据退役 | G05 完整冻结形状、合法 FK 正例/漂移反例、引用与文件冷备校验；独立副本 fresh/populated 演练 | 不执行生产迁移，保留可核验的旧事实 |
| 部署准备 | 实际 Compose 参数链、operator、宿主插件、退役 env/profile/挂载的精确变更与恢复方案 | 不启动新版本、不删除服务器文件 |
| 上线验收 | 实际镜像 revision/schema、唯一主动 Bot、QQ/ASR/插件和原任务核对 | 如实报告未完成，不以健康检查代替真实效果验收 |

前三项按用户授权并行实施，须全部完成才能越过生产删除门槛。本轮代码层验收和生产冷备、实际部署分别记录。

### 4.2 后续现场切换顺序

1. 重核主线、实际 Compose 参数链、插件合同、schema、operator 文件、路径/volume 使用者以及未终态旧任务。
2. 先实现 G01—G07 和定向验证；在独立生产副本演练完整迁移、配置加载、插件重新批准与恢复，不能启动第二个主动 Bot 写生产库。
3. 停接纳并清退旧语音效果，停止旧 Bot/明确 Genie Worker，保存数据库、配置、插件、被引用音频、协议对象与持久环境回执一致冷备。普通 QQ 网关可保持运行。
4. **启动前**从所有实际 operator 的授权中只撤去 `control.speech.read/mutate`，保持其他身份、roles、token_env、enabled 和权限；用目标代码离线加载验证。精确清理语音 env/profile/覆盖/挂载，不用示例覆盖生产配置。
5. 更新宿主挂载的保留插件到新 API，按原权限重新批准；执行新 head 迁移，启动一个新 Bot，核实际 revision、schema、固定工具合同、QQ/ASR/插件与恢复状态。
6. Worker 若存在，只移除明确 Genie 容器；当前样本无 Worker，不做多余重启。禁止 `compose down --remove-orphans`、全局 volume/image prune、清理 QQ 登录或其他环境。
7. 文件另外形成“精确绝对路径—归属/使用者—是否被引用—保全位置—可删除性”清单。`speech_root` 可自定义，不直接 `rm -rf data/speech`；先核引用/挂载再处理模型、声线、参考音频、WAV cache、词典和 IPC volume。
8. 清理源码不等于已经删除服务器文件。本轮仅实施源码与隔离验证；实际文件删除按后续明确授权和保全清单执行，不清整个 data、用户音频、共享 artifact 或旧有效备份。

## 5. 验证与验收

| 验证 | 必须得到的结果 |
| --- | --- |
| 固定工具/参数 | 语音偏好工具及 voice 参数消失；新调用/确定未派发旧调用的退役字段按键存在拒绝，发送次数为零；已有 accepted/unknown 先核原回执；主合同、send 和 Automation schema 分别版本化 |
| 普通聊天 SQL | 无语音偏好/session/目录/socket 查询；用相同隔离场景记录 SQL 和连接取得的前后差异，不拿代码行数推算墙钟提速 |
| 共享发送 | 文本、多条序列、@、引用、emoji、图片、文件与上传附言正常；失败/未知/迟到确认沿原回执处理 |
| 恢复重放 | G03 全状态矩阵及旧格式 fixtures；图片精确默认值兼容，冻结计划/两种 hash/意图 payload/顺序/key 不变；混合剩余计划在任何派发前拒绝不可执行部分；原预算及每片次数可核验 |
| ASR/历史 | 当前及引用识别、空识别 no_speech、失败/取消、转写/FTS/Rollup/Memory/隐私删除与历史 record→voice 标签/受权转写展示保留；音频文件仍能上传 |
| 数据库 | fresh 全链；populated `0096`→实际新 head；6 条合法 FK 与完整形状正例通过；漂移/外部依赖在首次配置 DML 或 DROP 前零部分写入；失败整体回滚，非语音 schema/data 不变；原引用冷备校验；匹配 writer 改动的 SQLite 竞争回归 |
| SDK/管理 | 旧 API 导入前拒绝；新非语音插件工作且批准不扩权；公开和实际 operator 文件可加载，权限不自动忽略 |
| 配置/前端 | snapshot/设置/CLI/QQ help/Setup/ControlPlane无语音功能，普通页面和 ASR 配置有效 |
| 交付 | 无 Worker 源目录仍可过 release_validate、Bot wheel/Docker import/start、source-free bundle、Compose JSON及 smoke；bootstrap/release/finalize 均不要求 Worker 或 speech profile，保留 Bot 不可变产物/digest 校验，不触发真实 Release |
| 文档 | 当前说明无可用 TTS 宣称/坏链接，四份 release-baseline 与实际新 head 一致；旧版本事实不改写，完整 PR 清单继续维护 |
| 线上 | 实际 Worker/挂载/环境/退役权限退出，仅一个主动 Bot；QQ和原任务状态正常。资源采样区分稳态/重启暂降，不以健康或缓存命中宣称自然回复提速 |

删除纯 TTS 单测和 Worker/转换器专属检查；保留混合测试中普通图片/表情/已确认效果的断言。重点现有入口：`test_public_tool_surface`、`test_social`、`test_social_delivery_integrity`、Work delivery/恢复、`test_plugin_facades`、SDK/权限、config/runtime snapshot/control/setup/frontend、`test_cleanup_pages`、`test_asr`、`test_derived_text_transactions`、`test_versioned_docker_release`。验证匹配实际改动风险，不机械重复无关全量；真实合成不用于证明已移除功能，也不自动发送 QQ 测试消息。

以下是后续实现的最小回归组织，文件名是现有测试定位入口，不声明当前已覆盖新增反例或已运行：

| 改动 | 现有入口与需要补的反例 |
| --- | --- |
| 工具/Social | `tests/unit/test_public_tool_surface.py`、`test_social.py`、`test_social_delivery_integrity.py`：三个过滤点、直接服务绕 schema、voice 值变体与旧 accepted/unknown |
| Work 投递 | `tests/unit/test_work_delivery_ownership.py` 及 Work 恢复测试：真实旧图片/AUDIO/混合计划、原冻结 JSON/hash、重复恢复及迟到回执 |
| 插件 | `tests/unit/test_plugin_facades.py` 与 SDK/Host 合同测试：旧 API 导入前拒绝、11 项退役事件/5 权限退出、保留插件新 API 和精确批准 |
| 迁移 | 现有迁移/数据库测试中新增对应 head 用例：合法完整 Speech 形状、populated 事实/冷备、逐类漂移零 DML、失败回滚及非语音数据保留 |
| ASR/媒体 | `tests/unit/test_asr.py`、`test_derived_text_transactions.py` 和历史媒体测试：入站、引用、空识别、出站历史标签、文本/图片/emoji/文件 |
| 配置/发行 | `tests/unit/test_config.py`、`test_runtime_config_read_snapshot.py`、`test_versioned_docker_release.py` 及 Setup/Control/前端测试：保留姓名 validator、移除专属键、三条发布分支和四份 baseline |

本轮已进入代码层实现与隔离回归；实际 QQ、生产冷备、迁移、Release 和部署验收仍未执行。验证结果在整合完成后记录，不以定向通过掩盖未完成项目。

## 6. 交付状态清单

- [x] 运行时、工具/SDK/配置/界面、部署/发布/文档三路源码审计。
- [x] 原任务书的线上有界只读元数据记录（历史证据，本轮未刷新）。
- [x] 全量受管 AOCI 索引建立与 Verify/Check/Guide 校验。
- [x] 按索引回读源码，细化任务书及恢复/迁移/版本/发行边界。
- [ ] 实施当天现场状态、未终态旧执行与完整引用保全重新核验。
- [x] G01—G07 源码实现及定向验证（生产操作、Docker build/smoke 另列未完成）。
- [ ] 生产数据副本演练及实际权限/旧任务清退；合成 SQLite 副本演练已通过。
- [ ] 源码和 AOCI 提交（用户已授权，按 Git 记录确认）；PR、合并、目标镜像构建和部署另行确认。
- [ ] 文件精确清理、线上验收与资源变化记录。

每项按真实证据分别更新，不把任务书、代码删除、合并或健康检查称为全部上线完成。

## 7. 本地实施记录

实施树为 `aoci-yuki-index`，分支 `codex/remove-genie-speech-output`。开始时重新 fetch，
`origin/main` 与源码基线 `25cd608` 一致。原主目录及另一任务书工作树不承接本轮源码修改；
原 stash 保留。此实施记录生成于提交前；用户已授权将源码、任务书和 AOCI 托管资产一并提交，实际提交身份以 Git 为准，未合并或部署。

| 范围 | 已取得的本地证据 | 尚未取得的证据 |
| --- | --- | --- |
| 当前功能面 | TTS 域、Worker、转换器、声线/偏好、SDK/工具/管理/前端及专属挂载退出；主合同 13、send/Automation schema 2、Plugin API 3.2 | 实际宿主插件批准和 operator 授权清退 |
| 历史与共享能力 | ASR、历史 record→voice、文本/图片/emoji/文件与原效果读取保留；无语音依赖的 Container/health 及真实 Processor 图片成功/失败回归 | 真实 QQ/ASR 自然流量验收 |
| 恢复 | 新 13 项跨合同恢复回归及独立复现通过；同合同隐私失效、prepare 后隐私变化均 0 派发，原 accepted AUDIO/key/hash/预算保持；重复恢复拒绝重发 | 生产未终态执行清单、WAV 引用保全及人工清退 |
| 存储 | 19 项退役迁移回归；独立空库全链至 0097，四表不存在，integrity=ok，foreign_key_check=0；历史往返 fixtures 固定历史 revision，真实 0097 不可 downgrade | 生产一致冷备及数据副本演练 |
| 发行 | release_validate、19 项发行测试、单 Bot Compose 静态校验、wheel 构建及应用/ORM/health imports；8 项发行附件合同保留 | Docker daemon 未启动，Bot 镜像 build 和真实 container smoke 未执行 |
| 质量 | frozen Python 3.12.13；Ruff、format、Linux 目标 mypy 669 源文件通过；全仓分段回归及失败复验、227 项保留插件回归通过 | 分段计数不冒充一次全量零失败结果；没有 CI/Release 运行证据 |

全仓首次在旧迁移 fixture 处停止：1,666 passed、5 skipped、8 failed；八项失败均已修复
并分别复验。后半 188 文件为 2,296 passed、5 skipped、2 failed，其中两项是在修补前
导入的 `0092/0093` fixture，最新整文件分别 26/39 passed。后半包含单独完成的 109 项
恢复回归；这些重复计数不再叠加成虚构的全仓通过总数。主会话变更后前段复验另为
120 + 31 passed，保留插件另为 227 passed。跳过项包括私有备份与 Linux 平台专属合同，
不补造生产样本或将 Windows 结果当作 Linux 实机验收。

代码删除后 AOCI 正式条目必须在稳定源码上单独维护，校验 missing/stale/orphan 和观察证据。
本轮全量 Overview 刷新发生 Host 输出截断，已停止该链，未确认交付或提交 Attestation；
按源文件证据继续维护，不宣称当前完整系统认知可靠。索引治理对齐与模型认知验证分别核验。

项目 AGENTS 的 AOCI 合同要求 Host 截断时停止认知链，并由用户将
`overview_delivery.chunk_tokens` 改为更小的合法值后重新开始；本轮未自动改配置或补答。

本地 3 个已忽略的旧 `.pyc` 缓存清理被自动审批拒绝，返回 `blocked by policy`；
缓存不含源码、不纳入 Git 或 wheel，保留原地。本轮未删除任何服务器文件。

后续由 dot 制定工作开始门禁修复方案和重构计划，输入事实见
[只读调查记录](../operations/work-start-delivery-investigation-2026-10-07.md)。
此调查未修改该机制，避免与 Genie 退出混为一次运行时修复。
