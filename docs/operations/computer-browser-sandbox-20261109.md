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

（实现完成后补写：Manager ↔ 驱动请求协议、结果形状、`resume` 在沙箱中的行为、图像替换规则与 Responses 续写。）

### 图像（§5.2／§5.3）

- 归档：`ToolResultBudgeter` 归档所有无 `tool_handle` 且来源非 `workspace`／`history` 的成功结果图像，
  删除 `local_media` 特例与 Provider 条件；`view_file(device)` 改标 `source="tool"`。
- 替换：`llm/request_media.py` 的 `carry_request_media` 在组装每个主请求（含压缩摘要请求）时，从最新的
  工具媒体观察往前保留到现有帧／字节上限，更早的观察保留原文字（含 `call_id`、`media_artifact_handle`
  或 path）并追加一句未发送说明；同时改写续写载荷中的图像部分。准入与派发都按替换后实际携带的图像计。
- 与任务书措辞的偏离：Responses 没有 provider 侧持有的历史图像（`store:false`，Host 重放整份续写），
  所以载荷中的旧图同样计数并替换。详见[结果预算](../architecture/tool-results.md#工具图像与长循环)。

## 验证

（实现完成后补写。）

- 图像（本地，离线）：`tests/integration/test_tool_media_long_loop_e2e.py` 以真实 `AgentRunner`、真实
  `TaskModelExecutor`、四种真实方言适配器（Chat Completions 分无续写与带 reasoning_details 续写两种）经
  `httpx.MockTransport` 各跑 30 轮截图加一次 `read_result`；覆盖 M1、M2、M4、M5、M7–M15。产物
  `test-results/computer-browser/media/<dialect>.json`（每次请求体，base64 缩为 hash＋长度，替换文字，
  每次携带帧数与字节）、`no_image_input.json` 与 `workspace_and_oversized_<dialect>.json`。
