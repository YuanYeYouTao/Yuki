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

选定的模型文件缺失会阻止启动。`MODEL_PROFILES_LEGACY_COMPATIBILITY` 及环境变量自动生成旧路由的兼容分支已移除；模型档案必须明确存在，连接内仍可显式引用相应环境变量。不能把正式配置丢失当作正常启动。

当新路径已经加载并验证，旧 `config/model_profiles.toml` 可按原部署记录归档。先核对实际启动脚本和完整 Compose 文件列表，不因当前源码已退役旧路径就删除仍由历史镜像使用的文件。
