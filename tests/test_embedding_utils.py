import os
import sys
import pytest

# Add bin to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import embedding_utils

def test_pack_unpack():
    """Test packing and unpacking of float vectors."""
    vec = [0.1, 0.2, -0.3, 0.4]
    packed = embedding_utils.pack(vec)
    unpacked = embedding_utils.unpack(packed)
    
    assert len(unpacked) == len(vec)
    for a, b in zip(vec, unpacked):
        assert pytest.approx(a) == b

def test_cosine_similarity():
    """Test cosine similarity calculation."""
    v1 = [1.0, 0.0]
    v2 = [0.0, 1.0]
    v3 = [1.0, 1.0]
    
    # Orthogonal vectors should have 0 similarity
    assert pytest.approx(embedding_utils.cosine(v1, v2)) == 0.0
    # Same vectors should have 1 similarity
    assert pytest.approx(embedding_utils.cosine(v1, v1)) == 1.0
    # 45 degree angle
    assert embedding_utils.cosine(v1, v3) > 0.7

def test_batch_cosine_inhomogeneous():
    """Test batch_cosine robust handling of inhomogeneous dimensions."""
    query = [1.0, 0.0]
    matrix = [
        [1.0, 0.0],    # matching dim, match
        [0.0, 1.0],    # matching dim, orthogonal
        [1.0, 0.0, 0.0], # different dim, should be ignored (0.0 score)
        [0.5, 0.5]     # matching dim, partial match
    ]
    
    scores = embedding_utils.batch_cosine(query, matrix)
    assert len(scores) == 4
    assert scores[0] == 1.0
    assert scores[1] == 0.0
    assert scores[2] == 0.0
    assert scores[3] > 0.7
