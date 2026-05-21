"""Multi-round LLM-driven agent with iterative tool orchestration via MCP.

Implements proper conversation loop where tool results feed back to LLM for continued investigation.
Output format: structured pod-level dashboard JSON for rich Slack rendering.
"""
import sys
import os
import json
import re
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.mcp_client import mcp_client
from src.llm_clients import OpenRouterClient
from config import CONFIG
import logging
from datetime import datetime, timezone, timedelta

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
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Query Intent Extraction ────────────────────────────────────────────────

def _extract_query_intent(user_query: str, alert_context: dict = None):
    """Extract namespaces, datasources, and investigation type from the query.
    
    Returns dict with:
      - namespaces: list of namespace strings, or ["all"]
      - datasource_uid: "mimir" or "prometheus" (best guess)
      - datasource_broken: True if the alert's own datasource errored
      - investigation_type: "restarts"|"oom"|"cpu"|"memory"|"logs"|"general"
      - pod_name: specific pod if mentioned, else None
      - alert_error: the error string from the alert, if any
      - alertname: the alert rule name, if any
      - summary: the alert summary template, if any
    """
    q = user_query.lower()
    result = {
        "namespaces": ["all"],
        "datasource_uid": "mimir",
        "datasource_broken": False,
        "investigation_type": "general",
        "pod_name": None,
        "alert_error": None,
        "alertname": None,
        "summary": None,
    }

    # 1. Extract from alert context first (most reliable)
    if alert_context:
        ns = alert_context.get("namespace")
        if ns:
            result["namespaces"] = [ns]
        pod = alert_context.get("pod")
        if pod:
            result["pod_name"] = pod
        alertname = alert_context.get("alertname")
        if alertname:
            result["alertname"] = alertname
        summary = alert_context.get("summary")
        if summary:
            result["summary"] = summary

        # Datasource intelligence: check if the alert's datasource is broken
        ds = alert_context.get("datasource_uid")
        error = alert_context.get("Error") or alert_context.get("error") or ""
        result["alert_error"] = error if error else None

        if ds:
            # Check if the error indicates the datasource itself is unreachable
            error_lower = error.lower()
            if any(sig in error_lower for sig in ["no such host", "connection refused",
                                                    "dial tcp", "failed to execute query",
                                                    "datasource not found"]):
                # The alert's datasource is broken — use the alternative
                result["datasource_broken"] = True
                result["datasource_uid"] = "mimir" if ds == "prometheus" else "prometheus"
                print(f"[Agent] Alert datasource '{ds}' is broken (error: {error[:100]}), falling back to '{result['datasource_uid']}'")
            else:
                result["datasource_uid"] = ds

        # Infer investigation type from alert name
        if alertname:
            aname_lower = alertname.lower()
            if any(w in aname_lower for w in ["restart", "crashloop", "crash"]):
                result["investigation_type"] = "restarts"
            elif any(w in aname_lower for w in ["oom", "memory"]):
                result["investigation_type"] = "oom"
            elif "cpu" in aname_lower:
                result["investigation_type"] = "cpu"

    # 2. Extract namespace from query text (only if not already set from alert)
    if result["namespaces"] == ["all"]:
        # Common English words that should NOT be treated as namespace names
        ns_stopwords = {"for", "the", "and", "all", "any", "pod", "pods", "check",
                        "get", "show", "list", "find", "from", "with", "what",
                        "restarts", "restart", "memory", "cpu", "usage", "errors",
                        "today", "yesterday", "cluster", "dev", "developer"}
        # Jinja/Go template fragments that are NOT real namespaces
        template_stopwords = {"}}/", "}}", "{{", "$labels", "$value", "$labels.namespace",
                              "$labels.pod", "labels.namespace"}
        ns_patterns = [
            # Most specific patterns first
            r'(?:check|scan|inspect|look at)\s+(\S+)\s+namespace',
            r'in\s+(\S+)\s+namespace',
            r'\bns[:\s=]+["\']?(\S+)["\']?',
            r'namespace\s*=\s*(\S+)',
        ]
        for pat in ns_patterns:
            m = re.search(pat, q)
            if m:
                candidate = m.group(1).strip("\"'")
                # Reject stopwords and Jinja template fragments
                if (candidate.lower() not in ns_stopwords and
                    not any(t in candidate for t in template_stopwords) and
                    re.match(r'^[a-z][a-z0-9-]*$', candidate)):
                    result["namespaces"] = [candidate]
                    break

    # Direct namespace name mention (only if no explicit pattern matched)
    if result["namespaces"] == ["all"]:
        common_ns = [
            "kube-system", "ingress-nginx", "cert-manager", "velero",
            "awx", "observability", "observe", "platform", "default",
            "monitoring", "argocd", "istio-system", "logging",
        ]
        for ns in common_ns:
            # Ensure it's a standalone mention, not inside a URL like prometheus-server.monitoring.svc
            pattern = r'(?:^|\s|[,;:])' + re.escape(ns) + r'(?:$|\s|[,;:])'
            if re.search(pattern, q):
                result["namespaces"] = [ns]
                break

    # "all namespaces" / "every namespace" / "cluster wide"
    if any(phrase in q for phrase in ["all namespace", "every namespace", "cluster wide",
                                       "entire cluster", "all ns", "across namespace"]):
        result["namespaces"] = ["all"]

    # 3. Extract pod name from query text (if not already set from alert)
    if not result["pod_name"]:
        pod_stopwords = {"restarts", "restart", "memory", "cpu", "usage", "errors",
                         "status", "logs", "health", "issues", "problems", "high",
                         "crash", "check", "info", "name", "names"}
        pod_match = re.search(r'pod[:\s=]+["\']?([a-z0-9][\w.-]+)["\']?', q)
        if pod_match:
            candidate = pod_match.group(1)
            if candidate.lower() not in pod_stopwords:
                result["pod_name"] = candidate

    # 4. Determine investigation type from query text (if not already set from alert)
    if result["investigation_type"] == "general":
        if any(w in q for w in ["restart", "crashloop", "crash loop", "crashing"]):
            result["investigation_type"] = "restarts"
        elif any(w in q for w in ["oom", "out of memory", "oomkill", "memory kill"]):
            result["investigation_type"] = "oom"
        elif any(w in q for w in ["cpu", "throttl", "cpu usage"]):
            result["investigation_type"] = "cpu"
        elif any(w in q for w in ["memory", "mem usage", "memory usage"]):
            result["investigation_type"] = "memory"
        elif any(w in q for w in ["log", "error log", "stderr"]):
            result["investigation_type"] = "logs"

    logger.info(f"Query intent: {json.dumps(result)}")
    print(f"[Agent] Query intent: {json.dumps(result)}")
    return result


