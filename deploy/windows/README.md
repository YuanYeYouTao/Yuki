# Yuki / Bocchi Windows 一键部署

此安装器为全新 x64 Windows 10（19041 以上）或 Windows 11 电脑准备独立的
`Yuki-Bocchi` WSL2 环境。Bot 在 Linux 中以普通用户运行，NapCat 由同一环境中的
Docker Engine 运行。无需预装 Python、Git、Node、Rust 或 Docker Desktop。

## 使用

1. 将私有 ZIP 完整解压到本地磁盘，双击 `Deploy.cmd`，允许 Windows 管理员安装请求。
2. 输入你自己的管理员 QQ。机器人的 QQ 已由私有包指定，两者不能相同。
3. 如提示重启，重启并登录同一个 Windows 用户；脚本会续装。也可再次双击 `Deploy.cmd`。
4. 安装完成后会打开登录凭据文件、NapCat 和 Yuki 管理界面。在 NapCat 输入其凭据，
   用私有包指定的机器人 QQ 扫码登录。之后向该 QQ 发私聊即可使用。

需要联网下载依赖、具备硬件虚拟化支持，建议安装盘至少留出 25 GB 空间。
首次编译时间取决于电脑和网络，安装器不会强制重启，也不会卸载其他 WSL 环境。
运行中不要删除专用 WSL 环境：它包含 QQ 登录、数据库、长期记忆、原执行回执和工作区。

## 已配置内容

- ZIP 内的 `source.tar.gz` 是 `manifest.json` 指定的完整固定源码提交，不使用旧发行镜像。
- 私有 `provider.json` 保存 API 地址、真实模型名和密钥；所有模型任务显式接到这条连接。
  主任务为 high、后台任务为 low，采用已实测的 Anthropic Messages 与函数工具结构输出。
- 完整角色提示词通过 `BOT_PERSONA_FILE` / `SYSTEM_PROMPT_FILE` 接到系统人格层；显示名、
  别名也改为 Bocchi，不混入默认 Yuki 人格。API 实际调用的模型以私有连接配置为准，角色回答不改变 API 路由。
- 启用持久 Work、自动化、工作者与本机隔离 Monty Code Mode。安装器编译指定 Monty、
  安装匹配 wheel、核对真实隔离与死亡回收，再使用普通迁移升级到 `0096`。
- 每次部署检查真实 API 的工具调用与原回执续接，测试会消耗 API 用量；不发送 QQ 测试消息。
- Yuki 管理界面 `http://127.0.0.1:18765`、NapCat `http://127.0.0.1:6099`。
  各自生成独立随机凭据，保存在 Windows 安装目录下仅当前用户和 SYSTEM 可读的 `management.txt`。
- 登录 Windows 后自动启动。`Start-Yuki.cmd` 可手动启动，`Stop-Yuki.cmd` 停止 Bot 与 QQ 网关。
  数据保持原样；再次执行安装器保留原配置、密钥、身份和数据库，不进行自动版本替换。

已通过真实 API 看图验证的主模型直接接收原生图片，保留来源、权限和恢复检查。
联网搜索、语音，以及需要 gVisor / Manager 的持久终端环境需要相应供应商或服务，
本包没有凭空配置这些额外连接。Monty Code Mode 的受限 Python 与持久终端是两种不同能力。

## 失败检查与验证边界

安装器仅在原生 Monty 隔离、两次真实 API 看图、工具调用和原回执续接、数据库 head / quick_check、两项服务与
Windows localhost 检查全部通过后标记安装完成。QQ 在扫码完成之前仍未登录。
任一步失败保留进度，修正具体错误后再次双击 `Deploy.cmd`；不会改用非隔离 worker，
不会关闭全局 seccomp / AppArmor / user namespace 安全规则，也不会用旧数据库覆盖新事实。

可从 PowerShell 查看运行状态：

```powershell
wsl -d Yuki-Bocchi -u root -- systemctl status yuki-bocchi --no-pager
wsl -d Yuki-Bocchi -u root -- journalctl -u yuki-bocchi -n 80 --no-pager
wsl -d Yuki-Bocchi -u root -- docker compose -f /home/yuki/app/gateway/compose.yaml --project-name yuki-bocchi-gateway ps
```

部署证据在 Linux `/home/yuki/app/deployment-evidence/`。源码、脚本、配置和 ZIP 校验通过
不等于已在目标 Windows 运行；最终目标机状态以安装器实际检查和 QQ 扫码登录结果为准。

这个 ZIP **含你的 API 密钥和角色配置，请只传给你自己的部署电脑，不要公开或提交到 Git**。
源码仓库中的安装器不含密钥。构建缓存可清理，运行数据不能当临时测试文件删除。

## 官方安装机制

Windows 安装使用 [Microsoft 的 WSL tar 导入机制](https://learn.microsoft.com/en-us/windows/wsl/use-custom-distro)、
[systemd 启动配置](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)和
[localhost 访问](https://learn.microsoft.com/en-us/windows/wsl/networking)。
Ubuntu 根文件系统与 SHA256 来自 [Canonical 的官方 WSL 镜像](https://cloud-images.ubuntu.com/wsl/releases/24.04/20240423/)，
Node 归档与 SHA256 来自 [Node.js 官方发行目录](https://nodejs.org/dist/v24.21.0/)。

## 为自己的配置生成私有包

在已授权的开发电脑中执行，输出路径必须是一个不存在的新目录：

```sh
uv run --frozen python scripts/build_windows_private_bundle.py \
  --provider-file /absolute/private/provider.md \
  --persona-file /absolute/private/persona.md \
  --output /absolute/private/Yuki-Windows-Bocchi \
  --bot-qq YOUR_BOT_QQ \
  --revision HEAD
```

新提交改变迁移 head 或源码合同后应更新部署验证，不能自动套用本包的 `0096` 验收。
