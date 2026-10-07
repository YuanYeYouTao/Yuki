# 删除重构生产执行记录（2026-10-08）

本记录区分源码、镜像、迁移、上线和短时观察。所有时间为 UTC；北京时间 / 台北时间加 8 小时。没有创建正式 Release 或 tag。

## 源码与镜像

- 源码 `f4f483d70c3fdb0302b2bece9f6220971ef3f955`，[CI 37696648000](https://github.com/YuanYeYouTao/Yuki/actions/runs/37696648000) 五项 job 成功，主测试 4800 passed、239 skipped。
- [PR #265](https://github.com/YuanYeYouTao/Yuki/pull/265) 于 2026-10-07 22:59:06 合并；merge `dd819fe28c6e3c4a5fc9b70531db0f3328942680` 与 f4 树完全相同。
- 服务器实际加载的不可变镜像 ID：`sha256:82ed61b72b0231fd43f933f79467deef5778ae7efa08324789d48e15897478bb`；OCI revision 为上述 f4。Docker archive config ID 为 `sha256:7208b68e877f5ecd6d9cf5c5a5a806b6d09082615935f2c6102d85a8022362de`，两者不混用。
- 镜像压缩包 265799752 bytes，SHA-256 `b837d60127e8c413949f608e4fb9356ae4ee71da6d9caa4bc87ba3dfaf87659b`。默认构建目标 direct；服务器精确镜像包装门通过，Monty binding / worker / launcher 均不存在，Code 默认 false。
- 完整 source-free smoke 在 d768 执行通过；f4 与其全部 12 个 RootFS layers 相同，仅 revision 标签不同，f4 自身另过包装门。没有声称在 f4 重复完整 smoke。

## 冷备与迁移

2026-10-07 23:00:22 停止 Bot 和 Manager。冷备期间暂停既有 `yuki-environment`，完成后原 ID / StartedAt 恢复运行；未操作 SnowLuma。归档的 11 个持久根均通过写入者检查，61 个实际 Compose 文件取自容器标签。备份目录：`/opt/yuki-qqbot/backups/deletion-final-20261007T230005Z`。

| 文件 | bytes | SHA-256 |
| --- | ---: | --- |
| qq_ai_bot.db | 1247981568 | `4a1622d26cda431a9c6a69f26509b70f1720e3a180b52bbfacffa3f98ea00406` |
| state.tar.gz | 2039043470 | `20cf12a26750c10595e1405298dd7e760295e186b8d7bf4124bc83242fdc7e2c` |
| compose.tar.gz | 6735 | `bdfba500d0d90ee1792424201e255b81cab6f2879ac39454cb7e6776e4a49c32` |

冷备 DB 为 0096、quick_check ok、外键违反 0；chat_events 85124、memory_facts 2461、runtime_work 740、runtime_work_journal 350、runtime_work_effects 812、runtime_work_budgets 719、tool_artifacts 23。Manager 冻结快照记录 628 个原任务 ID / 终态、2 个目录下 7189 个普通文件和 0 个 symlink。

精确 direct 镜像在单个有界容器执行真实 `alembic upgrade head`，0096 → 0099，退出 0、未 OOM；未重跑迁移。保留表校验耗时 569.49 秒：按预定退役项排除规则比较（runtime_config_overrides 仅扣除预定废弃配置项），135 张保留表其余原列的有序行哈希及行数全部一致，新增 projection metadata 两列可空且历史值全 NULL；升级前后 quick_check 均 ok、外键违反均 0。

该校验程序完成全部行比较后，在最后导入 schema_guard 时因服务器原 Manager venv 没有 Alembic 失败。保留原失败证据；没有安装依赖或将失败报告改成成功。随后在精确发行镜像、只读 DB 挂载下执行真实 require_canonical_schema，通过且退出 0、未 OOM。数据库本体大小 / mtime 不变，冷备字节哈希仍与上表相同；只读 SQLite 连接新建的两个 WAL 均 0 bytes，没有提交帧。独立 final-db-completion.json 绑定原行校验报告 SHA-256 `5533b530b7ef86759702648d01a8f5be2854d949b452e90fcb2169cf1cc25793` 与补验镜像。

## 启动时配置兼容处理

精确更新四个既有文件：删除废弃 automation_text_generation 路由、删除 speech 两项 operator capability、两个启用插件的 manifest API 从 3.1 更新至 3.3；保留其他路由、凭据和插件源码。

首次启动发现 operator 文件还含已退役的 `control.workspace.mutate`，被现行能力合同拒绝，导致启动失败和一次自动重启；没有 OOM。停止 Bot 后仅删除该旧权限，并在精确发行镜像中验证完整 ControlOperatorAccess 模型通过。权限文件 SHA-256 由 `3b23358c5dc494feb0ae39ddf87a5c0cfc1e1d2b8df30f7627483d9c0db81bfd` 变为 `b14406a12a62c0201af7640cabffbaba5956888cccb696f26e0a333b9b7f005d`。没有授予替代权限或放宽验证器；原插件恢复计划的其余字段保持不变。

Manager 仅原子替换 src，保留原 venv、systemd unit、jobs、home、workspace 和 environment 容器；原源码保存在服务器。修正后 Bot 于 2026-10-07 23:29:04 再次启动。健康、持久保留和自然流量验收见后续记录。

## 上线验收

- Bot / DB 健康均 ok，OneBot 已连接；主合同 frozen、mode=direct，tool_count=model_tool_count=75，persistent_environment_tools_complete=true，restart_required=false。修正后的 StartedAt 为 23:29:04，最后健康采样时自动重启数 0。
- GitHub Monitor 与 Kun Game 均重新运行；仅恢复原有 9 / 6 项精确权限，未增加 tool.register。旧的禁用插件没有启用。所有管理请求先保存原 request_id，再查询原回执，不盲目重放。
- Manager active，684 个安装源码文件逐 hash 与发行 manifest 相同；原 systemd unit、venv、持久挂载与安全设置保留。Bot 实际主进程 UID 10001，20 个废弃环境变量不再注入，10 个保留挂载与旧容器一致。
- SnowLuma 原 ID `4e4595ad107c93f48e4ba1aac2d30dd8477dc47958d5db4caabaab0dd507eae0`、StartedAt `2026-10-06T19:57:38.688743853Z` 未变化并保持运行；persistent environment 原 ID / StartedAt 也未变化且未暂停。
- 628 个 Manager 原任务 ID 及终态全部保留；7188 个原文件逐字节相同。剩余工作区 manifest 原文件字节变化，初版严格 hash 校验如实失败。提取冷备中的原文件（SHA-256 `4a17dd3705a9f960b75b88fbcf5ba5d5ec70b93d047137ece555e8f10250c488`）独立比较后证实：197 个 artifacts、167 个 artifact_snapshots、3 个 short_state 的全部原行原字段不变，唯一结构变化为 artifact_snapshots 新增 source_key TEXT nullable 且全部 NULL，expires_at 没有变化。
- 修订的只读验收只允许上述确切 manifest 结构变化，其他文件仍严格 hash；9 项校验器 SQLite 回归覆盖原字段变化 / 原行缺失拒绝。最终 production-online-acceptance-v2.json passed=true，未覆盖初版失败记录。
- 当前进程 / maps 快照未观察到 Monty；“进程从未载入”的结论不由快照证明，无原生组件的包装门另有证据。

## 内存与业务观察边界

同一只读采样器记录 Bot 进程树 RSS / PSS / Swap，容器 cgroup 包含文件缓存，不能与进程 RSS 混用。以下均为 KiB：

| UTC | 状态 / 容器运行时长 | RSS | PSS | Swap | 主机 MemAvailable |
| --- | --- | ---: | ---: | ---: | ---: |
| 22:59:36 | 旧版 healthy，约 27 小时 | 457444 | 457422 | 289752 | 528112 |
| 23:26:47 | 首次启动中，约 46 秒；随后因旧权限失败 | 148184 | 148162 | 0 | 870616 |
| 23:31:00 | direct healthy，约 117 秒 | 273288 | 273266 | 0 | 709068 |
| 23:33:47 | direct healthy，约 283 秒 | 264228 | 264206 | 45356 | 718604 |
| 23:36:43 | direct healthy，约 459 秒 | 241276 | 241254 | 66440 | 741748 |

23:33:47 的 cgroup memory.current=362598400 bytes、peak=492572672 bytes、swap.current=113664000 bytes；主机 loadavg=2.63/2.09/1.42。23:36:43 的 cgroup memory.current=387977216 bytes、peak=492572672 bytes、swap.current=132624384 bytes，loadavg=0.86/1.56/1.33；进程树 RSS 约 236 MiB、Swap 约 65 MiB。RSS 下降伴随交换增长，不将其记为内存优化收益。本窗口还进行了保留文件哈希和冷备归档读取，不能称稳态同负载性能实验。新进程树 RSS 约 258 MiB 是短时现场事实，不能与旧热进程计算节省百分比，也不能证明长期无泄漏。可复现取消集合增长及修复的独立证据见 [内存报告](deletion-memory-20261008.md)。Code 仍必须显式选择可选镜像与开关，并另做容量验收。

截至 23:36:44，冷备后新增 trace 已出现自然 self_initiative 的模型 / provider 记录及 1 个 turn_end，但新增 chat_events 和带新增 delivered event 的 Social 回执仍为 0。没有发送额外 QQ 测试消息；不把健康或背景回合称为真人 QQ 对话收发验收。

启动后仍出现 participation_scope_failed category=CanonicalIdentityError。部署前 21:43 的私有生产日志已有同类 scope / hydration 告警，21:18 / 21:26 的恢复日志也有记录，因此不是本次新版本才出现的证据；不能将全局健康解释为每个群的语义参与均正常。该既有问题与长期内存、真人收发保持明确观察边界，不以此次任务索引勾选替代。 进一步用 mode=ro / query_only 的纯 SQLite 探针读取 4 个候选 scope：其中 1 个 disabled space 仍保留 semantic/legacy owner，可确定性触发 snapshot 的 canonical_owner_disabled 校验；另 3 个未命中该禁用条件。按源码该异常按 scope 隔离，不能由此宣称其他 scope 已通过完整业务验收；日志仅有异常类，也不能将全部告警逐条归到该候选。未启用禁用空间、改写身份或放宽校验。

## 证据位置

生产私有执行目录 `/opt/yuki-qqbot/deletion-release-20261008/ops/rollout-f4f483d7` 保留冷备绑定、迁移、原失败行验证、schema 补验、启动配置纠正、原请求回执；本地主会话 `.cache` 保存对应镜像、CI、最终 online-acceptance-v2、健康、资源和自然流量汇总。配置和凭据未提交仓库。源码完成、CI 成功、合并和生产 direct 上线均已分别确认；真人自然收发与长期稳态无泄漏没有冒充已验收。
