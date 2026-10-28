# 消息身份、投递和桌面富媒体实施记录

任务来源：[10-28任务书](../architecture/Yuki-消息身份与投递链路收敛任务书-2026-10-28.md)。后端基于 `afb509e7`，桌面基于 `1d42784`，两者均在新的独立worktree实施。另一个会话的小bug工作区未改动。

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

## 验证进度

源码已进入整合验证。后端原生与App回归、真实旧head迁移、原来源hash和提交确认丢失回归已执行；完整Linux unit/integration仍在运行，最终结果在本节收尾后列出。

桌面已执行真实Electron播放、seek及渲染用例；最终合同将固定配套后端commit。主进程受控媒体流避免整件IPC复制。实际安装发行、真实平台流量和生产部署不包含在本轮PR范围。

## 复审记录

第一轮沿调用者检查身份拆分，修复了内部reply被native空值丢弃、手造测试来源缺接收Account、活DTO和旧JSON reader混用。

第二轮按取消、提交异常与并发检查本地发表。发现确认丢失被返回为工具错误会引起模型重复调用，修为读原operation成功回执；原生claim取消只在能证明本次物理提交成功且尚未调用API时结算为未尝试，避免抢结算另一并发owner。

第三轮跨仓检查由独立实现者复审核心Social、冻结迁移、SDK/插件和桌面生命周期。结果与后续修复在最终提交前补齐。

## 交付

后端、桌面与独立GitHub Monitor插件分支将分别提交PR。本轮未合并、未发布Release、未构建替换生产镜像、未上线，也未发送真实平台测试消息。
