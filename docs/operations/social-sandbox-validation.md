# Social/workspace/sandbox acceptance record

## Local deterministic checks

- Full suite: **800 passed**, 195 existing SQLite datetime deprecation warnings.
- Ruff and `mypy src`: passed (536 source files).
- Memory quality: all 19 fixtures passed; baseline comparison has no regressions.
- Release validation for 3.8.2 and both Compose configurations: passed.
- Targeted delivery tests cover real repositories plus a fake OneBot handle: correct
  private/group ledger ownership, file handoff cleanup, recall, group members, poke,
  unknown-contact rejection, paused routes and uncertain replay without another call.
- Workspace tests cover TTL across reopen, read/rename without extension, CAS,
  quota, traversal/hardlink rejection, and all-or-nothing artifact publication.

These are code-contract tests, **not real QQ delivery confirmation**.

## Actual local runsc exercise

Used a disposable nested Docker host on the local machine, with runsc
`release-20260831.0` (archive SHA-512 checked during build). The outer privileged
container is only a trusted local test host. Actual Python jobs use the unprivileged
runsc resource template, not that outer container.

Observed outcomes:

| Probe | Result |
| --- | --- |
| Python, Pillow/openpyxl/pypdf and artifact import | Successful |
| Public HTTPS and private/metadata/host rejection | Successful |
| Infinite loop | Failed within configured timeout |
| 512 MiB allocation against 256 MiB limit | Failed, no artifacts |
| Fork pressure against process limit | Task aborted; later jobs still execute |
| 100,000-character stdout | Successful with 32 KiB truncation |
| Symlink artifact to `/etc/passwd` | Rejected, no artifact published |

Local DNS was intercepted by the desktop proxy and returned fake-IP 198.18.* for
PyPI. This was correctly rejected. For this local exercise only, PyPI's public
address was independently retrieved over TLS-verified DNS-over-HTTPS and pinned
in Squid's test hosts file. **No private-IP exception was added.** The production
configuration does not include that test pin; production DNS independently returned
public addresses and must be checked again during activation.

## Production activation

- PR: #66. Bot image was built locally, transferred with SHA-256 verification;
  no remote build and no Docker daemon restart.
- Image: `ghcr.io/yuanyeyoutao/yuki-qqbot:social-0a38550`, ID
  `sha256:af26f227114b8803664520e512b3c169aaeac336d8b1e5d61107f85d28442b74`.
- The isolated 0051 to 0052 rehearsal preserved content hashes and row counts of
  all 102 existing tables; integrity and foreign-key checks passed.
- Host runsc passed all seven probes above with production DNS, without a hosts pin.
- Real systemd testing exposed two deployment-specific issues: UMask removed input
  directory traversal for the sandbox UID, and runtime-directory recreation left
  Bot's bind mount stale. Explicit input permissions and RuntimeDirectoryPreserve
  fix these. Bot UID 10001 executed Python successfully and retained socket access
  across a manager restart. Cancellation now exits the service cleanly.
- Final Bot restart: 2026-09-11 05:22:48 +08:00. SnowLuma retains its original
  2026-08-30 start time; no QQ test messages were sent.
- Rollback backup: `/opt/yuki-qqbot/backups/pre-social-0a38550`; retained image
  `yuki-rollback:pre-social-0a38550`. The old binary expects 0051: preserve new data
  and assess schema compatibility before rollback, never blindly overwrite new messages.

## Remaining live verification

No real QQ recipient/group was designated, so no message, poke or recall was sent
to users for testing. NapCat/SnowLuma's real action compatibility must not be inferred
from the mock Provider test. These actions remain explicitly unverified end-to-end.
