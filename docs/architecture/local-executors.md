# 本机执行器

本机执行器连接到当前 Yuki 实例，属于创建它的原 Person。服务端保存授权别名、摘要和原操作回执；真实路径与执行器明文凭据由桌面主程序保管。它不运行第二套 Agent，也不持有聊天设备或 operator 凭据。

## HTTP 与凭据

设备 Bearer 使用 `/client/v1/executors` 创建、列出、替换 grants、暂停、恢复、撤销及读取操作记录。创建与修改沿原个人命令回执去重；grants 使用 `expected_revision`。创建回执只有执行器 ID、名称、平台和时间。明文 token 只出现在首次创建响应，服务端只保存 SHA-256；重放创建不会取回 token。响应丢失时先查原 request_id，撤销尚未领用的记录，再以新 request_id 创建。

执行器 Bearer 只使用 `/executor/v1`：`config`、`stream`、`heartbeat`、`operations/{op_id}`、`ack`、`result` 及该操作的 `content`。两类凭据不能交叉使用。撤销或原 Person 停用后，执行器身份验证失败。Person 合并按当前同人族授权，历史操作保持原来源；遗忘清除执行器、授权及操作正文，只留下原操作 ID、摘要与终态事实。

SSE 首帧提供 pending ID 与授权摘要，后续提供 operation/config 提示。连接计数直接表示当前是否在线；心跳记录实际时间，不用超时估计在线。重连恢复已登记的原操作，暂停阻止新调用，原操作仍可完成。

## 工具与回执

固定工具 `local_device(action=list|invoke|get)` 同时提供给平台、App 与 Code Mode。普通聊天使用可信 ToolActor 的 Person，Work 使用原 `source_json.actor_person_id`。没有本人来源时不借用其他人身份。Host operation ID 是唯一 `tool_call_key`；普通聊天不因此建立 Work。

新 invoke 在短写事务确认在线、授权和暂停状态后登记意图，提交后按原 ID 派发。离线返回 `executor_offline`，不登记或排队。工具最多等待二十秒，未完成则返回 pending；后续通过 get 查证。现有 Work 可选择 time_due 等待，未新增调度器或等待种类。

执行器先在本机持久化 started，再确认服务器接纳 ack，之后才执行外部效果。终态只有 succeeded、failed、unknown；同内容重送返回原结果，异内容产生冲突。awaiting_user 是非终态进度。folder 读写的成功等级是 confirmed，open 与 notify 只有 submitted。成功结果须携带对应证据；文件内容的摘要和大小需要一致。

有 Work 的终态与原 pending effect 在同一个 SQLite writer 内收敛，保留 invocation/composition。Host 首次接纳也在自己的实际 writer 内按原调用键重读操作，避免先读 pending、结果提交、再写入旧 pending 的交错。终端 run 与本机操作共用条件回执替换；已有 accepted 回执的普通改写保护不变。迟到结果不会改变 Work 生命周期或累计预算。

`folder.write` 的成果引用必须指向当前实例的不可变版本。执行器仅能下载自己未终态操作所引用的内容，响应携带 X-Yuki-Sha256；不要求成果曾出现在出站聊天中。文件写入后由本机执行器重新读取验证，操作回执保留原 artifact_id/version。

迁移 0146 新增 client_executors、executor_grants、executor_operations。OpenAPI revision 5 提供固定 HTTP 与两类 SSE schema。桌面应固定导出文件、源提交与内容哈希，不能在构建时读取浮动后端工作目录。
