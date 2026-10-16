"""Split the downloaded Kubernetes reference pages into RAG-ready chunks.

The source pages stay untouched under knowledge/runbooks/. This script creates
small semantic Markdown chunks with stable IDs and retrieval metadata.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "knowledge" / "runbooks"
OUTPUT_DIR = SOURCE_DIR / "chunks"


SOURCES = {
    "pod-crashloop.md": {
        "url": "https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/",
        "title": "Debug Pods",
        "resource_kind": "Pod",
        "severity": "warning",
        "groups": [
            ("pod-triage", "Diagnosing the problem", "My pod stays pending"),
            ("pod-pending", "My pod stays pending", "My pod stays waiting"),
            ("pod-image-pull", "My pod stays waiting", "My pod stays terminating"),
            ("pod-terminating", "My pod stays terminating", "My pod is crashing or otherwise unhealthy"),
            ("pod-crashing", "My pod is crashing or otherwise unhealthy", "My pod is running but not doing what I told it to do"),
            ("pod-running-unhealthy", "My pod is running but not doing what I told it to do", "Debugging Replication Controllers"),
            ("pod-controller", "Debugging Replication Controllers", "Debugging Services"),
            ("service-endpoints", "Debugging Services", "Network traffic is not forwarded"),
            ("service-network", "Network traffic is not forwarded", "{{% heading \"whatsnext\" %}}"),
        ],
    },
    "pod-oomkilled.md": {
        "url": "https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/",
        "title": "Resource Management for Pods and Containers",
        "resource_kind": "Pod",
        "severity": "warning",
        "groups": [
            ("resource-requests-limits", "Requests and limits", "Resource types"),
            ("resource-types", "Resource types", "Resource requests and limits of Pod and container"),
            ("resource-units-cpu-memory", "Resource units in Kubernetes", "Container resources example"),
            ("resource-scheduling", "How Pods with resource requests are scheduled", "How Kubernetes applies resource requests and limits"),
            ("resource-enforcement", "How Kubernetes applies resource requests and limits", "Monitoring compute & memory resource usage"),
            ("resource-monitoring", "Monitoring compute & memory resource usage", "Local ephemeral storage"),
            ("resource-ephemeral-storage", "Local ephemeral storage", "Extended resources"),
            ("resource-troubleshooting-pending", "My Pods are pending with event message `FailedScheduling`", "My container is terminated"),
            ("resource-troubleshooting-terminated", "My container is terminated", "{{% heading \"whatsnext\" %}}"),
        ],
    },
    "node-pressure-eviction.md": {
        "url": "https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/",
        "title": "Node-pressure Eviction",
        "resource_kind": "Node",
        "severity": "warning",
        "groups": [
            ("eviction-overview", "Self healing behavior", "Eviction signals and thresholds"),
            ("eviction-signals-memory", "Memory signals", "Filesystem signals"),
            ("eviction-signals-filesystem", "Filesystem signals", "Deprecated kubelet garbage collection features"),
            ("eviction-thresholds", "Eviction thresholds", "Eviction monitoring interval"),
            ("node-conditions", "Node conditions", "Reclaiming node level resources"),
            ("node-reclaim-resources", "Reclaiming node level resources", "Pod selection for kubelet eviction"),
            ("pod-eviction-selection", "Pod selection for kubelet eviction", "Minimum eviction reclaim"),
            ("minimum-eviction-reclaim", "Minimum eviction reclaim", "Node out of memory behavior"),
            ("node-oom", "Node out of memory behavior", "Good practices"),
            ("good-practices", "Good practices", "Known issues"),
        ],
    },
}


def _heading_positions(lines: list[str]) -> dict[str, int]:
    positions: dict[str, int] = {}
    for index, line in enumerate(lines):
        match = re.match(r"^#{2,6}\s+(.+?)\s*$", line)
        if match:
            title = re.sub(r"\s*\{#[^}]+\}\s*$", "", match.group(1).strip())
            positions.setdefault(title, index)
    return positions


def _slice(lines: list[str], starts: dict[str, int], start: str, end: str | None) -> str:
    if start not in starts:
        raise ValueError(f"Heading not found: {start}")
    start_index = starts[start]
    end_index = starts.get(end, len(lines)) if end else len(lines)
    content = "".join(lines[start_index:end_index]).strip()
    return content


def _clean_content(content: str) -> str:
    # Remove Hugo-only comments and template delimiters while preserving prose.
    content = re.sub(r"^<!--.*?-->\s*$", "", content, flags=re.MULTILINE | re.DOTALL)
    content = re.sub(r"\{\{<[^>]+>\}\}", "", content)
    content = re.sub(r"\{\{<\s*/[^>]+>\}\}", "", content)
    content = re.sub(r"\{\{[%<][^}]+[%>]\}\}", "", content)
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


def _front_matter(meta: dict, chunk_id: str, title_path: list[str], source: str) -> str:
    path = " > ".join(title_path).replace('"', "'")
    return (
        "---\n"
        f"chunk_id: {chunk_id}\n"
        f"source: {source}\n"
        f"source_url: {meta['url']}\n"
        f"title: \"{meta['title']}\"\n"
        f"title_path: \"{path}\"\n"
        "kind: runbook\n"
        f"resource_kind: {meta['resource_kind']}\n"
        f"severity: {meta['severity']}\n"
        "---\n\n"
    )


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUTPUT_DIR.glob("*.md"):
        old.unlink()

    manifest: list[dict] = []
    for source_name, meta in SOURCES.items():
        source_path = SOURCE_DIR / source_name
        if not source_path.exists():
            continue
        lines = source_path.read_text(encoding="utf-8").splitlines(keepends=True)
        starts = _heading_positions(lines)
        for chunk_id, start, end in meta["groups"]:
            content = _clean_content(_slice(lines, starts, start, end))
            if not content:
                continue
            title_path = [meta["title"], start]
            output_name = f"{source_path.stem}__{chunk_id}.md"
            output_path = OUTPUT_DIR / output_name
            output_path.write_text(
                _front_matter(meta, chunk_id, title_path, source_name) + content + "\n",
                encoding="utf-8",
            )
            manifest.append(
                {
                    "chunk_id": chunk_id,
                    "file": str(output_path.relative_to(ROOT)).replace("\\", "/"),
                    "source": source_name,
                    "source_url": meta["url"],
                    "title": meta["title"],
                    "title_path": title_path,
                    "kind": "runbook",
                    "resource_kind": meta["resource_kind"],
                    "severity": meta["severity"],
                    "characters": len(content),
                    "words": len(content.split()),
                }
            )

    (OUTPUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Generated {len(manifest)} chunks in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
