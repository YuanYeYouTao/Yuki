# #163 文件唯一源实现后三轮交叉复审

日期：2026-10-19（UTC+8）。依据[任务书](../architecture/Yuki-163表情目录索引与异步配图重构任务书-2026-10-19.md)、[开发约束](../architecture/development-contract.md)。新方案的四轮实施前审查已完成；本页重新记录实现后三轮，旧数据库方案的P1/P2不计入。

| 轮次 | 实际交叉范围 | 结论 |
| --- | --- | --- |
| P1 | 存储审发送；发送审入站/SDK/迁移；入站审文件/Manager；根核全局合同、配置、部署和残留 | 已完成；实际发现G36–G45，修复专项通过；未代替后两轮 |
| P2 | 交换owner后审最深并发、异常与恢复，反查P1修复和真实效果 | 待开始 |
| P3 | 最终全链、任务书每S/F、CI与实际部署闭环 | 待开始 |

## 实施与专项基线

三路新方案已落地后才开始本轮。原图库表/来源DTO/repository/旧scan与导出RPC/保存hook/Qwen重试已删除；正文与后台图分别认原Social事实。施工又查明G35物理文件桥全局锁队头，已改短receipt锁并以真实阻塞双发布核验；TG原生分步前一个纯提及事件不再误写表情标记。当地原生贴纸flag在已有普通编目后可更新，保原文件/标签且不重新caption。

Linux专项包含：文件核心27、共享原件15、实际Manager/Unix socket发布与checkout9、深度发送恢复19、AgentRunner三种正文/纯图/纯图提及3；另有媒体/SDK/迁移56、来源与长文件名5、应用direct/Code启动关闭和重复取消3。集合存在重叠，不相加冒充独立总数。独立养鲲插件在当前Host SDK重新99项通过。完整unit/integration与最终静态验证另记。

## 新文件引擎性能

Python3.12.15、SQLite3.46.1、Linux WSL2容器/tmp；1000/10000张实际不同hash PNG及canonical JSON。standalone rebuild分别2293.551/17764.628ms、errors=0。10000项四个查询smile/大笑/😂/无命中的SQL中位0.2427/0.6259/0.6691/0.0054ms；实际进程启动与候选文件hash的搜索约236–374ms。EXPLAIN为词项主键covering search，json_each仅遍历查询词，聚合/排序处理命中集，无资产全表扫描。数据来自ignored `.cache/sticker-file-index-probe.json`。这些是刚写入文件的离线样本，不证明冷磁盘、线上投递、语义准确率或Provider缓存命中。

## P1 修复与独立反查

实际竞争/异常探针查明并修复：错误码分类保留BUSY/IOERR与程序错误；候选失效清理重读当前JSON，不删并发已完成update；Manager.close反复取消仍等待实际物理owner；同一原publish请求共享在途事实，不把正在完成的快照误判为关闭意图；cache重建后再消失沿真实needs_build继续。原件接纳前核原generation/floor，普通compact不加收藏门槛。

TG旧IMAGE无MIME原件按真实字节处理TGS/WebM，Workspace无TGS预览时只给真实元信息。入站/共享媒体33项、独立旧WebM当前/引用/缓存/历史/Workspace两项Linux实际ffmpeg通过，每原件一次HTTP、无Qwen或跨Bot回退、不改原segments。发送深度恢复与物理发布组合27项通过；这些集合重叠，不相加。

部署闭包另外修复Manager的30秒强制停止与不必要的Bot模型顶层导入。孤立源码只复制sandbox/workspace即可导入Manager；6个工具完整dataclass与原HEAD相等，未加载PIL/SQLAlchemy/pydantic/nonebot。持久rootFS内安装helper后仍须沿原私有checkpoint机制保留，生产尚未执行。
