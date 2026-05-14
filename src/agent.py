"""SRE agent orchestration. Simulation-first: calls mock tools and builds RCA.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src import tools
from config import CONFIG

SYSTEM_PROMPT = """You are an expert SRE AI assistant connected to Grafana's observability stack via MCP tools.

Your job is to help engineers diagnose incidents, understand failures, and generate Root Cause Analysis (RCA) reports.
"""


def run_sre_agent(user_query: str, service: str = "payment-service", time_range: str = "last_2h") -> dict:
    # Follow investigation steps: alerts -> sift -> logs -> metrics -> correlate -> RCA
    result = {"query": user_query, "tools": [], "rca": {}}

    # 1. Check alerts
    alerts = tools.list_firing_alerts(service=service, state="all")
    result["tools"].append({"name": "list_firing_alerts", "result": alerts})

    # 2. Sift investigation
    sift = tools.run_sift_investigation(service=service, time_range=time_range)
    result["tools"].append({"name": "run_sift_investigation", "result": sift})

    # 3. Query logs (ERROR)
    logs = tools.query_loki_logs(service=service, time_range=time_range, level="ERROR")
    result["tools"].append({"name": "query_loki_logs", "result": logs})

    # 4. Query Prometheus metrics (db pool, latency, error rate)
    db_pool = tools.query_prometheus_metrics(metric_name="payment_service_db_pool_used", service=service, time_range=time_range)
    latency = tools.query_prometheus_metrics(metric_name="payment_service_request_latency_p99_ms", service=service, time_range=time_range)
    err_rate = tools.query_prometheus_metrics(metric_name="payment_service_error_rate_percent", service=service, time_range=time_range)
    result["tools"].extend([
        {"name": "query_prometheus_metrics_db_pool", "result": db_pool},
        {"name": "query_prometheus_metrics_latency", "result": latency},
        {"name": "query_prometheus_metrics_error_rate", "result": err_rate},
    ])

    # 5. Correlate signals and craft RCA using ONLY returned evidence
    rca = {
        "what_failed": "",
        "when": "",
        "root_cause": [],
        "evidence": [],
        "impact": "",
        "immediate_fix": [],
        "long_term_fix": []
    }

    # Pull facts from tools
    # Alerts
    firing_alerts = [a for a in alerts.get("alerts", []) if a.get("state") == "firing"]
    if firing_alerts:
        rca["evidence"].append({"alerts": firing_alerts})

    # Sift
    if sift.get("investigation"):
        rca["evidence"].append({"sift": sift.get("investigation")})
        spike = sift.get("investigation", {}).get("error_spike")
        if spike:
            rca["root_cause"].append("Correlated spike with batch-job-runner (high confidence)")

    # Logs
    log_lines = logs.get("log_lines", [])
    for l in log_lines:
        if "DB connection pool exhausted" in l:
            rca["what_failed"] = "payment-service (DB connection pool exhaustion)"
            rca["when"] = l.split()[0]
            rca["evidence"].append({"log_line": l})
        if "Circuit breaker OPEN" in l:
            rca["evidence"].append({"log_line": l})

    # Metrics
    def latest_value(series):
        if not series:
            return None
        if isinstance(series, dict) and series.get("data_points"):
            return series["data_points"][-1]
        if isinstance(series, dict) and series.get("data"):
            return series["data"]
        return None

    db_latest = latest_value(db_pool)
    lat_latest = latest_value(latency)
    err_latest = latest_value(err_rate)
    if db_latest:
        rca["evidence"].append({"db_pool": db_latest})
    if lat_latest:
        rca["evidence"].append({"p99_latency": lat_latest})
    if err_latest:
        rca["evidence"].append({"error_rate": err_latest})

    # Impact & fixes (derived from synthetic investigation if present)
    sift_info = sift.get("investigation")
    if sift_info:
        rca["impact"] = f"{sift_info.get('slow_requests', {}).get('total_affected_requests', 'unknown')} requests affected"
        rca["immediate_fix"].append("Increase DB connection pool or limit batch-job-runner connections")
        rca["long_term_fix"].append("Schedule heavy batch jobs outside peak hours; add PgBouncer")

    result["rca"] = rca
    return result
