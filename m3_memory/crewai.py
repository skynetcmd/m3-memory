"""Public re-export shim: ``from m3_memory.crewai import M3StorageBackend``.

The short, memorable path the docs promise, forwarding to the integration package
``m3_memory.integrations.crewai``. ``M3StorageBackend`` is resolved LAZILY (it
imports CrewAI), so a missing/too-old CrewAI fails loud with an actionable
``pip install m3-memory[crewai]`` hint at access time (§3), never here.

    from m3_memory.crewai import M3StorageBackend
    from crewai.memory import Memory
    mem = Memory(storage=M3StorageBackend(user_id="crew-alpha"))
"""

from __future__ import annotations

from typing import Any

# The integration package's __init__ has no eager CrewAI dependency (the whole
# surface is lazy via its __getattr__), so this import is safe on any install
# that ships the payload. An ImportError here means the payload itself is absent.
try:
    from m3_memory.integrations import crewai as _pkg
    from m3_memory.integrations.crewai import (  # noqa: F401
        MIN_CREWAI_VERSION,
        __all__,
    )
except ImportError as _e:
    if "integrations" in str(_e) or "crewai" in str(_e):
        raise ImportError(
            "m3_memory.crewai is unavailable because this m3-memory build does not "
            "ship the CrewAI integration payload "
            "(m3_memory/integrations/crewai/). Upgrade to a build that includes "
            "it:\n    pip install -U m3-memory\n"
            "If you installed from source, reinstall so the integration subpackage "
            "is importable."
        ) from _e
    raise


def __getattr__(name: str) -> Any:
    # Forward M3StorageBackend (lazy, version-guarded) to the package __getattr__,
    # preserving its actionable optional-dep / version ImportError hints.
    return getattr(_pkg, name)
