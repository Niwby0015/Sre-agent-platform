"""Change Executor subagent — applies approved changes with HITL for every write operation.

In the default (supervised) mode every write pauses for a human decision. With
``HITL_ENABLED=false`` the agent runs unattended: the interrupts are gone, and in
exchange the executor owns verification and recovery — it must confirm its own
change landed and try a different approach when it did not (see ``_RETRY_POLICY``).
"""
from config import AUTONOMOUS_RETRY_LIMIT, HITL_ENABLED, MODEL
from tools import (
    # Read tools for verification
    kubectl_get_pods,
    kubectl_get_deployments,
    kubectl_get_hpa,
    kubectl_top_pods,
    kubectl_describe_deployment,
    kubectl_get_custom_resources,
    kubectl_get_secret,
    # Read tools added because the executor is the only agent that can write,
    # and it kept being asked to verify or inspect something it could not see.
    # Reported verbatim in the evaluation: "I have no kubectl get events,
    # kubectl describe pod" and "No tool exposes the pod-template scheduling
    # fields". Every repair that needed a readback stalled at that boundary.
    kubectl_get_resource,
    kubectl_describe_pod,
    kubectl_get_events,
    kubectl_get_pod_logs,
    kubectl_get_endpoints,
    kubectl_get_services,
    kubectl_get_configmap,
    # Write tools — ALL require HITL approval
    kubectl_scale_deployment,
    kubectl_scale_bulk,
    kubectl_delete_resources_bulk,
    kubectl_delete_resource,
    kubectl_delete_custom_resource,
    kubectl_patch_resource_limits,
    kubectl_patch_hpa,
    kubectl_patch_secret,
    kubectl_patch_configmap,
    kubectl_delete_pod,
    kubectl_apply_manifest,
    kubectl_patch_deployment,
    kubectl_cordon_node,
    kubectl_uncordon_node,
    kubectl_rollout_restart,
    kubectl_resize_pvc,
    kubectl_rollback_deployment,
)

# All write tool names require human approval
CHANGE_EXECUTOR_INTERRUPT_ON = {
    "kubectl_scale_deployment": True,
    "kubectl_scale_bulk": True,
    "kubectl_delete_resources_bulk": True,
    "kubectl_delete_resource": True,
    "kubectl_delete_custom_resource": True,
    "kubectl_patch_resource_limits": True,
    "kubectl_patch_hpa": True,
    "kubectl_patch_secret": True,
    "kubectl_patch_configmap": True,
    "kubectl_delete_pod": True,
    "kubectl_apply_manifest": True,
    "kubectl_patch_deployment": True,
    "kubectl_cordon_node": True,
    "kubectl_uncordon_node": True,
    "kubectl_rollout_restart": True,
    "kubectl_resize_pvc": True,
    "kubectl_rollback_deployment": True,
}

# The one guideline that has to differ between supervised and unattended runs.
# Supervised, a failed change is a finding to hand back — retrying would mean
# mutating a live cluster further while a human is already watching. Unattended,
# nobody hands anything back, so stopping at the first failure converts a
# recoverable mistake into a failed run. The retries are bounded because an
# unbounded "keep trying" loop on a real cluster is its own kind of incident.
_RETRY_POLICY = (
    "- If a change fails, stop and report the error — do not retry automatically\n"
    if HITL_ENABLED
    else (
        "- After every change, VERIFY it worked: read the resource back and check the\n"
        "  pods/endpoints it affects. An applied change is not a working change.\n"
        "- If verification fails, the hypothesis was wrong — form a new one and apply a\n"
        "  DIFFERENT fix. Never repeat a change that already failed.\n"
        f"- At most {AUTONOMOUS_RETRY_LIMIT} distinct approaches per goal. After that, stop\n"
        "  and report what you tried and what each attempt showed.\n"
    )
)

# Same split, for the moment of execution. Supervised, the pause is real and the
# model should expect it rather than read it as a failed call. Unattended, the
# opposite: a model told to expect an approval that never arrives tends to
# announce it is waiting and stop, which is indistinguishable from a hang.
_EXECUTE_STEP = (
    "2. EXECUTE: apply the change (this will pause for human approval)\n"
    if HITL_ENABLED
    else "2. EXECUTE: apply the change — it takes effect immediately, there is no approval step\n"
)

