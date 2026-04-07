from sentence_transformers import SentenceTransformer

_model = None


def get_model():
    global _model
    if _model is None:
        # Swap this later for an MLX-backed model if desired
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def embed(texts: list[str]) -> list[list[float]]:
    model = get_model()
    return model.encode(texts, convert_to_numpy=False).tolist()

