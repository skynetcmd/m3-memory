"""m3-memory files ingestion package.

Directory-walking, hierarchical file ingestion. Maintains a separate
`files.db` from the core `memory.db` (see `docs/FILE_INGESTION_PLAN.md`).

Layout mirrors `bin/memory/` but each submodule is files-store-specific:

  - config:      path resolution, env, schema version
  - db:          connection helpers, schema init, integrity checks
  - schema:      DDL (loaded by db._lazy_init)
  - walker:      directory traversal + filtering
  - identity:    file-node identity resolution + sha256 hashing
  - chunkers/:   per-filetype chunkers (markdown, pdf, text-fallback)
  - summarize:   file + leaf summary generation
  - ingest:      orchestration (walk → chunk → embed → write)
  - search:      hybrid FTS5 + vector + MMR over leaves
  - index:       summary-first wiki-index primitive
  - tools:       MCP tool registration

This package has NO direct imports of `bin/memory` internals beyond
`memory.config` (path resolution) and `memory.embed` (the embed cascade).
Cross-DB linkage is via UUID references only — never direct table joins.
"""
# Submodules are imported as they land. The package boots cleanly even
# during phased construction — each `from . import X` line is added when
# module X exists, so `import files_memory` always works.
from . import config  # noqa: F401
from . import db  # noqa: F401
from . import identity  # noqa: F401
from . import walker  # noqa: F401
from . import chunkers  # noqa: F401
from . import summarize  # noqa: F401
from . import embed  # noqa: F401
from . import entities  # noqa: F401
from . import extract  # noqa: F401
from . import ingest  # noqa: F401
from . import search  # noqa: F401
from . import index  # noqa: F401
from . import promote  # noqa: F401
from . import staleness  # noqa: F401
from . import provenance  # noqa: F401
from . import carry_forward  # noqa: F401
from . import promotability  # noqa: F401
from . import dedup  # noqa: F401
from . import corpora  # noqa: F401
from . import watch  # noqa: F401

# `tools` is the MCP/CLI entry point. NOT imported eagerly because it's a
# `python -m files_memory.tools …` target — eager import here would
# trigger a RuntimeWarning ("module found in sys.modules") when run as
# main. Callers wanting the registration function do
# `from files_memory import tools` explicitly.

__all__ = [
    "config", "db", "identity", "walker", "chunkers",
    "summarize", "embed", "entities", "extract",
    "ingest", "search", "index", "promote", "staleness",
    "provenance", "carry_forward", "promotability", "dedup",
    "corpora", "watch",
]
