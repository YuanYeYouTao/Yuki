# 语音回复效果

主聊天只保留一条执行链：

```text
MessageProcessor → Conversation Runtime → Memory Runtime → Capability Runtime
  → Main Agent → send_message / send_voice
```

没有语音关键词路由，也没有独立语音会话。用户明确索要语音时，Main Agent 调用 `send_voice`；
是否主动使用语音由 Main Agent 自行决定；后端只执行人物 `text_only` 偏好、目标权限、声线和
传输能力等硬约束。

## 明确请求与 Agent 工具

Main Agent 根据自然语言和上下文判断是否调用 `send_voice`，不依赖固定词表。该工具只能选择
公开风格与 `auto/zh/jp`，不能传入模式、profile、模型、参考音频、文件或路径。未授权时直接
伪造调用会得到 `voice_not_authorized`。最终是否发送语音以该工具的真实投递回执为准。

## 日常主动语音

用户未明确索要时，Main Agent 仍可按语境调用 `send_voice`。后端不再维护频率预算、近期语音
比例或独立回复效果账本；真实工具回执就是投递事实。人物 `text_only` 偏好仍会拒绝语音，
`auto` 与 `prefer_voice` 作为上下文提供给 Main Agent，而不是后端概率门禁。

## 持久人物偏好

`person_speech_preferences` 以 QQ 为主键，只保存一个当前模式、来源消息 ID 和时间。只有用户
本人在真实消息轮中明确表达“以后、默认、切换模式”等持续语义时，Main Agent 才能调用
`set_voice_preference` 写入 `persistent` 修改；只约束当前轮的要求不会落库，自主群聊也不能
修改人物偏好。删除人物时该行通过外键级联删除。

未保存人物偏好时，`SPEECH_DEFAULT_MODE` 作为全局基线：

- `text` → `text_only`；
- `optional` → `auto`；
- `voice` / `text_and_voice` → `prefer_voice`。

## 语言、失败与可观测性

默认声线只公开目标语言，Main Agent 可以按语境选择中文或日文。合成前仍按最终正文脚本校验
语言，避免把中文正文交给日语 G2P。TTS 不可用时返回明确失败回执，由 Main Agent 决定是否
改发文字；已提交的发送不因新消息到达而被旧轮次取消。
