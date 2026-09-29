# 模型配置单一路径迁移

模型连接的部署文件统一为 `webui-config/model_profiles.toml`；同目录的
`model_profiles.secrets.json` 保存 WebUI 输入的 API Key。基础 Compose 将目录只读挂入
`/app/webui-config`，WebUI overlay 对同一目录提供热配置所需的写权限。
`config/model_profiles.toml` 是旧部署路径，不再作为新部署的模型路由来源。

## 从旧部署迁移

升级或运行 guided setup 前，先确认当前真正加载的文件与任务路由。已有
`webui-config/model_profiles.toml` 时，以它为准，**不要用旧文件覆盖它**；旧文件可能仍含
`pro`、`flash` 等已退役连接。仅当新路径不存在、旧路径存在且已人工核对旧路由确实需要保留时，
复制旧模型文件及存在的密钥 sidecar：

```bash
mkdir -p webui-config
cp -p config/model_profiles.toml webui-config/model_profiles.toml
if test -f config/model_profiles.secrets.json; then
  cp -p config/model_profiles.secrets.json webui-config/model_profiles.secrets.json
fi
```

核对 `routes`、`search_connection` 和密钥引用后，将 `.env` 中的
`MODEL_PROFILES_FILE` 改为 `webui-config/model_profiles.toml`。然后运行
`qq-ai-bot-cli setup validate` 与 `docker compose config --quiet`；正式重建仍按原 Compose
文件列表执行。Guided setup 在发现仅有旧文件时会明确要求先迁移，不自动复制或重写旧连接。
原文件及修改前的 `.env` 应作为带时间戳的备份保留，供回退审计。

## 缺文件与退役旧路径

选定的模型文件缺失会阻止启动。确需短期运行没有 TOML 的旧环境变量部署时，必须显式设置
`MODEL_PROFILES_LEGACY_COMPATIBILITY=true`；该模式会生成 `main` 连接，不读取旧文件，
只适合有意继续使用旧环境变量的短期过渡，不能把正式配置丢失当作正常启动。
完成迁移后关闭此开关。

当新路径已经加载并验证，旧 `config/model_profiles.toml` 可以归档。对仍可能运行旧镜像或
遗漏 WebUI overlay 的部署，**不能只删除旧文件**：旧程序可能在文件缺失时回退到 `.env`
中的 DeepSeek 设置。先备份旧文件与 `.env`，再在旧路径放一个显式无效的退役哨兵
（例如 `schema_version = 0`），使误用旧路径明确失败；这一步需单独验证实际镜像的加载行为。
长期应让所有启动脚本使用完整的 Compose 文件列表，并移除旧镜像和旧 `.env` 路径。