def _parse_alert_from_text(text: str) -> dict:
    """Parse a pasted Grafana alert payload from Slack message text.
    
    Handles the full Grafana alert notification format:
      Value: [no value]
      Labels:
       - alertname = Pod High Restart Rate
       - namespace = monitoring
       - pod = alloy-metrics-ztkq9
       - severity = warning
      Annotations:
       - Error = [sse.dataQueryError] failed to execute query ...
       - datasource_uid = prometheus
       - summary = Pod {{ $labels.namespace }}/{{ $labels.pod }} restarted ...
    """
    ctx = {}
    
    # Relevant fields from both Labels and Annotations (case-insensitive lookup)
    relevant_keys_lower = {
        # From Labels
        "namespace", "pod", "severity", "alertname", "grafana_folder",
        # From Annotations
        "datasource_uid", "error", "summary", "ref_id",
        "grafana_state_reason", "description",
    }
    # Canonical key names (preserve original casing for these)
    canonical_keys = {
        "error": "Error",
        "datasource_uid": "datasource_uid",
        "grafana_state_reason": "grafana_state_reason",
        "grafana_folder": "grafana_folder",
        "ref_id": "ref_id",
    }
    
    # Parse all "- key = value" lines from Labels and Annotations sections
    # Use multiple regex patterns to handle Slack text formatting variations
    patterns = [
        r'-\s*([\w_]+)\s*=\s*(.+)',              # Standard: - key = value
        r'[•\*]\s*([\w_]+)\s*=\s*(.+)',           # Slack bullet: • key = value
        r'^\s*([\w_]+)\s*=\s*(.+)',                # No prefix: key = value (per line)
    ]
    
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.MULTILINE):
            key_raw = m.group(1).strip()
            val = m.group(2).strip()
            key_lower = key_raw.lower()
            
            if key_lower in relevant_keys_lower:
                # Use canonical key name if defined, otherwise use raw key
                key = canonical_keys.get(key_lower, key_lower)
                # Don't overwrite a value already captured (first match wins)
                if key not in ctx:
                    ctx[key] = val
    
    if not ctx:
        return None
    
    # Log what we parsed for debugging
    keys_found = list(ctx.keys())
    print(f"[Agent] Parsed alert context ({len(keys_found)} fields: {', '.join(keys_found)}): {json.dumps(ctx, indent=2)}")
    logger.info(f"Parsed alert context: {json.dumps(ctx)}")
    return ctx


# ─── Dynamic System Prompt ───────────────────────────────────────────────────

