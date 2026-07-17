"""Public re-export shim: ``from m3_memory.pydantic_ai import M3Deps, ...``.

The short, memorable path the docs promise, forwarding to the integration package
``m3_memory.integrations.pydantic_ai``. The PydanticAI-coupled surface is resolved
LAZILY, so a missing/too-old pydantic-ai fails loud with an actionable
``pip install m3-memory[pydantic-ai]`` hint at access time (§3), never here.

    from pydantic_ai import Agent
    from m3_memory.pydantic_ai import M3Deps, register_m3_tools, m3_recall_processor

    agent = Agent("anthropic:claude-sonnet-5", deps_type=M3Deps)
    register_m3_tools(agent)
    agent.run_sync("remember I prefer dark roast", deps=M3Deps(user_id="alice"))
"""

from __future__ import annotations

from typing import Any

try:
    from m3_memory.integrations import pydantic_ai as _pkg
    from m3_memory.integrations.pydantic_ai import (  # noqa: F401
        MIN_PYDANTIC_AI_VERSION,
        __all__,
    )
except ImportError as _e:
    if "integrations" in str(_e) or "pydantic_ai" in str(_e):
        raise ImportError(
            "m3_memory.pydantic_ai is unavailable because this m3-memory build "
            "does not ship the PydanticAI integration payload "
            "(m3_memory/integrations/pydantic_ai/). Upgrade to a build that "
            "includes it:\n    pip install -U m3-memory\n"
            "If you installed from source, reinstall so the integration subpackage "
            "is importable."
        ) from _e
    raise


def __getattr__(name: str) -> Any:
    # Forward the public surface (lazy, version-guarded) to the package __getattr__,
    # preserving its actionable optional-dep / version ImportError hints.
    return getattr(_pkg, name)
