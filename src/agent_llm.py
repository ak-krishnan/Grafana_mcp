"""LLM-driven agent loop that supports tool-calling via MCP to a live Grafana instance.

Flow:
 1. Send system+user to LLM -> LLM returns tool call plan
 2. Execute each requested tool via MCP
 3. Send tool outputs back to LLM -> LLM returns final answer
"""
import sys
import os
import json
import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.mcp_client import mcp_client
from src.llm_clients import OpenRouterClient
from config import CONFIG
import logging

# Configure audit logger
logging.basicConfig(
    filename='agent_audit.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("agent_audit")

# Current time for the LLM to use
NOW = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

SYSTEM_PROMPT = f"""You are an SRE assistant connected to a live Grafana observability stack via MCP tools.
Current UTC time: {NOW}

=== GRAFANA INSTANCE INFO ===
URL: https://grafana.secureai-meridian.in

DATASOURCES (use these exact UIDs):
- Prometheus: uid="prometheus"
- Mimir-dev (default): uid="mimir"
- Mimir-prod: uid="mimir-prod"
- Mimir-staging: uid="mimir-staging"
- Loki-dev: uid="loki"
- Loki-prod: uid="loki-prod"
- Loki-staging: uid="loki-staging"
- Tempo-dev: uid="tempo-dev"
- Tempo-prod: uid="tempo-prod"

AVAILABLE DASHBOARDS:
- Kubernetes / Views / Pods: uid="k8s_views_pods"
- Kubernetes / Views / Nodes: uid="k8s_views_nodes"
- Kubernetes Cluster: uid="os6Bh8Omk"
- K8S Pod Metrics: uid="r1m-inuIk"
- K8S Pod Metrics Enhanced: uid="k8s-pod-enhanced"
- Container Resources: uid="px1WKJznk"
- Node Exporter Full: uid="rYdddlPWk"
- APISIX API Monitoring: uid="cflbv4hghxj40f"
- CoreDNS: uid="bfknb88h5b0g0c"
- OpenCost: uid="opencost-mixin-kover-jkwq"
- Enterprise Security App: uid="es-app-overview"
- Enterprise Security Cluster: uid="es-cluster-health"
- Velero Backup: uid="velero-backup-dashboard"

LOKI LABELS: app, component, container, instance, job, namespace, node_name, pod, service_name

=== INVESTIGATION WORKFLOW ===
1. FIRST: Use search_dashboards or list_datasources to orient yourself
2. THEN: Use query_prometheus with datasourceUid="mimir" (default) for metrics
3. Use query_loki_logs with datasourceUid="loki" for logs
4. Use list_alert_rules to check alerts
5. Use get_dashboard_by_uid to inspect specific dashboard panels
6. Always use recent timestamps (around {NOW}), NOT old dates

=== IMPORTANT RULES ===
- ALWAYS use datasourceUid="mimir" for Prometheus queries (NOT "prom1" or empty string)
- ALWAYS use datasourceUid="loki" for Loki queries  
- Use real timestamps near {NOW}, NOT dates from 2023
- For LogQL: use simple queries like {{namespace="default"}} |= "error"
- For PromQL: use real metrics like kube_pod_status_phase, container_cpu_usage_seconds_total, etc.

When you have gathered enough evidence, provide your final answer as JSON:
{{
  "rca": {{
    "what_failed": "Clear description of what service/component failed",
    "how_it_failed": "Technical explanation of the failure mechanism",
    "root_cause": ["Root cause 1", "Root cause 2"],
    "evidence": [{{"type": "metric/log/alert/dashboard", "source": "tool_name", "detail": "specific finding"}}],
    "impact": "Business/user impact description",
    "immediate_fix": ["Step 1", "Step 2"],
    "long_term_fix": ["Improvement 1", "Improvement 2"]
  }}
}}
Do not include markdown backticks. Just raw JSON.
"""

def _extract_tool_calls(msg_obj):
    """Extract tool calls from various LLM response formats."""
    tool_calls = msg_obj.get("tool_calls", [])
    if tool_calls:
        normalized = []
        for tc in tool_calls:
            if isinstance(tc, dict):
                func = tc.get("function", {})
                call_id = tc.get("id", f"call_{len(normalized)}")
                name = func.get("name", "")
                args = func.get("arguments", "{}")
                if isinstance(args, dict):
                    args = json.dumps(args)
                normalized.append({
                    "id": call_id,
                    "function": {"name": name, "arguments": args}
                })
            else:
                normalized.append(tc)
        return normalized
    return []


def run_llm_orchestrated_query(user_query: str, service: str = "payment-service", model: str = None):
    logger.info(f"--- NEW INVESTIGATION STARTED: {service} ---")
    logger.info(f"Query: {user_query}")
    
    client = OpenRouterClient()
    model = model or CONFIG.get("OPENROUTER_MODEL")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Service: {service}\nQuery: {user_query}\n\nPlease investigate using the available Grafana tools. Use datasourceUid='mimir' for Prometheus and datasourceUid='loki' for Loki."}
    ]
    
    max_iterations = 6
    executed_tools = []
    
    for iteration in range(max_iterations):
        print(f"\n[Agent] === Iteration {iteration + 1}/{max_iterations} ===")
        
        schemas = mcp_client.get_tool_schema()
        resp = client.create_chat_completion(model=model, messages=messages, tools=schemas)
        
        choices = resp.get("choices", [])
        if not choices:
            print("[Agent] WARNING: Empty choices from LLM")
            break
            
        msg_obj = choices[0].get("message", {})
        tool_calls = _extract_tool_calls(msg_obj)
        
        if not tool_calls:
            content = (msg_obj.get("content") or "{}").strip()
            print(f"[Agent] Final answer received ({len(content)} chars)")
            
            # Strip markdown code fences
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
            
            try:
                final_json = json.loads(content)
            except json.JSONDecodeError:
                # Try to find JSON embedded in the text
                import re
                json_match = re.search(r'\{[\s\S]*"rca"[\s\S]*\}', content)
                if json_match:
                    try:
                        final_json = json.loads(json_match.group())
                    except json.JSONDecodeError:
                        final_json = None
                else:
                    final_json = None
                
                if not final_json:
                    # Build RCA from free-text analysis
                    lines = content.split('\n')
                    # Try to extract fix suggestions from the text
                    fixes_now = []
                    fixes_long = []
                    how_failed = []
                    for line in lines:
                        l = line.strip().lstrip('- •*1234567890.)')
                        if not l:
                            continue
                        lower = l.lower()
                        if any(kw in lower for kw in ['fix', 'increase', 'restart', 'scale', 'add', 'configure', 'update', 'reduce', 'limit']):
                            if any(kw in lower for kw in ['long', 'future', 'prevent', 'permanent']):
                                fixes_long.append(l)
                            else:
                                fixes_now.append(l)
                        elif any(kw in lower for kw in ['failed', 'error', 'crash', 'timeout', 'exhaust', 'spike', 'down', 'unavailable', 'oom']):
                            how_failed.append(l)

                    final_json = {
                        "rca": {
                            "what_failed": service,
                            "how_it_failed": '\n'.join(how_failed[:5]) if how_failed else content[:300],
                            "root_cause": [content[:500] if content else "LLM did not return structured output"],
                            "evidence": [{"type": "llm_analysis", "source": "qwen2.5:14b", "detail": "See full analysis below"}],
                            "impact": "See analysis below",
                            "immediate_fix": fixes_now[:5] if fixes_now else ["Review the full analysis below for recommendations"],
                            "long_term_fix": fixes_long[:5] if fixes_long else []
                        },
                        "raw_response": content
                    }
            
            final_json["tools"] = executed_tools
            logger.info(f"Completed Investigation. Final RCA: {json.dumps(final_json)[:2000]}")
            return final_json
        
        # Build assistant message
        assistant_msg = {"role": "assistant", "content": msg_obj.get("content") or ""}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)
        
        for tc in tool_calls:
            fname = tc.get("function", {}).get("name", "")
            raw_args = tc.get("function", {}).get("arguments", "{}")
            call_id = tc.get("id", f"call_{len(executed_tools)}")
            
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            
            logger.info(f"Executing Tool: {fname} with args: {json.dumps(args)}")
            print(f"[Agent] Calling tool: {fname}({json.dumps(args)[:150]})")
            
            try:
                res = mcp_client.execute_tool(fname, args)
            except Exception as e:
                res = {"status": "error", "error": str(e)}
                print(f"[Agent] Tool error: {e}")
            
            logger.info(f"Tool Result ({fname}): {json.dumps(res)[:1000]}")
            
            executed_tools.append({
                "name": fname,
                "args": args,
                "result": res
            })
            
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(res)[:4000]
            })
            
    final_json = {"rca": {"what_failed": f"Investigation for {service} completed (max iterations)."}}
    final_json["tools"] = executed_tools
    return final_json
