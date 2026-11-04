#!/usr/bin/env bash
# kind 클러스터 + Online Boutique + kube-prometheus-stack + RBAC 설치.
# 모든 kubectl/helm 호출에 --context를 명시해 다른 클러스터를 건드리지 않는다.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER="sre-copilot"
CTX="kind-${CLUSTER}"
APP_NS="online-boutique"
KCFG_DIR="${ROOT}/.kube"

if ! kind get clusters 2>/dev/null | grep -qx "${CLUSTER}"; then
  echo "[setup] kind 클러스터 생성: ${CLUSTER}"
  kind create cluster --config "${ROOT}/infra/kind-config.yaml"
else
  echo "[setup] 클러스터 ${CLUSTER} 이미 존재, 생성 생략"
fi

echo "[setup] Online Boutique 배포"
kubectl --context "${CTX}" create namespace "${APP_NS}" --dry-run=client -o yaml | kubectl --context "${CTX}" apply -f -
kubectl --context "${CTX}" apply -n "${APP_NS}" -f "${ROOT}/infra/online-boutique.yaml"

echo "[setup] kube-prometheus-stack 설치"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null
helm --kube-context "${CTX}" upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  -f "${ROOT}/infra/monitoring-values.yaml" --wait --timeout 10m

echo "[setup] RBAC 적용 및 SA 전용 kubeconfig 생성"
kubectl --context "${CTX}" apply -f "${ROOT}/infra/rbac.yaml"
mkdir -p "${KCFG_DIR}"
SERVER="$(kubectl config view --raw --minify --context "${CTX}" -o jsonpath='{.clusters[0].cluster.server}')"
CA="$(kubectl config view --raw --minify --context "${CTX}" -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
for SA in sre-read sre-write; do
  TOKEN="$(kubectl --context "${CTX}" create token "${SA}" -n sre-copilot --duration=8760h)"
  OUT="${KCFG_DIR}/${SA#sre-}.kubeconfig"
  cat > "${OUT}" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: ${CLUSTER}
    cluster:
      server: ${SERVER}
      certificate-authority-data: ${CA}
users:
  - name: ${SA}
    user:
      token: ${TOKEN}
contexts:
  - name: ${SA}
    context:
      cluster: ${CLUSTER}
      user: ${SA}
current-context: ${SA}
EOF
  chmod 600 "${OUT}"
done

echo "[setup] Online Boutique 준비 대기"
kubectl --context "${CTX}" wait --for=condition=Available deployment --all -n "${APP_NS}" --timeout=600s
echo "[setup] 완료. Prometheus: http://localhost:9090"
