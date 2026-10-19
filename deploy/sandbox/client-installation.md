# 角色环境中的 Yuki CLI

新环境镜像在独立 `/opt/yuki-client` venv 安装本次源码构建的 Yuki 包及 `uv.lock` 锁定的生产依赖，使用 Python 3.12；不会把 Bot 数据库、模型配置或设备凭据复制进环境。`yuki --version` 可在没有人格配置与数据库的目录执行，个人客户端与 ACP 继续使用既有协议。

构建环境镜像时通过 `--build-arg VCS_REF=<实际源码提交>` 记录来源。`INSTALLATION.json` 记录包版本、来源提交、锁文件 SHA-256、安装记录摘要和 Python 主次版本；未提供来源提交时明确记录 `unknown`。摘要用于核对选定安装包，不代表已经完成真实 ACP 或网络验收。

已有持久环境可从本地已校验的同版本环境镜像补齐客户端：

```sh
sh deploy/sandbox/install-yuki-client.sh --container yuki-environment-mika --image <已校验的本地环境镜像>
```

脚本核对原容器的 `persistent-v1` 标签、Python 3.12 和镜像原生平台，仅创建一个从不启动的临时镜像容器来提取 venv；安装后逐包核对版本、安装记录、实际文件摘要与缺档，再检查 `--version`、`client --help` 与 `acp --help`。它不拉取镜像、不更换原环境、不重启 Manager，也不写 HOME、原任务、网关或登录文件。临时镜像容器和本地安装暂存目录在结束时清理。

同一安装标识可重复核对。已有不同或未完成的安装时，先显式关闭使用该客户端的 ACP peer，再加 `--replace` 完成覆盖或恢复中断的复制；不会自动关闭 peer 或重放其 prompt。复制失败保持失败状态，不能因安装标识文件已出现便宣布成功。原 rootFS 的备份／检查点仍沿持久环境文档，不把更改未来基础镜像当成已有 rootFS 已升级。

一个 Manager 对应一个角色的长期 HOME、状态根、成果库与 socket。`python -m yuki.sandbox.manager --name yuki-environment-<slug> …` 将名称交给现有持久环境实现；省略参数仍使用原 `yuki-environment`。不同角色不可共享 HOME、状态库或活动环境容器。

相关验证：`test_character_manager_cli.py` 实际构建两个 Manager 并操作隔离 HOME；`test_character_client_install.py` 实跑 CLI、安装过程及失败恢复，Docker 传输使用文件替身。这些单元测试不代表 Docker/gVisor 原生执行或跨角色协作已通过。
