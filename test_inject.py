"""Quick test of _inject_intent_into_dashboard with a simulated CPU alert scenario."""
import json
from datetime import datetime, timezone

NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# Copy the function directly to test without heavy imports
exec_globals = {"NOW": NOW}
with open("src/agent_llm.py") as f:
    source = f.read()

# Extract just the function
start = source.index("def _inject_intent_into_dashboard(")
end = source.index("\n\n\n# ─── Main Orchestration", start)
func_code = source[start:end]
exec(compile(func_code, "agent_llm.py", "exec"), exec_globals)
inject_fn = exec_globals["_inject_intent_into_dashboard"]

# Simulate: CPU throttling alert, ALL tools failed
result = {
    "dashboard": {
        "critical_pods": 0, "total_restarts": 0,
        "namespaces_affected": [], "pods": [],
        "technical_findings": "", "theoretical_analysis": "",
        "analysis": {}, "conclusion": "",
        "follow_up_questions": [], "recommended_actions": []
    },
    "tools": [
        {"name": "query_prometheus", "args": {}, "result": {"status": "error", "error": "no such host"}},
        {"name": "list_alert_rules", "args": {}, "result": {"status": "success", "mcp_data": ["rules"]}},
    ]
}

intent = {
    "namespaces": ["kube-system"],
    "pod_name": "oke-node-problem-detector-j6zzr",
    "alertname": "Pod CPU Throttling High",
    "alert_error": "dial tcp: lookup prometheus-server.monitoring.svc.cluster.local: no such host",
    "summary": "Pod kube-system/oke-node-problem-detector-j6zzr CPU throttling",
    "investigation_type": "cpu",
    "datasource_uid": "mimir",
    "datasource_broken": True,
}

inject_fn(result, intent)
d = result["dashboard"]

sections = [
    ("NAMESPACES", d["namespaces_affected"]),
    ("PODS", [(p["name"], p["severity"], p["category"]) for p in d["pods"]]),
    ("TECHNICAL FINDINGS", d["technical_findings"][:200]),
    ("THEORETICAL ANALYSIS", d["theoretical_analysis"][:200]),
    ("ANALYSIS", {k: v[:80] for k, v in d.get("analysis", {}).items()}),
    ("CONCLUSION", d["conclusion"][:200]),
    ("RECOMMENDED ACTIONS", d["recommended_actions"]),
    ("FOLLOW-UP QUESTIONS", d["follow_up_questions"]),
]

all_ok = True
for name, value in sections:
    filled = bool(value) and value not in ([], {}, "", [""], None)
    status = "✅" if filled else "❌ EMPTY"
    if not filled:
        all_ok = False
    print(f"{status} {name}: {json.dumps(value, default=str)[:120]}")

print(f"\n{'✅ ALL SECTIONS POPULATED' if all_ok else '❌ SOME SECTIONS EMPTY'}")
