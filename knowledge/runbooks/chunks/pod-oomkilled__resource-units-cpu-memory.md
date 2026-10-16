---
chunk_id: resource-units-cpu-memory
source: pod-oomkilled.md
source_url: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
title: "Resource Management for Pods and Containers"
title_path: "Resource Management for Pods and Containers > Resource units in Kubernetes"
kind: runbook
resource_kind: Pod
severity: warning
---

## Resource units in Kubernetes

### CPU resource units {#meaning-of-cpu}

Limits and requests for CPU resources are measured in *cpu* units.
In Kubernetes, 1 CPU unit is equivalent to **1 physical CPU core**,
or **1 virtual core**, depending on whether the node is a physical host
or a virtual machine running inside a physical machine.

Fractional requests are allowed. When you define a container with
`spec.containers[].resources.requests.cpu` set to `0.5`, you are requesting half
as much CPU time compared to if you asked for `1.0` CPU.
For CPU resource units, the [quantity](/docs/reference/kubernetes-api/common-definitions/quantity/) expression `0.1` is equivalent to the
expression `100m`, which can be read as "one hundred millicpu". Some people say
"one hundred millicores", and this is understood to mean the same thing.

CPU resource is always specified as an absolute amount of resource, never as a relative amount. For example,
`500m` CPU represents the roughly same amount of computing power whether that container
runs on a single-core, dual-core, or 48-core machine.

Kubernetes doesn't allow you to specify CPU resources with a precision finer than
`1m` or `0.001` CPU. To avoid accidentally using an invalid CPU quantity, it's useful to specify CPU units using the milliCPU form 
instead of the decimal form when using less than 1 CPU unit. 

For example, you have a Pod that uses `5m` or `0.005` CPU and would like to decrease
its CPU resources. By using the decimal form, it's harder to spot that `0.0005` CPU
is an invalid value, while by using the milliCPU form, it's easier to spot that
`0.5m` is an invalid value.

### Memory resource units {#meaning-of-memory}

Limits and requests for `memory` are measured in bytes. You can express memory as
a plain integer or as a fixed-point number using one of these
[quantity](/docs/reference/kubernetes-api/common-definitions/quantity/) suffixes:
E, P, T, G, M, k. You can also use the power-of-two equivalents: Ei, Pi, Ti, Gi,
Mi, Ki. The Kubernetes API also allows m as a suffix (for millibytes: 1/1000 of a byte),
but this isn't useful to specify: you must always assign whole numbers of bytes, or sometimes larger chunks such as multiples of 1 gibibyte.

Here are some examples of memory quantities that represent roughly the same value:

```shell
128974848, 129e6, 129M,  128974848000m, 123Mi
```

Pay attention to the case of the suffixes. "M" means megabytes, while "m" means millibytes. If you request `400m` of memory, this is a request for 0.4 bytes. Someone who types that probably meant to ask for 400 mebibytes (`400Mi`)
or 400 megabytes (`400M`).
