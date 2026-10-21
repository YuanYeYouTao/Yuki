# #163 文件唯一源实现后三轮交叉复审

日期：2026-10-19（UTC+8）。依据[任务书](../architecture/Yuki-163表情目录索引与异步配图重构任务书-2026-10-19.md)、[开发约束](../architecture/development-contract.md)。新方案的四轮实施前审查已完成；本页重新记录实现后三轮，旧数据库方案的P1/P2不计入。

| 轮次 | 实际交叉范围 | 结论 |
| --- | --- | --- |
| P1 | 存储审发送；发送审入站/SDK/迁移；入站审文件/Manager；根核全局合同、配置、部署和残留 | 已完成；实际发现G36–G45，修复专项通过；未代替后两轮 |
| P2 | 交换owner后审最深并发、异常与恢复，反查P1修复和真实效果 | 已完成；G46/G47修后44项专项及静态检查通过 |
| P3 | 最终全链、任务书每S/F及用户追加共享媒体范围 | 本地三次交叉审查已收拢；完整CI及实际部署另列，不由源码审查宣称上线 |

## 实施与专项基线

三路新方案已落地后才开始本轮。原图库表/来源DTO/repository/旧scan与导出RPC/保存hook/Qwen重试已删除；正文与后台图分别认原Social事实。施工又查明G35物理文件桥全局锁队头，已改短receipt锁并以真实阻塞双发布核验；TG原生分步前一个纯提及事件不再误写表情标记。当地原生贴纸flag在已有普通编目后可更新，保原文件/标签且不重新caption。

Linux专项包含：文件核心27、共享原件15、实际Manager/Unix socket发布与checkout9、深度发送恢复19、AgentRunner三种正文/纯图/纯图提及3；另有媒体/SDK/迁移56、来源与长文件名5、应用direct/Code启动关闭和重复取消3。集合存在重叠，不相加冒充独立总数。独立养鲲插件在当前Host SDK重新99项通过。完整unit/integration与最终静态验证另记。

## 新文件引擎性能

Python3.12.15、SQLite3.46.1、Linux WSL2容器/tmp；1000/10000张实际不同hash PNG及canonical JSON。standalone rebuild分别2293.551/17764.628ms、errors=0。10000项四个查询smile/大笑/😂/无命中的SQL中位0.2427/0.6259/0.6691/0.0054ms；实际进程启动与候选文件hash的搜索约236–374ms。EXPLAIN为词项主键covering search，json_each仅遍历查询词，聚合/排序处理命中集，无资产全表扫描。数据来自ignored `.cache/sticker-file-index-probe.json`。这些是刚写入文件的离线样本，不证明冷磁盘、线上投递、语义准确率或Provider缓存命中。

## P1 修复与独立反查

实际竞争/异常探针查明并修复：错误码分类保留BUSY/IOERR与程序错误；候选失效清理重读当前JSON，不删并发已完成update；Manager.close反复取消仍等待实际物理owner；同一原publish请求共享在途事实，不把正在完成的快照误判为关闭意图；cache重建后再消失沿真实needs_build继续。当时原件接纳前仍核原generation/floor；用户后续明确临时原文件全会话共享，这部分已由下述新范围取代，不能继续作为全局读取门槛。

TG旧IMAGE无MIME原件按真实字节处理TGS/WebM，Workspace无TGS预览时只给真实元信息。入站/共享媒体33项、独立旧WebM当前/引用/缓存/历史/Workspace两项Linux实际ffmpeg通过，每原件一次HTTP、无Qwen或跨Bot回退、不改原segments。发送深度恢复与物理发布组合27项通过；这些集合重叠，不相加。

部署闭包另外修复Manager的30秒强制停止与不必要的Bot模型顶层导入。孤立源码只复制sandbox/workspace即可导入Manager；6个工具完整dataclass与原HEAD相等，未加载PIL/SQLAlchemy/pydantic/nonebot。持久rootFS内安装helper后仍须沿原私有checkpoint机制保留，生产尚未执行。


## P2 交叉审查与证据边界

交换到非原owner后重读实际消费者。入站侧72项Linux通过，无skip/失败；实际平台编辑另2项证实变化附件已沿原入口清退cache/hash/expiry，仅正文变化保原缓存与TTL。最初直接DML改事件绕过正常编辑链的诊断反例已撤回，不作为产品漏洞，也没有据此增加version门槛。

发送/恢复四模块88项、实际Manager发布和direct/Code装配7项通过；新增真实账号transfer、双owner网络claim竞争、重复取消等4项长期回归，所属recovery模块23项通过。集合有重叠，不相加。F07–F12原Social向量、source/target身份、当前授权、已确认正文、回执优先、慢图隔离与固定工具均沿原事实检查。

文件侧实际发现G46：重复原生编目在hash期间，Main完成标签修改或原件改名/update，迟到编目会写回旧资料；修为耗时工作后读取当前文件，变动路径保当前资料，仅补本次native事实。G47是ready与SQL之间cache被终端删除的真实窗口，当前缺库返回needs_build，健康库程序SQL错误及BUSY/IOERR仍报原失败。专项和最终收尾另记，不把源码阅读当实际发送验收。

## 最终追踪入口

