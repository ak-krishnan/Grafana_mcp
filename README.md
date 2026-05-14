# 🤖 LLM-Driven Grafana SRE Agent

This project is an autonomous, local, LLM-driven Site Reliability Engineering (SRE) Assistant. It utilizes the **Model Context Protocol (MCP)** to bridge a local LLM (like Qwen via Ollama) directly to your live Grafana, Prometheus, and Loki infrastructure. 

The agent can dynamically execute queries, fetch real-time logs, analyze metrics, and generate beautifully formatted Root Cause Analysis (RCA) reports—all exposed through a premium Dark Mode Chat UI.

---

## 🏗️ Architecture

The system is built with a decoupled architecture ensuring that your LLM has live access to observability data without relying on hardcoded prompts:

1. **The UI (Flask + HTML/CSS)**: A modern chat interface that renders the user query, intermediate MCP tool execution logs, and the final RCA.
2. **The Orchestrator (`agent_llm.py`)**: The brain of the operation. It receives user queries, fetches the available tool schemas from the MCP server, and coordinates the multi-turn conversation with the local LLM.
3. **The LLM Client (`llm_clients.py`)**: Connects via an OpenAI-compatible API to your local LLM (e.g., Ollama running `qwen2.5:7b`). It features an extended timeout (`timeout=None`) to gracefully handle heavy model loads on standard laptops.
4. **The MCP Bridge (`mcp_client.py`)**: Uses the Anthropic Python SDK (`mcp.client.stdio`) to spin up and communicate with an external `@leval/mcp-grafana` server via JSON-RPC. It passes the LLM's requests straight into your observability stack.
5. **Observability Stack (`docker-compose.yml`)**: A local Docker cluster running Grafana, Prometheus, and Loki.

---

## 🚀 Setup & Execution

### 1. Start the Observability Stack
Spin up your local Prometheus, Loki, and Grafana instances using Docker:
```bash
cd /path/to/grafana
docker compose up -d
```

### 2. Start your Local LLM
Ensure Ollama is installed and running on your machine. Pull and start the recommended 7B model (which easily runs on a MacBook without swapping to disk):
```bash
ollama run qwen2.5:7b
```
*(Leave this running in the background).*

### 3. Configure the Environment
Ensure your `.env` file in the `grafana_agent` folder looks like this:
```env
USE_MCP_SERVER=1
MCP_SERVER_COMMAND=npx -y @leval/mcp-grafana
GRAFANA_URL=http://localhost:3000
GRAFANA_SERVICE_ACCOUNT_TOKEN=your_grafana_token_here

# LLM Configuration
SIMULATION_MODE=0
OPENROUTER_BASE_URL=http://localhost:11434/v1
OPENROUTER_MODEL=qwen2.5:7b
OPENROUTER_API_KEY=ollama
```

### 4. Start the SRE Web UI
Activate your virtual environment and start the Flask web app:
```bash
cd grafana_agent
source ../.venv/bin/activate
python -m src.webapp
```
Open your browser and navigate to `http://127.0.0.1:5000`. 

---

## 🧪 Simulation Mode vs Real Mode

- **Real Mode (`SIMULATION_MODE=0` & `USE_MCP_SERVER=1`)**: The LLM actively thinks, decides which tools to call, executes queries against your Docker containers via the MCP server, and dynamically generates the RCA based on what it finds.
- **Synthetic Fallback (`SIMULATION_MODE=0` & `USE_MCP_SERVER=0`)**: The LLM actively thinks, but instead of hitting the live Docker containers, the backend intercepts the tool calls and feeds it mock observability data from `src/synthetic_data.py`. This is excellent for testing the LLM's reasoning capabilities without needing to generate artificial traffic in Prometheus.
- **Full Simulation (`SIMULATION_MODE=1`)**: The LLM is bypassed entirely. The system immediately spits out a hardcoded tool execution plan and a hardcoded RCA.

---

## 🎨 UI Features

The chat interface is designed to emulate a premium engineering console. When you click **Analyze**, you will see:
1. **User Query Bubble**: Right-aligned, dark teal.
2. **MCP Tool Call Log (⚙️)**: Displays the exact JSON RPC payload the agent requested to run.
3. **Tool Response Log (📊)**: Displays the raw metric arrays, Loki logs, or Alert lists returned from the MCP server.
4. **SRE Assistant RCA (🤖)**: The final synthesized JSON parsed into beautiful `What Failed`, `Root Cause`, `Evidence`, and `Fix` sections.
