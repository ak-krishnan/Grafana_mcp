"""Run a quick CLI test prompt against the local agent (simulation mode).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from src.agent import run_sre_agent
from src.agent_llm import run_llm_orchestrated_query
import json


def cli_simulation():
    q = "Our payment service had a major outage around 2 AM today. What happened, why did it fail, and how do we fix it?"
    out = run_sre_agent(q)
    print("== Direct Agent RCA (simulation) ==")
    print(json.dumps(out, indent=2))


def cli_llm_flow():
    q = "Our payment service had a major outage around 2 AM today. What happened, why did it fail, and how do we fix it?"
    out = run_llm_orchestrated_query(q)
    print("== LLM Orchestrated Flow ==")
    print(json.dumps(out, indent=2))


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else "direct"
    if mode == "llm":
        cli_llm_flow()
    else:
        cli_simulation()
