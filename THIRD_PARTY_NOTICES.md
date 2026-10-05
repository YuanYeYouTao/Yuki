# Third-party notices for the Code Mode experiment

Yuki's original [LICENSE](LICENSE) is retained. This record covers third-party
components used by the Code Mode distribution. Design references are documented
separately in [the architecture record](docs/architecture/pi-port-provenance.md).

Monty is built from `3f9d6ef413fb951e5b80113b7088d535bd028fcb` with the recorded
string-cache iterator patch. Its [original MIT license](vendor/monty/LICENSE)
and the [Apache 2.0 license](vendor/monty/TYPESHED-LICENSE) for its vendored
typeshed at `0e16ea31d2e188fdc126cb31e7c4fcc6b5a8da96` are retained separately.

[The machine-readable audit](vendor/monty/THIRD_PARTY_NOTICES.json) records
all 598 Cargo.lock packages, exact archive checksums, declared SPDX expressions,
complete available license/notice texts with hashes, and membership in the
native worker / CPython binding build trees. The committed audit is for
`aarch64-apple-darwin`: 352 target packages and 321 distinct notice texts.
It does not assert that all 598 packages are shipped, or select a license from
an alternative-license expression. Each Linux distribution build generates
its own target-specific audit beside its actual worker and wheel.
The verified [Linux target audit](docs/architecture/pi-codemode-evidence/p09-linux-distribution.json)
records 353 target packages and the actual worker, launcher and installed wheel
hashes. Its 321 full notice texts were verified identical to the shared audit;
the target report references those texts by hash rather than duplicating them.

Eight package archives and their available exact upstream source lack complete
license text. Two are in the current target tree: `quote-use-0.8.4` and
`quote-use-macros-0.8.4` (declared MIT; exact packaged upstream commit
`05096f346f8b17fba8a57a235b958917ad9d99e0`). Six are lock-only on this target:
`r-efi-5.3.0`, `r-efi-6.0.0`, `rustls-platform-verifier-android-0.1.1`,
`symbolic-common-12.18.3`, `winapi-i686-pc-windows-gnu-0.4.0`, and
`winapi-x86_64-pc-windows-gnu-0.4.0`. The audit preserves their license metadata
and marks `license_text_audit_complete=false`; no copyright attribution is
invented. External publication has not been performed and this audit does not
claim those missing notices have been resolved.

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
