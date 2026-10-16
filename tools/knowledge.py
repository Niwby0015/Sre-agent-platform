"""Read-only retrieval tool for the locally indexed SRE Runbooks."""
from __future__ import annotations

from langchain.tools import tool

from config import RAG_MIN_SCORE, RAG_TOP_K
from rag.embeddings import EmbeddingUnavailableError, embed_query
from rag.vector_store import QdrantRunbookStore, VectorStoreUnavailableError


_MAX_RESULTS = 4
_MAX_CONTENT_CHARS = 1_800

# Retrieval-quality policy, fixed here rather than exposed as tool arguments:
# the model could pass min_score=0 to defeat the relevance threshold.
_RETRIEVAL_LIMIT = min(max(RAG_TOP_K, 1), _MAX_RESULTS)
_RETRIEVAL_MIN_SCORE = RAG_MIN_SCORE


@tool
def search_knowledge(query: str) -> str:
    """Search local Kubernetes Runbooks for remediation guidance.

    Use this only after Kubernetes tools, metrics, events, or logs identify a
    concrete incident type (for example OOMKilled, FailedScheduling,
    CrashLoopBackOff, or node MemoryPressure), or when the user explicitly
    asks for a Runbook. Search once per distinct incident type. Treat the
    results as reference guidance: live cluster evidence remains authoritative,
    and this tool must never justify or execute a write operation.
    """
    normalized_query = query.strip()
    if not normalized_query:
        return "ERROR: search_knowledge requires a non-empty incident query."

    try:
        results = QdrantRunbookStore().search(
            embed_query(normalized_query),
            limit=_RETRIEVAL_LIMIT,
            min_score=_RETRIEVAL_MIN_SCORE,
        )
    except (EmbeddingUnavailableError, VectorStoreUnavailableError) as exc:
        return (
            f"ERROR: Runbook search is unavailable: {exc} "
            "Continue diagnosis using live Kubernetes evidence alone."
        )
    except Exception as exc:
        return (
            f"ERROR: Runbook search failed: {type(exc).__name__}: {exc} "
            "Continue diagnosis using live Kubernetes evidence alone."
        )

    if not results:
        return (
            "NO RUNBOOK MATCH: no indexed Runbook met the relevance threshold. "
            "Continue diagnosis using live Kubernetes evidence."
        )

    lines = ["RUNBOOK GUIDANCE (reference only; verify against live cluster evidence):"]
    for number, result in enumerate(results, start=1):
        payload = result.payload
        title_path = payload.get("title_path") or []
        heading = " > ".join(map(str, title_path)) or payload.get("title", "Runbook")
        content = str(payload.get("content", "")).strip()
        if len(content) > _MAX_CONTENT_CHARS:
            content = content[:_MAX_CONTENT_CHARS].rstrip() + "\n[Runbook excerpt truncated]"
        lines.extend(
            [
                f"{number}. {heading} (score={result.score:.2f}; chunk={result.chunk_id})",
                f"Source: {payload.get('source_url') or payload.get('source') or 'unknown'}",
                content or "[No Runbook content stored for this result.]",
            ]
        )
    return "\n".join(lines)
