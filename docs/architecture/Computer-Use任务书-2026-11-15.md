# Computer Use 任务书

日期：2026-11-15。源码基线：Yuki `main` `1ea35334`（连接器 `0.6.2`，工具合同版本 37，Client API revision 12）；Yuki-Desktop `main` `87c55da`。执行前以 origin/main 为准。

状态：用户已批准（第 17 节已定 1–12）。后端 P0–P5 已在 `feat/computer-use`（PR #622）本地实现并通过本地与 CI 验证（P0 结果与修订见 13.1）；桌面端 PR（P6）与真机验收（P7，需用户授权付费模型与操作真机）未做。合并与上线另行记录。实现在同一分支 `feat/computer-use` 的后端 PR 内进行；桌面端另开一个 PR（Yuki-Desktop 仓库）。合并与上线另行记录。

实施前阅读[共同开发约束](development-contract.md)、[Tool Kernel](tool-kernel.md)、[个人 API](client-api.md)、[统一 Computer Use 与 Browser Use 任务书](Yuki-统一ComputerUse与BrowserUse任务书-2026-11-09.md)（本稿的基线，§5.2「按当下几何换算、不保存最近一次截图」与 §5.5「与本人共用桌面」继续有效）、[桌面电脑与有头环境](../operations/desktop-computer.zh-CN.md)、[本机连接器](../operations/local-agent-connector.zh-CN.md)、[3b 后台窗口 spike](../operations/computer-3b-spike-20261110.md)。

## 1. 结论

定位（用户已定）：

- 一个后端大 PR 加一个桌面端 PR，目标是完整、达到业界领先水平的 computer use。后端 PR 覆盖 Bot、Yuki 自己电脑的驱动（`yuki_computer.py`，随 Bot 版本从代码卷挂入环境）和 `yuki-agent-connector` 0.7.0。桌面端 PR 内置新连接器并重新打包，如有需要再展示新的回显。
- 继续是同一组 `computer`／`browser` 工具、同一份固定合同，与 Provider 无关。不接各家私有 computer-use 协议，不按模型 Profile 换算坐标，不按内容路由。

本稿交付：

1. **P0 截图尺寸**：`computer` 截图使用自己的上限，与聊天图片的 `vision_*` 分开，默认长边 1568、约 1.15MP。改完后，模型看到的就是这张图本身，任何 Provider 都不会再在服务端缩它，"坐标是所附截图的像素"这句合同在所有模型上重新成立。两条执行路径都生效，旧连接器也立即生效。
2. **坐标约定与回显**：增加可选 `scale`（只允许 1000）。超出所声明坐标系的坐标直接报错。每个输入动作回显 `point` 和 `scale`；截图结果回显宽、高和坐标系说明。
3. **看清细节**：`zoom(region)` 返回高清局部图，坐标仍按整张截图计；截图画出鼠标指针。
4. **动作补齐**：click 类动作支持修饰键，新增 `triple_click`、`middle_click`；`key` 支持 `repeat`；两条路径统一按键语法；Linux 拖拽补插值移动。Yuki 自己的电脑上新增 `paste`（经她自己的剪贴板输入长文本，6.12）。
5. **动作结果的事实**：`route`（走的是哪条投递途径）、`hit`（落点控件的 role 和 name）。只做回显，不设门槛。
6. **坐标之外的定位**：读动作 `elements` 按窗口列出无障碍节点（AX／UIA／AT-SPI），点击、输入等动作可以传 `element`。
7. **`device_not_supported` 增加事实原因 `cause`**（实现时由 `reason` 改名，见 13.1）；连接器经 `device_capabilities` 公布新能力，服务器按事实拒绝旧连接器不支持的参数，避免它们被静默忽略。
8. **提示词与工具说明**：告诉模型怎样用回显和截图核对每一步、怎样避免谎报成功。只写成提示，不设门槛。
9. **合同版本 37 → 38**。

## 2. 现状事实

源码只读核对（2026-11-15）。

| 事实 | 证据 |
| --- | --- |
| 截图尺寸由 `image_scale(w,h,max_dimension,max_pixels)` 决定，只缩不放。Bot 每次调用都把 `vision_max_dimension`／`vision_max_pixels` 传给环境驱动和连接器 | `agent_tools.py:1777-1796`；`local_device.py:93-123`；`cdp.py:44-46`；`screen.py:87-100` |
| 默认 `VISION_MAX_DIMENSION=4096`、`VISION_MAX_PIXELS=16777216`，与聊天图片预处理共用（`media.py:59-60`）。因此 2560×1440 原样发出 | `config.py:336-337`；`.env.example:352-353` |
| 方言不控制服务端缩图：Claude 图像块不带 `transformations`，Chat Completions 的 `image_url` 不带 `detail`，Gemini 的 `inlineData` 不带 `mediaResolution`。截图以 `role=user` 的"Host 工具媒体观察"追加，不放在 tool_result 里 | `anthropic_messages.py:113-122`；`openai_compatible.py:73`；`gemini.py:145`；11-09 任务书 §4.1 |
| 按官方规则，标准档 Claude 会把 2560×1440 缩成 1456×819 再交给模型，模型的像素坐标随之落在缩小图上，偏约 1.76 倍；gpt-5.4 的 auto 等同 high（≤2048），缩成 2048×1152；tile 类模型缩到短边 768。高分档 Claude（4.7 及以后）和 gpt-5.x 的 `original` 碰巧不缩 | 第 3 节来源 [2][4] |
| 用 gemini-3.8-flash 实测：模型输出 0–1000 归一化坐标，一次还出现"归一化 y 配像素 x"，点击落空 | 用户观察 |
| 坐标按动作执行时读到的几何换算（Retina、Windows DPI、多屏负坐标、窗口原点），不保存最近一次截图 | `screen.py` 模块文档串；`yuki_computer.py:314-327` |
| 动作集：`screenshot click double_click right_click move drag type key scroll resume`，外加自己电脑专用的 `wake sleep snapshot restore`。没有修饰键点击、三击、中键、按键重复、zoom、无障碍元素 | `environment_tools.py:133-152` |
| 按键语法两边不同：Linux 把 `text` 原样交给 `xdotool key`，连接器经 `parse_keys` 白名单解析 | `yuki_computer.py:347`；`screen.py:166-182` |
| Linux 拖拽是 mousedown 后直接跳到终点，中间没有移动事件；macOS 后台分 12 步插值 | `yuki_computer.py:337-340`；`screen_macos.py:752-765` |
| 截图里的光标：macOS 全屏截图带光标，macOS 窗口截图不带；Windows 用 GDI BitBlt，没有绘制光标；Linux 用 ImageMagick `import`，不含 X 光标（推断，P0 核实） | `screen_macos.py:396,604`；`screen_windows.py:394-399` |
| 输入结果只有 `submitted:true`、`width`、`height`、`control` 和动作后截图；不回显落点、投递途径或命中的控件 | `desk.py:305-340`；`yuki_computer.py:367-416` |
| macOS 后台投递：先在目标窗口自己的 AX 子树里找元素做 AXPress、AXShowMenu、AXFocused，找不到才发带窗口标记的事件。窗口不在本人当前 Space 时动作集为空，返回 `device_not_supported`；这一结论与 Cua 一致 | `screen_macos.py:1-15,562,699-713`；第 3 节 [10] |
| Windows 后台只能经 UIA 投递 click、scroll、type，type 要先给 x,y 指向输入框；move、drag、key 返回 `device_not_supported`；不退回前台 | `screen_windows.py:700-894` |
| `device_not_supported` 至少有七种来源，模型分不出原因：生命周期动作带了 device、连接器所在平台没有 GUI、窗口不在当前 Space、Windows 某种动作没有后台途径、落点没有 UIA 控件、没有已聚焦的输入框、连接器太旧 | `agent_tools.py:1769-1773`；`desk.py:195-199,272-278`；`screen_windows.py:789-894`；`local_device.py:137-142` |
| 连接器对未知参数用 `args.get` 读取，不校验。旧连接器会**静默忽略**新参数（例如 `scale`），点击会落到错误位置 | `desk.py:184-213` |
| 连接器把能力事实放在 `device_capabilities`（例如 `"windows": True`），服务器据此判断 | `connector.py:247-249`；`desk.py:137-147` |
| 本人的停止与继续、`owner_active` 让出、`control_held`、`status:"unknown"`、`target_elevated`、权限缺失都已有结构化错误 | `desk.py` |
| 旧截图在请求组装时从最老的开始，超出帧数或字节上限就替换为文字 | `request_media.py:209-232`；`turn_execution.py:996-999` |
| 每个 `computer` 调用只执行一个动作，结果都附一张截图；同一模型回复里的多个调用各有调用键和回执，按环境或设备串行执行 | `environment_tools.py:126`；`desk.py:212` |
| Yuki 自己电脑的 Xvnc 分辨率 1440×900；有头档装有 xdotool、x11-utils、imagemagick，没有 AT-SPI | `desktop_service.py:108-109`；`environment_programs.py:18-30` |
| 网页已有按节点操作的 `browser`（`[e…]` 节点，节点失效时返回 `node_stale` 并附新快照） | `environment_tools.py:170-205` |
| CI：`connector` 任务在 ubuntu／windows／macos 三平台跑测试并打开一个真实的专用 Chrome 窗口；`desktop-headed` 在有头镜像里跑真实的 Xvnc、Chromium 和驱动；macOS 托管 runner 无法授予 TCC 权限 | `.github/workflows/quality.yml:66-105,183-204` |
| 桌面端由 `build-tools/prepare-connector.mjs` 从显式指定的后端 checkout 构建随附的连接器运行时 | Yuki-Desktop `build-tools/prepare-connector.mjs` |

