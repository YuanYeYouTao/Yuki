# ec568631/0113真实请求、注入顺序与缓存记录

产品为`ec56863185b01197bb587918f39122cb605815ff`、direct/72工具、数据库0113。全18会话新历史重置后，固定自然窗为**2026-10-13 16:11:17.387604799–16:26:17.387604799，UTC+8**。付费重放在封存后进行，两个阶段分开统计。部署、原件保全和CI见[上线记录](current-material-deployment-20261013.md)。

## 新版真实注入与公共历史

13份真实自然请求声明均只有1个当前工作Host块，块在当前输入上方，有前后双换行。普通聊天6份来自3条真实内部用户事件，当前场景文字均为6字符，资料与原事件正文之间也有双换行。7份实际初始请求经精确源码的原native mapper重新序列化，messages/system/tools/toolConfig与实际派发逐字段相同；原opaque或媒体续轮不从红删诊断重建私有检查点。

以下仅示意顺序，不引用真人正文：

~~~text
当前工作：……

当前是群聊

[真实事件时间和作者] 用户本次消息
~~~

16:27:49.638794的实际公共存储快照只有同一Conversation/generation24的一份view，view_key等于Conversation，17个选取及投影项均为原事件model_input；旧actor/read_scope/contract_revision列不存在。当前Host块及完整本轮资料均未进入这些公共项。9个有旧片段的相邻实际请求对中，61/61次旧片段出现逐字保持；这是出现次数，不是61个唯一事件。

普通6份的真实作者为同一Person，**跨人自然相邻样本为0**。原SQLite/Main/SDK跨人行为回归已通过；本窗尚不能称多人切换的自然验收通过。后取的公共revision/epoch不冒充每份请求当时的状态。

## 自然响应与缺值覆盖

实际Host响应、网关日志和原usage统计各13份，均HTTP200；服务器实报physical均1。原model_start/model_route共26份，gzip/size/SHA全部验证。12/13有完整唯一请求树及真实metadata/session/usage互证；剩余1份续轮的6个inlineData在诊断中已红删，原像素size/SHA不能核验，保持关联未知。已保存thoughtSignature引用可验证，不借其补齐缺图。响应ID未同时保存到网关/raw表，不宣称13份SQL回执逐ID关联。

| 实际来源 | 响应 | 全部input | cache已知/总数 | 已知子集input | 实报cache | 条件加权命中率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 普通user_message Main | 6 | 99,667 | 5/6 | 83,510 | 71,297 | 85.3754% |
| 合法plugin_background/Work | 7 | 122,518 | 5/7 | 89,912 | 73,651 | 81.9145% |
| 本窗合计 | 13 | 222,185 | 10/13 | 173,422 | 144,948 | 83.5811% |

全窗output实报666、reported total实报222851；reasoning 0/13实报，数量未知，不补0、不用相减制造思考数。网关13份耗时中位数4332ms、范围2738–11516ms；这不是QQ端到端响应时间。[自然CSV](current-material-cache-0113-20261013-natural.csv)独立列原客户端usage及已严格关联的网关值，缺少完整关联的网关字段留空。12份已闭合客户端usageMetadata与原上游native子树逐树相同；13份整体usage多集和raw已知值多集一致，整体校验不替代缺失的逐调用键。

同session的8个相邻请求对，incoming及upstream的system/tools/generation/toolConfig固定字段均相等，首差路径两端一致；首差在实际contents的增长或当前尾部，本窗未见网关额外制造更早差异。实际1个账号、5个incoming及5个upstream session，对应共同Main和4个真实Work来源。公共历史共享和私有执行会话身份分别处理，不从无真人actor的Work猜人。本窗未复现旧按人分叉或异常重试，供应商内部缓存规则仍未核定。

## 两个Provider的原样重放

从本窗3个完整普通Main初始请求导出原ChatRequest和实际Gemini prepared，保原source/session。文件SHA256分别为`26bc08fdc43e29da535179a994a658ab86587435e11d0ad11c0ed2cc4d56c7e5`和`3fdbaf17c1d09cf678190a6b0a35d54b260f7ab8d6771e4ff692035075c0af3c`。使用现有`tools.cache_probe_replay --live`，每组序列`0,1,2,0,1,2`，不执行返回的业务工具或QQ，不加retry/warmup。

