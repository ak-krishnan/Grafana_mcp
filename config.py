import os
from dotenv import load_dotenv

load_dotenv()

# Put real API keys into environment variables. Defaults are placeholders.
CONFIG = {
    "OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "qwen-local"),
    "OPENROUTER_BASE_URL": os.environ.get("OPENROUTER_BASE_URL", "http://157.180.109.167:9005/v1"),
    "OPENROUTER_MODEL": os.environ.get("OPENROUTER_MODEL", "Qwen/Qwen3.6-35B-A3B"),
    "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
    "PROMETHEUS_URL": os.environ.get("PROMETHEUS_URL", "http://localhost:9090"),
    "SIMULATION_MODE": os.environ.get("SIMULATION_MODE", "1") == "1",
    "USE_MCP_SERVER": os.environ.get("USE_MCP_SERVER", "0") == "1",
    "MCP_SERVER_COMMAND": os.environ.get("MCP_SERVER_COMMAND", "npx -y @leval/mcp-grafana"),
    "MCP_API_KEY": os.environ.get("MCP_API_KEY", ""),
    "MCP_SERVER_PORT": os.environ.get("MCP_SERVER_PORT", "3000"),
    "MCP_SERVER_HOST": os.environ.get("MCP_SERVER_HOST", "localhost"),
    "MCP_LOG_LEVEL": os.environ.get("MCP_LOG_LEVEL", "info"),
    "GRAFANA_URL": os.environ.get("GRAFANA_URL", "https://grafana.secureai-meridian.in"),
    "GRAFANA_SERVICE_ACCOUNT_TOKEN": os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", ""),
}
