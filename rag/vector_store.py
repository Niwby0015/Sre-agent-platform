"""Qdrant persistence for the local SRE Runbook knowledge index."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from config import (
    RAG_EMBEDDING_DIMENSIONS,
    RAG_QDRANT_API_KEY,
    RAG_QDRANT_COLLECTION,
    RAG_QDRANT_URL,
)


class VectorStoreUnavailableError(RuntimeError):
    """Raised when the optional Qdrant client dependency is unavailable."""


@dataclass(frozen=True)
class VectorPoint:
    """A vector and its retrievable Runbook payload."""

    chunk_id: str
    vector: list[float]
    payload: dict[str, Any]


@dataclass(frozen=True)
class SearchResult:
    """One scored Runbook chunk returned from Qdrant."""

    chunk_id: str
    score: float
    payload: dict[str, Any]


def qdrant_point_id(chunk_id: str) -> str:
    """Map a stable human-readable chunk id to Qdrant's UUID id format."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sre-agent/runbooks/{chunk_id}"))


class QdrantRunbookStore:
    """Small, explicit Qdrant adapter for the versioned Runbook collection."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        collection_name: str = RAG_QDRANT_COLLECTION,
    ) -> None:
        self.collection_name = collection_name
        self._client = client or self._build_client()

    @staticmethod
    def _qdrant_models() -> Any:
        try:
            from qdrant_client import models
        except ImportError as exc:
            raise VectorStoreUnavailableError(
                "Qdrant support requires qdrant-client. Install dependencies with "
                "'python -m pip install -r requirements.txt'."
            ) from exc
        return models

    @staticmethod
    def _build_client() -> Any:
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:
            raise VectorStoreUnavailableError(
                "Qdrant support requires qdrant-client. Install dependencies with "
                "'python -m pip install -r requirements.txt'."
            ) from exc
        return QdrantClient(url=RAG_QDRANT_URL, api_key=RAG_QDRANT_API_KEY or None)

    def ensure_collection(self, *, recreate: bool = False) -> None:
        """Create the collection, or validate its vector dimension if it exists."""
        models = self._qdrant_models()
        exists = self._client.collection_exists(self.collection_name)
        if exists and recreate:
            self._client.delete_collection(self.collection_name)
            exists = False

        if not exists:
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(
                    size=RAG_EMBEDDING_DIMENSIONS,
                    distance=models.Distance.COSINE,
                ),
            )
            return

        collection = self._client.get_collection(self.collection_name)
        vectors = collection.config.params.vectors
        observed_size = getattr(vectors, "size", None)
        if observed_size != RAG_EMBEDDING_DIMENSIONS:
            raise RuntimeError(
                f"Qdrant collection {self.collection_name!r} has dimension "
                f"{observed_size}, expected {RAG_EMBEDDING_DIMENSIONS}. Use a new "
                "collection name or run the indexer with --rebuild."
            )

    def existing_hashes(self, chunk_ids: Iterable[str]) -> dict[str, str]:
        """Return stored content hashes for the requested chunk identifiers."""
        ids = list(chunk_ids)
        if not ids:
            return {}
        requested_ids = [qdrant_point_id(chunk_id) for chunk_id in ids]
        records = self._client.retrieve(
            collection_name=self.collection_name,
            ids=requested_ids,
            with_payload=["chunk_id", "content_hash"],
            with_vectors=False,
        )
        return {
            str(record.payload["chunk_id"]): str(record.payload["content_hash"])
            for record in records
            if record.payload and record.payload.get("chunk_id") and record.payload.get("content_hash")
        }

    def upsert(self, points: Iterable[VectorPoint]) -> None:
        """Persist a batch of new or changed chunks."""
        values = list(points)
        if not values:
            return
        models = self._qdrant_models()
        self._client.upsert(
            collection_name=self.collection_name,
            points=[
                models.PointStruct(
                    id=qdrant_point_id(point.chunk_id),
                    vector=point.vector,
                    payload=point.payload,
                )
                for point in values
            ],
            wait=True,
        )

    def delete_except(self, current_chunk_ids: set[str]) -> int:
        """Delete vectors whose chunk id no longer appears in the manifest."""
        stale_ids: list[str] = []
        offset: Any = None
        while True:
            records, offset = self._client.scroll(
                collection_name=self.collection_name,
                limit=256,
                offset=offset,
                with_payload=["chunk_id"],
                with_vectors=False,
            )
            stale_ids.extend(
                str(record.id)
                for record in records
                if not record.payload or str(record.payload.get("chunk_id", "")) not in current_chunk_ids
            )
            if offset is None:
                break

        if stale_ids:
            models = self._qdrant_models()
            self._client.delete(
                collection_name=self.collection_name,
                points_selector=models.PointIdsList(points=stale_ids),
                wait=True,
            )
        return len(stale_ids)

    def all_chunks(self) -> list[dict[str, Any]]:
        """Return every stored chunk's payload (small offline runbook index).

        Used by the read-only Runbook library panel, which groups chunks by their
        top-level ``title``. Bounded by the index size (a handful of runbooks), so
        a full scroll is cheap and avoids exposing Qdrant's cursor API upward.
        """
        chunks: list[dict[str, Any]] = []
        offset: Any = None
        while True:
            records, offset = self._client.scroll(
                collection_name=self.collection_name,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            chunks.extend(dict(record.payload) for record in records if record.payload)
            if offset is None:
                break
        return chunks

    def search(
        self,
        query_vector: list[float],
        *,
        limit: int,
        min_score: float | None = None,
    ) -> list[SearchResult]:
        """Retrieve the highest-scoring chunks with Qdrant's current query API."""
        response = self._client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            limit=limit,
            score_threshold=min_score,
            with_payload=True,
            with_vectors=False,
        )
        return [
            SearchResult(
                chunk_id=str(point.payload.get("chunk_id", point.id)),
                score=float(point.score),
                payload=dict(point.payload or {}),
            )
            for point in response.points
        ]
