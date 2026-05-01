import os
import sys
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

# Add bin to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import llm_failover


@pytest.fixture(autouse=True)
def _reset_failover_caches():
    """The llm_failover module caches discovered endpoints process-globally
    to avoid the GET /v1/models roundtrip on every call. That cache must
    not leak between tests — clear before each test runs."""
    llm_failover.clear_failover_caches()
    yield
    llm_failover.clear_failover_caches()


def test_parse_model_size():
    """Test parsing model sizes from strings."""
    assert llm_failover.parse_model_size("llama-3-70b") == 70.0
    assert llm_failover.parse_model_size("phi-3-3.8b") == 3.8
    assert llm_failover.parse_model_size("deepseek-v3-235a22b") == 235.0
    assert llm_failover.parse_model_size("nomic-embed-text-500m") == 0.5
    assert llm_failover.parse_model_size("no-size-here") == 0.0

@pytest.mark.asyncio
async def test_get_best_embed_success():
    """Test successful embedding model discovery."""
    mock_client = AsyncMock()
    mock_resp = MagicMock() # MagicMock for sync methods like raise_for_status
    mock_resp.status_code = 200
    
    # httpx response.json() is NOT a coroutine, but we mocked it as one 
    # if we used AsyncMock. We need to be careful.
    # In llm_failover.py it's called as: data = response.json() (sync)
    mock_resp.json.return_value = {
        "data": [
            {"id": "llama-3-8b"},
            {"id": "nomic-embed-text-v1.5"}
        ]
    }
    mock_client.get.return_value = mock_resp
    
    with patch("llm_failover.LLM_ENDPOINTS", ["http://localhost:1234/v1"]):
        result = await llm_failover.get_best_embed(mock_client, "token")
        assert result is not None
        assert result[0] == "http://localhost:1234/v1"
        assert result[1] == "nomic-embed-text-v1.5"

@pytest.mark.asyncio
async def test_get_best_llm_size_sorting():
    """Test that get_best_llm picks the largest available model."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"id": "llama-3-8b"},
            {"id": "deepseek-r1-70b"},
            {"id": "phi-3-3b"}
        ]
    }
    mock_client.get.return_value = mock_resp
    
    with patch("llm_failover.LLM_ENDPOINTS", ["http://localhost:1234/v1"]):
        result = await llm_failover.get_best_llm(mock_client, "token")
        assert result is not None
        assert result[1] == "deepseek-r1-70b"
