---
chunk_id: eviction-signals-filesystem
source: node-pressure-eviction.md
source_url: https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/
title: "Node-pressure Eviction"
title_path: "Node-pressure Eviction > Filesystem signals"
kind: runbook
resource_kind: Node
severity: warning
---

#### Filesystem signals

The kubelet recognizes three specific filesystem identifiers that can be used with
eviction signals (`<identifier>.inodesFree` or `<identifier>.available`):

1. `nodefs`: The node's main filesystem, used for local disk volumes,
    emptyDir volumes not backed by memory, log storage, ephemeral storage,
    and more. For example, `nodefs` contains `/var/lib/kubelet`.

1. `imagefs`: An optional filesystem that container runtimes can use to store
   container images (which are the read-only layers). If there is no separate
   `containerfs`, the image filesystem also stores container writable layers.

1. `containerfs`: An optional filesystem that container runtimes can use to
   store container writable layers. When `containerfs` is used, the `imagefs`
   filesystem can be split to only store images (read-only layers) and nothing
   else.

These identifiers describe the filesystems as the kubelet observes them. They do
not always mean three different mount points: in common layouts, two or all
three identifiers can refer to the same underlying filesystem.

The _split image filesystem_ feature adds new eviction signals, thresholds, and
metrics for `containerfs`. To use `containerfs`, the Kubernetes release
v requires the `KubeletSeparateDiskGC`
[feature gate](/docs/reference/command-line-tools-reference/feature-gates/) to
be enabled. For Kubernetes v, only CRI-O (v1.29 or
higher) offers `containerfs` filesystem support.

The kubelet supports three common layouts for container filesystems:

- Everything is on the single `nodefs`, also referred to as "rootfs" or
  simply "root". In this layout, `nodefs`, `imagefs`, and `containerfs`
  all refer to the same underlying filesystem.

- Container runtime storage is on a dedicated disk, separate from the root
  filesystem. In this layout, `imagefs` and `containerfs` refer to the same
  underlying filesystem, which stores both image layers and container writable
  layers. This is often referred to as "split disk" (or "separate disk")
  filesystem.

- Container writable layers are on `nodefs`, and the container images
  (read-only layers) are stored on a separate `imagefs`. In this layout,
  `containerfs` and `nodefs` refer to the same underlying filesystem. This is
  often referred to as a "split image" filesystem.

The kubelet will attempt to auto-discover these filesystems with their current
configuration directly from the underlying container runtime and will ignore
other local node filesystems.

The kubelet does not support other container filesystems or storage configurations,
and it does not currently support multiple filesystems for images and containers.
