# SQLite 实现后交叉复审 R3：原 A 域

固定产品基线 `1fb34b85031dc3a7df1f8f5c7e7f0cdf60fc6966`。本轮由原 A 实现、R1 Memory、R2 C reviewer轮转回原 A 240 路径。
性能窗口先纯读；不调用真实模型、不碰生产、不改性能脚本。原 A 路径的实际 DB 方法与间接 owner 入口已完成独立复审；纯算法/类型/协议仅作明确结构排除。追加 services 域的实际边界补审已完成（交叉 owner 的删除 delta 另由 B 收口），不把 AST 解析当语义审完。

## 逐路径状态

F=源全文；D=全部真实DB方法及局部调用边界；I=间接owner编排；S=当前imports/调用树明确排除；X=本轮其他owner审其实现，A消费者仍核。

| 路径 | 行数 | 等级 | 本轮范围 |
| --- | ---: | --- | --- |
| `src/yuki/acp_adapter/__init__.py` | 6 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/acp_adapter/agent.py` | 394 | I | 完整调用树及_session140–157/_submit159–169/new_session171–185/_finish288–338/prompt340–373/cancel375–394；PersonalClient独立Outbox原UUID+receipt恢复，不用超时/静默判任务完成，无主库writer |
| `src/yuki/acp_adapter/cli.py` | 51 | I | 当前全部imports/逐方法调用树核现有API/Outbox/owner委托，无自有DB transaction；实际owner边界已分项审，纯命令参数/目录渲染不冒称全文 |
| `src/yuki/adapters/__init__.py` | 1 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/adapters/onebot/__init__.py` | 1 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/adapters/onebot/activity.py` | 21 | S | 当前全部imports/逐方法调用树核为平台网络/文件传输/registry内存连接，无自有数据库或repo调用；不宣称纯协议算法全文 |
| `src/yuki/adapters/onebot/card_parser.py` | 122 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/adapters/onebot/media.py` | 77 | S | 当前全部imports/逐方法调用树核为平台网络/文件传输/registry内存连接，无自有数据库或repo调用；不宣称纯协议算法全文 |
| `src/yuki/adapters/onebot/message_id.py` | 9 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/adapters/onebot/normalizer.py` | 472 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/adapters/onebot/provider_adapter.py` | 152 | S | 当前全部imports/逐方法调用树核为平台网络/文件传输/registry内存连接，无自有数据库或repo调用；不宣称纯协议算法全文 |
| `src/yuki/adapters/onebot/sender.py` | 168 | S | 当前全部imports/逐方法调用树核为平台网络/文件传输/registry内存连接，无自有数据库或repo调用；不宣称纯协议算法全文 |
| `src/yuki/adapters/onebot/social.py` | 132 | S | 当前全部imports/逐方法调用树核为平台网络/文件传输/registry内存连接，无自有数据库或repo调用；不宣称纯协议算法全文 |
| `src/yuki/admin/__init__.py` | 23 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/audit.py` | 242 | D | add_audit_event108–144、transaction154–158/record160–204/history206–232/get234–242完整；供给session同writer；redacted摘要和实际生成ID的小回执，不移出原原子单位 |
| `src/yuki/admin/authority/__init__.py` | 1 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/authority/capabilities.py` | 110 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/authority/command_types.py` | 341 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/authority/commands.py` | 85 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/authority/decision.py` | 114 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/authority/json_types.py` | 50 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/authority/operations.py` | 118 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/authority/operators.py` | 114 | D | authenticate80–114完整；凭证hash在reader前，独立operator与Person存在性分别处理，无新身份门槛 |
| `src/yuki/admin/authority/principal.py` | 119 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/authority/problems.py` | 62 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/authority/targets.py` | 61 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/authority/tokens.py` | 38 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/config_files.py` | 482 | I | 当前全函数imports/calls核只有文件bundle/pool/Web owner；无SQLite IO。由已全文审ModelOperations.external_command在DB外执行，不宣称纯文件变换全文 |
| `src/yuki/admin/config_owners.py` | 16 | F | 16行全文；原Space存在性小投影，复用supplied session |
| `src/yuki/admin/config_registry.py` | 160 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/config_service.py` | 1966 | D | Repository184–496、Service597–1659/1781–1807/1847–1933全部DB方法；legacy rollback1038–1461原审计payload+lineage完整准备/phantom SQL复核在首DML前；配置小值+同笔audit保原子，不外包成第二个owner |
| `src/yuki/admin/config_spec_helpers.py` | 77 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/config_specs_asr.py` | 77 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/config_specs_future.py` | 45 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/config_specs_hot.py` | 525 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/config_specs_planner_plugins.py` | 172 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/config_specs_protected.py` | 122 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/config_specs_restart.py` | 512 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/config_specs_social.py` | 7 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/config_specs_tooling.py` | 64 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/control_resolution.py` | 270 | D | 所有Principal/target/native binding DB方法99–195/248–270；读当前原Person/root资格，不把账号角色当稳定授权 |
| `src/yuki/admin/models.py` | 296 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/operations/__init__.py` | 39 | I | 当前逐函数全调用树核装配/进程内owner或能力绑定，无自有DB事务；实际caller/资格按领域核 |
| `src/yuki/admin/operations/base.py` | 346 | D | 全部authorize/prepare/replay/database_command/external_command46–346完整；savepoint/domain/audit/receipt同writer；external reserve→效果→settle分离，实际原receipt胜过ACK异常 |
| `src/yuki/admin/operations/config.py` | 194 | D | revision/rollback_revision与_change完整；原rollback reader准备后同Control writer，只有已回滚的准备变化重备，不重做external |
| `src/yuki/admin/operations/groups.py` | 35 | F | 35行全文；set_enabled同CommandExecutor writer+原revision/receipt |
| `src/yuki/admin/operations/identity.py` | 573 | D | 全部DB身份操作61–573完整；forget/merge reader准备→current proof→原receipt同提交；原账号阶段与route binder不捕获旧session；后提交Registry绑定不改原receipt |
| `src/yuki/admin/operations/models.py` | 56 | I | apply_models27–56完整；原external_command先reserve，文件/模型池激活在DB writer外，再按原执行ID settle |
| `src/yuki/admin/operations/plugins.py` | 170 | D | plugin_action/configure/retry_outbox全部；R2 external typed ACK保原phase，supplied纯业务转换不增加多余异常分支 |
| `src/yuki/admin/operations/work.py` | 40 | F | 40行全文；manage_work供给原命令writer，不新开owner |
| `src/yuki/admin/permission_catalog.py` | 781 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin/queries.py` | 442 | D | person/binding/plugin/operation/traces/persons/bindings/presences/work 全部实际DB查询72–442；纯读、不隐式flush/commit，operation按原receipt投影 |
| `src/yuki/admin_api/__init__.py` | 1 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/admin_api/cli.py` | 342 | I | 当前全部imports/逐方法调用树核现有API/Outbox/owner委托，无自有DB transaction；实际owner边界已分项审，纯命令参数/目录渲染不冒称全文 |
| `src/yuki/admin_api/client.py` | 145 | I | 当前全部imports/逐方法调用树核现有API/Outbox/owner委托，无自有DB transaction；实际owner边界已分项审，纯命令参数/目录渲染不冒称全文 |
| `src/yuki/admin_api/http.py` | 646 | I | 全部嵌套路由/exception handler调用树核现有Commands/Queries/SOUL/Identity owner委托，无自有数据库事务；同请求ID/expectedrevision与unknown_write_response保留 |
| `src/yuki/automation/__init__.py` | 6 | X | 本轮核心D/Automation owner独立审；这里只核A实际消费者，不声称本轮已全文此实现 |
| `src/yuki/automation/authority.py` | 61 | X | 本轮核心D/Automation owner独立审；这里只核A实际消费者，不声称本轮已全文此实现 |
| `src/yuki/automation/context.py` | 72 | X | 本轮核心D/Automation owner独立审；这里只核A实际消费者，不声称本轮已全文此实现 |
| `src/yuki/automation/control_context.py` | 152 | X | 本轮核心D/Automation owner独立审；这里只核A实际消费者，不声称本轮已全文此实现 |
| `src/yuki/automation/creation_key.py` | 14 | X | 本轮核心D/Automation owner独立审；这里只核A实际消费者，不声称本轮已全文此实现 |
| `src/yuki/automation/gateway.py` | 12 | X | 本轮核心D/Automation owner独立审；这里只核A实际消费者，不声称本轮已全文此实现 |
| `src/yuki/automation/models.py` | 152 | X | 本轮核心D/Automation owner独立审；这里只核A实际消费者，不声称本轮已全文此实现 |
| `src/yuki/automation/repository.py` | 1289 | X | 本轮核心D/Automation owner独立审；这里只核A实际消费者，不声称本轮已全文此实现 |
| `src/yuki/automation/service.py` | 790 | X | 本轮核心D/Automation owner独立审；这里只核A实际消费者，不声称本轮已全文此实现 |
| `src/yuki/automation/task_spec.py` | 62 | X | 本轮核心D/Automation owner独立审；这里只核A实际消费者，不声称本轮已全文此实现 |
| `src/yuki/automation/tools.py` | 484 | X | 本轮核心D/Automation owner独立审；这里只核A实际消费者，不声称本轮已全文此实现 |
| `src/yuki/automation/worker.py` | 334 | X | 本轮核心D/Automation owner独立审；这里只核A实际消费者，不声称本轮已全文此实现 |
| `src/yuki/capabilities/__init__.py` | 65 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/capabilities/binding.py` | 43 | I | 当前全部imports/逐方法调用树核现有API/Outbox/owner委托，无自有DB transaction；实际owner边界已分项审，纯命令参数/目录渲染不冒称全文 |
| `src/yuki/capabilities/catalog.py` | 124 | I | 当前全部imports/逐方法调用树核现有API/Outbox/owner委托，无自有DB transaction；实际owner边界已分项审，纯命令参数/目录渲染不冒称全文 |
| `src/yuki/capabilities/coordinator.py` | 235 | I | execute_batch52–211真实InvocationService/结果capture链完整；原执行identity/工具预算/目录固定声明保留，无自有DB owner |
| `src/yuki/capabilities/exposure.py` | 5 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/capabilities/invocation.py` | 173 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/capabilities/media.py` | 30 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/capabilities/models.py` | 143 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/capabilities/policy.py` | 62 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/capabilities/provider.py` | 327 | I | 完整调用树及_bound276–321；仅绑定实际execute和original context，不改提交声明/权限，DB由实际owner负责 |
| `src/yuki/capabilities/results.py` | 694 | X | R3 B补实际三态消费窄复核，root C总工具certainty；A绑定/Coordinator消费者已独立核，不并发改共同结果实现 |
| `src/yuki/capabilities/runtime.py` | 119 | I | 当前逐函数全调用树核装配/进程内owner或能力绑定，无自有DB事务；实际caller/资格按领域核 |
| `src/yuki/capabilities/validation.py` | 151 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/character/__init__.py` | 1 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/character/db_models.py` | 38 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/character/repository.py` | 265 | D | 全部DB方法36–265完整；原profile/owner/receipt及当前设备+Person族，源停用不随归并复活；热名字设置在writer后 |
| `src/yuki/character/soul.py` | 354 | D | document/reserve/finish/save/activate/reload90–355完整；SOUL锁→reserve提交→文件/热装配→finish，失ACK沿原版本/UUID恢复，不拿主库锁等文件 |
| `src/yuki/client_api/__init__.py` | 1 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/client_api/db_models.py` | 40 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/client_api/errors.py` | 10 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/client_api/files.py` | 143 | D | attachments/_authorized32–130完整；原内部event/artifact/version与真实Social/Work可读权限，读快照不新写授权 |
| `src/yuki/client_api/http.py` | 324 | I | 全部嵌套路由调用树核owner委托；唯一直接reader cancel_prompt174–185完整，退出reader再cancel；错误映射由已审http_errors原三态 |
| `src/yuki/client_api/http_errors.py` | 73 | F | 全文80；actual phase且未确认回滚先投影unknown，typed业务码不推翻确认提交；普通credential失败保原语义 |
| `src/yuki/client_api/identity.py` | 804 | D | 全部实际DB方法115–804及各pure fingerprint consumer；设备/code时钟在writer后取、原owner/Person族/UUID回执，merge两阶段并复用supplied writer |
| `src/yuki/client_api/messages.py` | 151 | F | 全文151；原payload reader replay与App canonical入口；真实commit phase冲突先核原receipt，找不到则传播原unknown，不盲重调Main |
| `src/yuki/client_api/models.py` | 111 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/client_api/openapi.py` | 30 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/client_api/prompts.py` | 301 | D | 全部associated_work/finish/projection/cancel56–301；finish原JSON reader准备，取消join在writer外，实际Work树/revision与原receipt同writer |
| `src/yuki/client_api/requests.py` | 265 | D | personal_command/message/get29–265完整；原UUID跨设备命名空间/来源/Work与confirmed send，原live owner退出后新reader收确认发送，不复执行 |
| `src/yuki/client_api/sessions.py` | 115 | F | 全文115；原设备+ACP Binding/Presence/Conversation和原UUID命令同事务；普通load只reader |
| `src/yuki/client_api/version.py` | 3 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/client_api/views.py` | 331 | D | 全部DB方法31–286与stream289–331；home/events/sync显式BEGIN，SSE等待时无session，原commit hint出口通知 |
| `src/yuki/client_api/works.py` | 652 | D | 全部真实DB/owner方法54–626及public_many164–351批量投影；submit根commit后短borrowed lease清理不覆盖原input receipt；详见发现与29项回归 |
| `src/yuki/client_sdk/__init__.py` | 6 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/client_sdk/cli.py` | 196 | I | 当前全部imports/逐方法调用树核现有API/Outbox/owner委托，无自有DB transaction；实际owner边界已分项审，纯命令参数/目录渲染不冒称全文 |
| `src/yuki/client_sdk/client.py` | 435 | I | _bind86–117/query/resolve195–233/_verify235–281/submit283–346/refresh348–405/watch407–435完整；独立Outbox短事务前后均不持DB等网络；transport unknown保原submitted UUID查询原receipt |
| `src/yuki/client_sdk/outbox.py` | 122 | F | 全文122独立sqlite owner；原UUID+path/body与server/person/binding/session绑定，正文在首DML前编码，无网络事务 |
| `src/yuki/conversation/__init__.py` | 1 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/conversation/autonomy_binding.py` | 162 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/conversation/autonomy_db_models.py` | 110 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/conversation/autonomy_repository.py` | 754 | D | ensure/get/transition/accept_host_proposal115–474、全部GET476–619、record_feedback621–696+prepare699–754完整。feedback原source/sequence/replay同repo writer核，不恢复R2删除的重复outer proof |
| `src/yuki/conversation/canonical_db_models.py` | 722 | D | 全部installer/_install_parent_route_guards_if_ready/assignment_roles及event注册和canonical_schema SQL链已读；纯ORM字段结构排除 |
| `src/yuki/conversation/canonical_schema.py` | 176 | F | 当前SQL常量/生成及引用链全文；canonical assignment角色/原owner、projection source/reset/privacy/retire chunks原触发边界保留，无新应用层写者 |
| `src/yuki/conversation/correlation.py` | 115 | F | 全文115；原内部trigger_event_id、Conversation/Presence点核，不用platform id重建owner |
| `src/yuki/conversation/event_entry.py` | 29 | F | 全文29；物理投递入口以原内部event证明，原Binding/Presence不由platform号反建 |
| `src/yuki/conversation/features.py` | 131 | I | admission_features43–78完整；只调用ledger.list_recent_activity的reader，小当前活动投影无writer；metrics81–127亦读，无数据库副作用 |
| `src/yuki/conversation/frozen_fragments.py` | 359 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/conversation/hydrate.py` | 434 | D | 全部DB方法43–396；原owner入口和不可变primary alias，savepoint winner回读，generation取消Work同session；无外部效果 |
| `src/yuki/conversation/initiative_sources.py` | 58 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/conversation/media_index.py` | 135 | D | index_event_media97–135与prepare字段完整；新实际event ID的元数据/原upload回执同writer，reply snapshot正文已preencode |
| `src/yuki/conversation/media_service.py` | 1186 | D | 所有自有DB方法146–162/407–522/661–781/988–1170及media锁边界完整；下载/hash/rename/unlink在线外，GC原引用/24h源版本与stat token，无并行writer文件工作 |
| `src/yuki/conversation/observation_models.py` | 75 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/conversation/observation_schema.py` | 131 | S | 完整AST/调用树为静态历史DDL/纯SQL生成；无自有session/execute。当前运行installer实际取chunks→compact的链另全文核，不冒称全部历史SQL等价性验收 |
| `src/yuki/conversation/observations.py` | 134 | F | 全文134；真实observation DAG/父成员/版本/原privacy资格；actualcaller只有reader source准备，不在writer追历史DAG |
| `src/yuki/conversation/ordinary_admission.py` | 509 | F | 全文509；R2纯SQL EXISTS与f8七步原谓词逐项同义，existing原admission优先；source字段/segments/generation/floor/keeper/identity一笔查询，controller current回调与input publisher仍同session |
| `src/yuki/conversation/ordinary_admission_db_models.py` | 36 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/conversation/participation.py` | 318 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/conversation/projection_chunks_schema.py` | 80 | F | 当前SQL常量/生成及引用链全文；canonical assignment角色/原owner、projection source/reset/privacy/retire chunks原触发边界保留，无新应用层写者 |
| `src/yuki/conversation/projection_compact_schema.py` | 209 | F | 当前SQL常量/生成及引用链全文；canonical assignment角色/原owner、projection source/reset/privacy/retire chunks原触发边界保留，无新应用层写者 |
| `src/yuki/conversation/projection_current_schema.py` | 268 | S | 完整AST/调用树为静态历史DDL/纯SQL生成；无自有session/execute。当前运行installer实际取chunks→compact的链另全文核，不冒称全部历史SQL等价性验收 |
| `src/yuki/conversation/projection_history_schema.py` | 220 | S | 完整AST/调用树为静态历史DDL/纯SQL生成；无自有session/execute。当前运行installer实际取chunks→compact的链另全文核，不冒称全部历史SQL等价性验收 |
| `src/yuki/conversation/projection_material_schema.py` | 268 | S | 完整AST/调用树为静态历史DDL/纯SQL生成；无自有session/execute。当前运行installer实际取chunks→compact的链另全文核，不冒称全部历史SQL等价性验收 |
| `src/yuki/conversation/projection_models.py` | 82 | D | install_projection_triggers75–82与Base.metadata注册→chunks SQL链已读；纯ORM字段结构排除 |
| `src/yuki/conversation/projection_revision_schema.py` | 85 | S | 完整AST/调用树为静态历史DDL/纯SQL生成；无自有session/execute。当前运行installer实际取chunks→compact的链另全文核，不冒称全部历史SQL等价性验收 |
| `src/yuki/conversation/projection_schema.py` | 58 | S | 完整AST/调用树为静态历史DDL/纯SQL生成；无自有session/execute。当前运行installer实际取chunks→compact的链另全文核，不冒称全部历史SQL等价性验收 |
| `src/yuki/conversation/projections.py` | 690 | D | read/read_row/invalidation/checkpoint154–242、prepare_commit244–579完整嵌套publication、selected581–626/invalidate628–647；历史/body先prepare，source/member/absence/原CAS/配额同writer，原新增selection冲突rollback |
| `src/yuki/conversation/prompt_history.py` | 317 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/conversation/scope.py` | 118 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/conversation/scope_models.py` | 50 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/conversation/scope_repository.py` | 300 | D | 所有reader get/generation/load/prepare90–300与原proactive metadata；显式BEGIN完整源视图，cache只复用同stamp，原Source/selection独立保真 |
| `src/yuki/conversation/self_initiative.py` | 59 | F | 全文59；实际run原scene与当前Conversation/generation/Space/Presence资格，supplied reader不新开owner |
| `src/yuki/conversation/uploads.py` | 149 | F | 全文149；原owner/replay读→流文件hash→media锁→短writer receipt，unknown不删最终可引用文件 |
| `src/yuki/deployment_setup/__init__.py` | 5 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/deployment_setup/command.py` | 934 | I | 当前全部imports/逐方法调用树核现有API/Outbox/owner委托，无自有DB transaction；实际owner边界已分项审，纯命令参数/目录渲染不冒称全文 |
| `src/yuki/deployment_setup/environment_check.py` | 40 | I | 当前全部imports/逐方法调用树核现有API/Outbox/owner委托，无自有DB transaction；实际owner边界已分项审，纯命令参数/目录渲染不冒称全文 |
| `src/yuki/deployment_setup/service.py` | 970 | D | apply_pending_plugins592–645/_bootstrap648–707/load_plugin_setup_states710–751完整；现有OS owner锁：在线走API，离线临时Database后必close；纯配置/文件函数结构排除 |
| `src/yuki/deployment_setup/terminal.py` | 168 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/emoji/__init__.py` | 1 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/emoji/delivery.py` | 412 | D | source84–165/prepare_admission/admit/project/replay/validate_claim167–281/worker283–314/_deliver347–412完整；当前route/source窄投影同writer，重启读原intent/原Social receipt，未知ACK不盲发 |
| `src/yuki/emoji/library.py` | 342 | D | 唯一实际DB existing_snapshot200–208+publish197–234完整；独立workspace reader退出后才Manager RPC；其余index/模型路径全调用树排除主库writer |
| `src/yuki/emoji/media.py` | 151 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/emoji/models.py` | 27 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/emoji/sticker_index.py` | 543 | D | 全部真实sqlite方法valid_schema/cache/ready/_cache_batch/rebuild/discard_cache/_search_ready及catalog/update/annotate调用链完整；hash/元数据准备在cache BEGIN前，独立flock协调；原文件不当缓存删 |
| `src/yuki/gateway/__init__.py` | 23 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/gateway/activity.py` | 11 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/gateway/compatibility.py` | 66 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/gateway/local_clients.py` | 7 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/gateway/media.py` | 30 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/gateway/models.py` | 44 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/gateway/provider.py` | 145 | I | 当前逐函数全调用树核装配/进程内owner或能力绑定，无自有DB事务；实际caller/资格按领域核 |
| `src/yuki/gateway/providers/__init__.py` | 36 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/gateway/providers/app/__init__.py` | 45 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/gateway/providers/app/ingress.py` | 262 | F | 262行全文；start/receive纯读、Processor提交ACK后发布；原请求task/UUID独立于HTTP取消，on_finished单独结算 |
| `src/yuki/gateway/providers/app/social.py` | 27 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/gateway/providers/feishu.py` | 279 | S | 当前全部imports/逐方法调用树核为平台网络/文件传输/registry内存连接，无自有数据库或repo调用；不宣称纯协议算法全文 |
| `src/yuki/gateway/providers/feishu_ingress.py` | 406 | D | start300–310/receive333–406全部DB方法；getMe/member probe锁外，建group短写，ACK以Processor root commit为准；normalizer未冒称全文 |
| `src/yuki/gateway/providers/feishu_protocol.py` | 55 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/gateway/providers/feishu_social.py` | 198 | S | 当前全部imports/逐方法调用树核为平台网络/文件传输/registry内存连接，无自有数据库或repo调用；不宣称纯协议算法全文 |
| `src/yuki/gateway/providers/feishu_ws.py` | 231 | S | 当前全部imports/逐方法调用树核为平台网络/文件传输/registry内存连接，无自有数据库或repo调用；不宣称纯协议算法全文 |
| `src/yuki/gateway/providers/snowluma.py` | 63 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/gateway/providers/telegram.py` | 183 | S | 当前全部imports/逐方法调用树核为平台网络/文件传输/registry内存连接，无自有数据库或repo调用；不宣称纯协议算法全文 |
| `src/yuki/gateway/providers/telegram_activity.py` | 28 | S | 当前全部imports/逐方法调用树核为平台网络/文件传输/registry内存连接，无自有数据库或repo调用；不宣称纯协议算法全文 |
| `src/yuki/gateway/providers/telegram_ingress.py` | 505 | D | start338–351/receive396–505全部DB方法；平台getMe/member probe在writer外，迁群/建group短写，on_committed之后ACK；normalizer纯算法按调用树排除 |
| `src/yuki/gateway/providers/telegram_social.py` | 126 | S | 当前全部imports/逐方法调用树核为平台网络/文件传输/registry内存连接，无自有数据库或repo调用；不宣称纯协议算法全文 |
| `src/yuki/gateway/registry.py` | 449 | I | 当前逐函数全调用树核装配/进程内owner或能力绑定，无自有DB事务；实际caller/资格按领域核 |
| `src/yuki/gateway/social.py` | 126 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/identity/__init__.py` | 1 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/identity/authority.py` | 26 | F | 全文26；原Person和root启用后取当前超管，supplied session不新开owner |
| `src/yuki/identity/bootstrap.py` | 28 | F | 28行全文；仅启动allowlist具名短写，无网络 |
| `src/yuki/identity/canonical_repository.py` | 395 | D | 所有DB方法71–376完整；Account/Assignment原阶段替换、savepoint、原私聊/别名删除和人物族，内存test hook不计全文 |
| `src/yuki/identity/canonical_uow.py` | 837 | D | append_inbound381–614、全部prepare/bind/replay632–794完整；原receipt优先，current accounts/person closure首DML前bind，原件单行native/body兼容保留，非历史scan |
| `src/yuki/identity/db_models.py` | 347 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/identity/errors.py` | 11 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/identity/event_author.py` | 117 | F | 全文117；Account/原Assignment批量投影，原人/root启用；历史ended阶段保原事实，不猜当前人 |
| `src/yuki/identity/evidence.py` | 31 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/identity/ingress.py` | 792 | D | pre_admit147–200/_admit_local202–241/_admit243–585及reply/source/hydrate588–792全部实际DB方法；热reader显式BEGIN，冷原资格重核，外部probe在writer外 |
| `src/yuki/identity/memory_guard.py` | 45 | F | 45行全文；live job水位与永久证据可读性分开，未扩成历史gate |
| `src/yuki/identity/mentions.py` | 131 | F | 全文131；按内部Account/Assignment原时段与同平台当前资料准备mention，不发网络，不从平台号复建已有event |
| `src/yuki/identity/ownership.py` | 56 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/identity/ownership_interval.py` | 139 | F | 全文BindingOwnership和实际历史归属44–139；当前/ended段原时间证明，未知gap不借今日owner |
| `src/yuki/identity/person_identity.py` | 526 | D | 当前root/family全部SQL、rename113–149及prepare197–370/merge373–526完整；原族、配置实际成员和losing原payload/alias absence重核，enabled不被归并复活 |
| `src/yuki/identity/person_state.py` | 726 | D | 两个production preparer63–346/357–427闭包及全部profile/group/set方法452–726；writer自有ORM单join核原Assignment三字段/root/Space/Binding/Membership，同session apply，无old observe入口 |
| `src/yuki/identity/readers.py` | 43 | F | 全文43；Person/Space只读policy，所有当前授权由实际caller核，不开writer |
| `src/yuki/identity/receipt_compat.py` | 204 | D | transport/load/require_claimed及全部native/body兼容和reply DB方法74–204；原唯一receipt/keeper/author/时间/正文不可借今天资料替换 |
| `src/yuki/identity/routing.py` | 1605 | D | 全部真实DB方法141–1522；候选reader/成员probe退出reader后，CAS writer只核实际owner/route/connection。prepare_person_merge_routes闭包接writer，不保旧reader；原pins/代际/授权语义保留 |
| `src/yuki/identity/space_metadata.py` | 63 | F | 全文63；空/相同只读返回，writer固定原Presence/SpaceBinding资格重查再更新；无平台network |
| `src/yuki/identity/sql_constraints.py` | 23 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/identity/write_settings.py` | 51 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/persistence/__init__.py` | 1 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/persistence/artifact_refs.py` | 29 | F | 全文32固定SQL；原segments immutable artifact/version引用触发器与原event同提交 |
| `src/yuki/persistence/command_receipts.py` | 827 | D | 全部DB方法record_personal/recover/reserve/unknown/load/replay/result/success/failure228–827完整；实际原audit/operation/request关系及原receipt，不产生第二套事实 |
| `src/yuki/persistence/commit_hints.py` | 27 | X | 核心D本轮owner独立核；A只核消费/锁序，不声称重复全文 |
| `src/yuki/persistence/database.py` | 417 | X | 本轮核心D/Automation owner独立审；这里只核A实际消费者，不声称本轮已全文此实现 |
| `src/yuki/persistence/diagnostic_writer.py` | 227 | X | 本轮核心D/Automation owner独立审；这里只核A实际消费者，不声称本轮已全文此实现 |
| `src/yuki/persistence/event_repository.py` | 986 | D | 全部真实DB方法51–986完整；history read取消interrupt等待不删，query/history_around复用原BEGIN快照，所有派生写委托scoped writer |
| `src/yuki/persistence/event_view.py` | 66 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/persistence/history_search.py` | 174 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/persistence/history_search_index.py` | 97 | F | 97行全文；runtime replace使用原writer+预分词；离线rebuild256条读取/分词退出原读事务后再短IMMEDIATE，不按在线owner处理 |
| `src/yuki/persistence/instance_lock.py` | 94 | X | 核心D本轮owner独立核；A只核消费/锁序，不声称重复全文 |
| `src/yuki/persistence/job_claims.py` | 0 | X | C Memory R3删除泛型公共入口并合回memory/job_claims；本轮原240清单保删除项，当前239个live文件；见C报告 |
| `src/yuki/persistence/media_repository.py` | 243 | D | 全部cache/read/save/associate/expiry30–171及codec；原body序列化writer前，source_event不被cache刷新换主，expiry候选reader/当前原键writer重核 |
| `src/yuki/persistence/metadata.py` | 37 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/persistence/models.py` | 1844 | D | 唯一_install_artifact_references321–323/after_create→artifact_refs SQL链已读；1844行纯ORM字段结构排除，不冒称全文 |
| `src/yuki/persistence/people_repository.py` | 1841 | D | 全部真实DB方法和完整privacy prepare/apply/SQL selection证明214–1808；R3修人物族全部原Binding隐私门闭包，清退仍原原子单位和真实ownership intervals；完整成员/phantom/原JSON核，不靠计数hash |
| `src/yuki/persistence/repositories.py` | 33 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/persistence/repository_helpers.py` | 147 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/persistence/repository_records.py` | 183 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/persistence/schema_guard.py` | 345 | X | R3 B全文公共实现与实际驱动回归；A reader/worker/source消费者仍独立核，不并发改共同owner |
| `src/yuki/persistence/scoped_event_uow.py` | 1184 | D | append213–315/update324–469/derived489–592/causal与duplicate helpers632–837/append838–1184；supplied准备必给，原唯一receipt兼容和新event/FTS/水位同笔，派生历史reader准备 |
| `src/yuki/persistence/sqlite_connection.py` | 35 | X | R3 B全文公共实现与实际驱动回归；A reader/worker/source消费者仍独立核，不并发改共同owner |
| `src/yuki/persistence/sqlite_diagnostics.py` | 112 | X | 本轮核心D/Automation owner独立审；这里只核A实际消费者，不声称本轮已全文此实现 |
| `src/yuki/persistence/unit_of_work.py` | 56 | X | 本轮核心D/Automation owner独立审；这里只核A实际消费者，不声称本轮已全文此实现 |
| `src/yuki/persistence/web_repository.py` | 50 | F | 全文50；旧Web索引retention只候选reader/原expiry writer，非新检索事实源 |
| `src/yuki/persistence/worker_recovery.py` | 28 | X | R3 B全文公共实现与实际驱动回归；A reader/worker/source消费者仍独立核，不并发改共同owner |
| `src/yuki/web/__init__.py` | 19 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/web/base.py` | 95 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/web/bridge_state.py` | 63 | F | 全文63独立可丢cache；JSON在BEGIN前，读取body在连接关闭后，过期/128原cache上界不作为业务门槛 |
| `src/yuki/web/http_transport.py` | 304 | S | 当前全部imports/逐方法调用树核为平台网络/文件传输/registry内存连接，无自有数据库或repo调用；不宣称纯协议算法全文 |
| `src/yuki/web/models.py` | 64 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/web/native_bridge.py` | 337 | D | 实际BridgeState.access cache77–98、Work paid reserve/validate→provider100–250与diagnostic record252–311完整；付费请求不在writer，诊断失败只本地日志不重发已付请求 |
| `src/yuki/web/native_sources.py` | 89 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/web/page_extractor.py` | 117 | I | 完整调用树_extract/extract只HTTP与独立BridgeState.access；hash/HTML/网络在独立cache事务外，无主库session；BridgeState实际owner已全文审 |
| `src/yuki/web/route_context.py` | 22 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/web/search_config.py` | 54 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/web/tavily.py` | 270 | S | 当前全部imports/逐方法调用树核为平台网络/文件传输/registry内存连接，无自有数据库或repo调用；不宣称纯协议算法全文 |
| `src/yuki/workspace/__init__.py` | 1 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/workspace/files.py` | 170 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |
| `src/yuki/workspace/inspect.py` | 139 | D | validate_artifact132–139及其metadata闭包完整；独立store只读，to_thread外置，像素/模型路径无主库writer |
| `src/yuki/workspace/service.py` | 244 | I | 全方法调用树及validate_images160–195/copy197–218/publish220–244；Manager RPC在主库事务外，原request/source_key/版本不变；store实际owner另审 |
| `src/yuki/workspace/short_state.py` | 109 | F | 109行全文；snapshot read投影expiry不写，真实update才同独立store writer建表/清过期/更新原slot |
| `src/yuki/workspace/store.py` | 284 | D | 全部manifest事务/read/snapshot/cleanup38–329；文件流/hash/fsync在线外，原manifest发布小rename fence保留；失ACK不删已发布blob，GC核当前原引用 |
| `src/yuki/workspace/tools.py` | 65 | S | 当前逐函数imports/全调用树核无自有数据库IO：纯类型/转换/配置/内存协调，未声称纯算法全文 |

