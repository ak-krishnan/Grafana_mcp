"""Slack Bot for Grafana SRE Agent.

Renders investigation results as a rich dashboard in Slack:
- Stat cards (Critical Pods, Total Restarts, Namespaces Affected)
- Pod issue cards grouped by severity
- Interactive Recommended Action buttons
- Full-picture narrative summary

Usage:
    python -m src.slack_bot

Requires env vars:
    SLACK_BOT_TOKEN   — Bot User OAuth Token (xoxb-...)
    SLACK_APP_TOKEN   — App-Level Token for Socket Mode (xapp-...)
"""
import os
import sys
import json
import logging
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from src.agent_llm import run_llm_orchestrated_query, _parse_alert_from_text
from config import CONFIG

logger = logging.getLogger("agent_audit")

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")

if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
    print("ERROR: Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN in .env")
    sys.exit(1)

app = App(token=SLACK_BOT_TOKEN)


# ─── Severity → display config ───────────────────────────────────────────────
SEVERITY_CONFIG = {
    "critical": {"emoji": "🔴", "label": "CRITICAL"},
    "high":     {"emoji": "🟠", "label": "HIGH"},
    "medium":   {"emoji": "🟡", "label": "MEDIUM"},
    "low":      {"emoji": "🟢", "label": "LOW"},
}

CATEGORY_BADGES = {
    "OOMKilled":          "🔴 OOMKilled",
    "Error exit":         "🟠 Error exit",
    "CrashLoopBackOff":   "🔴 CrashLoopBackOff",
    "High CPU":           "🟡 High CPU",
    "High Memory":        "🟡 High Memory",
    "High restarts":      "🟠 High restarts",
    "Healthy":            "🟢 Healthy",
}


def _strip_xml(text):
    """Remove any XML/HTML-like tags from text to prevent LLM markup leaking into Slack."""
    if not text or not isinstance(text, str):
        return text or ""
    import re
    # Remove <tool_call>...</tool_call> blocks completely
    text = re.sub(r'<tool_call>.*?</tool_call>', '', text, flags=re.DOTALL)
    # Remove incomplete <tool_call> blocks (truncated)
    text = re.sub(r'<tool_call>.*$', '', text, flags=re.DOTALL)
    # Remove <think>...</think> blocks
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # Remove any remaining XML-like tags
    text = re.sub(r'</?(?:function|parameter|tool_call|think)[^>]*>', '', text)
    return text.strip()