## 3. 业界对照（调研结论）

| 维度 | 业界做法 | 本稿取舍 |
| --- | --- | --- |
| 截图尺寸与坐标 | Anthropic：Claude 返回的是"它实际处理的那张图"上的像素；标准档长边 1568、1568 视觉 token，高分档 2576、4784；computer use 工具要求应用自己先缩图，再把坐标按比例放大回去 [1][2]。OpenAI：缩到 1440×900 或 1600×900 效果好，用 `detail:"original"` [3][4]。Claude Code：自动缩到约 1372×887，不可配置 [7]。官方 demo 和 Skyvern 缩到 XGA／WXGA 一档 [11][18] | 本地固定上限 1568／1.15MP（Anthropic 文档的示例规则），两条路径的图都由来源直接产出最终尺寸 |
| 坐标约定 | Claude 和 OpenAI 用像素；Anthropic 明说 Claude 用 0–1000 归一化坐标效果差 [2]。Gemini、Qwen3-VL、GLM-V、豆包 Seed 用 0–1000 或 0–999 [6]。Qwen 官方 cookbook 和 Cua 的通用 loop 都在工具描述里声明"屏幕分辨率 1000×1000"来引导归一化模型 | 默认像素，可选 `scale:1000`；声明由模型显式给出，不按数值猜 |
| 动作集 | Anthropic toolset 有 17 个成员：修饰键点击、triple／middle、`left_mouse_down/up`、`hold_key`、`key.repeat`、`wait`、`cursor_position`、`zoom` [1]。Gemini 桌面版有 triple／middle、mouse_down/up、key_down/up、hotkey、`type.press_enter` [6]。Bytebot 有 `holdKeys`、`clickCount`、`paste_text` [12]。Agent S3 有 `num_clicks`、`hold_keys`、`hold_and_press` [13] | 第 6.4 节逐项取舍 |
| 截图画光标 | Anthropic demo 用 `scrot -p` / `gnome-screenshot -p` [11]；Cua driver 画光标覆盖层 [10] | Linux、Windows 补上；后台窗口模式只回显 point |
| 动作结果 | Anthropic：Claude 有时把没核对的动作当成功，应要求它每步截图后用一句话说明画面并判断成败；批量动作第一个失败就停 [1]。Cua：每个动作返回 `effect`、`route`、`delivery`、`evidence`，以及解析后的落点和命中元素 [10] | 回显 `point`、`route`、`hit`；只回显事实，不推断效果 |
| 后台与焦点 | Claude Code 走前台：隐藏其他应用，Esc 全局停止，同一时间只允许一个会话 [7]。Cua 走后台：先 AX，再精确窗口事件，不在当前 Space 的窗口只观察，拒绝静默退回全局输入 [10] | 保持现有后台设计；把 `device_not_supported` 的原因细分 |
| 坐标以外的定位 | browser-use 1.0 先查无障碍树再点击盒子中心 [15]；Claude in Chrome 用 `read_page`／`find` 的 ref [19]；UFO2 以控件 ID 为主、0–1 比例坐标兜底 [17]；Cua `get_window_state` 同时给 AX 树和截图 [10] | 新增 `elements` 和 `element` 参数 |
| 人机协作 | Operator：接管、确认、watch mode [9]；Gemini `require_confirmation` [6]；Claude Code 按应用授权 [7]；Claude in Chrome 某些动作始终要确认 [8] | 不做强制确认和按应用授权（第 16 节） |
| 批量 | OpenAI `actions[]` [5]；Anthropic 第一个失败就停，只在批末附截图 [1]；Gemini 一轮可返回多个调用 [6] | 不做（第 6.8 节） |

## 4. 目标

- 同一个界面任务换用不同模型（标准档 Claude、gemini-3.8-flash、gpt-5.x、Qwen 系）时，点击落点由模型能力决定，不再取决于 Provider 怎样缩图。
- 模型每一步都能从回执知道点到了哪里、走了哪条途径、点中了什么，并能从截图核实效果；"已投递"与"已生效"始终分开。
- 小字、密集界面能看清；能用无障碍节点时可以不靠坐标。
- 两条执行路径（Yuki 自己的电脑、本人电脑）合同一致，差别只来自平台的真实能力，并以事实报告。
- 不新增鉴权、确认或审批，不改变原回执、恢复和让出的语义。

## 5. 设计约束

- **固定合同**：只在现有 `computer` 上增加动作和参数，部署冻结的清单随版本整体更新一次（37 → 38）。不新增工具。
- **不按内容路由、不猜**：坐标约定只认模型显式给出的 `scale`。不按坐标数值、模型名或 Profile 推断约定或换算。
- **事实回显不是门槛**：`point`、`route`、`hit`、`cursor` 只回显。取不到 `hit` 时省略该字段，不阻止输入，也不把它判为失败。
- **按事实拒绝**：坐标超出所声明的坐标系、元素已失效、平台没有这条途径、连接器缺少能力，都在投递前以明确错误返回，`status` 为 `failed`（没有任何输入发生）。不做自动纠偏、重试或降级。
- **恢复语义不变**：输入动作的回执、按调用键重试返回原结果、`unknown` 不重放、`owner_active` 让出、`control_held`，全部沿用。新动作只是新的输入或读取。
- **几何按当下读取**（11-09 任务书 §5.2）：不保存最近一次截图，不把调用绑定到某张截图（与 Cua 的 `capture_id` 不同，理由见第 17 节）。
- **只缩不放**：任何图像都不放大到原生像素以上。

## 6. 设计

### 6.1 截图尺寸（P0）

- 新增两项配置 `COMPUTER_SCREENSHOT_MAX_DIMENSION=1568`、`COMPUTER_SCREENSHOT_MAX_PIXELS=1150000`，热配置键 `computer.screenshot_max_dimension`／`computer.screenshot_max_pixels`，接入方式与 `VISION_MAX_DIMENSION` 相同。`_screen_action` 下发时改用这两项；`vision_*` 只管聊天图片。
- 两条路径的来源（环境驱动、连接器）照旧按下发的值直接产出最终尺寸。连接器 0.6.x 本来就逐次读取 `max_dimension`／`max_pixels`，**升级 Bot 后旧连接器立即生效**，不需要发新版。
- 结果：2560×1440 → 1430×804；Retina 3456×2234 → 1334×862；Yuki 自己电脑的 1440×900 → 1356×848。标准档 Claude 的视觉 token 都在 1568 以内（1488–1519），不会再被服务端缩放。
- 代价：高分档 Claude 和 gpt-5.x `original` 原本能看到 2560×1440，现在少了细节，由 `zoom` 补回（6.3）。
- 方言层（第 17 节已定 6）：工具截图（`ChatImage.source == "tool"`）在 OpenAI（`openai`、`azure_openai` 预设）的 Chat Completions `image_url` 与 Responses `input_image` 上带 `detail:"original"`，模型看到的就是来源产出的那张图；这是 vendor 的方言选项 `tool_image_detail`，其他 OpenAI 兼容预设默认不发，可在 Profile 显式打开。Anthropic 不加 `oversized_image`、`transformations`。Gemini 增加方言选项 `tool_image_media_resolution`（默认不发，即服务端默认 1120 token），只作用于工具截图的 part 级 `mediaResolution`；真机验收里对 gemini-3.8-flash 做默认与 `ultra_high` 的 A/B，再按数据决定默认值。它是 Profile 的部署设置，不按内容、模型名或任务路由。
- 已知残留：tile 类旧模型（gpt-4o／4.1／5.1 的 high）会把短边超过 768 的图再缩一次（16:9 下约 4.5%）。不为此再加第三种上限，在 P0 记录。

### 6.2 坐标约定与回显

**参数**：`scale`，整数，只允许 1000，可省略。

- 省略时：`x,y,x2,y2,region` 是本次结果所附图像（整屏或 `window` 图）的像素，原点在左上角，合法范围 `0 ≤ x < width`、`0 ≤ y < height`。`width`、`height` 按执行时的几何算出，与这次动作后附的截图一致。
- `scale=1000` 时：同一组坐标按 0–1000 解释，两个轴都适用；换算为 `px = min(round(v × width / 1000), width − 1)`，合法范围 `0 ≤ v ≤ 1000`。
- `dx`、`dy`（滚动格数）、`repeat` 不受 `scale` 影响。
- 超出范围时返回 `coordinate_out_of_range`，`status:"failed"`，不投递任何输入。错误附 `coordinates`（坐标系说明）和 `width`、`height`，例如"坐标是本次截图的像素：宽 1430、高 804（x 0–1429，y 0–803）；使用 0–1000 归一化坐标时请传 scale=1000"。
- 校验在驱动或连接器内、几何读出之后、第一个输入之前做，所以只会以 `failed` 结束，不会是 `unknown`。

**回显**（每个带图的结果）：

- `width`、`height`（已有），以及 `coordinates`：一句固定说明"x,y 为本图像素，左上为原点；传 scale=1000 时为 0–1000"。
- 输入动作另加：`point:[x,y]`（换算后的本图像素；drag 另有 `point2`），`scale`（1000 或 null）。用 `element` 时 `point` 是实际使用的元素中心。只有不带坐标的 `type`、`key` 没有 `point`。
- 全屏截图另加 `cursor:[x,y]`：指针在本显示器上时，给出它在本图上的像素位置；不在本显示器上时省略。这替代了单独的 `cursor_position` 动作（6.4）。