## 本轮确认并修复的问题

1. 人物合并后，`PeopleRepository.affected_conversation_scopes` 已拿到完整 Person family，却只查询传入 Person 自己的 Binding。Processor 的隐私命令按该范围停止旧 turn 并取得会话 effect gate，因而可能漏掉族内合并来源账号的 legacy raw mention。改为与 prepare/apply 相同的 family Binding 集合，继续使用原 platform 与 ownership interval 判定；不扩大为同平台号码所有时期，也不新加 gate。删除唯一已无调用的 `bindings_for_person` helper/export。新增真实 source→target merge+旧 QQ raw mention 回归，原前任 owner 负例保留。

2. `ClientWorks.submit` 的业务 input/event/UUID receipt 根提交成功后，`finally` 中独立释放短借 lease 的写失败会覆盖已确认业务结果。现在失败路径保原异常（cleanup 失败附注，取消继续传播），已确认结果路径局部记录 lease cleanup 错误并返回原 receipt。没有增加 retry/runtime/持久旗标；同 UUID 继续查询原 receipt，未确认业务不返回成功。新增真实 release DML 后、根 commit 前注入 OSError rollback 的 success/failure/cancelled 三种回归：成功的原 input/receipt 各一笔，原业务失败和取消均零笔，取消对象不被清理异常替换。

