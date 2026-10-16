"""Incrementally embed the checked-in Runbook chunks into Qdrant."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from config import RAG_EMBEDDING_MODEL
from rag.embeddings import embed_texts
from rag.vector_store import QdrantRunbookStore, VectorPoint


@dataclass(frozen=True)
class RunbookChunk:
    """A normalized source chunk ready to embed and persist."""

    chunk_id: str
    embedding_text: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class IndexResult:
    """Counts emitted by one index run."""

    total: int
    indexed: int
    skipped: int
    deleted: int
    dry_run: bool


def _chunk_body(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"Chunk {path} is missing YAML front matter.")
    parts = text.split("---", 2)
    if len(parts) != 3:
        raise ValueError(f"Chunk {path} has invalid YAML front matter.")
    metadata = yaml.safe_load(parts[1])
    if not isinstance(metadata, dict):
        raise ValueError(f"Chunk {path} front matter must be a mapping.")
    body = parts[2].strip()
    if not body:
        raise ValueError(f"Chunk {path} has no body to embed.")
    return metadata, body


def load_runbook_chunks(manifest_path: Path) -> list[RunbookChunk]:
    """Load manifest-referenced chunks and build stable embedding documents."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list):
        raise ValueError(f"Manifest {manifest_path} must contain a JSON list.")

    project_root = manifest_path.parents[3]
    chunks: list[RunbookChunk] = []
    chunk_ids: set[str] = set()
    for entry in manifest:
        if not isinstance(entry, dict):
            raise ValueError("Manifest entries must be objects.")
        chunk_id = str(entry.get("chunk_id", "")).strip()
        relative_file = str(entry.get("file", "")).strip()
        if not chunk_id or not relative_file:
            raise ValueError("Each manifest entry needs chunk_id and file.")
        if chunk_id in chunk_ids:
            raise ValueError(f"Duplicate chunk_id in manifest: {chunk_id}")

        chunk_path = (project_root / relative_file).resolve()
        if not chunk_path.is_relative_to(project_root.resolve()):
            raise ValueError(f"Chunk path escapes the project: {relative_file}")
        metadata, body = _chunk_body(chunk_path)
        if metadata.get("chunk_id") != chunk_id:
            raise ValueError(f"Manifest and front matter disagree for {relative_file}")

        # The checked-in manifest stores this as a JSON list. Existing Markdown
        # front matter stores the same path as "Title > Section", so support
        # both forms while preserving a list in the Qdrant payload.
        title_path = entry.get("title_path") or metadata.get("title_path") or []
        if isinstance(title_path, str):
            title_path = [part.strip() for part in title_path.split(">") if part.strip()]
        if not isinstance(title_path, list):
            raise ValueError(f"title_path must be a list in {relative_file}")
        title = str(metadata.get("title") or entry.get("title") or "Runbook")
        embedding_text = "\n".join([title, " > ".join(map(str, title_path)), body]).strip()
        content_hash = hashlib.sha256(embedding_text.encode("utf-8")).hexdigest()
        payload = {
            "chunk_id": chunk_id,
            "content": body,
            "content_hash": content_hash,
            "embedding_model": RAG_EMBEDDING_MODEL,
            "source": str(metadata.get("source") or entry.get("source") or ""),
            "source_url": str(metadata.get("source_url") or entry.get("source_url") or ""),
            "title": title,
            "title_path": title_path,
            "kind": str(metadata.get("kind") or entry.get("kind") or "runbook"),
            "resource_kind": str(metadata.get("resource_kind") or entry.get("resource_kind") or ""),
            "severity": str(metadata.get("severity") or entry.get("severity") or ""),
        }
        chunks.append(RunbookChunk(chunk_id, embedding_text, payload))
        chunk_ids.add(chunk_id)
    return chunks


def index_runbooks(
    manifest_path: Path,
    *,
    store: QdrantRunbookStore | None = None,
    dry_run: bool = False,
    rebuild: bool = False,
    delete_stale: bool = False,
    batch_size: int = 8,
) -> IndexResult:
    """Index only changed chunks; destructive cleanup needs an explicit flag."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1.")
    chunks = load_runbook_chunks(manifest_path)
    if dry_run:
        return IndexResult(len(chunks), len(chunks), 0, 0, True)

    vector_store = store or QdrantRunbookStore()
    vector_store.ensure_collection(recreate=rebuild)
    stored_hashes = {} if rebuild else vector_store.existing_hashes(chunk.chunk_id for chunk in chunks)
    changed = [chunk for chunk in chunks if stored_hashes.get(chunk.chunk_id) != chunk.payload["content_hash"]]

    for start in range(0, len(changed), batch_size):
        batch = changed[start : start + batch_size]
        vectors = embed_texts([chunk.embedding_text for chunk in batch], batch_size=batch_size)
        vector_store.upsert(
            VectorPoint(chunk.chunk_id, vector, chunk.payload)
            for chunk, vector in zip(batch, vectors, strict=True)
        )

    deleted = vector_store.delete_except({chunk.chunk_id for chunk in chunks}) if delete_stale else 0
    return IndexResult(
        total=len(chunks),
        indexed=len(changed),
        skipped=len(chunks) - len(changed),
        deleted=deleted,
        dry_run=False,
    )
