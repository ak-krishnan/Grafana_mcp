"""Flask web app for the Grafana SRE Agent.
Serves a premium dashboard UI and handles investigation queries.
"""
import sys
import os
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from flask import Flask, render_template, request, jsonify
from src.agent import run_sre_agent
from config import CONFIG

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), '..', 'templates'))


@app.route('/', methods=['GET'])
def index():
    return render_template('index.html', default_query='Our payment service had a major outage around 2 AM today.')


@app.route('/run', methods=['POST'])
def run():
    query = request.form.get('query')
    service = request.form.get('service') or 'payment-service'
    
    if CONFIG.get("SIMULATION_MODE"):
        # Pure simulation mode - no LLM, no MCP
        res = run_sre_agent(query, service=service)
    else:
        # Real mode: LLM + MCP
        from .agent_llm import run_llm_orchestrated_query
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
        from .agent_llm import run_llm_orchestrated_query
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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