## 挑战后明确保留的边界

- Canonical/scoped append 对唯一原 receipt 单行的 native/body/JSON 兼容核验仍保在 supplied writer；它是原幂等冲突事实，不是历史扫描。热重复已有 reader 快路，不为偶发 race 新建 DTO/mode。
- 普通入站 5→1 SQL 合并继续核原 Person/root、assignment/account、presence、binding/space、原 event/generation/floor/keeper；相同 supplied session 下写接纳与 Work publication。不恢复旧 observe/串行 helper 或添加 snapshot 门槛。
- Person forget/merge 是罕见整次原子单位。原成员/配置完整集合与 absence/phantom 校验保留；正文、分词和原 JSON 准备在 reader。
- history 的 BEGIN 快照和取消 interrupt 等待保留；它们保证读一致性和 driver 生命周期，并非旧 writer admission 诊断包装。
- schema 的实际 installer、当前 SQL 链与静态历史模板分级记录；未把本轮源码阅读写成全迁移建库等价性运行验收。

## 本轮验证

- `tests/unit/test_binding_ownership_privacy.py tests/unit/test_client_works.py`：29 passed，20.20s；一个既有 ORM FK 排序 warning。第一次运行只有新增取消用例多要求一条非必要附注而失败；原取消对象、真实 cleanup rollback 和零业务事实均正确，删除该附注断言后两文件完整重跑通过。
- 三个产品文件 `client_api/works.py`、`persistence/people_repository.py`、`identity/canonical_repository.py` mypy 通过；五个修改源码/测试 Ruff 通过。
- 原 A 240 清单保留：一个 `persistence/job_claims.py` 由 Memory owner 删除，现239个 live path；公共核心/Automation 交叉 owner 的 X 与纯结构 S 均单列，不以数量冒充审查。
- 完成修改后已重新阅读 People family closure、删除 helper 的实际剩余引用与 ClientWorks 成功/异常/取消路径；目前无已确认未处理的 A 域 DB 缺口。后续 services 补审结论另列。

