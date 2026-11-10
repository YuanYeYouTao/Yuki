# Yuki 沙箱 computer / browser 工具（阶段 1，2026-11-09）

依据[统一 Computer Use 与 Browser Use 任务书](../architecture/Yuki-统一ComputerUse与BrowserUse任务书-2026-11-09.md)
§4.1、§5.1–§5.6、§6.1 与 §7。本记录先列出写代码前的失败情况（含实施时补充的协议级失败），再记录实现与验证。
阶段 0（连接器稳定性）与阶段 3（本人电脑）不在本次范围。

## 写代码前列出的可能失败情况

### 7.1 合同与恢复

- C1 直调与 Code Mode 清单不是 29 项、合同版本未从 35 升到 36、`computer`／`browser` 与 `yuki_<name>` 包装冲突。
- C2 两工具缺 `namespace="sandbox.run"`／`effect="write_state"`／`result_cacheable=false`，截图被缓存或与点击并行。
- C3 给出 `device` 却在 Yuki 环境执行，而不是 `device_not_supported`（且不接触 Manager）。
- C4 同一调用键重试时重新点击／输入，而不是返回保存的结果；读类动作（`screenshot`、`snapshot`、浏览器
  `screenshot`、沙箱 `resume`）重试却得到 `unknown`，或读类动作写了待定行。
- C5 Manager 在待定行与结果之间崩溃（或驱动在执行中退出）后，同一键报告成功或重跑，而不是 `status:"unknown"`；
  照搬 `display_wake` 的“待定即重跑”。
- C6 bot 侧 7 s RPC 截止先到，bot 报“不确定”而 Manager 仍在排队执行；Manager 有界等待到期后，驱动又执行了仍在
  请求目录中的旧请求。
- C7 两个 Work 同时操作，一方得到 `control_held`，而不是在 Manager 内串行；遗留的 agent 租约（Manager 在动作中途
  崩溃）使之后每次输入都报 `control_held`。
- C8 本人接管后下一次输入仍执行（xdotool 路径、节点解析与派发之间、拖拽／输入框替换的多步输入中途）；交还后
  `wait-control` 不完成。
- C9 动作失败／超时／驱动不可用后 agent 租约未释放；撤销 Desktop 设备后本人租约未释放（原有路径，不得回归）。

### 7.2 图像

- M1 Chat Completions、Claude Messages、Gemini、Responses 任一方言没有把截图带进请求，或替换后的旧图仍在请求体中。
- M2 截图以 `source="workspace"` 发送，下一帧覆盖后按文件版本复核失败；`view_file(device)` 不再归档；`read_result`
  读回的图像被重复归档；删除 `local_media` 特例后插件图像不再归档。
- M3 截图超过 Manager 响应行缓冲或被 Manager 子进程 32 KiB 输出截断（截图不得经 `docker exec` 输出返回）。
- M4 不支持图像的模型丢失点击效果（应 `media_read:false`、`media_error` 且效果保留）。
- M5 30 步截图循环在第 17 张时被请求准入拒绝；被替换的旧图无法用 `media_artifact_handle` 经 `read_result` 重读；
  Responses 续写链把已在续写载荷中的图像重复计入或漏计；替换只作用于一处预算（派发或准入）。
- M6 图像坐标与点击坐标不一致（Host 二次缩放、窗口原点）；并行 Work 使用对方截图的坐标（不得保存“最近一次截图”）。

### 7.3 浏览器

- B1 页面加载完成前快照；跳转后旧节点点中错误元素（应 `node_stale` 并附新快照）。
- B2 跨站 iframe 表单不可达；OOPIF 节点 ID 在 frame 增减后指向错误元素。
- B3 JS 对话框使调用挂起；`accept_dialog`／`dismiss_dialog` 未用 `Page.handleJavaScriptDialog`；回车不提交表单；
  文件输入弹出原生选择框。
- B4 下载落在 `/workspace/downloads` 之外或未返回路径；同一键回放再次触发下载。
- B5 新标签页不可达或归属错误（应按 `openerId` 归属开它的 Work）。
- B6 巨大页面的快照被截断，而不是经既有结果预算与 `read_result` 续读。
- B7 中文与 Unicode 输入在浏览器（`Input.insertText`）或 xdotool 中出错。
- （B8 属本人电脑专用 Chrome，阶段 3。）

