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

## Remaining live verification

No real QQ recipient/group was designated, so no message, poke or recall was sent
to users for testing. NapCat/SnowLuma's real action compatibility must not be inferred
from the mock Provider test. Deployment status and host pressure/connection checks
are recorded separately after activation; these local results alone are not a claim
that production is running the new build.
