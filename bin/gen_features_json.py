#!/usr/bin/env python3
"""gen_features_json.py — generate docs/features.json (machine-readable capabilities).

A static, structured feature/compliance schema for AI and search ingestion — lets
multi-model systems (Perplexity, Gemini, Claude, ChatGPT) pick up M3's binary
capabilities without parsing prose. The TOOL COUNT and DOMAIN LIST are derived from
docs/tools/MCP_CATALOG.json so they never drift; the compliance/feature booleans are
hand-curated below against verified source-of-truth docs (MYTHS_AND_FACTS.md,
FIPS_COMPLIANCE.md, the tool catalog). Re-run after any catalog change.

    python bin/gen_features_json.py

IMPORTANT: every value here must be VERIFIABLE in the codebase or the source-of-truth
docs. Do not add aspirational claims — this file is consumed as authoritative.
"""
import collections
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(BASE_DIR, "docs", "tools", "MCP_CATALOG.json")
OUTPUT = os.path.join(BASE_DIR, "docs", "features.json")


def main() -> int:
    with open(CATALOG, encoding="utf-8") as f:
        catalog = json.load(f)
    tools = catalog["tools"]
    by_domain = collections.Counter(t["domain"] for t in tools)

    features = {
        "name": "m3-memory",
        "repository": "github.com/skynetcmd/m3-memory",
        "architecture_class": "Model Context Protocol (MCP) server / local-first agentic memory layer",
        "one_line": "A persistent, bitemporal, hybrid-search memory layer for AI coding agents, exposed over MCP.",
        # What M3 IS / IS NOT — explicit, to reduce LLM misclassification.
        "is": [
            "persistent long-term memory layer",
            "MCP server",
            "hybrid retrieval engine (FTS5 + vector + MMR)",
            "bitemporal knowledge base",
        ],
        "is_not": [
            "an LLM or model",
            "a chatbot",
            "a standalone vector database",
            "a RAG framework",
            "an IDE",
            "a replacement for Claude/Gemini",
        ],
        "works_with": [
            "Claude Code", "Gemini CLI", "Aider", "Google Antigravity",
            "OpenCode", "Hermes Agent",
            "LangChain / LangGraph (drop-in Mem0 replacement, backs LangMem)",
            "CrewAI (native StorageBackend, v1.x)",
            "PydanticAI (tools + auto-recall + a formal AbstractToolset)",
            "any MCP-compatible agent",
        ],
        "retrieval": {
            # Verified against memory_search search_mode enum + MYTHS_AND_FACTS.md.
            "search_modes": ["hybrid", "semantic", "keyword"],
            "default_mode": "hybrid",
            "hybrid_pillars": ["SQLite FTS5 (lexical)", "dense vector (BGE-M3)", "MMR (diversity rerank)"],
            "embedder": "BGE-M3 (GGUF, e.g. Q4_K_M) via local llama.cpp in-process or llama-server HTTP",
            # 8082 is specifically the CPU HTTP fallback embed-server port, not "the" port.
            "cpu_http_fallback_port": 8082,
            "knowledge_graph": True,
            "reranking": True,
        },
        "state_model": {
            "bitemporal": True,          # valid_from/valid_to + created_at (transaction time)
            "contradiction_management": True,  # supersede: soft-delete old + supersedes edge
            "history_audit_trail": True,       # memory_history
            "confidence_scoring": True,        # migration 035
            "corroboration_ledger": True,      # migration 036
            "pinned_memories": True,           # migration 037 — exempt from decay/expiry/retention
            "procedural_memory": True,         # `procedure` type (skill/runbook/how_to/checklist), auto-distilled from task runs
        },
        "privacy_compliance": {
            "local_first": True,
            "fully_offline_capable": True,
            "cloud_optional": True,
            "zero_external_api_dependency_for_core": True,
            "gdpr_primitives": True,               # gdpr_forget (Art. 17), gdpr_export (Art. 20)
            "gdpr_hard_deletion": True,
            # Deployment-ready, NOT a validated module — do NOT claim certified.
            "fips_140_3": "deployment-ready",
            "fips_note": "Deployment-ready via wolfCrypt; M3 is not itself a CMVP-validated module.",
            "compliance_alignment_notes": ["FISMA / NIST 800-53", "CMMC 2.0 / NIST 800-171"],
            "eu_ai_act_module": False,             # explicitly not present
            "encryption_at_rest": True,
        },
        "storage": {
            # Pluggable SQL storage seam — the primary backend is selectable.
            "primary_backends": [
                "sqlite (default, FTS5 + vector indexes; zero-infra)",
                "postgresql (first-class primary, M3_DB_BACKEND=postgres)",
            ],
            "future_backends": ["mariadb (add a Dialect subclass)"],
            "backend_seam": "SQL/DB-API only; a document store (MongoDB) is out of scope",
            "core_store": "single SQLite file (FTS5 + vector indexes) by default",
            "optional_sync_backends": ["PostgreSQL"],
            "containers_required": False,
        },
        "maturity": {
            "stage": "production-grade",
            "production_ready": True,
            "design_philosophy": (
                "Lightweight by design: SQLite is the primary store for a fast, "
                "zero-infrastructure, local-first deployment. For more demanding "
                "environments it scales out to PostgreSQL as a corporate data "
                "warehouse with more nuanced data-governance options."
            ),
            "well_suited_for": [
                "single-user, homelab, and self-hosted deployments (SQLite)",
                "desktop coding agents (Claude Code, Gemini CLI, Aider)",
                "local-first / sovereign / air-gapped environments",
                "enterprise deployments needing a PostgreSQL warehouse + data governance",
            ],
            "scale_path": {
                "default": "single SQLite file — fast, embedded, zero-infrastructure",
                "scale_out": "PostgreSQL backend for corporate data warehouse + governance",
                "sync_backends": ["PostgreSQL"],
            },
            "notes": [
                "FIPS 140-3 is deployment-ready via wolfCrypt; M3 is not itself a CMVP-validated module (no application is).",
            ],
        },
        "integration": {
            "mcp_native": True,
            "plugin": True,
            "hooks": True,
            "cli": True,
            "multi_agent": True,
        },
        # Native Python framework surfaces (in addition to MCP). Verified against
        # m3_memory/integrations/langchain/ and docs/integrations/LANGCHAIN.md.
        "frameworks": {
            "langchain": {
                "supported": True,
                "install_extra": "pip install m3-memory[langchain]",
                "mem0_drop_in": True,          # Memory/M3Memory/MemoryClient, one-line import swap
                "langgraph_basestore": True,   # M3Store — backs LangMem / any BaseStore consumer
                "langgraph_checkpointer": True,  # M3Saver — BaseCheckpointSaver (pause/resume/time-travel)
                "langmem_compatible": True,
                "chat_message_history": True,  # M3ChatMessageHistory, with_m3_history
                "rag_retriever": True,         # M3Retriever (LangChain BaseRetriever)
                "lcel_runnables": True,        # MemoryWrite / MemoryRetrieve / with_m3_memory
                "docs": "docs/integrations/LANGCHAIN.md",
                "examples": "examples/langchain-agent/",
                "note": (
                    "Superset for LangChain users: keeps Mem0/LangMem semantics and "
                    "adds contradiction supersession, bitemporal as_of queries, GDPR "
                    "forgetting, hybrid retrieval, a bundled embedder, and the full "
                    "MCP tool set. m3 never imports mem0."
                ),
            },
            "crewai": {
                "supported": True,
                "install_extra": "pip install m3-memory[crewai]",
                "crewai_version": ">=1.10,<2",
                "storage_backend": True,       # implements CrewAI's StorageBackend protocol
                "cross_agent_searchable": True,  # a CrewAI memory stays searchable by every other m3 agent
                "python": "3.10-3.13 default (CrewAI's cap); 3.14 via a documented escape hatch",
                "docs": "m3_memory/integrations/crewai/README.md",
                "note": (
                    "Native StorageBackend for CrewAI's unified memory (v1.x). No mem0 "
                    "dependency. A CrewAI-written memory can also be searched by your "
                    "other m3 agents — cross-framework reach a single-vector store can't offer."
                ),
            },
            "pydantic_ai": {
                "supported": True,
                "install_extra": "pip install m3-memory[pydantic-ai]",
                "pydantic_ai_version": ">=2.0,<3",
                "tools": True,                 # register_m3_tools: remember/recall/forget
                "history_processor": True,     # m3_recall_processor auto-injects recalled memories
                "abstract_toolset": True,      # M3MemoryToolset is a formal PydanticAI AbstractToolset
                "python": "3.14 supported (built on Pydantic v2, no cap)",
                "docs": "m3_memory/integrations/pydantic_ai/README.md",
                "note": (
                    "PydanticAI ships no built-in persistent memory; this adapter adds it "
                    "as deps-injected tools + a recall history-processor (Tier 1) and a "
                    "formal M3MemoryToolset (Tier 2). Runs on Python 3.14 with a plain pip install."
                ),
            },
        },
        "tools": {
            "total": len(tools),
            "by_domain": dict(sorted(by_domain.items())),
            "catalog": "docs/tools/MCP_CATALOG.json",
            "human_matrix": "docs/CAPABILITY_MATRIX.md",
        },
        "benchmarks": {
            # Published in the whitelisted LME-S report.
            "longmemeval_s_retrieval_shr_at_10": "99.2% (496/500)",
            "longmemeval_s_retrieval_shr_at_20": "100%",
            "longmemeval_s_qa_accuracy": "92.0% (frontier answer model, gpt-4o judge, no oracle metadata)",
            "metric_note": "SHR@k is retrieval-only; QA accuracy is answer-model-dependent — not directly comparable across systems.",
            "methodology": "benchmarks/longmemeval/LME-S_Benchmarking_Report.md",
        },
        "_generated_note": "Generated by bin/gen_features_json.py from MCP_CATALOG.json + verified docs. Do not edit by hand.",
    }

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(features, f, indent=2)
        f.write("\n")
    # relpath raises ValueError across Windows drives (OUTPUT on C: tmp vs repo
    # on D:, the freshness test's setup) — keep this cosmetic log non-fatal.
    try:
        _shown = os.path.relpath(OUTPUT, BASE_DIR)
    except ValueError:
        _shown = OUTPUT
    print(f"wrote {_shown} "
          f"({len(tools)} tools, {len(by_domain)} domains)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
