---
chunk_id: node-conditions
source: node-pressure-eviction.md
source_url: https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/
title: "Node-pressure Eviction"
title_path: "Node-pressure Eviction > Node conditions"
kind: runbook
resource_kind: Node
severity: warning
---

## Node conditions {#node-conditions}

The kubelet reports [node conditions](/docs/concepts/architecture/nodes/#condition)
to reflect that the node is under pressure because hard or soft eviction
threshold is met, independent of configured grace periods.

The kubelet maps eviction signals to node conditions as follows:

| Node Condition    | Eviction Signal                                                                       | Description                                                                                |
|-------------------|---------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| `MemoryPressure`  | `memory.available`                                                                    | Available memory on the node has satisfied an eviction threshold                           |
| `DiskPressure`    | `nodefs.available`, `nodefs.inodesFree`, `imagefs.available`, `imagefs.inodesFree`, `containerfs.available`, or `containerfs.inodesFree` | Available disk space and inodes on either the node's root filesystem, image filesystem, or container filesystem has satisfied an eviction threshold              |
| `PIDPressure`     | `pid.available`                                                                       | Available processes identifiers on the (Linux) node has fallen below an eviction threshold |

The control plane also [maps](/docs/concepts/scheduling-eviction/taint-and-toleration/#taint-nodes-by-condition)
these node conditions to taints.

The kubelet updates the node conditions based on the configured
`--node-status-update-frequency`, which defaults to `10s`.

### Node condition oscillation

In some cases, nodes oscillate above and below soft eviction thresholds without
holding for the defined grace periods. This causes the reported node condition
to constantly switch between `true` and `false`, leading to bad eviction decisions.

To protect against oscillation, you can use the `eviction-pressure-transition-period`
flag, which controls how long the kubelet must wait before transitioning a node
condition to a different state. The transition period has a default value of `5m`.
