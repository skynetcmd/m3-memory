"""Embedder-model mismatch detection in `m3 doctor`.

`seed_shared_config` records the served `embed_model` in .embed_config.json (the
installer / `m3 setup` / `m3 embedder shared` / `m3 doctor --fix` all call it), and
the shared-embedder probe cross-checks that pin against what :8082 actually serves
— warning loudly when a foreign model would write an INCOMPATIBLE vector space.
(It deliberately does NOT pin a primary `embed_url`: the shared server is the
tier-2 fallback via /embedding; pinning the primary at :8082 would 404.) All
hermetic — tmp config root, config/env only, never a live server/GPU/DB.
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BIN = _ROOT / "bin"
for _p in (str(_ROOT), str(_BIN)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from doctor import shared_embedder_probe as P  # noqa: E402

from m3_memory import embedder_admin as EA  # noqa: E402

# ── seed_shared_config: the single source of truth all three surfaces call ──────

def test_seed_records_embed_model_and_fallback(tmp_path):
    path, wrote = EA.seed_shared_config(str(tmp_path))
    assert wrote is True
    data = json.loads(Path(path).read_text())
    assert data["embed_model"] == "bge-m3-GGUF-Q4_K_M.gguf"  # what :8082 serves
    assert data["fallback_url"] == "http://127.0.0.1:8082"
    # Deliberately NO primary `embed_url` pin (that would 404 the primary tier).
    assert "embed_url" not in data


def test_seed_embed_model_from_gguf_basename(tmp_path):
    gguf = "/models/deepsweet/bge-m3-GGUF-Q4_K_M/bge-m3-GGUF-Q4_K_M.gguf"
    path, _ = EA.seed_shared_config(str(tmp_path), gguf_path=gguf)
    data = json.loads(Path(path).read_text())
    assert data["embed_model"] == "bge-m3-GGUF-Q4_K_M.gguf"


def test_seed_preserves_hand_tuned_values(tmp_path):
    # A user pinned a custom model; overwrite=False must not clobber it.
    cfg = tmp_path / ".embed_config.json"
    cfg.write_text(json.dumps({"embed_model": "custom-embedder"}))
    path, _ = EA.seed_shared_config(str(tmp_path))
    data = json.loads(Path(path).read_text())
    assert data["embed_model"] == "custom-embedder"


# ── model-family normalization (bge-m3 aliases are the SAME weights) ────────────

def test_norm_tag_collapses_bge_m3_aliases():
    assert P._norm_model_tag("bge-m3-GGUF-Q4_K_M.gguf") == "bge-m3"
    assert P._norm_model_tag("text-embedding-bge-m3") == "bge-m3"
    assert P._norm_model_tag("nomic-embed-text") == "nomic-embed-text"


# ── doctor cross-check: pinned embed_model vs served (hermetic, no DB) ──────────

def _classify(monkeypatch, *, pinned, served_model, served_dim=1024):
    monkeypatch.delenv("M3_EMBED_MODEL", raising=False)   # env would override the pin
    cfg = {"embed_model": pinned} if pinned else {}
    return P._classify_embed_model(cfg, {"model": served_model, "dim": served_dim})


def test_bge_m3_alias_is_not_a_mismatch(monkeypatch):
    # Pin is the API tag; server serves the GGUF tag — same bge-m3 weights.
    token, _lines = _classify(monkeypatch, pinned="text-embedding-bge-m3",
                              served_model="bge-m3-GGUF-Q4_K_M.gguf")
    assert token is None


def test_pinned_vs_foreign_served_is_warned(monkeypatch):
    token, lines = _classify(monkeypatch, pinned="text-embedding-bge-m3",
                             served_model="nomic-embed-text")
    assert token == "model-mismatch"
    assert any("INCOMPATIBLE" in ln for ln in lines)


def test_env_pin_overrides_config(monkeypatch):
    monkeypatch.setenv("M3_EMBED_MODEL", "nomic-embed-text")
    token, _ = P._classify_embed_model({"embed_model": "bge-m3-GGUF-Q4_K_M.gguf"},
                                       {"model": "bge-m3-GGUF-Q4_K_M.gguf", "dim": 1024})
    assert token == "model-mismatch"   # env pin (nomic) diverges from served bge-m3


def test_no_pin_stays_quiet(monkeypatch):
    # Nothing pinned -> nothing to compare -> no verdict, no noise (the existing
    # doctor tests seed no embed_model and must stay green).
    token, lines = _classify(monkeypatch, pinned="", served_model="bge")
    assert token is None
    assert lines == []
