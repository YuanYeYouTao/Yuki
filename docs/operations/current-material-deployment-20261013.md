# 本轮资料与共同Main：2026-10-13上线记录

## 最终公共历史补修与0113上线

当前生产产品为 `ec56863185b01197bb587918f39122cb605815ff`、数据库 `0113`，direct 构建、72项固定Main工具。公共聊天只冻结实际采用的原事件；本轮资料、首次当前Work事实在触发正文上方，用双换行分隔，真实触发正文在最后。原私有已付请求、签名、工具配对及恢复journal不改序。

[PR316](https://github.com/YuanYeYouTao/Yuki/pull/316)删除按说话人、读取范围及入口分叉的公共历史，清退公共笔记/观察摘要链与个人时区，并修正新Host资料的文字边界。[PR317](https://github.com/YuanYeYouTao/Yuki/pull/317)删除新会话时误删原私有观察的旧trigger及离线重置的旧终态门槛，同步守旧测试；冻结0113仅卸载旧trigger，没有增加兼容或替代机制。

精确ec568631的[Quality](https://github.com/YuanYeYouTao/Yuki/actions/runs/38362008940)成功：Ruff、634份源码mypy、前端测试及构建通过；pytest **1610 passed、63 skipped、1440 warnings，1010.93秒**。保留前一版97210435的8项失败及其修正记录，不用旧d75的绿CI替代新版本验收。

| 实际阶段（UTC） | 结果 |
| --- | --- |
| 07:35:05.220 最终镜像load | ec568631完整tar三端SHA相同，安装634份源码、68份迁移逐字匹配；97210435候选仅load，未启动 |
| 07:42:10 当前冷备 | 只停Bot；保存实际0111数据库/WAL/SHM、6个数据/配置根、.env及当时实际81份Compose |
| 07:58:20.241819 迁移审计完成 | 实际安装CLI升级0111→0112→0113；16私有表199295条原行及142旧Version逐值、类型与hash保持 |
| 08:01:04.672941991 首次新版启动 | 仅Bot.image变化；原SnowLuma、代理、环境及egress容器保持原ID、镜像、启动时间和重启次数 |
| 08:08:44.548259 全会话新历史审计 | 只停新版Bot，沿原离线重置入口完成18会话、取消1个非终态旧Work；26个受保护表及142旧Version保持，FK0 |
| 08:11:17.387604799 重置后启动 | 启动同一新版容器，没有再次换镜像或重建其它服务 |
| 08:27:53.105850 健康复查 | healthy、restart0、OOM false；HTTP/DB ok、OneBot true、72项direct工具；active Work/wait均0 |
| 09:02:25.868810 收尾复查 | 同一ec568631容器、0113，healthy/OneBot true、restart0、OOM false；active Work/wait均0 |

实际镜像为 `ghcr.io/yuanyeyoutao/yuki-qqbot:ops-ec568631`，Engine/manifest为 `sha256:c8992f14f3058d733c056b1fc44cbc4595bb9749bbd526b76a8b15249ce31951`，OCI config为 `sha256:c546b16f2617a63db7f338cf31714342001829d602807ce2c9557be6e24f1135`。最终容器ID为 `49f94d546b4def7e385ab121972d29e2140b42679cad624482b008030077d82d`。Main manifest revision为 `5a8609142b70cd37a5f3aca51475e5120b5a1866b7ed0c5a684d1f7b08d7842a`，语义参与依赖仍为 `63522288e9829fc71dce955e041d5ae49f919c5a`。

完整tar为267490816字节，三端SHA256为 `64e250809cae91f484aef8f0969b2ad7ca00b1c69a55eeefcd4cf7ca1eafeed9`。构建151.427809秒，限640MiB、0.5 CPU、单并行，OOM事件0；未使用本机Docker。冷备为 `/opt/yuki-qqbot/ops/common-public-history-ec568631-20261013/backup-current-20261013T074210Z`，原DB SHA256为 `3705a3b936f43678073b0fa2f27dc0eb9fcbd825e92e4166f255b17e5cd7349a`。完整文件/integrity校验造成该次停写较长，没有将其写成模型或网关卡死。

迁移时原11条观察及来源DAG、19份产物、125条安排保留；10545个引用协议文件和19个产物文件完整性通过。旧公共两表清空、4个分叉列与2张个人时区表退出，旧reset trigger不存在。0112的SHA256仍为 `06ccc07de6b8afbb7b4a1f6348d2448fc5a13bbc2b001822493e6def16703374`；0113为 `9992c9a84b17ae539e40cc0da3bcf6c2f5d549ff025978068201db025169e82f`。

全会话重置批次为 `full-ai-new-ec568631-20261013T0805Z`。18个Conversation各推进一次generation与真实内部事件floor，释放旧租约/owner，取消旧输入并清空Rollup及公共投影；不删除原93332条聊天、2572条Memory事实、证据、身份、原Work journal/effects及私有协议文件。原11条观察及142旧Version的hash保持；旧终态Work记录仍是已发生事实。迁移报告SHA256为 `07f6cf31c1fe7998e9b7c89c99845d5d41db8625f50cd5858d9932f4b07620a2`，重置审计为 `997b5c50e86fb2401b1337dfa5821f926ccf1c3d778d576b881fb360a9ec1306`。

相对最初实施基线ff0dbdf重新做两端Git diff，产品源码126文件新增3044、删除7493，净减4449行；测试50文件新增5998、删除6125，工具5文件新增1306、删除1566。Markdown优先归文档，tests目录（含插件tests）归测试；src/plugins/frontend/src、迁移和其余插件Python示例归产品。中间PR差量不再相加。随后收尾文档不算产品代码。

本次已合并316/317；按用户最新指示，313/314/315保留已有只读结论，暂停审核并保持未合并。新自然窗与实际重放的数据见[0113实测](current-material-cache-0113-20261013.md)及[公共历史核验](public-history-freeze-audit-20261013.md)，不以健康接口代替请求顺序、缓存或真实QQ行为验收。原敏感报文及操作报告仅保存在受控外部目录。

## 首批d75/0111上线的历史记录

Yuki 产品提交为 `d75a89463c491ad7d3eba4b074c2cbd3b8992050`，数据库 `0111`，direct 构建、74项固定Main工具，语义参与依赖固定 `63522288e9829fc71dce955e041d5ae49f919c5a`。普通运行资料只用于当前激活，公共历史只保实际原消息和有来源的笔记；定时、SELF和聊天复用共同Main。对应[任务书](../architecture/Yuki-本轮资料仅当前注入与主入口一致性任务书-2026-10-13.md)逐项记录实现与证据。

[PR308](https://github.com/YuanYeYouTao/Yuki/pull/308)、原生品牌续接修补[PR309](https://github.com/YuanYeYouTao/Yuki/pull/309)及实际安装迁移入口修补[PR310](https://github.com/YuanYeYouTao/Yuki/pull/310)已合并。精确d75的[Quality](https://github.com/YuanYeYouTao/Yuki/actions/runs/38328535997)全绿：1595 passed、63 skipped，1424 warnings，pytest 608.62秒；Ruff、633份源码mypy、前端18项和构建通过。

| 实际阶段（UTC） | 结果 |
| --- | --- |
| 00:24:18 停Bot后冷备 | 原0108数据库、私有引用文件、配置、sidecar、.env、插件、工作区及原80份Compose保存 |
| 首轮安装CLI迁移 | 0109普通包导入在首次SQL前失败，完整事务回滚；0108完整schema同冷备，integrity ok、FK0 |
| PR310最小修补 | 用Alembic官方文件加载器读取冻结0108；无路径兜底、重试或兼容名单 |
| 00:56:29 实际新镜像load | d75完整tar三端SHA相同，Engine/Descriptor/BuildKit manifest一致；未启动候选2f/8a |
| 实际生产0111迁移 | 16表196752条私有行及86旧Version逐值/类型/hash保持，10183引用协议文件/34产物完整，integrity ok、FK0 |
| 01:04:51.662179732 新Bot启动 | 容器`78b19c8017927ad193b588c68455781da7ce43f46145b64f53b42f83d5345b45`，仅Bot.image变化 |
| 01:06:01 / 01:06:14 | application startup完成 / OneBot连接；冷启动尚未监听时的短暂reset/unhealthy恢复 |
| 02:51:11复查 | 同一容器healthy、restart0、OOM false，/healthz ok、OneBot true、direct |

最终镜像 `ghcr.io/yuanyeyoutao/yuki-qqbot:ops-d75a8946`，实际Engine/manifest为 `sha256:87d001a4809ef8cafd538b0093c4cd858f1957e346a2a37ed0e643e81dfc62fe`；OCI config digest为 `sha256:365de22caa338122c3841dfcf91517aadbd2bbeac915f45ebb387f09ba363d9c`。615份安装Python和66份迁移逐字匹配精确提交。完整tar为267529216字节，构建端、本机和目标SHA均为 `232c593d6f2308b1ed9221f3053e2025a03ee4a682cb642b19dd665427df2cdb`。传输较慢，使用已验证旧成员复原新完整tar并核全量SHA后load；gzip原件完整保留在构建端，未回填三端gzip传输为完成。

冷备位于 `/opt/yuki-qqbot/ops/current-material-2f66b459-20261013/backup-pre0111-20261013T002418Z`，规范化0108数据库SHA为 `0e7d36a6b1556ffcfe25c5eb3bc2ff4653f51605dbff709d239e98e01e7397b2`。迁移前后196838条原私有行/Version的hash JSONL相同，SHA为 `3e1a8599dc1fa61adaa919c4f261a1e2b705b63eced4c302bf298174a2fed40e`。120条公共投影全空，compiled-material选取与公共automatic根为0；旧visible_material列和autonomy_source_claims表删除，11条受原私有执行依赖的automatic观察原件保留。启动后的合法新事实单列，不要求运行中的私有表永远不变。

八条实际Gemini路由保持各自旧e30的Profile revision；两份Profile仅provider改为agm，删死automation_agent路由，原endpoint/model/key引用/wire/search逐字段保持。sidecar和.env与冷备逐字相同。原SnowLuma、Mihomo、environment及sandbox-egress的ID、镜像、启动时间和重启次数全保持；没有发送人工QQ测试、伪造入站或重跑旧Work。

完整冷数据校验完成后，root读回实际报告并立即启动，中间无DML。临时ops删除了启动前重复full audit，保原Bot ID/镜像/peer核验；实际SHA为 `1935747470805c1012fca9e014e47d3aaadeb01edf28ff1b2b99603b656d4cd1`，原脚本和首次失败日志另存。模型/QQ验收不以健康接口代替。

[真实缓存实验](current-material-cache-experiment-20261013.md)包含DeepSeek12次及Gemini27次HTTP200、覆盖、输入、名义费和负收益。产品源码相对ff0dbdf最终新增2352行、删除5919行，净减3567；全部文本新增9550、删除13649，测试/脚本/文档分列在任务书9.12。后续收尾文档不算产品源码。

配套网关发布、客户端完整usage、原数据迁移及新Host自然请求报告只在[网关fork](https://github.com/YuanYeYouTao/Antigravity-Manager/tree/beta/docs/operations)维护，本仓不保存其源码、镜像或专用运维资产。原聊天、私有协议、凭证及未红删诊断保留在受控外部证据目录，公开文档只有计数、hash、时间和真实验证范围。

自然请求验证：首个新Host辅助Memory请求已HTTP200且完成原批次，完整客户端usage与网关一致；其原身份字段为空，不作为Main业务会话头通过。Main的真实会话编号、完整usage和自然QQ结果另行补记，不能将辅助请求或离线Mock扩写成这项验收完成。