def _format_dashboard_blocks(result, query):
    """Convert dashboard JSON into rich Slack Block Kit blocks.

    Layout:
    1. Search Scope — where the agent is looking
    2. Stat Cards — horizontal, dynamic based on investigation type
    3. Technical Findings — blockquote box (what the tools returned)
    4. Theoretical Analysis — blockquote box (what the data means)
    5. Pod Table — compact horizontal layout (only if issues exist)
    6. RCA Analysis — 2-column key-value table
    7. Conclusion — outer box styling
    8. Recommended Actions — horizontal buttons (imperative fixes)
    9. Follow-up Questions — horizontal buttons (investigative, contextual)
    """
    dashboard = result.get("dashboard") or {}
    if not isinstance(dashboard, dict):
        dashboard = {"raw_data": dashboard}
    tools = result.get("tools", [])

    critical_pods = dashboard.get("critical_pods", 0)
    total_restarts = dashboard.get("total_restarts", 0)
    namespaces = dashboard.get("namespaces_affected", [])
    pods = dashboard.get("pods", [])
    technical_findings = _strip_xml(dashboard.get("technical_findings", ""))
    theoretical_analysis = _strip_xml(dashboard.get("theoretical_analysis", ""))
    analysis = dashboard.get("analysis", {})
    if isinstance(analysis, dict):
        analysis = {k: _strip_xml(str(v)) for k, v in analysis.items()}
    conclusion = _strip_xml(dashboard.get("conclusion", "") or dashboard.get("full_picture", ""))
    recommended_actions = dashboard.get("recommended_actions", [])
    follow_up_questions = dashboard.get("follow_up_questions", [])
    # Backward compat
    if not follow_up_questions and not recommended_actions:
        old_actions = dashboard.get("recommended_actions", [])
        if isinstance(old_actions, list) and old_actions and isinstance(old_actions[0], dict):
            follow_up_questions = [a.get("query", a.get("title", "")) for a in old_actions[:3]]

    has_issues = critical_pods > 0 or total_restarts > 0 or any(
        p.get("restarts", 0) > 0 or p.get("severity") in ("critical", "high") for p in pods
    )

    blocks = []

    # ━━ 1. SEARCH SCOPE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Use _meta from intent if available for accurate namespace display
    meta = result.get("_meta", {})
    if not namespaces or namespaces == ["all"]:
        meta_ns = meta.get("namespaces", ["all"])
        if meta_ns and meta_ns != ["all"]:
            namespaces = meta_ns

    ns_label = ", ".join(f"`{ns}`" for ns in namespaces) if namespaces and namespaces != ["all"] else "`all`"
    # Show targeted pod if available
    meta_pod = meta.get("pod_name")
    pod_label = f"  |  *pod:* `{meta_pod}`" if meta_pod else ""

    tool_names = []
    if tools:
        seen = set()
        for t in tools:
            name = t.get("name", "")
            label = {
                "query_prometheus": "Prometheus",
                "query_loki_logs": "Loki",
                "list_alert_rules": "Alerts",
                "search_dashboards": "Dashboards",
                "list_datasources": "Datasources",
            }.get(name, name)
            if label not in seen:
                tool_names.append(label)
                seen.add(label)

    source_label = " → ".join(tool_names[:4]) if tool_names else "Grafana MCP"
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"🔍 *Searching:* {ns_label}{pod_label}  |  *via:* {source_label}"}
    })
    blocks.append({"type": "divider"})

    # ━━ 2. STAT CARDS (horizontal, multi-type aware) ━━━━━━━━━━━━━━━━━━━━━━━
    # Detect ALL investigation types mentioned in the query
    query_lower = query.lower() if query else ""
    detected_types = []
    if any(w in query_lower for w in ["restart", "crash", "crashloop"]):
        detected_types.append("restarts")
    if any(w in query_lower for w in ["cpu", "throttl"]):
        detected_types.append("cpu")
    if any(w in query_lower for w in ["memory", "oom", "mem"]):
        detected_types.append("memory")
    if any(w in query_lower for w in ["log", "error", "stderr"]):
        detected_types.append("logs")
    if not detected_types:
        # Fall back to meta investigation_type or general
        meta_type = meta.get("investigation_type", "general")
        detected_types = [meta_type] if meta_type != "general" else ["general"]

    pods_with_issues = sum(1 for p in pods if p.get("restarts", 0) > 0 or p.get("severity") in ("critical", "high"))
    total_metric = dashboard.get("total_restarts", 0)

    # Build dynamic stat cards based on detected types
    type_labels = {
        "restarts": {"s1": "Pods restarted", "s2": "Total restarts today"},
        "cpu":      {"s1": "High CPU pods", "s2": "CPU alerts"},
        "memory":   {"s1": "High memory pods", "s2": "Memory alerts"},
        "oom":      {"s1": "OOMKilled pods", "s2": "OOM events"},
        "logs":     {"s1": "Pods with errors", "s2": "Error count"},
        "general":  {"s1": "Pods with issues", "s2": "Total issues"},
    }

    # For multi-type queries, combine the labels
    if len(detected_types) == 1:
        lbl = type_labels.get(detected_types[0], type_labels["general"])
        s1_label = lbl["s1"]
        s2_label = lbl["s2"]
    else:
        # Combine: "Pods restarted / High CPU"
        s1_parts = [type_labels.get(t, type_labels["general"])["s1"] for t in detected_types[:2]]
        s2_parts = [type_labels.get(t, type_labels["general"])["s2"] for t in detected_types[:2]]
        s1_label = " / ".join(s1_parts)
        s2_label = " / ".join(s2_parts)

    s1_emoji = "🔴" if critical_pods > 0 else "🟡" if pods_with_issues > 0 else "🟢"
    s2_emoji = "🔴" if total_metric > 500 else "🟠" if total_metric > 50 else "🟡" if total_metric > 0 else "🟢"

    blocks.append({
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"{s1_emoji}  *{pods_with_issues}*\n{s1_label}"},
            {"type": "mrkdwn", "text": f"{s2_emoji}  *{total_metric:,}*\n{s2_label}"},
            {"type": "mrkdwn", "text": f"📦  *{len(namespaces)}*\nNamespaces affected"},
        ]
    })
    blocks.append({"type": "divider"})

    # ━━ 3. TECHNICAL FINDINGS (blockquote box) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if technical_findings:
        # Wrap in blockquote for visual box effect
        quoted = "\n".join(f"> {line}" for line in technical_findings[:1500].split("\n"))
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"📊 *Technical Findings*\n{quoted}"}
        })
        blocks.append({"type": "divider"})

    # ━━ 4. THEORETICAL ANALYSIS (blockquote box) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if theoretical_analysis:
        quoted = "\n".join(f"> {line}" for line in theoretical_analysis[:1500].split("\n"))
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"📖 *Theoretical Analysis*\n{quoted}"}
        })
        blocks.append({"type": "divider"})

    # ━━ 5. POD TABLE (only if issues exist) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    restarted_pods = [p for p in pods if p.get("restarts", 0) > 0]
    if restarted_pods:
        sorted_pods = sorted(restarted_pods, key=lambda p: p.get("restarts", 0), reverse=True)
        max_restarts = sorted_pods[0].get("restarts", 1) or 1

        # Header row
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "📋 *Pod Status Table*"}
        })
        # Column headers as context
        blocks.append({
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "*Pod*"},
                {"type": "mrkdwn", "text": "*Namespace*"},
                {"type": "mrkdwn", "text": "*Restarts*"},
                {"type": "mrkdwn", "text": "*Category*"},
            ]
        })

        for pod in sorted_pods[:10]:
            name = pod.get("name", "unknown")
            ns = pod.get("namespace", "?")
            restarts = pod.get("restarts", 0)
            category = pod.get("category", "")

            if restarts > 50: badge = "🔴"
            elif restarts > 10: badge = "🟠"
            else: badge = "🟡"

            bar_len = min(10, max(1, int(10 * restarts / max_restarts)))
            bar = "█" * bar_len + "░" * (10 - bar_len)

            cat_icon = {"OOMKilled": "💀", "CrashLoopBackOff": "🔄", "Error exit": "❌",
                        "High CPU": "🔥", "High Memory": "🧠", "High restarts": "🟠", "Healthy": "✅"}.get(category, "⚪")

            blocks.append({
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"{badge} *{name}*\n`{ns}`"},
                    {"type": "mrkdwn", "text": f"`{restarts}` {bar} {cat_icon} {category}"},
                ]
            })

        blocks.append({"type": "divider"})
    elif not has_issues:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "✅ *All pods healthy* — no restarts or issues detected"}
        })
        blocks.append({"type": "divider"})

    # ━━ 6. RCA ANALYSIS (2-column key-value — only if issues exist) ━━━━━━━━━
    if analysis and isinstance(analysis, dict) and has_issues:
        what = analysis.get("what_failed", "N/A")
        how = analysis.get("how_it_failed", "N/A")
        where = analysis.get("where_it_failed", "N/A")
        why = analysis.get("why_it_failed", "N/A")
        when = analysis.get("when_it_failed", "N/A")
        fix = analysis.get("how_to_fix", "N/A")

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "🔎 *Root Cause Analysis*"}
        })
        # Row 1: What + How (horizontal)
        blocks.append({
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*❓ What failed*\n> {what}"},
                {"type": "mrkdwn", "text": f"*⚡ How it failed*\n> {how}"},
            ]
        })
        # Row 2: Where + Why (horizontal)
        blocks.append({
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*📍 Where*\n> {where}"},
                {"type": "mrkdwn", "text": f"*🔬 Why*\n> {why}"},
            ]
        })
        # Row 3: When + Fix (horizontal)
        blocks.append({
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*🕐 When*\n> {when}"},
                {"type": "mrkdwn", "text": f"*🛠️ How to fix*\n> {fix}"},
            ]
        })
        blocks.append({"type": "divider"})

    # ━━ 7. CONCLUSION (outer box) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if conclusion:
        quoted = "\n".join(f"> {line}" for line in conclusion[:2000].split("\n"))
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"💬 *Summary*\n{quoted}"}
        })
        blocks.append({"type": "divider"})

    # ━━ 8. RECOMMENDED ACTIONS (horizontal buttons — imperative fixes) ━━━━━━━
    if recommended_actions and has_issues:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "🎯 *Recommended Actions*"}
        })
        action_elements = []
        for i, action in enumerate(recommended_actions[:3]):
            a_text = str(action)[:70]
            action_elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": f"⚡ {a_text}"[:75], "emoji": True},
                "style": "primary",
                "value": json.dumps({"query": a_text, "service": namespaces[0] if namespaces else "all"}),
                "action_id": f"recommended_action_{i}"
            })
        if action_elements:
            blocks.append({"type": "actions", "elements": action_elements})
        blocks.append({"type": "divider"})

    # ━━ 9. FOLLOW-UP QUESTIONS (horizontal buttons — investigative) ━━━━━━━━━
    if follow_up_questions:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "❓ *Related Questions*"}
        })
        q_elements = []
        # Offset action_ids to avoid conflict with recommended actions
        offset = len(recommended_actions[:3]) if recommended_actions else 0
        for i, q in enumerate(follow_up_questions[:3]):
            q_text = str(q)[:70]
            action_id = f"recommended_action_{offset + i}"
            q_elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": f"💡 {q_text}"[:75], "emoji": True},
                "value": json.dumps({"query": q_text, "service": namespaces[0] if namespaces else "all"}),
                "action_id": action_id
            })
        if q_elements:
            blocks.append({"type": "actions", "elements": q_elements})

    # ━━ Footer ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if tools:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"🔧 _{len(tools)} MCP tool calls_  •  _Datasource: {namespaces[0] if namespaces else 'auto'}_"}]
        })

    # Slack blocks limit is 50; trim if needed
    if len(blocks) > 48:
        blocks = blocks[:47]
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "⚠️ _Output truncated to fit Slack limits_"}]
        })

    return blocks


