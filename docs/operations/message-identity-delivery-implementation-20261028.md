# 消息身份、投递和桌面富媒体实施记录

任务来源：[10-28任务书](../architecture/Yuki-消息身份与投递链路收敛任务书-2026-10-28.md)。后端基于 `afb509e7`，整合时已合入main的 #530；桌面基于 `1d42784`。两者均在新的独立worktree实施，另一个会话的小bug工作区未改动。实施跨10月28日至29日。

## 实施范围

| 项目 | 改动与验证重点 |
| --- | --- |
| T01 | Inbound完整ingress/native、账本origin/native、内部event关联分开；不保含义猜测alias |
| T02 | 0154→0155冻结迁移、原PK/FK/自增高水位、原fingerprint和其他表数据保持 |
| T03 | 本地Social单writer发表与结算；删除App发送noop、伪平台号及平台列恢复扫描 |
| T04 | 原outbox先准备稳定artifact，SQL时核attempt/lease，事件与sent原子提交；实际Client读取 |
| T05 | 原生引用由真实native投影；无号文件成功、首个物理part保内部reply、旧prepared保原JSON |
| T06 | 完整接入键、首次引用原生namespace、Account跨assignment和TG跨接收账号语义 |
| T07 | SDK9、hooks与消费者、旧不可变actor/authority reader、新自动化冷列空值 |
| T08 | 原operation/outbox/Work/请求回执恢复；不增加全局恢复管理器 |
| T09 | claim物理提交后finally唤醒、活跃intent先过滤、实际阶段计时 |
| T10 | 索引missing-open竞态、原artifact锁双owner读写、GC迭代 |
| T11 | TGS流式C级跳块、正确JSON数字、真实8MiB文件成本与完整保存链 |
| T12 | 当前合同、升级指南、SDK配套及三仓独立交付 |
| T13 | 桌面受控app流与Range、统一预览、Bot图片内联、音视频/PDF、公式/Mermaid/内部图 |

## 验证

后端原生与App、真实0154旧库升级、原来源hash、长接入键修复、取消/确认丢失、并发CAS、真实批准通知的Client文件链和远端晚确认均已定向验证。Ruff与Linux平台MyPy全689个源码文件通过。旧迁移原行、预算、外键与原件比较的六文件回归19项通过；原Linux root双UID锁与GC实际执行，没有用普通用户skip冒充通过。

为避免跨Windows文件系统反复扫描迁移脚本，正式整套验证使用原生Linux目录和独立环境，固定源码归档 `60787e8`；CLI、Vec1和Git index均指向该快照。完整integration为 **926 passed、1 skipped、94 deselected**，跳过的是按显式开关运行的付费Codex测试，native_worker由独立CI job验证。

该快照的unit原始结果为 **3785 passed、7 failed、25 skipped**。7项为已退役参数的旧测试、三个pending观察器未转发active、旧late-event字典字段，以及新增native列的来源分类和outbox观察器字段。保留原日志，不将它改写为全绿。对应当前补丁的五整模块新进程 **166 passed**，最后两整模块 **22 passed**，七项均已覆盖修正；未为通过而恢复旧字段、放宽异常或延长并发期限。最终提交的整套CI以[后端PR](https://github.com/YuanYeYouTao/Yuki/pull/531) Actions为准。

桌面合同固定公开后端 `8720d929` 的实际OpenAPI，内容hash保持原值，Client API仍为7。build、lint、typecheck和合同检查通过，单元318项通过、3项跳过；四套真实Electron媒体E2E及连续六次主题切换的实际文字像素验证通过。播放、seek、PDF、原件到期、双角色、冷重开、128MiB视频和500条历史的详细证据见[桌面报告](https://github.com/Institute-of-Agent-Life/Yuki-Desktop/blob/codex/desktop-rich-media-20261028/docs/reports/2026-10-28-rich-media/README.md)。

独立GitHub Monitor以公开SDK9归档在新环境安装，25项测试、lint、格式和doctor入口通过，其PR的CI已通过。

## 局部成本

TGS同一8MiB输入、预热一次并测七次：旧逐字节实现中位392.61ms，本次9.48ms，Python峰值245,311B；宽高数值同时修正。完整真实8MiB原件的header→复制→catalog链实测62.3ms、峰值189,233B。[原始TGS样本](message-identity-tgs-cost-20261028.json)保留全部计时。

整合复审再删除未选intent发现阶段的重复来源核验。它原本无论核验结果都进入ready，真正selection前本来还会查源；已选、待文字的撤权核验继续保留。同一100个待发intent，五次发现查询固定203→3，中位182.76→7.32ms，ready仍为100，相关81项回归通过，原3秒期限未改。[原始发现成本](message-identity-pending-read-cost-20261028.json)已入仓。

桌面128MiB视频IPC仅269B元数据，真实Range返回16KiB；同样500条历史，旧版分到500条3.645s、新版3.739s，renderer CPU接近、工作集增量200.1→174.3MiB。局部计时与单次历史对照不换算成整条发送延迟或固定上限。

## 复审记录

第一轮沿调用者检查身份拆分，修复了内部reply被native空值丢弃、手造测试来源缺接收Account、活DTO和旧JSON reader混用。

第二轮按取消、提交异常与并发检查本地发表。发现确认丢失被返回为工具错误会引起模型重复调用，修为读原operation成功回执；原生claim取消只在能证明本次物理提交成功且尚未调用API时结算为未尝试，避免抢结算另一并发owner。

第三轮由独立实现者复审核心Social、冻结迁移、SDK/插件和桌面生命周期。删除了远端真实确认仍受255字符限制的遗留，并为native精确查找加索引；旧模型来源编码与付费回执保持不变。桌面补齐了隐藏session资源释放、共享SHA的独立来源资格、Mermaid根级htmlLabels与主题对比。缩略图疑似缺字经原尺寸像素反证为误判，未留下盲目重试、强制repaint或替代渲染机制。另查出并删除了上述未选intent的重复SQL。

各专题的三轮细项与命令见[SDK复审](message-identity-sdk-review-20261028.md)和[通知/媒体复审](message-identity-backend-media-review-20261028.md)。

## 交付

三份PR分别为[后端 #531](https://github.com/YuanYeYouTao/Yuki/pull/531)、[桌面 #13](https://github.com/Institute-of-Agent-Life/Yuki-Desktop/pull/13)、[GitHub Monitor #4](https://github.com/Institute-of-Agent-Life/Yuki-GitHub-Monitor/pull/4)。本轮只提交PR，未合并、发布Release或上线，生产镜像和其他服务未操作，也未发送真实平台测试消息。

桌面生成的临时源码归档清理被自动批准审查以 `blocked by policy` 拒绝，删除数量为0；这些目录保留并排除提交。产品文档、必要截图、性能样本和真实媒体fixture均已单独纳入PR。
