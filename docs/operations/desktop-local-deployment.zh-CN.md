# Desktop 本机部署与公网入口

Desktop 的“在本机部署 Yuki”启动同一份 Bot 和个人 API，使用 Linux Docker 容器。
它不安装 QQ/TG/飞书，不附带模型，也不安装服务器持久终端 Manager。
本机 Codex 另由独立 Agent Connector 接入，不能把两者当作同一个服务。

## 本机 App-only

完整桌面包携带与所支持后端匹配的部署模板和明确镜像引用。用户选择目录、角色名、本人名称、
模型与本机端口后，Desktop 调用镜像里的 `yuki setup desktop-configure`，复用现有 setup 配置、
模型 Profile、operator 和 SOUL 写入；密钥通过 stdin 传递，保存在该部署的私有 `.env`。
模型不鉴权时可留空 API Key，对应 Profile 明确不声明 `api_key_env`；声明了密钥变量但缺值仍是配置错误。
Docker 中的同机模型需可从容器访问，Desktop 将用户明确输入的 loopback 主机转为 `host.docker.internal`。

首次沿 `init-db --character-name` 初始化，再启动原 Compose 服务。数据目录已有数据库时沿正常迁移，
不重命名角色。后端的 `desktop-pair` 命令只在所选容器内复用原 operator API，
以原 request UUID 创建 Person，再签 chat 码供 Desktop 正常配对。有效配对即可使用共享角色功能，不创建 owner 授权；旧 Desktop 传入的 owner_request_id 仅接受解析，不产生授权效果。
operator 凭据不成为聊天 token，不要求先有平台账号或默认授予 Person 超管。

`docker-compose.local.yml` 与原 Compose 合用，只发布 `127.0.0.1:<所选端口>:8080`，
不绑定公网，也不挂载尚未安装的 Manager。需支持 `!override` 的 Compose（2.24.4+）。
每份 Desktop 管理的目录使用独立 Compose project，配置/journal 与真实实例身份保留。
已有其他部署的目录不被静默接管；可连接原服务，或给新部署选另一目录。
关闭 Desktop 不删除数据；“停止本机服务”只停止这份实例的 Bot。

本机模板把 Desktop 的 POSIX UID/GID 传给原容器启动脚本，数据仍属于选择目录的用户；
Windows 没有宿主 POSIX 身份时使用镜像原有的 10001。启动脚本仍完成原目录准备后降权运行，
不要求宿主 UID 在容器里另建用户。未设置时服务器原有部署也继续使用 10001。

本机 HTTP/WS 无需购买域名。部署写完、容器 running、健康、配对和实际模型可用是不同结果；
模型验证必须有真实请求证据，不能以 healthz 或填写了 Key 代替。

## 服务器的 HTTPS/WSS

公网连接器复用 Yuki 原域名和 443。现有反向代理应把 `/client/v1/` 与 `/agents/v1/`
都转发到原 Bot，并允许 WebSocket Upgrade 与长连接，不为 Agent 再开一个端口。
电脑只主动连接服务器，普通用户不需要 SSH 或电脑入站端口。

没有现成代理时，可由部署者选择并固定 Caddy 2 镜像，准备真实域名/DNS，将
`config/Caddyfile.example` 复制为 `config/Caddyfile`，在 `.env` 填 `YUKI_CADDY_IMAGE`
和 `YUKI_PUBLIC_HOST`，用 `docker-compose.https.yml` 叠加启动。
域名应已指向这台服务器，80/443 应可供该代理使用；已有代理占用端口时复用原代理。
Caddy 的 `reverse_proxy` 支持 WebSocket；证书由其正常 HTTPS 配置管理。
见 [Caddy 官方反向代理说明](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。

```sh
docker compose -f docker-compose.yml -f docker-compose.https.yml up -d
```

`CLIENT_PUBLIC_URL` 填该实例真实可访问的客户端 base URL，仅用于生成私聊配对链接。
它不创建 DNS/TLS/代理，不改变 Provider 路由或授权；不填仍可使用原码加明确地址。
部署有路径前缀时，代理和这个 base URL 必须对应同一前缀，不能只修改生成的链接。
验收需在同一 HTTPS authority 上完成配对及 WSS hello/catalog；GET meta 成功不代表 Upgrade 已接通。

## 开发与本地验证

Desktop 用显式后端 checkout 生成模板，镜像固定为对应 commit 的 `desktop-<sha>`，不浮动拉 main。
本地开发可明确传入自己构建的 image，示例见 Desktop README。镜像构建、CI 原生包、
正式签名/Release、真实模型和生产部署分别记录。所有自动测试只管理自己的临时目录/Compose project，
不修改用户已有容器、账号或原生会话。
