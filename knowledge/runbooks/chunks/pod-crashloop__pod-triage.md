---
chunk_id: pod-triage
source: pod-crashloop.md
source_url: https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/
title: "Debug Pods"
title_path: "Debug Pods > Diagnosing the problem"
kind: runbook
resource_kind: Pod
severity: warning
---

## Diagnosing the problem

The first step in troubleshooting is triage. What is the problem?
Is it your Pods, your Replication Controller or your Service?

   * [Debugging Pods](#debugging-pods)
   * [Debugging Replication Controllers](#debugging-replication-controllers)
   * [Debugging Services](#debugging-services)

### Debugging Pods

The first step in debugging a Pod is taking a look at it. Check the current
state of the Pod and recent events with the following command:

```shell
kubectl describe pods ${POD_NAME}
```

Look at the state of the containers in the pod. Are they all `Running`?
Have there been recent restarts?

Continue debugging depending on the state of the pods.
