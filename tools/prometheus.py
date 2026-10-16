"""Read-only Prometheus client.

Shared by the ops console's data-centre page and (later) by the agent's
Prometheus query tools, so the HTTP/parsing logic lives in one place.

Deliberately stdlib-only (`urllib`): Prometheus speaks plain HTTP+JSON, and the
project already carries enough dependencies without adding a client library for
two endpoints. It also keeps this importable from contexts that have no
HTTP libraries loaded yet.

Configuration comes from `config.PROMETHEUS_URL`. When it is empty or the
server is unreachable, every helper raises `PrometheusUnavailable` rather than
returning an empty result — callers must distinguish "Prometheus says there is
no data" from "Prometheus is not there", because a dashboard that renders a
confident zero for an unreachable backend is worse than one that says it cannot
reach it.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

# Prometheus scrapes on a 30s default interval; 8s is generous for a query that
# only touches in-memory head blocks.
DEFAULT_TIMEOUT = 8.0


class PrometheusUnavailable(RuntimeError):
    """Raised when PROMETHEUS_URL is unset, unreachable, or returns an error."""


def base_url() -> str:
    """The configured Prometheus root, without a trailing slash."""
    from config import PROMETHEUS_URL

    return (PROMETHEUS_URL or "").strip().rstrip("/")


def _fetch(path: str, params: dict[str, str], timeout: float) -> dict:
    url = base_url()
    if not url:
        raise PrometheusUnavailable(
            "PROMETHEUS_URL is not set — point it at the Prometheus endpoint "
            "to enable metric panels."
        )

    full = f"{url}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(full, timeout=timeout) as resp:
            payload = json.load(resp)
    except Exception as exc:  # noqa: BLE001 — urllib raises a wide family
        raise PrometheusUnavailable(f"{path} failed: {exc}") from exc

    if payload.get("status") != "success":
        raise PrometheusUnavailable(
            f"{path} returned status={payload.get('status')!r}: "
            f"{payload.get('error') or 'no error detail'}"
        )
    return payload.get("data") or {}


def _vector_points(data: dict) -> list[tuple[dict, float]]:
    """Flatten an instant-query result into (labels, value) pairs.

    Prometheus encodes every sample as a string; a non-numeric one means the
    expression produced NaN/Inf (common for ratios over an empty window), which
    is dropped rather than propagated as 0 — it would otherwise chart as a
    real reading of zero.
    """
    out: list[tuple[dict, float]] = []
    for series in data.get("result") or []:
        try:
            value = float(series["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if value != value or value in (float("inf"), float("-inf")):  # NaN / ±Inf
            continue
        out.append((series.get("metric") or {}, value))
    return out


def query(expr: str, timeout: float = DEFAULT_TIMEOUT) -> list[tuple[dict, float]]:
    """Run an instant query. Returns (labels, value) pairs, newest sample."""
    return _vector_points(_fetch("/api/v1/query", {"query": expr}, timeout))


def scalar(expr: str, timeout: float = DEFAULT_TIMEOUT) -> float | None:
    """First value of an instant query, or None when the expression is empty.

    `None` is a real answer here — e.g. a rate() over a window with no samples
    yet — and callers render it as "—" rather than as zero.
    """
    points = query(expr, timeout=timeout)
    return points[0][1] if points else None


def query_range(
    expr: str, range_seconds: int, step_seconds: int, timeout: float = DEFAULT_TIMEOUT
) -> list[dict]:
    """Run a range query and return `[{labels, points: [[ts, value], ...]}]`."""
    import time as _time

    end = _time.time()
    data = _fetch(
        "/api/v1/query_range",
        {
            "query": expr,
            "start": f"{end - range_seconds:.0f}",
            "end": f"{end:.0f}",
            "step": f"{step_seconds}s",
        },
        timeout,
    )

    out: list[dict] = []
    for series in data.get("result") or []:
        points: list[list[float]] = []
        for pair in series.get("values") or []:
            try:
                ts, raw = float(pair[0]), float(pair[1])
            except (IndexError, TypeError, ValueError):
                continue
            if ts != ts or raw != raw or raw in (float("inf"), float("-inf")):
                continue
            points.append([ts, raw])
        if points:
            out.append({"labels": series.get("metric") or {}, "points": points})
    return out


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------
# Kept together so the page and the agent's future tools can share them, and so
# a PromQL fix lands in one place rather than in each caller.

# Excludes tmpfs/ramfs/overlay: those report the host's RAM as a "filesystem"
# and would otherwise dominate the utilisation figure with a meaningless number.
_REAL_FS = '{fstype!~"tmpfs|ramfs|overlay|squashfs|autofs"}'

EXPR_CPU_PERCENT = (
    '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'
)
EXPR_MEM_PERCENT = (
    "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100"
)
# max() across mountpoints, not avg(): node-exporter reports several bind mounts
# of the same underlying filesystem on a containerised node, and averaging them
# would understate a genuinely full disk.
EXPR_DISK_PERCENT = (
    f"max((node_filesystem_size_bytes{_REAL_FS} "
    f"- node_filesystem_avail_bytes{_REAL_FS}) "
    f"/ node_filesystem_size_bytes{_REAL_FS}) * 100"
)
EXPR_DISK_USED = f"max(node_filesystem_size_bytes{_REAL_FS} - node_filesystem_avail_bytes{_REAL_FS})"
EXPR_DISK_TOTAL = f"max(node_filesystem_size_bytes{_REAL_FS})"

# container!="" drops the per-pod pause container, whose usage is an artefact of
# the sandbox rather than workload consumption.
EXPR_POD_CPU_TOP = (
    'topk({n}, sum(rate(container_cpu_usage_seconds_total{{container!=""}}[5m])) by (pod))'
)
EXPR_POD_MEM_TOP = (
    'topk({n}, sum(container_memory_working_set_bytes{{container!=""}}) by (pod))'
)
EXPR_CONTAINERS = 'count(container_memory_working_set_bytes{container!=""})'
EXPR_TARGETS_TOTAL = "count(up)"
EXPR_TARGETS_UP = "count(up == 1)"

# Actual consumption, both already carrying namespace/pod/container labels so
# they can be joined against the kube-state-metrics quota series on that key.
# CPU is summed across the per-mode series cAdvisor emits.
_EXPR_USAGE = {
    "memory": 'container_memory_working_set_bytes{container!=""}',
    "cpu": (
        'sum by (namespace, pod, container) '
        '(rate(container_cpu_usage_seconds_total{container!=""}[5m]))'
    ),
}
# Both quota metrics report in the base unit: bytes for memory, cores for CPU.
_QUOTA_SCALE = {"memory": 1024 * 1024, "cpu": 0.001}  # -> MiB, millicores


def _quota_key(metric: dict) -> tuple[str, str, str]:
    return (
        metric.get("namespace") or "",
        metric.get("pod") or "",
        metric.get("container") or "",
    )


def _phase_counts(metric_name: str, key: str = "phase") -> dict[str, int]:
    """Count objects per phase.

    These metrics are one series per (object, phase) with a 1 on the phase the
    object is actually in and 0 on the rest, so summing values would count every
    object once per possible phase.
    """
    counts: dict[str, int] = {}
    for metric, value in query(metric_name):
        if value != 1:
            continue
        phase = metric.get(key) or "Unknown"
        counts[phase] = counts.get(phase, 0) + 1
    return counts


def pod_health(top_n: int = 8) -> dict:
    """Pod phase distribution, restart hot spots, readiness and node density."""
    restarts = []
    for metric, value in query("kube_pod_container_status_restarts_total"):
        if value <= 0:
            continue
        restarts.append(
            {
                "namespace": metric.get("namespace") or "",
                "pod": metric.get("pod") or "",
                "container": metric.get("container") or "",
                "count": int(value),
            }
        )
    restarts.sort(key=lambda r: -r["count"])

    not_ready = sum(
        1 for _, value in query("kube_pod_container_status_ready") if value != 1
    )

    # kube_pod_info carries the pod -> node assignment; it is the only place
    # that mapping exists as a metric.
    by_node: dict[str, int] = {}
    for metric, _ in query("kube_pod_info"):
        node = metric.get("node") or "未调度"
        by_node[node] = by_node.get(node, 0) + 1

    return {
        "phases": _phase_counts("kube_pod_status_phase"),
        "restarts": restarts[:top_n],
        "restart_total": sum(r["count"] for r in restarts),
        "not_ready_containers": not_ready,
        "by_node": by_node,
    }


def _replica_rows(kind: str, spec: str, ready: str, extra: dict[str, str] | None = None) -> list[dict]:
    """Join a controller's desired replica count with its observed ones."""
    desired = {_name_key(m, kind): v for m, v in query(spec)}
    ready_map = {_name_key(m, kind): v for m, v in query(ready)}
    extra_maps = {
        field: {_name_key(m, kind): v for m, v in query(expr)}
        for field, expr in (extra or {}).items()
    }

    rows = []
    for key, want in desired.items():
        namespace, name = key
        if not name:
            continue
        row = {
            "namespace": namespace,
            "name": name,
            "desired": int(want),
            "ready": int(ready_map.get(key, 0)),
        }
        for field, mapping in extra_maps.items():
            row[field] = int(mapping.get(key, 0))
        rows.append(row)
    # Degraded first: the page exists to surface what is not keeping up.
    rows.sort(key=lambda r: (r["ready"] >= r["desired"], r["name"]))
    return rows


