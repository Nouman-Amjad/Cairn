"""Emit the scenario corpus as YAML files.

Run once; the output is committed and then edited by hand. Scenarios are data
so that an SRE can add one without opening any Python — this script exists to
bootstrap thirty of them, not to be the source of truth.

    python services/cairn-eval/tools/generate_scenarios.py

Twenty solvable, ten unsolvable-by-design. The ten matter more than the
twenty: an agent that answers them confidently is worse than one that
abstains, and `abstention_precision` is the metric that says so.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

OUT = Path(__file__).resolve().parents[1] / "scenarios"


def series(name: str, labels: str, before: float, after: float, at: int = 30) -> dict[str, Any]:
    return {
        "metric": name,
        "labels": labels,
        "points": 60,
        "min": min(before, after),
        "max": max(before, after),
        "mean": round((before + after) / 2, 4),
        "first": before,
        "last": after,
        "series": [[i, before if i < at else after] for i in range(60)],
    }


def flat(name: str, labels: str, value: float) -> dict[str, Any]:
    return series(name, labels, value, value)


def logs(
    lines: list[str], pod: str, level: str = "error", repeat: int = 400
) -> list[dict[str, Any]]:
    """Compact log fixtures.

    A real incident window produces thousands of near-identical lines, and the
    server-side token cap only means something if the fixtures reach that
    volume. Writing them out literally would put 1,200 near-identical rows in
    a YAML file nobody can review, so each entry carries an `occurrences`
    count and the fake world expands it at load time.
    """
    return [
        {
            "ts": f"2026-07-26T03:{(offset * 7) % 60:02d}:00Z",
            "line": line,
            "pod": pod,
            "level": level,
            "occurrences": repeat,
        }
        for offset, line in enumerate(lines)
    ]


#: All scenarios are anchored to a fixed incident time so timestamps are
#: stable across regenerations and a diff shows real changes only.
INCIDENT_AT = datetime(2026, 7, 26, 3, 0, tzinfo=UTC)


def deploy(sha: str, minutes_ago: int, service: str, who: str = "rmoore") -> dict[str, Any]:
    at = INCIDENT_AT - timedelta(minutes=minutes_ago)
    return {
        "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "revision": sha,
        "source": "argocd",
        "service": service,
        "creator": who,
    }


def runbook(doc_id: str, title: str, content: str) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "title": title,
        "content": content,
        "matched_by": "semantic #1 + keyword #1",
    }


# --------------------------------------------------------------- solvable

SOLVABLE: list[dict[str, Any]] = [
    {
        "id": "inc-001",
        "title": "Connection pool exhaustion after a deploy raised concurrency",
        "archetype": "resource_exhaustion",
        "question": "why did checkout-api latency spike at 03:06?",
        "ground_truth": {
            "root_cause": (
                "The 03:02 deploy of checkout-api raised request concurrency without "
                "raising maxPoolSize, so the DB connection pool saturated and requests "
                "queued behind a 30s acquire timeout."
            ),
            "causal_service": "checkout-api",
            "contributing_deploy": "9f2c1ab4de11",
            "required_evidence": [
                {"metric": "checkout_db_pool_wait_seconds"},
                {"log_pattern": "TimeoutError: QueuePool limit"},
                # The deploy itself is the event, identified by the revision
                # the agent can actually retrieve from get_deploy_timeline.
                {"event": "9f2c1ab4de11"},
            ],
        },
        "distractors": [
            "unrelated frontend deploy at 02:45",
            "correlated but non-causal CPU spike on payments-api",
        ],
        "world": {
            "get_deploy_timeline": [
                deploy("9f2c1ab4de11", 178, "checkout-api"),
                deploy("aa41bb92cc03", 195, "frontend", "jpatel"),
            ],
            "query_metrics": [
                series("http_request_duration_p95", "app=checkout-api", 0.18, 2.4),
                series("checkout_db_pool_wait_seconds", "app=checkout-api", 0.01, 29.8),
                series("checkout_db_pool_in_use", "app=checkout-api", 4, 20),
                series("container_cpu_usage", "app=payments-api", 0.3, 0.78),
            ],
            "query_logs": logs(
                [
                    "TimeoutError: QueuePool limit of size 20 overflow 0 reached",
                    "HikariPool-1 - Connection is not available, request timed out",
                    "upstream request timeout after 30000ms",
                ],
                "checkout-api-7f9d4b",
            ),
            "search_runbooks": [
                runbook(
                    "runbooks/db-pool.md",
                    "Connection pool exhaustion",
                    "Symptoms: 'QueuePool limit ... reached'. Usually a release that "
                    "raised concurrency without raising maxPoolSize. Fix: roll back, "
                    "then size the pool against the new concurrency.",
                )
            ],
        },
    },
    {
        "id": "inc-002",
        "title": "Connection pool exhaustion after an RDS replica downscale",
        "archetype": "resource_exhaustion",
        "question": "checkout-api started timing out at 03:00, no deploy went out — why?",
        "ground_truth": {
            "root_cause": (
                "The RDS read replica fleet scaled 3 to 1 at 02:58. checkout-api's pool "
                "max of 20 was sized for three replicas; connection wait exceeded the "
                "30s timeout against the single remaining replica."
            ),
            "causal_service": "checkout-api",
            "contributing_deploy": None,
            "required_evidence": [
                {"metric": "checkout_db_pool_wait_seconds"},
                {"log_pattern": "TimeoutError: QueuePool limit"},
                {"event": "rds_replica_scale_down"},
            ],
        },
        "distractors": [
            "unrelated frontend deploy at 02:45",
            "correlated but non-causal CPU spike on payments-api",
        ],
        "world": {
            "get_deploy_timeline": [deploy("aa41bb92cc03", 195, "frontend", "jpatel")],
            "query_metrics": [
                series("checkout_db_pool_wait_seconds", "app=checkout-api", 0.02, 31.2),
                series("rds_replica_count", "cluster=checkout-db", 3, 1, at=28),
                series("container_cpu_usage", "app=payments-api", 0.3, 0.71),
            ],
            "query_logs": logs(
                [
                    "TimeoutError: QueuePool limit of size 20 overflow 0 reached",
                    "rds_replica_scale_down event observed for cluster checkout-db",
                ],
                "checkout-api-7f9d4b",
            ),
            "search_runbooks": [
                runbook(
                    "runbooks/db-pool.md",
                    "Connection pool exhaustion",
                    "Pool size is sized against replica count. A downscale halves "
                    "effective capacity without any application change.",
                )
            ],
        },
    },
    {
        "id": "inc-003",
        "title": "Memory leak producing OOMKills between restarts",
        "archetype": "memory_leak_oom",
        "question": "search-api keeps restarting since about 02:40, why?",
        "ground_truth": {
            "root_cause": (
                "A memory leak in search-api: the working set climbs steadily between "
                "restarts until it crosses the container limit and the kubelet OOMKills "
                "the pod. Not a burst — the sawtooth is monotonic."
            ),
            "causal_service": "search-api",
            "contributing_deploy": None,
            "required_evidence": [
                {"metric": "container_memory_working_set_bytes"},
                {"log_pattern": "OutOfMemoryError"},
                {"event": "oom_kill"},
            ],
        },
        "distractors": ["a routine deploy six days ago", "elevated GC time on cart-service"],
        "world": {
            "get_deploy_timeline": [deploy("0011aabbccdd", 8820, "search-api")],
            "query_metrics": [
                series("container_memory_working_set_bytes", "pod=search-api-a1b2", 4.1e8, 1.06e9),
                series("kube_pod_container_status_restarts_total", "pod=search-api-a1b2", 0, 7),
                series("jvm_gc_pause_seconds", "app=cart-service", 0.02, 0.09),
            ],
            "query_logs": logs(
                [
                    "java.lang.OutOfMemoryError: Java heap space",
                    "Container killed due to memory limit (OOMKilled), oom_kill event recorded",
                ],
                "search-api-a1b2",
            ),
            "search_runbooks": [
                runbook(
                    "runbooks/oom.md",
                    "OOMKilled pods",
                    "A working set climbing steadily between restarts indicates a leak "
                    "rather than a burst. Raising the limit is a stopgap, not a fix.",
                )
            ],
        },
    },
    {
        "id": "inc-004",
        "title": "Upstream dependency outage surfacing as 502s",
        "archetype": "dependency_outage",
        "question": "checkout-api is returning 502s from 03:10, what broke?",
        "ground_truth": {
            "root_cause": (
                "payments-gateway, an upstream dependency, stopped accepting "
                "connections. checkout-api's circuit breaker opened and it returned "
                "502s. The fault is not in checkout-api."
            ),
            "causal_service": "payments-gateway",
            "contributing_deploy": None,
            "required_evidence": [
                {"log_pattern": "circuit breaker open for payments-gateway"},
                {"metric": "http_requests_total"},
                {"event": "upstream_connection_refused"},
            ],
        },
        "distractors": [
            "checkout-api deploy four days ago",
            "a memory increase on checkout-api that is a symptom of request queuing",
        ],
        "world": {
            "get_deploy_timeline": [deploy("0011aabbccdd", 5760, "checkout-api")],
            "query_metrics": [
                series("http_requests_total", "app=checkout-api,status=502", 0, 340),
                series("container_memory_working_set_bytes", "pod=checkout-api-ee11", 3.9e8, 5.2e8),
            ],
            "query_logs": logs(
                [
                    "upstream connect error to payments-gateway: connection refused",
                    "circuit breaker open for payments-gateway",
                    "upstream_connection_refused recorded",
                ],
                "checkout-api-ee11",
            ),
            "get_traces": [
                {
                    "trace_id": "abc123def456",
                    "root_service": "checkout-api",
                    "root_operation": "POST /checkout",
                    "duration_ms": 30012,
                }
            ],
            "search_runbooks": [],
        },
    },
    {
        "id": "inc-005",
        "title": "Feature flag change with no deploy",
        "archetype": "config_change",
        "question": "error rate for cart-service jumped at 03:20 and there was no deploy — why?",
        "ground_truth": {
            "root_cause": (
                "A config reload enabled the 'new_pricing' feature flag at 03:19, which "
                "exercised a null-unsafe path in PricingResolver. No code was deployed; "
                "the change arrived through configuration."
            ),
            "causal_service": "cart-service",
            "contributing_deploy": None,
            "required_evidence": [
                {"log_pattern": "config reload"},
                {"log_pattern": "NullPointerException in PricingResolver"},
                {"metric": "http_requests_total"},
            ],
        },
        "distractors": [
            "an inventory-api deploy at 02:10 outside the window",
            "a coincident increase in request volume",
        ],
        "world": {
            "get_deploy_timeline": [],
            "query_metrics": [
                series("http_requests_total", "app=cart-service,status=500", 2, 190),
                series("http_requests_total", "app=cart-service", 900, 1150),
            ],
            "query_logs": logs(
                [
                    "config reload: feature flag 'new_pricing' enabled",
                    "NullPointerException in PricingResolver after config reload",
                ],
                "cart-service-9911",
            ),
            "search_runbooks": [
                runbook(
                    "runbooks/flags.md",
                    "Feature flag rollouts",
                    "Flag changes are not deploys and do not appear on the deploy "
                    "timeline. Check config reload log lines for the window.",
                )
            ],
        },
    },
    {
        "id": "inc-006",
        "title": "Noisy neighbour saturating the node",
        "archetype": "noisy_neighbour",
        "question": "inventory-api p99 doubled around 03:30 but its own metrics look fine?",
        "ground_truth": {
            "root_cause": (
                "Another workload saturated node ip-10-0-3-21. inventory-api was CFS "
                "throttled as a result; its own utilisation looks healthy because it is "
                "being denied CPU, not consuming it."
            ),
            "causal_service": "inventory-api",
            "contributing_deploy": None,
            "required_evidence": [
                {"metric": "node_cpu_utilisation"},
                {"metric": "container_cpu_cfs_throttled_seconds"},
                {"event": "node_saturation"},
            ],
        },
        "distractors": [
            "a deploy of notification-worker on the same node",
            "an unrelated increase in inventory-api request volume",
        ],
        "world": {
            "get_deploy_timeline": [deploy("77aa1234bbcc", 240, "notification-worker")],
            "query_metrics": [
                series("node_cpu_utilisation", "node=ip-10-0-3-21", 0.35, 0.98),
                series("container_cpu_cfs_throttled_seconds", "app=inventory-api", 0.1, 14.2),
                series("container_cpu_usage", "app=inventory-api", 0.42, 0.44),
            ],
            "query_logs": logs(
                ["slow response, no errors observed, node_saturation suspected"],
                "inventory-api-c0de",
                level="warn",
                repeat=4,
            ),
            "search_runbooks": [
                runbook(
                    "runbooks/throttling.md",
                    "CFS throttling",
                    "Flat container CPU with rising throttled seconds means the pod is "
                    "being denied CPU. Look at the node, not the pod.",
                )
            ],
        },
    },
    {
        "id": "inc-007",
        "title": "Expired TLS certificate on outbound calls",
        "archetype": "cert_expiry",
        "question": "payments-api started failing all outbound calls at 03:00",
        "ground_truth": {
            "root_cause": (
                "The client certificate payments-api presents to its partner endpoint "
                "expired at 03:00 UTC. Every outbound TLS handshake now fails "
                "verification."
            ),
            "causal_service": "payments-api",
            "contributing_deploy": None,
            "required_evidence": [
                {"log_pattern": "x509: certificate has expired"},
                {"metric": "http_requests_total"},
                {"event": "tls_handshake_failure"},
            ],
        },
        "distractors": [
            "a DNS resolution latency increase that is unrelated",
            "a deploy of payments-api three weeks earlier",
        ],
        "world": {
            "get_deploy_timeline": [deploy("bb22ccdd33ee", 30240, "payments-api")],
            "query_metrics": [
                series("http_requests_total", "app=payments-api,status=0", 0, 500),
                series("coredns_request_duration_seconds", "app=coredns", 0.002, 0.006),
            ],
            "query_logs": logs(
                [
                    "x509: certificate has expired or is not yet valid",
                    "tls: failed to verify certificate, tls_handshake_failure",
                ],
                "payments-api-77aa",
            ),
            "search_runbooks": [
                runbook(
                    "runbooks/certs.md",
                    "Certificate expiry",
                    "An abrupt total failure of outbound calls with x509 errors and no "
                    "deploy is almost always an expiry. Check notAfter.",
                )
            ],
        },
    },
    {
        "id": "inc-008",
        "title": "Kafka consumer group thrashing",
        "archetype": "queue_backlog",
        "question": "notification-worker is lagging badly since 02:50",
        "ground_truth": {
            "root_cause": (
                "The notification-worker consumer group is stuck in a rebalance loop. "
                "Partitions are reassigned faster than they can be processed, so "
                "committed offsets stall and lag grows without bound."
            ),
            "causal_service": "notification-worker",
            "contributing_deploy": None,
            "required_evidence": [
                {"metric": "kafka_consumergroup_lag"},
                {"log_pattern": "NoBrokersAvailable"},
                {"event": "consumer_rebalance"},
            ],
        },
        "distractors": [
            "a producer-side volume increase that is within normal range",
            "an unrelated broker restart the previous day",
        ],
        "world": {
            "get_deploy_timeline": [],
            "query_metrics": [
                series("kafka_consumergroup_lag", "group=notification-worker", 120, 480000),
                series("kafka_consumer_rebalance_total", "group=notification-worker", 0, 14),
                series("kafka_messages_in_total", "topic=notifications", 4200, 4400),
            ],
            "query_logs": logs(
                [
                    "consumer group rebalancing, consumer_rebalance triggered",
                    "NoBrokersAvailable while fetching metadata",
                ],
                "notification-worker-ab12",
            ),
            "search_runbooks": [
                runbook(
                    "runbooks/kafka.md",
                    "Consumer lag",
                    "NoBrokersAvailable during rebalance means the group is thrashing. "
                    "Check session.timeout.ms against processing time.",
                )
            ],
        },
    },
    {
        "id": "inc-009",
        "title": "DNS resolution failures after a CoreDNS config change",
        "archetype": "dns_failure",
        "question": "half of cart-service requests started failing at 03:12 with connection errors",
        "ground_truth": {
            "root_cause": (
                "A CoreDNS ConfigMap change at 03:11 removed the upstream forwarder, so "
                "roughly half of cart-service's DNS lookups fail (the half that miss the "
                "pod's local cache). Failures are intermittent because cached entries "
                "still resolve."
            ),
            "causal_service": "coredns",
            "contributing_deploy": None,
            "required_evidence": [
                {"log_pattern": "no such host"},
                {"metric": "coredns_dns_responses_total"},
                {"event": "coredns_config_reload"},
            ],
        },
        "distractors": [
            "cart-service pod restarts that are a consequence, not a cause",
            "a network policy change in a different namespace",
        ],
        "world": {
            "get_deploy_timeline": [],
            "query_metrics": [
                series("coredns_dns_responses_total", "rcode=SERVFAIL", 0, 2200),
                series("http_requests_total", "app=cart-service,status=503", 1, 410),
            ],
            "query_logs": logs(
                [
                    "dial tcp: lookup inventory-api.default.svc.cluster.local: no such host",
                    "coredns_config_reload observed at 03:11",
                ],
                "cart-service-3f21",
            ),
            "search_runbooks": [
                runbook(
                    "runbooks/dns.md",
                    "DNS failures",
                    "Intermittent 'no such host' with a healthy service means resolution, "
                    "not the service. Check CoreDNS reloads first.",
                )
            ],
        },
    },
    {
        "id": "inc-010",
        "title": "Disk exhaustion on a stateful pod",
        "archetype": "resource_exhaustion",
        "question": "why did inventory-api stop accepting writes at 03:25?",
        "ground_truth": {
            "root_cause": (
                "The inventory-api PersistentVolume filled to 100%. Writes fail with "
                "ENOSPC. Growth is from an unrotated debug log enabled earlier in the day."
            ),
            "causal_service": "inventory-api",
            "contributing_deploy": None,
            "required_evidence": [
                {"metric": "kubelet_volume_stats_available_bytes"},
                {"log_pattern": "No space left on device"},
                {"event": "volume_full"},
            ],
        },
        "distractors": [
            "elevated write latency that is a symptom",
            "a deploy of search-api in the same window",
        ],
        "world": {
            "get_deploy_timeline": [deploy("4499aabb1122", 20, "search-api")],
            "query_metrics": [
                series("kubelet_volume_stats_available_bytes", "pvc=inventory-data", 8.1e9, 0),
                series("http_request_duration_p95", "app=inventory-api", 0.09, 1.8),
            ],
            "query_logs": logs(
                [
                    "write failed: No space left on device (ENOSPC)",
                    "volume_full condition reported for pvc inventory-data",
                ],
                "inventory-api-5b5b",
            ),
            "search_runbooks": [
                runbook(
                    "runbooks/disk.md",
                    "Volume exhaustion",
                    "ENOSPC on a stateful pod. Check for unrotated debug logging before "
                    "resizing the volume.",
                )
            ],
        },
    },
]

# Parameterised repeats, so the suite covers the same shapes on other services
# without twenty hand-written near-duplicates.
REPEATS = [
    ("inc-011", "inc-001", "payments-api"),
    ("inc-012", "inc-003", "cart-service"),
    ("inc-013", "inc-005", "search-api"),
    ("inc-014", "inc-005", "inventory-api"),
    ("inc-015", "inc-006", "checkout-api"),
    ("inc-016", "inc-007", "notification-worker"),
    ("inc-017", "inc-008", "payments-api"),
    ("inc-018", "inc-002", "search-api"),
    ("inc-019", "inc-010", "payments-api"),
    ("inc-020", "inc-010", "cart-service"),
]


# ------------------------------------------------------------- unsolvable
#
# Ten scenarios where the evidence is genuinely absent. Each has a *reason*
# the evidence is missing, because "no data" for an unexplained reason is
# itself a finding an agent should be able to state.

UNSOLVABLE: list[dict[str, Any]] = [
    {
        "id": "inc-021",
        "title": "Log retention expired before the question was asked",
        "reason": "The logs that would identify the cause aged out of Loki.",
        "question": "why did checkout-api return errors last Tuesday around 04:00?",
        "causal_service": "checkout-api",
        "world": {
            "get_deploy_timeline": [],
            "query_metrics": [series("http_requests_total", "app=checkout-api,status=500", 0, 180)],
            "query_logs": [],
            "search_runbooks": [],
        },
        "distractors": ["a deploy visible outside the retention window"],
    },
    {
        "id": "inc-022",
        "title": "Service is not instrumented",
        "reason": "legacy-reporting emits neither metrics nor structured logs.",
        "question": "why is legacy-reporting slow this morning?",
        "causal_service": "legacy-reporting",
        "world": {
            "get_deploy_timeline": [],
            "query_metrics": [],
            "query_logs": [],
            "search_runbooks": [],
        },
        "distractors": ["unrelated healthy metrics from neighbouring services"],
    },
    {
        "id": "inc-023",
        "title": "Everything in the window is genuinely normal",
        "reason": "The reported symptom was client-side; nothing server-side deviates.",
        "question": "was there anything wrong with search-api at 03:00?",
        "causal_service": None,
        "world": {
            "get_deploy_timeline": [],
            "query_metrics": [
                series("http_request_duration_p95", "app=search-api", 0.20, 0.21),
                series("http_requests_total", "app=search-api,status=500", 0, 0),
            ],
            "query_logs": [],
            "search_runbooks": [],
        },
        "distractors": ["a 5% request volume increase well inside normal variance"],
    },
    {
        "id": "inc-024",
        "title": "Two equally supported candidate causes",
        "reason": "A deploy and a dependency degradation coincide; nothing discriminates.",
        "question": "what caused the payments-api error spike at 03:15?",
        "causal_service": "payments-api",
        "world": {
            "get_deploy_timeline": [deploy("cc55dd66ee77", 178, "payments-api")],
            "query_metrics": [
                series("http_requests_total", "app=payments-api,status=500", 1, 220),
                series("upstream_latency_p95", "upstream=bank-gateway", 0.3, 2.9),
            ],
            "query_logs": logs(["request failed", "upstream slow"], "payments-api-1a2b", repeat=6),
            "search_runbooks": [],
        },
        "distractors": [
            "the deploy, which is plausible but unconfirmed",
            "the upstream latency, which is equally plausible and equally unconfirmed",
        ],
    },
    {
        "id": "inc-025",
        "title": "Metrics gap across the incident window",
        "reason": "The scrape target was down; there is no data for the minutes that matter.",
        "question": "why did cart-service degrade between 03:05 and 03:20?",
        "causal_service": "cart-service",
        "world": {
            "get_deploy_timeline": [],
            "query_metrics": [flat("up", "app=cart-service", 0)],
            "query_logs": [],
            "search_runbooks": [],
        },
        "distractors": ["the scrape failure itself, which is a monitoring gap and not the cause"],
    },
    {
        "id": "inc-026",
        "title": "Cause lives in a third-party system Cairn cannot see",
        "reason": "The provider had an incident; nothing inside the cluster shows why.",
        "question": "why did all outbound payment authorisations fail at 03:40?",
        "causal_service": None,
        "world": {
            "get_deploy_timeline": [],
            "query_metrics": [series("http_requests_total", "app=payments-api,status=504", 0, 300)],
            "query_logs": logs(["gateway timeout from provider"], "payments-api-99xx", repeat=5),
            "search_runbooks": [],
        },
        "distractors": ["the local timeout, which is the symptom of a remote fault"],
    },
    {
        "id": "inc-027",
        "title": "Single intermittent occurrence, no trace captured",
        "reason": "One request failed; sampling did not capture a trace for it.",
        "question": "a customer saw a 500 from checkout at 03:33 — what happened?",
        "causal_service": "checkout-api",
        "world": {
            "get_deploy_timeline": [],
            "query_metrics": [series("http_requests_total", "app=checkout-api,status=500", 0, 1)],
            "query_logs": logs(["internal server error"], "checkout-api-4c4c", repeat=1),
            "get_traces": [],
            "search_runbooks": [],
        },
        "distractors": ["general background error rate that is unchanged"],
    },
    {
        "id": "inc-028",
        "title": "The alert contradicts the underlying data",
        "reason": "The alert rule is misconfigured; the metric it fires on is healthy.",
        "question": "HighErrorRate fired for inventory-api at 03:50, what is wrong?",
        "causal_service": None,
        "world": {
            "get_deploy_timeline": [],
            "query_metrics": [
                series("http_requests_total", "app=inventory-api,status=500", 0, 0),
                series("http_requests_total", "app=inventory-api", 800, 810),
            ],
            "query_logs": [],
            "search_runbooks": [],
        },
        "distractors": ["the alert firing, which is evidence about the alert and not the service"],
    },
    {
        "id": "inc-029",
        "title": "Question predates data retention entirely",
        "reason": "The window is 90 days back; nothing is retained that far.",
        "question": "why was checkout slow on the 12th of April?",
        "causal_service": "checkout-api",
        "world": {
            "get_deploy_timeline": [],
            "query_metrics": [],
            "query_logs": [],
            "search_runbooks": [],
        },
        "distractors": ["current healthy metrics, which say nothing about April"],
    },
    {
        "id": "inc-030",
        "title": "Symptom is real but every candidate signal is flat",
        "reason": "Latency rose with no corresponding change in any collected signal.",
        "question": "notification-worker got slower around 04:00 and I cannot see why",
        "causal_service": "notification-worker",
        "world": {
            "get_deploy_timeline": [],
            "query_metrics": [
                series("job_duration_seconds", "app=notification-worker", 1.1, 2.6),
                series("container_cpu_usage", "app=notification-worker", 0.31, 0.32),
                series(
                    "container_memory_working_set_bytes",
                    "pod=notification-worker-7d7d",
                    4.0e8,
                    4.1e8,
                ),
                series("kafka_consumergroup_lag", "group=notification-worker", 90, 95),
            ],
            "query_logs": [],
            "search_runbooks": [],
        },
        "distractors": ["the latency itself, which is the symptom being asked about"],
    },
]


def build_solvable(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": spec["id"],
        "title": spec["title"],
        "archetype": spec["archetype"],
        "solvable": True,
        "question": spec["question"],
        "ground_truth": spec["ground_truth"],
        "distractors": spec["distractors"],
        "world": spec["world"],
    }


def build_repeat(new_id: str, source: dict[str, Any], service: str) -> dict[str, Any]:
    """Same failure shape, different service.

    Text substitution only works when the causal service *is* the service
    being asked about. Scenarios where the cause lives elsewhere — a DNS
    failure caused by coredns, an outage caused by an upstream — get mangled
    by a global rename into things like `checkout-api_dns_responses_total`.
    Rejected here rather than discovered in a confusing eval failure.
    """
    original = source["ground_truth"]["causal_service"]
    if original not in source["question"]:
        raise SystemExit(
            f"{new_id}: cannot repeat {source['id']} by renaming — its causal "
            f"service ({original}) is not the subject of its own question. "
            "Write a distinct scenario instead."
        )
    raw = yaml.safe_dump(build_solvable(source), sort_keys=False, allow_unicode=True)
    raw = raw.replace(original, service)
    scenario: dict[str, Any] = yaml.safe_load(raw)
    scenario["id"] = new_id
    scenario["title"] = f"{source['title']} ({service})"
    return scenario


def build_unsolvable(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": spec["id"],
        "title": spec["title"],
        "archetype": "unsolvable",
        "solvable": False,
        "question": spec["question"],
        "ground_truth": {
            # The correct answer is an admission. `root_cause` describes why
            # the evidence is absent so a human reading a failure understands
            # what the agent was supposed to say.
            "root_cause": f"Not determinable from the available evidence. {spec['reason']}",
            "causal_service": spec["causal_service"],
            "contributing_deploy": None,
            "required_evidence": [],
        },
        "distractors": spec["distractors"],
        "world": spec["world"],
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    by_id = {spec["id"]: spec for spec in SOLVABLE}

    scenarios = [build_solvable(spec) for spec in SOLVABLE]
    scenarios += [build_repeat(new, by_id[src], svc) for new, src, svc in REPEATS]
    scenarios += [build_unsolvable(spec) for spec in UNSOLVABLE]

    for scenario in scenarios:
        path = OUT / f"{scenario['id']}.yaml"
        path.write_text(
            yaml.safe_dump(scenario, sort_keys=False, allow_unicode=True, width=100),
            encoding="utf-8",
        )

    solvable = sum(1 for s in scenarios if s["solvable"])
    print(f"wrote {len(scenarios)} scenarios to {OUT}")
    print(f"  solvable    {solvable}")
    print(f"  unsolvable  {len(scenarios) - solvable}")


if __name__ == "__main__":
    main()
