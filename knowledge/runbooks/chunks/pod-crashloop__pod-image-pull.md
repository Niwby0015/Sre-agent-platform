---
chunk_id: pod-image-pull
source: pod-crashloop.md
source_url: https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/
title: "Debug Pods"
title_path: "Debug Pods > My pod stays waiting"
kind: runbook
resource_kind: Pod
severity: warning
---

#### My pod stays waiting

If a Pod is stuck in the `Waiting` state, then it has been scheduled to a worker node, 
but it can't run on that machine. Again, the information from `kubectl describe ...`
should be informative. The most common cause of `Waiting` pods is a failure to pull the image.
There are three things to check:

* Make sure that you have the name of the image correct.
* Have you pushed the image to the registry?
* Try to manually pull the image to see if the image can be pulled. For example,
  if you use Docker on your PC, run `docker pull <image>`.
