# tyk-sre-tool Helm Chart

This folder contains the Helm chart for installing the tyk-sre-tool into minikube

For NetworkPolicies to work correctly, we need to start minikube with Calico CNI.

```bash
minikube start --cni=calico
```

## Install with a Docker Hub image

```bash
helm upgrade --install tyk-sre-tool ./helm/tyk-sre-tool \
  --namespace tyk-sre-tool \
  --create-namespace \
  --set image.repository=<dockerhub-user>/tyk-sre-tool \
  --set image.tag=latest
```

## Install with a local Minikube image

If you build the image directly inside Minikube's Docker daemon, use a local tag and disable pull:

```bash
eval $(minikube docker-env)
docker build -t tyk-sre-tool:local .
helm upgrade --install tyk-sre-tool ./helm/tyk-sre-tool \
  --namespace tyk-sre-tool \
  --create-namespace \
  --set image.repository=tyk-sre-tool \
  --set image.tag=local \
  --set image.pullPolicy=Never
```

## Useful Helm operations

```bash
helm lint ./helm/tyk-sre-tool
helm template tyk-sre-tool ./helm/tyk-sre-tool
helm status tyk-sre-tool -n tyk-sre-tool
helm uninstall tyk-sre-tool -n tyk-sre-tool
```