def _run_investigation(say, query, service, thread_ts, alert_context=None):
    """Run the investigation in a background thread and post dashboard."""
    try:
        result = run_llm_orchestrated_query(user_query=query, service=service, alert_context=alert_context)
        blocks = _format_dashboard_blocks(result, query)
        say(blocks=blocks, thread_ts=thread_ts)
    except Exception as e:
        logger.error(f"Investigation failed: {e}")
        import traceback
        traceback.print_exc()
        say(text=f"❌ Investigation failed: {str(e)[:500]}", thread_ts=thread_ts)


# ─── Event Handlers ──────────────────────────────────────────────────────────

@app.event("app_mention")
def handle_mention(event, say):
    """Handle @SRE-Agent mentions in any channel."""
    text = event.get("text", "")
    thread_ts = event.get("ts")
    channel = event.get("channel", "")

    # Debug: log the raw event text so we can see what Slack actually sends
    logger.info(f"RAW EVENT TEXT ({len(text)} chars): {repr(text[:500])}")
    print(f"[Agent] RAW EVENT TEXT ({len(text)} chars): {repr(text[:500])}")

    # Remove all <@USERID> bot mention tags to get clean query text
    import re as _re
    query = _re.sub(r'<@[\w]+>', '', text).strip()

    if not query:
        say(text="👋 I'm the SRE Agent! Ask me about your K8s cluster health, pod restarts, logs, etc.\n"
                 "Example: `@SRE-Agent check all namespaces for pod restarts`\n"
                 "You can also paste a Grafana alert payload and I'll investigate it.",
            thread_ts=thread_ts)
        return

    # Try to parse alert context from the FULL event text first
    alert_context = _parse_alert_from_text(text)

    # If alert context is incomplete (missing namespace), try reading the thread parent message
    # The user may have pasted the alert in one message and @mentioned in a reply
    if (not alert_context or not alert_context.get("namespace")) and event.get("thread_ts"):
        parent_ts = event.get("thread_ts")
        try:
            result = app.client.conversations_replies(
                channel=channel,
                ts=parent_ts,
                limit=5,
                inclusive=True
            )
            messages = result.get("messages", [])
            # Combine all thread messages to find alert context
            thread_text = "\n".join(m.get("text", "") for m in messages)
            logger.info(f"THREAD TEXT ({len(thread_text)} chars): {repr(thread_text[:500])}")
            print(f"[Agent] THREAD TEXT ({len(thread_text)} chars): {repr(thread_text[:500])}")
            thread_alert = _parse_alert_from_text(thread_text)
            if thread_alert:
                # Merge: thread context fills in missing fields
                if alert_context:
                    for k, v in thread_alert.items():
                        if k not in alert_context:
                            alert_context[k] = v
                else:
                    alert_context = thread_alert
        except Exception as e:
            logger.warning(f"Failed to read thread context: {e}")
            print(f"[Agent] Failed to read thread context: {e}")

    # Extract namespace: from alert context, from query text, or default to "all"
    service = "all"
    if alert_context and alert_context.get("namespace"):
        service = alert_context["namespace"]
    else:
        # Check if a namespace is explicitly mentioned in the query
        import re
        ns_match = re.search(r'(?:namespace|ns)[:\s=]+["\'"]?([a-z][a-z0-9-]+)["\'"]?', query.lower())
        if ns_match:
            candidate = ns_match.group(1)
            # Reject Jinja/Go template fragments
            if not any(t in candidate for t in ('}}/','}}','{{','$labels','$value')):
                service = candidate
        if service == "all":
            # Check for common namespace names in the query (word-boundary match)
            common_ns = [
                "kube-system", "ingress-nginx", "cert-manager", "velero",
                "awx", "observability", "observe", "platform", "monitoring",
                "argocd", "istio-system", "logging", "default",
            ]
            for ns in common_ns:
                # Use word-boundary to avoid matching substrings in URLs
                pattern = r'(?:^|\s|[,;:])' + re.escape(ns) + r'(?:$|\s|[,;:])'
                if re.search(pattern, query.lower()):
                    service = ns
                    break

    scope_desc = f"*{service}* namespace" if service != "all" else "*all namespaces*"
    pod_desc = f" (pod: `{alert_context['pod']}`)" if alert_context and alert_context.get("pod") else ""
    say(text=f"🔍 Investigating {scope_desc}{pod_desc}...\n_This may take 1-3 minutes._", thread_ts=thread_ts)

    t = threading.Thread(
        target=_run_investigation,
        args=(say, query, service, thread_ts),
        kwargs={"alert_context": alert_context},
        daemon=True
    )
    t.start()


