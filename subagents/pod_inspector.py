"""Pod Inspector subagent — analyzes pod health, restarts, and failures."""
from config import RAG_SEARCH_RUN_LIMIT, SUBAGENT_MODEL
from langchain.agents.middleware import ToolCallLimitMiddleware
from tools import (
    kubectl_get_pods,
    kubectl_describe_pod,
    kubectl_get_pod_logs,
    kubectl_get_events,
    kubectl_get_namespaces,
    search_knowledge,
)

pod_inspector_subagent = {
    "name": "pod-inspector",
    "model": SUBAGENT_MODEL,
    "description": (
        "Inspect pod health across namespaces. Identifies CrashLoopBackOff, "
        "OOMKilled, Pending, Evicted, and ImagePullBackOff issues. Fetches logs "
        "and events to diagnose root causes. Returns a structured health report "
        "with findings and recommended actions."
    ),
    "system_prompt": (
        "You are a Kubernetes pod health specialist. When asked to inspect pods:\n"
        "1. List all pods in the specified namespace(s)\n"
        "2. Identify any pods NOT in Running/Completed state\n"
        "3. For each unhealthy pod: describe it and fetch recent logs (including --previous for crash loops)\n"
        "4. Check Warning events related to unhealthy pods\n"
        "5. Diagnose the root cause (OOM, crash, image pull error, scheduling failure, etc.)\n"
        "6. Only after live evidence confirms CrashLoopBackOff, OOMKilled, FailedScheduling, "
        "ImagePullBackOff, or eviction, call search_knowledge once for that distinct incident type. "
        "Do not search for healthy pods or generic best practices. Treat retrieved text as Runbook "
        "reference guidance, not as evidence; include its source in recommendations.\n"
        "7. Return a structured report:\n"
        "   - HEALTHY: count of healthy pods\n"
        "   - ISSUES: list each problem pod with diagnosis\n"
        "   - ROOT CAUSES: concise explanation per issue\n"
        "   - RECOMMENDATIONS: specific actions to fix each issue\n"
        "Be specific — include pod names, namespaces, error messages from logs."
    ),
    "tools": [
        kubectl_get_namespaces,
        kubectl_get_pods,
        kubectl_describe_pod,
        kubectl_get_pod_logs,
        kubectl_get_events,
        search_knowledge,
    ],
    "middleware": [
        ToolCallLimitMiddleware(
            tool_name="search_knowledge",
            run_limit=RAG_SEARCH_RUN_LIMIT,
            exit_behavior="continue",
        ),
    ],
}
