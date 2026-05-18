"""Mock MCP tools - return synthetic data or query Prometheus when configured.
"""
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src import synthetic_data
from config import CONFIG
import requests

def get_data_for(service):
    return synthetic_data.SYNTHETIC_DATA.get(service, synthetic_data.SYNTHETIC_DATA["payment-service"])

def query_loki_logs(service: str, time_range: str, level: str = "ERROR"):
    data = get_data_for(service)
    logs = data["logs"].strip().split("\n")
    if level != "ALL":
        logs = [l for l in logs if f"[{level}]" in l or level == "ERROR"]
    return {
        "status": "success",
        "service": service,
        "time_range": time_range,
        "log_lines": logs,
        "total_lines": len(logs)
    }

def query_prometheus_metrics(metric_name: str, service: str, time_range: str = "last_1h"):
    # Query real Prometheus via Grafana datasource proxy
    prom_url = CONFIG.get("PROMETHEUS_URL")
    if prom_url:
        try:
            r = requests.get(f"{prom_url}/query", params={"query": metric_name}, timeout=5)
            r.raise_for_status()
            return {"status": "success", "metric": metric_name, "service": service, "data": r.json()}
        except Exception as e:
            return {"status": "error", "message": str(e), "detail": f"Failed to query Prometheus at {prom_url}"}

    return {"status": "error", "message": "PROMETHEUS_URL not configured"}

def list_firing_alerts(service: str = "", state: str = "all"):
    data = get_data_for(service or "payment-service")
    alerts = data["alerts"]
    if service:
        alerts = [a for a in alerts if service in a.get("labels", {}).get("service", "")]
    if state and state != "all":
        alerts = [a for a in alerts if a.get("state") == state]
    return {"status": "success", "alerts": alerts, "total": len(alerts)}

def run_sift_investigation(service: str, time_range: str):
    data = get_data_for(service)
    return {"status": "success", "investigation": data["sift"]}

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "query_loki_logs",
            "description": "Query Loki log aggregation system. Returns recent log lines for a service.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "Service name to query logs for"},
                    "time_range": {"type": "string", "description": "Time range e.g. last_30m, last_1h"},
                    "level": {"type": "string", "enum": ["ERROR", "WARN", "INFO", "ALL"]}
                },
                "required": ["service", "time_range"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_prometheus_metrics",
            "description": "Execute a PromQL query against Prometheus to get metric time series data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "metric_name": {"type": "string", "description": "Metric name to query"},
                    "service": {"type": "string", "description": "Service to filter by"},
                    "time_range": {"type": "string", "description": "Time range for the query"}
                },
                "required": ["metric_name", "service"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_firing_alerts",
            "description": "List all currently firing and recently resolved alerts from Grafana Alerting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "Optional: filter alerts by service name"},
                    "state": {"type": "string", "enum": ["firing", "resolved", "all"]}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_sift_investigation",
            "description": "Run Grafana Sift automated investigation to detect error patterns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "Service to investigate"},
                    "time_range": {"type": "string", "description": "Investigation time window"}
                },
                "required": ["service", "time_range"]
            }
        }
    }
]
