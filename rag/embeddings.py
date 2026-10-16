"""Local multilingual E5 embedding model used by the SRE knowledge index."""
from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from config import RAG_EMBEDDING_DEVICE, RAG_EMBEDDING_DIMENSIONS, RAG_EMBEDDING_MODEL


class EmbeddingUnavailableError(RuntimeError):
    """Raised when the optional local embedding dependency is unavailable."""


def _cuda_available() -> bool:
    """Return whether this Python environment has a usable CUDA runtime."""
    try:
        import torch
    except ImportError:
        return False
    return torch.cuda.is_available()


def _model_device() -> str:
    """Resolve the operator setting to a concrete SentenceTransformer device."""
    requested = RAG_EMBEDDING_DEVICE.strip().lower()
    cuda_available = _cuda_available()

    if requested in {"", "auto"}:
        return "cuda" if cuda_available else "cpu"
    if requested == "cpu":
        return "cpu"
    if requested in {"cuda", "gpu"}:
        if not cuda_available:
            raise EmbeddingUnavailableError(
                "RAG_EMBEDDING_DEVICE requests CUDA, but the installed PyTorch runtime "
                "does not have CUDA available. Set it to 'auto' or 'cpu', or install a "
                "CUDA-enabled PyTorch build."
            )
        return "cuda"
    raise ValueError(
        "RAG_EMBEDDING_DEVICE must be one of: auto, cpu, cuda, gpu. "
        f"Got {RAG_EMBEDDING_DEVICE!r}."
    )


@lru_cache(maxsize=1)
def get_embedding_model() -> Any:
    """Load multilingual E5 only when an ingest or retrieval call needs it.

    Keeping this import and model construction lazy avoids a model download or
    GPU allocation while the API starts without RAG enabled.
    """
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise EmbeddingUnavailableError(
            "Local RAG embeddings require sentence-transformers. Install dependencies with "
            "'python -m pip install -r requirements.txt'."
        ) from exc

    device = _model_device()
    return SentenceTransformer(
        RAG_EMBEDDING_MODEL,
        device=device,
    )


def _dense_vectors(vectors: Any) -> list[list[float]]:
    """Convert SentenceTransformer output to validated JSON-compatible vectors."""
    values = vectors.tolist() if hasattr(vectors, "tolist") else vectors
    output = [[float(value) for value in vector] for vector in values]
    if any(len(vector) != RAG_EMBEDDING_DIMENSIONS for vector in output):
        observed = sorted({len(vector) for vector in output})
        raise RuntimeError(
            f"Embedding dimension mismatch: expected {RAG_EMBEDDING_DIMENSIONS}, got {observed}."
        )
    return output


def _embed(texts: Sequence[str], *, prefix: str, batch_size: int = 8) -> list[list[float]]:
    """Embed E5 input with the query or passage prefix required by the model."""
    values = list(texts)
    if not values:
        return []
    if any(not text or not text.strip() for text in values):
        raise ValueError("Cannot embed empty text.")
    vectors = get_embedding_model().encode(
        [f"{prefix}{text.strip()}" for text in values],
        batch_size=batch_size,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return _dense_vectors(vectors)


def embed_texts(texts: Sequence[str], *, batch_size: int = 8) -> list[list[float]]:
    """Embed Runbook passages for multilingual E5 retrieval.

    E5 uses different prefixes for passages and queries. The passage prefix is
    applied only here so Chinese user queries can later retrieve English docs.
    """
    return _embed(texts, prefix="passage: ", batch_size=batch_size)


def embed_query(query: str) -> list[float]:
    """Embed one retrieval query with E5's query prefix."""
    return _embed([query], prefix="query: ")[0]