**混用约定时的稳健性**：超出范围的那部分（例如在 1430×804 上传了归一化的 y=900）会被拒绝，错误里带着坐标系说明。两个值都在范围内的混用无法从数值区分，按所声明的坐标系执行，靠 `point` 回显、截图里画出的指针和提示词让模型在下一步自己发现。这是接受的残留风险（失败情况 C10），不加启发式。

### 6.3 zoom

- `computer(action="zoom", region:[x0,y0,x1,y1], window?, display?, scale?, device?)`：`region` 用整张截图的坐标（受 `scale` 约束，按 6.2 校验，要求 `x0<x1`、`y0<y1`）。
- 来源按原生像素截取该区域，再按同样的上限缩放，只缩不放。结果：`{region, frame:{width,height}, width, height, coordinates:"zoom 图只用于查看；后续坐标仍用整张截图（宽…高…）"}` 加局部图。
- zoom 是读动作：不写回执、不取租约，本人叫停期间照常可用（与 screenshot 相同，`READ_ACTIONS` 增加 `zoom`）。
- 实现：Linux 用 `import -window … -crop`；macOS 全屏与窗口都用 SCK `sourceRect`（P0 第 8 项）；Windows 对子区域做 StretchBlt，窗口用 PrintWindow 后裁剪。

### 6.4 动作补齐与取舍

| 项 | 决定 | 理由与语义 |
| --- | --- | --- |
| click 类修饰键 | **做** | `click`、`double_click`、`triple_click`、`right_click`、`middle_click`、`scroll`、`drag` 可带 `text`：修饰键 `shift`、`ctrl`、`alt`、`cmd`（`super`、`win`、`meta`、`option` 按现有别名），用 `+` 连接，只在这一次点击或滚动期间按住（与 Anthropic 同义）。投递失败时也保证释放（finally）。多选、范围选、ctrl+滚轮缩放都依赖它 |
| `triple_click` | **做** | 选中整行或整段。Linux 用 `click --repeat 3`；macOS 前台和后台都发 clickState 1→3；Windows 前台在系统双击间隔内发三次 SendInput；Windows 后台没有途径 |
| `middle_click` | **做** | Linux 用按键 2；macOS 用 other-mouse 事件（后台带窗口标记）；Windows 前台可用，后台没有途径 |
| `key.repeat` | **做** | 1–100，默认 1。每次重复前检查本人的停止标志；中途叫停时余下的不再发送，按已发送情况报 `unknown`，与 type 的分段一致 |
| 统一按键语法 | **做** | `parse_keys` 移到只依赖标准库的新模块 `yuki_agent_connector/actions.py`（P0 后定名，13.1），与 `cdp.py` 一样挂入环境。Linux 先经它解析，再映射成 xdotool keysym。接受现有写法（`Return`、`ctrl+l`、`KP_Enter` 等 xdotool 常见名并入别名表），未知键两边都返回 `invalid_arguments` |
| Linux 拖拽插值 | **做** | mousedown 后分 12 步 mousemove（每步约 10ms）再 mouseup，与 macOS 一致 |
| `hold_key`、`key_down/up`、`mouse_down/up` | **不做** | 按住状态会跨调用存在：连接中断、驱动重启或本人叫停发生在按下与抬起之间时，本人的键盘或鼠标会一直处于按住状态，这是对本人电脑的真实伤害。按住期间也会一直占着设备锁。常见需求已经由修饰键、drag 和 `repeat` 覆盖 |
| 粘贴式长文本输入 | **只在 Yuki 自己的电脑上做**（6.12） | 本人电脑上不做：会覆盖本人剪贴板，是动作之外的副作用；macOS 和 Windows 的 unicode 分段输入已经能输入 CJK 和长文本。Yuki 自己的电脑上剪贴板属于她自己，按 6.12 以单独的 `paste` 动作提供 |
| `cursor_position` | **不单独做** | 全屏截图结果回显 `cursor`（6.2），指针也画进图里（6.5），不需要额外一次往返 |
| `wait` | **不做** | 已有全局 `wait(seconds)` 工具；等待之后再 `screenshot` |
| `type` 的 `press_enter`／`clear_before_typing`（Gemini 风格） | **不做** | `key Return`、`key ctrl+a` 已能表达，不重复参数 |

### 6.5 截图画出指针

- Linux（Yuki 自己的电脑）：截图时用 XFixes 读取当前光标图像和位置（ctypes `libXfixes`，与现有 `Keymap` 的 ctypes 做法一致），合成到 `import` 的输出上之后再缩放。备选是 `xdotool getmouselocation` 加一个固定箭头（P0 选定）。
- Windows：GDI 截图时用 `GetCursorInfo` 加 `DrawIconEx`，在缩放前画到内存 DC 上，坐标按 per-monitor-v2 物理像素换算。
- macOS：全屏已经是 `setShowsCursor_(True)`，不变。
- 后台窗口模式（`window`）：真实指针不在目标处，不画；以回显的 `point` 为准。
- 绘制失败不影响截图返回：图照常返回，省略 `cursor` 字段。

### 6.6 动作结果的事实：route、hit

| 字段 | 取值 | 来源 |
| --- | --- | --- |
| `route` | `accessibility`（AXPress／AXShowMenu／AXFocused、UIA 模式、AT-SPI 动作）、`window_events`（macOS 带窗口标记的事件）、`global_input`（CGEventPost、SendInput、xdotool） | 每个实际投递分支自己写入；多步动作（先聚焦再输入）给出列表 |
| `hit` | `{role, name}`：投递**之前**落点上的控件 | 前台：macOS 用系统级 `AXUIElementCopyElementAtPosition`，Windows 用 UIA `ElementFromPoint`，Linux 用 AT-SPI `getAccessibleAtPoint`。后台：用目标窗口自己的子树（macOS 现有 `_ancestors`，Windows 用 UIA 子树） |

- `hit` 的查询受平台的消息超时约束（macOS `AXUIElementSetMessagingTimeout` 0.25 s，P0 第 7 项）。超时或取不到时省略字段，照常投递。
- 不增加 `effect`／`evidence` 推断（Cua 的 `confirmed`／`suspected_noop`）。是否生效仍以截图为准，避免另造一套"效果判定"。

### 6.7 elements 与 element 参数

**读动作** `computer(action="elements", window, text?, scale?, device?)`：

- 必须给 `window`（`screenshot` 的 `windows[].id`）。来源：macOS 是该窗口的 AX 子树，Windows 是 UIA 子树，Linux 是 AT-SPI 中属于该 X 窗口的应用节点。
- 输出与 `browser` 快照同风格，每行一个节点：`- button "关闭" [w0x1a2b.e17] (12,8,40,30)`。bounds 是 `[x0,y0,x1,y1]`，单位与 6.2 相同（本窗口图像素，或 `scale=1000`）。只列可见、尺寸非零的节点；无名的纯容器保留子节点、去掉自己；`text` 只返回匹配节点及其祖先路径。与 browser 一样只做无损过滤，不设字符上限，超出按现有结果预算交给 `read_result`。
- 是读动作：不写回执，本人叫停期间可用；Chromium／Electron 的 AX 开启等待沿用现有 `ACCESSIBILITY_SECONDS`。

**`element` 参数**：`click` 类、`move`、`scroll`、`type`、`key` 可以传 `element`，与 `x,y` 互斥（同时给出返回 `invalid_arguments`）。执行时：

1. 解析 id → 在驱动或连接器内存的注册表里找到节点；重新读取它**当前**的 bounds 和所属窗口。
2. 单击左键、没有修饰键时优先走无障碍动作（AXPress、UIA Invoke／Toggle／SelectionItem、AT-SPI `doAction`）；其余情况以当前 bounds 的中心为 `point`，按原有的前台或后台指针途径投递。`type`、`key` 先聚焦该节点（AXFocused、UIA ValuePattern 或 Focus、AT-SPI `grabFocus`）。
3. 回显 `element`、`point`、`route`、`hit`。

**id 的生命周期**（各平台统一）：

- 格式 `<window>.e<n>`，由一次 `elements` 调用分配，保存在当前驱动或连接器进程的内存中，按窗口只保留**最近一次**列表。
- 以下情况失效：同一窗口再次 `elements`、节点已销毁（macOS 返回 `kAXErrorInvalidUIElement`，UIA 返回 `ElementNotAvailable`，AT-SPI 对象已失效）、窗口已关闭、驱动或连接器进程重启。
- 失效时返回事实错误：`element_stale`，附该窗口新的列表，不执行，与 browser 的 `node_stale` 相同；id 里的窗口与当前给出的 `window` 不一致时返回 `invalid_arguments`；节点当前不可见或 bounds 不在窗口内时返回 `element_not_visible`，附 bounds，不退回按坐标点击。

**各平台**：

- macOS 和 Windows 复用现有 AX／UIA 代码，前台、后台都可用。窗口不在本人当前 Space 时，`elements` 返回 `device_not_supported`／`off_space`，与输入一致（只能看）。
- Linux（Yuki 自己的电脑）：有头档增加 `at-spi2-core`（`libatspi` 已随 GTK 装好），`desktop_service.py` 启动一条会话 D-Bus 总线，GTK 应用经它自动注册到 AT-SPI。读取逻辑在环境内新文件 `yuki_accessibility.py`，由驱动用 ctypes 调 `libatspi`（驱动保持只依赖标准库）。X 窗口经 `_NET_WM_PID` 对应到 AT-SPI 应用（P0 第 3 项）。Chromium 的网页内容不经 AT-SPI 暴露（不打开 `IsEnabled`），网页用 `browser`。总线或 `libatspi` 不可用、或该窗口的应用没有注册时，`elements` 和 `element` 返回 `device_not_supported`／`no_accessibility`，其他动作不受影响。
- 网页仍优先用 `browser` 的节点；`elements` 面向原生应用。

