# Yuki 单文件 Docker 部署

当前安装入口统一使用 `yuki` / `yuki-bot`、`ghcr.io/yuanyeyoutao/yuki`、Compose 项目 `yuki`、网络 `yuki-network` 和数据库 `data/yuki.db`。一份 Compose 文件描述实际服务；环境值和模型凭据分别保存在 `.env`、`webui-config/model_profiles.toml` 等配置文件中。

4.0 当前是开发基线，尚无正式 4.0 部署包。以下使用已经由仓库的 **Desktop Bot image** 工作流构建成功的完整提交镜像。镜像已构建、核心服务健康、实际模型可用和各聊天网关接通分别验证。

## 从空目录安装

Linux Docker 宿主需要 Docker Engine 和 Compose 插件。使用目标镜像对应的源码模板：

```sh
git clone https://github.com/YuanYeYouTao/Yuki.git
cd Yuki
git checkout <已构建镜像的完整提交>
export YUKI_VERSION="desktop-$(git rev-parse HEAD)"
image="ghcr.io/yuanyeyoutao/yuki:$YUKI_VERSION"
docker pull "$image"

docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  --volume "$PWD:/deploy" \
  --workdir /deploy \
  --entrypoint yuki \
  "$image" setup --deployment-root /deploy
```

向导保存配置，不启动服务或升级数据库。在生成的 `.env` 中，将 `YUKI_VERSION` 保存为刚才相同的完整 `desktop-…` 标签，供以后重新打开终端时使用。开发镜像不能仅凭程序显示的 `4.0.0` 版本号替换为尚未发行的同名镜像标签。

沿同一份 `docker-compose.yml` 初始化所选角色并启动服务：

```sh
docker compose run --rm --no-deps --entrypoint yuki bot init-db --character-name "Yuki"
docker compose up -d --no-build
docker compose exec bot yuki setup verify
```

空目录创建的是 `data/yuki.db`。角色名称可自行选择；已有角色不重复初始化。QQ 沿向导选择 SnowLuma，详见 [SnowLuma 部署](../deployment/snowluma.md)；App、飞书或 Telegram 部署可不启用 QQ。需要让宿主访问 HTTP 时，在这份 Compose 的 Bot 服务中按实际入口设置端口；默认只 expose 容器内的 8080。

持久终端的 Linux 宿主 Manager 按[持久环境说明](persistent-environment.zh-CN.md)单独安装，指向这次部署的实际工作区和媒体缓存。配置向导、Docker 启动和核心健康不会自动证明 Manager 或聊天网关已经可用。

## 现有部署收敛为一份配置

先从正在运行的容器标签读取实际项目、目录和完整 Compose 文件列表，再解析最终配置。Bot、网关可能来自不同的启动文件列表，分别核对镜像、环境、端口、挂载和网络，不能只复制其中一份旧模板。

将核对后的完整配置保存为部署目录的 `compose.yaml`，以后只从这一入口启动：

```sh
docker compose --project-directory /opt/yuki -f /opt/yuki/compose.yaml config --quiet
docker compose --project-directory /opt/yuki -f /opt/yuki/compose.yaml up -d --no-build
```

解析后的文件可能包含实际凭据，应设置为仅部署操作员可读，不提交到仓库。重新读取成品，核对凭据中的 `$`、命令和环境值没有被二次插值改变。已存在的网络、宿主代理、防火墙网段与网桥接口也属于实际部署参数，须与新配置一起核对。

新配置启动并验证后，删除已无容器或服务引用的旧覆盖文件，运行入口只保留这一份 Compose 文件；后续修改直接维护这份配置。

## 显式统一存量名称

有业务数据的安装以原数据库和原挂载为起点。决定统一名称时，在原写者停止后移动现有部署目录、主库和实际存在的 WAL/SHM 文件，并同步 `DATABASE_URL`、挂载、Manager unit 与其工作区、缓存配置。记录移动前后 inode、schema 和事件水位，确认启动读取的仍是原库；不以空库替换现有历史。

Compose 项目更名需要重新创建对应容器，并核对网络与聊天网关连接。保留 QQ 登录目录、角色原件、模型配置、插件、工作文件及原执行回执。持久环境更换挂载时先保存当前完整 rootFS，继续使用原 HOME/runtime 和原 Manager 记录，避免从基础镜像丢失已安装工具。

只改镜像不会自动重命名显式配置的目录、项目或数据库。历史迁移与审计记录保留原始身份和路径事实，不作为新安装默认值。

## 实测范围

2026-11-02 使用 `4b75fe17a622d7090bddbf38beb48be6d322eca7` 镜像和对应仓库模板，在独立空目录实际执行配置、初始化和启动。只使用一份 Compose 文件，生成 `data/yuki.db`，迁移至 `0155`，核心健康为 `ok`，模型调用记录为 0。测试未连接真实 QQ、飞书或 Telegram，也未购买模型请求；这些结果证明该镜像的干净安装与核心启动路径。
