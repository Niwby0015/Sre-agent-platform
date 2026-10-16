---
chunk_id: resource-types
source: pod-oomkilled.md
source_url: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
title: "Resource Management for Pods and Containers"
title_path: "Resource Management for Pods and Containers > Resource types"
kind: runbook
resource_kind: Pod
severity: warning
---

## Resource types

A *resource type* has a base unit and can be requested, limited, or both.
Kubernetes has the following built-in resource types:

| Resource type | Description | Base unit |
|---|---|---|
| `cpu` | Compute processing | cpu (core) |
| `memory` | RAM | Bytes |
| `ephemeral-storage` | [Local ephemeral storage](/docs/concepts/storage/ephemeral-storage/) | Bytes |
| `hugepages-<size>` | [Huge pages](#huge-pages) (Linux only) | Bytes |

Clusters can also provide
[extended resources](/docs/concepts/configuration/manage-resources-containers/#extended-resources)
(resources with a custom name, typically exposed by device plugins).

### Huge pages

For Linux workloads, you can specify _huge page_ resources.
Huge pages are a Linux-specific feature where the node kernel allocates blocks of memory
that are much larger than the default page size.

For example, on a system where the default page size is 4KiB, you could specify a limit,
`hugepages-2Mi: 80Mi`. If the container tries allocating over 40 2MiB huge pages (a
total of 80 MiB), that allocation fails.

You cannot overcommit `hugepages-*` resources.
This is different from the `memory` and `cpu` resources.

CPU and memory are collectively referred to as *compute resources*, or *resources*. Compute
resources are measurable quantities that can be requested, allocated, and
consumed. They are distinct from
[API resources](/docs/concepts/overview/kubernetes-api/). API resources, such as Pods and
[Services](/docs/concepts/services-networking/service/) are objects that can be read and modified
through the Kubernetes API server.
