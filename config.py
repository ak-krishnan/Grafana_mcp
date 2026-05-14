import os
from dotenv import load_dotenv

load_dotenv()

# Put real API keys into environment variables. Defaults are placeholders.
CONFIG = {
    "OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", ""),
    "OPENROUTER_BASE_URL": os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    "OPENROUTER_MODEL": os.environ.get("OPENROUTER_MODEL", "qwen2.5:14b"),
    "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
    "PROMETHEUS_URL": os.environ.get("PROMETHEUS_URL", "http://localhost:9090"),
    "SIMULATION_MODE": os.environ.get("SIMULATION_MODE", "1") == "1",
    "USE_MCP_SERVER": os.environ.get("USE_MCP_SERVER", "0") == "1",
    "MCP_SERVER_COMMAND": os.environ.get("MCP_SERVER_COMMAND", "npx -y @leval/mcp-grafana"),
    "GRAFANA_URL": os.environ.get("GRAFANA_URL", "http://localhost:3000"),
    "GRAFANA_SERVICE_ACCOUNT_TOKEN": os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", ""),
}
