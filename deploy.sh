#!/usr/bin/env bash
# deploy.sh — build, push to ECR, and deploy the SRE bot to EKS
set -euo pipefail

# Target account, region, and cluster come from the environment so no
# account-specific identifiers live in this public repo. Export them once, e.g.
#   export AWS_ACCOUNT=123456789012 AWS_REGION=us-east-2 CLUSTER=my-cluster
AWS_ACCOUNT="${AWS_ACCOUNT:?set AWS_ACCOUNT, e.g. export AWS_ACCOUNT=123456789012}"
AWS_REGION="${AWS_REGION:-us-east-1}"
CLUSTER="${CLUSTER:?set CLUSTER, e.g. export CLUSTER=my-eks-cluster}"
ECR_REPO="${ECR_REPO:-sre-agent}"
IMAGE="${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"
TAG="${1:-latest}"

# The deployment spec must reference an IMMUTABLE tag. Pinning :latest meant the
# rendered manifest matched the live spec byte-for-byte, so kubectl apply made no
# change, no ReplicaSet was created, and the old pod kept running the old image
# while this script reported success. It also made "what is deployed" unanswerable
# from the cluster and made rollout undo meaningless, since every revision named
# the same mutable tag.
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo "")"
if [ -n "${GIT_SHA}" ] && ! printf '%s' "${GIT_SHA}" | grep -qE '^[0-9a-f]{7,40}$'; then
  echo "ERROR: git rev-parse returned an unexpected value. Aborting."
  exit 1
fi
# An explicit tag argument wins; otherwise deploy the commit.
if [ "$#" -ge 1 ]; then
  DEPLOY_TAG="$1"
elif [ -n "${GIT_SHA}" ]; then
  DEPLOY_TAG="${GIT_SHA}"
else
  echo "ERROR: not a git checkout and no tag argument given. Pass a tag explicitly."
  exit 1
fi
NAMESPACE="${NAMESPACE:-sre-agent}"

echo "==> Checking AWS auth..."
aws sts get-caller-identity --query 'Account' --output text > /dev/null

echo "==> Creating ECR repository (if not exists)..."
aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${AWS_REGION}" > /dev/null 2>&1 || \
  aws ecr create-repository \
    --repository-name "${ECR_REPO}" \
    --region "${AWS_REGION}" \
    --image-scanning-configuration scanOnPush=true \
    --output text > /dev/null

echo "==> Logging into ECR..."
aws ecr get-login-password --region "${AWS_REGION}" | \
  docker login --username AWS --password-stdin "${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo "==> Building image (${IMAGE}:${TAG})..."
docker build \
  --platform linux/amd64 \
  -t "${IMAGE}:${TAG}" \
  -t "${IMAGE}:$(git rev-parse --short HEAD 2>/dev/null || echo 'local')" \
  "$(dirname "$0")"

echo "==> Pushing image..."
docker push "${IMAGE}:${TAG}"
docker push "${IMAGE}:$(git rev-parse --short HEAD 2>/dev/null || echo 'local')" 2>/dev/null || true

echo "==> Ensuring kubectl context is set to ${CLUSTER}..."
aws eks update-kubeconfig --name "${CLUSTER}" --region "${AWS_REGION}"

echo "==> Checking secrets are populated..."
K8S_DIR="$(dirname "$0")/k8s"
if [ ! -f "${K8S_DIR}/secret.yaml" ]; then
  echo ""
  echo "ERROR: k8s/secret.yaml does not exist."
  echo "  cp k8s/secret.yaml.example k8s/secret.yaml   # then fill in the values"
  echo ""
  exit 1
