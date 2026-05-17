"""Multi-round LLM-driven agent with iterative tool orchestration via MCP.

Implements proper conversation loop where tool results feed back to LLM for continued investigation.
"""
import sys
import os
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.mcp_client import mcp_client
from src.llm_clients import OpenRouterClient
from config import CONFIG
import logging
from datetime import datetime, timedelta

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    filename='agent_audit.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("agent_audit")

MAX_TOOL_ROUNDS = 4


def get_prometheus_datasource_uid(cluster_or_env: str) -> str:
    """Map cluster/environment to correct Prometheus datasource UID"""
    cluster = (cluster_or_env or "").lower()
    if "prod" in cluster:
        return "mimir-prod"
    if "staging" in cluster:
        return "mimir-staging"
    return "mimir"


def extract_cluster(query: str) -> str:
    """Extract cluster name from query"""
    q = query.lower()
    if "oci-enterprise-security-dev" in q:
        return "oci-enterprise-security-dev"
    if "prod" in q:
        return "prod"
    if "staging" in q:
        return "staging"
    return "dev"


def needs_prometheus_query(user_query: str) -> bool:
    """Check if query requires Prometheus metric investigation"""
    q = user_query.lower()
    keywords = ["cpu", "memory", "restart", "latency", "error rate", "pod", "high", "usage", "peak", "top"]
    return any(kw in q for kw in keywords)


SYSTEM_PROMPT = """You are an expert SRE incident analysis assistant for Kubernetes clusters using Grafana.

You have access to these MCP tools via tool calls:
- list_datasources: Get Grafana datasource UIDs
- query_prometheus: Execute PromQL queries (requires datasourceUid)
- list_prometheus_label_values: Discover label values (namespace, pod, app, service, etc)
- list_prometheus_label_names: List available metric dimensions
- search_dashboards: Search Grafana dashboards

CRITICAL RULES:

1. CLUSTER vs NAMESPACE:
   - oci-enterprise-security-dev is a CLUSTER NAME, not a namespace
   - prod-cluster is a CLUSTER NAME, not a namespace
   - Namespaces are inside clusters: payment, production, kube-system, etc

2. DATASOURCE MAPPING:
   - oci-enterprise-security-dev cluster → datasourceUid="mimir"
   - prod-cluster → datasourceUid="mimir-prod"
   - staging → datasourceUid="mimir-staging"

3. CPU QUERIES MUST:
   - Always use container_cpu_usage_seconds_total metric (NOT 'up')
   - Use PromQL: topk(20, sum(rate(container_cpu_usage_seconds_total{container!="", image!=""}[5m])) by (namespace, pod))
   - Call query_prometheus after list_datasources
   - Include timestamps in ISO8601 format

4. INVESTIGATION FLOW:
   - For CPU/memory/restart/latency/error rate: MUST call query_prometheus
   - CANNOT complete RCA with only datasource discovery
   - Must have actual metric evidence before final RCA
   - Continue calling tools until metric data is collected

When you have sufficient metric/log evidence, return final RCA as JSON:
{
  "rca": {
    "what_failed": "...",
    "how_it_failed": "...",
    "root_cause": [...],
    "evidence": [...],
    "impact": "...",
    "immediate_fix": [...],
    "long_term_fix": [...]
  }
}

Return ONLY the JSON RCA when you have completed investigation, no other text."""

ANALYSIS_PROMPT = """You are an expert SRE. Analyze these investigation results and return final RCA.

oci-enterprise-security-dev is a CLUSTER NAME, not a namespace.
Never put cluster names into namespace filters.

Return ONLY JSON RCA with: what_failed, how_it_failed, root_cause (list), evidence (list), impact, immediate_fix (list), long_term_fix (list)."""