### 6.12 粘贴（只在 Yuki 自己的电脑上）

**接口**：新动作 `computer(action="paste", text)`，只作用于 Yuki 自己的电脑。

- 执行：驱动用 `xclip` 把 `text` 同时放进她自己 X 显示的 CLIPBOARD 与 PRIMARY 两个选区，然后按 `shift+Insert`。X11 上 `shift+Insert` 在 GTK、Qt、Chromium（读 CLIPBOARD）和 xterm 类终端（读 PRIMARY）里都是粘贴，两个选区内容相同，所以不需要再给按键参数。落在当前输入焦点，与不带坐标的 `type` 相同；不接受 `x,y`。
- 回显：`route:["clipboard","global_input"]`（先写选区，再发按键），没有 `point`；附动作后截图。
- 是输入动作：写回执、取租约；`control_held` 时在写选区之前拒绝；写入选区后按键失败按已投递报 `unknown`。
- 给出 `device` 时 Bot 直接返回 `device_not_supported`／`own_computer_only`，命令不发给连接器，本人电脑的剪贴板不会被碰到。
- 不碰本人的剪贴板：她的选区只存在于环境内的 Xvnc；桌面端（Yuki-Desktop）只向 Xvnc 发送本人主动粘贴的文字（ClientCutText），不订阅 Xvnc 发出的剪贴板事件，所以 Yuki 的粘贴内容不会进入本人电脑的剪贴板。粘贴之后她自己的剪贴板保留这段文字，这是她自己电脑上的正常状态。
- 环境里还没有 `xclip`（Manager 尚未补装）时，在任何输入之前返回 `device_not_supported`／`no_clipboard`。

**为什么是单独的动作而不是 `type` 的选项**：

1. 副作用不同：`paste` 会改写剪贴板，`type` 永远不会。把它做成 `type` 的开关，同一个动作的副作用就取决于一个参数，`type` 在本人电脑上"不碰剪贴板"的保证也要额外加条件说明。
2. 归属判断与现有的自己电脑专用动作一致：`wake`、`sleep`、`snapshot`、`restore` 都是按动作名在 Bot 侧以 `own_computer_only` 拒绝设备调用，`paste` 沿用同一处判断，不需要新增参数校验。
3. 途径事实清楚：回显的 `route` 写明 `clipboard`，模型和验收脚本都能看出这一步改了剪贴板。

### 6.8 批量动作（决定：不做）

评估"一次调用带多个子动作、每个子动作有回执、第一个失败就停、批末附截图"后，本稿**不做**：

1. **已有等价能力**：同一模型回复里的多个 `computer` 调用已经按环境或设备串行执行，每个都有自己的调用键、回执和恢复语义。批量带来的唯一新收益是少几张中间截图。
2. **成本已经大幅下降**：6.1 把每张截图从约 4784 个视觉 token（高分档 Claude）降到约 1500，旧截图也会被替换为文字。
3. **会成为第二套机制**：子动作回执要在 `environment_mutations`、连接器 journal 和 Bot 结果配对里各加一层"调用内序号"，部分完成的批次还要另定 `unknown` 语义，这正是共同约束 §0 要避免的替代机制。
4. 重新评估的条件：真机验收显示中间截图成为主要成本或延迟来源时，另写任务书。

### 6.9 `device_not_supported` 的事实原因

错误码保持 `device_not_supported`（现有提示词和模型习惯不变），增加 `cause`（字段名见 13.1：`reason` 已是连接器回执协议的派发事实，会被 journal 与 Bot 剥除）：

| cause | 含义 | 来源 |
| --- | --- | --- |
| `own_computer_only` | wake／sleep／snapshot／restore 带了 device | Bot |
| `connector_outdated` | 连接器缺少本次调用用到的能力（6.10） | Bot |
| `platform` | 连接器所在系统不能操作屏幕（Linux 连接器、macOS 低于 14） | 连接器 |
| `off_space` | 窗口不在本人当前 Space，只能看 | 连接器（macOS） |
| `no_background_route` | 平台没有把这种动作投递给后台窗口的途径（Windows 后台的 move、drag、key、middle、triple、修饰键） | 连接器 |
| `no_control_at_point` | 落点没有能承载该输入的无障碍控件（Windows UIA） | 连接器 |
| `no_focused_field` | 后台 type 前没有已聚焦的输入框（Windows） | 连接器 |
| `no_accessibility` | 无障碍接口不可用（Yuki 自己电脑的 AT-SPI） | 环境驱动 |
| `no_clipboard` | 自己的电脑上还没有剪贴板工具（`xclip`），不能 `paste` | 环境驱动 |

每种都在 `message` 里写明可行的下一步（例如"省略 window 改在前台操作"或"请本人在桌面端点「更新并保留资料」"）。

### 6.10 能力事实与旧连接器

- 连接器 0.7.0 在 `device_capabilities` 的屏幕事实中增加 `features`：`["scale","zoom","elements","modifiers","triple_click","middle_click","key_repeat","route","cursor"]`，只列本机真实可用的项（例如 AT-SPI 不在连接器上；macOS 低于 14 没有屏幕能力）。
- Bot 在发往设备之前，把本次调用**实际用到的**新动作或新参数与当前连接的 `features` 对照，缺少就返回 `device_not_supported`／`connector_outdated`，不把命令发给连接器。这样旧连接器就不会静默忽略 `scale`、修饰键或 `element`。只用旧参数的调用照常发给旧连接器。
- 自己的电脑：驱动随 Bot 版本挂入，与 Bot 同版本，不需要对照；AT-SPI 的可用性由驱动在执行时以 `no_accessibility` 报告。
- 连接器协议仍是 `4`，不升级：新增的只是新动作和新参数，按能力事实开放（与 11-09 任务书"新增资源用能力事实即可，不升协议"一致）。

### 6.11 各平台实现要点

| 项 | Yuki 自己的电脑（Linux／Xvnc） | macOS 前台 | macOS 后台（window） | Windows 前台 | Windows 后台（window） |
| --- | --- | --- | --- | --- | --- |
| 修饰键点击 | xdotool keydown／click／keyup | CGEvent flags | 带窗口标记的事件加 flags（跳过 AX） | SendInput 按下与抬起 | `no_background_route` |
| triple／middle | xdotool | CGEvent | 带窗口标记的事件 | SendInput | `no_background_route` |
| key.repeat | xdotool 循环 | CGEvent 循环 | PostToPid 循环 | SendInput 循环 | `no_background_route`（与现有 key 相同） |
| zoom | import crop | SCK sourceRect | 窗口截图后裁剪 | StretchBlt 子区域 | PrintWindow 后裁剪 |
| 指针 | XFixes 合成（P0 选定） | 已有 | 不画 | DrawIconEx | 不画 |
| hit | AT-SPI 按点查询（可用时） | 系统级 AX | 窗口 AX 子树 | UIA ElementFromPoint | UIA 子树 |
| elements | AT-SPI（ctypes `libatspi`） | AX | AX | UIA | UIA |
| 拖拽 | 12 步插值（新增） | 已有 | 已有 12 步 | 已有 | `no_background_route` |

## 7. 工具合同（版本 38）

```
computer(action, x?, y?, x2?, y2?, dx?, dy?, text?, repeat?, scale?, region?, element?,
         window?, display?, snapshot?, device?)
  action: screenshot | zoom | elements | click | double_click | triple_click | right_click |
          middle_click | move | drag | type | paste | key | scroll | resume |
          wake | sleep | snapshot | restore
  只对 Yuki 自己的电脑：paste（6.12）、wake、sleep、snapshot、restore
  新增参数：
    scale   整数，只允许 1000；省略时坐标为本次截图像素
    region  [x0,y0,x1,y1]，zoom 使用，整张截图坐标
    repeat  1–100，key 使用
    element elements 返回的节点 id，与 x,y 互斥
    text    在 click 类、scroll、drag 上表示按住的修饰键（如 "shift"、"ctrl+shift"）；
            type、paste 为输入文字，key 为按键组合，snapshot 为备注（原义不变）
  成功（带图）：{width, height, coordinates, display?|window?, control, cursor?,
               point?, point2?, scale?, route?, hit?, element?, submitted?} + 动作后截图
  zoom：{region, frame:{width,height}, width, height, coordinates} + 局部图
  elements：{window, elements:"<节点行>"}（无图）
  新错误：coordinate_out_of_range | element_stale | element_not_visible；
          device_not_supported 增加 cause
```

- `READ_ACTIONS["computer"]` 变为 `screenshot`、`resume`、`zoom`、`elements`。
- 工具说明重写坐标一段："x,y 是本次结果所附截图的像素，左上为原点，范围见结果的 width、height；若你习惯 0–1000 归一化坐标，必须传 scale=1000"。另外写明 `zoom`、`elements`／`element`、修饰键、`repeat` 和回显字段的含义；`submitted` 只表示已投递，是否生效以截图为准。
- direct 与 Code Mode 共用同一份清单；声明哈希变化后自然产生新的 manifest revision。旧链按其冻结的声明继续执行，旧调用是新合同的子集，执行器照常接受。

## 8. 提示词

`prompting/contracts.py`：把 GUI 操作规范从【本人电脑】提出来，写成两台电脑共用的一段（程序规则，不写入 SOUL）；【我的电脑】与【本人电脑】只保留各自特有的内容。要点：

