# Local Run Guide

## 1. Create a Minikube cluster with Calico

On Apple Silicon, Docker images should usually be built for `linux/arm64` for local testing.

```bash
minikube start --cni=calico
```

Check the cluster:

```bash
kubectl get nodes
kubectl get pods -A
kubectl get crd | grep -i networkpolicies
```

You should see the Calico CRD for `networkpolicies.crd.projectcalico.org`.

## 2. Install dependencies and run tests

```bash
cd python
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
python3 tests.py -v
```

The current tests are unchanged and still validate:
- Kubernetes version retrieval
- `/healthz`

## 3. Run locally against Minikube

```bash
python3 main.py --kubeconfig ~/.kube/config --address 127.0.0.1:8080
```

Test endpoints:

```bash
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/readyz | jq
curl http://127.0.0.1:8080/deployments/health | jq
curl http://127.0.0.1:8080/networkpolicies | jq
curl "http://127.0.0.1:8080/networkpolicies?managed_only=true" | jq
```

## 4. Create a bidirectional block policy

The service creates two deny policies so the selected workloads cannot talk to each other in either direction.

```bash
curl -X POST http://127.0.0.1:8080/networkpolicies \
  -H "Content-Type: application/json" \
  -d '{
    "name": "team-a-to-team-b",
    "sourceNamespace": "team-a",
    "sourceLabels": {
      "app": "api-a"
    },
    "targetNamespace": "team-b",
    "targetLabels": {
      "app": "api-b"
    }
  }' | jq
```

List only managed policies:

```bash
curl "http://127.0.0.1:8080/networkpolicies?managed_only=true" | jq
```

Delete one of the created policies:

```bash
curl -X DELETE http://127.0.0.1:8080/networkpolicies/team-a-to-team-b-source-egress | jq
```

## 5. Build the local Docker image

From the `python/` folder:

```bash
docker build --platform linux/arm64 -t tyk-sre-tool:local .
```

If you want Minikube to use the local image without pushing to Docker Hub:

```bash
minikube image load tyk-sre-tool:local
```



