# #163 文件唯一源：四轮实施前审查

日期：2026-10-19（UTC+8）。用户明确要求先四轮审查，再实施、PR、合并所有未合PR并上线。依据[新版任务书](../architecture/Yuki-163表情目录索引与异步配图重构任务书-2026-10-19.md)及[开发约束](../architecture/development-contract.md)。本记录重新计数，旧审查不抵本轮。

| 轮次 | 规定重点 | 状态 |
| --- | --- | --- |
| R1 | 资料、执行、权限、平台与首次调用边界 | 完成，三路源码核查并根审，新增G14–G19 |
| R2 | 最广：全部producer/consumer、装配、SDK/插件/自动化、历史/升级、CI/部署/现存环境 | 完成；新增G20–G28及实际部署／CI闭环 |
| R3 | 最深：索引并发、首次创建、真实HTTP、接纳与回执、原件快照、恢复/取消竞态 | 完成；真实Linux/SQLite/HTTP探针与三路源码核查，新增G29–G34 |
| R4 | 汇总后的终审，反查每项任务/验收/owner与删除，确认无未定职责后实施 | 完成；统一三处文件语义及TG原生计划，实施职责闭合 |

## R1：边界

- 发送：逐意图并发若只查pending首页，会把串行等待换成分页队头；跨页发现沿原排序，不增持久状态/配额。
- 文件桥：已成功save之后await图库编目会吞原成功。删除WorkspaceService.stickers隐式hook，Main自由执行helper，不把通用文件桥反向耦合到图库。
- SDK：既有current media投影补通用is_sticker、格式、MIME、尺寸及预览事实；保持原SDK批准，不能借Main自由给第三方开放Host数据库。
- 首次标注：删数据库created之后，首次以canonical文件原子创建实际赢家确定；不靠标签为空、重启、缓存缺失或额外caption状态表。
- 真实付费：Qwen provider的HTTP重试使一次analyze可重复post。删除对应循环与配置，测试真实HTTP请求数。
- 部分资料：单个坏JSON/缺失文件不能封住全平台索引；逐项失败不补假资料，正常项继续。
- 主库：asset/terms/source及装配/metadata注册/Person forget分支整链删除，原事件核验留在Host媒体owner；文件引擎不接主库。

本轮只修改计划，无新文件引擎实现。源码位置及行为证据由三个原模块审查者交叉提供，根审已回写任务书G14–G19。

## R2：最广覆盖

| 范围 | 实际检查与结论 |
| --- | --- |
| QQ/TG、共享current/reply/preview、缓存、历史与Workspace | 新增TG真GIF animation漏链；按真实bytes走共用帧处理。原TTL/原Presence/内部事件不改变 |
| SDK/facades/registrar/permissions/fake/Code/内外插件 | G16通用事实补齐，通知原artifact MIME漏传；第三方已有SDK授权保持，旧Emoji专属producer无必要保留 |
| Person/数据库metadata/迁移/forget | 三图库表及依赖全删，原事件／paid／控制receipt reader继续。文件不会按Person误删 |
| Social所有Person/SELF/PLUGIN_BACKGROUND入口、自动化、Work与效果恢复 | 实复现纯emoji quote/@丢语义、SELF源Presence被目标覆盖、同Binding双Presence未消费原preferred；其余显式文件/通知保持原同步效果合同 |
| event_prompt/history_projection/FrozenFragments/compact、direct/Code | 短tag只追加原新发送，旧冻结／已付请求不重建，工具声明不随库变 |
| Workspace schema/Store/SDK产物/Social/客户端下载 | 任意128仅两schema与Store；80真实filename截断还影响历史reader后缀。删实际门槛与资料截断，留展示职责 |
| SandboxClient/Manager/启动关闭/打包/镜像rootFS | cold不能套7秒观察期限；旧scan/index_update/cursor全删；helper须脱Host依赖并实际装入原环境 |
| Vision配置、constructor、HTTP与fixtures/文档 | 清Qwen自己的隐式付费重试闭包，不顺手改其他LLM Provider |
| CI／升级／部署 | 完整unit/integration继续；显式Linux ffmpeg覆盖。#476 Person候选只读核实后operator授原人，退休SUPERUSERS；SDK5现用插件按原权限批准，保KV/outbox/存档；不发送平台试消息 |

根另核#479新9530fe1c的原request/receipt竞争修复与真实回归，后续和全部未合PR统一合并；该PR不是图库新实现验收。线上旧管理员候选已只读核对为单一有效Person、无转移歧义，部署仍沿operator显式授予，不把候选脚本改成自动赋权。R2无产品改动。

## R3：最深

- 实际双owner SQLite竞争复现迟到no_candidate覆盖已选定资料；条件关闭只保护未selected阶段，真实选定后失败仍正常结束。
- PREPARED原计划后Person合并不能重算原hash；Social原计划与当前same_person/授权分开核验，保真转移阻止新效果。
- 实际Linux Manager→FD→WorkspaceStore探针得到version A/artifact B，正式修改必须在单次snapshot真实digest后、首次DML前核expected。
- 实际TG Social原生计划有sendMessage→sendSticker两个part，父PREPARED时子项可能已SUCCEEDED。恢复认原向量，不重发@、不为同payload假承诺改格式。
- 真实Qwen MockTransport一次analyze收到2 POST；删provider实际重试，不只改调用者。
- 实际合法175033B大尺寸PNG被视觉像素限制挡在收藏前。收藏与可选解码分开，保解码器本身限制。
- 实际socket约7秒断开时文件线程还运行；awaiter取消后Manager.close返回而线程继续写。长本地操作按实际owner drain，不套30秒command期限或以kill docker exec客户端冒充容器内进程结束。
- 多进程设计POC：仅SQLite串行不足，旧扫描覆盖已完成update；协调段内writer外重读JSON才保新值。8进程完整JSON原子创建仅1赢家；完整性ok的cache仍可能只有半库，ready只属于派生cache格式。
- Host FD拒绝symlink/父目录替换；SQLite及lock也不能从Host打开用户路径。同一独立helper在现有环境namespace运行，无新daemon/jobs/权限。
- 媒体接纳使用Host单JOIN只读核验，Library先于媒体owner关闭；保原Presence/TTL/generation/TGS原件与真实预览，不建来源表或接纳状态。

临时探针在ignored `.cache/`；现有媒体回归18 passed/3 codec skipped仅基线。新的引擎、并发、HTTP与真实codec验收留实施后，不能用POC代替。R3结束后再审整份计划，尚未实施新产品源。

## R4：任务书终审

三路重新通读G01–G34、S01–S13、F01–F13并根审：大尺寸原件解码失败仍编目；主动复制图+JSON保复制文本但重算目标事实；caption在途最后写仍是普通文件语义；TG原生分步不伪造同payload承诺。实际claim/settlement提交后ACK丢失探针证明现有Social原CAS可分别保unknown零网络重发及成功只发一次，无需第二台账。F09/F10覆盖原向量、双owner迟到close、PREPARED之后Person合并及真实事务提交确认丢失；F13补原Person超管与插件原权限/存档保留。

实施归属：A负责独立文件引擎/Library/Manager helper桥/实际HTTP重试删除及核心回归；B负责Social接纳/并发/恢复/Work原向量、prepared人物合并、snapshot版本和路径schema；C负责主库清退/装配/媒体只读接纳、GIF与历史文件名/TG预览、SDK/通知MIME及配置消费者；根负责现行文档/CI、合流、最终验证与实际部署。共享文件按代码块协调，不互相覆盖。四轮实施前审查结束，开始新方案实施；产品完成与实现后三轮复审尚未发生。