fi
# REPLACE_ME is the sentinel in secret.yaml.example; REPLACE_WITH_BASE64 is the
# older one, still matched so an existing unfilled secret.yaml is caught too.
if grep -qE "REPLACE_ME|REPLACE_WITH_BASE64" "${K8S_DIR}/secret.yaml"; then
  echo ""
  echo "ERROR: k8s/secret.yaml still contains placeholder values."
  echo "Fill them in. secret.yaml.example uses stringData, so paste plaintext;"
  echo "if you switched it to data:, encode with printf (never echo without -n):"
  echo ""
  echo "  printf '%s' 'sk-ant-...' | base64   # ANTHROPIC_API_KEY"
  echo "  printf '%s' 'lsv2_pt_...' | base64   # LANGSMITH_API_KEY"
  echo "  printf '%s' 'xoxb-...'    | base64   # SLACK_BOT_TOKEN"
  echo "  printf '%s' 'xapp-...'    | base64   # SLACK_APP_TOKEN"
  echo ""
  exit 1
fi

# deployment.yaml ships a placeholder image so no account-specific registry is
# committed. Substitute it BEFORE applying: patching afterwards created a
# ReplicaSet with an unparseable image, which failed InvalidImageName and stayed
# in the rollout history as a broken `kubectl rollout undo` target.
echo "==> Rendering manifests with image ${IMAGE}:${DEPLOY_TAG}..."
PLACEHOLDER="REPLACE_WITH_YOUR_REGISTRY/sre-agent:latest"
RENDERED="$(kubectl kustomize "${K8S_DIR}")"
SUBBED="$(printf '%s' "${RENDERED}" | sed "s|${PLACEHOLDER}|${IMAGE}:${DEPLOY_TAG}|g")"

# Fail closed on both halves: the placeholder must be gone, and the real image
# must be present. Either check alone would let a renamed placeholder through and
# silently deploy whatever image happened to be in the manifest.
if printf '%s' "${SUBBED}" | grep -q "REPLACE_WITH_YOUR_REGISTRY"; then
  echo "ERROR: image placeholder still present after substitution. Aborting."
  exit 1
fi
if ! printf '%s' "${SUBBED}" | grep -qF "${IMAGE}:${DEPLOY_TAG}"; then
  echo "ERROR: expected image ${IMAGE}:${DEPLOY_TAG} not found in rendered manifests."
  echo "       Has the placeholder in k8s/deployment.yaml been renamed?"
  exit 1
fi

echo "==> Applying Kubernetes manifests..."
# Captured before the apply, so a genuine no-op is distinguishable from a rollout.
GEN_BEFORE="$(kubectl get deploy sre-agent -n "${NAMESPACE}" \
  -o jsonpath='{.metadata.generation}' 2>/dev/null || echo 0)"

printf '%s' "${SUBBED}" | kubectl apply -f -

echo "==> Waiting for rollout..."
kubectl rollout status deployment/sre-agent -n "${NAMESPACE}" --timeout=120s

# A deploy that changes nothing is legitimate (same commit redeployed), but it must
# be stated, not implied by a success message.
RUNNING_IMAGE="$(kubectl get deploy sre-agent -n "${NAMESPACE}" \
  -o jsonpath='{.spec.template.spec.containers[0].image}')"
echo "==> Deployment is running: ${RUNNING_IMAGE}"
if [ "${RUNNING_IMAGE}" != "${IMAGE}:${DEPLOY_TAG}" ]; then
  echo "ERROR: deployment image is ${RUNNING_IMAGE}, expected ${IMAGE}:${DEPLOY_TAG}."
  exit 1
fi
if [ "${GEN_BEFORE}" = "$(kubectl get deploy sre-agent -n "${NAMESPACE}" -o jsonpath='{.metadata.generation}')" ]; then
  echo "    NOTE: spec unchanged, so no new pod was created. This commit was already deployed."
fi

echo "==> Deployment status:"
kubectl get pods -n "${NAMESPACE}"

echo ""
echo "Done! To access the web UI:"
echo "  kubectl port-forward svc/sre-agent 8080:80 -n ${NAMESPACE}"
echo "  open http://localhost:8080"
