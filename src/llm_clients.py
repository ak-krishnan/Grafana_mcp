"""LLM client stubs. Real calls are placeholders; simulation mode uses deterministic agent.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
import requests
from config import CONFIG


class LLMClient:
    def create_chat_completion(self, model: str, messages: list, tools: list = None, **kwargs):
        raise NotImplementedError()


class OpenRouterClient(LLMClient):
    def __init__(self, api_key=None, base_url=None):
        self.api_key = api_key or CONFIG.get("OPENROUTER_API_KEY")
        self.base_url = base_url or CONFIG.get("OPENROUTER_BASE_URL")
        self.simulation = CONFIG.get("SIMULATION_MODE")
        print(f"[LLM Client] SIMULATION_MODE={self.simulation}, model will use: {'MOCK DATA' if self.simulation else 'REAL LLM'}")

    def create_chat_completion(self, model: str, messages: list, tools: list = None, **kwargs):
        if self.simulation:
            print(f"[LLM] Using SIMULATION MODE (mock response)")
            return self._simulation_response(messages)

        # ── Real LLM call (Ollama / OpenRouter compatible) ──
        url = f"{self.base_url}/chat/completions"
        
        # Clean messages for Ollama compatibility
        clean_messages = []
        for m in messages:
            msg = {}
            if isinstance(m, dict):
                msg = dict(m)
            else:
                # Handle objects (e.g. from previous LLM responses stored directly)
                msg = {"role": getattr(m, "role", "user"), "content": getattr(m, "content", str(m))}
            
            # Ensure content is always a string
            if msg.get("content") is None:
                msg["content"] = ""
            
            # Ollama requires tool_call_id on tool messages
            if msg.get("role") == "tool" and not msg.get("tool_call_id"):
                msg["tool_call_id"] = "call_0"
                
            clean_messages.append(msg)
        
        payload = {"model": model, "messages": clean_messages}
        
        # IMPORTANT: Don't send tools to Qwen - it doesn't support OpenAI tools format
        # Just let the system prompt and messages guide the analysis
        
        headers = {
            "Content-Type": "application/json",
        }
        # Only add auth header for non-Ollama endpoints
        if self.api_key and self.api_key != "ollama":
            headers["Authorization"] = f"Bearer {self.api_key}"
        
        print(f"[LLM] Calling {url} with model={model}, {len(clean_messages)} messages, {len(tools or [])} tools")
        print(f"[LLM] Full payload keys: {payload.keys()}")
        print(f"[LLM] Payload: {json.dumps(payload, indent=2, default=str)[:500]}...")
        
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=300)
            r.raise_for_status()
            response = r.json()
            
            # Normalize Ollama response to OpenAI format if needed
            if "choices" not in response and "message" in response:
                response = {"choices": [{"message": response["message"]}]}
            
            msg_obj = response.get("choices", [{}])[0].get("message", {})
            
            # Log what we got back
            has_tools = bool(msg_obj.get("tool_calls"))
            content_preview = (msg_obj.get("content") or "")[:100]
            print(f"[LLM] Response: has_tool_calls={has_tools}, content_preview='{content_preview}'")
            
            return response
            
        except requests.exceptions.Timeout:
            print("[LLM] ERROR: Request timed out after 300s")
            return {"choices": [{"message": {"content": '{"rca": {"what_failed": "LLM request timed out", "root_cause": ["Model inference took too long"]}}'}}]}
        except requests.exceptions.RequestException as e:
            error_body = None
            if hasattr(e.response, 'text'):
                error_body = e.response.text[:500]
            print(f"[LLM] ERROR: Request failed: {e}")
            if error_body:
                print(f"[LLM] Response body: {error_body}")
            return {"choices": [{"message": {"content": f'{{"rca": {{"what_failed": "LLM request failed", "root_cause": ["{str(e)}"]}}}}'}}]}

    def _simulation_response(self, messages):
        """Simulation-mode behavior: return structured tool call plan first,
        then expect agent to call tools and re-invoke with tool outputs to get final answer."""
        joined = "\n".join([m.get("content", "") for m in messages if m.get("role") in ("user", "assistant", "tool")])
        if "TOOL_RESULTS:" in joined or any(m.get("role") == "tool" for m in messages):
            final = {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "## Root Cause Analysis — (simulated Qwen3)\n"
                                "What failed: payment-service (DB connection pool exhaustion)\n"
                                "When: 2026-05-13T01:58:03Z\n"
                                "Root cause: batch-job-runner opened many DB connections, exhausting pool\n"
                                "Evidence: logs, metrics, alerts (attached)\n"
                                "Impact: ~1847 failed requests over ~7 minutes\n"
                                "Immediate fix: increase DB pool or limit batch job concurrency\n"
                                "Long-term: schedule batch jobs off-peak; add PgBouncer\n"
                            )
                        }
                    }
                ]
            }
            return final

        # Otherwise return a plan to call tools
        tool_calls = [
            {
                "id": "tc-1",
                "function": {
                    "name": "list_firing_alerts",
                    "arguments": "{\"service\": \"payment-service\"}"
                }
            },
            {
                "id": "tc-2",
                "function": {
                    "name": "run_sift_investigation",
                    "arguments": "{\"service\": \"payment-service\", \"time_range\": \"last_2h\"}"
                }
            },
            {
                "id": "tc-3",
                "function": {
                    "name": "query_loki_logs",
                    "arguments": "{\"service\": \"payment-service\", \"time_range\": \"last_2h\", \"level\": \"ERROR\"}"
                }
            }
        ]
        return {"choices": [{"message": {"content": "CALL_TOOLS", "tool_calls": tool_calls}}]}


class AnthropicClient(LLMClient):
    def __init__(self, api_key=None):
        self.api_key = api_key or CONFIG.get("ANTHROPIC_API_KEY")
        self.simulation = CONFIG.get("SIMULATION_MODE")

    def create_chat_completion(self, model: str, messages: list, tools: list = None, **kwargs):
        # Simulation-mode behavior: return structured tool call plan first,
        # then expect agent to call tools and re-invoke with tool outputs to get final answer.
        if self.simulation:
            # If messages already include tool results, return a final RCA text
            joined = "\n".join([m.get("content", "") for m in messages if m.get("role") in ("user", "assistant", "tool")])
            if "TOOL_RESULTS:" in joined:
                final = {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "## Root Cause Analysis — (simulated)\n"
                                    "What failed: payment-service (DB connection pool exhaustion)\n"
                                    "When: 2026-05-13T01:58:03Z\n"
                                    "Root cause: batch-job-runner opened many DB connections, exhausting pool\n"
                                    "Evidence: logs, metrics, alerts (attached)\n"
                                    "Impact: ~1847 failed requests over ~7 minutes\n"
                                    "Immediate fix: increase DB pool or limit batch job concurrency\n"
                                    "Long-term: schedule batch jobs off-peak; add PgBouncer\n"
                                )
                            }
                        }
                    ]
                }
                return final

            # Otherwise return a plan to call tools
            tool_calls = [
                {
                    "id": "tc-1",
                    "function": {
                        "name": "list_firing_alerts",
                        "arguments": "{\"service\": \"payment-service\"}"
                    }
                },
                {
                    "id": "tc-2",
                    "function": {
                        "name": "run_sift_investigation",
                        "arguments": "{\"service\": \"payment-service\", \"time_range\": \"last_2h\"}"
                    }
                },
                {
                    "id": "tc-3",
                    "function": {
                        "name": "query_loki_logs",
                        "arguments": "{\"service\": \"payment-service\", \"time_range\": \"last_2h\", \"level\": \"ERROR\"}"
                    }
                }
            ]
            return {"choices": [{"message": {"content": "CALL_TOOLS", "tool_calls": tool_calls}}]}

        # Non-simulated: placeholder for real Anthropic HTTP API usage
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")
        # Implement real Anthropic API calls here if desired.
        raise NotImplementedError("Anthropic client integration not implemented (non-simulated)")
