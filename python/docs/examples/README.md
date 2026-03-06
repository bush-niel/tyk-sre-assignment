# Testing by running the app from local

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
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
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


# Testing with app deployed into cluster via helm

## 1. Deploy the app using helm

```bash
helm upgrade --install tyk-sre-tool ./helm/tyk-sre-tool \
  --namespace tyk-sre-tool \
  --create-namespace \
  --set image.repository=bushniel/tyk-sre-tool \
  --set image.tag=latest
  ```

## 2. Create demo namespaces and workloads in minikube cluster

Create two namespaces:
```bash
kubectl create namespace team-a
kubectl create namespace team-b
```

Create two simple pods:
```bash
kubectl run api-a -n team-a --image=nginx --labels=app=api-a
kubectl run api-b -n team-b --image=nginx --labels=app=api-b

kubectl wait --for=condition=Ready pod/api-a -n team-a --timeout=120s
kubectl wait --for=condition=Ready pod/api-b -n team-b --timeout=120s
```

Test connectivity before the deny: This should succeed

```bash
kubectl exec -n team-a api-a -- curl -I --max-time 3 http://<api-b_POD_IP> || true
kubectl exec -n team-b api-b -- curl -I --max-time 3 http://<api-a_POD_IP> || true
```

Port forward to the sre tool to run the next set of curl commands:
```bash
kubectl port-forward -n tyk-sre-tool service/tyk-sre-tool 8080:8080
````

## 3. Create a bidirectional block policy

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
curl -X DELETE http://127.0.0.1:8080/networkpolicies/team-a-to-team-b | jq
```