- 先截图看清，按截图坐标动作；坐标是本图像素，若用 0–1000 必须声明 `scale=1000`，不要在一次调用里混用两种约定。
- 每次动作后先读回执里的 `point`、`route`、`hit`，再看新截图（指针画在图里），用一句话说明画面变化以及这一步成没成功；看不出成功就当作没成功，换个办法再试或如实说明。`submitted` 只表示已投递。
- 小字或密集控件先 `zoom`；原生应用能用 `elements` 时优先按节点操作；网页优先用 `browser`。
- 结果为 `unknown` 时先截图确认，不要盲目重复输入；`coordinate_out_of_range` 按错误里的坐标系改正后再试。
- 向用户汇报时以截图和回执里的事实为准，不把投递当成完成。发送、提交、删除前先截图核对目标（保留现有一句）。

具体参数与取值写在工具说明里，不在提示词中重复数值。

## 9. Client API 与桌面端

- Client API **不升 revision**：工具结果原样作为数据经现有接口展示，没有新的路由或字段约定。
- 连接器 stdout 的 `activity` 状态行增加 `route`（`background`／`foreground` 已有 `mode`），桌面端据此显示"Yuki 正在操作（后台／前台）"，不需要改协议。
- 桌面端 PR（Yuki-Desktop 仓库）：
  1. `prepare-connector.mjs` 指向含连接器 0.7.0 的后端提交，重新打包；`connector-resources`、签名与 `packages` 发布测试照常。
  2. 「這台電腦」页显示随附版本与运行中版本；版本较旧时提示「更新并保留资料」（沿现有更新流程，保留 device、instance、配对与 journal）。
  3. 在会话的工具卡截图上用回显的 `point` 画一个标记（第 17 节已定 7）；后端只负责在结果里给出 `point`。
  4. 文案遵循该仓库现有 i18n（繁体为主，简体同步）。

## 10. 对生产的影响

**升级路径**

1. 先部署 Bot（含 Manager 与环境内程序）。截图上限立即对两条路径生效，包括旧连接器；Yuki 自己电脑的新动作立即可用（驱动随 Bot 版本挂入）。有头档新增 AT-SPI 软件包时，由 Manager 启动时现有的 `apply_programs()` 按档位补装（需要网络；失败按现有规则保留原安装并在下次启动重试）。
2. 再发桌面端新版。本人在「設定 → 這台電腦」点「更新并保留资料」，连接器换成 0.7.0；原设备、实例、配对、journal 保留；之后 `features` 生效，新参数可用。
3. 回退：回退 Bot 即回到旧上限和旧合同；0.7.0 连接器面对旧 Bot 时，只会收到旧参数，行为与 0.6.2 相同。

**旧连接器兼容**

- 0.6.x：旧动作照常可用，并立即使用新的截图尺寸；结果没有 `point`、`route`、`hit`、`cursor`。用到新动作或新参数的调用返回 `device_not_supported`／`connector_outdated`，并在 message 里提示更新桌面端。
- 升级前已发出、按 2560×1440 截图计算的旧调用如果在升级之后才执行，会按新尺寸校验：超出范围的被拒绝；在范围内的按新图执行，回显 `point`（失败情况 C12）。

**token 成本**（每张截图，按官方公式估算，P0 以真实 usage 核对）

| 屏幕 | 现在发送 | 改后 | 标准档 Claude token | 高分档 Claude token | OpenAI patch（32px） |
| --- | --- | --- | --- | --- | --- |
| Mac mini 2560×1440 | 2560×1440 | 1430×804 | 服务端缩到 1456×819，约 1560（而且坐标错位）→ 1508 | 4784 → 1508 | 3600 → 1170 |
| Retina 3456×2234 | 原样 | 1334×862 | 服务端缩放 → 1488 | 9920 超限、被缩 → 1488 | 7560 → 1134 |
| 自己电脑 1440×900 | 原样 | 1356×848 | 1716 超限、被缩 → 1519 | 1716 → 1519 | 1305 → 1161 |

- Gemini 每张图的 token 数由 `media_resolution` 决定，不受尺寸影响（默认 1120），不变。
- 截图字节约降为三分之一，连接器以 64 KiB 分块传图的往返次数相应减少。
- `zoom` 与 `elements` 是按需的额外读取，由模型决定是否使用。

## 11. 失败情况（先于代码）

平台记号：L＝Yuki 自己的电脑（Linux／Xvnc），M＝macOS，W＝Windows；前台用 fg、后台窗口用 bg 标注。"两条路径"指 L 与 M/W。

坐标与尺寸：

| 编号 | 情况 | 期望结果 |
| --- | --- | --- |
| C1 | M 2560×1440 全屏截图 | 图像 1430×804，结果的 width/height 与图像字节一致 |
| C2 | L 1440×900 | 1356×848；点击按比例换算到真实像素 |
| C3 | M Retina（3456×2234 物理像素） | 按物理像素出图，点击落在正确的点坐标 |
| C4 | W 150% DPI | 不重复缩放，落点正确 |
| C5 | M/W 副屏在主屏左侧（负坐标），给 `display` | 换算为正确的全局坐标 |
| C6 | 两条路径，bg 窗口模式 | 坐标系是窗口图，换算加窗口原点 |
| C7 | 省略 scale，x 或 y 超出 [0,width)×[0,height) | `coordinate_out_of_range`，`failed`，附坐标系说明，没有任何输入 |
| C8 | scale=1000，v 在 0–1000 | 按比例换算，1000 映射到 width−1；回显 point（像素）与 scale |
| C9 | scale=1000，v>1000 或 <0 | `coordinate_out_of_range` |
| C10 | 模型混用约定（归一化 y 配像素 x）且两个值都在范围内 | 按所声明的坐标系执行；回显 point，截图画出指针；记为已知残留风险 |
| C11 | 截图与动作之间分辨率或窗口尺寸变化 | 按当下几何换算和校验；回显 point |
| C12 | 升级前的旧调用（按 2560×1440 计算）在升级后执行 | 超出范围被拒；在范围内按新图执行并回显 |
| C13 | 标准档 Claude 收到 1430×804 | 服务端不缩放（P0 用 token 计数或 `oversized_image:error` 探测确认） |
| C14 | 运行中热改 computer 截图上限 | 下一张图按新值出图，每个结果写明自己的 width/height |
| C15 | 改 `VISION_MAX_*` | 不影响 computer 截图；反之亦然 |
| C16 | `dx`、`dy`、`repeat` 与 `scale` 同时给出 | 它们不被换算 |
| C17 | 旧连接器 0.6.x | 立即按新上限出图 |

zoom：

| 编号 | 情况 | 期望结果 |
| --- | --- | --- |
| Z1 | 合法 region（L、M fg/bg、W fg/bg） | 返回原生像素局部图（只缩不放），结果有 region 和 frame；下一次点击仍按整张截图坐标 |
| Z2 | region 越界、反向或面积为零 | `coordinate_out_of_range` 或 `invalid_arguments`，不截图 |
| Z3 | zoom 带 scale=1000 | region 按 0–1000 解释 |
| Z4 | 本人叫停期间 zoom | 照常返回（读动作） |
| Z5 | 旧连接器 zoom | `connector_outdated` |

动作：

| 编号 | 情况 | 期望结果 |
| --- | --- | --- |
| A1 | click 带 `text:"shift"`／`"ctrl+shift"`（L、M fg、W fg） | 只在这次点击期间按住修饰键，结束后释放；网页事件日志显示修饰键正确 |
| A2 | 投递中途失败（OSError） | 修饰键仍被释放（finally）；`unknown` 或 `failed` 按已投递情况 |
| A3 | 未知修饰键 | `invalid_arguments`，没有输入 |
| A4 | triple_click 选中整行（L、M fg/bg、W fg） | 事件日志 detail=3；选区为整行 |
| A5 | middle_click（L、M fg/bg、W fg） | button=1（中键） |
| A6 | W bg 下的 middle、triple 或修饰键点击 | `device_not_supported`／`no_background_route`，没有投递 |
| A7 | M bg 带修饰键的 click | 走带窗口标记的事件（不走 AXPress），route=`window_events` |
| A8 | key repeat=5 | 发送 5 次；repeat 为 0 或 101 时 `invalid_arguments` |
| A9 | repeat 中途本人点「停止」 | 余下的不发送，`unknown`＋`control_held` |
| A10 | L 用 `cmd+c`、`Return`、`KP_Enter`、`ctrl+l` | 经统一解析器映射为 xdotool keysym，行为与连接器一致 |
| A11 | 两条路径的未知键 | 两边都返回 `invalid_arguments`（L 过去会交给 xdotool） |
| A12 | L 拖拽滑块或 HTML5 拖放 | 目标收到中间移动事件，拖拽生效（事件日志记录路径点数 >1） |
| A13 | drag 或 scroll 带修饰键 | 修饰键在整个动作期间按住 |
| A14 | 同时给 element 和 x,y | `invalid_arguments` |
| A15 | 长段中文 type（L、M、W） | 照常输入（现有 keymap 修复、unicode 分段） |

粘贴（只在 Yuki 自己的电脑上，6.12）：

