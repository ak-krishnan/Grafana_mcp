import json
import os
import asyncio
import threading
from config import CONFIG
from src import tools as local_tools

from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

class MCPClientProxy:
    """
    A bridge to an external MCP server via stdio.
    If USE_MCP_SERVER is 0, it falls back to the native Python simulation tools.
    """
    def __init__(self):
        self.use_mcp = CONFIG["USE_MCP_SERVER"]
        self.mcp_command = CONFIG["MCP_SERVER_COMMAND"]
        
        self.session = None
        self.loop = None
        self.thread = None
        self._tools_cache = None
        
        if self.use_mcp:
            print(f"[MCP Client] Initializing MCP server with command: {self.mcp_command}")
            self.loop = asyncio.new_event_loop()
            self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
            self.thread.start()
            # Wait for initialization to complete (with timeout)
            try:
                asyncio.run_coroutine_threadsafe(self._init_mcp(), self.loop).result(timeout=60)
            except Exception as e:
                print(f"[MCP Client] WARNING: MCP init failed: {e}")
                print("[MCP Client] Falling back to local tools.")
                self.use_mcp = False

    async def _init_mcp(self):
        parts = self.mcp_command.split(" ")
        # Build env: pass Grafana credentials + suppress logging that corrupts stdio JSON-RPC
        custom_env = {
            **os.environ,
            "GRAFANA_URL": CONFIG.get("GRAFANA_URL", "http://localhost:3000"),
            "GRAFANA_SERVICE_ACCOUNT_TOKEN": CONFIG.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", ""),
            # Suppress log messages that corrupt the JSON-RPC stdio channel
            "LOG_LEVEL": "fatal",
            "PINO_LOG_LEVEL": "silent",
            "NODE_ENV": "production",
        }
        server_params = StdioServerParameters(
            command=parts[0],
            args=parts[1:],
            env=custom_env
        )
        self.client_ctx = stdio_client(server_params)
        self.read, self.write = await self.client_ctx.__aenter__()
        self.session_ctx = ClientSession(self.read, self.write)
        self.session = await self.session_ctx.__aenter__()
        await self.session.initialize()
        print("[MCP Client] Successfully connected and initialized MCP session.")
        
        # Cache the tool list at init time to avoid repeated calls
        result = await self.session.list_tools()
        self._tools_cache = []
        for t in result.tools:
            self._tools_cache.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.inputSchema
                }
            })
        tool_names = [t["function"]["name"] for t in self._tools_cache]
        print(f"[MCP Client] Available MCP tools ({len(tool_names)}): {', '.join(tool_names[:15])}{'...' if len(tool_names) > 15 else ''}")

    def get_tool_schema(self):
        if not self.use_mcp:
            return local_tools.TOOLS_SCHEMA
        
        # Return cached tools if available
        if self._tools_cache:
            return self._tools_cache
            
        async def _get():
            result = await self.session.list_tools()
            schemas = []
            for t in result.tools:
                schemas.append({
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.inputSchema
                    }
                })
            return schemas
            
        return asyncio.run_coroutine_threadsafe(_get(), self.loop).result(timeout=30)

    def execute_tool(self, name, args):
        if not self.use_mcp:
            # Fallback to local python tool logic
            if name == "list_firing_alerts": return local_tools.list_firing_alerts(**args)
            elif name == "run_sift_investigation": return local_tools.run_sift_investigation(**args)
            elif name == "query_loki_logs": return local_tools.query_loki_logs(**args)
            elif name == "query_prometheus_metrics": return local_tools.query_prometheus_metrics(**args)
            else: return {"error": f"unknown tool {name}"}

        # Real MCP execution - Log arguments being passed
        print(f"[MCP Client] Executing {name}")
        print(f"[MCP Client] Arguments type: {type(args)}")
        print(f"[MCP Client] Arguments: {json.dumps(args) if args else 'EMPTY'}")
        
        async def _exec():
            try:
                print(f"[MCP Client] Calling session.call_tool({name}, arguments={args})")
                res = await asyncio.wait_for(
                    self.session.call_tool(name, arguments=args),
                    timeout=120
                )
                text_content = [c.text for c in res.content if hasattr(c, "text")]
                # Try to parse JSON responses for richer data
                parsed = []
                for t in text_content:
                    try:
                        parsed.append(json.loads(t))
                    except (json.JSONDecodeError, TypeError):
                        parsed.append(t)
                return {"status": "success", "mcp_data": text_content, "parsed_data": parsed}
            except asyncio.TimeoutError:
                return {"status": "error", "error": f"Tool {name} timed out after 120s"}
            except Exception as e:
                return {"status": "error", "error": str(e)}
                
        return asyncio.run_coroutine_threadsafe(_exec(), self.loop).result(timeout=130)

mcp_client = MCPClientProxy()
