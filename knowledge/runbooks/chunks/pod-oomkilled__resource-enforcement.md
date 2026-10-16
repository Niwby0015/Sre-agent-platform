---
chunk_id: resource-enforcement
source: pod-oomkilled.md
source_url: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
title: "Resource Management for Pods and Containers"
title_path: "Resource Management for Pods and Containers > How Kubernetes applies resource requests and limits"
kind: runbook
resource_kind: Pod
severity: warning
---

## How Kubernetes applies resource requests and limits {#how-pods-with-resource-limits-are-run}

When the kubelet starts a container as part of a Pod, the kubelet passes that container's
requests and limits for memory and CPU to the container runtime.

On Linux, the container runtime typically configures
kernel  that apply and enforce the
limits you defined.

- The CPU limit defines a hard ceiling on how much CPU time the container can use.
  During each scheduling interval (time slice), the Linux kernel checks to see if this
  limit is exceeded; if so, the kernel waits before allowing that cgroup to resume execution.
- The CPU request typically defines a weighting. If several different containers (cgroups)
  want to run on a contended system, workloads with larger CPU requests are allocated more
  CPU time than workloads with small requests.
- The memory request is mainly used during (Kubernetes) Pod scheduling. On a node that uses
  cgroups v2, the container runtime might use the memory request as a hint to set
  `memory.min` and `memory.low`.
- The memory limit defines a memory limit for that cgroup. If the container tries to
  allocate more memory than this limit, the Linux kernel out-of-memory subsystem activates
  and, typically, intervenes by stopping one of the processes in the container that tried
  to allocate memory. If that process is the container's PID 1, and the container is marked
  as restartable, Kubernetes restarts the container.
- The memory limit for the Pod or container can also apply to pages in memory backed
  volumes, such as an `emptyDir`. The kubelet tracks `tmpfs` emptyDir volumes as container
  memory use, rather than as local [ephemeral storage](/docs/concepts/storage/ephemeral-storage/).
  When using memory backed `emptyDir`,
  be sure to check the notes [below](#memory-backed-emptydir).

If a container exceeds its memory request and the node that it runs on becomes short of
memory overall, it is likely that the Pod the container belongs to will be
.

A container might or might not be allowed to exceed its CPU limit for extended periods of time.
However, container runtimes don't terminate Pods or containers for excessive CPU usage.

To determine whether a container cannot be scheduled or is being killed due to resource limits,
see the [Troubleshooting](#troubleshooting) section.

### Resizing container resources

After creating a Pod, you may need to adjust its CPU or memory resources based on
actual usage patterns. Kubernetes provides two approaches for resizing Pod resources:

#### In-place resize {#pod-resize-inplace}

You can modify the CPU and memory `requests` and `limits` of containers
in a running Pod without recreating it. This is called _in-place Pod vertical scaling_
or _in-place Pod resize_. To perform an in-place resize, update the container's resource
specifications using the Pod's `/resize` subresource. You can control whether a container
restart is required by setting the `resizePolicy` field in the container specification.

In-place resize currently applies to container-level resources. For resizing Pod-level
resources, see [Resize Pod CPU and Memory Resources](/docs/tasks/configure-pod-container/resize-pod-resources/).

When the `InPlacePodVerticalScalingSchedulerPreemption` feature gate is enabled,
deferred in-place resize requests can trigger `kube-scheduler` to preempt
lower-priority Pods on the assigned node to make room for the resize.
For more details, see
[Preemption for in-place Pod resize](/docs/concepts/scheduling-eviction/pod-priority-preemption/#preemption-for-in-place-pod-resize).

#### Resizing by launching replacement Pods

The cloud native approach to changing a Pod's resources is to update the Pod template
in the workload object (such as a Deployment or StatefulSet) and let the workload's
controller replace Pods with new ones that have the updated resources. This approach
works with any Kubernetes version and can change any Pod specification.

For more details about Pod resizing, see [Resizing Pods](/docs/concepts/workloads/pods/pod-lifecycle/#pod-resize).
For detailed instructions on in-place resize, see
[Resize CPU and Memory Resources assigned to Containers](/docs/tasks/configure-pod-container/resize-container-resources/).
You can also use the [Vertical Pod Autoscaler](/docs/concepts/workloads/autoscaling/vertical-pod-autoscale/)
to automatically manage Pod resource recommendations.
