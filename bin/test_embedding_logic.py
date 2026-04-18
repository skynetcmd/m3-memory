import asyncio
import os
import sys

# Hack to add bin to path
sys.path.insert(0, os.path.join(os.getcwd(), "bin"))

from m3_sdk import M3Context
from memory_core import _embed


async def main():
    ctx = M3Context()
    print("LM_API_TOKEN (obfuscated):", "*" * len(ctx.get_secret("LM_API_TOKEN") or ""))
    vec, model = await _embed("hello world")
    print("Vector len:", len(vec) if vec else None, "Model:", model)

asyncio.run(main())
