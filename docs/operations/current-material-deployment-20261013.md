# 本轮资料与共同Main：2026-10-13上线记录

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