| 编号 | 情况 | 期望结果 |
| --- | --- | --- |
| V1 | L 上 `paste` 多行中英混排长文（数千字）到 Chromium 输入框 | 文字原样到达；回显 `route:["clipboard","global_input"]`，没有 point；附截图 |
| V2 | L 上 `paste` 到 xterm 类终端（读 PRIMARY） | 文字原样到达 |
| V3 | 给 `device` 的 `paste` | `device_not_supported`／`own_computer_only`，命令不发给连接器，本人剪贴板不变 |
| V4 | `paste` 的 `text` 为空或缺失，或带了 `x,y` | `invalid_arguments`，没有输入、选区不变 |
| V5 | `control_held` 时 `paste` | `control_held`，选区不变，没有按键 |
| V6 | 写入选区后按键失败或超时 | `unknown`，不重放 |
| V7 | 环境里没有 `xclip` | `device_not_supported`／`no_clipboard`，没有输入 |
| V8 | 同键重试 | 返回原回执，不再写选区、不再按键 |
| V9 | 本人经桌面端连着画面时 Yuki `paste` | 本人电脑剪贴板不变（桌面端不订阅 Xvnc 剪贴板事件） |

指针：

| 编号 | 情况 | 期望结果 |
| --- | --- | --- |
| P1 | L 全屏截图 | 图上指针位置与 `cursor` 一致（像素检查） |
| P2 | W 全屏截图 | 画出指针；`cursor` 正确 |
| P3 | M 全屏截图 | 已有，`cursor` 正确 |
| P4 | 两条路径的 bg 窗口截图 | 不画指针；输入结果回显 point |
| P5 | 指针在另一块显示器上 | 本图没有 `cursor` 字段 |
| P6 | 读取光标图像失败 | 截图照常返回，省略 `cursor` |

route／hit：

| 编号 | 情况 | 期望结果 |
| --- | --- | --- |
| R1 | 每个带坐标的输入 | 有 point、scale、route |
| R2 | M bg 下 AXPress 成功 | route=`accessibility`，hit 为该按钮的 role/name |
| R3 | M bg 下 AX 找不到元素 | route=`window_events` |
| R4 | M fg、W fg | route=`global_input`，hit 来自系统级命中测试 |
| R5 | W bg 下 UIA Invoke | route=`accessibility` |
| R6 | L 上 AT-SPI 可用或不可用 | 可用时有 hit；不可用时省略 hit，输入照常 |
| R7 | hit 查询超时（应用无响应） | 省略 hit，输入照常，不判为失败 |
| R8 | 输入失败或 unknown | 已算出的 point 照常回显 |
| R9 | 同一调用键重试 | 返回原回执，包括原 point、route、hit |

elements：

| 编号 | 情况 | 期望结果 |
| --- | --- | --- |
| E1 | elements(window)（M、W、L） | 节点行带 id、role、name 和本图 bounds；`text` 过滤 |
| E2 | click(element) | 重新读取当前 bounds；能走无障碍动作就走；回显 element、point、route |
| E3 | 节点已销毁 | `element_stale`，附新列表，不执行 |
| E4 | 同一窗口重新 elements 之后用旧 id | `element_stale` |
| E5 | 连接器或驱动重启后用旧 id | `element_stale` |
| E6 | id 的窗口与给出的 window 不一致 | `invalid_arguments` |
| E7 | 节点不可见或在窗口外 | `element_not_visible`，附 bounds，不退回按坐标点击 |
| E8 | Chromium／Electron 窗口 | 沿现有 AX 开启等待，之后能列出网页节点 |
| E9 | 节点很多 | 不截断；超出结果预算时交给 `read_result` |
| E10 | M 窗口不在当前 Space | `device_not_supported`／`off_space`；screenshot 照常 |
| E11 | W 管理员权限窗口 | `target_elevated` |
| E12 | L 上 AT-SPI 总线不可用或应用不暴露无障碍信息 | `device_not_supported`／`no_accessibility`；其他动作不受影响 |
| E13 | 本人叫停期间 elements | 照常返回（读动作） |
| E14 | type(element) | 先聚焦再输入；W 的 ValuePattern 沿现有"接在原内容之后"的语义 |
| E15 | 旧连接器 elements 或 element | `connector_outdated` |

设备、能力与兼容：

| 编号 | 情况 | 期望结果 |
| --- | --- | --- |
| D1 | 0.6.x 连接器只用旧参数 | 照常执行，使用新尺寸；结果没有新回显 |
| D2 | 0.6.x 连接器加上任一新动作或新参数 | `connector_outdated`，命令不发给连接器 |
| D3 | 0.7.0 连接器 | `features` 完整，全部可用 |
| D4 | Work 进行中连接器重连并降级 | 下一次调用按当前能力事实判断 |
| D5 | M 窗口不在当前 Space 时输入 | `off_space` |
| D6 | W bg 下 move、drag、key | `no_background_route` |
| D7 | W bg 下落点没有 UIA 控件 | `no_control_at_point` |
| D8 | W bg 下 type 前没有聚焦的输入框 | `no_focused_field` |
| D9 | Linux 连接器或 macOS 13 | `platform` |
| D10 | 生命周期动作带 device | `own_computer_only` |
| D11 | 所有 cause | 错误码仍是 `device_not_supported`，message 写明下一步 |

回执与流程：

| 编号 | 情况 | 期望结果 |
| --- | --- | --- |
| X1 | 坐标或元素校验失败（L：Manager 已写待定行；M/W：journal 已 begin） | 回执结果为 `failed`、没有输入，不是 `unknown` |
| X2 | 同键重试新动作 | 返回原回执 |
| X3 | 同一回复里的多个 computer 调用 | 串行执行，各有回执和截图 |
| X4 | 新输入动作遇到 owner_active | 照现有规则让出 |
| X5 | 新输入动作遇到 control_held | 照现有规则拒绝 |
| X6 | 不支持图像的模型 | `media_read:false`；point、route、hit 照常返回 |
| X7 | Code Mode | 与 direct 同一份清单和执行器 |

模型与提示词：

| 编号 | 情况 | 期望结果 |
| --- | --- | --- |
| M1 | 标准档 Claude 在 Mac mini 上完成任务 | 落点正确（真机验收记录） |
| M2 | gemini-3.8-flash | 要么一直用像素，要么一直声明 scale=1000；落点正确 |
| M3 | 模型没有证据却声称成功 | 验收中按事实统计"谎报率" |
| M4 | 模型收到 `coordinate_out_of_range` | 按错误里的坐标系改正后再试 |

## 12. E2E 验收与产物

以 E2E 为主，每项都留可复现的产物。隔离单元测试只在必要时写，且先按第 11 节列出的情况写，再写代码。

**CI `desktop-headed`**（有头镜像内真实 Xvnc、Chromium、驱动，测试模式 Manager，脚本化模型驱动完整应用）：新增 `tests/integration/test_computer_use_e2e.py`。

- 本地测试页 `pointer-lab.html` 在 Yuki 的 Chromium 窗口中记录全部指针和键盘事件（button、detail、修饰键、拖拽路径点、按键次数），同时写入页面和文件。
- 依次覆盖：截图 1356×848（C2），粘贴（V1、V2、V4–V8），scale=1000 点击目标（C8），越界错误（C7、C9、X1），修饰键、三击、中键（A1、A3–A5），key repeat（A8），统一按键（A10、A11），拖拽插值（A12、A13），zoom（Z1–Z3），指针像素检查（P1、P6），elements／element（E1–E7、E12，取决于 P0；目标为一个 GTK 测试窗口），route／hit（R1、R6–R9），叫停期间的读动作（Z4、E13）。
- 产物：`test-results/computer-use/steps.json`（每步请求、回执、回显）、每步截图（另存一份把 point 画上的标注图）、`pointer-lab-events.json`。

**CI `sandbox-compose`**（真实 install.sh、Manager、环境，`desktop` 阶段）：经 Manager `computer_action` 以默认上限核对 1356×848 与 `zoom`（C2、Z1），核对 at-spi2-core、xclip 已装与 `AT_SPI_BUS` 已发布，记录 runc／runsc 下无障碍进程内存。C14、C15 由 Bot 层测试核对（13.1）。产物 `test-results/sandbox-compose/computer-use.json`。

**CI `connector`**（ubuntu／windows／macos）：

- 三平台：`actions.py` 解析、坐标换算与越界、`features` 公布、cause 映射，用真实连接器进程加脚本化服务端核对（A3、A11、C7–C9、D9、D11）。
- windows-2025：如果 P0 确认托管 runner 有可交互桌面，就做真实 GDI 截图画指针、SendInput 修饰键、三击、中键（打在专用 Chrome 打开的 `pointer-lab.html` 上），以及 UIA elements（记事本）（P2、A1、A4、A5、E1–E3、D6–D8）。不可交互时这些项转为真机验收。
- macos-15：托管 runner 不能授予 TCC，只跑事实与换算；屏幕相关项全部转为真机验收。
- 产物：`test-results/connector-computer/<os>/`。

**Bot 层**：用 `tests/support` 里的脚本化旧连接器（只报 0.6.2 的能力）和新连接器核对 D1–D4、D10、D11、X2；实际 manifest 产物核对合同版本 38 与新参数（X7）。产物 `test-results/computer-use/manifest.json`、`capability-gating.json`。

**真机验收（必须，需用户在执行时授权：使用付费模型并操作本人电脑）**