def _build_system_prompt(intent: dict):
    """Build a system prompt tailored to the query intent."""
    namespaces = intent.get("namespaces", ["all"])
    ds_uid = intent.get("datasource_uid", "mimir")
    pod_name = intent.get("pod_name")

    # Namespace filter string for PromQL examples
    if namespaces == ["all"] or "all" in namespaces:
        ns_filter = ""  # no namespace filter = scan everything
        ns_instruction = "Query ALL namespaces (do NOT add a namespace filter)."
    else:
        ns_list = "|".join(namespaces)
        ns_filter = f'namespace=~"{ns_list}"'
        ns_instruction = f"Focus on namespace(s): {', '.join(namespaces)}."

    pod_filter = ""
    pod_instruction = ""
    if pod_name:
        pod_filter = f', pod=~".*{pod_name}.*"'
        pod_instruction = f"Pay special attention to pod: {pod_name}."

    prom_filter = f"{{{ns_filter}{pod_filter}}}" if (ns_filter or pod_filter) else ""
    # Clean up leading comma if ns_filter was empty
    prom_filter = prom_filter.replace("{, ", "{").replace("{,", "{")

    # Example namespace for PromQL examples (use actual target, not hardcoded)
    example_ns = namespaces[0] if namespaces and namespaces[0] != "all" else "kube-system"

    return f"""You are an SRE assistant with Grafana MCP tools. Current time: {NOW}

DATASOURCES — use these UIDs:
- Prometheus/Mimir metrics: datasourceUid="{ds_uid}"
- Loki logs: datasourceUid="loki"

If a query fails with one datasource, try the alternative:
  - If "{ds_uid}" fails, try "{"prometheus" if ds_uid == "mimir" else "mimir"}"

NAMESPACE SCOPE: {ns_instruction}
{pod_instruction}

IMPORTANT: Always use query_prometheus for metrics. Do NOT use list_prometheus_metric_names.

INVESTIGATION STRATEGY:
1. DISCOVERY — Query across the target scope to find what's broken:
   kube_pod_container_status_restarts_total{prom_filter}
2. DIAGNOSIS — For pods with restarts, check termination reason:
   kube_pod_container_status_last_terminated_reason{prom_filter}
3. RESOURCES — Check CPU and memory for problematic pods:
   rate(container_cpu_usage_seconds_total{prom_filter}[5m])
   container_memory_working_set_bytes{prom_filter}
4. LOGS — Pull error logs from Loki for the worst offenders
5. SYNTHESIZE — Build the dashboard JSON

EXAMPLE TOOL CALLS:
1. Pod restarts (all namespaces):
   query_prometheus(expr="kube_pod_container_status_restarts_total", queryType="instant", datasourceUid="{ds_uid}", startTime="{NOW}")
2. Pod restarts (specific namespace):
   query_prometheus(expr="kube_pod_container_status_restarts_total{{namespace=\\"{example_ns}\\"}}", queryType="instant", datasourceUid="{ds_uid}", startTime="{NOW}")
3. Last terminated reason:
   query_prometheus(expr="kube_pod_container_status_last_terminated_reason{prom_filter}", queryType="instant", datasourceUid="{ds_uid}", startTime="{NOW}")
4. Error logs:
   query_loki_logs(logql="{{namespace=\\"{example_ns}\\"}} |= \\"error\\"", datasourceUid="loki", startRfc3339="{NOW[:11]}00:00:00Z", endRfc3339="{NOW}")

Use timestamps near {NOW}.

After gathering data, return ONLY valid JSON (no markdown, no code fences).
Use this EXACT structure:

{{"dashboard": {{
  "critical_pods": <number of pods with OOMKilled or >10 restarts>,
  "total_restarts": <sum of all restart counts found>,
  "namespaces_affected": ["namespace1", "namespace2"],
  "pods": [
    {{
      "name": "<pod-name>",
      "namespace": "<namespace>",
      "severity": "critical|high|medium|low",
      "category": "OOMKilled|Error exit|CrashLoopBackOff|High CPU|High Memory|Healthy",
      "restarts": <restart count>,
      "description": "<1-2 sentence explanation>",
      "log_excerpt": "<key error log line or empty string>",
      "recommended_action": "<specific fix>"
    }}
  ],
  "technical_findings": "<What the tools returned — raw data summary. e.g. 'Queried kube_pod_container_status_restarts_total across all namespaces. Found 16 pods with non-zero restarts. Highest: workflow-service-6c9c7bdc5c-xq7cd (281 restarts). Checked termination reasons: 3 OOMKilled, 12 Error exit.'>",
  "theoretical_analysis": "<Interpretation of the data — what it MEANS, not what the tools returned. e.g. 'The high restart rate on workflow-service suggests a memory leak or misconfigured resource limits. The OOMKilled termination reason confirms the container is being killed by the kernel when it exceeds its memory limit. This is a common pattern when workloads grow beyond initial capacity planning.'>",
  "analysis": {{
    "what_failed": "<Which pods/services failed, or 'No failures detected — all pods are healthy'>",
    "how_it_failed": "<Crash type: OOMKilled, CrashLoopBackOff, Error exit, etc. or 'N/A'>",
    "where_it_failed": "<Namespace and cluster location, e.g. 'monitoring namespace on dev cluster'>",
    "why_it_failed": "<Root cause: memory limits too low, DNS resolution failure, config error, etc. or 'N/A'>",
    "when_it_failed": "<Time of last restart or incident window, or 'N/A'>",
    "how_to_fix": "<Specific remediation steps, or 'No action needed'>"
  }},
  "conclusion": "<2-3 sentence summary of the overall health. Be conversational like a senior SRE. If healthy, say so clearly.>",
  "recommended_actions": [
    "<An imperative fix based on findings — e.g. 'Increase memory limits for workflow-service to 512Mi'>",
    "<Another action — e.g. 'Restart alloy-metrics pod after fixing config'>",
    "<A third action — e.g. 'Add PgBouncer to prevent DB connection exhaustion'>"
  ],
  "follow_up_questions": [
    "<A relevant investigative question based on what was found — e.g. 'What do the Loki error logs show for workflow-service?'>",
    "<Another question — e.g. 'Is memory usage trending up over the last 24h?'>",
    "<A third question — e.g. 'Are other namespaces showing similar OOM patterns?'>"
  ]
}}}}

SEVERITY RULES:
- critical: OOMKilled pods or restarts > 50
- high: restarts > 10 or Error exit
- medium: restarts > 0 but < 10
- low: no restarts, minor issues
- If everything is healthy, set critical_pods=0, use category="Healthy", and say so in the analysis.
  In that case, set recommended_actions to ["No action needed — system is healthy"] and
  follow_up_questions to relevant exploratory questions like checking other namespaces.

Sort pods by severity (critical first), then by restart count (highest first).

CRITICAL RULES FOR follow_up_questions:
- Questions MUST be related to what was found in THIS investigation
- Questions MUST be DIFFERENT every time — NEVER repeat the same generic questions
- Questions should be investigative (start with "What", "Why", "How", "Is", "Are", "Check", "Show")
- Questions should help the user dig deeper into the specific issues found
- Example bad: "Check pod restarts" (too generic)
- Example good: "What do the Loki error logs show for workflow-service-6c9c7bdc5c in the last 2 hours?"

CRITICAL RULES FOR recommended_actions:
- Actions MUST be imperative commands (start with a verb: "Increase", "Restart", "Add", "Check", "Scale")
- Actions MUST be specific to the findings (include pod names, namespaces, actual values)
- If no issues, use ["No action needed — all systems healthy"]
"""


# ─── Tool Filtering ──────────────────────────────────────────────────────────

# Only send the most useful tools to the LLM to reduce prompt size
PRIORITY_TOOLS = {
    "query_prometheus",
    "query_loki_logs",
    "list_alert_rules",
    "search_dashboards",
    "get_dashboard_by_uid",
    "list_datasources",
}

def _filter_tools(schemas):
    """Keep only priority tools to fit within local LLM context."""
    filtered = [s for s in schemas if s.get("function", {}).get("name") in PRIORITY_TOOLS]
    if not filtered:
        return schemas[:10]  # fallback: just take first 10
    return filtered

def _extract_tool_calls(msg_obj):
    """Extract tool calls from various LLM response formats.
    
    Handles:
    1. Standard OpenAI format: msg_obj["tool_calls"] = [{function: {name, arguments}}]
    2. Qwen3 XML format in content: <tool_call>\n<function=name>\n<parameter=key>value</parameter>\n</function>\n</tool_call>
    3. Qwen3 alternate XML: <tool_call>{"name": "func", "arguments": {...}}</tool_call>
    """
    # ── 1. Standard OpenAI tool_calls field ──────────────────────────────────
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
                    "type": "function",
                    "function": {"name": name, "arguments": args}
                })
            else:
                normalized.append(tc)
        return normalized
    
    # ── 2. Parse XML-format tool calls from content ──────────────────────────
    content = msg_obj.get("content", "") or ""
    
    # Strip <think>...</think> blocks first
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
    
    if "<tool_call>" not in content:
        return []
    
    normalized = []
    
    # Pattern A: <tool_call>\n<function=query_prometheus>\n<parameter=key>value</parameter>\n</function>\n</tool_call>
    xml_blocks = re.findall(r'<tool_call>(.*?)</tool_call>', content, re.DOTALL)
    
    for block in xml_blocks:
        block = block.strip()
        
        # Try Pattern A: <function=name>...<parameter=key>value</parameter>...</function>
        func_match = re.search(r'<function=(\w+)>', block)
        if func_match:
            fname = func_match.group(1)
            # Extract all parameters
            params = {}
            for pm in re.finditer(r'<parameter=(\w+)>(.*?)</parameter>', block, re.DOTALL):
                key = pm.group(1).strip()
                val = pm.group(2).strip()
                # Try to parse value as JSON (for nested objects)
                try:
                    val = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    pass  # keep as string
                params[key] = val
            
            normalized.append({
                "id": f"call_{len(normalized)}",
                "type": "function",
                "function": {"name": fname, "arguments": json.dumps(params)}
            })
            continue
        
        # Try Pattern B: {"name": "func", "arguments": {...}} inside <tool_call>
        try:
            tc_json = json.loads(block)
            name = tc_json.get("name", "")
            args = tc_json.get("arguments", {})
            if isinstance(args, dict):
                args = json.dumps(args)
            if name:
                normalized.append({
                    "id": f"call_{len(normalized)}",
                    "type": "function",
                    "function": {"name": name, "arguments": args}
                })
        except json.JSONDecodeError:
            pass
    
    if normalized:
        names = [tc["function"]["name"] for tc in normalized]
        print(f"[Agent] Parsed {len(normalized)} XML-format tool calls: {', '.join(names)}")
    
    return normalized