def run_llm_orchestrated_query(user_query: str, service: str = "payment-service", model: str = None):
    """Multi-round LLM-driven tool orchestration with iterative investigation"""
    logger.info(f"=== Investigation START: {service} ===")
    logger.info(f"Query: {user_query}")
    print(f"\n[Agent] Query: {user_query}")
    print(f"[Agent] Service: {service}")
    
    # Detect cluster and datasource
    cluster = extract_cluster(user_query)
    datasource_uid = get_prometheus_datasource_uid(cluster)
    print(f"[Agent] Cluster: {cluster} → Datasource: {datasource_uid}")
    
    client = OpenRouterClient()
    model = model or CONFIG.get("OPENROUTER_MODEL")
    
    # Initialize conversation
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Investigate: {service}\nCluster: {cluster}\nQuery: {user_query}"}
    ]
    
    all_tool_results = []
    
    # Multi-round loop
    for round_idx in range(MAX_TOOL_ROUNDS):
        print(f"\n[Agent] ═══ ROUND {round_idx + 1}/{MAX_TOOL_ROUNDS} ═══")
        
        # Ask LLM what to do
        print(f"[Agent] Calling LLM...")
        resp = client.create_chat_completion(model=model, messages=messages)
        llm_response = (resp.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        
        print(f"[Agent] LLM Response: {llm_response[:300]}...")
        
        # Check if LLM returned final RCA
        if '{"rca"' in llm_response or '"what_failed"' in llm_response:
            print(f"[Agent] LLM produced final RCA")
            return _parse_final_rca(llm_response, all_tool_results, user_query, service)
        
        messages.append({"role": "assistant", "content": llm_response})
        
        # Parse tool calls
        tool_calls = _parse_tool_calls(llm_response, datasource_uid)
        
        if not tool_calls:
            print(f"[Agent] No tool calls found in LLM response")
            return _parse_final_rca(llm_response, all_tool_results, user_query, service)
        
        print(f"[Agent] Tools to call: {[tc.get('name') for tc in tool_calls]}")
        
        # Execute tools
        round_results = []
        for tool_call in tool_calls:
            tool_name = tool_call.get("name")
            tool_args = tool_call.get("args", {})
            
            print(f"[Agent] → {tool_name}...")
            
            # Skip certain tools
            if tool_name == "list_prometheus_metric_names":
                round_results.append({
                    "name": tool_name,
                    "status": "skipped",
                    "error": "Disabled tool"
                })
                continue
            
            try:
                result = mcp_client.execute_tool(tool_name, tool_args)
                status = result.get("status", "error")
                print(f"[Agent]   ✓ {status}")
                
                round_results.append({
                    "name": tool_name,
                    "args": tool_args,
                    "status": status,
                    "data": result.get("mcp_data", []) if status == "success" else None,
                    "error": result.get("error") if status != "success" else None
                })
            except Exception as e:
                print(f"[Agent]   ✗ Error: {str(e)}")
                round_results.append({
                    "name": tool_name,
                    "args": tool_args,
                    "status": "error",
                    "error": str(e)
                })
        
        all_tool_results.extend(round_results)
        
        # Check if we need Prometheus but didn't query
        needs_prom = needs_prometheus_query(user_query)
        has_prom_result = any(r.get("name") == "query_prometheus" for r in all_tool_results)
        
        enforce_query = ""
        if needs_prom and not has_prom_result:
            enforce_query = (
                f"\n\nIMPORTANT: User asked for metrics but query_prometheus was not called. "
                f"MUST call query_prometheus now with datasourceUid='{datasource_uid}' and container_cpu_usage_seconds_total. "
                f"Do NOT generate final RCA without actual metric data."
            )
        
        # Add results back to conversation
        tool_text = json.dumps(round_results, indent=2, default=str)
        messages.append({
            "role": "user",
            "content": (
                "Tool results:\n"
                + tool_text
                + "\n\nContinue investigation. "
                + "If you have metric evidence, return final RCA JSON. "
                + "Otherwise, call more tools."
                + enforce_query
            )
        })
        
        print(f"[Agent] Round {round_idx + 1} complete")
    
    # Max rounds reached
    print(f"[Agent] Max rounds reached, generating final RCA...")
    resp = client.create_chat_completion(model=model, messages=messages + [
        {"role": "user", "content": "All rounds complete. Return final RCA JSON now with all evidence."}
    ])
    final_response = (resp.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    
    return _parse_final_rca(final_response, all_tool_results, user_query, service)


def _parse_tool_calls(response: str, datasource_uid: str):
    """Extract tool calls from LLM response"""
    tool_calls = []
    
    # Look for tool call patterns
    if "list_datasources" in response:
        tool_calls.append({"name": "list_datasources", "args": {}})
    
    if "query_prometheus" in response:
        # Find the PromQL expression
        expr = 'topk(20, sum(rate(container_cpu_usage_seconds_total{container!="", image!=""}[5m])) by (namespace, pod))'
        now = datetime.utcnow()
        start = now - timedelta(hours=1)
        
        tool_calls.append({
            "name": "query_prometheus",
            "args": {
                "datasourceUid": datasource_uid,
                "expr": expr,
                "queryType": "instant",
                "startTime": start.isoformat() + "Z",
                "endTime": now.isoformat() + "Z"
            }
        })
    
    if "list_prometheus_label_values" in response or "label" in response.lower():
        tool_calls.append({
            "name": "list_prometheus_label_values",
            "args": {"datasourceUid": datasource_uid, "labelName": "namespace"}
        })
    
    return tool_calls


def _parse_final_rca(response: str, all_tool_results: list, user_query: str, service: str):
    """Parse final RCA from LLM response"""
    # Try to extract JSON
    try:
        # Find JSON block
        start = response.find('{')
        if start >= 0:
            # Find matching closing brace
            brace_count = 0
            end = start
            for i in range(start, len(response)):
                if response[i] == '{':
                    brace_count += 1
                elif response[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        end = i + 1
                        break
            
            json_str = response[start:end]
            data = json.loads(json_str)
            
            final_rca = data if "rca" in data else {"rca": data}
            final_rca["tools"] = all_tool_results
            
            logger.info(f"=== Investigation COMPLETE ===")
            return final_rca
    except (json.JSONDecodeError, ValueError):
        pass
    
    # Fallback
    return _build_fallback_rca(service, all_tool_results, response)


def _build_fallback_rca(service, all_tool_results, raw_response):
    """Fallback RCA when JSON parsing fails"""
    return {
        "rca": {
            "what_failed": service,
            "how_it_failed": "Investigation completed",
            "root_cause": ["See evidence below"],
            "evidence": [{"type": "tool_results", "source": "mcp_tools", "detail": raw_response[:300]}],
            "impact": f"Analyzed using {len(all_tool_results)} tools",
            "immediate_fix": ["Review investigation results"],
            "long_term_fix": ["Set up better monitoring"]
        },
        "tools": all_tool_results,
        "raw_response": raw_response
    }