def _name_key(metric: dict, kind: str) -> tuple[str, str]:
    return (metric.get("namespace") or "", metric.get(kind) or "")


def workloads() -> dict:
    """Replica alignment for Deployments, StatefulSets and DaemonSets.

    Requests and limits are deliberately absent: they live per container, and
    the overview page's quota panel already reports them.
    """
    deployments = _replica_rows(
        "deployment",
        "kube_deployment_spec_replicas",
        "kube_deployment_status_replicas_ready",
        {
            "available": "kube_deployment_status_replicas_available",
            "up_to_date": "kube_deployment_status_replicas_updated",
        },
    )
    statefulsets = _replica_rows(
        "statefulset", "kube_statefulset_replicas", "kube_statefulset_status_replicas_ready"
    )
    daemonsets = _replica_rows(
        "daemonset",
        "kube_daemonset_status_desired_number_scheduled",
        "kube_daemonset_status_number_ready",
    )

    every = deployments + statefulsets + daemonsets
    return {
        "deployments": deployments,
        "statefulsets": statefulsets,
        "daemonsets": daemonsets,
        "summary": {
            "total": len(every),
            "degraded": sum(1 for r in every if r["ready"] < r["desired"]),
        },
    }


def nodes() -> dict:
    """Node readiness, capacity, allocatable headroom and pod density."""
    info = {m.get("node") or "": m for m, _ in query("kube_node_info")}

    # kube_node_status_condition is one series per (node, condition, status).
    # The series with value 1 is the condition's current state, so the two
    # things worth extracting are Ready=true, and the pressure conditions while
    # they are true — a pressure condition that is false is just a node being
    # healthy and does not belong on the page.
    ready_nodes: set[str] = set()
    pressure: dict[str, list[str]] = {}
    for metric, value in query("kube_node_status_condition"):
        if value != 1:
            continue
        node = metric.get("node") or ""
        cond = metric.get("condition") or ""
        status = (metric.get("status") or "").lower()
        if cond == "Ready":
            if status == "true":
                ready_nodes.add(node)
        elif status == "true":
            pressure.setdefault(node, []).append(cond)

    def _resources(metric_name: str) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for metric, value in query(metric_name):
            node = metric.get("node") or ""
            res = metric.get("resource") or ""
            # Only cpu and memory are comparable across nodes; the rest are
            # counts of ephemeral objects.
            if res not in ("cpu", "memory"):
                continue
            out.setdefault(node, {})[res] = value
        return out

    capacity = _resources("kube_node_status_capacity")
    allocatable = _resources("kube_node_status_allocatable")

    density: dict[str, int] = {}
    for metric, _ in query("kube_pod_info"):
        node = metric.get("node") or ""
        density[node] = density.get(node, 0) + 1

    rows = []
    for name in sorted(set(capacity) | set(info) | set(density)):
        if not name:
            continue
        cap = capacity.get(name, {})
        alloc = allocatable.get(name, {})
        rows.append(
            {
                "name": name,
                "ready": name in ready_nodes,
                "kubelet_version": info.get(name, {}).get("kubelet_version") or "",
                "capacity_cpu": cap.get("cpu"),
                "capacity_memory": cap.get("memory"),
                "allocatable_cpu": alloc.get("cpu"),
                "allocatable_memory": alloc.get("memory"),
                "pods": density.get(name, 0),
                "pressure": pressure.get(name, []),
            }
        )
    return {"nodes": rows}


