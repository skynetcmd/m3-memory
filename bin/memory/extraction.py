"""Pluggable Entity-Extraction Backend for m3-memory.

Allows configuring local/remote LLMs, rule-based extractors, or custom scripts
to automatically extract structured entities and relationships from raw text,
fully integrating with m3-memory's entity-graph CRUD and bitemporal structures.
"""
from __future__ import annotations

import abc
import asyncio
import importlib.util
import inspect
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

# Ensure bin/ is on path for unified_ai and llm_failover imports
_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.dirname(_HERE)
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import llm_failover
import unified_ai

from memory import config as _config
from memory import entity as _entity_mod

logger = logging.getLogger("memory.extraction")


# ──────────────────────────────────────────────────────────────────────────────
# Abstract Base Class
# ──────────────────────────────────────────────────────────────────────────────
class BaseExtractor(abc.ABC):
    """Abstract base class for all pluggable entity-extraction backends."""

    @abc.abstractmethod
    async def extract(self, text: str) -> dict:
        """Extract entities and relationships from text.

        Returns a dictionary in the shape:
        {
            "entities": [
                {
                    "canonical_name": str,
                    "entity_type": str,
                    "mention_text": str,
                    "mention_offset": int,
                    "confidence": float
                },
                ...
            ],
            "relationships": [
                {
                    "from_entity": str,
                    "to_entity": str,
                    "predicate": str,
                    "confidence": float
                },
                ...
            ]
        }
        """
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Slugify & Normalization Helpers
# ──────────────────────────────────────────────────────────────────────────────
def normalize_entity_id(name: str, etype: str) -> str:
    """Slugify and prefix entity name to generate a canonical entity ID.

    Example: normalize_entity_id("Roanoke", "place") -> "place:roanoke"
    """
    clean = name.strip().lower()
    clean = re.sub(r"[^\w\s-]", "", clean)
    clean = re.sub(r"[\s_]+", "_", clean)
    return f"{etype.strip().lower()}:{clean}"


def canonicalize_relationship(rel_type: str) -> str:
    """Normalize a relationship predicate string to snake_case.

    Example: canonicalize_relationship("lives in") -> "lives_in"
    """
    clean = rel_type.strip().lower()
    clean = re.sub(r"[^\w\s-]", "", clean)
    clean = re.sub(r"[\s_-]+", "_", clean)
    return clean


