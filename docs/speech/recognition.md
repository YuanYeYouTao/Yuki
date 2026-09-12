# Incoming voice recognition / 接收语音识别

Issue [#14](https://github.com/YuanYeYouTao/Yuki-QQbot/issues/14) requests file sending
and speech recognition. File sending already uses the shared workspace/social
delivery service, including separate upload and caption receipts. This change
adds automatic recognition of inbound OneBot `record` segments.

## Behaviour

Private voice messages and group messages admitted by the existing reply policy
are transcribed before the ordinary Main Agent runs. Current and quoted audio
are labelled separately. Transcription does not run for deterministic commands
or unaddressed group observations, and recognized `/ai` syntax is never parsed
as an administrative command. No extra Agent tool or generation entry point is
introduced; all normal main-agent tools and prompt-prefix rules remain shared.

The transcript is supplied as attachment context in the current request and
stored separately from immutable QQ text in `chat_events.audio_transcript`.
History rendering, searches, rollups, source revisions and memory evidence read
the same interpretation. Quoted audio is available as context but excluded from
the current speaker's automatic memory evidence. ASR is an interpretation that
may be wrong; the prompt labels it accordingly.

Original audio is processed transiently and removed after the call. Encoded
audio is omitted from persisted OneBot segments. URLs and provider responses
are not included in ASR error logs. Resetting a conversation interrupts ongoing
recognition and prevents old results from appearing in the new generation.
Deleting source events also deletes their transcripts and search entries.

## Provider and configuration

The public DeepSeek API contract checked on 2026-09-12 documents text and image
inputs, but no audio input or ASR endpoint. This implementation therefore uses
Qwen's dedicated `qwen3-asr-flash`, with `input_audio` in `chat/completions`.
It does not send audio to a DeepSeek model or to an ordinary Qwen chat model.

Connection selection is deterministic:

1. If either `ASR_BASE_URL` or `ASR_API_KEY` is set, use this dedicated pair;
   both must be provided for recognition to be configured.
2. Otherwise reuse the complete Qwen `VISION_BASE_URL` / `VISION_API_KEY` pair,
   even when the visual service itself is disabled.
3. Otherwise reuse the configured main-model connection if its model is Qwen.

Example for an existing Qwen deployment:

```dotenv
ASR_ENABLED=true
ASR_MODEL=qwen3-asr-flash
ASR_BASE_URL=
ASR_API_KEY=
```

An independent connection may use a Model Studio workspace URL or the existing
DashScope compatible-mode URL, ending in `/compatible-mode/v1`. Choose an API
key from the same region. Changing ASR configuration requires a Bot restart.
The admin config catalog uses `asr.*`; the secret key only exposes configured
status. `/healthz` includes `asr.enabled`, `asr.configured` and `asr.pending`
without making a billable provider request.

`ASR_ENABLED` is independent of Genie-TTS's `SPEECH_ENABLED`. Recognition needs
no local AI model, TTS worker, GPU or additional Python dependency. FFmpeg and
FFprobe are already included in the Bot image; local development must provide
both executables on PATH.

## Transport and resource bounds

Use the gateway that delivered the incoming event, regardless of active send
routing. `get_record(file, out_format="mp3")` handles QQ SILK conversion. Prefer
returned Base64 over URLs, which may point to the original SILK resource.
If the gateway supplies only a path local to its own container, the Bot does
not open it. The gateway must supply Base64 or a reachable audio URL. SnowLuma
documents Base64 with `out_format`; compatible NapCat versions may provide it
too. Event URLs remain subject to the existing bounded downloader's SSRF,
redirect and DNS-pinning checks. No private-URL bypass is enabled for ASR.

Defaults are 10 MiB per downloaded clip, 180 seconds per clip, 3 clips per turn,
2 concurrent messages, 8 total pending messages, and a 60-second deadline that
includes queueing, transport, conversion and recognition. The configurable
duration never exceeds Qwen's 300-second limit. Local decoding accepts common
audio containers and emits mono 16 kHz MP3, bounded below 7 MB before Base64.
Decoder processes have their own deadlines, no remote protocols and one thread;
cancellation kills them and removes temporary files.

Existing message rate limits apply before billable recognition. Failed
recognition is not retried automatically. Audio-only failure returns an explicit
message without asking the Main Agent to invent its contents. Mixed text/audio
and partial failures retain the available content and disclose the missing part.

## Storage and upgrade

Migration `0055` adds the transcript column, indexes it together with raw text,
and installs a source-revision invalidation trigger. A late transcript changes
the rollup source fingerprint and durable character count. If its source was
already compacted, derived rollups are rebuilt from the retained ledger.
Downgrade to `0054` keeps transcript data and restores the previous text-only
index; upgrading again restores transcript search. Back up the database before
the migration. Existing deployment rules still apply: build locally, verify the
image, and replace only the Bot while preserving the QQ gateway and login state.

## 中文说明

直接发私聊语音，或在群内按原有触发规则发送、引用语音即可。识别结果进入当轮完整主 Agent，
并保存到历史、搜索和 Rollup；自动记忆只把当前消息的语音当作当前发言者的证据，引用他人的
语音不会被归到当前用户名下。语音里出现 `/ai new` 等文字不会执行管理命令。

默认复用现有千问地址和密钥，使用专门的 `qwen3-asr-flash`。这项功能不依赖发送语音的
Genie-TTS 开关。默认上限为单条 10 MiB、180 秒、每轮最多 3 条、并发 2、待处理 8，整轮
识别最多 60 秒。失败、空结果、超限和繁忙均有明确反馈，不会无声重试或假装听到了。

升级会增加 `0055` 数据库迁移。回滚保留已经识别的文字；`/ai new` 会中断识别，旧任务不能
把结果写入新会话。原始音频仅用于当次处理，临时文件随处理结束清理。

## Validation

Targeted tests cover Qwen's request and failure contracts, gateway conversion,
private/group admission, quoted-speaker attribution, history/FTS/rollup,
idempotency, cancellation, and migration downgrade/re-upgrade. Existing file
delivery tests also cover upload/caption partial success.

On 2026-09-12 an isolated check used the server's existing Qwen connection and
FFmpeg with Alibaba Cloud's public `welcome.mp3` sample. The 27,212-byte input
was normalized to 14,732 bytes and recognized as `欢迎使用阿里云。` in 4.14 seconds
including download and conversion. The installed SnowLuma action registry was
also inspected: `get_record` accepts `out_format` and returns converted `base64`.
This check sent no QQ messages, changed no database data and restarted no service.

## Official references

- [Qwen ASR API](https://help.aliyun.com/en/model-studio/qwen-asr-api-reference)
- [DeepSeek Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)
- [OneBot get_record](https://github.com/botuniverse/onebot-11/blob/master/api/public.md#get_record-获取语音)
- [SnowLuma extended API](https://snowluma.github.io/api/extended/index.html)