def storage() -> dict:
    """PVC phases, requested capacity and the StorageClasses backing them."""
    phase = {
        _name_key(m, "persistentvolumeclaim"): (m.get("phase") or "Unknown")
        for m, v in query("kube_persistentvolumeclaim_status_phase")
        if v == 1
    }
    requested = {
        _name_key(m, "persistentvolumeclaim"): v
        for m, v in query("kube_persistentvolumeclaim_resource_requests_storage_bytes")
    }
    # StorageClass per claim, when the object exposes it.
    classes = {
        _name_key(m, "persistentvolumeclaim"): (m.get("storageclass") or "")
        for m, _ in query("kube_persistentvolumeclaim_info")
    }

    rows = []
    for key in sorted(set(phase) | set(requested)):
        namespace, name = key
        if not name:
            continue
        rows.append(
            {
                "namespace": namespace,
                "name": name,
                "phase": phase.get(key, "Unknown"),
                "requested_bytes": requested.get(key),
                "storage_class": classes.get(key) or "",
            }
        )
    rows.sort(key=lambda r: (r["phase"] == "Bound", r["name"]))

    storage_classes = [
        {
            "name": m.get("storageclass") or "",
            "provisioner": m.get("provisioner") or "",
            "reclaim_policy": m.get("reclaim_policy") or "",
            "binding_mode": m.get("volume_binding_mode") or "",
        }
        for m, v in query("kube_storageclass_info")
        if v == 1
    ]

    return {
        "pvcs": rows,
        "storage_classes": storage_classes,
        "summary": {
            "total": len(rows),
            "bound": sum(1 for r in rows if r["phase"] == "Bound"),
            "not_bound": sum(1 for r in rows if r["phase"] != "Bound"),
        },
    }


