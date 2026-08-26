# Versioned Docker Release 运维说明

Yuki 3.8.0 正式产物只由 `.github/workflows/release.yml` 发布，目标平台为 `linux/amd64`。
本地开发镜像不属于发布合同。

## 发布前一致性

最终 main SHA 必须满足：

- `pyproject.toml`、运行时 `__version__`、`uv.lock` 和 Release notes 均为 `3.8.0`。
- Alembic head 为 `0049`；fresh `0048 -> 0049` 和 historical populated `0048 -> 0049` 通过。
- Plugin API 为 `2.0`；Genie-TTS Worker 内部版本仍为 `1.9.0`。
- README、安装器默认版本、升级指南与部署包文件名一致。
- Quality、完整 pytest、测试预算、Memory release validation、Compose config 和 release smoke
  全部通过。
- 没有 `.env`、token、Cookie、QQ 号、数据库、Gateway 登录数据或构建临时目录进入 Git。

historical 0048 fixture 必须代表已完成 canonical v2 的数据库。pre-3.8、v1 或 cutover 中间态不是
3.8 Release 的升级来源。

## GHCR bootstrap

如果 Package 尚未公开，只在正式 tag 前执行一次：

1. 对最新 main 手动运行 Release workflow 的 bootstrap 路径。
2. 只发布 Bot 与 Genie Worker 的 `bootstrap-amd64` 标签。
3. 在 GitHub Packages 中将两个 Package 设为 Public。
4. 在未登录 GHCR 的环境验证匿名拉取。

bootstrap 不创建正式 Release，不发布版本 tag 或 `latest`。

## 正式发布

1. 确认目标 commit 已合入 main，工作树与远端 main 一致。
2. 确认 Quality workflow 对该 SHA 通过。
3. 创建严格的 `v3.8.0` annotated tag 并推送。
4. Release workflow 在 tag SHA 上重新运行完整 Quality。
5. 构建 amd64 Bot/Worker 镜像和无源码部署包。
6. 在临时部署中验证 fresh baseline、0049 head、Provider profiles、挂载与容器重建。
7. 推送不可变 `3.8.0` 镜像。
8. 在匿名环境拉取版本镜像并启动；digest 校验通过后才更新 `latest`。
9. 上传带 SHA-256 的部署资产并创建 GitHub Release，正文使用
   [v3.8.0 发布说明](../releases/v3.8.0.md)。

同一版本 tag 重跑时，只有 OCI `org.opencontainers.image.revision` 等于当前 tag SHA 才允许
复用或覆盖资产；不同 revision 不得覆盖既有版本镜像。

## Release 资产

至少包含：

- `yuki-3.8.0-deploy.tar.gz`
- `SHA256SUMS`
- `install.sh`
- `install.ps1`
- SnowLuma Provider 文档
- 3.8 升级指南

安装器必须验证 archive checksum 和精确目录布局；不能从 main 分支即时下载未固定文件补齐
Release bundle。

## 数据迁移发布门

发布 smoke 必须分别验证：

1. 空目录初始化 `0048` baseline 并升级到 `0049`。
2. populated historical `0048` 在 preflight 全通过时原子桥接到 `0049`。
3. v1、conflict、processing lease、ownership 缺口和 schema manifest 不匹配均失败关闭。
4. 任一 failpoint 后数据库签名回滚。
5. 迁移后 quick check、foreign key、FTS、trigger、关键表行数与摘要一致。
6. 旧事件不重新进入 Memory worker。

`0049` 没有 downgrade。Release 与升级文档必须把 DB/WAL/SHM 同时点快照列为唯一回退路径。

## Provider 发布门

- NapCat QQ A 与 SnowLuma QQ B 可同时收发。
- 同一 QQ 的第二条连接在 Adapter 和 Registry 两层被拒绝，原连接不受影响。
- 停止旧 Provider并确认注销后，同一 QQ 可连接新 Provider并复用 Presence。
- 切换只增加 ConnectionGeneration，不改变 ConversationGeneration 或 RouteGeneration。
- `SNOWLUMA_NOVNC_BIND_ADDRESS` 与 `SNOWLUMA_WEBUI_BIND_ADDRESS` 默认 `127.0.0.1`；
  `0.0.0.0` 只作为显式部署选择，文档必须要求强密码和防火墙。
- Release 日志和资产不包含 OneBot token、VNC 密码、Cookie 或登录目录。

不得宣称某个 Provider 能降低腾讯账号风控风险。

## 发布失败

- Quality、migration smoke、匿名拉取或 digest 校验任一失败：不创建 GitHub Release，不更新
  `latest`。
- 已推送不可变版本镜像但 Release 未完成：修复 workflow 后在同 tag SHA 重跑，不重建另一个
  revision 覆盖它。
- tag 指向错误 SHA：停止发布，删除尚未公开的错误 tag并重新走评审；不要覆盖已发布版本数据。
- 生产数据库问题：停止写入并恢复升级前同一时点 DB/WAL/SHM 及匹配镜像，不运行 downgrade。
