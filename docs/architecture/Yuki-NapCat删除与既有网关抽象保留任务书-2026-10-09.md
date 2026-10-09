# Yuki：NapCat 删除与既有网关抽象保留任务书

## 0. 最高约束

注重可扩展性和可维护性，不把代码写死，为未来变化留出必要空间。**能通过删除或放宽现有条件解决，就不增加替代代码；非必要不增加代码。**

本次彻底退出 Yuki 的 NapCat 支持，保留**现在已有的网关抽象**和 SnowLuma 实现。无需新增抽象、接口、注册框架或切换协调器；也不把业务核心改成 SnowLuma 专用逻辑。以后接入其他网关，沿用现有合同。

删除专属实现时一起清理消费者、装配、配置、部署、测试和当前说明。旧测试和“以前为了安全”不能证明一个限制必要；真实身份、授权、来源、连接代次和已发生效果仍须正确。共同约束见[开发合同](development-contract.md)。

## 1. 范围与基线

- 审计基线：main `e783175d41fb48dc5c7e7a352c255e9099258115`。完整路径相对仓库根目录；源码模块短路径相对 `src/qq_ai_bot/`，同段短文件名沿所指模块。行号只用于定位该基线。
- 当前交付：实现、验证、PR 合并、镜像构建、生产上线与服务器清理均已完成；实际回执见任务索引、§12 与[上线报告](../operations/napcat-retirement-20261009.md)。3.9.0 仍未正式发行。
- 实施目标：Yuki 不再附带、装配、安装或承诺兼容 NapCat；当前附带网关为 SnowLuma，网关层继续可替换。
- 用户于 2026-10-09 追加授权：按老规矩 PR、合并、本地构建、上传上线，并清理服务器旧镜像，只保留最新与第二新。只更新 Bot，保持 SnowLuma 实例和 QQ 登录资料；清理先核实际容器、挂载和持久引用。
- 不把“NapCat 上游停止维护”写成事实：2026-10-09 核查官方仓库时仍未归档，且当天发布了 [v4.18.34](https://github.com/NapNeko/NapCatQQ/releases/tag/v4.18.34)。本任务依据是用户决定让 **Yuki 停止支持 NapCat**，不依赖对上游维护状态的判断。

任务书是待实施方案，不能用它覆盖现行架构合同；完成代码修改后再同步现行合同。未来实施开始时复核 HEAD 和消费者，不机械照搬这里的行号。

## 2. 任务索引

**完成一项，就在对应索引行标记完成，并填入实际验证或交付证据；仅改了文件不能提前标记验收完成。**

| 编号 | 任务 | 状态 / 完成证据 |
| --- | --- | --- |
| N00 | 审计运行时、部署、配置、测试、文档与 CI，形成任务书 | 已完成：main e783175d 的两轮只读审计；二轮补充见 §11 |
| N01 | 删除 NapCat Provider、适配器、入口、孤立启动元数据和公开导出 | 已完成：真实 SnowLuma 路由注册、原 socket 生命周期及自定义 Provider 回归通过 |
| N02 | 删除核心品牌分派、失效继承与无人使用的来源兜底 | 已完成：通用社交操作、自定义 Provider、原 ingress 与历史来源回归通过 |
| N03 | 删除 NapCat 渲染器、CLI 和管理占位配置 | 已完成：doctor 沿既有 catalog；旧命令和配置占位删除，定向检查通过 |
| N04 | 删除 Compose、镜像构建、启动脚本与环境模板专属支持 | 已完成：Compose 实际解析仅 bot/snowluma，bot 无旧环境/挂载；direct 镜像构建及上线验证通过 |
| N05 | 简化 setup：删除 NapCat/both、隐式默认和无消费者切换状态 | 已完成：8 个 setup 行为用例通过，空选择/自定义 profile/原备份语义保留 |
| N06 | 停止创建、打包 NapCat 目录；核对旧部署与资料保护 | 已完成：新 zip/tar 各 66 项，无专属目录或实际 .env；遗留私密资料排除保留 |
| N07 | 删除重复主机品牌名单与无效发送重选 | 已完成：真实 send 本地失败/断连/未知结果不换 Bot 不重发；URL/DNS/Base64 smoke 通过 |
| N08 | 删除专属和源码关键词测试，调整现有通用 fixture | 已完成：4 组首轮 84 passed，补充路由/社交/历史来源后相关回归通过 |
| N09 | 同步当前 README、帮助、架构、部署、3.9.0 与插件文档 | 已完成：15 份当前文档与 Plugin API 3.3 同步，168 本地链接/锚点通过 |
| N10 | 完成定向行为验证、包检查与适用的现有 CI | 已完成：本地1088 passed/50 skipped；PR276 Linux CI1089 passed/49 skipped及静态/前端通过；direct构建、token/路由握手、source-free持久化通过 |
| N11 | 终审可执行残留、既有抽象及历史数据边界 | 已完成：独立审查无阻断发现；原 Registry/migrations 未改；生产旧字段、服务与挂载也已清退，历史来源与私密资料保护保留 |
| N12 | 按用户授权记录 PR、合并、部署和线上状态 | 已完成：PR276 合并main528e2adc，本地构建后上传ops-528e2adc；Bot healthy/restart0/OneBot connected，SnowLuma原容器不变 |
| N13 | 清理服务器旧镜像与无引用垃圾 | 已完成：Yuki仅留528e2adc和31d12022；移除3旧镜像、旧备份、上传包、临时构建、空旧volume/APT缓存；释放约3.0GB，磁盘54% |

## 3. 保留什么

| 现有层 | 保留理由 |
| --- | --- |
| `gateway/provider.py` 的 GatewayProvider、GatewayConnectionProfile、GatewayProviderCatalog | 已有中性接口、连接事实和装配目录。provider 标识继续是通用字符串，不缩成仅 SnowLuma 的类型或品牌枚举 |
| `gateway/models.py`、`gateway/registry.py` | 原 handle、Presence、健康、generation、能力、连接冲突和断连事实，不能用单一网关推断它们无用 |
| `adapters/onebot/provider_adapter.py` 的 ProviderOneBotAdapter、ProviderConnectionGuard | 同一 Provider 的重复 socket、快速重连、迟到断连也需要同步生命周期和真实账号归属 |
| `gateway/providers/social.py` 的 OneBotSocialOperations | 两个 Provider 当前共用的实际社交动作实现；无需再造 facade |
| `gateway/providers/snowluma.py`、SnowLumaOneBotAdapter 及其配置渲染、现有 WS 路径 | 当前可运行实现；品牌只在实现与装配处存在 |
| OneBot 中性收发、媒体/ASR、文件 handoff、插件 SDK、权限和持久回执 | 真实消费者仍存在，不能按 NapCat 曾使用这些功能就删除 |

`builtin_provider_catalog()` 删除 NapCat 成员后继续存在。现有 Catalog 本来就支持自定义 Provider；使用既存测试 `_Provider` 验证这个能力即可，不增加新测试框架。现有 `test_canonical_ingress.py:284–317` 还覆盖了自定义 `lagrange` 来源入账。

## 4. N01–N04：删除专属实现及其消费者

| 位置（基线） | 具体处理 |
| --- | --- |
| `src/qq_ai_bot/gateway/providers/napcat.py:1–49` | 整文件删除：NapCatProvider、ID、能力常量及 napcat_provider_catalog |
| `src/qq_ai_bot/gateway/providers/__init__.py:4–7,16–18,22–27` | 删除 NapCat 导入、builtin 实例与导出；保留现有 Catalog 装配，同步修正“一律需要显式选择”的旧 docstring |
| `src/qq_ai_bot/adapters/onebot/provider_adapter.py:16,119–127,171` | 删除 NapCatOneBotAdapter、专属导入和导出；其继承开启的旧 OneBot HTTP/WS 接入入口随之退出，不把旧入口改接 SnowLuma |
| `src/qq_ai_bot/adapters/onebot/__init__.py:5,12`；`src/qq_ai_bot/main.py:16,77` | 删除公开导出、import 和 driver 注册，核对所有消费者 |
| `pyproject.toml:94–97` 的 `[tool.nonebot]` | 删除孤立的 plugins/plugin_dirs/裸 OneBot V11 适配器启动元数据。本仓正式入口不消费它，外部 NoneBot 工具却可另生成注册裸 Adapter 的启动脚本；不把声明改成 SnowLuma，也不增添另一条启动链。保留现有 console scripts、OneBot 依赖和 main 的实际插件加载 |
| `src/qq_ai_bot/gateway/compatibility.py:8,49–53` | 删除 NapCat doctor 路径声明；保留实际使用的 OneBot action 合同和 SnowLuma 声明 |
| `src/qq_ai_bot/social/service.py:28–29,237–244` | 删除每次构造的 `{napcat: NapCatProvider(), snowluma: SnowLumaProvider()}` 品牌分派和未知品牌拒绝。两个实现没有覆写社交动作，直接复用已有 OneBotSocialOperations；原 route、效果 claim 和授权链保持 |
| `src/qq_ai_bot/gateway/providers/snowluma.py:8,25` | N02 直接复用 OneBotSocialOperations 后，删除 SnowLumaProvider 失去消费者的 mixin 继承和 import。GatewayProvider 合同只要求 provider_id/describe_connection；保留 SnowLuma 原 profile/能力实现和共同社交操作，不合并出新抽象 |
| `src/qq_ai_bot/adapters/onebot/provider_adapter.py:159–165` 与相关 `__all__` | 删除零消费者的 provider_id_for_bot，连同仅供它使用的 OneBot Bot import（:12）；不新增替代 getter |
| `src/qq_ai_bot/adapters/onebot/sender.py:76–89` | 删除零消费者的 provider_id 属性及 Registry → adapter → `onebot` 来源兜底。真实来源已经由 ingress 的 Registry snapshot 入账，不制造默认来源 |
| `src/qq_ai_bot/cli.py:63–97,637–638,670–671` | 删除 NapCat 配置渲染函数、render-napcat-config 注册与分发，并删除死 import |
| `src/qq_ai_bot/cli.py:30,652` | 删除 doctor 的 NapCat 选择；能直接使用现存 catalog.provider_ids 时就复用，不另造发现机制 |
| `src/qq_ai_bot/admin/config_specs_protected.py:101–110` | 删除 NapCat WebUI Token 占位规格，不新增另一个品牌占位替代。前端经 list_config_specs 间接消费，此项不需要新页面 |
| `.env.example:8,13–14,24–28,447` | 删除所有 NAPCAT_* 与支持说明；新部署示例明确选择附带的 snowluma profile。这只是部署示例，不是核心来源默认值 |
| `docker-compose.yml:24–25,38,57–80` | 删除 bot 的 NapCat env/mount 和完整 napcat service/profile：镜像、账号、UID/GID、WebUI/token、6099 和专属目录。共享 social-transfer 仍供 SnowLuma 与 bot 使用，保留 |
| `Dockerfile:61`；`scripts/start.sh:4,10–12` | 删除 NapCat 目录创建和启动渲染分支；保留原 bot/SnowLuma 启动流程 |

旧入口退役以实际适配器注册和服务路由证明，不增加按账号、载荷或 User-Agent 猜品牌的拒绝代码，也不扫描环境变量建立退役品牌黑名单。

## 5. N05–N06：安装与部署链不能留暗门

`deployment_setup/service.py:41–42,444–467` 现在维护双品牌名单，并把空、未知或仅自定义 Compose profile 隐式解释成 NapCat。删除该回退，**不改成隐式解释 SnowLuma**；配置没有声明附带网关时就保留未选择状态，自定义 Compose profiles 按现有机制保留。

连同删除后的消费者一起处理：

- `deployment_setup/command.py:547–568` 的 NapCat/SnowLuma/both 选择，及 `current[0]` 必有值的假设；沿现有页面简化为真实的附带部署选择，不发明新 provider 管理层。
- `command.py:184–185` 的无条件 NapCat token 生成，`:839–842` 的 WebUI/凭据提示，`:663,814` 的摘要消费者。ONEBOT token、SnowLuma 密码的既有生成方法仍有用途。
- `service.py:498` 的“NapCat 启用时不能占 6099”分支；前半段现有 SnowLuma 两个宿主端口不能相同的检查仍有真实用途。
- `service.py:93–94,395–409,419` 的 gateway-action.json 属性、写入和备份排除。全仓没有读取、执行或恢复消费者，删除这个无人执行的状态及双品牌切换比较，连同仅供该比较使用的 `old_environment = document.values()`（:372）。`existing_deployment`（:371）仍供 restart-required（:397）使用，保留；EnvironmentDocument.values 本身也仍有消费者。
- `docs/deployment/snowluma.md:48–60,89–90` 与 `docs/help.md:42–50` 宣称安装器自动完成严格切换、停止失败时不启动新 Provider、保留 action 文件供重试，与配置写入实现不符。一起删除自动切换及故障恢复假象，不补造自动切换协调器。
- `command.py:62–64` 的新目录创建；`scripts/build_release_bundle.py:34–36` 的 NapCat 空目录打包。两个入口均停止生成 napcat-data/config/plugins。

旧安装目录需要单独核实：`install.sh` / `install.ps1` 不替换既有 managed files；EnvironmentDocument.merge 也会保留未知字段。**只删模板、再跑安装器，不会自动删除原 Compose 和 .env 的执行支持。** 后续实施在授权的部署目录使用现有配置编辑能力清掉旧 service、mount、NAPCAT_* 和 napcat profile，不给运行时增加迁移 reader。仅退役这一 profile，不把其他扩展 profile 一并清空。

核对实际部署目录后，连同清除旧 `data/setup/gateway-action.json` 无消费者标记；不清空其他 setup 状态或凭据资料。例如旧 `COMPOSE_PROFILES=napcat,external` 一次性清理后保留 `external`，再次运行 setup 不重建 NapCat 支持。

`.gitignore:28–30`、`.dockerignore:7–9` 排除旧 NapCat 资料的规则暂保留，并注明只是遗留私密资料保护。旧目录可能含 QQ 登录 HOME、token、用户配置和插件，删除 ignore 会使它们误入 Git 或镜像。这些排除规则不是安装或运行支持；通用备份和私有 ACL 同样保留。

本次不默认清空旧登录目录、备份或用户插件；需要删除实际存量资料时，另外核对目标、引用与授权。新发行包不能包含这些资料，也不能主动创建 NapCat 目录。

## 6. N07：逆向检查防御与冗余

### 6.1 已有证据，纳入删除

| 位置 | 为什么删，如何收束 |
| --- | --- |
| `web/base.py:11–22,72–76` 与 `services/media_resolver.py:23–34,369–374` | 整份静态 `_BLOCKED_HOSTS` 均被现存单标签或内部后缀判断覆盖。删除整个品牌名单及成员判断，而非只删 napcat 或改列 snowluma；保留现有协议、凭据、私网 IP、后缀及媒体 DNS/IP 校验，不新增规则 |
| `adapters/onebot/sender.py:91–103,147–170` | 删除失败后的同 Presence 自动换 Bot 重试。当前只在 dispatched=False 时执行；真实 bot.send 前已置 True（:111,136），因此它修复不了断连，只会重试空消息、非法 reply、媒体类型等本地错误。send 直接沿原 ingress handle 调用已有 _deliver，不改持久 Work 恢复或原回执 |
| `tests/unit/test_gateway_providers.py:239–248` | 删除读取 registry/models 源码并搜 napcat 的测试。不能改成全仓 NapCat 零关键词门或 SnowLuma 禁词门；验证真实行为 |

自动重选并非 NapCat 私有功能；这项是顺着本次线索查到的无效兜底。`test_canonical_ingress.py:247–264` 仅直接测试私有 `_same_presence_bot()`，不能证明断连时实际恢复，应删除这种实现断言并按实际 send/不重发行为验证。OneBotSendError.dispatched 当前唯一读取者就是待删的重选分支；一并核对并删除失去消费者的参数、字段和 _deliver 局部赋值，以及仅供重选使用的 cast import（sender.py:8,170）。`OneBotSender.bot` 仍供 processor 的原连接/群路由处理使用，保留；Work/Social 自己的持久派发状态和回执也仍有消费者，保持原合同。

### 6.2 尚不能判定可删，不混入确定清单

`gateway/registry.py:80,337–342,362–375` 的 pins/多连接择 pin 看起来冗余，但公开 connect 的同 handle reconnect 分支（:99–116）可在传 presence_id 时直接调用 _bind_locked，绕过另两处冲突核验。不能宣称多候选不可达、机械删除 pin；正常 Adapter.connect + main.bind_presence 不走这条重绑定路径。

这是独立待核缺口，记录可达性和实际恢复消费者后再判断；本次 NapCat 退出不依赖解决它，不顺手新增围栏或替代协调器。终审须明确记录处置结论，不能冒充已修复。

### 6.3 有真实用途，保留

握手前账号占用、同步 Registry 注册/断连、main 在数据库等待后确认原 handle 仍 live，以及路由的 Presence/generation、成员可达性、效果 claim、uncertain 和原发送回执核验，都仍适用于同一 SnowLuma 的重连与并发。删除品牌支持不能成为绕过来源与权限的理由。

媒体 get_image/get_record、Base64/可达 URL、ASR、禁止读取网关容器本地路径和文件 handoff 都有现有消费者。现存 `services/processor.py:1495` → `services/attachment_inputs.py:100–104` → `services/forwarded_inputs.py:48` 的 get_forward_msg 和 `adapters/onebot/normalizer.py` 的 forward/node 投影同样保留。不根据品牌注释删掉共同协议能力；当前未发现 get_file action 消费者，不补造这类兼容代码。

## 7. N08：测试跟着真实合同删改

- `tests/unit/test_gateway_providers.py`：删除 NapCat 专属 profile、handle、能力和 doctor 用例；删除双品牌必须相同、builtin 必须恰好两个，以及当前双项 builtin 省略 provider_id 必失败的旧期待。保留真实 SnowLuma 行为、Catalog 的自定义 Provider、重复/未知注册和连接生命周期验证；单项可省略、多项必须显式指定的现有合同均保留，多 Provider 情况复用该文件已有 `_Provider`。
- `tests/support/gateway.py` 的 napcat_registry：改用现存 builtin/真实 SnowLuma 装配，改名或内联即可；连同 `tests/support/canonical_ingress.py`、`tests/conftest.py` 间接消费者核对。
- `tests/unit/test_canonical_ingress.py`、`test_presence_routing.py`、`test_group_route_recovery.py`：替换 fixture 和显式 napcat provider_id，保留身份、原来源、连接代次、路由 CAS、去重、原回执恢复等真实错误覆盖；多账号不要求两种厂商。
- 删除私有重选方法测试，用现存行为测试证明本地错误不自动换连接、已经派发或结果未知不重发。

不新增源码词、固定文件数、目录布局、调用次数门；不为凑数量保留已退役测试，不因为 fixture 名含 NapCat 就删除整份仍覆盖通用错误的测试文件。

## 8. N09：当前说明与历史证据分开

| 当前文档 | 同步内容 |
| --- | --- |
| `README.md:98,154`、`README.en.md:93,147`、`docs/help.md:39–50,70,264–266` | 删除双品牌要求、自动切换承诺和 NapCat doctor/logs/profile 命令，明确 Yuki 不再支持 NapCat；只列实际支持的附带部署 |
| `docs/deployment/snowluma.md` | 删除旧隐式默认、NapCat/both 安装选择、自动切换假象、双品牌示例和 compose ps 中旧服务；保留 SnowLuma 多账号与旧部署文件不会自动替换的事实 |
| `docs/architecture/canonical-runtime.md:11,94–100` | 删除双正式 Provider 承诺；明确已有抽象继续可替换、当前仅装配 SnowLuma，owner 不随传输实现改变 |
| `docs/architecture/development-contract.md`、`docs/architecture/README.md` | 原删除优先/可扩展性原则保留；核对入口和网关说明，不另写一套抽象规范 |
| `docs/architecture/memory-v2.md:9`、`memory-v2-embedding.md:122` | 品牌例子改成实际的通用 owner/网关关系；记忆不随网关复制，Bot 部署边界保持 |
| `docs/speech/recognition.md:72–74`、`services/media_resolver.py:128` | 删除 NapCat 兼容承诺；保留原 ingress 网关返回的数据与路径限制 |
| `docs/operations/social-workspace-sandbox.md:45` | 删除双厂商支持说法，保持现有社交操作合同 |
| `examples/plugins/com.example.echo/README.md:14`、`plugins/github-monitor/README.md:54` | 去掉现行 NapCat 部署描述，沿原 SDK/Host/网关边界说明 |
| `docs/releases/v3.9.0.md`、`docs/upgrade-3.9.0.md`、`CHANGELOG.md` 未发布部分 | 实施完成后记录退役、旧配置处理、抽象与历史资料保留。当前草案尚无 NapCat 引用，不提前填写已完成或线上通过 |

已发布 3.8.x 及更早 Release、对应旧升级指南、日期验收、源码路径/哈希证据保留当时事实，不把历史 NapCat 部署改成 SnowLuma。不从旧指南恢复已退役入口；当前入口不得把旧版安装指令当最新支持。历史事实和本任务书本身允许出现 NapCat 字样。

## 9. 历史与持久化不需要品牌迁移

`persistence/models.py:263–271` 的 chat_events.ingress_provider 是 nullable String(32)，不是品牌枚举。现有来源链为 Registry snapshot → identity/ingress → canonical_uow → 历史字段；投影也保留该来源事实。

因此不新增 Alembic 迁移，不重写已发布迁移，不把旧 ingress_provider=`napcat` 改成 snowluma，不删除历史事件、Person、Presence、Conversation、Memory、路由或 Work。社交效果回执本来按内部 owner、operation ID、Presence 和平台关联存储，不需要品牌迁移。

旧来源字样不构成继续装配旧 Provider 的理由；读取历史不能变成重发、重跑或重建业务所有权的依据。

## 10. N10–N12：实施顺序与验收

顺序：N01–N04 删除实现及装配 → N05–N06 部署/setup/包 → N07 冗余删除 → N08–N09 测试/文档 → N10 定向验证 → N11 终审 → 获准的 N12 交付。互不依赖的审计/文档可并行，源码所有权保持清楚。

最低验证围绕真实行为，不为本次仅写任务书跑部署或全量测试：

1. 当前 catalog 无 NapCat，启动不注册旧适配器和旧接入入口，包括裸 Adapter 的默认 HTTP/WS `/onebot/v11/`、`/onebot/v11/http[/]`、`/onebot/v11/ws[/]`；SnowLuma 原 WS 路径和现有尾斜线形式仍可按原 token 建立连接。现存自定义 Provider 合同仍可使用；第三方 OneBot 依赖本身保留。
2. canonical ingress、Presence routing/CAS、重连/迟到断连、group route recovery、社交效果/历史、send 回执与媒体相关现有测试通过；来源和派发结果未知不导致默认来源或重发。
3. setup 的空 profile 不伪造网关，不强制内置品牌；保留自定义 profiles。新配置不生成 NAPCAT_*、旧渲染命令、占位密钥或 gateway-action.json。
4. Compose 的实际解析结果无 NapCat service/profile、bot 专属 env/mount、6099；实际新包不创建旧目录、不含旧凭据。旧安装文件和未知 env 保留行为已解释且有对应升级处理。
5. 静态品牌名单删除后，现有 URL/媒体行为仍拒绝内部单标签、内部后缀和私网目标；通用网关媒体与 ASR 不受品牌限制。
6. 运行匹配改动的 Ruff/mypy 与现存适用 CI，构建部署包核内容。当前 quality/release 工作流没有 NapCat 专属矩阵，release_smoke 已无 NapCat 专属验收，故不凭空新增或恢复旧 CI；仅删除实际因退役失效的条目。
7. 终审使用关键词搜索定位，再沿实际消费者复核；不要求全仓 NapCat 零命中。逐项确认剩余命中只属于历史来源/证据、遗留私密资料排除或本任务书，不存在当前支持暗门。

线上验收与本地验证分开记录。后续部署沿当时实际 Compose overlay，只替换获准的 bot；不执行 down、remove-orphans 或 SnowLuma 重启，不发送未经授权的 QQ 测试消息。若真的存在运行中的旧 NapCat 服务，单独核对其实例、账号与停用范围，不把退役扩大为清空 QQ HOME 或重启其他服务。

交付记录分别填写本地修改、验证、提交、推送、PR、合并、镜像/部署和真实线上状态；部分成果按实际状态记录，不把未实施的任务书写成完成上线。

## 11. 二轮漏项核查记录（2026-10-09）

本轮沿非品牌关键词的启动、继承、状态读取和文档消费者继续核查，补充到 N01、N02、N05、N07、N09：

- `[tool.nonebot]` 的裸适配器启动元数据，不随 main 删除 NapCat 注册自动消失；外部工具可以消费它。本轮未启动该入口，也不宣称它能成功处理 Yuki 消息。
- 核心品牌分派删除后，SnowLumaProvider 的社交 mixin 继承失去消费者；删继承，不删共同动作实现。
- 删除 getter/重选后留下的 Bot、cast import，以及删除切换比较后留下的 old_environment 读取，连同清理。
- 帮助文档和部署故障恢复段落也重复宣称安装器执行切换，必须与无人消费的 action 状态一起删除。

外部元数据消费者依据为官方 [nb-cli 项目启动代码](https://github.com/nonebot/nb-cli/blob/00dadf0599e4fe98c971faaf3080bde0e539e70d/nb_cli/handlers/project.py#L48-L104)及[启动模板](https://github.com/nonebot/nb-cli/blob/00dadf0599e4fe98c971faaf3080bde0e539e70d/nb_cli/template/scripts/project/_prepare.py.jinja#L4-L40)；旧默认入口依据为本仓锁定版本的 [OneBot Adapter v2.4.6](https://github.com/nonebot/adapter-onebot/blob/v2.4.6/nonebot/adapters/onebot/v11/adapter.py#L77-L111)。固定源码仅用于解释该孤立声明的作用，不新增 nb-cli 依赖或启动支持。

重新核对安装器、CLI 健康提示、发行包与 CI，未发现任务书之外的新 NapCat 专属执行入口。Registry pins 仍是 §6.2 的未决项；这轮没有把它升级为已证死代码或已修复问题。此记录是计划补全，不是实现验收。

## 12. 实施与交付回执

- 本地实施：2026-10-09，分支 `codex/retire-napcat`，基线 e783175d。send 的单一 `_deliver` 包装同时删除，原连接发送体直接留在 send。
- 静态验证：Ruff 全仓检查通过，匹配修改的格式检查通过；Linux 目标 mypy 649 源文件通过。Windows 默认 mypy 的 30 个 POSIX API 错误来自既有 Linux 专属模块，未添加忽略规则。额外全仓格式检查发现未改的 memory/mutation/service.py 漂移；同一 HEAD 原件同样不通过，没有为本次退役改无关格式。
- 完整 pytest：1088 passed、50 skipped，539.88 秒；跳过项为未构建的可选 Monty 与一个 POSIX FD 用例。direct 镜像 source-free smoke 通过，含迁移 head 与 Bot 重建后数据库/标记持久化；direct 无 binding/worker/launcher。
- 隔离实际握手：网络隔离容器内，原 SnowLuma WS 与尾斜线形式按 token 连接，错误 token 拒绝，关闭后 Registry 断连，旧 HTTP/WS 入口拒绝。未连接 QQ、未发送消息；临时容器已清理。
- 删除统计：实现及部署配置 23 个文件，删除 346 行、增加 40 行，净减少 306 行；不把文档、任务书和行为回归的变化混入该数字。
- 生产起点：ops-31d12022，Bot healthy、restart 0、OneBot connected；磁盘使用 57%。实际 Compose 文件列表已从运行容器 labels 获取，旧 NapCat 专属环境与 mount 来自原 base 和一个旧 bot override，部署时按实际文件清退。
- Registry pins 处置：保留；隔离复现公开 connect 重绑定后同 Presence live_count=2，原 pin 继续择原连接。正常 Adapter/main 装配仍不走此路径；这是独立未决问题，不计为本次已修复，也不以它扩大为新协调器。
- [PR276](https://github.com/YuanYeYouTao/Yuki/pull/276) 于 21:35:19（Asia/Taipei）合并，main `528e2adc73a42cf25af04771ab864718a840886d`；Linux CI 1089 passed/49 skipped，静态检查与前端测试/构建通过。PR 与合并树一致，未重复修改执行源码。
- 最新一致性部署备份通过 quick_check、外键检查，schema0102；65个实际Compose原文件与配置同批保全。生产base与一个frozen override的专属项逐键清退，其余frozen环境/挂载及SnowLuma定义保持。
- 镜像 `ops-528e2adc` 本地构建、压缩、上传，两端 SHA-256 一致；只重建 Bot。21:43:52（Asia/Taipei）核心健康通过、OneBot已连接、restart0，SnowLuma原ID/镜像/StartedAt不变，direct包装验证通过。没有QQ测试发信或自然回复验收声明。
- 清理保留新Yuki镜像与上一版31d12022，以及其它在用服务/有效checkpoint/QQ资料；旧应用备份换为最新一份，释放3,044,302,848字节，磁盘54%。详细回执见[上线报告](../operations/napcat-retirement-20261009.md)。
- 记忆既有标记：`superseded_without_chain_count=162`，部署前备份与部署后线上按相同原谓词均为162，足以使memory_consistency_healthy=false；未声明Memory全健康，不改写事实或伪造后继来压标记，也未重跑模型任务。该旧问题与Registry重绑定另行追踪。
