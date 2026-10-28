# 桌面富媒体消费审查与任务书补充（2026-10-28）

用户要求把桌面下游一并纳入[消息身份与投递任务书T13](../architecture/Yuki-消息身份与投递链路收敛任务书-2026-10-28.md)。后端审查基线`afb509e7c96f1949651b505e601758f14d2f8fd8`；桌面为`Institute-of-Agent-Life/Yuki-Desktop`最新main `1d42784a17bfc09f509e9f50f0f5c0be877b2d7d`（PR #11）。本轮只改任务文档，未修改任一仓库的业务代码或生产。

## 链路与已确认缺口

完整路径为Social/通知发表→事件与稳定附件→Client API→桌面主进程下载/缓存→renderer气泡/查看器/播放器。每一层分别核对，不能用一层修复代替其他层。

| 位置 | 当前事实 | 任务归属 |
| --- | --- | --- |
| 后端通知图片 | `notification_delivery.py:557-568`仅记临时media_handle；事件投影后只剩kind=image与索引，没有稳定可读原件身份 | T04冻结artifact/version及event关系；T13走真实通知端到端 |
| 普通Social成果 | 会保存artifact/version/name/size，Client有原件接口 | 正常链路，不能误报为所有图片后端都坏 |
| App上传尺寸 | segment写file_size，event_view只透size，公开尺寸丢失 | T13只改投影到既有size，不改旧segments/hash |
| 桌面出站图片/表情 | `Media.tsx:60`只自动读取非artifact图片；成果图气泡只卡片，点击后可看 | T13沿精确artifact版本内嵌图像，保点击放大/原件 |
| 普通audio附件 | `connections.ts:776`白名单缺audio，`Media.tsx:205`播放器因此拿不到URL | T13打通实际audio路径并验证播放时间推进 |
| 视频/音频成果 | `artifactContent`缺video/audio字节，`Files.Content`也缺播放器 | T13两种来源统一支持，不用普通附件视频测试替代成果 |
| PDF | 成果PDF有iframe，普通附件PDF只有保存后打开 | T13两查看器补齐实际PDF查看 |
| 富文本 | Markdown/表格/Shiki已有；公式、Mermaid没有实现，Markdown图片主动禁用 | T13明确新增本地公式/图表及获准内部图片显示，继续复用已有正文渲染 |

音频结论仅指真实audio/*路径：部分录音缺MIME、文件名voice.webm时，可能被扩展名误判为video并借视频控件播音轨，不能宣称所有录音都不能播放。两条读取路径的MIME解释应收敛；缺MIME保持事实，结合filename和kind推断，不能假定上传表里已有未存储的MIME。

后端字节接口的application/octet-stream是既有二进制合同，不单独算显示错误；桌面已有文件名/元数据和缓存。解码器不能播放某个真实codec，也应与字节未交给播放器、文件未发表区分。

## PR #11范围

该PR修复已收eventId而历史页未跟上时消息短暂消失、latest_event_id水位以及共享角色/本机Agent等问题。它没有修改上述Media.tsx、Files.tsx和媒体读取实现，不能把“消息显示修复”写成富媒体全部补齐。

## 本轮证据与任务规格复核

三路分别核renderer、主进程/IPC/缓存、后端事件/下载合同，再检查T13的最小范围：

- 真实`event_view`合成DTO探针：普通Social产物有artifact/version/name/size，App上传有media_id/filename但缺size，通知图只剩类型和索引。结果纳入[便携证据](message-identity-delivery-evidence-20261028.json)，不将合成投影当真实通知已成功。
- 两来源与两查看器都纳入矩阵，音频要求真实currentTime推进，视频要求实际解码/播放/seek，PDF要求实际文档内容，图像要求真实像素。卡片、ASR文本、HTML标签存在均不足以验收。
- 复用磁盘ContentCache与现有app://受控媒体出口；音视频分块/seek按实际Electron验证，不把完整文件多次复制为IPC数组和Blob。测首帧/首声、两进程RSS、IPC传输量、读取/hash次数。
- 公式/Mermaid是新增渲染能力，按需本地加载，正文先可见；内部Markdown图片仍映射现有获准AttachmentSource/ArtifactSource，未增加自动外链下载服务。
- Client API保持实际revision7/OpenAPI，SDK9属于不同合同；两仓分别提PR/运行检查/打包，再做配套端到端验收。后端合并不等于用户安装包已更新。

原始短审查及建议保存在本机`D:/Code/Reports/Yuki/desktop-rich-media-review-20261028/`。本次未启动真实播放器、没有重新执行完整Electron测试；这些已列为实施后的验收。本轮也未发送消息、调用付费模型、安装依赖、提交、推送或部署。
