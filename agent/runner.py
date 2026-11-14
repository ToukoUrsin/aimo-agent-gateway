"""Starts one of the demo agents and records how it ended.

"llm" is the model-driven agent (the model is run["model"], any provider in llm.py); "claude" is its older name.
"""
import traceback

from . import buyer, rogue, script

AGENTS = {"llm": buyer.run, "claude": buyer.run, "script": script.run, "rogue": rogue.run}


async def start(run: dict, base: str):
    try:
        await AGENTS.get(run["agent"], buyer.run)(run, base)
        run["status"] = "done"
    except Exception as e:
        traceback.print_exc()
        run["status"] = "error"
        run["steps"].append({"t": "", "kind": "error", "title": "Agent crashed", "text": f"{type(e).__name__}: {e}"})