| 范围 | 源码/长期回归入口 | 交付边界 |
| --- | --- | --- |
| S01–S05 / F01–F04 | emoji/sticker_index.py、media.py、library.py；sticker_library、sticker_catalog_edits、vision_retry_configuration | 文件唯一源、共享终端与caption事实；独立检索性能另记 |
| S06–S07 / F07–F12 | emoji/delivery.py、social/emoji_intents.py、social/service.py、sandbox/persistent.py、workspace/store.py；illustration/recovery、publication、publish_versions | 原Social/FD快照与固定声明；不冒充平台展示 |
| S08–S09 / F05–F06 | shared_inbound_media、telegram_sticker_inputs、telegram_legacy_image_webm、telegram_event_edits、plugin_native_media、notification_platform_entries | 真实原件/codec/来源/SDK；无实际平台测试消息 |
| S10–S11 | 0138/0139、migration_0138、sticker_container_assembly、quality.yml | 前向保事实、实际装配与CI；完整验证另报 |
| S12–S13 / F13 | 当前模块合同、SDK5迁移、persistent-environment与人物授权升级说明 | 所有PR、原权限插件、实际Manager/helper/检查点、Bot健康后清理分别记录；未操作前不记通过 |


## P3 用户追加范围与最终反查

用户要求Main自行存图、简单维护索引，并核“一见附件就读/念表情”。真实核查发现自动FILE解析及当前输入强制回应文字，未发现普通Main额外付费视觉fallback。追加G48–G51/F14：单图用现有update增量编目，显式--tag替换、未给保资料；独立CLI冷搜索实际重建、热搜索不全扫。维护跟随实际操作，不新增watcher、定时扫描、权限或维护状态表。

最初追加只针对普通FILE；随后用户明确扩大为全部图片/视频/贴纸/文件首请求均只给索引并按需读取，旧Main自动准备链因此整删。TGS真实原件和平台预览仍沿显式reader保存，不能误删为普通文档。输入成功/失败改事实标签，系统静态媒体提示约束自然接话、按需选读和emoji.goal，不改用户原问题、已冻结历史或paid轨迹。三路在新源落地后再次交叉核查，未完成的实际回归与交付仍单独列。

完整Linux冻结基线d9c18829的integration为599 passed、90 skipped（可选Monty/真实外部集成环境未启用），无失败。后续P2/P3新增源用专项补验，最终完整CI另核；这不是把基线结果算作新源全量通过。同一冻结基线unit原始结果2680 passed、20 skipped、4 failed；其一为旧视频mock没有创建声明的路径，修真实fixture后该模块6 passed；另3为archive没有.git，重建精确tracked index后release模块4 passed。没有在产品吞错或跳过失败。新追加范围最终完整验证另记。


## 共享全媒体追加范围：重新交叉审查

用户明确“统一放共享沙箱，各会话可直接读”，不是保留旧Conversation授权而仅开终端旁路。实际现网原缓存41MiB、位于Bot数据挂载，环境HOME为独立2GiB；部署采用同份cache的只读输入挂载，不搬到Main可写HOME、不复制第二份缓存。此前“保图片自动输入/仅FILE按需”和旧私人附件范围已被这次授权替代。

扩展第一遍核全部真实接缝，新增G52–G62：首次输入资料缺失、observed reply无真实内部来源、32/容量丢缓存门槛、shared cache挂载/Host symlink、WorkSourceGuard重复隔离、Telegram流close、GC与取消物理owner、正常edit旧weak候选、已知reply原Presence。修复后重新核实际缓存、原生读取和Main派发，不以P1/P2既有结果代替新增范围验证。

根新增真实PNG跨Conversation及源Conversation执行ai new后按需读像素，经过Workspace validator、冷/热WorkSourceGuard和restore均可继续；删除实际文件仍由媒体validator报告失效。context guard不把图片源事件变成当前聊天历史依赖。所属18项通过，原上下文/隐私/owner/lease变更回归保持。

扩展第二遍由非原owner审真实cache/ASR/Manager与恢复，修正G59–G65及已知引用的编辑移位：原接收Presence、真实canonical段位、内联audio、流关闭、活动暂存owner、独立上传并行、缓存不解压gzip。统一原件已只返回(Path,sha)，删除旧内存分支，真实视觉DownloadMedia输入仍保留。原件流式/cacheowner组合39项、新owner与音频14项、纯Path后32项分别通过，集合有重叠不相加。

扩展第三遍只读反查§3.2/S01–S16/F01–F17与全消费者：原Library自动收藏/首次创建赢家caption、TGS预览、CLI主动维护、纯emoji与正文先发、原Social分步回执/unknown恢复、SDK/通知/自动化、旧capability真实receipt reader、Main当前索引和固定prefix。补掉工具schema仍称“当前会话”的旧范围文案，path说明纳入共享cache；不是新增授权或分流。其余未发现新的实质缺口。

原#163十一模块在新媒体实现下120 passed/93.65s，覆盖文件引擎、标签并发、主动维护、正文/纯emoji/恢复、真实FD发布、一次Qwen、direct/Code装配、主动收藏及平台通知。最后死分支后另跑21项shared/caption/TGS通过。全源码Linux mypy634份、全仓Ruff通过；完整CI、PR、合并和生产仍按真实后续结果记录，不把这些专项冒充全量或平台自然流量验收。


## PR阶段启动顺序反查

PR #480 首版9dfeb1d8的checks/integration通过时，继续检查部署启动恢复，实际发现G66。真实PluginManager正常stop把仍enabled/原grant未撤的插件状态写回approved，emoji owner早于plugin.start恢复会把原pending误close为permission_denied。正式新回归贯通真实Container、磁盘插件生命周期、PluginNotificationRepository授权/事件与原Social配图；套回旧顺序实际红，调整注册到plugins/manifest之后、Runtime之前后，含原direct/Code装配三项通过，container Mypy与Ruff通过。

这次只改真实生命周期依赖顺序，不放宽授权、不增加启动状态或等待门槛。首版CI只作为首版证据；修复后PR新head重新运行完整Quality。实际生产尚未切换。
