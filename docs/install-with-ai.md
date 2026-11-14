# 让 AI 帮你安装 Yuki

[English](install-with-ai.en.md) · [README](../README.md#安装)

把下面整段复制给能在你电脑上执行命令的 AI 助手（例如 Claude Code、Codex），先把第一行的三项改成你的情况。它会一步步检查、安装并核对结果，需要你做的事（输入密钥、授权管理员权限）会停下来告诉你。

```text
请帮我安装 Yuki 4.0.2（https://github.com/YuanYeYouTao/Yuki）。
我的情况：装在【这台电脑 / 一台 Linux 服务器（我已用 SSH 登录）】；模型服务是【例如 DeepSeek、OpenAI 兼容服务，或我本机的 Ollama】；Yuki 的电脑想要【lite 只要终端 / headless 加浏览器 / desktop 加桌面，不确定就按内存推荐】。

请按这个顺序做，每一步先告诉我要执行什么、为什么，再执行；出错时把关键日志给我看并停下，不要自己绕过：

1. 检查环境：操作系统和架构、可用内存和磁盘、`docker info` 是否正常、是否为 Linux 容器、`docker compose version` 是否可用。
2. 如果没有 Docker：告诉我适合我系统的安装方式（macOS 可用 Docker Desktop、OrbStack 或 Colima；Windows 用 Docker Desktop + WSL 2；Linux 用 Docker Engine），需要管理员密码或图形界面的步骤由我来做，你等我完成后再继续。
3. 选一个安装目录（默认当前目录下的 yuki/），下载安装器：
   curl -fsSLO https://github.com/YuanYeYouTao/Yuki/releases/download/v4.0.2/install.sh
   然后在终端里交互运行 `sh install.sh --dir <目录>`。安装器会问 Yuki 的名字、我的名字、模型连接和档位：
   - API 密钥由我自己在终端里输入，不要让我把密钥贴到对话里，也不要写进命令参数或 shell 历史。
   - 如果必须无人值守，就创建一个只有我能读的 model.json（chmod 600），由我填写密钥，运行
     `sh install.sh --dir <目录> --yes --profile <档位> --owner-name "<我的名字>" --answers model.json`，装完立即删除 model.json。
   - 本机模型服务（如 Ollama）的地址写 http://host.docker.internal:端口/v1，不要写 127.0.0.1。
4. 安装器结束时会显示配对链接（服务器配置了 `CLIENT_PUBLIC_URL` 时）或配对码（一次性，24 小时有效）。把它告诉我：链接在 Yuki Desktop 里打开或粘贴即可；只有配对码时，我在 Yuki Desktop 用「部署到我的服务器 / 通过 SSH 连接」连到服务器并使用它。过期了在安装目录运行 `sh deploy/pair.sh` 再取一个。
5. 核对，并把结果告诉我：
   - `docker compose ps` 里 bot 和 manager 都在运行；
   - `docker compose exec --user 10001:10001 bot yuki setup environment-check` 输出 "ok": true；
   - `curl -fsS http://127.0.0.1:8080/livez` 成功。
6. 如果是服务器：Yuki 只监听服务器的 127.0.0.1:8080。请按 docs/operations/tailscale-connection.zh-CN.md 帮我用 Tailscale 把它安全地连到我的电脑；不要把 8080 端口开放到公网，不要关闭防火墙。

注意：
- 不要删除名字以 yuki- 开头的 Docker 卷，也不要删除安装目录里的 data/、character/、workspace/。
- 端口 8080 被占用时，在 .env 里设 YUKI_PORT=其他端口，再 `docker compose up -d`。
- 沙箱起不来时看 `docker compose logs manager`。如果网络走 Clash/mihomo 一类 fake-ip 代理，环境里域名可能解析到 198.18.x.x 而连不上，参考 docs/operations/persistent-environment.zh-CN.md 的 DNS 说明。
- 这台 Docker 上已经有旧版 Yuki（3.9 或更早）时，不要直接覆盖，改按 docs/upgrade-4.0.2.md 升级。
```

## 装好以后

- 改人格：编辑安装目录里的 `character/SOUL.md`。
- 接 QQ、Telegram、飞书：见 [README](../README.md#装好之后)。
- 以后升级：在同一目录再运行一次安装器。