### 7.6 历史与界面

- H1 Client API 的 Work 步骤未把 `computer`／`browser` 归入「操作電腦」「使用瀏覽器」；旧 `run_command`／
  `yuki-computer` 回执（`step_label`）与 help run 投影变化。
- H2 文档、提示词或夹具仍引用 `terminal_exec` 或 `yuki-computer` 浏览器流程（`run_command` 说明、
  `desktop_computer_work_fixture.py`、`desktop_browser_work.py`、运维文档）。
- H3 `desktop-headed` CI job 没有运行新 E2E 或没有上传 `test-results/computer-browser/`。

### 实施时补充（Manager ↔ 长驻驱动）

- P1 驱动经 runtime 挂载取请求：Manager 写请求文件后崩溃，重启后同键只剩待定行 → `unknown`，且旧请求文件须被
  移除，不能在之后被驱动执行。
- P2 Manager 等待到期时请求仍未被驱动领取：确定未执行，删除请求文件后报 `timeout`（不带 `unknown`）；已被领取
  才是 `unknown`。
- P3 驱动结果文件、图像文件经符号链接把 Manager 引到挂载外（Manager 读写一律经 `FileWorkspace` 检查过的 FD）。
- P4 runtime 卷只有约 128 MiB：结果图像读后即删；下载与截图产物不写入 runtime 卷。
- P5 无头模式：`computer` 返回 `display_unavailable`，`browser` 照常工作并可截图。
- P6 桌面服务停止时：`computer`／`browser` 先按原 `display_wake` 路径唤醒，再执行；唤醒失败返回
  `desktop_unavailable`／`browser_unavailable`，不留下待执行请求。
- P7 Desktop 的 `tabs_list` 失去 Work 归属（原来由 CLI 的 `tabs.json` 提供，现由长驻驱动写出）。
- P8 驱动等待 CDP 回应无时限（原 `recv()` 无超时）；对话框打开时其他调用挂起（应返回 `dialog_open`）。
- P9 单个请求在驱动中抛出未归类的异常（例如请求缺 `max_dimension`）：不得结束驱动、连带重启整个桌面服务；
  该请求以 `driver_failed` 回答，输入类附 `status:"unknown"`（不知道出错前已做了多少输入），读类不附；栈写进
  桌面服务输出；之后的请求照常执行。（2026-11-09 上线冒烟时，缺参数的截屏让驱动退出，桌面服务整体重启。）

### 实施补充（Host 图像，§5.2／§5.3）

核对代码后发现任务书“Responses 续写中已由 provider 持有的历史图像不随本次请求重发”与实现不符：四种方言都由
Host 重放自己保存的续写载荷（Responses `store:false`、无 `previous_response_id`），工具媒体观察一经发出就并入
载荷、之后每次请求都重发。据此补充：

- M7 只替换可移植 `ChatMessage` 中的旧图，续写载荷（Chat Completions `image_url`、Claude `image`、Gemini
  `inlineData`、Responses `input_image`）里的旧图照旧重发且不计数。
- M8 相邻两次请求对同一旧观察的替换结果不同（截止点后退、改写文本不一），破坏缓存前缀；一经替换必须保持替换。
- M9 `append_tool_media` 按整条转写去重：`read_result` 读回的图像与已替换的原观察相等（同像素、同句柄），
  重读不产生新观察。
- M10 `_validate_tool_media` 在没有选中图像时回退为复核转写中的全部图像，已替换、文件已变化的旧工作区图使整次
  请求失败。
- M11 派发预算把将被替换的旧观察计入而拒收新截图；或完全不计，放过本身与入站图合计已超限的单个结果。
- M12 入站（current／reply）图像被替换；它们单独超限时应仍为 `media_request_budget_exceeded`。
- M13 工作区／历史图像没有句柄，替换文字未给出重看所需的 path／artifact_id／event_id。
- M14 替换改写了转写或 journal 中保存的原条目，而不只是本次发出的请求。
- M15 容量估算与压缩触发按未替换的请求计算；续写载荷中的图像按 base64 字符计入容量（原只有 Gemini 按媒体计），
  一两张真实尺寸截图就越过压缩触发值；压缩摘要请求携带前缀中的全部旧图。

