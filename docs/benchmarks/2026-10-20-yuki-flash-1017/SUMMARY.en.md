# Yuki × DeepSeek Flash 1017: evaluation summary

Yuki's native Main / Work / tool runtime resolved **216/300 tasks (72.00%) on SWE-bench Lite test** and passed **66/89 tasks (74.16%) on Terminal-Bench 2.0**. Both denominators cover the full formal datasets.

The measured source is [313b6b9bdf07d411751db13b0a35f8f634f0035b](https://github.com/YuanYeYouTao/Yuki/commit/313b6b9bdf07d411751db13b0a35f8f634f0035b), before 4.0.0. These are historical measurements supporting the release documentation, **not a rerun of the 4.0.0 tag**. The batch started on October 20, 2026; the final report snapshot is October 21, 01:25:47 UTC+8.

[Detailed Chinese report](README.md) · [Machine-readable summary](summary.json) · [Leaderboard submission guide](SUBMISSION.md)

## Method

The adapter connected Yuki's own agent runtime to official task containers. Another coding agent did not produce the answers. SWE patches were graded by swebench 5.0.2; terminal artifacts were graded by Harbor 0.1.44. Each task has one formal model attempt. Calibration and earlier infrastructure-failed attempts are excluded.

The model was DeepSeek Flash 1017, using the API alias deepseek-flash, thinking enabled and high reasoning effort. Requests allowed 393216 output tokens. No extra total model/tool-call budget or finite HTTP timeout was set; Code Mode and subagents were disabled. Agent-selected command timeouts and official task limits still applied. Container preparation had no additional finite deadline; official TB agent/verifier limits retained multiplier 1.0.

Execution used an i9-13900H host with 32 GB RAM, Windows/WSL2 Linux and Docker, without a GPU. SWE concurrency changed from 2 to 4; TB from 1 to 2. This is not a controlled throughput comparison.

- SWE dataset revision: b0dde1093fe417d83b7184254edf8199c1f0dff5, Lite test 300.
- TB task repository revision: 69671fbaac6d67a7ef0dfec016cc38a64ef7a77c, version 2.0, 89 tasks.

## Usage and cache coverage

| Scope | Cumulative input | Cached input | Output | Total tokens | Weighted cache hit rate |
|---|---:|---:|---:|---:|---:|
| SWE 300, complete | 463,412,887 | 451,694,208 | 5,537,393 | 468,950,280 | **97.47%** |
| TB 84 complete tasks | 183,840,675 | 180,637,952 | 2,241,022 | 186,081,697 | **98.26%** |
| Known fields across TB 89 | 207,694,348 | 204,195,584 | 2,629,959 | 210,324,307 | **98.32%, known subset** |

Cache hit rate is summed cached-input tokens divided by summed input tokens on jointly known records, not an average of task percentages. Tokens are cumulative request usage, including repeated context.

SWE has complete coverage for 8,290 physical requests. TB records 3,094 invocation rows and 3,092 physical requests; five tasks fail the strict completeness check. Three have a logical request-count/record-count difference; this alone does not prove an unrecorded request was dispatched or billed. Two include a physical0 local WorkspaceError row with null tokens. Nulls remain unknown, and 98.32% is not presented as full 89-task coverage.

## Outcomes and limitations

TB retains 66 reward1, 22 reward0 and one rewardnull. Of 17 AgentTimeoutError outcomes, six still received reward1. Harbor ran the verifier after cancellation, and these six are already part of the original 66/89. Their recorded agent wall time exceeded nominal limits by 11.43–30.16 seconds. The entire excess is not established to be pure cleanup, nor is every background artifact's cutoff time established. Both timeout and reward labels are retained; no manual exception or regrading increased the score.

The 23 TB nonpasses are classified by primary evidence: eight functional/artifact-contract mismatches, five unfinished artifacts/runtime behavior at the official deadline, eight verifier dependency/environment blockers, one native runtime failure, and one missing completed verification result. Four environment blockers followed agent-selected apt installation timeouts and an interrupted package-manager state.

The 84 SWE unresolved cases comprise 68 F2P-only, seven F2P-and-P2P, eight P2P-only and one unparseable test result. These labels describe official test outcomes, not 84 proven code-level root causes. Missing/skipped tests and dependency symptoms are not automatically called patch regressions.

These measurements demonstrate that Yuki's native runtime can carry real repository-repair and terminal workflows, while exposing weaknesses in delivery-contract checks, recovery after installation failures and finishing within task limits. There is no repeated-seed stability estimate, same-model agent comparison, official leaderboard submission or claimed global rank. Later TB 2.1 revisions are version context, not corrections to this score.

[Per-task SWE JSON](swe-bench-lite.tasks.json) / [CSV](swe-bench-lite.tasks.csv), [TB JSON](terminal-bench.tasks.json) / [CSV](terminal-bench.tasks.csv), failure analyses and small official receipt extracts are included. The package excludes secrets, user identities, problem statements, gold answers, patch bodies, full trajectories, verifier source and databases.
