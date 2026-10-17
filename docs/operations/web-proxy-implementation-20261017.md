# 标准代理接入实施记录

日期：2026-10-17，UTC+8。基线：main `d2e8a998`；实施分支：`codex/fix-web-proxy`。

本次只实施[搜索与网页读取任务书](../architecture/Yuki-搜索与网页读取解耦任务书-2026-10-17.md)中的代理、启动加载及读取超时接线。搜索自动抓页、本轮 URL 门槛和重复来源索引尚未删除。

## 实现

- 本机网页读取使用标准代理发现，支持 HTTP/HTTPS 代理与 SOCKS5/5h；不新增专属代理配置。按原 URL 匹配 NO_PROXY，逐跳校验公网 IP，保留原 Host、目标 SNI 和 HTTPS 代理自身的 TLS 校验。代理认证不交给目标站点。
- 用公开 HTTPX/HTTPCore 接口适配，保原物理连接容量；关闭、超时和重复取消收拢自有流与连接池。代理错误脱敏，不自动切协议或直连。专用匿名页面 client 不保存站点 Cookie，避免按固定 IP 串到另一域名。
- Bot、普通 CLI、setup 从实际 .env 加载标准代理/CA 白名单；进程值、空值和大小写优先明确，不导出其他 Bot 密钥。Compose 传入标准变量并提供宿主网关别名；配置方式见[代理说明](http-proxy.zh-CN.md)。
- Bridge 显式使用 Web reader 和既有 WEB_TIMEOUT_SECONDS，模型请求仍沿 Profile；不改主工具声明、模型请求正文或公共冻结历史。页面缓存仍用原 direct-page-v2、URL/问题与原过期规则，切换代理不清缓存。

## 本地验证

| 验证 | 结果 |
| --- | --- |
| 新 transport、真实 reader 集成和启动/Compose 联合测试 | `tests/unit/test_web_http_transport.py`、`tests/integration/test_web_page_proxy.py`、`tests/unit/test_http_environment.py`：87 passed |
| 原生搜索、来源保真、连接选择、工具原件、辅助请求及平台媒体回归 | 7 个相关文件：110 passed；14 条既有 SQLAlchemy 循环外键排序警告 |
| setup 相关回归 | 38 passed；与新增启动/Compose 42 项合计 80，42 项已计入上方 87，不重复计数 |
| 静态检查 | 全仓 Ruff 通过；按 Linux 目标检查全部 616 个源文件，Mypy 通过；diff 空白检查通过 |

相关行为测试合计 235 项通过。新网络测试只使用本机假代理、临时 CA 和严格的 loopback 转发，不调用真实模型、不发送真实聊天消息。Docker 测试只执行临时配置解析，不启动容器。

Windows 默认目标下 Mypy 仍报告 4 个既有 POSIX 模块的 30 项平台属性错误；本次未修改这些文件，使用 `--platform linux` 对齐 CI 的运行目标。TLS fake server 的网络测试在 Windows 使用 Selector 夹具；不能将其记为默认 Proactor、实际 Linux 网络或生产代理验收。

## 交付边界

上述是本地实现和定向验证。CI、PR、合并、镜像、部署及实际引用 URL 正文读取各自记录；已有根路径 302 和页面缓存命中均不能代替新部署的正文下载验收。没有修改生产配置或重新调用已付模型请求。

本次代码、测试及交叉审查由当前 Codex 会话与并行子任务完成；代理默认复用、范围与删除导向按维护者要求确定。
