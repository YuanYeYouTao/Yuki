# Rollup 预算与调度修正交付记录

当前：代码实现与本地定向验证完成，尚未部署。

- 独立生成预算默认 16384，Rollup Provider 与总等待超时默认 90 秒；摘要字符限制保持独立。
- maintenance/required 只影响 Rollup。普通聊天不取消已开始的维护请求，全局维护容量为一个；exclusive 仍可取消。
- 前台超限先等待同会话、同 generation 的现有任务提交；必要时自行接纳语义压缩。全部前台批次共享等待期限，失败后才应急抽取。
- 超时先停止本地请求，再等待原 worker 提交释放；最多额外 5 秒，未确认释放则失败关闭。worker 取消也收拢其模型子任务。
- 两种协议实际 HTTP 请求验证 16384 输出预算、最低思考合同和 90 秒专用超时；普通聊天仍使用 Profile 的 30 秒超时。
- 72 项 Rollup、来源覆盖、聊天补触发及新调度定向测试通过；Ruff、Linux Mypy 通过。新增 11 项测试，CI 预算为 921。
- Profile 可显式配置 `max_output_tokens_limit`，请求超限前拒绝；未提供的 Provider 上限保持未知。没有修改主 Agent 的提示词或工具声明。

本次不修改 Self Reflection、Dream、Extraction 的任务策略，不删除历史、不重建 Rollup 数据模型。
最终上线镜像、恢复点、健康及自然 Rollup 样本在部署后补录；不能用离线测试声称前台延迟或生产成功率已改善。