## 设计落实

### 合同（§5.1）

- `computer`、`browser` 加入 `SANDBOX_TOOLS`（`namespace="sandbox.run"`、`effect="write_state"`、不缓存），
  参数按任务书一次冻结；合同版本 35 → 36，直调与 Code Mode 直调视图都增两名，`CODE_API_REVISION` 不变。
  给出 `device` 时 Bot 直接返回 `device_not_supported`，不接触 Manager。`run_command` 说明删去 `yuki-computer`。
- 成功结果是业务数据本身，截图作为 `source="tool"` 的 `PreparedMediaData` 像素（按 §5.2 归档，结果带
  `media_artifact_handle`）；成功的输入动作带 `mutation_committed=True`，无图像能力或归档失败时只报
  `media_read:false`，效果保留（M4）。失败为 `{ok:false,error,message}`，可能已发生时 `status:"unknown"`。
- Work 步骤投影：`computer` →「操作電腦」，`browser` →「使用瀏覽器」；旧 `step_label` 回执照旧投影。

### Manager ↔ 长驻驱动（§5.4／§5.6）

- Bot 调 Manager RPC `computer_action`／`browser_action`，不设 bot 侧截止、使用大响应缓冲；传输失败时
  只有输入类报 `unknown`，读类不报（C4）。
- Manager（`computer_runtime.py`）按环境用一把 `asyncio.Lock` 串行（C7）。输入类：同键已有结果直接返回；
  只有待定行时报 `action_unknown`／`status:"unknown"` 并撤回尚未被领取的请求文件（C5／P1），绝不再执行；
  否则写待定行 → 进程内 `agent_acquire`（actor `{request_id, work_id}`，不再要求 run）→ 写请求 →
  等结果 → 写结果行 → `finally` 释放租约（C9）。本人持有时直接 `control_held`，不写请求（C8）。
  遗留的 agent 租约（Manager 中途结束）在下一次取得时被替换，不再报成人类接管（C7）。
  读类（截图、快照、浏览器截图、沙箱 `resume`）不写待定行、不取租约，本人持有时照常可读。
- 协议在 runtime 挂载 `computer/driver/` 下：Manager 原子写 `requests/<id>.json`；驱动以 `rename` 领取到
  `taken/`，执行后写 `results/<id>.jpg`（如有）与 `results/<id>.json`，Manager 经 `FileWorkspace` 读取并删除
  （P3／P4）。Manager 有界等待到期时，请求仍可删除即确定未执行（`timeout`，无 `unknown`，P2）；已被领取
  才是 `unknown`（C6）。驱动启动时删除遗留 `taken/` 而不执行。新请求开始前清理上一次放弃等待的结果文件。
- 驱动：`desktop_service.py` 在 CDP 就绪后把 `yuki_computer.py serve` 作为子进程托管；浏览器部分是只依赖
  标准库的 `yuki_agent_connector/cdp.py`（自带 RFC 6455 客户端，所有接收带时限，P8），屏幕部分是 xdotool／
  ImageMagick。每次输入（含 xdotool）前复核 `control.json` 的租约（C8）。驱动写 `driver/tabs.json` 供
  Desktop 电脑页与 `yuki-computer status` 读取标签页及 Work 归属（P7）。桌面服务未运行时第一次动作沿
  `display_wake` 唤醒（P6）；无头模式 `computer` 返回 `display_unavailable`（P5）。
- 删除：文件命令协议（`collect_commands`／`read_command`／`commands/`）、按 run 取放租约、`finish_run` 的
  agent 释放、CLI 的 `wake`／`sleep`／`screenshot`／`browser`／`input`、`tabs.json` 记录与 websockets 依赖；
  保留 `observe_display()`、`help-request`、`wait-control`、`status`。
- 沙箱 `resume`：沙箱没有“本人叫停”状态（本人接管与交还仍是 Desktop 操作），`resume` 是一次读取，
  返回与 `screenshot` 相同的结果。

### 实施中发现、写回的事实

- `mousemove --sync` 在指针已在目标位置时永不返回（E2E 第二次点同一坐标时超时）；删去 `--sync`，
  X 服务器按序先移动后点击。
