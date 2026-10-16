"""CLI entry point for incrementally indexing local SRE Runbooks into Qdrant."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "knowledge" / "runbooks" / "chunks" / "manifest.json"

# Direct script execution adds scripts/ rather than the project root to
# sys.path. Make both `python scripts/index_runbooks.py` and module execution
# resolve the local rag package consistently.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rag.indexer import index_runbooks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true", help="Validate chunks without loading the model or writing Qdrant.")
    parser.add_argument("--rebuild", action="store_true", help="Delete and recreate the configured collection before indexing.")
    parser.add_argument("--delete-stale", action="store_true", help="Delete vectors absent from the manifest after indexing.")
    args = parser.parse_args()

    result = index_runbooks(
        args.manifest,
        dry_run=args.dry_run,
        rebuild=args.rebuild,
        delete_stale=args.delete_stale,
        batch_size=args.batch_size,
    )
    mode = "DRY RUN" if result.dry_run else "INDEXED"
    print(
        f"{mode}: total={result.total} indexed={result.indexed} "
        f"skipped={result.skipped} deleted={result.deleted}"
    )


if __name__ == "__main__":
    main()