# ─── JSON Parsing ────────────────────────────────────────────────────────────

def _parse_dashboard_json(content):
    """Parse the LLM response into dashboard JSON, handling various formats."""
    content = content.strip()
    
    # Strip markdown code fences
    if content.startswith("```json"):
        content = content[7:]
    if content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    content = content.strip()
    
    # Strip thinking tags (Qwen3 includes <think>...</think>)
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
    
    # Try direct parse
    try:
        parsed = json.loads(content)
        if "dashboard" in parsed:
            # Ensure dashboard value is a dict, not a list or other type
            if not isinstance(parsed["dashboard"], dict):
                parsed["dashboard"] = {"raw_data": parsed["dashboard"]}
            return parsed
        # Wrap old-format RCA in dashboard
        if "rca" in parsed:
            return _convert_rca_to_dashboard(parsed)
        if isinstance(parsed, dict):
            return {"dashboard": parsed}
        return {"dashboard": {"raw_data": parsed}}
    except json.JSONDecodeError:
        pass
    
    # Try to find embedded JSON
    json_match = re.search(r'\{[\s\S]*"dashboard"[\s\S]*\}', content)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    
    # Try to find old-format RCA JSON
    json_match = re.search(r'\{[\s\S]*"rca"[\s\S]*\}', content)
    if json_match:
        try:
            parsed = json.loads(json_match.group())
            return _convert_rca_to_dashboard(parsed)
        except json.JSONDecodeError:
            pass
    
    return None


def _convert_rca_to_dashboard(rca_json):
    """Convert old-format RCA JSON to the new dashboard format."""
    rca = rca_json.get("rca", rca_json)
    
    # Build a single pod entry from the flat RCA
    pod = {
        "name": rca.get("what_failed", "unknown"),
        "namespace": "unknown",
        "severity": "high",
        "category": "Error exit",
        "restarts": 0,
        "description": rca.get("how_it_failed", "See investigation details"),
        "log_excerpt": "",
        "recommended_action": rca.get("immediate_fix", ["Review logs"])[0] if isinstance(rca.get("immediate_fix"), list) else str(rca.get("immediate_fix", "Review logs"))
    }
    
    actions = []
    for fix in rca.get("immediate_fix", []):
        actions.append({"title": str(fix)[:60], "description": "Immediate fix", "query": str(fix)})
    for fix in rca.get("long_term_fix", []):
        actions.append({"title": str(fix)[:60], "description": "Long-term improvement", "query": str(fix)})
    
    evidence_text = ""
    for e in rca.get("evidence", []):
        if isinstance(e, dict):
            evidence_text += f"- [{e.get('type','')}] {e.get('source','')}: {e.get('detail','')}\n"
    
    return {
        "dashboard": {
            "critical_pods": 1 if rca.get("root_cause") else 0,
            "total_restarts": 0,
            "namespaces_affected": ["unknown"],
            "pods": [pod],
            "recommended_actions": actions[:4],
            "full_picture": rca.get("impact", "") + "\n\n" + "\n".join(rca.get("root_cause", [])) + "\n\n" + evidence_text
        }
    }


# ─── Pre-fetch Grafana Data ──────────────────────────────────────────────────

