# 2026-10-12 资料与数据库查询优化交付记录

本轮完成[任务书](../architecture/Yuki-资料与数据库查询删减优化任务书-2026-10-12.md)的44项实施与本地核验、16项候选调查；候选包含保持现状和上游未决结论。实验工作量不代表生产延迟或p95。源码、测试、CI、部署和真实QQ分别登记。

## Git与质量

| 阶段 | 实际结果 |
| --- | --- |
| 查询优化 | 66924a81；[PR #298](https://github.com/YuanYeYouTao/Yuki/pull/298)合并为1aa8668d。 |
| 原实现Quality | [PR38169745791](https://github.com/YuanYeYouTao/Yuki/actions/runs/38169745791)、[main38170960821](https://github.com/YuanYeYouTao/Yuki/actions/runs/38170960821)均成功，各1523 passed、63 skipped。Ruff、mypy642文件、前端18项及构建通过；两次不累加。 |
| 本地与支持镜像 | Windows全套1514 passed、69 skipped；终审必要回归另记。SQLite3.46.1真实迁移、DDL/metadata、原完整行及claim/wait验证通过，FTS30组合完整ID/顺序一致。 |
| SELF补修 | 69f756a6；[PR #302](https://github.com/YuanYeYouTao/Yuki/pull/302)合并为2e7458ace3cac4dc28c8893d9714724060a0e61f。 |
| 补修验证 | SELF35项、来源/检查点/付费请求风险组合20项、SQLite3.46.1九个实际WAL/双领取探针通过；不对重叠套件求和。定向Ruff、mypy、diff检查通过。 |
| 补修Quality | [PR38174381545](https://github.com/YuanYeYouTao/Yuki/actions/runs/38174381545)、[main38174423678](https://github.com/YuanYeYouTao/Yuki/actions/runs/38174423678)均completed/success，各1532 passed、63 skipped；pytest分别945.82s/990.08s，不累加。 |

## 首次迁移与实际异常

首轮操作目录：`/opt/yuki-qqbot/ops/query-optimization-1aa8668d-20261011T201611Z`，恢复记录为其中`rollout.json`，一致备份在`backup/qq_ai_bot.db`。从实际Bot labels取得全部Compose files、root和project，只停止和重建Bot。

原镜像`ops-f439a267`更新到`ops-1aa8668d`。停写备份及副本0106→0107演练通过，生产按真实Alembic链升级，增加四索引并删除退役override。副本与实际生产迁移前后计数相同：automations120、versions79、runs154、Work167、social receipts17017、chat events90867；这是迁移当时的保全证据，不要求后续计数不增长。旧人数环境键从有效Compose和.env删除。

备份及副本完整性扫描耗时，停机约14分钟；最后生产只读完整性检查移到启动后完成，未持writer。04:47:05 +08:00实际生产integrity=ok、外键异常0。

首个Bot启动遇到`database is locked`并退出，自动重启一次后恢复健康。原JSON中首次StartedAt为`2026-10-11T20:30:17.268783008Z`，健康回执为自动重启后的`2026-10-11T20:31:32.146726357Z`，不能混写为无重启。04:34:52 +08:00实际health/database=ok、OneBot connected、schema0107。

独立双WAL回归复现原生517：本轮SELF多加的六处显式BEGIN固定读快照，后续写入遇到快照升级竞争。补修删除六处包装，保首次DML前准备及原派生水位事务。普通failed retry旧ORM更新在真实竞争下双领取，改按原run/status条件写，保checkpoint、fingerprint和累计attempt，未接纳即退出。没有添加锁、重试器、吞错、状态或完成门槛；真实Memory事实变更仍核原owner/来源/generation。

## 第二次代码更新

仅镜像变更，没有迁移，没有重复整库备份或全库扫描。全部有效配置除`services.bot.image`外一致，环境与挂载一致。

| 项目 | 实际回执 |
| --- | --- |
| 操作目录 | `/opt/yuki-qqbot/ops/query-code-only-2e7458ac-20261011T210459Z`，实际`rollout.json`保存原78个Compose files与新增Bot-only override。 |
| 镜像 | `ghcr.io/yuanyeyoutao/yuki-qqbot:ops-2e7458ac`，OCI revision为2e7458ace3cac4dc28c8893d9714724060a0e61f，image ID `sha256:93bc57b4b87a7b8edb4850e700e67bb43fc4819662aec143f5bfe2d401f6ca2e`。 |
| 构建/上传 | 本地direct构建，SQLite3.46.1、Code Mode=false、无Monty；两个补修文件镜像hash与源码一致。265359729B归档SHA256 `0d559f8cd7a0e1648ee07f3cbdcaaddcba234c1b65be2bf3b943725cb2057d52`，服务器核对并加载成功。 |
| Bot | `93f18f5fd1b931bff0417a46458c9d9bdf089addb7356d99aa6c6027e0a8d6bf`，StartedAt `2026-10-11T21:05:08.496028593Z`。 |
| 服务核验 | 05:08:07及05:16:04 +08:00：running/healthy，health/database=ok、OneBot connected、RestartCount=0、schema0107；退役override=0、环境键不存在。 |
| SELF观察 | 05:08:55 +08:00：水位96615等于当时最大event，processing run为0；启动以来读取日志未见SELF scan/cycle失败、startup失败或SQLite锁错误。没有新入站时水位不变，不当作新来源处理验收。 |
| SnowLuma | 两轮均未操作；ID `cbdabcdf7c2fff6763d3688b10f606b26c6be06ffce98cfba8bccd2032850d89`与StartedAt `2026-10-09T05:10:47.99332112Z`前后一致。 |
| 真实QQ功能 | 本轮未额外执行响应、等待续跑和工具读取测试，保持待验收。健康/连接不代替功能或自然延迟验收。 |

启动前旧Bot曾一次HTTP health15s超时；资源快照为Bot382MiB/CPU94%、SnowLuma197MiB、服务器available596MiB，没有据此归因内存泄漏。新Bot05:08:55快照285MiB/CPU34%；不同生命周期的单次快照不能证明内存优化量、持续延迟或无泄漏。

OneBot adapter仍为2.4.6；项目删外层250ms取消不等于上游迟到/重复echo竞争已修复，Q03结论保留未决。3.9.0仍为草案，本次没有tag或GitHub Release。PR #303文档合并78e65707及后续状态补录的src/tests/migrations与已部署2e7458ac相同；文档触发的新CI不等待，不冒称已绿。核验后仅删除本轮两份已加载上传归档，共530720184B；镜像、数据库恢复点和操作脚本保留。

## 当前数据保全与回退

第二轮只改代码，代码回退继续保持0107、当前数据挂载和真实回执。其部署前镜像为`ops-1aa8668d`，image ID `sha256:040076ca39c20416e7dd4b872e1b9f8d4a9b793ac40dad4fa465ec12fb030004`。需要回退时显式执行同一helper，之后另核健康：

```sh
python3 /opt/yuki-qqbot/ops/query-code-only-2e7458ac-20261011T210459Z/query-code-only-rollout.py ghcr.io/yuanyeyoutao/yuki-qqbot:ops-1aa8668d 1aa8668d489ec592d04aee82c194f51b888d8c21
```

helper重新按当时Bot labels取得完整Compose清单，追加仅镜像override；不操作SnowLuma，不降级、不重放原执行。它保留第一次查询优化版本的已知SELF问题，只作为代码回退选项。

首轮如需回退到不认识0107的`ops-f439a267`，必须使用首轮operation中的`rollback-query-optimization.py`及首轮`rollout.json`，先备份回退当时的当前库，再由认识0107的新镜像执行真实0107→0106，核完整性后启动旧Bot。0107 downgrade仅删除四索引，不恢复退役配置；不可套用第二轮JSON，也不直接用旧备份覆盖新消息、文件、授权、预算和效果。

## 代码统计

原实施a4c6e652→66924a81：源码64文件+2876/-1794；测试24文件+2494/-63；迁移1文件+37/-0；docs五文件+485/-8，根README+3/-3，配置示例删1行；全提交96文件+5895/-1869。

最终部署a4c6e652→2e7458ac：源码64文件 **+2889/-1802**，净增1087行；测试24文件 **+2790/-63**；迁移1文件 **+37/-0**。本回执提交前工作树相对a4c6e652：docs六文件+566/-8、根README+3/-3，合计文档七文件+569/-11；配置示例删1行，全树97文件+6285/-1877。没有新增服务、表、长期缓存、任务状态、经验阈值、人数开关或重试器。不把测试或文档算作源码删除。
