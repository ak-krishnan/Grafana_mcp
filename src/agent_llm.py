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

SYSTEM_PROMPT = f"""You are an SRE assistant with Grafana MCP tools. Current time: {NOW}

DATASOURCES — use these exact UIDs:
- Prometheus/Mimir: datasourceUid="mimir"
- Loki logs: datasourceUid="loki"

KEY RULES:
- query_prometheus needs: expr (PromQL string), queryType ("instant" or "range"), datasourceUid="mimir", startTime (RFC3339)
- For range queries also add: endTime, stepSeconds (e.g. 60)
- query_loki_logs needs: logql, datasourceUid="loki", startRfc3339, endRfc3339
- LogQL example: {{namespace="default"}} |= "error"
- Use timestamps near {NOW}, never 2023 dates
- Available metrics include: kube_pod_status_phase, container_cpu_usage_seconds_total, container_memory_working_set_bytes, kube_pod_container_status_restarts_total

DASHBOARDS: k8s_views_pods, k8s_views_nodes, os6Bh8Omk (cluster), rYdddlPWk (nodes), cflbv4hghxj40f (apisix)

After investigation, return JSON (no markdown):
{{"rca": {{"what_failed": "...", "how_it_failed": "...", "root_cause": ["..."], "evidence": [{{"type": "...", "detail": "..."}}], "impact": "...", "immediate_fix": ["..."], "long_term_fix": ["..."]}}}}
"""

# Only send the most useful tools to the LLM to reduce prompt size
# (43 tools overwhelms a local 14B model and causes timeouts)
PRIORITY_TOOLS = {
    "search_dashboards",
    "get_dashboard_by_uid",
    "list_datasources",
    "query_prometheus",
    "list_prometheus_metric_names",
    "query_loki_logs",
    "list_alert_rules",
    "get_alert_rules_by_dashboard_uid",
    "list_prometheus_label_values",
    "list_prometheus_label_names",
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
    logger.info(f"--- NEW INVESTIGATION STARTED: {service} ---")
    logger.info(f"Query: {user_query}")
    
    client = OpenRouterClient()
    model = model or CONFIG.get("OPENROUTER_MODEL")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Service: {service}\nQuery: {user_query}\n\nPlease investigate using the available Grafana tools. Use datasourceUid='mimir' for Prometheus and datasourceUid='loki' for Loki."}
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
                "content": json.dumps(res)[:1500]  # Aggressive truncation to keep context small
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