change_executor_subagent = {
    "name": "change-executor",
    "model": MODEL,
    "description": (
        "Execute approved Kubernetes changes: scaling deployments, updating resource "
        "limits, patching HPAs, restarting workloads, and applying manifests. "
        "Every write operation requires explicit human approval before execution. "
        "Verifies state before and after each change."
    ),
    "system_prompt": (
        "You are a Kubernetes change execution specialist. You apply changes safely.\n\n"
        "## Bulk operations (same action on multiple resources)\n"
        "When scaling multiple deployments/statefulsets: use kubectl_scale_bulk with a JSON array "
        "of all targets — this is ONE tool call and ONE approval for the entire batch.\n"
        "When deleting multiple resources: group them into as few calls as possible into "
        "kubectl_delete_resources_bulk call with one JSON array containing everything "
        "(deployments, statefulsets, pvcs, services, configmaps, pods, etc. mixed together). "
        "Do NOT split by resource type. Batches are capped at BULK_DELETE_MAX targets, so if a "
        "request exceeds that, split it into several calls rather than trying to force one. "
        "Deletions targeting protected namespaces are refused outright.\n"
        "NEVER call kubectl_delete_resources_bulk or kubectl_scale_bulk more than once "
        "for the same user request.\n\n"
        "## Operator-managed / custom resources\n"
        "If resources keep getting recreated after you delete them, or they have "
        "ownerReferences to a custom resource, an operator is reconciling them. Deleting "
        "the child Deployments/Pods is futile — delete the OWNING custom resource instead "
        "with kubectl_delete_custom_resource (or a resource_type:'custom' entry in "
        "kubectl_delete_resources_bulk), which stops reconciliation and cascade-deletes "
        "children via ownerReferences. Use kubectl_get_custom_resources / kubectl_get_crds "
        "(from the main agent) to find the CR's group, version, and plural.\n\n"
        "## Single changes\n"
        "For individual changes:\n"
        "1. BEFORE: read current state (describe the resource to be modified)\n"
        + _EXECUTE_STEP
        + "3. AFTER: verify the change was applied correctly\n"
        "4. REPORT: summarize what was done and the new state\n\n"
        "## Guidelines\n"
        "- For scaling UP: check pod readiness after the change\n"
        "- For resource limits: describe the deployment after patching\n"
        "- For pod deletes: verify the new pod starts healthy\n"
        + _RETRY_POLICY
        + "- Use rollout_restart instead of delete for graceful workload restarts\n"
        "Always include the before/after state in your final report."
    ),
    "tools": [
        # Read tools for pre/post verification
        kubectl_get_pods,
        kubectl_get_deployments,
        kubectl_get_hpa,
        kubectl_top_pods,
        kubectl_describe_deployment,
        kubectl_get_custom_resources,
        kubectl_get_secret,
        kubectl_get_resource,
        kubectl_describe_pod,
        kubectl_get_events,
        kubectl_get_pod_logs,
        kubectl_get_endpoints,
        kubectl_get_services,
        kubectl_get_configmap,
        # Write tools (all require HITL)
        kubectl_scale_deployment,
        kubectl_scale_bulk,
        kubectl_delete_resources_bulk,
        kubectl_delete_resource,
        kubectl_delete_custom_resource,
        kubectl_patch_resource_limits,
        kubectl_patch_hpa,
        kubectl_patch_secret,
        kubectl_patch_configmap,
        kubectl_delete_pod,
        kubectl_apply_manifest,
        kubectl_patch_deployment,
        kubectl_cordon_node,
        kubectl_uncordon_node,
        kubectl_rollout_restart,
        kubectl_resize_pvc,
        kubectl_rollback_deployment,
    ],
    # Dropped entirely in unattended mode rather than auto-approved on resume:
    # the eval harness would otherwise pay a checkpoint write per write call, and
    # any interrupt that never gets resumed strands the run with no output.
    "interrupt_on": CHANGE_EXECUTOR_INTERRUPT_ON if HITL_ENABLED else {},
}
