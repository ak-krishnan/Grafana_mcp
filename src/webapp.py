"""Flask web app for the Grafana SRE Agent.
Serves a premium dashboard UI and handles investigation queries.
"""
import sys
import os
import json
import hashlib
import hmac
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from flask import Flask, render_template, request, jsonify
from src.agent import run_sre_agent
from config import CONFIG

# Initialize Slack client if token is available
SLACK_BOT_TOKEN = CONFIG.get("SLACK_BOT_TOKEN")
if SLACK_BOT_TOKEN:
    from slack_sdk import WebClient
    slack_client = WebClient(token=SLACK_BOT_TOKEN)
else:
    slack_client = None

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), '..', 'templates'))


@app.route('/', methods=['GET'])
def index():
    return render_template('index.html', default_query='Our payment service had a major outage around 2 AM today.')


@app.route('/run', methods=['POST'])
def run():
    query = request.form.get('query')
    service = request.form.get('service') or 'payment-service'
    
    sim_mode = CONFIG.get("SIMULATION_MODE")
    print(f"[WEBAPP] SIMULATION_MODE={sim_mode} (type: {type(sim_mode)})")
    
    if sim_mode:
        # Pure simulation mode - no LLM, no MCP
        print("[WEBAPP] Using agent.py (simulation mode)")
        res = run_sre_agent(query, service=service)
    else:
        # Real mode: LLM + MCP
        print("[WEBAPP] Using agent_llm.py (real mode)")
        from src.agent_llm import run_llm_orchestrated_query
        res = run_llm_orchestrated_query(query, service=service)
        
    return render_template('index.html', result=res, default_query=query, service_name=service)


@app.route('/api/run', methods=['POST'])
def api_run():
    """JSON API endpoint for async frontend calls."""
    data = request.get_json(force=True)
    query = data.get('query', '')
    service = data.get('service', 'payment-service')
    
    if CONFIG.get("SIMULATION_MODE"):
        res = run_sre_agent(query, service=service)
    else:
        from src.agent_llm import run_llm_orchestrated_query
        res = run_llm_orchestrated_query(query, service=service)
    
    return jsonify(res)


@app.route('/api/status', methods=['GET'])
def api_status():
    """Health check - reports MCP connection status."""
    from src.mcp_client import mcp_client
    return jsonify({
        "status": "ok",
        "mcp_connected": mcp_client.use_mcp and mcp_client.session is not None,
        "simulation_mode": CONFIG.get("SIMULATION_MODE"),
        "model": CONFIG.get("OPENROUTER_MODEL"),
        "grafana_url": CONFIG.get("GRAFANA_URL"),
        "tool_count": len(mcp_client.get_tool_schema()) if mcp_client.session else 0,
    })


# ═══════════════════════════════════════════════════════════════
# SLACK INTEGRATION
# ═══════════════════════════════════════════════════════════════

def verify_slack_request(timestamp, signature):
    """Verify Slack request authenticity using signing secret."""
    if abs(time.time() - int(timestamp)) > 300:
        return False
    
    signing_secret = CONFIG.get("SLACK_SIGNING_SECRET", "")
    if not signing_secret:
        return False
    
    base_string = f"v0:{timestamp}:{request.get_data(as_text=True)}"
    my_signature = "v0=" + hmac.new(
        signing_secret.encode(),
        base_string.encode(),
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(my_signature, signature)


@app.route('/slack/events', methods=['POST'])
def slack_events():
    """Handle Slack events (message mentions, app_mention)."""
    timestamp = request.headers.get('X-Slack-Request-Timestamp', '')
    signature = request.headers.get('X-Slack-Signature', '')
    
    # Verify request authenticity
    if not verify_slack_request(timestamp, signature):
        return jsonify({"error": "Unauthorized"}), 401
    
    body = request.get_json()
    
    # Handle URL verification challenge
    if body.get("type") == "url_verification":
        return jsonify({"challenge": body.get("challenge")})
    
    # Handle app mentions
    if body.get("type") == "event_callback":
        event = body.get("event", {})
        
        if event.get("type") == "app_mention":
            user_query = event.get("text", "").replace("<@U", "").split(">", 1)
            if len(user_query) > 1:
                user_query = user_query[1].strip()
            else:
                user_query = event.get("text", "")
            
            channel = event.get("channel")
            thread_ts = event.get("thread_ts") or event.get("ts")
            
            try:
                # Run investigation
                from src.agent_llm import run_llm_orchestrated_query
                res = run_llm_orchestrated_query(user_query, service="payment-service")
                
                # Format for Slack
                slack_message = format_rca_for_slack(res)
                
                # Send to Slack
                if slack_client:
                    try:
                        slack_client.chat_postMessage(
                            channel=channel,
                            text=slack_message,
                            thread_ts=thread_ts
                        )
                        print(f"[SLACK] ✓ Message sent to channel={channel}")
                    except Exception as e:
                        print(f"[SLACK] ✗ Failed to send message: {e}")
                else:
                    print(f"[SLACK] WARNING: slack_client not initialized (no SLACK_BOT_TOKEN)")
            
            except Exception as e:
                print(f"[SLACK] ERROR processing request: {e}")
                if slack_client:
                    slack_client.chat_postMessage(
                        channel=channel,
                        text=f"❌ Error: {str(e)}",
                        thread_ts=thread_ts
                    )
        
        return jsonify({"ok": True})
    
    return jsonify({"ok": True})


def format_rca_for_slack(rca_json):
    """Format RCA result as Slack message blocks."""
    rca = rca_json.get("rca", {})
    
    text = f"""
🔴 *{rca.get('what_failed', 'Unknown')} - Root Cause Analysis*

*How It Failed:*
{rca.get('how_it_failed', 'N/A')}

*Root Cause:*
"""
    for cause in rca.get('root_cause', []):
        text += f"• {cause}\n"
    
    text += f"\n*Impact:*\n{rca.get('impact', 'N/A')}"
    
    text += f"\n\n✅ *Immediate Actions:*\n"
    for action in rca.get('immediate_fix', []):
        text += f"• {action}\n"
    
    return text


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