def _prefetch_grafana_data(intent: dict) -> list:
    """Pre-fetch key metrics and logs from MCP tools based on query intent.
    
    Returns a list of executed tool records (same format as executed_tools).
    This ensures we always have real data even when the LLM can't use tool-calling.
    """
    results = []
    namespaces = intent.get("namespaces", ["all"])
    ds_uid = intent.get("datasource_uid", "mimir")
    pod_name = intent.get("pod_name")
    inv_type = intent.get("investigation_type", "general")
    
    # ── Datasource auto-discovery ────────────────────────────────────────
    # The hardcoded "mimir"/"prometheus" UIDs may not exist. Discover real ones.
    try:
        ds_result = mcp_client.execute_tool("list_datasources", {})
        if ds_result.get("status") == "success":
            ds_data = ds_result.get("mcp_data", [])
            # Parse datasources list — find Prometheus-compatible ones
            prom_uids = []
            for item in ds_data:
                # mcp_data can be a text blob or structured data
                text = str(item) if not isinstance(item, str) else item
                # Try structured parse
                if isinstance(item, dict):
                    ds_list = item.get("data", item.get("datasources", [item]))
                    if isinstance(ds_list, list):
                        for ds in ds_list:
                            if isinstance(ds, dict):
                                ds_type = ds.get("type", "").lower()
                                uid = ds.get("uid", "")
                                name = ds.get("name", "")
                                if uid and any(t in ds_type for t in ["prometheus", "mimir", "cortex", "thanos"]):
                                    prom_uids.append(uid)
                                    print(f"[Prefetch] Found Prometheus-compatible datasource: {name} (uid={uid}, type={ds_type})")
                else:
                    # Text-based: look for UIDs mentioned
                    import re as _re
                    uid_matches = _re.findall(r'uid["\s:=]+["\']([\w-]+)["\']', text)
                    for uid in uid_matches:
                        if uid not in ("loki",):
                            prom_uids.append(uid)
            
            if prom_uids:
                best_uid = prom_uids[0]
                if best_uid != ds_uid:
                    print(f"[Prefetch] Switching datasource: {ds_uid} → {best_uid}")
                    ds_uid = best_uid
                    intent["datasource_uid"] = best_uid  # Update intent for downstream use
            else:
                print(f"[Prefetch] No Prometheus-compatible datasources found in list_datasources response")
    except Exception as e:
        print(f"[Prefetch] list_datasources failed: {e}, using default ds_uid={ds_uid}")
    
    
    # Build namespace filter
    if namespaces == ["all"] or "all" in namespaces:
        ns_filter = ""
    else:
        ns_filter = f'namespace=~"{ "|".join(namespaces) }"'
    
    pod_filter = f', pod=~".*{pod_name}.*"' if pod_name else ""
    
    def _safe_call(name, args):
        """Execute an MCP tool and return the result record."""
        try:
            print(f"[Prefetch] Calling {name}({json.dumps(args)[:120]})")
            res = mcp_client.execute_tool(name, args)
            status = res.get('status', 'unknown')
            if status == 'error':
                print(f"[Prefetch] {name} → ERROR: {res.get('error', 'unknown')[:200]}")
            else:
                # Log a preview of the data
                data = res.get('mcp_data', [])
                data_preview = str(data[0])[:150] if data else "empty"
                print(f"[Prefetch] {name} → {status} ({len(data)} items, preview: {data_preview})")
            return {"name": name, "args": args, "result": res}
        except Exception as e:
            print(f"[Prefetch] {name} failed: {e}")
            # Try alternate datasource
            if "datasourceUid" in args:
                alt_ds = "prometheus" if args["datasourceUid"] == "mimir" else "mimir"
                try:
                    args_alt = dict(args)
                    args_alt["datasourceUid"] = alt_ds
                    res = mcp_client.execute_tool(name, args_alt)
                    print(f"[Prefetch] {name} fallback to '{alt_ds}' → {res.get('status', 'unknown')}")
                    return {"name": name, "args": args_alt, "result": res}
                except Exception as e2:
                    pass
            return {"name": name, "args": args, "result": {"status": "error", "error": str(e)}}
    
    filter_str = f"{{{ns_filter}{pod_filter}}}" if (ns_filter or pod_filter) else ""
    filter_str = filter_str.replace("{, ", "{").replace("{,", "{")
    
    # Time range for queries — MCP query_prometheus REQUIRES startTime/endTime
    now = datetime.now(timezone.utc)
    end_time = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    start_time = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    start_time_24h = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # 1. Pod restarts (relevant for all investigation types)
    results.append(_safe_call("query_prometheus", {
        "datasourceUid": ds_uid,
        "expr": f"kube_pod_container_status_restarts_total{filter_str}",
        "queryType": "instant",
        "startTime": start_time,
        "endTime": end_time
    }))
    
    # 2. Type-specific queries
    if inv_type in ("cpu", "general"):
        results.append(_safe_call("query_prometheus", {
            "datasourceUid": ds_uid,
            "expr": f"rate(container_cpu_usage_seconds_total{filter_str}[5m])",
            "queryType": "instant",
            "startTime": start_time,
            "endTime": end_time
        }))
    
    if inv_type in ("oom", "memory", "general"):
        results.append(_safe_call("query_prometheus", {
            "datasourceUid": ds_uid,
            "expr": f"container_memory_working_set_bytes{filter_str} / on(pod,namespace) kube_pod_container_resource_limits{{resource='memory'{(',' + ns_filter) if ns_filter else ''}{pod_filter}}}",
            "queryType": "instant",
            "startTime": start_time,
            "endTime": end_time
        }))
    
    if inv_type in ("restarts", "general"):
        results.append(_safe_call("query_prometheus", {
            "datasourceUid": ds_uid,
            "expr": f"changes(kube_pod_container_status_restarts_total{filter_str}[24h])",
            "queryType": "instant",
            "startTime": start_time_24h,
            "endTime": end_time
        }))
    
    # 3. Alert rules (always useful)
    results.append(_safe_call("list_alert_rules", {}))
    
    # 4. Loki error logs (if we have a specific namespace or pod)
    if ns_filter or pod_filter:
        loki_filter = f"{{{ns_filter}{pod_filter}}}" if (ns_filter or pod_filter) else '{}'
        loki_filter = loki_filter.replace("{, ", "{").replace("{,", "{")
        
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        results.append(_safe_call("query_loki_logs", {
            "datasourceUid": "loki",
            "logql": f'{loki_filter} |= "error"',
            "startRfc3339": start,
            "endRfc3339": end
        }))
    
    # Filter out failed results for logging
    ok_count = sum(1 for r in results if r["result"].get("status") == "success")
    print(f"[Prefetch] Completed: {ok_count}/{len(results)} calls succeeded")
    
    return results


# ─── Inject Intent into Dashboard ────────────────────────────────────────────