# ──────────────────────────────────────────────────────────────────────────────
# Rule-Based Heuristic Fallback Extractor
# ──────────────────────────────────────────────────────────────────────────────
class RuleBasedExtractor(BaseExtractor):
    """Heuristic rule-based extractor using regular expressions and vocabulary list alignment."""

    async def extract(self, text: str) -> dict:
        entities: list[dict[str, Any]] = []
        relationships: list[dict[str, Any]] = []

        # Pull active vocabulary from entity.py
        active_types = _entity_mod.VALID_ENTITY_TYPES
        active_predicates = _entity_mod.VALID_ENTITY_PREDICATES

        # 1. Known technologies/skills mapped to 'topic' or 'technology'
        extracted_names = set()
        tech_words = [
            ("Rust", "topic"), ("Python", "topic"), ("C\\+\\+", "topic"),
            ("SQLite", "topic"), ("Postgres", "topic"), ("ChromaDB", "topic"),
            ("workflow", "topic"), ("FIPS", "topic"), ("cryptography", "topic")
        ]
        for tech, etype in tech_words:
            if etype in active_types:
                tmatches = re.finditer(r"\b(" + tech + r")\b", text, re.IGNORECASE)
                for tm in tmatches:
                    name = tm.group(1)
                    if name not in extracted_names:
                        entities.append({
                            "canonical_name": name,
                            "entity_type": etype,
                            "mention_text": name,
                            "mention_offset": tm.start(),
                            "confidence": 0.80
                        })
                        extracted_names.add(name)

        # 2. Capitalized words (Person, Place, Organization candidate)
        # Match consecutive capitalized words, e.g., "John Doe", "Roanoke", "Google Corp"
        matches = re.finditer(r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)\b", text)

        for m in matches:
            name = m.group(1)
            offset = m.start()
            if name.lower() in ("i", "i'm", "me", "my", "the", "a", "an", "and", "or", "but"):
                continue
            if name in extracted_names:
                continue

            # Classify based on heuristics or active vocab defaults
            etype = "person"
            if any(term in name.lower() for term in ("corp", "inc", "ltd", "google", "microsoft", "amazon", "github", "apple")):
                etype = "organization"
            elif any(term in name.lower() for term in ("city", "roanoke", "enclave", "valley", "mount", "lake", "ocean", "river")):
                etype = "place"

            if etype in active_types and name not in extracted_names:
                entities.append({
                    "canonical_name": name,
                    "entity_type": etype,
                    "mention_text": name,
                    "mention_offset": offset,
                    "confidence": 0.70
                })
                extracted_names.add(name)

        # 3. Simple regex-based relationship predicates matching active predicates
        # e.g., "lives in [Place]" -> "located_in", "works at [Org]" -> "works_at", "learning [Tech]" -> "prefers" / "references"
        person_nodes = [e["canonical_name"] for e in entities if e["entity_type"] == "person"]
        place_nodes = [e["canonical_name"] for e in entities if e["entity_type"] == "place"]
        org_nodes = [e["canonical_name"] for e in entities if e["entity_type"] == "organization"]
        tech_nodes = [e["canonical_name"] for e in entities if e["entity_type"] == "topic"]

        for person in person_nodes:
            # check lives in / located in (allowing intermediate words or overall context)
            for place in place_nodes:
                p = re.escape(person)
                pl = re.escape(place)
                # match if they appear close or in a clear connection
                if re.search(p + r".{0,100}?(?:lives\s+in|moved\s+to|located\s+in|resides\s+in).{0,100}?" + pl, text, re.IGNORECASE) or \
                   re.search(pl + r".{0,100}?(?:lives\s+in|moved\s+to|located\s+in|resides\s+in).{0,100}?" + p, text, re.IGNORECASE):
                    pred = "located_in"
                    if pred in active_predicates:
                        relationships.append({
                            "from_entity": person,
                            "to_entity": place,
                            "predicate": pred,
                            "confidence": 0.75
                        })

            # check works at (allowing intermediate words or overall context)
            for org in org_nodes:
                p = re.escape(person)
                o = re.escape(org)
                if re.search(p + r".{0,100}?(?:works\s+at|employed\s+by|joined|hired\s+by).{0,100}?" + o, text, re.IGNORECASE) or \
                   re.search(o + r".{0,100}?(?:works\s+at|employed\s+by|joined|hired\s+by).{0,100}?" + p, text, re.IGNORECASE):
                    pred = "works_at"
                    if pred in active_predicates:
                        relationships.append({
                            "from_entity": person,
                            "to_entity": org,
                            "predicate": pred,
                            "confidence": 0.80
                        })

            # check learning / knows technology (allowing intermediate pronouns like "He is learning...")
            for tech in tech_nodes:
                p = re.escape(person)
                t = re.escape(tech)
                # Matches "John Doe... learning Rust" or "John Doe... [pronoun] is learning Rust"
                # Since tech words appear in the text, we check for presence of learning/skills verbs
                if re.search(p + r".{0,150}?(?:learning|knows|uses|programming|skills\s+in|writes).{0,100}?" + t, text, re.IGNORECASE) or \
                   re.search(r"\b(?:he|she|they)\b.{0,50}?(?:learning|knows|uses|programming|skills\s+in|writes).{0,100}?" + t, text, re.IGNORECASE):
                    pred = "prefers"
                    if pred in active_predicates:
                        relationships.append({
                            "from_entity": person,
                            "to_entity": tech,
                            "predicate": pred,
                            "confidence": 0.75
                        })

        return {"entities": entities, "relationships": relationships}


