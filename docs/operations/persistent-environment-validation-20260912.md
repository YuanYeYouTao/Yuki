# Persistent environment deployment — 2026-09-12

[中文](persistent-environment-validation-20260912.zh-CN.md)

The persistent workspace, terminal, package and service tools are deployed. The
release also includes ASR commit `1339930` and the previously merged memory fixes.
Implementation spans `18b5265`, `40af234`, `6d036d8`, `1dd3f95` and `ef28d91`.

## Deployed components

| Component | Running version |
| --- | --- |
| Bot | `ghcr.io/yuanyeyoutao/yuki-qqbot:persistent-1dd3f95` |
| Bot image digest | `sha256:65a9a8a31a1ffd84a839d9a2a9890df6fa81adad2d4f39431fdf503d4425954c` |
| Host Manager | `ef28d91`, Python 3.12 venv, websockets 15.0.1 |
| Execution image | `yuki-environment:20260912` |
| Execution image digest | `sha256:65bcf15180e96c026f0d898cd9c0e89fc50a4ec9072e82ee62a64f0b9908f5f5` |
| OpenSandbox execd | `441042c288a3eacbe93b36236a9baf6f6254ad4d` |
| Database revision | `0055` (`0055_audio_transcripts.py`) |
| Frozen main Agent manifest | 168 tools; `ce06e5b3812ccf0965b8c8b8413e3696e37e64e1442eb86ef3b590606ef3e4d0` |

Images were built locally, transferred with SHA256 verification and loaded on the
server. Only the Bot Compose service was recreated, preserving all existing override
files. Docker and SnowLuma were not restarted. The production environment container
`78a71ac5fa3e` survived Bot and Manager restarts; the QQ gateway remained `9e7a1a89696e`.
Storage mounts precede Manager startup, and Manager owns container restart/recovery.

All 23 existing artifacts were migrated with content hashes verified. Old IDs and
immutable snapshots remain usable. The 2 GiB home and 128 MiB runtime loop filesystems
are mounted; filesystem metadata makes usable capacity smaller than nominal size.
Published artifacts retain the separate 512 MiB/1,000-object limit. System package
growth uses a 1 GiB admission/monitor budget, not a hard overlay quota.

## Focused verification

- Changed Python code passed Ruff and Linux-platform mypy checks. The concentrated
  Windows workspace/sandbox/automation run passed 8 tests, with 3 POSIX-only skips;
  Linux persistent-environment checks passed all 5 tests. No unrelated full suite ran.
- Actual main-entry HTTP payload comparisons covered Chat Completions and Responses,
  including plugin, scheduled and continuation entries and their complete tool declarations.
- Real local execd checks covered shared files, Chinese paths, version conflicts,
  immutable publication, PTY input/state, incremental output, cancellation, legacy
  `run_python` exports and tasks finishing while Manager was disconnected.
- Real dependency checks installed pip `humanize==4.15.0` and apt `hello`, rebuilt
  from a checkpoint and verified both remained usable. Separate checks covered
  service restoration, ordinary-job interruption without replay, network failure,
  isolated cgroup OOM, interrupted apt and repair. Writable-home fake executables
  and shell startup files could not replace privileged package commands.
- Server acceptance used a separate gVisor container with small test filesystems:
  ordinary UID 10001, shared file visibility, real ENOSPC and subsequent recovery,
  public HTTPS through the proxy and rejection of new host SSH connections.
- A separate real execd reconnect check verified exactly one execution and one
  canonical receipt after Manager disconnection, repeated submission of the same
  request, and repeated acknowledgement. No real QQ messages were sent; isolated
  validation containers had no Bot or QQ credentials.

Production health reported database OK, OneBot connected, ASR enabled/configured,
completion and continuation workers running without errors, automation and plugin
workers running, and RSS application/database healthy. The frozen manifest contains
all persistent tools and no NetEase tools. Both remaining MCP servers connected.
Rollup was running with no expired processing leases or recent infrastructure error.
No unacknowledged sandbox completion remained at the observation point. Bot and
Manager restart counters were zero after the final switch.

Existing contested-memory records and historical dream-model failures remain visible
in health; this deployment does not claim to resolve those separate data conditions.

## Server cleanup and measurements

NetEase MCP and the old music-sign container are stopped with restart policy `no`.
The music-card plugin remains disabled and its configuration/data are retained.
`multipathd` and its socket, `ModemManager`, `fwupd` and refresh timer/service, and
`udisks2` are inactive and masked, including their automatic activation entries.
RSS, QQ, proxies, Docker and system maintenance remain enabled.

The first deployment exposed a persisted NetEase enable switch overriding the file
configuration. Its original state was saved, then the persisted switch was disabled;
the Bot recovered and the next manifest excluded NetEase. Two old deployment-probe
completion events had no Bot source record. Their payloads were retained in
`quarantined_completion_outbox` with an explicit reason instead of fabricating a
source or generating a continuation.

| Measurement | Observed value |
| --- | --- |
| Host physical memory | 1,612 MiB |
| Available memory before service cleanup | 607 MiB |
| Available memory immediately after service cleanup | 906 MiB |
| Available memory after final deployment/cleanup | about 747 MiB |
| Swap used after final deployment/cleanup | about 799 MiB |
| Manager process PSS | about 23 MiB |
| Idle execution container, Docker stats | 20–44 MiB across samples |
| execd guest-reported RSS | about 37 MiB |
| gVisor sentry + gofer host PSS, later sample | about 28 MiB |
| Separate gVisor test with a 64 MiB allocation | 135.5 MiB container usage; 1.19% CPU |
| Disk space reclaimed | 2,971,820,032 bytes, about 2.77 GiB |
| Final root-filesystem free space | about 8.9 GiB; 77% used |

Memory samples include changing application load, cache and swap activity. The
299 MiB initial availability increase cannot all be attributed to stopped services;
there is no controlled measurement proving the original 50–70 MiB estimate. Guest
RSS, host PSS and container accounting overlap and must not be added together.

Cleanup removed five obsolete backups, uploaded image archives, duplicate staging
trees, unmounted acceptance filesystems and eleven unused old Bot/rollback/test
image tags. It retained live images, the compatible fallback image and all user data.
No broad Docker volume or image prune was used.

## Retained recovery point

Only this consistent backup remains:

`/opt/yuki-qqbot/backups/pre-environment-final-20260912T145430`

`/opt/yuki-sandbox/latest-restore-point` points to it. It contains database/config,
artifacts, persistent home/runtime files, Manager source/receipts/units, Compose
overrides, original service startup states and migration records. Three copied SQLite
databases passed `quick_check`. `complete.json`, `health-final.json`, `cleanup.json`
and `runtime-process-memory.json` record the verification and measurements. The
directory is mode 0700 because the preserved configuration contains credentials.

The compatible fallback is `persistent-40af234` (image digest
`sha256:923d4a0618f79ddbd0feb6bfc4a482782aa542abf75173f461e10c74b6f44026`).
It understands database revision `0055` and the persistent environment contracts.
Reverting Bot alone means selecting that image in the final Compose override and
recreating only Bot with all overrides. Keep the current database, home, runtime,
Manager and execution receipts. Do not restore an older database over new messages,
files or completion records. Full host reboot was not performed on production;
recovery semantics were exercised in the isolated environment and unit ordering was
verified on the server.
