# Third-party notices for the Code Mode experiment

Yuki's original [LICENSE](LICENSE) is retained. This record covers third-party
components used by the Code Mode distribution. Design references are documented
separately in [the architecture record](docs/architecture/pi-port-provenance.md).

Monty is built from `3f9d6ef413fb951e5b80113b7088d535bd028fcb` with the recorded
string-cache iterator patch. Its [original MIT license](vendor/monty/LICENSE)
and the [Apache 2.0 license](vendor/monty/TYPESHED-LICENSE) for its vendored
typeshed at `0e16ea31d2e188fdc126cb31e7c4fcc6b5a8da96` are retained separately.

[The machine-readable audit](vendor/monty/THIRD_PARTY_NOTICES.json) records
598 Cargo.lock packages: 580 registry archives verified against their exact
checksums and 18 local workspace packages. It preserves declared SPDX
expressions, notice texts with hashes, and native worker / CPython binding
normal/build tree membership. The committed Darwin audit covers 352 target
packages and 326 distinct texts. Lock membership does not imply shipping every
package or selecting one license from an alternative-license expression.
Each Linux build generates its own audit beside the actual worker and wheel.

The [notice supplement evidence](docs/architecture/pi-codemode-evidence/monty-notice-supplement.json)
records how the eight previously missing text entries were resolved, without
changing the recorded Darwin artifacts. `r-efi` carries complete MIT terms and
original copyright statements in its archived AUTHORS files. Four other
packages use original license texts from fixed publication source revisions,
verified against their archived authored payload; generated files and winapi's
publish-only version edit are listed separately. No floating branch was used.

`quote-use-0.8.4` and `quote-use-macros-0.8.4` explicitly declare MIT in their
original manifests. Their pinned archives/upstream source do not supply a
license file or copyright attribution. The audit attaches the fixed
[SPDX standard MIT text](https://spdx.org/licenses/MIT.html) to those original
MIT declarations and separately retains both upstream omissions. The standard
text's year/holder placeholders are kept as template text, never presented as
package copyright. Unknown or changed declarations cannot use this fallback.
`license_text_audit_complete=true` describes text inventory, not a certification
that upstream supplied every notice or a legal selection of alternatives.

Historical [P09 Linux evidence](docs/architecture/pi-codemode-evidence/p09-linux-distribution.json)
records 353 target packages, 321 texts and eight missing entries before this
supplement. Its hashes and results are retained as history; current image
verification is recorded separately in the delivery document.

The reproducible source/build procedure is
`scripts/build_monty_distribution.sh OUTPUT_DIR ABSOLUTE_PYTHON`.
`artifacts.json` identifies the resulting files by actual SHA256. Worker bytes
were identical on the repeated local build; wheel packaging timestamps can
change its archive hash, so each build records its own wheel hash.

Both Dockerfiles retain the generated target audit and separate Yuki,
Monty, and typeshed licenses. Compilation and audit use separate build layers;
transient public downloads retry a bounded number of times, and exhausted
transport failures fail the audit rather than becoming missing-license records.
Image build results are recorded in the delivery document; no image has been
published or deployed.

## Moby container security profiles

The pinned seccomp baseline and derived Code Mode seccomp/AppArmor policies under `deploy/security` derive from Moby profiles commit `2ceae35d351c156cb5a8efc0fdc4a08cf94569d8`, copyright The Moby Authors, Apache License 2.0. The full license is `deploy/security/MOBY-PROFILES-LICENSE`; provenance and exact generation are recorded in `deploy/security/README.md` and `scripts/build_codemode_seccomp.py`.
