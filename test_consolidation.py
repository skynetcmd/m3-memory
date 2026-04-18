
import asyncio
import uuid
import sys
import os
from pathlib import Path

# Add bin to path
sys.path.insert(0, str(Path(__file__).parent / "bin"))

from memory_bridge import memory_write, memory_consolidate

async def main():
    CONS_AGENT = f"cons_test_{uuid.uuid4().hex[:4]}"
    print(f"Agent: {CONS_AGENT}")
    for i in range(5):
        res = await memory_write(type="note", content=f"Consolidation note {i}", title=f"Note {i}", agent_id=CONS_AGENT, embed=False)
        print(f"Write {i}: {res}")
    
    cons_res = await memory_consolidate(type_filter="note", agent_filter=CONS_AGENT, threshold=3)
    print(f"Consolidate result: {cons_res}")

if __name__ == "__main__":
    asyncio.run(main())
