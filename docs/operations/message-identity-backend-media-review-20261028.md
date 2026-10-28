# 后端通知、表情调度与富媒体复审（2026-10-28）

对应消息身份与投递收敛任务书 T04、T09、T10、T11、T13 的后端部分。以本分支实现为准；本文记录本地实现、隔离环境验证和三轮复审，未执行生产操作。

## 实现与接口

- 通知准备阶段在原 `prepared.route` 冻结 Provider、`local_ledger`、Presence、binding 和 Conversation。本地通知在调用原生发送前分支，复用 `ResolvedSend`，不调用 App 的空发送实现。
- 插件媒体经过一次 `read_owned`，将已校验的同一份 bytes 交给既有 `WorkspaceStore.snapshot` 发布稳定原件。artifact 身份由原 outbox ID 派生，原件的 version 是实际 SHA-256。通知事件只保留可下载的 artifact/version 和真实名称、大小、MIME，不向桌面暴露临时 `media_handle_id`。
- 本地最终 writer 先用 SQL CAS 核验原 attempt 与数据库当前时间下仍有效的 lease，再在同一事务中核验源、授权和目标，完成 ledger、artifact ref、outbox.event_id 和 sent。事务失败不留半条事件。取消和 commit 回执丢失先读取原 outbox；已经提交的 sent 不降级，不重新取媒体或重新发送。
- `NotificationDeliveryReceipt.message_id` 是内部可空字段。本地为 None，远端使用适配器确认的真实事实。共享通知层不要求所有成功操作都有原生消息号，也不再用空字符串、合成 outbox ID 或 `remote_confirmed` 表示本地成功。
- 远端晚到的真实确认仍按原 attempt 结算，允许没有原生消息号；本地准备 lease 不会阻挡已经发生的远端效果。旧 processing 缺少冻结的本地证明时仍保留 uncertain，不凭当前 Provider 推测重放。
- 表情队列先排除 `_deliveries` 中的活动 ID，再查询各项依赖。一页所依赖的原 Social operation 批量读一次，相同 parent 的文本完成状态只计算一次。原 `_finished` wake 保留。选择、发布可以与文字并行，真正发送仍等待原文字步骤结束。
- 表情日志使用真实阶段：源校验、选择结算、artifact 发布、文字状态核查、Social 执行结束、intent 关闭。`phase_ms` 是本阶段时长，`since_attempt_ms` 是本次尝试累计时间；不再把发布完成写成 artifact 已读。日志关联原 intent、derived call、artifact；未写目标、标签或聊天内容。
- sticker cache 的 exists→mode=ro 打开竞态，只在真实 `SQLITE_CANTOPEN` 且文件已经消失时走现有重建。SQL、权限、锁、磁盘和 I/O 错误继续报出，不混成缓存丢失。
- artifact 锁创建为 0660，配合 Manager 的 Group=10001、UMask=0007。安装器只在原 inode 上修 owner/mode，不替换锁文件。部署检查在 Bot UID 下实际 O_RDWR 打开锁文件。GC 通过迭代器只消费前 128 个 pending。
- TGS 使用 64 KiB 有界解压、缓存下一个引号位置的 `bytes.find` 跳过无关字符串，保留根字段和必要的数字。w/h 按 JSON 数字语法解释，128.0→128、1e3→1000。跳过 gzip 可选头后读取 raw deflate，因此坏 CRC、CRC 尾被截去、坏 JSON 尾都不会抹掉已识别前缀；没有整体 JSON 对象或新文件大小上限。
- `event_view` 在读投影中将现有 `file_size` 映射到公开 `size`，已经有 size 时保留。原 segments、请求摘要和持久 hash 不改。
- 附件下载只将 `authorized_path` 返回的真实 `expires_at` 透传到可选 `X-Yuki-Expires-At` 响应头。稳定 artifact 不增加 TTL。HTTP 原流继续是 octet-stream；真实 MIME 用已有元数据，缺省情况由桌面根据文件名、kind 推断。Client API 仍为 7；SDK 9 的更新不推动 Client 版本号。