def resource_usage(top_n: int = 8) -> dict:
    """Actual usage against configured requests and limits, per container.

    This is the join that kube-state-metrics makes possible: cAdvisor reports
    what a container *is* using, kube-state-metrics reports what it is *allowed*
    to use, and right-sizing needs both. Without the second, a panel can only
    say "this pod uses 216 MiB" — never "216 of its 1500 MiB limit", which is
    the number that predicts an OOM kill or a throttled workload.
    """
    out: dict = {"memory": [], "cpu": [], "unlimited": []}

    for res, scale in _QUOTA_SCALE.items():
        limits = {
            _quota_key(m): v
            for m, v in query(
                f'kube_pod_container_resource_limits{{resource="{res}", container!=""}}'
            )
        }
        requests = {
            _quota_key(m): v
            for m, v in query(
                f'kube_pod_container_resource_requests{{resource="{res}", container!=""}}'
            )
        }
        usage = {_quota_key(m): v for m, v in query(_EXPR_USAGE[res])}

        rows = []
        for key, used in usage.items():
            namespace, pod, container = key
            # The infra/pause container carries no quota and no meaningful use.
            if not pod or not container or container == "POD":
                continue
            limit = limits.get(key)
            if not limit:
                # Carry the usage too: an unquoted container burning 400 MiB is
                # a different risk from one using 2 MiB, and the page needs the
                # number to rank them.
                out["unlimited"].append(
                    {
                        "pod": pod,
                        "container": container,
                        "namespace": namespace,
                        "resource": res,
                        "usage": used / scale,
                    }
                )
                continue
            rows.append(
                {
                    "pod": pod,
                    "container": container,
                    "namespace": namespace,
                    "usage": used / scale,
                    "limit": limit / scale,
                    "request": (requests.get(key) or 0) / scale or None,
                    "ratio": used / limit if limit else None,
                }
            )

        rows.sort(key=lambda r: r["ratio"] or 0, reverse=True)
        out[res] = rows[:top_n]

    # Highest consumer first, but grouped by resource: with no limit to compare
    # against, consumption is the only ordering that says which of these
    # matters — and usage is MiB for one resource and millicores for the other,
    # so a single sort across both would rank 87 millicores above 400 MiB and
    # present a meaningless order as a ranking.
    out["unlimited"].sort(key=lambda r: (r["resource"], -r["usage"]))
    return out


