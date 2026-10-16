"""Performance Analyzer subagent — CPU, memory, and latency analysis."""
from config import PROMETHEUS_RUN_LIMIT, RAG_SEARCH_RUN_LIMIT, SUBAGENT_MODEL
from langchain.agents.middleware import ToolCallLimitMiddleware
from tools import (
    kubectl_top_pods,
    kubectl_top_nodes,
    kubectl_get_pods,
    kubectl_describe_deployment,
    kubectl_get_deployments,
    kubectl_get_hpa,
    kubectl_get_namespaces,
    kubectl_get_resource_quotas,
    get_windows_host_metrics,
    search_knowledge,
    prometheus_query,
    prometheus_query_range,
    prometheus_list_metrics,
)

performance_analyzer_subagent = {
    "name": "performance-analyzer",
    "model": SUBAGENT_MODEL,
    "description": (
        "Analyze Kubernetes workload performance: CPU throttling, memory pressure, "
        "resource limits vs actual usage, OOM risk, and right-sizing recommendations. "
        "Returns actionable resource configuration changes."
    ),
    "system_prompt": (
        "You are a Kubernetes performance and resource optimization specialist.\n"
        "When asked to analyze performance:\n"
        "1. For a local Windows-host performance request, run get_windows_host_metrics. "
        "State clearly that its readings describe the computer running this agent, not Kubernetes nodes.\n"
        "2. For Kubernetes workload performance, start with the Prometheus tools: prometheus_query for\n"
        "   current usage and prometheus_query_range for the trend across the incident. PromQL that matches\n"
        "   this task:\n"
        "     sum by (pod) (rate(container_cpu_usage_seconds_total{container!=\"\"}[5m]))\n"
        "     sum by (pod) (container_memory_working_set_bytes{container!=\"\"})\n"
        "     100 - (avg(rate(node_cpu_seconds_total{mode=\"idle\"}[5m])) * 100)\n"
        "     (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100\n"
        "   kubectl top pods / kubectl top nodes read the metrics-server API, which may not be installed\n"
        "   on this cluster — if they report no metrics available, that is a missing dependency, not a\n"
        "   healthy workload, and Prometheus is the authoritative source.\n"
        "3. For each deployment, compare resource requests/limits vs actual usage:\n"
        "   - CPU limit too low → causes throttling → latency spikes\n"
        "   - Memory limit too low → OOMKilled → restarts\n"
        "   - CPU/memory requests too high → wastes capacity and blocks scheduling\n"
        "4. Check namespace resource quotas for constraints\n"
        "5. Identify:\n"
        "   - Pods using >80% of their CPU limit (throttling risk)\n"
        "   - Pods using >80% of their memory limit (OOM risk)\n"
        "   - Pods with no resource limits set (dangerous in shared clusters)\n"
        "   - Pods with requests >> actual usage (over-provisioned)\n"
        "   - Nodes with high memory pressure or CPU saturation\n"
        "6. If live evidence confirms node MemoryPressure, DiskPressure, eviction, or a concrete "
        "resource exhaustion incident, call search_knowledge once per distinct incident type for "
        "Runbook guidance. Do not call it for normal utilization or generic right-sizing advice.\n"
        "7. Return a structured report:\n"
        "   - PERFORMANCE ISSUES: ranked by severity (critical/warning/info)\n"
        "   - RIGHT-SIZING RECOMMENDATIONS: specific new values for requests/limits\n"
        "     Use actual usage as baseline, add 20-30% headroom\n"
        "   - NODE HEALTH: overall node resource pressure\n"
        "All recommendations must include specific resource names, containers, and values."
    ),
    "tools": [
        kubectl_get_namespaces,
        kubectl_get_deployments,
        kubectl_describe_deployment,
        kubectl_top_pods,
        kubectl_top_nodes,
        kubectl_get_pods,
        kubectl_get_hpa,
        kubectl_get_resource_quotas,
        get_windows_host_metrics,
        search_knowledge,
        prometheus_query,
        prometheus_query_range,
        prometheus_list_metrics,
    ],
    # A subagent does not inherit the parent's middleware, so every cap that
    # matters has to be declared here as well.
    "middleware": [
        ToolCallLimitMiddleware(
            tool_name="search_knowledge",
            run_limit=RAG_SEARCH_RUN_LIMIT,
            exit_behavior="continue",
        ),
        ToolCallLimitMiddleware(
            tool_name="prometheus_query",
            run_limit=PROMETHEUS_RUN_LIMIT,
            exit_behavior="continue",
        ),
        ToolCallLimitMiddleware(
            tool_name="prometheus_query_range",
            run_limit=PROMETHEUS_RUN_LIMIT,
            exit_behavior="continue",
        ),
    ],
}
