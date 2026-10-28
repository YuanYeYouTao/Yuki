# 本地 App Social 对照复现

这组脚本比较 `afb509e7c96f1949651b505e601758f14d2f8fd8` 与 `27c71ee2822bbc91a32124bc6612180bdab5b177` 的真实 `Social.execute("send")` 路径。它使用各版本自己的 App fixture、路由、收据和 ledger。测量时不调用模型、外网或真实聊天平台。

已保存的结果在 `docs/operations/message-identity-social-cost-20261029.json`。每版三轮，每轮先测64个顺序请求，再测64个四并发请求；每个场景的8次预热、fixture准备、配对和0155迁移都在计时之外。

四并发是封闭批次：同批四个请求全部结束后立即开始下一批；顺序场景则等待上一请求结束。两者都不额外sleep。吞吐是这个到达模式下的处理量，不是固定到达率SLA。

## 从干净快照运行

在 Linux 仓库根目录执行，需要 Python 3.12、uv 和 C 编译器。两个 checkout、数据库和结果都放在独立临时目录；依赖环境共用新版 checkout 的 venv。

```sh
probe_dir="$(pwd)/docs/operations/probes/social-cost-20261029"
cost_root=$(mktemp -d /tmp/yuki-social-cost-source.XXXXXX)
git worktree add --detach "$cost_root/before" afb509e7c96f1949651b505e601758f14d2f8fd8
git worktree add --detach "$cost_root/after" 27c71ee2822bbc91a32124bc6612180bdab5b177
uv sync --project "$cost_root/after" --python 3.12 --frozen --all-extras
cost_python="$cost_root/after/.venv/bin/python"
(cd "$cost_root/before" && "$cost_python" scripts/build_vec1.py)
(cd "$cost_root/after" && "$cost_python" scripts/build_vec1.py)
sh "$probe_dir/reproduce.sh" "$cost_root/before" "$cost_root/after" "$cost_python"
```

脚本最后打印结果目录，里面包括：

- `measured-before.json`、`measured-after.json`：逐请求延迟、到达偏移、SQL/事务次数和每次 writer 上下文数据。
- `seed-comparison.json`：同一旧版 fixture seed 的复制、真实0155升级，以及原列数据严格比较；新增列不混入旧行比较。
- `delivery-audit.json`：每次发送的 event 与成功收据一一对应，含预热和setup的总量也必须吻合，检查没有额外投递或失败收据。

旧 fixture 通过自己的 metadata 建表，尚无 Alembic revision。`prepare_seed.py` 仅对这个复制出的测试seed写入已知0154标记，再执行真实0155；其耗时单独记录。不要把该脚本用于生产库迁移。

本次保留的快照也可直接重跑（生成新的结果目录，不覆盖原始样本）：

```sh
sh docs/operations/probes/social-cost-20261029/reproduce.sh \
  /tmp/yuki-social-cost-20261029/before \
  /tmp/yuki-social-cost-20261029/after \
  /tmp/yuki-id-final-env-20261028/bin/python
```

## 读数口径

SQLAlchemy事件按请求的 ContextVar 归属，后台另列，本次测量期后台计数为0。`writer entry` 包含排队、连接获取、BEGIN和调度；`writer context` 包含正文、flush、commit与清理，不能分别当成纯FIFO等待或独立commit耗时。读会话关闭产生的rollback事件也不表示写入失败。

合并后单次 writer 的尾部更长，但每请求累计writer上下文耗时和前台尾延迟下降。结果文件保留这两种相反走势及全部原始数据。p50采用中位数，p95采用最近秩 `ceil(0.95*n)`。

这次原始probe/runner/audit仍保存在 `D:/Code/Reports/Yuki/id-taskbook-review-20261028/`。仓库副本做了排版和参数化，测量入口的AST与原probe一致；JSON同时记录原测量文件和仓库副本的SHA-256。这里只打包已运行的方法，没有为整理文档新增样本。