def _pod_rows(expr: str, n: int, scale: float) -> list[dict]:
    """Top-N pods as [{pod, value}], largest first, scaled to display units."""
    rows = query(expr.format(n=n))
    rows.sort(key=lambda r: r[1], reverse=True)
    return [{"pod": (lbl.get("pod") or "—"), "value": val / scale} for lbl, val in rows]


def snapshot(range_seconds: int = 3600, step_seconds: int = 60, top_n: int = 6) -> dict:
    """Everything the data-centre page renders, in one call.

    A single backend call rather than one per panel: the page polls, and six
    round-trips per poll to a backend on the other side of a port-forward is
    six chances to time out independently.
    """
    cpu_now = scalar(EXPR_CPU_PERCENT)
    mem_now = scalar(EXPR_MEM_PERCENT)
    disk_now = scalar(EXPR_DISK_PERCENT)
    disk_used = scalar(EXPR_DISK_USED)
    disk_total = scalar(EXPR_DISK_TOTAL)
    containers = scalar(EXPR_CONTAINERS)

    targets_total = scalar(EXPR_TARGETS_TOTAL) or 0
    targets_up = scalar(EXPR_TARGETS_UP) or 0

    def _trend(expr: str) -> list[list[float]]:
        series = query_range(expr, range_seconds, step_seconds)
        if not series:
            return []
        # These expressions aggregate across the node, so several series would
        # mean several nodes; taking the busiest keeps the line meaningful
        # rather than averaging a hot node into a quiet one.
        busiest = max(series, key=lambda s: max(p[1] for p in s["points"]))
        return busiest["points"]

    return {
        "cpu_percent": cpu_now,
        "mem_percent": mem_now,
        "disk_percent": disk_now,
        "disk_used_bytes": disk_used,
        "disk_total_bytes": disk_total,
        "containers": int(containers) if containers is not None else None,
        "targets_total": int(targets_total),
        "targets_up": int(targets_up),
        "cpu_trend": _trend(EXPR_CPU_PERCENT),
        "mem_trend": _trend(EXPR_MEM_PERCENT),
        # CPU is scaled to millicores (matches how kubectl and the manifests
        # express requests) and memory to MiB.
        "top_pods_cpu": _pod_rows(EXPR_POD_CPU_TOP, top_n, 0.001),
        "top_pods_mem": _pod_rows(EXPR_POD_MEM_TOP, top_n, 1024 * 1024),
        "range_seconds": range_seconds,
        "step_seconds": step_seconds,
    }


def metric_names(pattern: str = "", limit: int = 200) -> list[str]:
    """Metric names known to Prometheus, optionally filtered by substring.

    Backs the agent's discovery tool. A model asked to query metrics for a
    cluster it has never seen will otherwise invent plausible-but-absent names
    (`container_cpu_usage` for `container_cpu_usage_seconds_total`) and burn
    calls on empty results.
    """
    # This endpoint's "data" IS the array of names - unlike query endpoints,
    # where it is an object wrapping "result".
    data = _fetch("/api/v1/label/__name__/values", {}, DEFAULT_TIMEOUT)
    names = [str(n) for n in (data if isinstance(data, list) else [])]
    if pattern:
        needle = pattern.lower()
        names = [n for n in names if needle in n.lower()]
    return sorted(names)[:limit]


def active_targets() -> list[dict]:
    """Every active scrape target with its health and last error.

    Returns [] rather than raising when Prometheus is unreachable: this backs a
    table beside panels that already report the outage, and a second identical
    error message adds nothing.
    """
    return _targets(include_up=True)


def down_targets() -> list[dict]:
    """Scrape targets currently not up, with the reason Prometheus recorded."""
    return _targets(include_up=False)


def _targets(include_up: bool) -> list[dict]:
    try:
        data = _fetch("/api/v1/targets", {"state": "active"}, DEFAULT_TIMEOUT)
    except PrometheusUnavailable:
        return []
    out = []
    for t in data.get("activeTargets") or []:
        health = t.get("health") or "unknown"
        if health == "up" and not include_up:
            continue
        labels = t.get("labels") or {}
        out.append(
            {
                "job": labels.get("job") or "—",
                "instance": labels.get("instance") or "—",
                "health": health,
                "error": (t.get("lastError") or "")[:200],
            }
        )
    out.sort(key=lambda r: (r["health"] == "up", r["job"], r["instance"]))
    return out
