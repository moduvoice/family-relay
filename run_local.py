"""Run the MCP server (:8790) and the simulator (:8791) in one process.

    python run_local.py            # then open http://127.0.0.1:8791/
"""
from __future__ import annotations

import argparse
import asyncio

import uvicorn


async def main(host: str, mcp_port: int, sim_port: int) -> None:
    from relay.mcp_app import app as mcp_app
    from simulator.backend import app as sim_app

    await asyncio.gather(
        uvicorn.Server(uvicorn.Config(mcp_app, host=host, port=mcp_port, log_level="info")).serve(),
        uvicorn.Server(uvicorn.Config(sim_app, host=host, port=sim_port, log_level="info")).serve(),
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--mcp-port", type=int, default=8790)
    ap.add_argument("--sim-port", type=int, default=8791)
    a = ap.parse_args()
    asyncio.run(main(a.host, a.mcp_port, a.sim_port))
