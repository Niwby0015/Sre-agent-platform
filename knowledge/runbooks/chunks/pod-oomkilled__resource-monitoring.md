---
chunk_id: resource-monitoring
source: pod-oomkilled.md
source_url: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
title: "Resource Management for Pods and Containers"
title_path: "Resource Management for Pods and Containers > Monitoring compute & memory resource usage"
kind: runbook
resource_kind: Pod
severity: warning
---

### Monitoring compute & memory resource usage

The kubelet reports the resource usage of a Pod as part of the Pod
[`status`](/docs/concepts/overview/working-with-objects/#object-spec-and-status).

If optional [tools for monitoring](/docs/tasks/debug/debug-cluster/resource-usage-monitoring/)
are available in your cluster, then Pod resource usage can be retrieved either
from the [Metrics API](/docs/tasks/debug/debug-cluster/resource-metrics-pipeline/#metrics-api)
directly or from your monitoring tools.

### Considerations for memory backed `emptyDir` volumes {#memory-backed-emptydir}

When the `InPlacePodVerticalScalingMemoryBackedVolumes` feature gate is enabled, you can dynamically adjust the `sizeLimit` of a memory-backed (`medium: Memory`) `emptyDir` volume on a running Pod without requiring Pod recreation or container restarts. For step-by-step instructions, see [Resize CPU and Memory Resources assigned to Containers](/docs/tasks/configure-pod-container/resize-container-resources/#resizing-memory-backed-emptydir-volumes).

If you do not specify a `sizeLimit` for an `emptyDir` volume, that volume may
consume up to that pod's memory limit (`Pod.spec.containers[].resources.limits.memory`).
If you do not set a memory limit, the pod has no upper bound on memory consumption,
and can consume all available memory on the node. Kubernetes schedules pods based
on resource requests (`Pod.spec.containers[].resources.requests`) and will not
consider memory usage above the request when deciding if another pod can fit on
a given node. This can result in a denial of service and cause the OS to do
out-of-memory (OOM) handling. It is possible to create any number of `emptyDir`s
that could potentially consume all available memory on the node, making OOM
more likely.

From the perspective of memory management, there are some similarities between
when a process uses memory as a work area and when using memory-backed
`emptyDir`. But when using memory as a volume, like memory-backed `emptyDir`,
there are additional points below that you should be careful of:

* Files stored on a memory-backed volume are almost entirely managed by the
  user application. Unlike when used as a work area for a process, you can not
  rely on things like language-level garbage collection.
* The purpose of writing files to a volume is to save data or pass it between
  applications. Neither Kubernetes nor the OS may automatically delete files
  from a volume, so memory used by those files can not be reclaimed when the
  system or the pod are under memory pressure.
* A memory-backed `emptyDir` is useful because of its performance, but memory
  is generally much smaller in size and much higher in cost than other storage
  media, such as disks or SSDs. Using large amounts of memory for `emptyDir`
  volumes may affect the normal operation of your pod or of the whole node,
  so should be used carefully.

If you are administering a cluster or namespace, you can also set
[ResourceQuota](/docs/concepts/policy/resource-quotas/) that limits memory use;
you may also want to define a [LimitRange](/docs/concepts/policy/limit-range/)
for additional enforcement.
If you specify a `spec.containers[].resources.limits.memory` for each Pod,
then the maximum size of an `emptyDir` volume will be the pod's memory limit.

As an alternative, a cluster administrator can enforce size limits for
`emptyDir` volumes in new Pods using a policy mechanism such as
[ValidatingAdmissionPolicy](/docs/reference/access-authn-authz/validating-admission-policy).
