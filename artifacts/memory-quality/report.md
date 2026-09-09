# Memory V2 Quality Report

- Suite: `memory-v2-quality-v2` / `full`
- Commit: `92fd37dd1d7b3e014ee563001c61b6709eae7a64`
- Dataset: `5dfc20b7c1de038d985ce14d773ca0d73c1c731134366b039db55e5d89af71d4`
- Cases: 19/19 passed
- Failed IDs: none
- Duration: 4.507s

| Metric | Value | Numerator/denominator |
|---|---:|---:|
| `average_consolidation_requests_per_claim` | 0.0 | 0/12 |
| `average_context_characters` | 245.21052631578948 | 245.211/1 |
| `average_extraction_requests_per_event` | 0.8571428571428571 | 12/14 |
| `average_query_embedding_requests_per_query` | 0.043478260869565216 | 1/23 |
| `blank_evidence_rate` | 0.0 | 0/1 |
| `bot_evidence_rate` | 0.0 | 0/2 |
| `bot_subject_rate` | 0.0 | 0/2 |
| `case_pass_rate` | 1.0 | 19/19 |
| `conflict_coactivation_rate` | 0.0 | 0/1 |
| `conflict_resolution_accuracy` | 1.0 | 1/1 |
| `contested_context_leak_rate` | 0.0 | 0/1 |
| `context_latency_p50_ms` | 0.03550015389919281 | 0.0355002/1 |
| `context_latency_p95_ms` | 0.03990018740296364 | 0.0399002/1 |
| `context_precision` | 1.0 | 16/16 |
| `context_recall` | 1.0 | 16/16 |
| `correction_resolution_accuracy` | 1.0 | 1/1 |
| `cross_group_contamination_rate` | 0.0 | 0/13 |
| `cross_person_contamination_rate` | 0.0 | 0/10 |
| `duplicate_active_fact_rate` | 0.0 | 0/23 |
| `duplicate_evidence_rate` | 0.0 | 0/12 |
| `empty_query_fact_leak_rate` | 0.0 | 0/1 |
| `evidence_provenance_accuracy` | 1.0 | 12/12 |
| `extraction_latency_p50_ms` | 18.346300115808845 | 18.3463/1 |
| `extraction_latency_p95_ms` | null | 0/0 |
| `fact_accuracy` | 1.0 | 27/27 |
| `fact_state_accuracy` | 1.0 | 27/27 |
| `fact_without_evidence_rate` | 0.0 | 0/11 |
| `historical_regression_rate` | 0.0 | 0/1 |
| `idempotency_failure_rate` | 0.0 | 0/1 |
| `mean_reciprocal_rank` | 1.0 | 1/1 |
| `ndcg_at_k` | 1.0 | 1/1 |
| `outbound_evidence_rate` | 0.0 | 0/1 |
| `pipeline_error_rate` | 0.0 | 0/19 |
| `precision_at_k` | 1.0 | 16/16 |
| `quality_suite_total_ms` | 986.1558999400586 | 986.156/1 |
| `rebuild_duplicate_commit_rate` | 0.0 | 0/1 |
| `rebuild_historical_overwrite_rate` | 0.0 | 0/1 |
| `rebuild_receipt_accuracy` | 1.0 | 1/1 |
| `rebuild_resume_accuracy` | null | 0/0 |
| `rebuild_review_bypass_rate` | 0.0 | 0/1 |
| `recall_at_k` | 1.0 | 16/16 |
| `retraction_resolution_accuracy` | null | 0/0 |
| `retrieval_latency_p50_ms` | 13.014200143516064 | 13.0142/1 |
| `retrieval_latency_p95_ms` | 21.314800018444657 | 21.3148/1 |
| `scope_attribution_accuracy` | 1.0 | 12/12 |
| `source_event_mismatch_rate` | 0.0 | 0/12 |
| `subject_attribution_accuracy` | 1.0 | 12/12 |
| `third_party_global_leak_rate` | 0.0 | 0/1 |
| `third_party_misattribution_rate` | 0.0 | 0/1 |
| `total_model_requests` | 12.0 | 12/1 |
| `total_query_embedding_requests` | 1.0 | 1/1 |
| `unknown_subject_acceptance_rate` | 0.0 | 0/1 |
| `wrong_group_context_rate` | 0.0 | 0/16 |
| `wrong_subject_context_rate` | 0.0 | 0/16 |
| `wrong_target_retrieval_rate` | 0.0 | 0/16 |
