# 📖 Engineering Journal: Building the Autonomous SRE Agent

**Objective**: Connect a local LLM to a live Grafana observability stack (Prometheus and Loki) via the Model Context Protocol (MCP) to automate Root Cause Analysis (RCA).

---

## 1. What We Built & How It Works
We successfully built a 100% local, autonomous Site Reliability Engineering (SRE) assistant. 

**How it works (The Pipeline):**
1. **Input**: You type a query into the Flask-based web UI (e.g., "Why did the payment service spike at 2AM?").
2. **Schema Discovery**: The Python backend (`mcp_client.py`) uses the official Anthropic MCP SDK to connect to the external `@leval/mcp-grafana` server via `stdio`. It retrieves the exact tools available (Loki queries, Prometheus metrics, etc).
3. **LLM Orchestration**: The backend sends your query and the tool schemas to your local Ollama LLM (`qwen2.5:7b`).
4. **Tool Execution**: The LLM decides what data it needs and returns a JSON tool-call request. The backend intercepts this, forwards the query to the Grafana MCP server, fetches live data from Docker, and feeds it back to the LLM.
5. **Final RCA**: The LLM synthesizes the live metrics and generates a final, structured JSON Root Cause Analysis, which is rendered beautifully in the dark-mode UI.

---

## 2. Step-by-Step Journey (What We Did & How We Did It)

### Phase 1: Infrastructure & The MCP Bridge
- **The Question**: *“I want to connect the grafana mcp servers and its tools like loki, metrics and sifts to the llm... what info do u need from mr to complete this”*
- **The Solution**: We started by getting your local Grafana URL (`http://localhost:3000`) and your Grafana Service Account token. We created a `.env` file to securely store these credentials alongside the `USE_MCP_SERVER=1` flag.
- **The Implementation**: We completely rewrote `src/mcp_client.py` to use an asynchronous background thread. This allowed the synchronous Flask app to talk flawlessly with the async MCP standard input/output (stdio) streams.

### Phase 2: Fixing the "Noisy" JSON-RPC Connection
- **The Blocker**: When we first hooked up the MCP server, the terminal started throwing `Pydantic` validation errors about missing JSON-RPC fields.
- **The Fix**: We realized the external Grafana MCP server was logging "Pino" debug messages directly to standard output, which was mixing with the JSON-RPC communication and corrupting it. We fixed this by dynamically injecting `PINO_LOG_LEVEL=silent` and `LOG_LEVEL=fatal` into the server's environment during initialization.

### Phase 3: The UI Transformation
- **The Question**: *“I need a ui where i can perform this and see”*
- **The Solution**: We threw away the basic HTML template and completely redesigned `index.html`. We built a premium, dark-mode chat interface featuring:
  - Custom chat bubbles for the "Engineer" and "SRE Assistant".
  - Accordion-style boxes to render the **MCP TOOL CALL** (the exact JSON sent to Grafana) and the **TOOL RESPONSE** (the raw metrics returned).
- **The Missing Logs**: *“Where is the json logs”*. At first, the UI was missing the tool calls because the backend wasn't returning them. We modified `agent_llm.py` to capture `executed_tools.append(res)` and explicitly pass them to the UI template.

### Phase 4: Taming the Local LLM (The 14B vs 7B Saga)
- **The Question**: *“I need to connect with the qwen 3 14b model which is local model”*
- **The Blocker**: You successfully downloaded the massive 9GB `qwen2.5:14b` model via Ollama. However, when we hit "Analyze", the Python script crashed with `requests.exceptions.ReadTimeout`.
- **The Fix**: 
  1. First, we realized the MacBook Air was struggling to load the massive 9GB model into memory, causing the initial response to take longer than the 30-second default HTTP limit. We increased the limit in `llm_clients.py` to `timeout=180`, and then removed it entirely (`timeout=None`).
  2. Even without the timeout, the massive swap-memory usage on the Mac made inference unbearably slow.
  3. We ultimately made the strategic decision to switch to the highly optimized `qwen2.5:7b` model. We updated the `.env` file, triggered `ollama pull qwen2.5:7b` in the background, and achieved snappy, reliable performance without melting the laptop!

---

## 3. Final Reflection
We successfully transitioned a mock-data SRE agent into a **fully autonomous, live-data observability AI**. We overcame complex networking issues, JSON-RPC stream corruption, UI data-binding gaps, and heavy local-LLM hardware constraints. You now possess a highly sophisticated, enterprise-grade architecture running entirely on your local machine.
