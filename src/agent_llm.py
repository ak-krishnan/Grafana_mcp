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

# Current time for the LLM to use
NOW = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

SYSTEM_PROMPT = f"""You are an SRE assistant with Grafana MCP tools. Current time: {NOW}

DATASOURCES — always use these exact UIDs:
- Prometheus/Mimir: datasourceUid="mimir"
- Loki logs: datasourceUid="loki"

IMPORTANT: Always use query_prometheus for metrics. Do NOT use list_prometheus_metric_names.

EXAMPLE TOOL CALLS THAT WORK:
1. Pod restarts:
   query_prometheus(expr="kube_pod_container_status_restarts_total{{namespace=\"monitoring\"}}", queryType="instant", datasourceUid="mimir", startTime="{NOW}")
2. CPU usage:
   query_prometheus(expr="rate(container_cpu_usage_seconds_total{{namespace=\"monitoring\"}}[5m])", queryType="instant", datasourceUid="mimir", startTime="{NOW}")
3. Memory usage:
   query_prometheus(expr="container_memory_working_set_bytes{{namespace=\"monitoring\"}}", queryType="instant", datasourceUid="mimir", startTime="{NOW}")
4. Pod status:
   query_prometheus(expr="kube_pod_status_phase{{namespace=\"monitoring\"}}", queryType="instant", datasourceUid="mimir", startTime="{NOW}")
5. Error logs:
   query_loki_logs(logql="{{namespace=\"monitoring\"}} |= \"error\"", datasourceUid="loki", startRfc3339="{NOW[:11]}00:00:00Z", endRfc3339="{NOW}")
6. All logs:
   query_loki_logs(logql="{{namespace=\"monitoring\"}}", datasourceUid="loki", startRfc3339="{NOW[:11]}00:00:00Z", endRfc3339="{NOW}")

Replace \"monitoring\" with the user's requested namespace.
Use timestamps near {NOW}.

After gathering data, return JSON (no markdown):
{{"rca": {{"what_failed": "...", "how_it_failed": "...", "root_cause": ["..."], "evidence": [{{"type": "metric/log", "source": "tool_name", "detail": "specific finding"}}], "impact": "...", "immediate_fix": ["step1", "step2"], "long_term_fix": ["improvement1"]}}}}
"""

# Only send the most useful tools to the LLM to reduce prompt size
# (43 tools overwhelms a local 14B model and causes timeouts)
PRIORITY_TOOLS = {
    "query_prometheus",
    "query_loki_logs",
    "list_alert_rules",
    "search_dashboards",
    "get_dashboard_by_uid",
}

