"""Run a list of tool calls against the MCP server in one session and save every preview.

    uv run python scripts/mcp_session.py plan.json --out previews/

plan.json is a list of {"tool": name, "args": {...}} (or {"tool": name, ...args}). The server
keeps selections, the edit stack and recordings for the length of the session, so a plan can
gaussify an image, select regions, edit, record and write the GIF in one go. Text results are
printed; image results are written to <out>/<NN>_<tool>.png.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(plan: list[dict], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    server = StdioServerParameters(command=sys.executable, args=["-m", "mirage_gaussifier_mcp.server"])
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        for i, step in enumerate(plan):
            tool = step["tool"]
            args = step.get("args", {k: v for k, v in step.items() if k != "tool"})
            result = await session.call_tool(tool, args)
            print(f"[{i:02d}] {tool} {json.dumps(args)}")
            for item in result.content:
                if item.type == "text":
                    print("     " + item.text.replace("\n", "\n     "))
                elif item.type == "image":
                    path = out / f"{i:02d}_{tool}.png"
                    path.write_bytes(base64.b64decode(item.data))
                    print(f"     image -> {path}")
            if result.isError:
                print("     ERROR, stopping")
                sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", type=Path)
    ap.add_argument("--out", type=Path, default=Path("previews"))
    a = ap.parse_args()
    asyncio.run(run(json.loads(a.plan.read_text()), a.out))


if __name__ == "__main__":
    main()
