---
chunk_id: resource-ephemeral-storage
source: pod-oomkilled.md
source_url: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
title: "Resource Management for Pods and Containers"
title_path: "Resource Management for Pods and Containers > Local ephemeral storage"
kind: runbook
resource_kind: Pod
severity: warning
---

## Local ephemeral storage

For general concepts about local ephemeral storage and hints about
configuring the requests and/or limits of ephemeral storage for a container,
please check the [local ephemeral storage](/docs/concepts/storage/ephemeral-storage/)
page.

### Resource monitoring for local ephemeral storage

The kubelet can measure how much local ephemeral storage is being used. It 
does this as long as you have enabled local ephemeral storage capacity isolation.

Kubernetes tracks the amount of ephemeral storage a Pod uses from the following:
* Writing to the container's writable layer (rootfs), container images, or both.
* Writing to local `emptyDir` volumes.
* The Pod's own logs (usually stored under `/var/log/pods`).
* System files managed by Kubernetes that are mapped into the Pod, such as `/etc/hosts`.
