"""m3-memory ↔ CrewAI integration (v1.x unified memory).

Public surface — the one-line wire-up CrewAI's own docs point at:

    from m3_memory.crewai import M3StorageBackend
    from crewai import Crew
    from crewai.memory import Memory

    crew = Crew(
        agents=[...], tasks=[...],
        memory=Memory(storage=M3StorageBackend(user_id="crew-alpha")),
    )

``M3StorageBackend`` implements CrewAI's ``StorageBackend`` Protocol
(``crewai.memory.storage.backend``, introduced v1.10, Feb 2026). It subclasses
nothing of ours — it IS the m3 backing store CrewAI writes to and reads from,
riding m3's one canonical in-process dispatch (§2 narrow seam; §12a).

Why cross-agent memory is the m3 edge here: CrewAI embeds with its OWN embedder
(default OpenAI text-embedding-3-large, 3072-dim) and hands us records/queries
already vectorized. m3 stores that vector AND — via its multi-embedding
``memory_embeddings`` schema — also derives its native bge-m3 (1024-dim) vector,
so a CrewAI-written memory is retrievable by BOTH CrewAI *and* every other m3
agent (Claude Code, Gemini, LangChain). A single-vector store (LanceDB, Qdrant,
mem0) cannot do this. See the package README.

CrewAI is an OPTIONAL dependency. This surface imports ``crewai`` lazily via
``__getattr__``, so a missing/too-old install fails loud with an actionable
message at ACCESS time — never a cryptic mid-crew crash (§3 fail-loud).
"""

from __future__ import annotations

from typing import Any

# The minimum CrewAI that ships the unified-memory StorageBackend protocol. v1.0
# GA (Oct 2025) predates the memory rewrite (PR #4420, first shipped v1.10.0,
# Feb 2026), so >=1.0 is NOT enough — pin the protocol's actual first release.
MIN_CREWAI_VERSION = "1.10.0"

__all__ = ["M3StorageBackend", "MIN_CREWAI_VERSION"]

_CREWAI_HINT = (
    "The CrewAI integration requires CrewAI v{min}+ (the unified-memory "
    "StorageBackend protocol shipped in v1.10). Install it with:\n"
    "    pip install m3-memory[crewai]\n"
    "or\n"
    "    pip install 'crewai>={min}'"
).format(min=MIN_CREWAI_VERSION)


def _check_crewai_version() -> None:
    """Fail loud (§3) if CrewAI is absent or predates the StorageBackend protocol.

    A single forward-only guard: this adapter targets v1.x only (the v0.x
    ``Storage`` contract is a different, incompatible shape). An older CrewAI must
    raise with a clear upgrade message rather than importing and then failing
    obscurely when ``crewai.memory.storage.backend`` is missing.
    """
    try:
        from importlib import metadata

        raw = metadata.version("crewai")
    except Exception as e:  # crewai not installed at all
        raise ImportError(_CREWAI_HINT) from e

    def _parts(v: str) -> tuple[int, ...]:
        out: list[int] = []
        for chunk in v.split(".")[:3]:
            num = ""
            for ch in chunk:
                if ch.isdigit():
                    num += ch
                else:
                    break
            out.append(int(num) if num else 0)
        return tuple(out)

    if _parts(raw) < _parts(MIN_CREWAI_VERSION):
        raise ImportError(
            f"CrewAI {raw} is installed, but the m3 integration needs "
            f">={MIN_CREWAI_VERSION} (the StorageBackend protocol is not present "
            f"before v1.10).\n{_CREWAI_HINT}"
        )


def __getattr__(name: str) -> Any:
    """Lazy-import the CrewAI-coupled surface so importing this package never
    hard-requires CrewAI (mirrors the langchain adapter's __getattr__)."""
    if name == "M3StorageBackend":
        _check_crewai_version()
        try:
            from .backend import M3StorageBackend
        except ImportError as e:
            raise ImportError(f"{name}: {_CREWAI_HINT}") from e
        return M3StorageBackend
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
