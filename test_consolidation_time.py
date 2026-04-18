
import asyncio
import uuid
import sys
import os
import time
from pathlib import Path

# Add bin to path
sys.path.insert(0, str(Path(__file__).parent / "bin"))

async def main():
    import memory_core
    from memory_bridge import memory_write, memory_consolidate
    
    CONS_AGENT = f"cons_time_test_{uuid.uuid4().hex[:4]}"
    print(f"Agent: {CONS_AGENT}")
    for i in range(5):
        await memory_write(type="note", content=f"Consolidation note {i}", title=f"Note {i}", agent_id=CONS_AGENT, embed=False)
    
    start_time = time.time()
    cons_res = await memory_consolidate(type_filter="note", agent_filter=CONS_AGENT, threshold=3)
    duration = time.time() - start_time
    
    print(f"Consolidate result: {cons_res}")
    print(f"Time taken: {duration:.2f} seconds")

if __name__ == "__main__":
    asyncio.run(main())