def _inject_intent_into_dashboard(result, intent):
    """Ensure the dashboard always reflects the intent's scope (namespace, pod, type).
    
    Uses alert context from the intent to populate meaningful analysis even when
    tools fail. The alert payload itself often contains enough info for diagnosis.
    """
    # Type guard: ensure dashboard is always a dict
    dashboard = result.get("dashboard", {})
    if not isinstance(dashboard, dict):
        dashboard = {"raw_data": dashboard}
        result["dashboard"] = dashboard
    
    intent_ns = intent.get("namespaces", ["all"])
    pod_name = intent.get("pod_name")
    alertname = intent.get("alertname")
    alert_error = intent.get("alert_error", "") or ""
    summary = intent.get("summary", "") or ""
    inv_type = intent.get("investigation_type", "general")
    ns_label = ", ".join(intent_ns) if intent_ns != ["all"] else "the cluster"
    
    # Fix namespaces_affected — use intent's namespaces if dashboard has empty/all
    ns_in_dash = dashboard.get("namespaces_affected", [])
    if (not ns_in_dash or ns_in_dash == ["all"] or ns_in_dash == []) and intent_ns != ["all"]:
        dashboard["namespaces_affected"] = intent_ns
    
    # Ensure pod is in the dashboard if the intent has one
    if pod_name:
        pod_names_in_dash = [p.get("name", "") for p in dashboard.get("pods", [])]
        if not any(pod_name in pn for pn in pod_names_in_dash):
            # Add the targeted pod from alert context
            severity = "high" if alertname else "medium"
            category = "High CPU" if inv_type == "cpu" else "OOMKilled" if inv_type in ("oom", "memory") else "High restarts" if inv_type == "restarts" else "Error exit"
            dashboard.setdefault("pods", []).insert(0, {
                "name": pod_name,
                "namespace": intent_ns[0] if intent_ns != ["all"] else "unknown",
                "severity": severity,
                "category": category,
                "restarts": 0,
                "description": summary or (f"Alert: {alertname}" if alertname else "Flagged by investigation"),
                "log_excerpt": alert_error[:200] if alert_error else "",
                "recommended_action": "Check pod logs and resource limits"
            })
    
    # Ensure technical_findings uses real tool data + alert context
    if not dashboard.get("technical_findings") or dashboard.get("technical_findings") in (
        "No data retrieved from tools", "Waiting for tool results to populate dashboard..."
    ):
        tools = result.get("tools", [])
        tech_lines = []
        success_count = 0
        error_count = 0
        for t in tools:
            status = t["result"].get("status", "unknown")
            if status == "success":
                success_count += 1
                data = t["result"].get("mcp_data", [])
                data_preview = str(data[0])[:200] if data else "empty"
                tech_lines.append(f"✅ {t['name']}: {data_preview}")
            else:
                error_count += 1
                err = t["result"].get("error", "unknown error")[:100]
                tech_lines.append(f"❌ {t['name']}: {err}")
        
        findings = f"Queried {len(tools)} tools ({success_count} succeeded, {error_count} failed).\n"
        findings += "\n".join(tech_lines[:6])
        if alert_error:
            findings += f"\n\nAlert error: {alert_error[:300]}"
        dashboard["technical_findings"] = findings
    
    # Ensure theoretical_analysis uses alert context for meaningful interpretation
    if not dashboard.get("theoretical_analysis") or dashboard.get("theoretical_analysis", "").startswith("Investigation of"):
        if alertname:
            analysis_text = f"Alert '{alertname}' fired for pod {pod_name or 'unknown'} in {ns_label}. "
            if inv_type == "cpu":
                analysis_text += "CPU throttling indicates the container is hitting its CPU limits. This typically means the pod needs higher CPU requests/limits, or the workload has grown beyond initial sizing. "
            elif inv_type in ("oom", "memory"):
                analysis_text += "OOMKilled indicates the container exceeded its memory limits. This could be a memory leak, undersized limits, or a spike in workload. "
            elif inv_type == "restarts":
                analysis_text += "Repeated restarts suggest the container is crash-looping. Common causes: configuration errors, missing dependencies, or resource exhaustion. "
            if alert_error:
                analysis_text += f"Note: The monitoring datasource is unreachable ({alert_error[:100]}...), which means live metrics could not be fetched. The analysis is based on the alert payload."
            dashboard["theoretical_analysis"] = analysis_text
        else:
            dashboard["theoretical_analysis"] = f"Investigation of {ns_label} for {inv_type} issues. Analysis based on available data."
    
    # Ensure analysis fields use alert context
    analysis = dashboard.get("analysis", {})
    if not analysis or all(v in ("N/A", "", None, "No failures detected yet") for v in analysis.values()):
        if alertname and pod_name:
            dashboard["analysis"] = {
                "what_failed": f"{pod_name} — {alertname}",
                "how_it_failed": {"cpu": "CPU throttling — pod hitting CPU limits", "oom": "OOMKilled — memory limit exceeded", "memory": "High memory usage", "restarts": "Container crash-looping"}.get(inv_type, alertname),
                "where_it_failed": f"{ns_label} namespace",
                "why_it_failed": summary or alert_error[:200] or "See alert details",
                "when_it_failed": f"Alert firing as of {NOW}",
                "how_to_fix": {"cpu": "Increase CPU limits/requests for the pod, or optimize the workload", "oom": "Increase memory limits, check for memory leaks", "memory": "Increase memory limits or optimize memory usage", "restarts": "Check pod logs for crash reason, review config"}.get(inv_type, "Review pod logs and resource configuration")
            }
        elif not analysis:
            dashboard["analysis"] = {
                "what_failed": "No specific failures detected",
                "how_it_failed": "N/A", "where_it_failed": ns_label,
                "why_it_failed": "N/A", "when_it_failed": "N/A",
                "how_to_fix": "No action needed"
            }
    
    # Ensure conclusion is meaningful
    if not dashboard.get("conclusion") or dashboard.get("conclusion") in ("Investigation in progress.", ""):
        if alertname:
            dashboard["conclusion"] = (
                f"Alert '{alertname}' is firing for pod {pod_name or 'unknown'} in {ns_label}. "
                f"{'The monitoring datasource is unreachable, so live metrics could not be verified. ' if alert_error else ''}"
                f"Based on the alert payload, {'CPU limits should be reviewed and increased' if inv_type == 'cpu' else 'resource limits should be reviewed'}."
            )
        else:
            pods = dashboard.get("pods", [])
            total = dashboard.get("total_restarts", 0)
            dashboard["conclusion"] = (
                f"Investigation of {ns_label} complete. "
                f"Found {len(pods)} pod(s) with {total} total restarts."
                + (" No critical issues detected." if total == 0 else " Review recommended actions.")
            )
    
    # Ensure follow_up_questions
    if not dashboard.get("follow_up_questions"):
        fq = []
        if pod_name:
            fq.append(f"What are the recent logs for {pod_name}?")
            fq.append(f"Show CPU and memory usage for {pod_name}")
        fq.append(f"Are there other alerts firing in {ns_label}?")
        dashboard["follow_up_questions"] = fq[:3]
    
    # Ensure recommended_actions
    if not dashboard.get("recommended_actions"):
        if alertname and pod_name:
            actions = []
            if inv_type == "cpu":
                actions.append(f"Increase CPU limits for {pod_name}")
                actions.append(f"Check CPU usage pattern: kubectl top pod {pod_name} -n {intent_ns[0] if intent_ns != ['all'] else 'default'}")
            elif inv_type in ("oom", "memory"):
                actions.append(f"Increase memory limits for {pod_name}")
                actions.append(f"Check for memory leaks in {pod_name}")
            elif inv_type == "restarts":
                actions.append(f"Check logs: kubectl logs {pod_name} -n {intent_ns[0] if intent_ns != ['all'] else 'default'} --previous")
            actions.append(f"Describe pod: kubectl describe pod {pod_name} -n {intent_ns[0] if intent_ns != ['all'] else 'default'}")
            dashboard["recommended_actions"] = actions[:3]
        elif dashboard.get("critical_pods", 0) > 0 or dashboard.get("total_restarts", 0) > 0:
            dashboard["recommended_actions"] = ["Review pod health in detail"]
        else:
            dashboard["recommended_actions"] = ["No action needed — system is healthy"]
    
    result["dashboard"] = dashboard


# ─── Main Orchestration ─────────────────────────────────────────────────────

