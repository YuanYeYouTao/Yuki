# Rollup 预算与调度修正交付记录

当前：代码实现、验证、合并及上线完成。以下为 2026-09-14 13:06 至 13:08（Asia/Shanghai）观察快照。

- 独立生成预算默认 16384，Rollup Provider 与总等待超时默认 90 秒；摘要字符限制保持独立。
- maintenance/required 只影响 Rollup。普通聊天不取消已开始的维护请求，全局维护容量为一个；exclusive 仍可取消。
- 前台超限先等待同会话、同 generation 的现有任务提交；必要时自行接纳语义压缩。全部前台批次共享等待期限，失败后才应急抽取。
- 超时先停止本地请求，再等待原 worker 提交释放；最多额外 5 秒，未确认释放则失败关闭。worker 取消也收拢其模型子任务。
- 两种协议实际 HTTP 请求验证 16384 输出预算、最低思考合同和 90 秒专用超时；普通聊天仍使用 Profile 的 30 秒超时。
- 72 项 Rollup、来源覆盖、聊天补触发及新调度定向测试通过；Ruff、Linux Mypy 通过。新增 11 项测试，CI 预算为 921。
- Profile 可显式配置 `max_output_tokens_limit`，请求超限前拒绝；未提供的 Provider 上限保持未知。没有修改主 Agent 的提示词或工具声明。

本次不修改 Self Reflection、Dream、Extraction 的任务策略，不删除历史、不重建 Rollup 数据模型。
## 验证与部署

- [PR #91](https://github.com/YuanYeYouTao/Yuki-QQbot/pull/91) 已合并到 main：`ccf224279715eb90157a529a5dfedb21e3e4dbbe`。
- [最终 CI](https://github.com/YuanYeYouTao/Yuki-QQbot/actions/runs/34807927853) 全通过：921 项测试、19 项迁移矩阵，以及 Docker、类型、格式、插件合同、语音和记忆质量检查。
- 协议补充的 27 项定向测试通过：Chat Completions 的 `length` 不再伪装正常完成；两种协议均可区分截断与 reasoning-only，诊断不保留推理正文。
- 本地完整 Dockerfile 构建成功；服务器使用原依赖镜像加源码更新层构建，619 个源码/迁移文件 SHA-256 校验通过。
- 镜像 `ghcr.io/yuanyeyoutao/yuki-qqbot:rollup-c7bcac8`，源码 `c7bcac86614354edaddc7fb355fb88419928109d` 与合并后的 main 运行代码一致。未另发正式版本或推送 GHCR 标签。
- 恢复点 `/opt/yuki-qqbot/backups/pre-rollup-budget-20260914T050445Z`。旧活动 Rollup 已收尾，中断 claim 数为 0；停 Bot 后保存一致性 Bot/Manager 数据库与配置。
- 数据库仍为 0060，本次无数据迁移。只替换 Bot，QQ 网关、RSS、代理、Manager 进程和持久环境容器均保持原身份。
- 线上实际配置为 `16384 / 90s / 1200 字符 / 32768 批次字符`，全局模型并发保持 4。健康正常、QQ 已连接、Bot 重启计数为 0。
- 主 Agent 工具声明仍为 174 项，冻结指纹 `b6861fe01db3fc9a459f9d714236ebbbfbc0fffa9c638075ccb966ecc52113f4`，没有因本次 Rollup 改动重排主声明。

## 生产观察边界

部署前累计 compaction 记录：成功 145、空响应 74、抢占 159。上线初始窗口内尚无新语义请求，
新进程 success/empty/timeout/preempted、overlay 增量均为 0；过期处理租约和 oldest pending 均为 0。
这些零值不是成功率证明；没有自然样本时，checkpoint 推进速度、completion token 分布、
30/60/90 秒耗时分布及前台延迟变化标记未知。未主动重放事件或制造模型请求来填充样本。
不能用离线测试声称生产饥饿、空响应或前台延迟已经完全消失。

同一观察窗口另有一次 GitHub Monitor 自主轮 `RequestCancelledError`，走现有主请求取消路径；
当时 Rollup 尚未发出模型请求，因此不能归因为本次维护请求抢占。该插件取消路径未在本轮改动，
此记录不宣称它已经修复，也不将其计入 Rollup 成功/失败。