- 原 `yuki_computer.py` 使用的是 `websockets` 包，不是标准库 WebSocket；驱动另写了标准库客户端。
- 导航可能把标签页换到新渲染进程，`backendDOMNodeId` 从头编号；驱动只接受自己为当前文档发出的节点 ID，
  其他一律 `node_stale`。后台标签页不出帧，动作与截图前 `Page.bringToFront`。
- xdotool 只有一个临时 keycode，"中文" 会打成 "中中"；驱动为缺失字符逐个绑定空闲 keycode 后再输入。

### 图像（§5.2／§5.3）

- 归档：`ToolResultBudgeter` 归档所有无 `tool_handle` 且来源非 `workspace`／`history` 的成功结果图像，
  删除 `local_media` 特例与 Provider 条件；`view_file(device)` 改标 `source="tool"`。
- 替换：`llm/request_media.py` 的 `carry_request_media` 在组装每个主请求（含压缩摘要请求）时，从最新的
  工具媒体观察往前保留到现有帧／字节上限，更早的观察保留原文字（含 `call_id`、`media_artifact_handle`
  或 path）并追加一句未发送说明；同时改写续写载荷中的图像部分。准入与派发都按替换后实际携带的图像计。
- 与任务书措辞的偏离：Responses 没有 provider 侧持有的历史图像（`store:false`，Host 重放整份续写），
  所以载荷中的旧图同样计数并替换。详见[结果预算](../architecture/tool-results.md#工具图像与长循环)。

## 验证

- 全应用（有头镜像，CI `desktop-headed` job）：`tests/integration/test_computer_browser_e2e.py` 以脚本化主模型
  驱动离线完整应用（App 私聊 → Work）、真实 Manager（`NativeManager`）、Xvnc、Openbox、Chromium 与驱动，
  对本地夹具站点完成：多步表单＋`<select>`＋回车提交、alert／confirm／prompt（对话框打开时其他动作得
  `dialog_open`）、下载、文件输入（无原生对话框）、`127.0.0.1` 页内 `localhost` 跨站 iframe 表单（OOPIF
  节点 ID）、`target=_blank` 第二个标签页，以及 30 步 `computer` 循环（窗口截图、按所附图像坐标点击、
  xdotool 中文输入）；第 13 步前本人经已配对 Desktop 设备的 Client API 接管，下一次输入得
  `control_held`，Yuki 以 `yuki-computer wait-control` 等原回执，本人交还后继续。断言：清单 29 项、站点
  输入日志、下载路径、每个输入都有最终回执且无遗留租约、每次请求至多 16 帧且最旧观察被替换为带
  `media_artifact_handle` 的文字、Work 步骤归入新类别。产物 `test-results/computer-browser/`：`manifest`、
  `frames/`（每次观察一帧）、`requests`（每次请求的帧数、观察文字与尾部消息）、`calls`、`log`（站点输入）、
  `timeline`（租约）、`receipts`（Manager 回执）、`downloads`、`steps`、`outcome`。
- 驱动：`tests/unit/test_computer_driver.py` 直接对真实 Chromium 与本地站点覆盖 B1–B7、C8、P1、P5、P9 与屏幕
  坐标换算（M6）；`tests/unit/test_computer_actions.py` 覆盖 Manager 侧 C4–C9、P1、P2、P7（真实 SQLite 与
  租约，驱动替换为按同一文件协议领取的线程）。
- 图像（本地，离线）：`tests/integration/test_tool_media_long_loop_e2e.py` 以真实 `AgentRunner`、真实
  `TaskModelExecutor`、四种真实方言适配器（Chat Completions 分无续写与带 reasoning_details 续写两种）经
  `httpx.MockTransport` 各跑 30 轮截图加一次 `read_result`；覆盖 M1、M2、M4、M5、M7–M15（也在 `desktop-headed` job 运行，与上面的产物一并上传）。产物
  `test-results/computer-browser/media/<dialect>.json`（每次请求体，base64 缩为 hash＋长度，替换文字，
  每次携带帧数与字节）、`no_image_input.json` 与 `workspace_and_oversized_<dialect>.json`。
