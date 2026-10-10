# Kira drill results

Written by `scripts/run_drills.py`; full answers in `results.jsonl`.
✅/❌: named the right service · named the right cause · called the expected tools.

## 2026-10-10 02:11 UTC · `us.anthropic.claude-sonnet-4-6`

| Scenario | Service | Cause | Tools | Result | Restored | Tools called | Tokens | Time |
|---|---|---|---|---|---|---|---|---|
| scaled_to_zero | ✅ | ✅ | ✅ | **pass** | ✅ | fetch_service_health, fetch_recent_changes, fetch_metrics, fetch_metrics, fetch_logs, fetch_metrics, fetch_recent_changes, scale_deployment | 57,847 | 35s |
| crash_loop | ✅ | ✅ | ✅ | **pass** | ✅ | fetch_service_health, fetch_recent_changes, fetch_logs, fetch_metrics, fetch_logs, fetch_metrics, fetch_metrics, fetch_recent_changes, fetch_logs, fetch_logs, fetch_logs | 45,613 | 64s |
| bad_image | ✅ | ✅ | ✅ | **pass** | ✅ | fetch_service_health, fetch_recent_changes, fetch_logs, fetch_logs, fetch_metrics, fetch_logs, fetch_logs, fetch_recent_changes, fetch_recent_changes | 41,201 | 41s |

3/3 passed.

## 2026-10-10 02:15 UTC · `us.anthropic.claude-sonnet-4-6`

| Scenario | Service | Cause | Tools | Grader | Result | Restored | Tools called | Tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| scaled_to_zero | ✅ | ✅ | ✅ | ✅ | **pass** | ✅ | fetch_service_health, fetch_recent_changes, fetch_logs, fetch_logs, fetch_metrics, fetch_recent_changes, fetch_logs, fetch_metrics | 73,441 | 41s |
| crash_loop | ✅ | ✅ | ✅ | ❌ The assistant blamed a broken container image deployed via CI/CD commit, when the real cause was a kubectl command override that changed the container's startup command to print a fatal error and exit 1, with no image change involved. | **fail** | ✅ | fetch_service_health, fetch_recent_changes, fetch_logs, fetch_logs, fetch_metrics, fetch_metrics, fetch_logs, fetch_logs, fetch_recent_changes | 38,142 | 41s |
| bad_image | ✅ | ✅ | ✅ | ❌ The assistant correctly identified the bad image tag but incorrectly attributed the trigger to a 'kira-drill-restore' scale event and an Argo CD sync, rather than a direct kubectl image set by an engineer; it also conflated the timeline with unrelated commits and syncs. | **fail** | ✅ | fetch_service_health, fetch_metrics, fetch_metrics, fetch_recent_changes, fetch_logs, fetch_metrics | 22,776 | 33s |

1/3 passed.

## 2026-10-10 02:18 UTC · `us.anthropic.claude-sonnet-4-6`

| Scenario | Service | Cause | Tools | Grader | Result | Restored | Tools called | Tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| scaled_to_zero | ✅ | ✅ | ✅ | ❌ The assistant correctly identified the manual scale-to-zero but attributed it to an 'unknown actor post-sync' and implied the ArgoCD sync may have been related, rather than clearly identifying it as a direct kubectl scale command unrelated to any git or ArgoCD activity. | **fail** | ✅ | fetch_service_health, fetch_recent_changes, fetch_metrics, fetch_logs | 23,713 | 34s |
| crash_loop | ✅ | ✅ | ✅ | ✅ | **pass** | ✅ | fetch_service_health, fetch_recent_changes, fetch_logs, fetch_logs, fetch_metrics, fetch_logs | 38,241 | 55s |
| bad_image | ✅ | ✅ | ✅ | ✅ | **pass** | ✅ | fetch_service_health, fetch_recent_changes, fetch_metrics, fetch_logs, fetch_metrics | 26,318 | 31s |

2/3 passed.

## 2026-10-10 02:22 UTC · `us.anthropic.claude-sonnet-4-6`

| Scenario | Service | Cause | Tools | Grader | Result | Restored | Tools called | Tokens | Time |
|---|---|---|---|---|---|---|---|---|---|
| scaled_to_zero | ✅ | ✅ | ✅ | ✅ | **pass** | ✅ | fetch_service_health, fetch_recent_changes, fetch_logs, fetch_logs, fetch_metrics | 26,312 | 39s |
| crash_loop | ✅ | ✅ | ✅ | ✅ | **pass** | ✅ | fetch_service_health, fetch_recent_changes, fetch_logs, fetch_logs, fetch_logs, fetch_metrics | 28,758 | 36s |
| bad_image | ✅ | ✅ | ✅ | ✅ | **pass** | ✅ | fetch_service_health, fetch_recent_changes, fetch_logs, fetch_logs, fetch_metrics | 27,252 | 37s |

3/3 passed.