root第一次把远端Profile的映射地址当成本机直达路径，6次得到空HTTP502，usage全部未知；原组完整保留。随后核实并复用现有SSH转发，单独授权6次Gemini实验，只改实验base_url origin，原`/v1beta`、协议、模型、body和session保持。没有修改生产Profile、重启通道或替换报文。Gemini corrected组在**16:46:16.983890–16:46:37.774129 UTC+8**完成6次HTTP200；独立DeepSeek Chat组在16:34完成6次HTTP200。

Gemini使用原prepared的16384/low，不能把Profile默认8192写成实际派发；DeepSeek沿真实独立Chat连接序列化neutral请求，8192/thinking enabled+low、无tool_choice。DeepSeek连接沿用此前真实实验配置，不称当前生产Main的Profile，也不称其Chat wire为原Gemini wire。

| 实际组/阶段 | 请求 | cache已知 | input | cache | 加权命中率 | output |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Gemini错误本机路径 | 6×502 | 0/6 | 未知 | 未知 | 未知 | 未知 |
| Gemini第一遍观察 | 3×200 | 3/3 | 48,917 | 42,139 | 86.1439% | 138 |
| Gemini同序重访 | 3×200 | 3/3 | 48,917 | 42,139 | 86.1439% | 111 |
| DeepSeek第一遍观察 | 3×200 | 3/3 | 56,694 | 38,528 | 67.9578% | 752 |
| DeepSeek同序重访 | 3×200 | 3/3 | 56,694 | 56,064 | 98.8888% | 474 |

Gemini成功6份总input97834、output249、reported total98083，reasoning全部未知；DeepSeek总input113388、completion1226、reported total114614，其中reasoning843已包含在completion中，不重复加价。HTTP耗时中位数分别3.056983秒及1.798796秒。18次客户端POST包括6次失败，付费组服务器physical未另外采集，保未知。[18行重放CSV](current-material-cache-0113-20261013-replay.csv)保逐条状态、缺值、哈希和耗时；各case重访body逐字相同，每协议固定字段均只有一份SHA，数据不混进自然窗。

Gemini同报文重访的cache计数仍完全相同，未命中量分别2418、2123、2237 token。不能将剩余未命中直接归为Host改写旧历史，也不能推出固定“尾部3k不缓存”。[Google官方缓存说明](https://ai.google.dev/gemini-api/docs/caching?hl=zh-cn)列的是最低输入量及提高命中几率的建议；最低输入量不是固定未缓存尾部。实际中转价格及内部缓存规则仍未知，不添加保温或缓存协调器追求99%。

## 资料减少与费用口径

本窗普通current场景文字实测6字符，当前工作块只在本次输入中出现，没有累计为公共垃圾历史。旧受控同资料实验的180→6字符减少96.6667%、两协议完整输入分别减少0.680568%/0.753828%，仍属于原实验。本窗重置了generation，又没有同窗旧版对照，**不能将新16k与旧50k/253k直接相减作为产品节省**。

2026-10-13核对[DeepSeek官方价格](https://api-docs.deepseek.com/quick_start/pricing/)；本组处于工作日UTC高峰，Flash每百万cache输入$0.006、miss输入$0.3、输出$1.2。按真实usage作名义估算：

| DeepSeek条件 | 输入USD | 输出USD | 合计USD |
| --- | ---: | ---: | ---: |
| 第一遍观察 | 0.005680968 | 0.000902400 | 0.006583368 |
| 同序重访 | 0.000525384 | 0.000568800 | 0.001094184 |
| 6次合计 | 0.006206352 | 0.001471200 | 0.007677552 |

输入名义差额$0.005155584是这组缓存热度变化，不是新旧产品收益。Gemini实际中转价格、完整思考费用及账单未取得，金额未知；本次没有声称生产省了某个比例。私有请求、response、认证、真人正文/平台账号与签名只保在受控外部目录，公开CSV只有匿名哈希和测量值。
