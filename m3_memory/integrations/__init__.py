"""m3-memory integration subpackages.

Importable integrations live here as real subpackages (e.g.
``m3_memory.integrations.langchain``). The vendored Hermes provider under
``hermes/`` ships as package *data*, not an importable package — it's loaded by
path, not imported. Making ``integrations`` a regular package (this file) is what
lets ``find_packages`` discover ``langchain`` so the wheel ships it; without it,
``from m3_memory.langchain import ...`` breaks for installed (non-dev) users.
"""
