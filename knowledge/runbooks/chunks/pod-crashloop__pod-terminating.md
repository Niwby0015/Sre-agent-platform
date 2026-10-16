---
chunk_id: pod-terminating
source: pod-crashloop.md
source_url: https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/
title: "Debug Pods"
title_path: "Debug Pods > My pod stays terminating"
kind: runbook
resource_kind: Pod
severity: warning
---

#### My pod stays terminating

If a Pod is stuck in the `Terminating` state, it means that a deletion has been
issued for the Pod, but the control plane is unable to delete the Pod object.

This typically happens if the Pod has a [finalizer](/docs/concepts/overview/working-with-objects/finalizers/)
and there is an [admission webhook](/docs/reference/access-authn-authz/extensible-admission-controllers/)
installed in the cluster that prevents the control plane from removing the
finalizer.

To identify this scenario, check if your cluster has any
ValidatingWebhookConfiguration or MutatingWebhookConfiguration that target
`UPDATE` operations for `pods` resources.

If the webhook is provided by a third-party:
- Make sure you are using the latest version.
- Disable the webhook for `UPDATE` operations.
- Report an issue with the corresponding provider.

If you are the author of the webhook:
- For a mutating webhook, make sure it never changes immutable fields on
  `UPDATE` operations. For example, changes to containers are usually not allowed.
- For a validating webhook, make sure that your validation policies only apply
  to new changes. In other words, you should allow Pods with existing violations
  to pass validation. This allows Pods that were created before the validating
  webhook was installed to continue running.
