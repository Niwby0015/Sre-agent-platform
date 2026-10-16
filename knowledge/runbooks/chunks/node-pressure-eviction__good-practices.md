---
chunk_id: good-practices
source: node-pressure-eviction.md
source_url: https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/
title: "Node-pressure Eviction"
title_path: "Node-pressure Eviction > Good practices"
kind: runbook
resource_kind: Node
severity: warning
---

## Good practices {#node-pressure-eviction-good-practices}

The following sections describe good practice for eviction configuration.

### Schedulable resources and eviction policies

When you configure the kubelet with an eviction policy, you should make sure that
the scheduler will not schedule pods if they will trigger eviction because they
immediately induce memory pressure.

Consider the following scenario:

- Node memory capacity: 10GiB
- Operator wants to reserve 10% of memory capacity for system daemons (kernel, `kubelet`, etc.)
- Operator wants to evict Pods at 95% memory utilization to reduce incidence of system OOM.

For this to work, the kubelet is launched as follows:

```none
--eviction-hard=memory.available<500Mi
--system-reserved=memory=1.5Gi
```

In this configuration, the `--system-reserved` flag reserves 1.5GiB of memory
for the system, which is `10% of the total memory + the eviction threshold amount`.

The node can reach the eviction threshold if a pod is using more than its request,
or if the system is using more than 1GiB of memory, which makes the `memory.available`
signal fall below 500MiB and triggers the threshold.

### DaemonSets and node-pressure eviction {#daemonset}

Pod priority is a major factor in making eviction decisions. If you do not want
the kubelet to evict pods that belong to a DaemonSet, give those pods a high
enough priority by specifying a suitable `priorityClassName` in the pod spec.
You can also use a lower priority, or the default, to only allow pods from that
DaemonSet to run when there are enough resources.
