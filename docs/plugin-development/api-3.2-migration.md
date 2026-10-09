# Plugin API 3.2 迁移

> 本文记录语音退出时的历史合同。当前 Host 精确接受 API 3.4，完成本步骤后继续 [API 3.3 迁移](api-3.3-migration.md)。

Plugin API 3.2 彻底退出语音合成与 QQ 语音发送 SDK。ASR、历史语音/转写、普通文件上传、
文本、图片、表情和通知合同继续保留。MCP 的历史退出步骤见 [API 3.1](api-3.1-migration.md)。

## 插件代码与 manifest

移除 `ctx.speech`、`SpeechFacade`、`GeneratedSpeechHandle`、FakeSpeechFacade、
`TTSProviderRegistration` 和 `register_tts_provider` 的使用及导入。
不把调用替换成私有 Host、OneBot 直发或其他自动选择的合成 Provider。

删除五项请求权限：`speech.profile.read`、`speech.generate`、`speech.send`、
`speech.manage`、`speech.provider.register`，以及 `speech.facade.v1`、
`speech.tts_provider.v1` Feature 和全部 11 项 `speech.*` 事件注册。
删除这些订阅/调用后复核真实剩余能力，不增加替代权限。

```toml
plugin_api = "3.2"
yuki_requires = ">=3.9.0,<4.0"
```

Host 只接受精确的 3.2，在导入前拒绝 3.1 等旧版本及未知权限；没有兼容 shim。
管理员必须更新实际宿主挂载的插件源码，而不只更新 Bot 镜像。按新 manifest 的精确权限
重新批准，保留原启用意图；不能自动扩权或忽略退役权限。测试 fixtures 使用新 API，
并保留旧 API 不导入的负向用例。

## 已有执行与效果

部署前按原插件执行 ID 核对进行中的合成和发送。已接受的发送保留原回执，结果未知仍
沿原核对/暂停边界处理；不能因为 handle、接口或权限删除就认成未执行，再换 request ID
重发。进程内 handle 不作为重启后新发送资格，不重建旧语音或自动改发文字。

主工具 `send_message` 的 `voice` 参数和 `set_voice_preference` 同时退出；主合同为 13，
send schema 为 2，Automation 的对应 Action 显式携带其 schema。旧脚本不暗套新 schema，
旧请求、预算、Social payload hash 和冻结投递计划保持原身份。需要继续的合法任务经
现有编辑/授权流程适配，不能借版本升级重放已发生效果。

## 部署前检查

从所有实际 operator 授权撤去 `control.speech.read`、`control.speech.mutate`，
保留其他身份、roles、token_env、enabled 和权限；目标代码严格加载验证。
语音专属表、WAV 和参考资料有真实执行事实，须在停写一致快照中核引用并保存私有冷备，
再执行目标迁移。文件/volume 清理需要精确归属与使用者清单，不删除普通音频或共享 artifact。

完整范围与恢复状态矩阵见 [移除任务书](../architecture/genie-speech-output-removal-taskbook-2026-10-07.md)。
适配、测试、合并、发布和上线分别记录；本文不是已部署证明。