- 设备：用户的 Mac mini（2560×1440，连接器已连接；先在桌面端更新到 0.7.0）。
- 任务 T1"关掉一个窗口、不碰另一个"：打开两个 Finder 窗口，分别进入 `Yuki-保留` 与 `Yuki-关闭` 两个文件夹（Finder 关闭窗口没有保存对话框）。指令："关掉显示 Yuki-关闭 的 Finder 窗口，另一个不要动。"
- 任务 T2"写到指定输入框"：在 TextEdit 的两个已保存文档中，只往标题为 `Yuki-目标` 的那个末尾写一行指定文字。
- 矩阵：模型 `claude-sonnet-4-6`（标准档）与 `gemini-3.8-flash` × 前台（省略 window）与后台（给 window）× 每格 10 次。另在升级前（main）对两个模型的前台 T1 各跑 5 次作为基线（第 17 节已定 9）。
- Gemini 图像分辨率 A/B（第 17 节已定 6）：gemini-3.8-flash 的前台 T1、T2 各再跑一组 10 次，Profile 设 `tool_image_media_resolution: ultra_high`，与默认组对比成功率、步数、真实图像 usage 与谎报率，按数据决定是否改默认值。
- 结果核对按事实：结束后由脚本经连接器读取窗口列表或文件内容（T1：`Yuki-关闭` 窗口不在、`Yuki-保留` 仍在、Finder 未退出；T2：只有目标文件多了那一行），与模型自己的结论对照。
- 记录：每步回执、截图和标注落点的图；每次运行的成功与否、步数、token（真实 usage）、模型声称成功但事实失败的次数（谎报率）。
- 产物：`test-results/computer-use-acceptance/<日期>/mac-mini/<model>/<fg|bg>/run-XX/` 与 `summary.json`；记录写入 `docs/operations/computer-use-acceptance-<日期>.md`。成功率只做记录，不作为合并门槛。

**Yuki 自己电脑的验收**：生产 Colima 的 `desktop` 档（需授权；或同版本本地有头环境），打开两个终端窗口（标题 `Yuki-保留`、`Yuki-关闭`），用同样的指令与模型矩阵（只有全局输入，即前台）各跑 10 次，按 `xprop` 读到的窗口列表核对，产物同上路径下的 `own-computer/`。

**只能真机验证的项**：macOS 的一切屏幕操作（TCC）、macOS 后台投递与 Space、真实模型落点与谎报率。

**Windows**：本轮不做真机验收（第 17 节已定 8），Windows 只由 CI `connector` 的 windows-2025 runner 覆盖（真实 GDI、SendInput、UIA，打在测试自己的窗口上）；用户之后自行在 Windows 真机上按同样的 T1、T2 验收并另行记录。

## 13. P0 实测（先于实现）

1. 标准档 Claude（经实际使用的 Provider 或代理）对 1430×804 的工具截图不再缩放：用 token 计数接口或一次带 `oversized_image:"error"` 的探测请求确认；同时记录 gemini-3.8-flash、claude-sonnet-4-6 的真实图像 usage。
2. gemini-3.8-flash 在新的工具说明下的行为：给 5 个截图点击任务，看它用像素还是声明 `scale=1000`、会不会混用；claude-sonnet-4-6 是否从不使用 scale。
3. Linux AT-SPI：有头镜像装 `at-spi2-core` 与 `python3-pyatspi`，会话内启动总线；GTK 应用和 Chromium 的暴露情况；X 窗口与 AT-SPI 应用的对应；runsc 下的内存开销（2 GiB 限额）。
4. Linux 光标：XFixes `XFixesGetCursorImage` 与 `getmouselocation` 加固定箭头两种方案，取一种。
5. GitHub windows-2025 runner：是否有可交互桌面，GDI 截图、SendInput、UIA 是否可用。
6. Windows：`DrawIconEx` 在 per-monitor-v2 下的位置与尺寸。
7. macOS：后台带窗口标记的事件在带修饰键 flags、clickState 3、中键时是否被目标应用接受；前台系统级 AX 命中测试的耗时与合适的消息超时。
8. macOS：SCK `sourceRect` 局部截图；AXUIElement 失效后返回的错误码（用来判定 `element_stale`）。

任一项与本稿假设不符，回到本稿修订，不在实现中临时放宽。

### 13.1 P0 结果（2026-11-15）

| 项 | 结果 | 对本稿的影响 |
| --- | --- | --- |
| 1 标准档 Claude 不再缩图 | 未由实现方运行（需付费模型，按用户要求留给经授权的真机验收第 0 步）。按官方规则：1430×804 长边 < 1568、114.97 万像素 < 115 万，28 px patch 计 52×29＝1508 token，不触发服务端缩放 | 不变；验收第 0 步用真实 usage 核对 C13 |
| 2 gemini-3.8-flash 的坐标约定 | 同上，留给真机验收（M2 记录"一直像素／一直 scale=1000／混用"的次数） | 不变 |
| 3 Linux AT-SPI | 在有头镜像（Debian 12、TigerVNC 1.12、Chromium 156）里实测：`libatspi2.0-0` 已随 GTK 装好，只需再装 `at-spi2-core`（总线启动器与 registryd），连同 `xclip` 磁盘增 19 MB；会话需要一条 D-Bus session 总线。GTK 3 窗口自动注册，节点的 role、name、屏幕坐标 extents 都可读（"push button"/"Press me"），遍历 6 个节点 2 ms；X 窗口经 `_NET_WM_PID` 与 `atspi_accessible_get_process_id` 一一对应。Chromium 只有在 `org.a11y.Status.IsEnabled=true` 时才注册，而且网页内容此时仍未构建（只有空 frame）。内存：总线启动器约 7 MB、registryd 约 7 MB、多一个 dbus-daemon 约 4 MB（runc 下测得）。CI `sandbox-compose`（2026-11-15，提交 423ba95b）在真实安装的 `desktop` 档复核：runc 下 dbus-daemon 约 4 MB、总线启动器约 8 MB；runsc（gVisor）下约 9 MB 与 21 MB（registryd 按需启动，未计入），相对 3 GiB 档位可忽略 | **修订 6.7**：不装 `python3-pyatspi`（会拉进 Debian 的 Python 3.11），改由环境内新文件 `yuki_accessibility.py` 经 ctypes 调 `libatspi`，驱动保持只依赖标准库；`desktop_service.py` 为会话、Chromium 和驱动启动一条 `dbus-daemon --session --nofork`。不打开 `IsEnabled`（会让 Chromium 常驻构建无障碍树），网页继续用 `browser`，`elements` 面向 GTK／Qt 等原生窗口 |
| 4 Linux 光标 | Xvnc 提供 XFIXES；`XFixesGetCursorImage` 0.04 ms 返回 24×24 ARGB 图像、热点与位置，与 `xdotool getmouselocation` 一致 | 选 XFixes（真实光标形状，I 形与箭头可区分） |
| 5 windows-2025 runner | 有可交互桌面：main 上 `connector` 任务的 `test_screen_native`（真实 GDI 截图、CI 上真实 SendInput 移动光标并核对位置）与 `test_window_native`（UIA 驱动测试自己的 Win32 窗口）都在 runner 上通过 | 第 12 节 windows-2025 的真实项保留在 CI |
| 6 DrawIconEx | windows-2025 runner 实测（提交 423ba95b，`foreground-native-win32.json`）：SendInput 移动后截图回显的 `cursor` 与指针位置一致（±1 px），指针处的像素块与指针移开后不同，确认画在缩放后的图上且位置正确 | 不变 |
| 7 macOS 后台事件与 AX 命中 | 只做了只读测量（不在用户真机上投递任何输入）：系统级 `AXUIElementCopyElementAtPosition` 首次 25–33 ms、之后 0.2–0.5 ms，设 0.25 s 消息超时不影响结果。带修饰键 flags、clickState 3、中键的后台盖章事件是否被接受未测：按用户要求不在真机上投递输入；而且本实现方进程里 AX 对所有窗口的 `AXPosition` 返回 `kAXErrorAttributeUnsupported`、`_AXUIElementGetWindow` 返回 `kAXErrorIllegalArgument`（macOS 27.0 26A425），AX 侧的读数不可信 | hit 的消息超时定为 0.25 s；后台事件与 AX 窗口对应转入真机验收第 0 步（经授权运行 `YUKI_NATIVE_WINDOWS=1` 的原生窗口测试，只操作测试自己的窗口） |
| 8 macOS SCK 与 AX 失效码 | `sourceRect` 对显示器过滤器与单窗口过滤器都有效：单位是点，原点在显示器或窗口左上；输出尺寸大于区域原生像素时不放大而是留白，所以输出必须不大于区域原生像素（与"只缩不放"一致）。AX 失效码同第 7 项原因未能实测 | **修订 6.3**：macOS 窗口的 zoom 也用 `sourceRect`，不再截原生分辨率后裁剪；`element_stale` 按文档的 `kAXErrorInvalidUIElement`（-25202）判定，真机验收第 0 步核对 |

其他修订：

- 6.9 的字段名改为 `cause`：连接器回执协议已经用 `reason` 记派发事实（`original_dispatch_not_confirmed` 等），journal 在终态时剥除它、Bot 的 `local_device` 也把它当作回执元数据丢掉，同名会让事实原因在两处都消失（实现中由连接器 E2E 发现）。
- 6.7 Linux 的总线：`desktop_service.py` 除会话 D-Bus 外还直接启动 `at-spi-bus-launcher --launch-immediately`。它在显示器根窗口发布 `AT_SPI_BUS`，她从终端（没有会话 D-Bus 地址）启动的 GTK 应用也会注册（实测：不带 `DBUS_SESSION_BUS_ADDRESS` 启动的 GTK 窗口与读取方都经该属性找到总线）。
- C14、C15 归 Bot 层：截图上限是 Bot 下发给两条路径的值，热改与 `VISION_MAX_*` 互不影响由 Bot 层测试核对；`sandbox-compose` 的 `desktop` 阶段核对真实 Manager 下 1356×848、`zoom`、at-spi2-core 与 xclip 已装、`AT_SPI_BUS` 已发布，并记录 runc／runsc 两路下无障碍进程的内存（P0 第 3 项的 runsc 部分）。
- 6.4 的共用模块：按键、修饰键和坐标约定（`scale` 换算与越界说明）是两条路径共同的动作语法，放在一个只依赖标准库的模块 `yuki_agent_connector/actions.py`（而不是只装按键的 `keys.py`），与 `cdp.py` 一样挂入环境。