## 跨 owner services 追加复核

按 root 的后续分工，额外核原 C 清单的全部 82 个 services 路径；其中 Admin/commands 与 plugin_sessions/subagent_execution/work_resume 由 R3 B 另作独立补审。本表保真实分工和等级，不把本轮原 A 冒称原 C 全域。纯协议、图像、模板与内存算法只明确排除数据库 I/O。

| 路径 | 当前行数 | 等级 | 本轮真实边界 |
| --- | ---: | --- | --- |
| `src/yuki/services/__init__.py` | 29 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/admin/__init__.py` | 13 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/admin/config_admin.py` | 108 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/admin/control_auth.py` | 73 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/admin/group_admin.py` | 0 | X | R3 B全文后确认无src/tests消费者，已删GroupAdminService与DI/导出全链；SpaceAdminContext就近移到GroupRecovery，未留兼容别名；173项跨域联测由B报告记录 |
| `src/yuki/services/admin/group_recovery.py` | 379 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/admin/memory_admin.py` | 1205 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/admin/preference_admin.py` | 131 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/agent_runner.py` | 1110 | D | _run_with_receipts308–388/_resume_tool_response474–578/_code_host633–715/_execute_control_call717–755/_execute_tool_batch757–1061完整：先保存原response/operation，再dispatch；原paired/unknown返回receipt，未派发原intent才可继续；预算不重置 |
| `src/yuki/services/agent_tools.py` | 2289 | D | execute778–1056三态catch与实际reader、_database_failure1058–1067、_person_memory_selection1431–1485、_memory_change1712–1855/_can_read1900–1912/_authorized1938–1967完整；全部方法调用树核Memory query/mutation/Social/Manager独立owner委托；大错误文案/结果渲染不称全文 |
| `src/yuki/services/automation_commands.py` | 121 | F | 121行全文；明确委托AutomationService当前owner及原ID，不借外层session；人类错误提示不授予机器confirmed rollback/replay |
| `src/yuki/services/autonomous_groups.py` | 326 | I | 完整await/calls链：runtime snapshot→admission features reader→原legacy_allowed/accept_legacy；无自有session/writer，quiet窗口内存slot不当执行receipt；纯local scoring排除 |
| `src/yuki/services/chat.py` | 1688 | D | 全部直接reader及stage/ready569–660、respond687–934、receipt936–986、validator1153–1221/run1232–1323、origin1342–1586/outbound1599–1668实际边界完整；context/paid/Network均不借主库writer。其它builder全调用树核owner委托 |
| `src/yuki/services/chat_preparation_timings.py` | 110 | I | 全调用树核emit只trace recorder委托，无自有session；诊断owner由root核，记录不作为业务gate |
| `src/yuki/services/client_link.py` | 124 | D | _require_proof31–51/execute53–124完整；reader准备People merge，原request_id保持一次生成，writer核真实连接/账户再claim；仅原preparation_changed且confirmed rollback重备 |
| `src/yuki/services/command_service.py` | 231 | I | execute143–231完整；原Person/receipt/forget/Work cancellation/automation/plugin委托，不自开DB writer；前端解析与帮助渲染结构排除 |
| `src/yuki/services/commands/__init__.py` | 1 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/commands/admin.py` | 154 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/commands/conversation.py` | 117 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/commands/group.py` | 14 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/commands/help.py` | 153 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/commands/identity.py` | 216 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/commands/memory.py` | 564 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/commands/models.py` | 46 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/commands/plugins.py` | 81 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/commands/work.py` | 49 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/concurrency.py` | 95 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/config_commands.py` | 286 | I | config47–207所有查询与mutation分支完整；原event→CommandExecutor original request/revision，prepare rollback由领域owner负责；纯帮助/parse/渲染排除 |
| `src/yuki/services/context_assembler.py` | 501 | D | 所有实际history reader委托/validation112–138/211–362/397–453完整；原触发event ID/entry/代际不以native ID重建；render准备在reader退出后的普通编排 |
| `src/yuki/services/context_boundary.py` | 55 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/context_compaction.py` | 652 | D | CompactReceipt.read85–122/write124–172完整、current/public publication175–238及计划249–310/497–652；payload在writer前encode，派发/response/paired保持原execution owner，paid原结果不因cache失败自动重买 |
| `src/yuki/services/deduplication.py` | 14 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/document_reader.py` | 214 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/durable_invocations.py` | 299 | F | 全部真实方法29–299完整及其固定source/contract；独立read之后activate，原bound plugin Work拒绝另建，settle后从持久receipt投影原outcome |
| `src/yuki/services/effect_gate.py` | 65 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/evidence_observation.py` | 92 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/evidence_state.py` | 39 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/execution_sources.py` | 599 | D | 全部真实DB/owner恢复199–599完整；按原Work request/原run与entry/account/person/权限事实重新解析，reader退出后才执行，不用新native号重建ownership |
| `src/yuki/services/group_recovery.py` | 131 | I | enable70–115/hint117–131与registry当前来源边界完整；真正DB准备/原receipt/成员network由B复核AdminGroupRecoveryService，纯命令识别排除 |
| `src/yuki/services/history_projection.py` | 221 | D | prepare_commit36–68/prepare_history71–221完整；真实源/选中摘要/完整missing history reader集合，编排不持writer；publication闭包只原prepared SQL |
| `src/yuki/services/history_query.py` | 73 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/http_logging.py` | 40 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/image_preprocessor.py` | 212 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/invocation_context.py` | 54 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/invocation_service.py` | 328 | D | invoke76–328完整；意图/派发计费/业务效果/accepted原receipt顺序，unknown只回原journal；typed已完成结果不因呈现失败丢失，后置audit仅在effect confirmed后非阻塞记录 |
| `src/yuki/services/main_agent_backend.py` | 765 | D | execute_call246–532完整，phase存在使用公共真实三态结果，不按effect猜读工具写入；旧declared catalog固定，执行处当前授权保留；其余方法完整calls核，不称纯目录/渲染全文 |
| `src/yuki/services/main_agent_contract.py` | 153 | I | 全调用树核definitions只runtime snapshot；工具目录固定投影/contract不写主库或修改已提交声明 |
| `src/yuki/services/main_agent_turns.py` | 672 | D | compose103–569所有publication/observation prepare→writer闭包、read_result571–598/run600–666完整；原projection/ordinary acceptance/journal同原子边界，阶段source guard只先prepare/核实际源，不持writer渲染 |
| `src/yuki/services/media_process.py` | 25 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/media_resolver.py` | 625 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/message_splitter.py` | 358 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/model_context_projection.py` | 10 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/native_media.py` | 126 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/native_tool_binder.py` | 87 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/participation_diagnostics.py` | 232 | D | 全部DB projection128–232完整；只读限窗，比例保已知/完整性未知，无新写门槛 |
| `src/yuki/services/participation_feedback.py` | 859 | D | 所有实际DB/owner方法ordinary bindings68–119/scope rows197–211/sync214–336/readfacts376–558/current561–684/commit687–743/reconcile746–797/replay800–859完整；完整paid tree/真实receipt/late no_reply phantom保真；R2多余outer runproof未恢复 |
| `src/yuki/services/participation_identity.py` | 57 | F | 57行全文；actor历史集合在writer外收集，当前root在supplied session批量查询；无持久身份副本 |
| `src/yuki/services/participation_ordinary.py` | 636 | D | event/hydrated/admitted scope49–176/binding256–312/prepare320–330/current333–397/runtime416–522/hint525–587/promote590–636完整；仅actual actor补集，未知新actor退出空writer重备；沿原controller当前unit谓词，不持state pointer/重造checkpoint witness |
| `src/yuki/services/participation_parameters.py` | 13 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/participation_snapshot.py` | 96 | F | 96行全文；确是独立SnapshotStore owner：单专属线程创建/load/save/close，JSON在线程工作事务前，取消shield保原commit次序。R2 S分类本轮纠正，不纳入主DB FIFO |
| `src/yuki/services/participation_trace.py` | 91 | D | evaluate39–91完整；当前actor root reader退出后observer网络/trace，未借writer；trace recorder root另审 |
| `src/yuki/services/plugin_events.py` | 64 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/plugin_sessions.py` | 356 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/policies.py` | 171 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/processing_status.py` | 148 | I | 全调用树核原router.resolve_send_for_conversation reader退出后平台activity；不自开DB owner |
| `src/yuki/services/processor.py` | 2179 | D | 实际DB/owner方法_attachment_metadata177–244/handle639–1027/foreground1029–1708/ASR与enqueue1710–1793/privacy1900–1950/outbound2066–2131读核；普通continuation只公共confirmed rollback才重备；root canonical commit之后ACK，后续失败不重建内部event |
| `src/yuki/services/profile_commands.py` | 137 | I | preference39–77/current ControlAccess100–103完整及全方法calls；whoami aliases/membership/count只读，mutation委托原PreferenceAdmin，不新增后置audit |
| `src/yuki/services/prompt_composer.py` | 159 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/prompt_registry.py` | 102 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/qq_face_resolver.py` | 55 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/rate_limit.py` | 79 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/renderer.py` | 82 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/semantic_participation.py` | 1523 | D | start195–212/close218–236/scene367–433/retire441–460/load466–504/save506–537/binding539–562/hydrate696–870/sources875–940/admission956–1182/dispatch1184–1299/reconcile1301–1321/tick1323–1512所有真实DB/store方法完整；网络与controller历史外置，保原session/save锁序与取消后实际revision |
| `src/yuki/services/subagent_execution.py` | 385 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/turn_coordinator.py` | 377 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/turn_execution.py` | 1679 | D | admit155–230/activate256–369/compact恢复371–461/boundary477–515/request608–817/compact1033–1339/response与settle1341–1679真实owner链完整；网络之前原dispatched journal/预算，收到response后原paired；native unknown无generic自动重买。其余容量/纯transcript树结构排除 |
| `src/yuki/services/turn_transcript.py` | 243 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/video_frames.py` | 142 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/vision_rate_limit.py` | 62 | S | 当前全部imports/函数与完整调用树核为纯类型/语法/渲染/内存协调或文件网络转换，无主库或独立DB owner/持久写；未冒称纯算法全文 |
| `src/yuki/services/vision_service.py` | 609 | D | _analyze244–465完整含cache reader/save与paid provider顺序；下载/像素/hash/model/observation JSON都在repo owned writer外；singleflight仅内存锁、不持SQLite事务 |
| `src/yuki/services/work_resume.py` | 442 | X | R3 B supplement：全部实际DB/owner方法与权限/原receipt/paid恢复独立复核；详情见R3B报告；本表不冒称本reviewer全文。GroupAdmin无消费者删除delta由B负责 |
| `src/yuki/services/yuki_runtime.py` | 87 | I | 完整方法calls及start/close生命周期owner；execution stop/drain→worker close，资源DB生命周期由注入owner负责，无自有DB transaction |

服务追加复核未发现新的业务正确性缺口；后续公共 reader 改为显式 checkout 时，与 B/root 交叉发现并删除三处原纯条件下的空借还，详见下文。原 source proof、ACK/paid 顺序及原ID恢复均保留；`participation_snapshot.py` 从原报告结构排除更正为独立数据库 owner 全文复核。未运行模型或生产；纯只读补审未重复启动全仓测试，最后三处移位已运行对应目标回归。B 删除 GroupAdmin 的最后 delta 已收束，173项跨域联测、8产品mypy与15文件Ruff/format通过（B执行，非本reviewer重复执行）。

## 最后交叉发现：删除空 reader 借还

公共 reader 为准确处理取消和连接归还改为进入时 checkout 后，与 B/root 独立交叉导航确认三处原分支会完全不执行 SQL：

- `ChatService.stage_work_input` 缺少入站 Person 或原 Work actor 时，原本就在 same_person 查询前返回；把这两个原纯条件移到 context 前。
- `sync_scope_effects` 没有 initiative ID 时原 ternary 产生空列表；把原集合分支移到 context 外，只在真实 ID 非空时借 reader。
- `AdminQueries.operation` 的既有 ID 形状/PrincipalId/RequestId 解析提前到 reader 前；合法 control/rebuild/dream 仍执行原 SQL，无效请求仍给原 validation_error。

没有增加 source/authority gate，没有新增旗标、索引、连接缓存或 query 框架。修改后再次逐笔比较三个完整分支：合法查询的 selection/条件、原同session边界不变；只删除原确定无 SQL 的借还。

`test_participation_feedback.py`、`test_participation_ordinary_feedback.py`、`test_participation_projection_cost.py`、`test_admin_control_review.py`、`test_overlong_work_input.py`、`test_empty_input_processor.py` 六文件共 **66 passed /18.66s**；三个改动产品文件 **mypy、Ruff check、format check 全过**。这是既有真实调用链回归，没有为同义移位堆实现镜像测试。

## 本 reviewer 收束状态

原 A 表240项及额外services82项无待审/重复路径；A F30/D47/I23/S117/X23，服务追加的其他owner分工见表。本轮两处正确性修复29项与最后空reader移位66项都已通过各自目标；两组测试互不重叠。所有本 reviewer 产品 delta 已在修改后再次阅读。当前没有已确认未处理的本域正确性或事务边界缺口；现在停止产品改动，交由 root 冻结最终源码、全局验证和PR。没有据此声称模型/生产或整体性能验收完成。

## 第三轮修复的独立 delta 复读

在上述全范围 R3 结束后，按 root 分工额外独立复读 `1fb34b85..c0af6e82` 的公共 owner /Memory/预算修改，以及 root 当前工作树的两处空 reader 删除。这是第三轮修复的交叉 delta 检查，不另称第四次全仓审查。未并发改产品、未另跑测试。

- **Database/SQLiteConnection：**完整读 `_complete`、`_connect_sqlite`、`read_session`、`immediate_session` 和 SQLiteConnection.stop/close，并核当前安装 aiosqlite.Connection.stop/close 与 SQLAlchemy.terminate 实现。进入 reader 时显式拿到实际 driver；取消到来后仍等原 checkout/configure 完成，按实际 invalidate/close/physical completion 归还。保原 reader无autocommit/autoflushFalse，writer仍在FIFO后checkout，phase、真实commit/rollback三态与根commit hint未变。删pre_ping只删每次借还的SELECT1，不增连接缓存；invalidated driver由原pool替换。物理close实际失败仍保原失败并让writer unavailable，未把这种失败说成已成功关闭或可重新派发。
- **Schema/Alembic：**读新外层wrapper、完整内层check/migration与finally dispose；`_complete`让原异步engine owner持续到真正读/DDL/关闭完毕，再传播原取消。没有开另一engine重查/重跑迁移，也没有把取消包装成成功；schema原revision/columns/trigger等价核验未改。
- **Memory claim：**完整读专用PreparedJobClaim/commit_job_claims、两个真实claim消费者及final fence；`next_updated_at(previous)`在writer准入后计算，UPDATE RETURNING实际token再回填MemoryJob。原job/event/status/token、live event watermarks、attempt/owner/source条件保留；排队6分钟不会把刚领取任务立刻当stale。只删除无其它消费者的公共泛型入口，未造第二个claim runtime。
- **Memory operation fingerprint：**读原操作入口、prepare/commit前快路、同writer duplicate分支与unknown ACK原receipt恢复；三条request路径都统一核原operation_id对应的request fingerprint。相同operation、不同payload不再误返回另一笔成功；同原payload保原mutation/fact ID并deduplicate。自动worker key本来包含完整claim fingerprint，仍按原key恢复；repository.find删除的是曾被丢弃的未消费参数，原receipt里fingerprint未删。
- **Work预算：**完整读charge/budget表定义/budget_root_id及checkpoint和首次accept两真实caller。INSERT SELECT以同writer原root Work的当前models/tools初始化，保on-conflict-do-nothing、不覆盖既有limits；之后原UPDATE条件加预算并RETURNING，失败仍同事务rollback。两处caller均先charge后增加Work计数，没有少计、重复加计或重置父树预算。原nullable limit省略即SQL NULL，不变成默认额度。
- **Root空reader：**完整读WorkRepository.prepare_input与ProtocolStore.publication。前者只把原before_publish=None分支移到context外，有publisher时仍进原writer调用，旧ready/input回执与payload不改；后者空digests跳过空文件核验/reader，仍yield空manifest、保原protocol lock及成功/失败清理。非空digest的文件核验、完整refs选择和publication atomic unit未变。

另读了新增的真实connector/read取消、schema/Alembic晚connector关闭、Memory两种claim排队时钟、三个原operation race/ACK用例源码；测试覆盖对应实际线程/SQL/receipt事实，未仅mock方法名冒充验收。具体运行结果仍以B/C/root各自执行记录为准，本 delta 不重复记测试数。

**结论：没有新增已确认正确性或原事务/receipt/预算语义缺口，不添加额外门槛或防御机制。本 reviewer 已再次停止产品改动。**