主要代码：`plugin_host/notification_delivery.py`、`notification_repository.py`、`emoji/delivery.py`、`social/emoji_intents.py`、`emoji/sticker_index.py`、`emoji/media.py`、`workspace/artifact_lock.py`、`workspace/store.py`、`sandbox/deployment_check.py`、`deploy/sandbox/install-manager.sh`、`persistence/event_view.py`、`client_api/files.py`、`client_api/http.py`。container 只新增对已有 WorkspaceStore 的注入。

## 三轮复审

### 第一轮：合同与实际可读性

逐段检查通知 prepared、平台调用、本地 append、artifact ref、Client history/files/download。清除依靠空 native reference 才走本地的旧流程，删除共享层的非空消息号和收据类型门槛。媒体从真实插件原件开始，经过 App 本地通知，在同一授权下走 history→附件 DTO→files 列表→精确 version 下载；错误 version 返回 404。普通 Social 原件保持既有行为。

回归：`test_notification_local_delivery.py`，`test_client_files.py::test_personal_attachment_enters_original_main_and_media_index`。后者同时验证公开大小、实际 expires_at 响应头，以及下载前后原 segments 完全一致。

### 第二轮：竞争、取消与原回执

检查 writer 前准备与 writer 内操作的边界；媒体读取、复制、hash 和分词都在 ledger writer 前。检查原 attempt 的 CAS、lease SQL 时间、取消前后是否真正提交、并发 claim，以及远端晚确认。

新增用例覆盖：本地文本和图片重复 `_deliver` 不重复写入；commit 成功但回执丢失；commit 后取消；commit 前取消；最终 CAS 时 lease 已过期；同一 outbox 重试复用原 artifact；旧 processing 无本地证明不重放；远端过期后 uncertain，再收到带/不带原生号的真实确认；准备后撤销 grant，不留下本地事件。

将审查时的 `test_queue_review.py` 复现改成正确行为回归：首个文字 claim 已提交后取消，finally 会 wake，实际未调用 API 的文字记 failed，图片可以继续。纯图片读取已提交 admission 返回 accepted；真正的原生 unknown 仍是 uncertain。百个 intent 的已有回归保留原 3 秒期限；单独运行通过。此前并行检查时发生过超时，没有靠修改 deadline 消除它。

### 第三轮：资源、平台与格式边界

在隔离 WSL Debian、Python 3.12.15 中检查真实文件和进程。以 root Manager（GID 10001、umask 0007）先创建锁，再由真实 UID 10001 的 Bot 获得共享锁；运行实际安装器片段，确认锁 inode 不变，两方共享持锁时第三方拿不到独占锁。既有双进程发布、杀死发布进程、失败释放锁用例一并通过。GC 用例让第 129 次枚举立即失败，证明只消费 128 项。

TGS 复审发现“简单扣掉最后 8 字节”在 gzip 尾已经缺失时会误扣 payload，已经改用实际 gzip 头与 raw deflate，并补缺尾、可选 header、坏 CRC、转义根键、嵌套同名字段、分块数字回归。早停止用例的 BinaryIO 替身原本无视 read(size) 固定返回 8192 字节，现改为遵守 read(size)，仍禁止累计读取超过 8192 字节。

源码 `ruff format/check` 和 Linux 平台 `mypy` 对本范围文件通过。未新增队列、协调器、持久表、模式开关或新的经验阈值。

## 成本实测

同一隔离 Linux 进程，各实现预热一次、计时七次。输入为根对象中 8 MiB 无关字符串，v/layers/w/h 位于尾部；数字为 128.0、1e3。峰值为 tracemalloc 记录的 Python 分配，不是整机 RSS。

| 实现 | 中位耗时 | Python 峰值 | 读取的 w/h |
| --- | ---: | ---: | --- |
| 本次 raw-deflate 流式嗅探 | 9.48 ms | 245,311 B | 128 / 1000 |
| afb509e7 的逐字节流式嗅探 | 392.61 ms | 49,336 B | 1280 / 13（错误） |
| 整体 gzip 解压 + json.loads 参照 | 21.83 ms | 22,429,393 B | 128.0 / 1000.0 |

本次约为原流式实现的 41.4 倍速度，也是整体 JSON 参照的约 2.3 倍速度。内存仍为固定解压块和小量状态；计时包含解压，完整大字符串不进入 Python 对象树。