def run_llm_orchestrated_query(user_query: str, service: str = "all", model: str = None, alert_context: dict = None):
    """Multi-round LLM-driven tool orchestration with dashboard-format output.
    
    Args:
        user_query: The user's question
        service: Namespace hint ("all" to scan everything, or a specific namespace)
        model: LLM model override
        alert_context: Parsed alert labels/annotations dict (namespace, pod, datasource_uid, etc.)
    """
    logger.info(f"=== Investigation START: {service} ===")
    logger.info(f"Query: {user_query}")
    print(f"\n[Agent] Query: {user_query}")
    print(f"[Agent] Service/Namespace: {service}")
    
    # Build intent from query + alert context
    intent = _extract_query_intent(user_query, alert_context)
    
    # Override namespaces from service param if it's specific
    if service and service != "all":
        intent["namespaces"] = [service]
    
    # Build dynamic system prompt
    system_prompt = _build_system_prompt(intent)
    
    client = OpenRouterClient()
    model = model or CONFIG.get("OPENROUTER_MODEL")
    
    # Build user message with rich context
    ns_desc = ", ".join(intent["namespaces"]) if intent["namespaces"] != ["all"] else "ALL namespaces"
    user_msg = f"Investigate: {user_query}\nTarget scope: {ns_desc}\n"
    user_msg += f"Use datasourceUid='{intent['datasource_uid']}' for Prometheus metrics and datasourceUid='loki' for Loki logs.\n"
    
    if intent["pod_name"]:
        user_msg += f"\nFocus on pod: {intent['pod_name']}\n"
    
    if intent.get("alertname"):
        user_msg += f"\nAlert: {intent['alertname']}\n"
    
    if intent.get("summary"):
        user_msg += f"Summary: {intent['summary']}\n"
    
    if intent.get("datasource_broken"):
        user_msg += (
            f"\n⚠️ NOTE: The alert's original datasource is BROKEN "
            f"(error: {(intent.get('alert_error') or '')[:200]}). "
            f"Use datasourceUid='{intent['datasource_uid']}' instead.\n"
        )
    
    if alert_context:
        user_msg += f"\nFull alert payload:\n{json.dumps(alert_context, indent=2)}\n"
    
    user_msg += "\nPlease investigate using the available Grafana tools."
    
    max_iterations = 6
    executed_tools = []
    
    # Get tool schemas for reference (but won't pass to LLM since vLLM doesn't support tool_choice)
    all_schemas = mcp_client.get_tool_schema()
    schemas = _filter_tools(all_schemas)
    tool_names = [s["function"]["name"] for s in schemas]
    print(f"[Agent] Available tools: {', '.join(tool_names)}")
    
    # ── Pre-fetch key data from MCP tools directly ──
    # This is our PRIMARY data source — the LLM will analyze this data
    prefetch_results = _prefetch_grafana_data(intent)
    executed_tools.extend(prefetch_results)
    
    # Build data context for the LLM from prefetch results
    data_sections = []
    for t in prefetch_results:
        if t["result"].get("status") == "success":
            mcp_data = t["result"].get("mcp_data", [])
            data_preview = str(mcp_data[0])[:3000] if mcp_data else "no data"
            data_sections.append(f"### {t['name']}({json.dumps(t['args'])})\n{data_preview}")
        else:
            err = t["result"].get("error", "unknown error")[:200]
            data_sections.append(f"### {t['name']} — FAILED: {err}")
    
    if data_sections:
        user_msg += f"\n\n--- PRE-FETCHED GRAFANA DATA ---\n" + "\n\n".join(data_sections) + "\n--- END DATA ---\n"
    
    # Add alert context explicitly
    if alert_context:
        user_msg += f"\n\nALERT CONTEXT:\n"
        for k, v in alert_context.items():
            user_msg += f"  {k}: {str(v)[:200]}\n"
    
    user_msg += "\n\nIMPORTANT: Analyze ALL the data above (including alert context if present, even if tools failed) and return your final JSON dashboard NOW. Do NOT say 'investigation in progress' — provide actual analysis."
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg}
    ]
    
    # ── Single-shot synthesis (no tool loop) ──
    # The vLLM server doesn't support tool_choice=auto, so we skip tools entirely
    # and ask the LLM to analyze the prefetched data in one shot
    print(f"\n[Agent] === Single-shot synthesis (no tool loop) ===")
    print(f"[Agent] Data sections: {len(data_sections)}, User msg: {len(user_msg)} chars")
    
    # Make the LLM call WITHOUT tools (single-shot)
    resp = client.create_chat_completion(model=model, messages=messages, tools=None)
    
    choices = resp.get("choices", [])
    if choices:
        msg_obj = choices[0].get("message", {})
        content = (msg_obj.get("content") or "{}").strip()
        # Strip XML/thinking tags
        content = re.sub(r'<tool_call>.*?</tool_call>', '', content, flags=re.DOTALL).strip()
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        print(f"[Agent] LLM response received ({len(content)} chars)")
        
        parsed = _parse_dashboard_json(content)
        if parsed:
            parsed["tools"] = executed_tools
            parsed["_meta"] = intent
            _inject_intent_into_dashboard(parsed, intent)
            logger.info(f"Completed Investigation. Dashboard: {json.dumps(parsed)[:2000]}")
            return parsed
        
        # LLM returned non-JSON text — use it as raw_text for fallback builder
        print(f"[Agent] Could not parse JSON from LLM response, using fallback builder")
        result = _build_dashboard_from_evidence(service, executed_tools, content, alert_context=alert_context, intent=intent)
        result["tools"] = executed_tools
        result["_meta"] = intent
        _inject_intent_into_dashboard(result, intent)
        return result
    
    # LLM returned empty response — pure fallback
    print("[Agent] WARNING: Empty LLM response, using fallback builder")
    result = _build_dashboard_from_evidence(service, executed_tools, "", alert_context=alert_context, intent=intent)
    result["tools"] = executed_tools
    result["_meta"] = intent
    _inject_intent_into_dashboard(result, intent)
    logger.info(f"Completed Investigation (fallback). Dashboard: {json.dumps(result)[:2000]}")
    return result


