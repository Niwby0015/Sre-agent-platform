---
chunk_id: pod-controller
source: pod-crashloop.md
source_url: https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/
title: "Debug Pods"
title_path: "Debug Pods > Debugging Replication Controllers"
kind: runbook
resource_kind: Pod
severity: warning
---

### Debugging Replication Controllers

Replication controllers are fairly straightforward. They can either create Pods or they can't.
If they can't create pods, then please refer to the
[instructions above](#debugging-pods) to debug your pods.

You can also use `kubectl describe rc ${CONTROLLER_NAME}` to introspect events
related to the replication controller.