另一条真实 8 MiB PNG 原件，在不启用 Vision 时走 cached_path→header 命名→FileWorkspace 实际复制→StickerIndex 实际 catalog，峰值 189,233 B、62.3 ms。验证最终文件大小和 SHA 与源相同，禁止 Path.read_bytes 全量读取，且没有后台 Vision 任务。这一测试未将八兆文件替换成小字节 mock。

## 验证记录

- 第一组后端定向：44 passed、5 个 Windows 平台跳过；另一个失败是测试观察器未接新增 active 参数，已修正。
- 新增通知、首 claim 取消、缓存打开、TGS 回归的 Windows 定向：25 passed。
- 隔离 Linux root 锁/GC、真实 8 MiB 收录、TGS 一组：14 passed，双 UID 用例实际执行，没有跳过。
- 隔离 Linux 通知、Client 原件、队列、缓存、TGS 一组：48 passed，两个 Client 测试预期修正后在最终组通过。
- 最终 gzip 头/缺尾/早停止/原件投影与 TTL 一组：15 passed。
- 本地通知的前 9 项在 Linux 整组通过；最后新增的准备后撤权一项单独通过（原 writer 回滚，下一次正常投递明确 cancelled）。全仓测试、提交、推送、PR 与上线由主任务单独记录。

本机原始证据：`D:/Code/Reports/Yuki/desktop-rich-media-review-20261028/tgs-current-benchmark.json`、`benchmark_tgs_current.py`；隔离 Linux JUnit：`/tmp/yuki-id-validation-20261028/backend-streaming.xml`、`backend-delivery.xml`、`backend-final.xml`、`backend-local-final.xml`、`backend-local-grant.xml`。成本原始样本另保存在仓库 `docs/operations/message-identity-tgs-cost-20261028.json`。复现真实文件回归可运行：

```bash
python -m pytest tests/unit/test_workspace_artifact_lock.py \
  tests/unit/test_sticker_library.py::test_real_eight_mib_original_without_vision_stays_streamed \
  tests/unit/test_notification_local_delivery.py tests/unit/test_emoji_media_facts.py \
  tests/unit/test_emoji_chain_repairs.py tests/unit/test_sticker_text_admission_recovery.py \
  tests/unit/test_sticker_cache_open_race.py \
  tests/unit/test_client_files.py::test_personal_attachment_enters_original_main_and_media_index
```

实际双 UID 用例需在隔离 Linux root 环境运行；普通用户运行会明确跳过该一项。

## 10-29 合并前复审

复现了通知准备与本地发表之间的三个竞争：Presence 停用、Binding 结束、Registry 连接更替。原实现三项均仍会发表；现在将原 `ResolvedSend` 传至最终 writer，在原 attempt/lease CAS 成功后复用 `validate_prepared_send`，校验当前路由后再 append。只使用当前 session 和内存连接快照，不在 writer 中访问文件、网络或另开 reader。校验失败时 sent CAS 与事件一同回滚，恢复仍认原 outbox。远端已确认的效果不受这项本地新发表校验影响。

Windows 两个相关通知模块 19 项通过，新增竞争用例在修复前均失败；Ruff、格式、定向 mypy 和 diff 检查通过。完整 CI、合并、镜像与生产升级分别核对，本次本地回归不代表生产已升级。

最终引用检查补齐两份离线工具：`benchmark_sqlite_writer.py` 的旧 InboundMessage 替换字段和 ORM 列，以及 `sqlite_large_history_seed.py` 的事件、receipt 和计数列。按所选 checkout 的实际表结构一次选择 0155／旧字段，保留同一脚本旧新对照、原报告键、语料、内部 ID、512 行批量和计时边界。直接 bulk 合成的历史没有原生接入证明，native 明确为空；经测试平台入口处理的测量消息保留其真实测试 ingress/native。没有给生产模型增加旧字段 alias。

旧实现四个实际反例分别触发 ORM AttributeError、域消息 replace TypeError、旧 receipt 列 SQL 错误及批量 ingress 唯一键冲突。修后 0155 最小真实 Main＋两行历史验证通过：event／FTS／receipt 各两行，一条测量入站及其原 Social 成功回执可查，workload_errors 为空。模型使用既有 Fake Provider，没有真实付费或生产请求；原始反例和修后结果保存在独立 review 工作区 `.cache/cli-identity-proof-20261029/`。
