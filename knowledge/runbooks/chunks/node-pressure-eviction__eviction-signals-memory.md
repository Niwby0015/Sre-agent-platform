---
chunk_id: eviction-signals-memory
source: node-pressure-eviction.md
source_url: https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/
title: "Node-pressure Eviction"
title_path: "Node-pressure Eviction > Memory signals"
kind: runbook
resource_kind: Node
severity: warning
---

#### Memory signals

On Linux nodes, the value for `memory.available` is derived from the cgroupfs instead of tools
like `free -m`. This is important because `free -m` does not work in a
container, and if users use the [node allocatable](/docs/tasks/administer-cluster/reserve-compute-resources/#node-allocatable)
feature, out of resource decisions
are made local to the end user Pod part of the cgroup hierarchy as well as the
root node. This [script](/examples/admin/resource/memory-available.sh) or
[cgroupv2 script](/examples/admin/resource/memory-available-cgroupv2.sh)
reproduces the same set of steps that the kubelet performs to calculate
`memory.available`. The kubelet excludes inactive_file (the number of bytes of
file-backed memory on the inactive LRU list) from its calculation, as it assumes that
memory is reclaimable under pressure.

On nodes with [hugepages](/docs/tasks/manage-hugepages/scheduling-hugepages/)
configured, the kubelet subtracts the node's total hugepage capacity from
`memory.available` used by eviction manager.
Without this adjustment, hugepage-reserved RAM inflates
`AvailableBytes` because the memory cgroup controller does not track hugetlb
allocations in its working set. This can delay eviction and lead to OOM kills.
To restore the previous behavior, disable the `HugepageAwareEviction`
[feature gate](/docs/reference/command-line-tools-reference/feature-gates/).

On Windows nodes, the value for `memory.available` is derived from the node's global
memory commit levels (queried through the [`GetPerformanceInfo()`](https://learn.microsoft.com/windows/win32/api/psapi/nf-psapi-getperformanceinfo)
system call) by subtracting the node's global [`CommitTotal`](https://learn.microsoft.com/windows/win32/api/psapi/ns-psapi-performance_information) from the node's [`CommitLimit`](https://learn.microsoft.com/windows/win32/api/psapi/ns-psapi-performance_information). Please note that `CommitLimit` can change if the node's page-file size changes!
