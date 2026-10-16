---
chunk_id: resource-requests-limits
source: pod-oomkilled.md
source_url: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
title: "Resource Management for Pods and Containers"
title_path: "Resource Management for Pods and Containers > Requests and limits"
kind: runbook
resource_kind: Pod
severity: warning
---

## Requests and limits

If the node where a Pod is running has enough of a resource available, it's possible (and
allowed) for a container to use more resource than its `request` for that resource specifies.

For example, if you set a `memory` request of 256 MiB for a container, and that container is in
a Pod scheduled to a Node with 8GiB of memory and no other Pods, then the container can try to use
more RAM.

Limits are a different story. Both `cpu` and `memory` limits are applied by the kubelet (and
),
and are ultimately enforced by the kernel. On Linux nodes, the Linux kernel
enforces limits with
.
The behavior of `cpu` and `memory` limit enforcement is slightly different.

`cpu` limits are enforced by CPU throttling. When a container approaches
its `cpu` limit, the kernel will restrict access to the CPU corresponding to the
container's limit. Thus, a `cpu` limit is a hard limit the kernel enforces.
Containers may not use more CPU than is specified in their `cpu` limit.

`memory` limits are enforced by the kernel with out of memory (OOM) kills. When
a container uses more than its `memory` limit, the kernel may terminate it. However,
terminations only happen when the kernel detects memory pressure. Thus, a
container that over allocates memory may not be immediately killed. This means
`memory` limits are enforced reactively. A container may use more memory than
its `memory` limit, but if it does, it may get killed.

There is a feature `MemoryQoS` which adds memory throttling and optional
tiered memory reservation on Linux nodes using cgroup v2. For details, see
[Memory QoS with cgroup v2](/docs/concepts/workloads/pods/pod-qos/#memory-qos-with-cgroup-v2).

If you specify a limit for a resource, but do not specify any request, and no admission-time
mechanism has applied a default request for that resource, then Kubernetes copies the limit
you specified and uses it as the requested value for the resource.
