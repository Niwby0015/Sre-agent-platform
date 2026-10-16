---
chunk_id: pod-pending
source: pod-crashloop.md
source_url: https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/
title: "Debug Pods"
title_path: "Debug Pods > My pod stays pending"
kind: runbook
resource_kind: Pod
severity: warning
---

#### My pod stays pending

If a Pod is stuck in `Pending` it means that it can not be scheduled onto a node.
Generally this is because there are insufficient resources of one type or another
that prevent scheduling. Look at the output of the `kubectl describe ...` command above.
There should be messages from the scheduler about why it can not schedule your pod.
Reasons include:

* **You don't have enough resources**: You may have exhausted the supply of CPU
  or Memory in your cluster, in this case you need to delete Pods, adjust resource
  requests, or add new nodes to your cluster. See [Compute Resources document](/docs/concepts/configuration/manage-resources-containers/)
  for more information.

* **You are using `hostPort`**: When you bind a Pod to a `hostPort` there are a
  limited number of places that pod can be scheduled. In most cases, `hostPort`
  is unnecessary, try using a Service object to expose your Pod.  If you do require
  `hostPort` then you can only schedule as many Pods as there are nodes in your Kubernetes cluster.