## 14. 文档、配置、测试与 CI

- **代码（Yuki）**：`sandbox/environment_tools.py`（合同 38、说明、`READ_ACTIONS`）；`services/agent_tools.py`（截图上限、能力对照、`own_computer_only`）；`external_agents/local_device.py`（下发、`connector_outdated`）；`sandbox/yuki_computer.py`（scale、校验、回显、新动作、拖拽插值、zoom、XFixes 光标、route、hit、elements 调用、paste）；新的环境内 `sandbox/yuki_accessibility.py`（ctypes `libatspi`）；`sandbox/desktop_service.py`（会话 D-Bus）；`sandbox/environment_programs.py`（headed 档加 `at-spi2-core`、`xclip`）；`ENVIRONMENT_FILES` 加 `actions.py`、`yuki_accessibility.py`；`config.py`、`settings_domains.py`、`admin/config_specs_hot.py`（新配置）；`llm/vendor_policy.py`、`openai_compatible.py`、`openai_responses.py`、`gemini.py`（工具截图的方言选项）；`prompting/contracts.py`。
- **代码（连接器 0.7.0）**：`actions.py`（新）；`screen.py`（换算、校验、features）；`desk.py`（新动作、回显、cause、element 注册表）；`screen_macos.py`、`screen_windows.py`（修饰键、三击、中键、repeat、zoom、光标、hit、elements、reason）；`connector.py`（`device_capabilities`）；`pyproject.toml` 版本号。
- **配置**：`.env.example` 增 `COMPUTER_SCREENSHOT_MAX_DIMENSION=1568`、`COMPUTER_SCREENSHOT_MAX_PIXELS=1150000`；`config/` 中如有对应项同步。
- **文档**：`tool-kernel.md`（computer 签名、合同版本 38）；`docs/operations/desktop-computer.zh-CN.md`（使用说明、AT-SPI、截图上限）；`docs/operations/local-agent-connector.zh-CN.md`（0.7.0、features、reason、升级）；`docs/operations/persistent-environment.zh-CN.md`（有头档软件包）；`docs/architecture/README.md` 索引本任务书；`CHANGELOG.md`。11-09 任务书作为基线保留，不改写。
- **测试**：第 12 节。受影响的现有测试（`test_computer_runtime.py`、`test_computer_actions.py`、连接器 `desk`／`screen` 测试、manifest 快照）按新行为改写，不保留冻结旧行为的断言。
- **CI**：`quality.yml` 的 `desktop-headed` 加 `test_computer_use_e2e.py` 并上传 `test-results/computer-use/`；`sandbox-compose` 上传 `computer-use.json`；`connector` 上传 `connector-computer/<os>/`。
- **Yuki-Desktop**：第 9 节。

## 15. 阶段（同一后端 PR 内）

| 阶段 | 内容 | 依赖 |
| --- | --- | --- |
| P0 | 第 13 节实测 | 无 |
| P1 | 截图上限（6.1）、scale 与回显（6.2）、统一按键、修饰键、三击、中键、repeat、拖拽插值（6.4），两条路径同时做 | P0 的 1、2 |
| P2 | zoom（6.3）、指针（6.5）、route／hit（6.6）、cause（6.9） | P0 的 4–8 |
| P3 | elements／element（6.7） | P0 的 3、7、8 |
| P4 | features 与能力对照（6.10）、连接器 0.7.0 打包 | P1–P3 |
| P5 | 工具说明与提示词（第 7、8 节）、文档、CI | P1–P4 |
| P6 | 桌面端 PR（第 9 节） | P4 |
| P7 | 真机验收与自己电脑的验收（需授权） | P5、P6 |

## 16. 不做

- **强制确认、按应用授权**：违反"不扩大限制"。发送、删除前截图核对只作为提示。
- **按模型 Profile 换算坐标**、按坐标数值猜约定：违反固定合同和"不按内容路由"。
- **各家私有 computer-use 协议**（Anthropic computer toolset、OpenAI `computer`、Gemini Computer Use 工具）：沿用 11-09 的决定 2。
- **批量动作**（6.8）；**`hold_key`、`key_down/up`、`mouse_down/up`**（6.4）；**本人电脑上的粘贴输入**（6.4，自己的电脑见 6.12）。
- **效果推断**（Cua 的 `effect`／`evidence`）：是否生效以截图为准。
- **把调用绑定到截图**（`capture_id`）：与"按当下几何、不保存最近截图"的现行设计冲突，第 17 节说明。
- **独立的 grounding 模型**（Agent S、e2b 的规划与定位分离）：会多一次付费调用，需要显式用途配置，另议。
- 不升级连接器协议，不升级 Client API revision。

## 17. 已定与待确认

已定（用户 2026-11-15）：

1. 一个后端 PR 加一个桌面端 PR；连接器发 0.7.0。
2. 截图上限默认长边 1568、约 1.15MP，与 `vision_*` 分开。
3. `scale` 只允许 1000，默认是本次截图像素；越界直接报错。
4. 不做强制确认、按应用授权、按 Profile 换算、按数值猜约定。
5. **Yuki 自己电脑的分辨率保留 1440×900**，截图按新上限缩成 1356×848，不改 Xvnc，不动她已布置好的桌面。
6. **方言层的图像提示**：工具截图（`source="tool"`）加 OpenAI `detail:"original"`；不加 Anthropic `oversized_image:"error"`；Gemini `mediaResolution: ultra_high` 不预先定论，在真机验收里按默认与 ultra_high 做 A/B，按数据再定。需要开关时只做部署设置（Profile 的方言选项），不按内容路由（6.1）。
7. **桌面端在工具卡截图上按回显的 `point` 画标记**：做，属于桌面端 PR；后端只需在结果里给出 `point`（第 9 节）。
8. **Windows 本轮不做真机验收**，只靠 CI（windows-2025 runner）；用户之后自行在 Windows 上验收（第 12 节）。
9. **真机验收规模**：每格 10 次，升级前基线各 5 次；成功率只记录，永远不作为合并门槛。
10. **AT-SPI 在 P0 不可行时**接受 Yuki 自己的电脑上 `elements` 返回 `device_not_supported`／`no_accessibility`（P0 实测可行，见第 13 节）。
11. **本人叫停期间截图、zoom、elements 照常可用**（只停输入），与现在一致，不另议。
12. **粘贴式长文本输入只在 Yuki 自己的电脑上开放**，不碰本人的剪贴板；本人电脑上继续不做（6.4、6.12）。

本稿决定（理由见正文）：批量不做（6.8）；`hold_key`、mouse down/up 不做，粘贴只在自己的电脑上以单独的 `paste` 动作提供（6.12），`cursor_position` 改为截图回显（6.4）；element id 按窗口只认最近一次列表（6.7）；`device_not_supported` 的错误码不变，增加 `cause`（6.9）；不绑定截图：一张截图之后几何可能变化，按当下几何换算并校验范围、再回显落点，已经覆盖"坐标对应哪张图"的问题，不需要再引入每张截图的标识和过期规则。

待确认：无。

## 18. 来源

[1] Anthropic computer use tool：https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
[2] Anthropic 坐标与图像缩放：https://platform.claude.com/docs/en/build-with-claude/vision-coordinates ；https://platform.claude.com/docs/en/build-with-claude/vision
[3] OpenAI computer use 集成：https://developers.openai.com/api/docs/guides/tools-computer-use-integration
[4] OpenAI 图像 detail：https://developers.openai.com/api/docs/guides/images-vision
[5] Azure OpenAI computer use（批量 actions、pending_safety_checks）：https://learn.microsoft.com/en-us/azure/foundry-classic/openai/how-to/computer-use
[6] Gemini Computer Use：https://docs.cloud.google.com/vertex-ai/generative-ai/docs/computer-use
[7] Claude Code computer use：https://code.claude.com/docs/en/computer-use
[8] Claude in Chrome 权限：https://support.claude.com/en/articles/12902446-getting-started-with-computer-use
[9] OpenAI Operator：https://openai.com/index/introducing-operator/ ；o3 CUA system card：https://cdn.openai.com/pdf/4375e605-f9a6-438d-bcc8-190599c183a6/o3_cua_system_card.pdf
[10] Cua driver：https://github.com/trycua/cua/blob/main/libs/cua-driver/docs/action-result-contract.md ；https://github.com/trycua/cua/blob/main/libs/cua-driver/docs/macos-background-input-v1-plan.md
[11] Anthropic computer-use-demo：https://github.com/anthropics/claude-quickstarts/tree/main/computer-use-demo
[12] Bytebot：https://github.com/bytebot-ai/bytebot/blob/main/packages/bytebot-agent/src/agent/agent.tools.ts
[13] Agent S3：https://github.com/simular-ai/Agent-S/blob/main/gui_agents/s3/agents/grounding.py
[15] browser-use 1.0：https://github.com/browser-use/browser-use/blob/main/browser_use/agent/prompt.py
[17] Microsoft UFO2：https://github.com/microsoft/UFO/blob/main/documents/docs/mcp/servers/app_ui_executor.md
[18] Skyvern：https://github.com/Skyvern-AI/skyvern/blob/main/skyvern/utils/image_resizer.py
[19] Claude in Chrome 工具（社区整理，非官方）：https://gist.github.com/sshh12/e352c053627ccbe1636781f73d6d715b