def _build_dashboard_from_evidence(service, executed_tools, raw_text, alert_context=None, intent=None):
    """Build a dashboard from raw tool results AND alert context when LLM can't produce JSON."""
    pods = []
    alert_context = alert_context or {}
    intent = intent or {}
    namespaces = set()
    total_restarts = 0
    critical_count = 0
    
    for t in executed_tools:
        data = t["result"].get("mcp_data", [])
        parsed = t["result"].get("parsed_data", [])
        status = t["result"].get("status", "unknown")
        
        if status == "error":
            continue
        
        # Try to extract pod info from Prometheus results
        for item in parsed:
            if isinstance(item, dict):
                results = item.get("data", {}).get("result", []) if isinstance(item.get("data"), dict) else []
                for r in results:
                    metric = r.get("metric", {})
                    value = r.get("value", [0, "0"])
                    val = float(value[1]) if len(value) > 1 else 0
                    
                    pod_name = metric.get("pod", metric.get("container", ""))
                    ns = metric.get("namespace", service)
                    namespaces.add(ns)
                    
                    if pod_name and "restarts" in t["name"].lower() or "restarts" in str(t.get("args", {})):
                        restarts = int(val)
                        total_restarts += restarts
                        
                        if restarts > 0:
                            severity = "critical" if restarts > 50 else "high" if restarts > 10 else "medium"
                            if severity == "critical":
                                critical_count += 1
                            
                            pods.append({
                                "name": pod_name,
                                "namespace": ns,
                                "severity": severity,
                                "category": "High restarts",
                                "restarts": restarts,
                                "description": f"Pod has restarted {restarts} times",
                                "log_excerpt": "",
                                "recommended_action": f"Check logs for {pod_name} to identify crash reason"
                            })
    
    # Deduplicate pods by name, keeping highest restart count
    seen = {}
    for p in pods:
        if p["name"] not in seen or p["restarts"] > seen[p["name"]]["restarts"]:
            seen[p["name"]] = p
    pods = sorted(seen.values(), key=lambda x: x["restarts"], reverse=True)
    
    if not pods:
        label = service if service != "all" else "cluster"
        pods = [{
            "name": label,
            "namespace": service if service != "all" else "all",
            "severity": "low",
            "category": "Healthy",
            "restarts": 0,
            "description": f"No issues detected in {label}" if not raw_text else raw_text[:200],
            "log_excerpt": "",
            "recommended_action": "No action needed — system appears healthy"
        }]
    
    actions = []
    for p in pods[:4]:
        if p["restarts"] > 0:
            actions.append({
                "title": f"Investigate {p['name']}",
                "description": f"Check why this pod has {p['restarts']} restarts",
                "query": f"check error logs for pod {p['name']} in {p['namespace']} namespace"
            })
    
    if not actions:
        actions.append({
            "title": "Run full health check",
            "description": "Scan all namespaces for issues",
            "query": "check all namespaces for pod restarts and errors"
        })
    
    conclusion = raw_text[:1000] if raw_text else (
        f"Investigation of {service if service != 'all' else 'the cluster'} found {len(pods)} pod(s) with "
        f"{total_restarts} total restarts across {len(namespaces) or 1} namespace(s). "
        + (f"The most critical pod is `{pods[0]['name']}` with {pods[0]['restarts']} restarts." if pods and pods[0]['restarts'] > 0 else "No critical issues detected.")
    )
    
    # Build technical findings from tool execution log
    tech_lines = []
    for t in executed_tools:
        status = t["result"].get("status", "unknown")
        tech_lines.append(f"{t['name']}: {status}")
    technical_findings = "; ".join(tech_lines) if tech_lines else "No tools executed"
    
    # Build theoretical analysis (interpretation)
    top_pod = pods[0] if pods and pods[0].get("restarts", 0) > 0 else None
    if top_pod:
        theoretical_analysis = (
            f"The restart pattern on {top_pod['name']} ({top_pod['category']}) "
            f"suggests {'a memory issue — the container is being killed when it exceeds its memory limit' if top_pod['category'] == 'OOMKilled' else 'the container is crashing due to application errors or misconfiguration'}. "
            f"With {top_pod['restarts']} restarts, this indicates a persistent issue that requires attention."
        )
    else:
        theoretical_analysis = (
            f"All pods in {service if service != 'all' else 'the cluster'} are running within normal parameters. "
            f"No crash loops, OOM kills, or abnormal restart patterns detected."
        )
    
    # Build analysis
    analysis = {
        "what_failed": f"{top_pod['name']} ({top_pod['category']})" if top_pod else "No failures detected — all pods are healthy",
        "how_it_failed": top_pod["category"] if top_pod else "N/A",
        "where_it_failed": f"{top_pod['namespace']} namespace" if top_pod else f"{service} namespace",
        "why_it_failed": top_pod["description"] if top_pod else "N/A",
        "when_it_failed": "Recent (within investigation window)",
        "how_to_fix": top_pod["recommended_action"] if top_pod else "No action needed"
    }
    
    # Generate recommended actions (imperative fixes)
    rec_actions = []
    if top_pod:
        rec_actions.append(f"Check logs for {top_pod['name']} in {top_pod['namespace']} namespace")
        if top_pod["category"] == "OOMKilled":
            rec_actions.append(f"Increase memory limits for {top_pod['name']}")
        elif top_pod["category"] in ("CrashLoopBackOff", "Error exit"):
            rec_actions.append(f"Review application config for {top_pod['name']}")
        rec_actions.append(f"Monitor {top_pod['namespace']} namespace for the next 30 minutes")
    else:
        rec_actions.append("No action needed — all systems healthy")
    
    # Generate contextual follow-up questions (investigative)
    follow_ups = []
    if top_pod:
        follow_ups.append(f"What do the Loki error logs show for {top_pod['name']}?")
        follow_ups.append(f"Is memory usage trending up for pods in {top_pod['namespace']}?")
        follow_ups.append(f"Are other namespaces showing similar {top_pod['category']} patterns?")
    else:
        follow_ups.append(f"What is the overall CPU and memory utilization across the cluster?")
        follow_ups.append(f"Are there any pending alerts in Grafana?")
        follow_ups.append(f"Show resource usage trends for the last 24 hours")
    
    return {
        "dashboard": {
            "critical_pods": critical_count,
            "total_restarts": total_restarts,
            "namespaces_affected": list(namespaces) or [service],
            "pods": pods[:10],
            "technical_findings": technical_findings,
            "theoretical_analysis": theoretical_analysis,
            "analysis": analysis,
            "conclusion": conclusion,
            "recommended_actions": rec_actions[:3],
            "follow_up_questions": follow_ups[:3]
        }
    }
