# 同机角色协作装配

协作复用现有 `external_agent`、持久环境 ACP supervisor 和个人 API。被调用端为调用角色
建立普通 Person，不授予角色拥有者或管理员。它的对话属于该 Person；用户切换到被调用
角色的本人连接时看不到该私人会话。双方各自保留原 Work、prompt job 和交付回执。

## 前置条件

先按 [README](README.md) 登记已有实例并准备独立的新角色。每个实际调用方环境中的
`/opt/yuki-client/bin/yuki --version`、`client --help`、`acp --help` 必须可用。已有环境使用
`deploy/sandbox/install-yuki-client.sh --container <原环境> --image <固定环境镜像>` 原地补齐，
不重建容器或 HOME；安装程序核对客户端与完整依赖的实际 RECORD 内容。

确认 `yuki-sandbox-egress` 的实际 IPv4 gateway，例如用 `docker network inspect` 查看。
同机入口在该 gateway 的独立端口监听，只转发 `/<slug>/client/v1/`，其他路径返回 404。
普通公开入口仍由现有 TLS 代理负责。不要把整个宿主或私网加入放行列表。

## 生成与审核

下例是完整的双向拓扑；重复执行时须提供完整的调用方和被调用方列表：

```sh
python3 deploy/characters/configure_collaboration.py prepare \
  --caller yuki --caller mika --callee yuki --callee mika \
  --gateway 172.30.252.1 --port 18780 --output /secure/review/collaboration-1
```

它只写入新的本地审核目录：Nginx server 片段、拓扑清单及每条 ACP 程序 TOML。
准备不会修改运行服务。检查实际地址、原 Character ID、原 HOME、原环境名称和代理规则。
审核目录、登记目录和 `/etc/yuki-sandbox` 都由 operator 管理，不能作为角色工具输入。

在目标主机明确执行安装：

```sh
sudo python3 deploy/characters/configure_collaboration.py apply /secure/review/collaboration-1 \
  --relay-destination /etc/nginx/conf.d/yuki-collaboration.conf \
  --existing-service yuki-sandbox.service \
  --existing-acp /etc/yuki-sandbox/yuki/acp-agents.toml
```

已有 Manager 须明确提供原 service 与可信 ACP 路径。脚本通过单独的 Environment drop-in
接入原 Manager，不替换其 ExecStart、root、HOME 或原环境；若原 ExecStart 明确指定了另一
ACP 文件，则保留并要求先核对原配置。新角色使用各自模板的 ACP 路径。未登记的操作员
文件和已经手工修改的生成文件不会被覆盖，已有其他 ACP 程序及注释保留。

安装流程核对公开与私有 C1 身份、实际环境 label 和 HOME 挂载，测试 Nginx 配置后加载入口。
防火墙只允许登记调用方的实际容器 IPv4 → gateway:port，放在原 INPUT DROP 前，不修改
DOCKER-USER、原 egress 私网限制或外部代理。root hook 在 network guard 重建时恢复规则；
Docker start/die/destroy 事件会刷新地址，事件订阅从首次刷新前的时间开始，覆盖启动间隙。
停止或不存在的环境不获得放行；Docker 检查失败时不清空现有规则。

随后从原调用方环境核对入口 C1，为被调用端建立普通 Person，用环境内真实 CLI 兑换装置
凭据。token 仅经进程环境／stdin 传递，并以 0600 保存在调用方 HOME 的
`/home/yuki/.config/yuki-peers/<callee>/token`。宿主记录原 Person、请求 UUID 和已知 device ID，
不记录 token 或原始绑定码，也不从模型可写 HOME 读取宿主程序。ACP 状态分别位于
`/home/yuki/.yuki-peers/<callee>/`。

ACP root 配置通过 `/bin/sh` 读取该 token，再 exec 环境中的 `yuki acp`。`NO_PROXY` 与
`no_proxy` 一起设置为相同 gateway 和必要 loopback 列表，其他代理变量保留。`yuki client`
本身使用 `trust_env=False`；ACP 客户端会读取代理环境，因此两种大小写必须一致。
安装完成会重启对应 Manager 使其加载可信配置，原持久环境继续保留。

## 中断与凭据修复

同一条连接沿已保存 UUID 继续，已落盘 token 可通过原 `/me` 恢复，不重复创建 Person。
绑定已兑换但凭据丢失时，脚本不会自动签发新 token。核对被调用方原装置后，明确在 apply
增加 `--replace-credential`：已知原 device ID 先沿原请求撤销，再为同一普通 Person 生成新码。
若兑换回应遗失而没有已知 device ID，应先通过原管理界面检查并处理孤立装置；脚本不会
猜测另一个装置的身份。修复选项作用于本次完整拓扑，请只选择需要修复的连接进行审核。

## 验证与交付

从实际角色环境检查 `external_agent list`、ACP 委托、`yuki client artifact` 下载以及
`workspace_publish` 后的新交付。远端 ResourceLink 保留远端基底 URL、artifact ID 与版本；
本地发布使用本地 artifact ID。原样转交的两端版本和下载 SHA-256 应相同，加工后应分别
保留两版。重启调用方 Work 后沿原 prompt job 和 effect 收尾，不能重送已确认 prompt 或
重做已完成下载／发布。

还须从环境实测：协作入口可达、宿主其他端口和私网仍被拒、容器地址变化后旧地址失效。
HTTP／CLI 测试和配置解析不能代替 gVisor、Nginx、iptables、systemd 以及实际持久环境验收。
