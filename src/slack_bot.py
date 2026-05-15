"""Slack Bot for Grafana SRE Agent.

Listens to @mentions in a Slack channel and routes them through the
MCP → LLM pipeline, posting the RCA back as a formatted Slack message.

Usage:
    python -m src.slack_bot

Requires env vars:
    SLACK_BOT_TOKEN   — Bot User OAuth Token (xoxb-...)
    SLACK_APP_TOKEN   — App-Level Token for Socket Mode (xapp-...)
"""
import os
import sys
import json
import threading
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

# Set up logging — INFO level to reduce noise
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger("sre_slack_bot")

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from src.agent_llm import run_llm_orchestrated_query
from config import CONFIG

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")

if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
    logger.error("Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN in .env")
    sys.exit(1)

logger.info(f"Bot token: {SLACK_BOT_TOKEN[:15]}...")
logger.info(f"App token: {SLACK_APP_TOKEN[:15]}...")

# Create app with single-thread concurrency to avoid BrokenPipe
app = App(token=SLACK_BOT_TOKEN)


def _format_rca_blocks(result, query):
    """Convert RCA dict into Slack Block Kit blocks."""
    rca = result.get("rca", {})
    tools = result.get("tools", [])

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🔭 SRE Agent — Root Cause Analysis", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Query:* _{query}_"}},
        {"type": "divider"},
    ]

    if rca.get("what_failed"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"🔴 *What Failed*\n{rca['what_failed']}"}})
    if rca.get("how_it_failed"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ *How It Failed*\n{rca['how_it_failed']}"}})
    if rca.get("root_cause"):
        causes = "\n".join(f"• {c}" for c in rca["root_cause"])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"🔍 *Root Cause*\n{causes}"}})
    if rca.get("evidence"):
        evidence_lines = []
        for e in rca["evidence"][:5]:
            if isinstance(e, dict):
                evidence_lines.append(f"• `[{e.get('type','')}]` {e.get('source','')} — {e.get('detail','')}")
            else:
                evidence_lines.append(f"• {e}")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "📋 *Evidence*\n" + "\n".join(evidence_lines)}})
    if rca.get("impact"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"⚡ *Impact*\n{rca['impact']}"}})

    fixes = []
    for f in rca.get("immediate_fix", []):
        fixes.append(f"• *Now:* {f}")
    for f in rca.get("long_term_fix", []):
        fixes.append(f"• *Long-term:* {f}")
    if fixes:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "✅ *How To Fix*\n" + "\n".join(fixes)}})

    blocks.append({"type": "divider"})

    if tools:
        tool_summary = ", ".join(f"`{t['name']}`" for t in tools[:6])
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"🔧 MCP Tools used: {tool_summary} | {len(tools)} calls total"}
        ]})

    return blocks


def _run_investigation(client, channel, query, service, thread_ts):
    """Run the investigation in a background thread using client.chat_postMessage."""
    try:
        logger.info(f"Starting investigation: service={service}")
        result = run_llm_orchestrated_query(user_query=query, service=service)
        blocks = _format_rca_blocks(result, query)
        client.chat_postMessage(channel=channel, blocks=blocks, thread_ts=thread_ts)
        logger.info("RCA posted to Slack successfully!")
    except Exception as e:
        logger.error(f"Investigation failed: {e}")
        client.chat_postMessage(
            channel=channel,
            text=f"❌ Investigation failed: {str(e)[:500]}",
            thread_ts=thread_ts
        )


@app.event("app_mention")
def handle_mention(event, say, client):
    """Handle @mentions in any channel."""
    logger.info(f"=== APP_MENTION EVENT RECEIVED ===")
    logger.info(f"Channel: {event.get('channel')}, User: {event.get('user')}")
    logger.info(f"Text: {event.get('text', '')[:200]}")

    text = event.get("text", "")
    channel = event.get("channel")
    thread_ts = event.get("ts")

    # Remove the bot mention from the text (<@U12345> query text)
    parts = text.split(">", 1)
    query = parts[1].strip() if len(parts) > 1 else text

    if not query:
        say(text="👋 I'm the SRE Agent! Ask me about your K8s cluster.\n"
                 "Example: `@Grafana check monitoring namespace for errors`",
            thread_ts=thread_ts)
        return

    # Extract namespace from query
    service = "monitoring"
    for ns in ["monitoring", "platform", "kube-system", "ingress-nginx",
               "velero", "awx", "cert-manager", "default", "control-plane"]:
        if ns in query.lower():
            service = ns
            break

    logger.info(f"Namespace: {service}, Query: {query[:100]}")
    say(text=f"🔍 Investigating *{service}* namespace...\n_This may take 1-3 minutes._",
        thread_ts=thread_ts)

    # Run in background thread to avoid Slack 3-second timeout
    t = threading.Thread(
        target=_run_investigation,
        args=(client, channel, query, service, thread_ts),
        daemon=True
    )
    t.start()


@app.event("message")
def handle_message(event, say):
    """Acknowledge regular messages (required to avoid unhandled event warnings)."""
    pass


if __name__ == "__main__":
    print("=" * 50)
    print("⚡ SRE Grafana Agent — Slack Bot")
    print("=" * 50)
    print(f"   Grafana: {CONFIG.get('GRAFANA_URL', 'not set')}")
    print(f"   Model:   {CONFIG.get('OPENROUTER_MODEL', 'not set')}")
    print(f"   Mode:    Socket Mode (single connection)")
    print("=" * 50)
    try:
        handler = SocketModeHandler(app, SLACK_APP_TOKEN)
        # Force single connection to prevent BrokenPipe race condition
        handler.client.num_connections = 1
        logger.info("Connecting to Slack via Socket Mode...")
        handler.start()
    except Exception as e:
        logger.error(f"FATAL: {e}")
        import traceback
        traceback.print_exc()
