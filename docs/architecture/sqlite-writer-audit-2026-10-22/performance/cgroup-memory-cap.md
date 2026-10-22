# Linux memory cap wrapper

`cgroup_memory_cap.py` runs an unchanged Python benchmark in a fresh cgroup-v2
leaf. It sets `memory.max`, `memory.swap.max=0`, and `memory.oom.group=1` before
starting the benchmark. It does not enable parent controllers or alter host
limits. The memory, CPU and I/O controllers must already be available.

Run the wrapper as root and explicitly select an existing unprivileged account.
The child drops supplementary groups, GID and UID before running the benchmark.
The benchmark's scratch and output directories must be writable by that account.

```sh
sudo python cgroup_memory_cap.py \
  --user YOUR_LINUX_USER --memory-mib 512 \
  --sidecar /path/to/output/old1.resources.json -- \
  /path/to/venv/bin/python /path/to/benchmark_sqlite_writer.py \
  --checkout /path/to/native/old-checkout \
  --output /path/to/output/old1.benchmark.json \
  --scratch /tmp/your-owned-scratch/old1 \
  --scenario mixed --messages 12 --interval 1 \
  --background 12 --background-interval 0.2 --cpu-cores 2
```

Use the same Python, benchmark script bytes, workload and cap for each run, with
separate clean old/new source checkouts and fresh scratch directories. Run the
complete old → new → new → old sequence. Native Linux Git must be able to resolve
each checkout; a Windows worktree pointer is not a native Linux checkout.

The sidecar records actual limits, child status, the benchmark script SHA-256,
kernel counters, process resource usage and stdout/stderr paths. The original
benchmark JSON remains the timing evidence. The wrapper does not retry a failed
run, adjust the limit or kill leftover descendants. It removes an empty leaf and
records a nonempty one for explicit follow-up.

`memory.peak` is charged cgroup memory, including cache/kernel pages and
descendants. It is not process RSS. Linux `ru_maxrss` is in KiB. Resource sidecars
cover the whole process lifetime, including imports, setup, verification and
output; benchmark timing covers its declared timed region. Empty `io.stat` and
missing child metrics are retained as such. A successful run far below the cap
does not demonstrate behavior near memory exhaustion.

The measured 2026-10-23 runs used this mechanism with the account supplied by a
local default. The attachment removes that machine-specific default and requires
`--user`; it changes no cap, workload or measurement behavior.

Sources: [cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html),
[/proc](https://docs.kernel.org/filesystems/proc.html),
[Python resource counters](https://docs.python.org/3.12/library/resource.html).
