# 语义参与与持续接话交付记录

状态：Host 本地实现完成，联合验证中；Host 最终 CI、合并和部署待完成。对应[任务书](../architecture/semantic-participation-continuation-taskbook-2026-10-03.md)，两个仓库联合交付。

## 行为变化

真人直接邀请、Jev 确认的邀请和已有互动的续聊进入普通 USER_MESSAGE，复用唯一
YukiRuntime、媒体准备、协调器及原 Work 输入匹配。Jev 只评价需要解释的真实焦点，
普通持续接话不再每轮增加一份语义模型请求。自主加入、记忆联系和无来源机会仍走 SELF。

普通接纳按原内部事件登记一次事实；已有 Work 的输入与接纳在同一事务发布。
它防止重复启动，不是普通聊天的完整执行日志。已接纳的无 Work 请求在崩溃窗口中可能
没有完成答复，不能由 Jev 自动重跑。尚未接纳的原消息准备可以随进程丢失，不按平台 ID
或最近正文猜回用户请求。原 Work、预算、工具和发送回执继续使用既有恢复机制。

Main 可稀疏报告 join/stay/quiet，沉默和 NO_REPLY 无需额外模型补问。quiet 收起自己的
局部参与，不制造用户停止事实；同版本已解释的真人焦点不重复评分。真实群发送仍是群
送达，原消息的绑定单独关联交流对象和讨论，不伪造成私人发送或增加全群的表达暴露。

参与资料有用才放进新轮尾部，并和本次选取一起冻结。旧历史片段不因状态变化改写，
固定 system/tools 与请求设置沿用原合同。删除旧表情占位句造成一次真实 system 前缀
变化；压缩和来源失效仍可产生明确的新前缀边界，不承诺永久命中。

## 核查与验证

独立库第一轮本地 179 用例通过，Ruff、format、strict mypy 及 wheel/sdist 构建通过；
[库 PR #13](https://github.com/YuanYeYouTao/Yuki-Semantic-Participation/pull/13)
CI 通过并合并。随后核查修复参与依据逐轮累计：固定单元建立依据，保留每次真实
输入、意愿和表达自己的来源。[库 PR #14](https://github.com/YuanYeYouTao/Yuki-Semantic-Participation/pull/14)
已合并为 `aa4124d214cbb3f5430cf2d9b59fc9cbc71d42ad`，库最终本地 183 用例通过。
Host 的 `pyproject.toml` 与 `uv.lock` 已固定该修订，frozen sync 后安装包位于
site-packages，`direct_url.json` 指向该提交归档，不是 editable 工作树。

Host 本地全量首轮 2703 passed、8 skipped、1 failed。失败用例沿用已退出的旧采样字段，
已改为确定性样本而保留真实机会率、接纳和 Work 路径；修复后的单例通过。Windows 跳过
包含 POSIX 文件安全和未提供私有备份的用例，Linux CI 结果另记。无关通过用例未机械重跑。

额外组合核查涵盖：真实邀请转普通接纳、无额外 Jev 的连续接话、第三个旧 SELF 锚点进入
实际观察请求、无 pending 停止到 Main quiet 再到 Jev 关闭、先解释后 quiet 不重评、未知
保持未知、主自主开关关闭时真人仍可接话、原 Work 输入原子回滚、附件权限、重启去重、
旧快照 reader 和非空解释依据。Host 全源 mypy 704 文件、format 1123 文件与
`release_validate` 已通过。最终归档依赖下，接纳/迁移/普通反馈/Gemini 前缀 33 项、
Host 原语义路径 21 项、邀请与边界组合 12 项通过。其中 50 轮续聊的绑定依据保持两条，
两种协调器版本下迟到的旧 quiet 都不覆盖更新的意愿或增加观察。Linux CI 数量另记。
旧 stop、最新 continue 与 in-flight 迟到的同一 Host 流水尚未联合实测；现有库分别覆盖
来源失效、排队合并和迟到围栏，不能把分项通过写成该完整流水已验收。

## 手工缓存样本

[脱敏数据](evidence/semantic-continuation-cache-20261003.json)保留口径、样本状态和 token
计量；该测试不是常驻 CI，不向 QQ 发送，也不使用生产消息或数据库。每种 Provider
各 13 次物理请求，覆盖同一会话续接、两 Work 切换、恢复与压缩，使用本轮固定工具和提示词。

| Provider | 已报告输入 | 已报告命中输入 | 加权命中率 | 缓存指标覆盖 |
| --- | ---: | ---: | ---: | --- |
| GeminiProvider / Gemini | 319485 | 121264 | 74.24% | 7/13；分母为这 7 次的 163332 输入 token |
| DeepSeekResponsesProvider / Responses | 316442 | 222976 | 70.46% | 13/13；包含冷请求与独立压缩请求 |

DeepSeek 热续接的四个场景分别为 85.42%、98.37%、98.97%、97.79%；第二场模型未发布
要求的 context note，保留这个能力缺项，不能仅凭缓存比率称整个场景成功。Gemini 缺缓存
指标的 6 次保持未知，不按零计算。两种 Provider 的输出、摘要和计量覆盖不同，不能由这
一组样本判断谁更好或本次重构使生产命中率上升。未取得反代最终上游报文的独立证据。

## 合并、升级与上线

尚待 Host 提交与 PR、Linux CI、目标镜像源核验，以及生产副本 0090→0091
迁移演练。正式操作只替换 Bot，保留原 Compose、数据挂载、SnowLuma 和网关。
完整备份包含 SQLite 一致快照与被引用的协议对象、工具正文和工作区文件。

0091 有普通接纳事实后拒绝降级删除表。回退必须使用保留这些事实并实际读取去重的
兼容镜像，不能恢复旧备份覆盖升级后消息、输入或效果。线上健康、实际镜像/依赖和原
Work 回执核验分别记录；自然 QQ 接话效果由用户验收。