# ──────────────────────────────────────────────────────────────────────────────
# Local / Remote LLM Extraction Backend
# ──────────────────────────────────────────────────────────────────────────────
class LLMExtractor(BaseExtractor):
    """LLM-based entity extractor utilizing the unified_ai and llm_failover modules."""

    def __init__(self, backend_config: dict):
        self.backend_config = backend_config
        self.provider = backend_config.get("type", "lmstudio")  # "lmstudio", "gemini", "claude"
        self.model = backend_config.get("model", "")
        self.url = backend_config.get("url", "")
        self.custom_prompt = backend_config.get("prompt", "")

    async def extract(self, text: str) -> dict:
        active_types = sorted(list(_entity_mod.VALID_ENTITY_TYPES))
        active_predicates = sorted(list(_entity_mod.VALID_ENTITY_PREDICATES))

        # Setup standard prompt serialization
        prompt = self.custom_prompt
        if not prompt:
            prompt = f"""You are a precise entity and relationship extraction backend.
Your job is to extract entities and their semantic relationships from the provided text.

Allowed Entity Types (DO NOT use other types):
{", ".join(active_types)}

Allowed Relationship Predicates (DO NOT use other predicates):
{", ".join(active_predicates)}

For each extracted entity, provide:
- canonical_name: The normalized, canonical name of the entity.
- entity_type: One of the allowed entity types.
- mention_text: The exact text from the source content.
- mention_offset: Character index offset of the start of the mention in text.
- confidence: Float between 0.0 and 1.0.

For each relationship, provide:
- from_entity: The canonical name of the origin entity.
- to_entity: The canonical name of the target entity.
- predicate: One of the allowed predicates.
- confidence: Float between 0.0 and 1.0.

You must output a single, raw, valid JSON object matching the schema below exactly. DO NOT wrap in markdown formatting (like ```json), write any explanations, or include conversational text:
{{
  "entities": [
    {{
      "canonical_name": "Caroline",
      "entity_type": "person",
      "mention_text": "Caroline",
      "mention_offset": 0,
      "confidence": 0.95
    }}
  ],
  "relationships": [
    {{
      "from_entity": "Caroline",
      "to_entity": "Roanoke",
      "predicate": "located_in",
      "confidence": 0.90
    }}
  ]
}}

Text to process:
"{text}"
"""

        gemini_key = os.environ.get("GEMINI_API_KEY")
        claude_key = os.environ.get("ANTHROPIC_API_KEY")
        lmstudio_url = self.url or os.environ.get("M3_EMBED_URL") or "http://localhost:1234"

        # Instantiate unified client
        cli = unified_ai.UnifiedAI(
            gemini_key=gemini_key,
            claude_key=claude_key,
            lmstudio_url=lmstudio_url
        )

        try:
            # Resolve model and provider via llm_failover if not explicitly locked down
            prov = self.provider
            mod = self.model

            if prov == "lmstudio" and not mod:
                async with httpx_async_client() as hcli:
                    best = await llm_failover.get_best_llm(hcli, "")
                    if best:
                        lmstudio_url = best[0]
                        mod = best[1]
                        cli.lmstudio_url = lmstudio_url
                    else:
                        mod = "default-model"

            # Execute Chat Completion
            response_text = await asyncio.to_thread(
                lambda: cli.chat(
                    provider=prov,
                    model=mod,
                    messages=[
                        {"role": "system", "content": "You are a precise JSON-only extractor backend."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.0
                )
            )

            # Scrub markdown block fences if LLM ignored the rule
            scrubbed = response_text.strip()
            if scrubbed.startswith("```"):
                lines = scrubbed.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                scrubbed = "\n".join(lines).strip()

            parsed = json.loads(scrubbed)
            return parsed

        except Exception as e:
            logger.warning(f"LLM entity extraction failed, falling back to rule-based: {e}")
            fallback = RuleBasedExtractor()
            return await fallback.extract(text)
        finally:
            cli.close()


def httpx_async_client() -> Any:
    """Get stock httpx AsyncClient."""
    import httpx
    return httpx.AsyncClient()


# ──────────────────────────────────────────────────────────────────────────────
# Custom Script Extraction Backend
# ──────────────────────────────────────────────────────────────────────────────
class CustomScriptExtractor(BaseExtractor):
    """Loads a user-defined custom extraction Python script dynamically."""

    def __init__(self, script_path: str, function_name: str = "extract"):
        self.script_path = script_path
        self.function_name = function_name

    async def extract(self, text: str) -> dict:
        try:
            path = Path(self.script_path)
            if not path.is_absolute():
                path = Path(_entity_mod.Path(_config.BASE_DIR)) / path

            spec = importlib.util.spec_from_file_location("custom_extractor_mod", str(path))
            if spec is None or spec.loader is None:
                raise ValueError(f"Could not load spec for {path}")

            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            fn = getattr(mod, self.function_name)

            # Support both async and sync custom functions. inspect.iscoroutinefunction
            # is the supported form — asyncio.iscoroutinefunction is deprecated (3.14)
            # and slated for removal in 3.16.
            if inspect.iscoroutinefunction(fn):
                res = await fn(text)
            else:
                res = fn(text)

            if not isinstance(res, dict):
                raise ValueError("Custom script extraction must return a dict.")
            return res

        except Exception as e:
            logger.warning(f"Custom script extraction failed, falling back to rule-based: {e}")
            fallback = RuleBasedExtractor()
            return await fallback.extract(text)


# ──────────────────────────────────────────────────────────────────────────────
# Config Loader & Extractor Factory
# ──────────────────────────────────────────────────────────────────────────────
def get_configured_extractor() -> BaseExtractor:
    """Instantiate and return the configured entity extractor based on settings."""
    # Look for env overrides first (typical M3 design language)
    etype = os.environ.get("M3_EXTRACTION_TYPE", "rule_based").lower().strip()
    model = os.environ.get("M3_EXTRACTION_MODEL", "").strip()
    url = os.environ.get("M3_EXTRACTION_URL", "").strip()
    prompt = os.environ.get("M3_EXTRACTION_PROMPT", "").strip()
    script = os.environ.get("M3_EXTRACTION_SCRIPT", "").strip()
    func = os.environ.get("M3_EXTRACTION_FUNCTION", "extract").strip()

    # Look for config file fallback (~/.m3/config/extraction.json)
    cfg_file = Path(_config._M3_CONFIG_ROOT) / "extraction.json"
    if cfg_file.exists() and etype == "rule_based" and not model and not url:
        try:
            with open(cfg_file, encoding="utf-8") as f:
                data = json.load(f) or {}
            backend_cfg = data.get("extraction_backend", {})
            if backend_cfg:
                etype = backend_cfg.get("type", etype).lower().strip()
                model = backend_cfg.get("model", model).strip()
                url = backend_cfg.get("url", url).strip()
                prompt = backend_cfg.get("prompt", prompt).strip()
                script = backend_cfg.get("script", script).strip()
                func = backend_cfg.get("function", func).strip()
        except Exception as e:
            logger.warning(f"Failed to load extraction config file: {e}")

    # Build and return the designated extractor subclass
    if etype in ("local_llm", "remote_llm", "llm", "gemini", "claude", "lmstudio"):
        return LLMExtractor({
            "type": "gemini" if etype == "gemini" else ("claude" if etype == "claude" else "lmstudio"),
            "model": model,
            "url": url,
            "prompt": prompt
        })
    elif etype == "custom_script" and script:
        return CustomScriptExtractor(script, func)
    else:
        return RuleBasedExtractor()


# ──────────────────────────────────────────────────────────────────────────────
# Unified MCP Tool Endpoint Implementation
# ──────────────────────────────────────────────────────────────────────────────
async def extract_entities_impl(text: str) -> str:
    """Implement `/extract_entities` MCP tool and endpoint. Performs extraction only."""
    if not text or not text.strip():
        return json.dumps({"entities": [], "relationships": [], "error": "Empty text provided."})

    extractor = get_configured_extractor()
    try:
        res = await extractor.extract(text)

        # Verify result format contains the necessary lists
        if not isinstance(res, dict):
            res = {"entities": [], "relationships": []}
        if "entities" not in res:
            res["entities"] = []
        if "relationships" not in res:
            res["relationships"] = []

        # Clean and normalize extraction formats
        active_types = _entity_mod.VALID_ENTITY_TYPES
        active_predicates = _entity_mod.VALID_ENTITY_PREDICATES

        cleaned_entities = []
        for ent in res["entities"]:
            cname = (ent.get("canonical_name") or ent.get("name") or "").strip()
            etype = (ent.get("entity_type") or ent.get("type") or "").strip().lower()
            if not cname or etype not in active_types:
                continue
            cleaned_entities.append({
                "canonical_name": cname,
                "entity_type": etype,
                "mention_text": (ent.get("mention_text") or cname).strip(),
                "mention_offset": int(ent.get("mention_offset") or 0),
                "confidence": float(ent.get("confidence") or 0.85)
            })

        cleaned_relationships = []
        for rel in res["relationships"]:
            from_ent = (rel.get("from_entity") or rel.get("from") or "").strip()
            to_ent = (rel.get("to_entity") or rel.get("to") or "").strip()
            pred = canonicalize_relationship(rel.get("predicate") or rel.get("type") or "")
            if not from_ent or not to_ent or pred not in active_predicates:
                continue
            cleaned_relationships.append({
                "from_entity": from_ent,
                "to_entity": to_ent,
                "predicate": pred,
                "confidence": float(rel.get("confidence") or 0.85)
            })

        return json.dumps({"entities": cleaned_entities, "relationships": cleaned_relationships})
    except Exception as e:
        logger.error(f"extract_entities_impl failed: {e}")
        return json.dumps({"entities": [], "relationships": [], "error": str(e)})