def _filter_tools(schemas):
    """Keep only priority tools to fit within local LLM context."""
    filtered = [s for s in schemas if s.get("function", {}).get("name") in PRIORITY_TOOLS]
    if not filtered:
        return schemas[:10]  # fallback: just take first 10
    return filtered

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
    """Multi-round LLM-driven tool orchestration with iterative investigation"""
    logger.info(f"=== Investigation START: {service} ===")
    logger.info(f"Query: {user_query}")
    print(f"\n[Agent] Query: {user_query}")
    print(f"[Agent] Service: {service}")
    
    client = OpenRouterClient()
    model = model or CONFIG.get("OPENROUTER_MODEL")
    
    # Initialize conversation
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Investigate: {service}\nCluster: {cluster}\nQuery: {user_query}"}
    ]
    
    max_iterations = 4
    executed_tools = []
    
    # Get and filter tools once (not per-iteration)
    all_schemas = mcp_client.get_tool_schema()
    schemas = _filter_tools(all_schemas)
    tool_names = [s["function"]["name"] for s in schemas]
    print(f"[Agent] Using {len(schemas)} tools: {', '.join(tool_names)}")
    
    for iteration in range(max_iterations):
        print(f"\n[Agent] === Iteration {iteration + 1}/{max_iterations} ===")
        
        resp = client.create_chat_completion(model=model, messages=messages, tools=schemas)
        
        choices = resp.get("choices", [])
        if not choices:
            print("[Agent] WARNING: Empty choices from LLM")
            break
            
        msg_obj = choices[0].get("message", {})
        tool_calls = _extract_tool_calls(msg_obj)
        
        if not tool_calls:
            print(f"[Agent] No tool calls found in LLM response")
            # Return partial RCA from what we've gathered
            return _build_rca_from_evidence(service, executed_tools, "")
        
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
                
                executed_tools.append({
                    "name": tool_name,
                    "args": tool_args,
                    "result": result
                })
                
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.get("id", f"call_{len(executed_tools)}"),
                    "content": json.dumps(result)[:1500]
                })
            except Exception as e:
                res = {"status": "error", "error": str(e)}
                print(f"[Agent] Tool error: {e}")
                
                executed_tools.append({
                    "name": tool_name,
                    "args": tool_args,
                    "result": res
                })
                
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.get("id", f"call_{len(executed_tools)}"),
                    "content": json.dumps(res)[:1500]
                })
    
    # === FORCE FINAL ANSWER ===
    # LLM kept calling tools and never produced an RCA. 
    # Make one last call WITHOUT tools to force a summary.
    print("[Agent] Max iterations reached. Forcing final summary (no tools)...")
    
    # Build a summary of what we found
    evidence_summary = []
    for t in executed_tools:
        status = t["result"].get("status", "unknown")
        data = t["result"].get("mcp_data", [])
        data_preview = str(data[0])[:200] if data else "no data"
        evidence_summary.append(f"- {t['name']}: {status} → {data_preview}")
    
    summary_prompt = f"""Based on the investigation of {service}, here is what the tools found:

{chr(10).join(evidence_summary[:10])}

Now provide your final Root Cause Analysis as JSON (no markdown):
{{"rca": {{"what_failed": "...", "how_it_failed": "...", "root_cause": ["..."], "evidence": [{{"type": "...", "detail": "..."}}], "impact": "...", "immediate_fix": ["..."], "long_term_fix": ["..."]}}}}"""

    messages_final = [
        {"role": "system", "content": "You are an SRE assistant. Analyze the evidence and return a JSON RCA. No markdown."},
        {"role": "user", "content": summary_prompt}
    ]
    
    try:
        resp = client.create_chat_completion(model=model, messages=messages_final, tools=None)
        content = (resp.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            final_json = json.loads(content)
        except json.JSONDecodeError:
            final_json = _build_rca_from_evidence(service, executed_tools, content)
    except Exception as e:
        print(f"[Agent] Final summary call failed: {e}")
        final_json = _build_rca_from_evidence(service, executed_tools, "")
    
    final_json["tools"] = executed_tools
    logger.info(f"Completed Investigation (forced). RCA: {json.dumps(final_json)[:2000]}")
    return final_json


def _build_rca_from_evidence(service, executed_tools, raw_text):
    """Build an RCA from the tool results when LLM can't produce JSON."""
    evidence = []
    errors_found = []
    empty_results = []
    
    for t in executed_tools:
        data = t["result"].get("mcp_data", [])
        status = t["result"].get("status", "unknown")
        
        if status == "error":
            errors_found.append(f"{t['name']}: {t['result'].get('error', 'unknown error')[:100]}")
        elif data and data[0] and str(data[0]) != "[]":
            evidence.append({"type": "tool_result", "source": t["name"], "detail": str(data[0])[:200]})
        else:
            empty_results.append(t["name"])
    
    what_failed = service
    if not evidence and not errors_found:
        how_failed = f"No data found for '{service}' in any datasource. This namespace/service may not exist in the cluster."
        root_cause = [f"The namespace '{service}' does not exist in Grafana's connected Kubernetes cluster"]
        immediate_fix = [
            f"Verify the service name — check existing namespaces in the K8s Pods dashboard",
            "Try querying with an existing namespace (e.g. 'default', 'monitoring', 'kube-system')"
        ]
    else:
        how_failed = "; ".join(errors_found[:3]) if errors_found else "See evidence below"
        root_cause = errors_found[:3] if errors_found else ["Investigation completed — see evidence"]
        immediate_fix = ["Review the evidence gathered by the MCP tools above"]
    
    return {
        "rca": {
            "what_failed": what_failed,
            "how_it_failed": how_failed,
            "root_cause": root_cause,
            "evidence": evidence[:5],
            "impact": f"Investigation covered {len(executed_tools)} tool calls across Prometheus and Loki",
            "immediate_fix": immediate_fix,
            "long_term_fix": ["Set up alerting for this service in Grafana", "Add dashboards for service-level monitoring"]
        },
        "raw_response": raw_text[:1000] if raw_text else ""
    }

