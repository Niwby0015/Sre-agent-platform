---
chunk_id: service-endpoints
source: pod-crashloop.md
source_url: https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/
title: "Debug Pods"
title_path: "Debug Pods > Debugging Services"
kind: runbook
resource_kind: Pod
severity: warning
---

### Debugging Services

Services provide load balancing across a set of pods. There are several common problems that can make Services
not work properly.  The following instructions should help debug Service problems.

First, verify that there are endpoints for the service. For every Service object,
the apiserver makes one or more `EndpointSlice` resources available.

You can view these resources with:

```shell
kubectl get endpointslices -l kubernetes.io/service-name=${SERVICE_NAME}
```

Make sure that the endpoints in the EndpointSlices match up with the number of pods that you expect to be members of your service.
For example, if your Service is for an nginx container with 3 replicas, you would expect to see three different
IP addresses in the Service's endpoint slices.

#### My service is missing endpoints

If you are missing endpoints, try listing pods using the labels that Service uses.
Imagine that you have a Service where the labels are:

```yaml
...
spec:
  - selector:
     name: nginx
     type: frontend
```

You can use:

```shell
kubectl get pods --selector=name=nginx,type=frontend
```

to list pods that match this selector. Verify that the list matches the Pods that you expect to provide your Service.
Verify that the pod's `containerPort` matches up with the Service's `targetPort`
