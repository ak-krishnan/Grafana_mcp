"""Synthetic Grafana data for local testing.
Includes multiple scenarios to test LLM reasoning.
"""
SYNTHETIC_DATA = {
    "payment-service": {
        "logs": """2026-05-13T01:58:03Z [ERROR] payment-service: DB connection pool exhausted (pool_size=10, waiting=47)
2026-05-13T01:58:05Z [ERROR] payment-service: Timeout waiting for DB connection after 30000ms
2026-05-13T01:58:11Z [ERROR] payment-service: Circuit breaker OPEN for postgres-prod-01
2026-05-13T01:58:14Z [INFO]  batch-job-runner: Opening 8 concurrent DB connections
2026-05-13T02:05:00Z [INFO]  payment-service: Circuit breaker CLOSED - service recovered""",
        "metrics": {
            "payment_service_db_pool_used": [{"timestamp": "01:58", "value": 10}, {"timestamp": "02:05", "value": 4}],
            "payment_service_error_rate_percent": [{"timestamp": "01:55", "value": 0.2}, {"timestamp": "01:58", "value": 91.2}]
        },
        "alerts": [
            {"name": "PaymentServiceHighErrorRate", "state": "firing", "labels": {"service": "payment-service"}, "annotations": {"summary": "Error rate >80%"}},
            {"name": "DBConnectionPoolExhausted", "state": "firing", "labels": {"service": "payment-service", "db": "postgres-prod-01"}, "annotations": {"summary": "Pool full"}}
        ],
        "sift": {"status": "complete", "anomaly_detected": True, "error_spike": {"peak_error_rate": "91.2%", "correlated_service": "batch-job-runner", "correlation_confidence": 0.94}}
    },
    "auth-service": {
        "logs": """2026-05-13T08:12:01Z [WARN] auth-service: Redis latency spike (150ms)
2026-05-13T08:12:15Z [ERROR] auth-service: Redis connection timeout (redis-cache-01)
2026-05-13T08:12:20Z [ERROR] auth-service: Failed to validate session token - Cache unreachable
2026-05-13T08:12:25Z [INFO] auth-service: Falling back to DB for session validation
2026-05-13T08:13:00Z [WARN] auth-service: DB CPU at 95% due to cache stampede""",
        "metrics": {
            "auth_service_redis_latency_ms": [{"timestamp": "08:10", "value": 2}, {"timestamp": "08:12", "value": 1500}],
            "auth_service_db_cpu_percent": [{"timestamp": "08:10", "value": 15}, {"timestamp": "08:13", "value": 95}],
            "auth_service_error_rate": [{"timestamp": "08:10", "value": 0.01}, {"timestamp": "08:12", "value": 12.5}]
        },
        "alerts": [
            {"name": "RedisCacheUnavailable", "state": "firing", "labels": {"service": "auth-service", "component": "redis"}, "annotations": {"summary": "Redis cache nodes unreachable"}},
            {"name": "HighDBCpuUsage", "state": "firing", "labels": {"service": "auth-service"}, "annotations": {"summary": "DB CPU > 90%"}}
        ],
        "sift": {"status": "complete", "anomaly_detected": True, "error_spike": {"peak_error_rate": "12.5%", "correlated_component": "redis-cache", "correlation_confidence": 0.98}, "slow_requests": {"p99_peak_ms": 4500, "total_affected_requests": 5200}}
    }
}