@app.event("message")
def handle_message(event, say):
    """Acknowledge regular messages (required to avoid unhandled event warnings)."""
    pass


# ─── Interactive Action Handlers ─────────────────────────────────────────────

def _handle_action_click(ack, body, say, action_index):
    """Generic handler for recommended action button clicks."""
    ack()

    action = body.get("actions", [{}])[0]
    value = action.get("value", "{}")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        payload = {"query": value, "service": "all"}

    query = payload.get("query", "investigate")
    service = payload.get("service", "all")

    # Get the thread context
    channel = body.get("channel", {}).get("id", "")
    thread_ts = body.get("message", {}).get("ts", "")
    user = body.get("user", {}).get("id", "")

    def _say(**kwargs):
        app.client.chat_postMessage(channel=channel, **kwargs)

    _say(text=f"🔍 <@{user}> triggered: *{query}*\n_Running follow-up investigation..._", thread_ts=thread_ts)

    t = threading.Thread(
        target=_run_investigation,
        args=(_say, query, service, thread_ts),
        daemon=True
    )
    t.start()


# Register handlers for action buttons 0-3
@app.action("recommended_action_0")
def handle_action_0(ack, body, say):
    _handle_action_click(ack, body, say, 0)

@app.action("recommended_action_1")
def handle_action_1(ack, body, say):
    _handle_action_click(ack, body, say, 1)

@app.action("recommended_action_2")
def handle_action_2(ack, body, say):
    _handle_action_click(ack, body, say, 2)

@app.action("recommended_action_3")
def handle_action_3(ack, body, say):
    _handle_action_click(ack, body, say, 3)

@app.action("recommended_action_4")
def handle_action_4(ack, body, say):
    _handle_action_click(ack, body, say, 4)

@app.action("recommended_action_5")
def handle_action_5(ack, body, say):
    _handle_action_click(ack, body, say, 5)


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("⚡ SRE Grafana Agent — Slack Bot (Dashboard Mode)")
    print("=" * 55)
    print(f"   Grafana: {CONFIG.get('GRAFANA_URL', 'not set')}")
    print(f"   Model:   {CONFIG.get('OPENROUTER_MODEL', 'not set')}")
    print(f"   Mode:    Socket Mode (single connection)")
    print("=" * 55)
    try:
        handler = SocketModeHandler(app, SLACK_APP_TOKEN)
        handler.client.num_connections = 1
        logger.info("Connecting to Slack via Socket Mode...")
        handler.start()
    except Exception as e:
        logger.error(f"FATAL: {e}")
        import traceback
        traceback.print_exc()
