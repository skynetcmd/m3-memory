# m3-langchain

A recognizable, `databricks-langchain`-style alias for **m3-memory**'s LangChain
/ LangGraph integration. It contains no code of its own — it re-exports
[`m3_memory.langchain`](https://github.com/skynetcmd/m3-memory/blob/main/docs/integrations/LANGCHAIN.md)
and depends on `m3-memory[langchain]`, so `pip install m3-langchain` gives you the
full, local-first m3 memory layer under a discoverable name.

```bash
pip install m3-langchain
```

```python
from m3_langchain import Memory, M3Store, M3Saver
# Memory  → drop-in Mem0 replacement (one-line import swap)
# M3Store → LangGraph BaseStore / backs LangMem
# M3Saver → LangGraph checkpointer (pause / resume / time-travel)
```

These are the same objects as `from m3_memory.langchain import ...`. New code can
import from either path; this package exists for name-discoverability.

**Full integration guide, examples, and the complete surface list** (retriever,
chat history, LCEL `MemoryWrite`/`MemoryRetrieve`, the m3-native extras
`.supersede`/`as_of=`/`.forget`/`.related`) live in the main repo:
[skynetcmd/m3-memory](https://github.com/skynetcmd/m3-memory).

Apache-2.0.
